#!/usr/bin/env python3
"""Fixed public HTTP/1.1 persistence feasibility test; never reconnect."""
import datetime as dt
import http.client
import json
import os
import socket
from pathlib import Path
import time
import urllib.parse

PROXY_NAMES = ('HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','NO_PROXY',
               'http_proxy','https_proxy','all_proxy','no_proxy')
LIMIT = 65536

class ReconnectForbidden(OSError):
    pass

class UnsupportedResolution(OSError):
    pass

class OneConnection(http.client.HTTPSConnection):
    def __init__(self):
        super().__init__('web.archive.org', timeout=30)  # Default verified TLS and HTTP/1.1 ALPN.
        self.connect_count = 0
        self.connection_open_count = 0
        self._create_connection = self.create_single_socket
    def create_single_socket(self, address, timeout=30, source_address=None):
        if self.connect_count:
            raise ReconnectForbidden('Second TCP attempt prohibited')
        host, port = address
        answers = {(family, kind, proto, sockaddr) for family, kind, proto, _, sockaddr
                   in socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)}
        if len(answers) != 1:
            raise UnsupportedResolution('Requires exactly one distinct DNS socket address')
        family, kind, proto, sockaddr = next(iter(answers))
        sock = socket.socket(family, kind, proto)
        try:
            sock.settimeout(timeout)
            if source_address is not None:
                sock.bind(source_address)
            self.connect_count += 1
            sock.connect(sockaddr)
            return sock
        except BaseException:
            sock.close()
            raise
    def connect(self):
        if self.connection_open_count:
            raise ReconnectForbidden('Second connection prohibited')
        self.connection_open_count += 1
        try:
            super().connect()
        finally:
            self.auto_open = 0


def targets():
    day = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d')
    query = urllib.parse.urlencode({'url':'https://somervillewatchco.com/pages/warranty-policy',
        'matchType':'exact','from':day+'000000','to':day+'235959','output':'json',
        'fl':'timestamp,original,statuscode,mimetype'})
    return (('system_status','/save/status/system',dict),
            ('warranty_cdx','/cdx/search/cdx?'+query,list))


def error_type(exc):
    result = {'class':type(exc).__name__}
    number = getattr(exc,'errno',None)
    if isinstance(number,int) and not isinstance(number,bool):result['errno']=number
    return result


def run():
    report = {'started_at_utc':dt.datetime.now(dt.timezone.utc).isoformat(),
        'proxy_environment_present':{name:name in os.environ for name in PROXY_NAMES},
        'requests':[], 'connect_count':0, 'outcome':'inconclusive',
        'policy':{'max_connections':1,'max_gets':2,'minimum_idle_seconds':120,
                  'redirects':False,'retries':False,'reconnect':False,'read_limit_bytes':LIMIT}}
    output = Path('persistent-connection-report.json')
    def save():output.write_text(json.dumps(report,indent=2))
    if any(report['proxy_environment_present'].values()):
        report['stop_reason']='proxy_environment_present_no_network'
        save();return report
    connection = OneConnection()
    first_socket = None
    try:
        for index,(label,path,expected) in enumerate(targets()):
            if index:
                time.sleep(120)  # Only after first response fully consumed.
                if connection.sock is None or connection.sock is not first_socket:
                    report['stop_reason']='socket_not_preserved_no_reconnect';break
            row={'endpoint':label,'method':'GET','phase':'send'}
            report['requests'].append(row)
            start=time.monotonic()
            response=None
            try:
                connection.request('GET',path,headers={'User-Agent':'SWC-Archive-Verification/1.0',
                                                     'Accept-Encoding':'identity','Accept':'*/*'})
                if index==0:first_socket=connection.sock
                row['socket_identity_same']=connection.sock is first_socket
                row['phase']='response'
                response=connection.getresponse()
                row['http_status']=response.status
                row['http_version']={10:'1.0',11:'1.1'}.get(response.version,'other')
                row['server_close']=bool(response.will_close or
                    'close' in [part.strip().lower() for part in response.getheader('Connection','').split(',')])
                row['retry_after_present']=response.getheader('Retry-After') is not None
                if response.status!=200 or response.version!=11 or row['retry_after_present'] or row['server_close']:
                    report['stop_reason']='server_response_disallows_continuation';break
                row['phase']='read'
                body=response.read(LIMIT+1)
                if len(body)>LIMIT:
                    report['stop_reason']='body_limit_exceeded';break
                # read(amt) does not reliably raise IncompleteRead. Remaining
                # declared length must be zero; then read EOF to finish framing.
                if response.length not in (None,0):
                    report['stop_reason']='incomplete_declared_body';break
                tail=response.read(1)
                if tail:
                    report['stop_reason']='body_not_fully_consumed';break
                row['body_fully_consumed']=True
                row['bytes_read']=len(body)
                obj=json.loads(body)
                row['schema_valid'] = (isinstance(obj, dict) and obj.get('status') == 'ok') if expected is dict else (
                    isinstance(obj, list) and (not obj or (isinstance(obj[0], list)
                    and {'timestamp','original','statuscode','mimetype'}.issubset(obj[0])
                    and all(isinstance(values, list) and len(values) == len(obj[0]) for values in obj[1:]))))
                if not row['schema_valid']:
                    report['stop_reason']='schema_invalid';break
                row['phase']='complete'
                row['outcome']='http_response'
            except Exception as exc:
                row['outcome']='error';row['error']=error_type(exc)
                report['stop_reason']='request_failed_no_reconnect';break
            finally:
                row['elapsed_seconds']=round(time.monotonic()-start,3)
                report['connect_count']=connection.connect_count
                if response is not None:response.close()
                save()
        else:
            report['outcome']='reuse_observed'
    except Exception as exc:
        report['stop_reason']='diagnostic_exception';report['error']=error_type(exc)
    finally:
        report['connect_count']=connection.connect_count
        try:
            connection.close()
        except Exception as exc:
            report['close_error'] = error_type(exc)
            report['outcome'] = 'inconclusive'
        report['completed_at_utc']=dt.datetime.now(dt.timezone.utc).isoformat()
        save()
    return report

if __name__=='__main__':
    run()
    print('Sanitized persistence feasibility report: persistent-connection-report.json')
