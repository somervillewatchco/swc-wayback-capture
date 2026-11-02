#!/usr/bin/env python3
"""Bounded public GET-only transport diagnostics. No capture API submissions."""
import datetime as dt
import errno
import json
import os
from pathlib import Path
import queue
import re
import socket
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

PROXY_NAMES = ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY',
               'http_proxy', 'https_proxy', 'all_proxy', 'no_proxy')
TIMEOUT = 10
DNS_TIMEOUT = 5
READ_LIMIT = 4096


def endpoints(now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    day = now.strftime('%Y%m%d')
    query = urllib.parse.urlencode({'url': 'https://somervillewatchco.com/pages/warranty-policy',
        'matchType': 'exact', 'from': day + '000000', 'to': day + '235959',
        'output': 'json', 'fl': 'timestamp,original,statuscode,mimetype'})
    return (
        ('archive_system_status', 'https://web.archive.org/save/status/system'),
        ('warranty_cdx_current_utc_day', 'https://web.archive.org/cdx/search/cdx?' + query),
        ('terms_exact_replay', 'https://web.archive.org/web/20261102015923id_/https://somervillewatchco.com/policies/terms-of-service'),
        ('storefront_refund', 'https://somervillewatchco.com/policies/refund-policy'),
    )


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Do not follow arbitrary destinations, even same-origin.


def error_metadata(exc):
    """Only type, errno and function names: no messages, paths or local values."""
    causes, seen = [], set()
    current = exc
    while isinstance(current, BaseException) and id(current) not in seen and len(causes) < 4:
        seen.add(id(current))
        row = {'class': type(current).__name__}
        number = getattr(current, 'errno', None)
        if isinstance(number, int):
            row['errno'] = number
            row['errno_name'] = errno.errorcode.get(number, 'UNKNOWN')
        functions = []
        tb = current.__traceback__
        while tb is not None and len(functions) < 20:
            name = tb.tb_frame.f_code.co_name
            if re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]{0,63}', name):
                functions.append(name)
            tb = tb.tb_next
        row['functions'] = functions
        causes.append(row)
        reason = getattr(current, 'reason', None)
        current = reason if isinstance(reason, BaseException) else current.__cause__
    return causes


def dns_probe(host):
    if host not in ('web.archive.org', 'somervillewatchco.com'):
        raise ValueError('Host is not in fixed diagnostic scope')
    started = time.monotonic()
    result = queue.Queue(maxsize=1)

    def lookup():
        try:
            answers = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
            families = sorted({socket.AddressFamily(row[0]).name for row in answers})
            result.put({'outcome': 'resolved', 'answer_count': len(answers), 'families': families})
        except Exception as exc:
            result.put({'outcome': 'error', 'errors': error_metadata(exc)})

    threading.Thread(target=lookup, daemon=True).start()
    try:
        row = result.get(timeout=DNS_TIMEOUT)
    except queue.Empty:
        row = {'outcome': 'timeout'}
    return {'host': host, 'phase': 'dns', 'elapsed_seconds': round(time.monotonic() - started, 3), **row}


def retry_delay(headers):
    value = headers.get('Retry-After')
    if value is None:
        return 60
    try:
        return max(60, int(value))
    except (TypeError, ValueError, OverflowError):
        from email.utils import parsedate_to_datetime
        try:
            return max(60, int(parsedate_to_datetime(value).timestamp() - time.time()))
        except (TypeError, ValueError, OverflowError):
            return 60


def probe(name, url, allowed, opener=None):
    if (name, url) not in allowed or (name, url) not in endpoints():
        raise ValueError('Endpoint is not in fixed diagnostic scope')
    opener = opener or urllib.request.build_opener(NoRedirect())
    # Default urllib proxy handling is retained; never change routes, TLS,
    # fingerprint, DNS answers or IP selection in order to bypass a failure.
    request = urllib.request.Request(url, method='GET', headers={
        'User-Agent': 'SWC-Archive-Verification/1.0', 'Accept-Encoding': 'identity'})
    row = {'endpoint': name, 'url': url, 'method': 'GET', 'phase': 'open'}
    started = time.monotonic()
    response = None
    try:
        try:
            response = opener.open(request, timeout=TIMEOUT)
        except urllib.error.HTTPError as exc:
            response = exc  # HTTP status is useful even for a refused request.
        row['http_status'] = response.code
        mime = response.headers.get('Content-Type', '').split(';', 1)[0].strip().lower()
        known = {'text/html', 'text/plain', 'application/json', 'application/xml', 'text/xml', 'application/octet-stream'}
        row['content_type'] = mime if mime in known else 'other'
        if response.code == 429:
            row['retry_after_seconds'] = retry_delay(response.headers)
        row['phase'] = 'read'
        body = response.read(READ_LIMIT)
        row['sample_bytes_read'] = len(body)
        # No response content is logged. Only recognize the public service flag.
        if name == 'archive_system_status':
            try:
                status = json.loads(body).get('status')
                row['system_status'] = ('ok' if status == 'ok' else
                    'overloaded' if isinstance(status, str) and 'overloaded' in status.lower() else 'unknown')
            except (ValueError, AttributeError, TypeError):
                row['system_status'] = 'unknown'
        row['phase'] = 'complete'
        row['outcome'] = 'http_response'
    except Exception as exc:
        row['outcome'] = 'transport_error'
        row['errors'] = error_metadata(exc)
    finally:
        if response is not None:
            try:
                response.close()
            except Exception as exc:
                row['close_errors'] = error_metadata(exc)
        row['elapsed_seconds'] = round(time.monotonic() - started, 3)
    return row


def run_diagnostics():
    allowed = endpoints()
    report = {'started_at_utc': dt.datetime.now(dt.timezone.utc).isoformat(),
        'python_version': sys.version.split()[0], 'ssl_version': ssl.OPENSSL_VERSION,
        'proxy_environment_present': {name: name in os.environ for name in PROXY_NAMES},
        'policy': {'passes': 2, 'between_passes_seconds': 30, 'request_timeout_seconds': TIMEOUT,
                   'only_public_get': True, 'redirects_followed': False}, 'passes': []}
    blocked_until = {}
    hosts = ('web.archive.org', 'somervillewatchco.com')
    for number in (1, 2):
        if number == 2:
            time.sleep(30)
        pass_report = {'pass': number, 'dns': [dns_probe(host) for host in hosts], 'requests': []}
        for index, (name, url) in enumerate(allowed):
            if index:
                time.sleep(3)
            host = urllib.parse.urlsplit(url).hostname
            if time.monotonic() < blocked_until.get(host, 0):
                row = {'endpoint': name, 'url': url, 'method': 'GET', 'phase': 'not_sent',
                       'outcome': 'skipped_host_retry_after'}
            else:
                row = probe(name, url, allowed)
                if row.get('http_status') == 429:
                    blocked_until[host] = time.monotonic() + row.get('retry_after_seconds', 60)
            pass_report['requests'].append(row)
        report['passes'].append(pass_report)
        Path('transport-report.json').write_text(json.dumps(report, indent=2))
    return report


if __name__ == '__main__':
    result = run_diagnostics()
    print('Public GET-only diagnostics completed; sanitized evidence: transport-report.json')
