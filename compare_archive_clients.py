#!/usr/bin/env python3
"""Public GET-only native-client comparison. No capture submissions or credentials."""
import datetime as dt
import ipaddress
import json
import os
from pathlib import Path
import re
import ssl
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from diagnose_transport import endpoints, NoRedirect, error_metadata, retry_delay, PROXY_NAMES

UA = 'SWC-Archive-Verification/1.0'
HEADERS = {'User-Agent': UA, 'Accept': '*/*', 'Accept-Encoding': 'identity'}
FIXED_ENDPOINTS = endpoints()[:2]


def peer_family(address):
    try:
        return 'IPv' + str(ipaddress.ip_address(address).version)
    except ValueError:
        return 'unknown'


def python_get(url):
    if url not in [value for _, value in FIXED_ENDPOINTS]:
        raise ValueError('Outside fixed comparison endpoints')
    request = urllib.request.Request(url, method='GET', headers=HEADERS)
    row = {'client': 'python', 'method': 'GET', 'phase': 'open', 'tls_verification': 'default_verified_context'}
    start = time.monotonic()
    response = None
    try:
        try:
            response = urllib.request.build_opener(NoRedirect()).open(request, timeout=30)
        except urllib.error.HTTPError as exc:
            response = exc
        row['http_status'] = response.code
        row['http_version'] = {10:'1.0',11:'1.1',20:'2'}.get(getattr(response,'version',None),'unknown')
        if response.headers.get('Retry-After') is not None:
            row['retry_after_seconds'] = retry_delay(response.headers)
        sock = getattr(getattr(getattr(response, 'fp', None), 'raw', None), '_sock', None)
        if sock is not None:
            try:
                row['peer_family'] = peer_family(sock.getpeername()[0])
                version = sock.version()
                if version in ('TLSv1','TLSv1.1','TLSv1.2','TLSv1.3'):
                    row['tls_version'] = version
            except (AttributeError, OSError, ValueError):
                pass
        row['phase'] = 'read'
        response.read(4096)
        row['outcome'] = 'http_response'
        row['phase'] = 'complete'
    except Exception as exc:
        row['outcome'] = 'transport_error'
        row['errors'] = error_metadata(exc)
    finally:
        if response is not None:
            try:
                response.close()
            except Exception as exc:
                row['outcome'] = 'transport_error'
                row['close_errors'] = error_metadata(exc)
        row['elapsed_seconds'] = round(time.monotonic()-start,3)
    return row


def curl_get(url):
    if url not in [value for _, value in FIXED_ENDPOINTS]:
        raise ValueError('Outside fixed comparison endpoints')
    row = {'client':'curl','method':'GET','tls_version':'not_exposed_by_safe_write_out'}
    start = time.monotonic()
    # -q first disables user curlrc; native proxy/TLS behavior retained. No
    # netrc, cookies, redirects, credentials, verbose output or retries.
    with tempfile.TemporaryDirectory() as folder:
        header_path = Path(folder)/'headers'
        args = ['curl','-q','--silent','--show-error','--request','GET',
                '--max-time','30','--connect-timeout','10','--retry','0',
                '--proto','=https','--output',os.devnull,'--dump-header',str(header_path),
                '--write-out','%{http_code}|%{http_version}|%{ssl_verify_result}|%{remote_ip}',
                '--user-agent',UA,'--header','Accept: */*','--header','Accept-Encoding: identity',url]
        try:
            result = subprocess.run(args, capture_output=True, text=True, timeout=35)
            row['exit_code'] = result.returncode
            fields = result.stdout.strip().split('|')
            if len(fields)==4:
                if re.fullmatch(r'[0-9]{3}',fields[0]): row['http_status']=int(fields[0])
                if fields[1] in ('0','1','1.0','1.1','2','3'): row['http_version']=fields[1]
                if fields[2].isdigit(): row['ssl_verify_result']=int(fields[2])
                row['peer_family']=peer_family(fields[3])
            if header_path.exists():
                for line in header_path.read_text(errors='replace').splitlines():
                    key,sep,value=line.partition(':')
                    if sep and key.lower()=='retry-after':
                        row['retry_after_seconds']=retry_delay({'Retry-After':value.strip()})
            row['outcome']='http_response' if result.returncode==0 else 'transport_error'
            # stderr is deliberately discarded, not persisted or echoed.
        except (OSError, subprocess.TimeoutExpired) as exc:
            row['outcome']='transport_error'
            row['errors']=error_metadata(exc)
    row['elapsed_seconds']=round(time.monotonic()-start,3)
    return row


def run_comparison():
    report={'started_at_utc':dt.datetime.now(dt.timezone.utc).isoformat(),
            'python_ssl_backend':ssl.OPENSSL_VERSION,
            'proxy_environment_present':{k:k in os.environ for k in PROXY_NAMES},
            'policy':{'maximum_gets':8,'interval_seconds':120,'same_headers':HEADERS,
                      'automatic_retries':0,'redirects_followed':False,
                      'failure_pair_followup':'only preplanned other client, same endpoint, after120s'},
            'confounds':['Shared UA differs from historical curl default.',
                         'Time-varying connectivity and early stop can prevent reverse-order comparison.',
                         'Native HTTP negotiation may differ; no forced version.',
                         'Python socket timeout differs from curl total timeout.',
                         'Peer family may describe proxy, not origin.',
                         'Python reads first4KiB; curl discards complete body.'],
            'requests':[]}
    try:
        version=subprocess.run(['curl','-q','--version'],capture_output=True,text=True,timeout=5)
        match=re.search(r'^curl ([0-9.]+)',version.stdout)
        if match: report['curl_version']=match.group(1)
        match=re.search(r'(OpenSSL|LibreSSL|BoringSSL|GnuTLS|wolfSSL)/([A-Za-z0-9._-]+)',version.stdout)
        if match: report['curl_tls_backend']=match.group(0)
    except (OSError,subprocess.TimeoutExpired):
        report['curl_version']='unavailable'
    next_at=time.monotonic()
    stop=False
    for order in (('python','curl'),('curl','python')):
        for name,url in FIXED_ENDPOINTS:
            pair_failed=False
            for member,client in enumerate(order):
                time.sleep(max(0,next_at-time.monotonic()))
                checked=dt.datetime.now(dt.timezone.utc).isoformat()
                row=(python_get if client=='python' else curl_get)(url)
                row.update(endpoint=name,checked_at_utc=checked,pair_order=list(order),pair_member=member+1)
                report['requests'].append(row)
                next_at=time.monotonic()+120
                refused=row.get('http_status') in (403,429) or 'retry_after_seconds' in row
                failure=row.get('outcome')=='transport_error'
                if refused:
                    stop=True
                    report['stop_reason']='server_refusal_or_retry_after'
                elif failure:
                    pair_failed=True
                Path('client-comparison-report.json').write_text(json.dumps(report,indent=2))
                if stop: break
            if pair_failed and not stop:
                stop=True
                report['stop_reason']='transport_failure_pair_finished'
            if stop: break
        if stop: break
    report['completed_at_utc']=dt.datetime.now(dt.timezone.utc).isoformat()
    Path('client-comparison-report.json').write_text(json.dumps(report,indent=2))
    return report


if __name__=='__main__':
    run_comparison()
    print('Sanitized client comparison: client-comparison-report.json')
