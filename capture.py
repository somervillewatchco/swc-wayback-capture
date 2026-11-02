#!/usr/bin/env python3
"""Conservative daily SPN capture and independent CDX/replay verification."""
import argparse
import datetime as dt
import gzip
import html
import http.client
import json
import os
from pathlib import Path
import re
import time
import urllib.error
import urllib.parse as up
import urllib.request as ur
import xml.etree.ElementTree as ET

BASE = 'https://somervillewatchco.com'
PRIORITY = ['/', '/policies/refund-policy', '/policies/shipping-policy',
 '/policies/terms-of-service', '/policies/privacy-policy',
 '/policies/contact-information', '/policies/legal-notice', '/pages/contact',
 '/pages/warranty-policy', '/pages/no-brand-affiliation',
 '/policies/subscription-policy', '/cart', '/cart/51183558787357:1?storefront=true']
TRANSPORT_PROBE = BASE + '/products/date-mod-ice-blue-arabic-watch'
PACING_PROBE = BASE + '/products/date-mod-olive-green-watch'
SUPPLEMENTAL_PROBE = BASE + '/pages/about-us'
UTC = dt.timezone.utc

def timestamp(epoch):
    return dt.datetime.fromtimestamp(epoch, UTC).strftime('%Y%m%d%H%M%S')

def epoch(value):
    return dt.datetime.strptime(value, '%Y%m%d%H%M%S').replace(tzinfo=UTC).timestamp()

def identity(url):
    if not isinstance(url, str):
        return None
    p = up.urlsplit(url)
    if p.scheme not in ('http', 'https') or p.hostname not in ('somervillewatchco.com', 'www.somervillewatchco.com'):
        return None
    return (p.path.rstrip('/') or '/', tuple(sorted(up.parse_qsl(p.query, keep_blank_values=True))))

def safe_status_ext(value):
    allowed = {'error:' + name for name in (
        'bad-gateway bad-request bandwidth-limit-exceeded blocked blocked-client-ip '
        'blocked-url browsing-timeout capture-location-error cannot-fetch celery filesize-limit '
        'ftp-access-denied gateway-timeout http-version-not-supported internal-server-error '
        'invalid-url-syntax invalid-server-response invalid-host-resolution job-failed '
        'method-not-allowed not-implemented no-browsers-available network-authentication-required '
        'no-access not-found proxy-error protocol-error read-timeout soft-time-limit-exceeded '
        'service-unavailable too-many-daily-captures too-many-redirects too-many-requests '
        'user-session-limit unauthorized max-daily-bandwidth max-daily-bandwidth-from-ip '
        'max-daily-bandwidth-host').split()}
    return value if isinstance(value, str) and value in allowed else 'unknown'

def response_metadata(http_status, headers, obj):
    """Allowlisted diagnostics only; never return raw messages, URLs or headers."""
    status = obj.get('status')
    message = obj.get('message', '')
    message = message.lower() if isinstance(message, str) else ''
    classification = ('same_snapshot' if 'same snapshot' in message else
                      'recent_capture' if 'recent capture' in message or 'already archived' in message else
                      'unknown')
    result = {'http_status': http_status,
              'status': status if status in ('success', 'pending', 'error') else 'unknown',
              'status_ext': safe_status_ext(obj.get('status_ext')),
              'message_classification': classification}
    category = ('invalid_syntax' if 'invalid syntax' in message or 'could not understand the request' in message else
                'authentication_required' if 'authentication' in message or 'unauthorized' in message else
                'host_paused' if 'host' in message and 'paused' in message else
                'cannot_fetch' if 'cannot fetch' in message or 'could not fetch' in message else 'unknown')
    result['message_category'] = category
    reported = re.search(r'\bhttp status\s*[:=]\s*([1-5][0-9]{2})\b', message)
    if reported:
        result['message_reported_http_status'] = int(reported.group(1))
    exception = obj.get('exception')
    result['exception_present'] = exception is not None
    exception_text = exception.lower() if isinstance(exception, str) else ''
    result['exception_category'] = ('timeout' if 'timeout' in exception_text or 'timed out' in exception_text else
        'connection_refused' if 'connectionrefused' in exception_text or 'connection refused' in exception_text else
        'tls_error' if 'sslerror' in exception_text or 'certificate verify failed' in exception_text else 'unknown')
    retry = headers.get('retry-after')
    if retry is not None:
        try:
            deadline = time.time() + max(0, int(retry))
        except (ValueError, TypeError, OverflowError):
            from email.utils import parsedate_to_datetime
            try:
                deadline = parsedate_to_datetime(retry).timestamp()
            except (ValueError, TypeError, OverflowError):
                deadline = 0
        if deadline > 0:
            result['retry_after_until'] = deadline
    return result

def latest_attempt_time(pending):
    request = pending.get('request_evidence', {})
    attempts = request.get('attempts', []) if isinstance(request, dict) else []
    value = attempts[-1].get('started_at') if isinstance(attempts, list) and attempts and isinstance(attempts[-1], dict) else None
    if not (isinstance(value, (int, float)) and not isinstance(value, bool) and 0 < value <= time.time() + 60):
        value = pending.get('submitted_at')
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 < value <= time.time() + 60 else None


def job_observation(http_status, headers, obj, url, pending):
    observation = response_metadata(http_status, headers, obj)
    observation.update(checked_at=timestamp(time.time()),
        response_job_id_matches=isinstance(obj.get('job_id'), str) and obj['job_id'] == pending.get('job_id'),
        original_url_present=bool(obj.get('original_url')),
        original_url_matches=identity(obj.get('original_url')) == identity(url))
    value = obj.get('timestamp')
    observation['timestamp_valid_format'] = isinstance(value, str) and bool(re.fullmatch(r'[0-9]{14}', value))
    observation['timestamp_fresh'] = False
    if observation['timestamp_valid_format']:
        try:
            captured = epoch(value)
            observation['timestamp'] = value
            started = latest_attempt_time(pending)
            observation['timestamp_fresh'] = started is not None and started - 5 <= captured <= time.time() + 60
        except ValueError:
            observation['timestamp_valid_format'] = False
    resources = obj.get('resources')
    if isinstance(resources, (list, dict)):
        observation['resources_count'] = len(resources)
    elif isinstance(resources, int) and not isinstance(resources, bool) and resources >= 0:
        observation['resources_count'] = resources
    return observation


def account_observation(http_status, headers, obj):
    """Capacity counters only; never echo account identity or response text."""
    result = {'http_status': http_status, 'checked_at': timestamp(time.time()), 'schema_valid': False,
              'cache_freshness': 'unknown'}
    if isinstance(obj, dict):
        for key in ('available', 'processing', 'daily_captures', 'daily_captures_limit'):
            value = obj.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                result[key] = value
        result['schema_valid'] = 'available' in result and 'processing' in result
    age = headers.get('age')
    if isinstance(age, str) and re.fullmatch(r'[0-9]{1,20}', age.strip()):
        result['response_age_seconds'] = int(age.strip())
    date = headers.get('date')
    if isinstance(date, str):
        from email.utils import parsedate_to_datetime, format_datetime
        try:
            parsed = parsedate_to_datetime(date)
            if parsed.tzinfo is not None:
                result['response_date'] = format_datetime(parsed.astimezone(UTC), usegmt=True)
        except (ValueError, TypeError, OverflowError):
            pass
    if 'response_age_seconds' in result and 'response_date' in result:
        result['cache_freshness'] = 'reported_headers_only'
    return result


def safe_recovery_event(event):
    result = {}
    for key in ('cycle', 'pass_number', 'http_status', 'errno', 'cooldown_until', 'retry_after_until'):
        value = event.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and abs(value) < 1e15:
            result[key] = value
    for key in ('healthy', 'final_url_matches', 'body_schema_matches'):
        if isinstance(event.get(key), bool):
            result[key] = event[key]
    allowed = {'phase': ('waiting', 'budget_exhausted', 'probe', 'resumed'),
               'endpoint': ('system_status', 'warranty_cdx'),
               'transport_phase': ('tcp_connect', 'open', 'read', 'decode')}
    for key, choices in allowed.items():
        if event.get(key) in choices:
            result[key] = event[key]
    for key in ('error_class', 'reason_class'):
        value = event.get(key)
        if isinstance(value, str) and re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,63}', value):
            result[key] = value
    checked = event.get('checked_at')
    if isinstance(checked, str) and re.fullmatch(r'[0-9]{14}', checked):
        result['checked_at'] = checked
    return result

def operational_summary(state):
    """Metadata only: no target URLs, job IDs, responses or environment values."""
    state = state if isinstance(state, dict) else {}
    queues = []
    queue_map = state.get('_queues', {})
    queue_map = queue_map if isinstance(queue_map, dict) else {}
    for key, queue in queue_map.items():
        if not isinstance(key, str) or not isinstance(queue, dict):
            continue
        if not re.fullmatch(r'(all|priority|pilot|supplemental|transport_probe|pacing_probe|supplemental_probe):[0-9]{8}:(capture|poll|verify)', key):
            continue
        urls = queue.get('urls', [])
        total = len(urls) if isinstance(urls, list) else 0
        cursor = queue.get('cursor', 0)
        cursor = cursor if isinstance(cursor, int) and not isinstance(cursor, bool) and 0 <= cursor < 1e15 else 0
        counts = {}
        rows = queue.get('results', [])
        rows = rows if isinstance(rows, list) else []
        for row in rows:
            label = row.get('state') if isinstance(row, dict) else 'unknown'
            if label not in ('verified', 'unverified', 'deferred', 'pending', 'failed', 'review_required', 'reused', 'unsupported-document-type', 'discovery-incomplete', 'no-additional-targets'):
                label = 'unknown'
            counts[label] = counts.get(label, 0) + 1
        queues.append({'key': key, 'total': total, 'cursor': cursor,
                       'remaining': max(0, total - cursor), 'complete': queue.get('complete') is True,
                       'in_flight': queue.get('started') is not None, 'processed_result_counts': counts})
    def safe_time(key):
        value = state.get(key, 0)
        return value if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value < 1e15 else 0
    pending = [v for k, v in state.items() if isinstance(k, str) and k.startswith(BASE + '/') and isinstance(v, dict)]
    events = state.get('_recovery_events', [])
    events = events if isinstance(events, list) else []
    return {'checked_at': timestamp(time.time()), 'queues': queues,
            'pending_counts': {'total': len(pending),
                               'with_job_id': sum(bool(p.get('job_id')) for p in pending),
                               'review_required': sum(p.get('review_required') is True for p in pending),
                               'known_not_sent': sum(p.get('known_not_sent') is True for p in pending)},
            'transport_cooldown_until': safe_time('_transport_cooldown_until'),
            'next_submit_at': safe_time('_next_submit'),
            'archive_next_request_at': safe_time('_archive_next_request_at'),
            'recovery_events': [safe_recovery_event(e) for e in events[-100:] if isinstance(e, dict)],
            'coverage_note': 'Queue progress and health probes are not capture coverage.'}

class LookupFailure(Exception):
    def __init__(self, message, transport=None):
        super().__init__(message)
        self.transport = transport


class RequestDeferred(LookupFailure):
    """No request was started: insufficient pacing/verification budget."""


class TrackedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *args, transport_tracker, **kwargs):
        super().__init__(*args, **kwargs)
        create_connection = self._create_connection

        def tracked_create(*connection_args, **connection_kwargs):
            transport_tracker['tcp_connect_attempted'] = True
            try:
                sock = create_connection(*connection_args, **connection_kwargs)
            except OSError:
                # Proof is deliberately narrow: no socket was returned. TLS,
                # proxy CONNECT, send and read failures never receive this proof.
                transport_tracker['tcp_connect_failed'] = True
                raise
            transport_tracker['tcp_established'] = True
            return sock

        self._create_connection = tracked_create


class TrackedHTTPSHandler(ur.HTTPSHandler):
    def __init__(self, tracker):
        super().__init__()  # Keep stdlib certificate/hostname validation.
        self.tracker = tracker

    def https_open(self, req):
        def connection(*args, **kwargs):
            return TrackedHTTPSConnection(*args, transport_tracker=self.tracker, **kwargs)
        return self.do_open(connection, req, context=self._context)


class NoRedirect(ur.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward Archive authorization to a redirect destination.

class NoRedirectIntoArchive(ur.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if up.urlsplit(newurl).hostname in ('web.archive.org', 'archive.org'):
            return None  # Do not enter Archive via an ungated storefront redirect.
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class Client:
    def __init__(self):
        self.archive_transport_failures = 0
        self.archive_healthy_responses = 0
        self.post_retry_deadline = 0  # Runner grants a bounded remaining budget.
        self.post_retry_not_before = 0
        self.post_attempt_callback = None
        self.archive_interval = 0  # Runner binds the persisted production gate.
        self.archive_next_request_at = 0
        self.archive_deadline = float('inf')
        self.archive_pacing_callback = None

    def paced_request(self, url, data=None, authenticated=False, attempt=1):
        is_archive = up.urlsplit(url).hostname in ('web.archive.org', 'archive.org')
        gated = is_archive and self.archive_interval > 0
        if gated:
            # POST needs room for status, CDX and replay, each behind the same
            # gate. This is a minimum useful reserve, not a success guarantee.
            reserve = 4 * 35 + 3 * self.archive_interval if data is not None else 35
            ready = max(time.time(), self.archive_next_request_at)
            if ready + reserve >= self.archive_deadline:
                raise RequestDeferred('Archive pacing/verification time budget')
            time.sleep(max(0, ready - time.time()))
            if time.time() + reserve >= self.archive_deadline:
                raise RequestDeferred('Archive pacing budget elapsed while waiting')
            self.archive_next_request_at = time.time() + self.archive_interval
            if self.archive_pacing_callback:
                self.archive_pacing_callback(self.archive_next_request_at)
        # Timestamp/audit must follow pacing and precede actual network IO.
        if data is not None and self.post_attempt_callback is not None:
            self.post_attempt_callback({'attempt': attempt, 'started_at': time.time()})
        response = None
        self.archive_observed_retry_after = 0
        try:
            response = self._request(url) if data is None and not authenticated else self._request(url, data, authenticated)
            return response
        finally:
            if gated:
                until = max(time.time() + self.archive_interval, self.archive_observed_retry_after)
                if response is not None:
                    headers = {k.lower(): v for k, v in response[1].items()}
                    until = max(until, response_metadata(response[0], headers, {}).get('retry_after_until', 0))
                self.archive_next_request_at = max(self.archive_next_request_at, until)
                if self.archive_pacing_callback:
                    self.archive_pacing_callback(self.archive_next_request_at)

    def request(self, url, data=None, authenticated=False):
        is_archive = up.urlsplit(url).hostname == 'web.archive.org'
        last_safe_failure = None
        for attempt in range(2):
            if data is None and is_archive and not self.archive_interval:
                wait = max(0, getattr(self, '_last_read', 0) + 2 - time.time())
                if wait:
                    time.sleep(wait)
                self._last_read = time.time()
            try:
                response = self.paced_request(url, data, authenticated, attempt=attempt + 1)
                if data is None and not authenticated and response[0] in (429, 502, 503, 504) and attempt == 0:
                    if not (is_archive and self.archive_interval):
                        time.sleep(30)
                    continue
                if is_archive and 200 <= response[0] < 400:
                    self.archive_healthy_responses += 1
                return response
            except RequestDeferred:
                if last_safe_failure is not None:
                    raise last_safe_failure  # No second dispatch: retain first proof.
                raise  # Never turn a local budget decision into another attempt.
            except LookupFailure as exc:
                if data is not None:
                    proof = exc.transport or {}
                    wait = max(60, self.post_retry_not_before - time.time(), self.archive_next_request_at - time.time())
                    reserve = 4 * 35 + 3 * self.archive_interval if self.archive_interval else 35
                    may_retry = (attempt == 0 and proof.get('known_not_sent') is True
                                 and time.time() + wait + reserve < self.post_retry_deadline)
                    if may_retry:
                        last_safe_failure = exc
                        time.sleep(wait)
                        continue
                elif attempt == 0:
                    if not (is_archive and self.archive_interval):
                        time.sleep(10)
                    continue
                if is_archive and exc.transport:
                    # Count the exhausted logical request, not its retry attempts.
                    self.archive_transport_failures += 1
                raise

    def _request(self, url, data=None, authenticated=False):
        headers = {'User-Agent': 'SWC-Archive-Verification/1.0', 'Accept-Encoding': 'identity'}
        if authenticated:
            target = up.urlsplit(url)
            if (target.scheme != 'https' or target.netloc != 'web.archive.org'
                    or not re.fullmatch(r'/save/?|/save/status/[A-Za-z0-9_-]+', target.path)
                    or target.query or target.fragment):
                raise LookupFailure('Authenticated endpoint is outside SPN scope')
            access, secret = os.environ.get('ARCHIVE_ORG_ACCESS_KEY'), os.environ.get('ARCHIVE_ORG_SECRET_KEY')
            if not access or not secret:
                raise LookupFailure('Archive credentials are not configured')
            headers['Authorization'] = 'LOW ' + access + ':' + secret
            headers['Accept'] = 'application/json'
        req = ur.Request(url, data=up.urlencode(data).encode() if data else None, headers=headers)
        tracker = {'tcp_connect_attempted': False, 'tcp_connect_failed': False, 'tcp_established': False}
        handlers = [TrackedHTTPSHandler(tracker)]
        if authenticated or up.urlsplit(url).hostname in ('web.archive.org', 'archive.org'):
            handlers.append(NoRedirect())  # No hidden redirect requests outside the global gate.
        else:
            handlers.append(NoRedirectIntoArchive())
        phase = 'open'
        try:
            try:
                response = ur.build_opener(*handlers).open(req, timeout=30)
            except urllib.error.HTTPError as exc:
                response = exc
            with response:
                if up.urlsplit(url).hostname in ('web.archive.org', 'archive.org'):
                    safe_headers = {k.lower(): v for k, v in response.headers.items()}
                    self.archive_observed_retry_after = response_metadata(response.status, safe_headers, {}).get('retry_after_until', 0)
                phase = 'read'
                body = response.read(8 * 1024 * 1024 + 1)
                if len(body) > 8 * 1024 * 1024:
                    raise LookupFailure('Response exceeds verification size limit')
                phase = 'decode'
                if body[:2] == b'\x1f\x8b':
                    body = gzip.decompress(body)
                return response.status, dict(response.headers.items()), body.decode('utf-8', errors='replace'), response.url
        except (OSError, TimeoutError, http.client.HTTPException) as exc:
            reason = getattr(exc, 'reason', exc)
            known_not_sent = (phase == 'open' and tracker['tcp_connect_failed']
                              and not tracker['tcp_established'])
            detail = {'phase': 'tcp_connect' if known_not_sent else phase,
                      'reason_class': type(reason).__name__,
                      'known_not_sent': known_not_sent}
            number = getattr(reason, 'errno', None)
            if isinstance(number, int):
                detail['errno'] = number
            raise LookupFailure('Network operation failed: ' + type(reason).__name__, transport=detail) from None

    def json(self, url, data=None, authenticated=False):
        status, headers, body, _ = self.request(url, data, authenticated)
        try:
            obj = json.loads(body)
        except ValueError:
            if status == 429:
                return status, {k.lower(): v for k, v in headers.items()}, {'status': 'error', 'status_ext': 'http-429'}
            raise LookupFailure('Non-JSON response (HTTP %s)' % status) from None
        return status, {k.lower(): v for k, v in headers.items()}, obj

    def verify(self, url, since, until=None):
        query = up.urlencode({'url': url, 'matchType': 'exact', 'from': timestamp(since),
            'to': timestamp(min(until, time.time() + 60) if until is not None else time.time() + 60), 'output': 'json',
            'fl': 'timestamp,original,statuscode,mimetype,digest'})
        status, _, data = self.json('https://web.archive.org/cdx/search/cdx?' + query)
        if status != 200 or not isinstance(data, list):
            raise LookupFailure('CDX unavailable')
        if not data:
            return None
        fields = data[0]
        if not isinstance(fields, list) or not {'timestamp', 'original', 'statuscode', 'mimetype'}.issubset(fields):
            raise LookupFailure('Invalid CDX schema')
        for values in reversed(data[1:]):
            if len(values) != len(fields):
                raise LookupFailure('Incomplete CDX row')
            row = dict(zip(fields, values))
            if row['statuscode'] != '200' or row['mimetype'] not in ('text/html', 'application/xhtml+xml', 'warc/revisit'):
                continue
            if identity(row['original']) != identity(url) or not identity(url):
                continue
            try:
                if not since <= epoch(row['timestamp']) <= (min(until, time.time() + 60) if until is not None else time.time() + 60):
                    continue
            except ValueError:
                continue
            replay = 'https://web.archive.org/web/' + row['timestamp'] + 'id_/' + row['original']
            code, headers, body, final = self.request(replay)
            match = re.fullmatch(r'https?://web\.archive\.org/web/(\d{14})(?:id_)?/(https?://.*)', final)
            if not match or match[1] != row['timestamp'] or identity(match[2]) != identity(url):
                continue  # Never accept Wayback's redirect to a different capture/page.
            lower = {k.lower(): v for k, v in headers.items()}
            if code != 200 or 'html' not in lower.get('content-type', '') or not usable_html(body, url):
                continue
            return {**row, 'replay_url': replay, 'verified_at': timestamp(time.time())}
        return None

def usable_html(body, url):
    title = re.search(r'<title[^>]*>(.*?)</title>', body, re.I | re.S)
    if len(body) < 512 or not title or not html.unescape(title[1]).strip():
        return False
    if re.search(r'just a moment|access denied|temporarily unavailable|robot check|page not found|verify you are human', title[1], re.I):
        return False
    for tag in re.findall(r'<link\b[^>]*>', body, re.I):
        attrs = dict((k.lower(), html.unescape(v)) for k, _, v in re.findall(r'([\w-]+)\s*=\s*([\'"])(.*?)\2', tag, re.S))
        if attrs.get('rel', '').lower() == 'canonical' and identity(up.urljoin(url, attrs.get('href', ''))) != identity(url):
            return False
    # Controlled cart must actually contain the expected variant, not merely a $0 cart shell.
    if '/cart/51183558787357:1' in url:
        return bool(re.search(r'name=[\'"]id[\'"][^>]*value=[\'"]51183558787357', body))
    return True

def discover(client):
    urls, seen = [], set()
    for page in range(1, 21):
        if page > 1:
            time.sleep(5)  # Do not burst live storefront pagination requests.
        url = BASE + '/collections/all' + ('?page=' + str(page) if page > 1 else '')
        status, headers, body, final = client.request(url)
        # Validate the actual requested pagination identity. Shopify's live
        # page-2 canonical includes ?page=2, so validating against the base
        # collection would incorrectly reject a valid catalog page.
        if status != 200 or identity(final) != identity(url) or not usable_html(body, url):
            raise LookupFailure('Catalog discovery failed on page %s (HTTP %s)' % (page, status))
        paths = set(re.findall(r'/products/[a-zA-Z0-9-]+', body))
        if not paths:
            if page == 1:
                raise LookupFailure('No products discovered')
            return urls + [BASE + p for p in sorted(seen)]
        if not paths - seen:
            raise LookupFailure('Catalog pagination repeats products')
        urls.append(url)
        seen.update(paths)
        # Stop only on a page that has no link to a later collection page.
        linked = [int(n) for n in re.findall(r'[?&](?:amp;)?page=(\d+)', html.unescape(body))]
        if not any(n > page for n in linked):
            return urls + [BASE + p for p in sorted(seen)]
    raise LookupFailure('Catalog pagination exceeded limit')

def discover_sitemap(client, max_sitemaps=32, max_depth=4, max_urls=5000):
    """Read bounded, same-origin sitemap XML; image locs are never targets."""
    namespace = 'http://www.sitemaps.org/schemas/sitemap/0.9'
    origin = up.urlsplit(BASE)
    visited, targets = set(), {}
    queue = [(BASE + '/sitemap.xml', 0)]

    def same_origin(value):
        try:
            parts = up.urlsplit(value)
            return (parts.scheme, parts.netloc) == (origin.scheme, origin.netloc) and not parts.fragment
        except ValueError:
            return False

    while queue:
        url, depth = queue.pop(0)
        if url in visited:
            continue
        if depth > max_depth or len(visited) >= max_sitemaps:
            raise LookupFailure('Sitemap recursion/quantity limit exceeded')
        if visited:
            time.sleep(2)
        visited.add(url)
        code, _, body, final = client.request(url)
        if code != 200 or not same_origin(final) or identity(final) != identity(url):
            raise LookupFailure('Sitemap request failed or redirected outside expected URL')
        # Reject DTD/entity processing entirely; a public sitemap needs neither.
        if re.search(r'<!\s*(?:DOCTYPE|ENTITY)\b', body, re.I):
            raise LookupFailure('Sitemap DTD/entities are not supported')
        try:
            root = ET.fromstring(body)
        except ET.ParseError:
            raise LookupFailure('Malformed sitemap XML') from None
        prefix = '{' + namespace + '}' if root.tag.startswith('{' + namespace + '}') else ''
        if root.tag not in (prefix + 'sitemapindex', prefix + 'urlset'):
            raise LookupFailure('Unsupported sitemap root or namespace')
        is_index = root.tag == prefix + 'sitemapindex'
        child_tag = prefix + ('sitemap' if is_index else 'url')
        for child in root.findall(child_tag):
            # Direct standard-namespace loc only: excludes image:loc/video:loc.
            loc = child.find(prefix + 'loc')
            value = loc.text.strip() if loc is not None and loc.text else ''
            if not value:
                raise LookupFailure('Sitemap record has no loc')
            if not same_origin(value):
                continue  # External resources are not in the approved site scope.
            if is_index:
                if value not in visited:
                    queue.append((value, depth + 1))
            else:
                key = identity(value)
                if key:
                    targets[key] = value
                    if len(targets) > max_urls:
                        raise LookupFailure('Sitemap URL limit exceeded')
    if not targets:
        raise LookupFailure('No published same-origin sitemap targets discovered')
    return sorted(targets.values())


def discover_supplemental(client):
    # Fully finish both inventories before any capture can occur. Subtract the
    # existing all-scope definition, not merely successful captures from it.
    baseline = {identity(BASE + path) for path in PRIORITY}
    baseline.update(identity(url) for url in discover(client))
    return [url for url in discover_sitemap(client) if identity(url) not in baseline]

class Runner:
    def __init__(self, client, state_path, budget=17400, interval=60, verify_only=False, poll_only=False):
        self.client, self.state_path = client, Path(state_path)
        self.state = json.loads(self.state_path.read_text()) if self.state_path.exists() else {}
        self.started = time.time()
        self.since = dt.datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        self.deadline, self.interval = self.started + budget, interval
        self.verify_only = verify_only
        self.poll_only = poll_only
        self.next_submit = self.state.get('_next_submit', 0)
        self.results = []
        self.transport_failure_streak = 0
        self.transport_circuit_open = False
        self.client.post_retry_deadline = self.deadline
        self.client.archive_interval = 120
        self.client.archive_next_request_at = max(self.state.get('_archive_next_request_at', 0),
                                                 self.state.get('_transport_cooldown_until', 0), self.state.get('_next_submit', 0))
        self.client.archive_deadline = self.deadline
        def persist_archive_gate(until):
            self.state['_archive_next_request_at'] = until
            self.save()
        self.client.archive_pacing_callback = persist_archive_gate

    def save(self):
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix('.tmp')
        tmp.write_text(json.dumps(self.state, indent=2))
        tmp.replace(self.state_path)
        self.write_operational_report()

    def write_operational_report(self):
        output = Path('operational-report.json')
        if (output.resolve() != self.state_path.resolve()
                and not (output.exists() and self.state_path.exists() and output.samefile(self.state_path))):
            output.write_text(json.dumps(operational_summary(self.state), indent=2))

    def recovery_event(self, phase, cycle, **details):
        event = safe_recovery_event({'checked_at': timestamp(time.time()), 'cycle': cycle, 'phase': phase, **details})
        events = self.state.setdefault('_recovery_events', [])
        events.append(event)
        self.state['_recovery_events'] = events[-100:]
        self.save()
        print(json.dumps({'recovery_event': event}), flush=True)

    def diagnostic_snapshot(self, url):
        # Only fields generated by our sanitizers; never raw SPN responses.
        pending = self.state.get(url, {})
        pending = pending if isinstance(pending, dict) else {}
        row = {}
        job_id = pending.get('job_id')
        if isinstance(job_id, str) and re.fullmatch(r'[A-Za-z0-9_-]+', job_id):
            row['job_id'] = job_id
        if isinstance(pending.get('submitted_at'), (int, float)):
            row['submitted_at'] = pending['submitted_at']
        for key in ('status_evidence', 'request_evidence', 'terminal_reused', 'retry_eligible_at', 'review_required', 'submission_response', 'status_response', 'transport_failure', 'known_not_sent', 'status_error', 'server_retry_after_until', 'status_paused_until', 'job_diagnostic'):
            if key in pending:
                row[key] = pending[key]
        return row

    def result(self, url, state, **extra):
        row = {'url': url, 'state': state, **extra, **self.diagnostic_snapshot(url)}
        self.results.append(row)
        print(json.dumps(row), flush=True)
        Path('capture-report.json').write_text(json.dumps(self.results, indent=2))
        return row

    def retire_pending(self, url, state, **details):
        # Audit before removing, including transitions that do not emit a row.
        completed = self.state.setdefault('_completed_jobs', [])
        completed.append({'url': url, 'state': state, **details,
                          **self.diagnostic_snapshot(url), 'completed_at': timestamp(time.time())})
        self.state['_completed_jobs'] = completed[-50:]
        self.state.pop(url, None)
        self.save()

    def terminal_result(self, url, state, **details):
        row = self.result(url, state, **details)
        self.retire_pending(url, state, **details)
        return row

    def terminal_failure(self, url, **details):
        return self.terminal_result(url, 'failed', **details)

    def verified_result(self, url, evidence, coverage_source):
        pending = self.state.get(url, {})
        submitted_at = latest_attempt_time(pending)
        new_capture = (isinstance(submitted_at, (int, float))
                       and epoch(evidence['timestamp']) >= submitted_at - 5)
        details = dict(coverage_source=coverage_source, new_capture=new_capture, **evidence)
        if pending.get('status_error'):
            details['status_conflict'] = True
        if pending:
            return self.terminal_result(url, 'verified', **details)
        return self.result(url, 'verified', **details)

    def pause(self, headers, obj):
        detail = (str(obj.get('status_ext', '')) + ' ' + str(obj.get('message', ''))).lower()
        paused = ('host-crawling-paused' in detail or 'crawling this host is paused' in detail
                  or bool(re.search(r'http status\s*[=:]\s*429', detail)))
        if not paused and obj.get('_http') != 429:
            return False
        retry = headers.get('retry-after', '660')
        try:
            seconds = int(retry)
        except ValueError:
            from email.utils import parsedate_to_datetime
            try:
                seconds = int(parsedate_to_datetime(retry).timestamp() - time.time())
            except (ValueError, TypeError):
                seconds = 660
        self.next_submit = time.time() + max(660, seconds)
        self.state['_next_submit'] = self.next_submit
        # A known server pause is stronger than the local global floor.
        self.client.archive_next_request_at = max(self.state.get('_archive_next_request_at', 0), self.next_submit)
        self.state['_archive_next_request_at'] = self.client.archive_next_request_at
        self.save()
        return True

    def classify_success(self, url, pending, check_daily=True):
        """Resolve a completed job using archive evidence, never status alone."""
        status = pending.get('status_evidence', {})
        if status.get('status') != 'success' or not status.get('original_url_matches'):
            return None
        if not isinstance(status.get('timestamp'), str) or not re.fullmatch(r'[0-9]{14}', status['timestamp']):
            return None
        try:
            captured_at = epoch(status.get('timestamp', ''))
        except (ValueError, TypeError):
            return None
        if captured_at > time.time() + 60:
            return None
        if check_daily:
            evidence = self.client.verify(url, self.since)
            if evidence:
                return self.verified_result(url, evidence, 'job_reconciled_index')
        if captured_at >= self.since:
            return None  # Current-day indexing may still be pending.
        evidence = self.client.verify(url, captured_at, until=captured_at)
        if not evidence or evidence.get('timestamp') != status['timestamp']:
            return None
        # This job is definitively completed with a usable older capture. Do
        # not poll it forever or call it today's coverage. The next UTC day's
        # regular opportunity is our scheduling policy, not an Archive limit.
        attempt_day = dt.datetime.fromtimestamp(latest_attempt_time(pending) or pending['submitted_at'], UTC).replace(
            hour=0, minute=0, second=0, microsecond=0).timestamp()
        pending['terminal_reused'] = evidence
        pending['retry_eligible_at'] = max(attempt_day + 86400,
            pending.get('server_retry_after_until', 0))
        self.save()
        return self.result(url, 'reused', reason='completed capture predates UTC day; no new submission in this run')

    def process(self, url):
        # A per-run circuit counts URL operations with exhausted Archive
        # transport failures; successful intermediate CDX reads cannot erase a
        # subsequent failed POST/status lookup for the same URL.
        if self.transport_circuit_open or self.state.get('_transport_cooldown_until', 0) > time.time():
            return self.result(url, 'deferred', reason='local transport circuit cooldown; no network requests made', queue_untouched=True)
        before_failed = getattr(self.client, 'archive_transport_failures', 0)
        before_healthy = getattr(self.client, 'archive_healthy_responses', 0)
        self._operation_transport_failed = False
        row = self._process(url)
        after_failed = getattr(self.client, 'archive_transport_failures', 0)
        after_healthy = getattr(self.client, 'archive_healthy_responses', 0)
        failed = self._operation_transport_failed or (isinstance(before_failed, int) and isinstance(after_failed, int) and after_failed > before_failed)
        healthy = isinstance(before_healthy, int) and isinstance(after_healthy, int) and after_healthy > before_healthy
        if failed:
            self.transport_failure_streak += 1
        elif healthy:
            self.transport_failure_streak = 0
        if self.transport_failure_streak >= 3:
            self.transport_circuit_open = True
            until = time.time() + 900  # Local conservative policy, not Archive's limit.
            self.state['_transport_cooldown_until'] = until
            self.state['_next_submit'] = self.next_submit = max(self.next_submit, until)
            self.save()
            row['transport_circuit'] = 'opened_after_three_transport_failed_url_operations'
            Path('capture-report.json').write_text(json.dumps(self.results, indent=2))
        return row

    def recover_transport(self, cycle=0):
        """Bounded public GET-only health gate, not proof of capture coverage."""
        until = max(self.state.get('_transport_cooldown_until', 0), self.next_submit, self.state.get('_archive_next_request_at', 0))
        # Estimated reserve; socket timeouts are not a hard wall-clock limit.
        if time.time() + max(0, until - time.time()) + 620 >= self.deadline:
            self.recovery_event('budget_exhausted', cycle, healthy=False, cooldown_until=until)
            return False
        self.recovery_event('waiting', cycle, cooldown_until=until)
        time.sleep(max(0, until - time.time()))
        query = up.urlencode({'url': BASE + '/pages/warranty-policy',
            'matchType': 'exact', 'from': timestamp(self.since), 'output': 'json',
            'fl': 'timestamp,original,statuscode,mimetype'})
        endpoints = [('https://web.archive.org/save/status/system', dict),
                     ('https://web.archive.org/cdx/search/cdx?' + query, list)]
        for pass_number in range(2):
            if pass_number:
                time.sleep(30)
            for endpoint, expected in endpoints:
                retry_until = 0
                detail = {'pass_number': pass_number + 1,
                          'endpoint': 'system_status' if expected is dict else 'warranty_cdx'}
                try:
                    # Bypass retry orchestration, NOT proxy/TLS: one normal
                    # unauthenticated GET per endpoint/pass, no credentials.
                    code, headers, body, final = self.client.paced_request(endpoint)
                    metadata = response_metadata(code, {k.lower(): v for k, v in headers.items()}, {})
                    retry_until = metadata.get('retry_after_until', 0)
                    detail['http_status'] = code
                    detail['final_url_matches'] = final == endpoint
                    if retry_until:
                        detail['retry_after_until'] = retry_until
                    detail['body_schema_matches'] = isinstance(json.loads(body), expected)
                    healthy = code == 200 and final == endpoint and detail['body_schema_matches']
                except RequestDeferred:
                    self.recovery_event('budget_exhausted', cycle, healthy=False, cooldown_until=until)
                    return False
                except (LookupFailure, ValueError, TypeError) as exc:
                    healthy = False
                    detail['error_class'] = type(exc).__name__
                    transport = getattr(exc, 'transport', None) or {}
                    if transport.get('phase') in ('tcp_connect', 'open', 'read', 'decode'):
                        detail['transport_phase'] = transport['phase']
                    reason = transport.get('reason_class')
                    if isinstance(reason, str) and re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,63}', reason):
                        detail['reason_class'] = reason
                    if isinstance(transport.get('errno'), int):
                        detail['errno'] = transport['errno']
                if not healthy:
                    until = max(time.time() + 900, retry_until, self.next_submit)
                    self.state['_transport_cooldown_until'] = until
                    self.state['_next_submit'] = self.next_submit = until
                    self.recovery_event('probe', cycle, healthy=False, cooldown_until=until, **detail)
                    return False
                self.recovery_event('probe', cycle, healthy=True, **detail)
        self.transport_circuit_open = False
        self.transport_failure_streak = 0
        self.state.pop('_transport_cooldown_until', None)
        self.recovery_event('resumed', cycle, healthy=True, cooldown_until=0)
        return True

    def run_queue(self, urls, scope):
        """Checkpoint untouched work; a cursor is never a coverage claim."""
        mode = 'verify' if self.verify_only else 'poll' if self.poll_only else 'capture'
        key = scope + ':' + timestamp(self.since)[:8] + ':' + mode
        queues = self.state.setdefault('_queues', {})
        # Yesterday's cursors must not suppress today's daily capture goal.
        for old in list(queues):
            if ':' + timestamp(self.since)[:8] + ':' not in old:
                if scope != 'supplemental_probe' or old.startswith('supplemental_probe:'):
                    del queues[old]
        queue = queues.get(key)
        if not queue or queue.get('complete'):
            queue = {'urls': list(dict.fromkeys(urls)), 'cursor': 0,
                     'results': [], 'complete': False}
            queues[key] = queue
        self.results.extend(queue['results'])
        # Save before dispatch. A crash after start cannot lead to a blind
        # replay of a possibly sent submission, even without a job ID.
        if queue.get('started') is not None:
            url = queue['urls'][queue['cursor']]
            self.result(url, 'unverified', reason='interrupted URL operation; resume skips it, pending state preserved')
            queue['results'] = list(self.results)
            queue['cursor'] += 1
            queue.pop('started', None)
        self.save()
        cycles = 0
        while queue['cursor'] < len(queue['urls']):
            if time.time() + 120 >= self.deadline:
                break
            if self.transport_circuit_open or '_transport_cooldown_until' in self.state:
                if cycles >= 2:
                    break
                cycles += 1
                if not self.recover_transport(cycle=cycles):
                    if time.time() + 1520 >= self.deadline:
                        break
                    continue
            url = queue['urls'][queue['cursor']]
            queue['started'] = url
            self.save()
            if scope == 'supplemental' and url == BASE + '/agents.md':
                self.result(url, 'unsupported-document-type', content_type='text/markdown',
                            reason='published text document is outside the HTML verifier; no submission made')
            else:
                row = self.process(url)
                if isinstance(row, dict) and row.get('queue_untouched') is True:
                    # No completed operation: retain this untouched cursor.
                    # Its transient report is not checkpointed as processed.
                    self.results.pop()
                    queue.pop('started', None)
                    self.save()
                    break
            queue['cursor'] += 1
            queue.pop('started', None)
            queue['results'] = list(self.results)
            self.save()
        queue['complete'] = queue['cursor'] == len(queue['urls'])
        self.save()
        # These reporting rows do not advance the saved cursor/results.
        for url in queue['urls'][queue['cursor']:]:
            self.result(url, 'deferred', reason='untouched queue checkpoint saved; recovery cycles or run budget exhausted')

    def run_supplemental_probe(self):
        """One fixed untouched canary; never release an unexpected prior attempt."""
        url = SUPPLEMENTAL_PROBE
        def prior_attempt():
            return url in self.state or any(isinstance(row, dict) and row.get('url') == url
                for row in self.state.get('_completed_jobs', []))
        if prior_attempt():
            return self.result(url, 'review_required',
                reason='About Us canary has saved prior-attempt state; retained without resubmission')
        mode = 'verify' if self.verify_only else 'poll' if self.poll_only else 'capture'
        key = 'supplemental_probe:' + timestamp(self.since)[:8] + ':' + mode
        queues = self.state.get('_queues', {})
        queued = queues.get(key) if isinstance(queues, dict) else None
        if (not isinstance(queues, dict) or (key in queues and
            (not isinstance(queued, dict) or queued.get('urls') != [url]
             or not isinstance(queued.get('cursor'), int) or isinstance(queued.get('cursor'), bool)
             or not 0 <= queued['cursor'] <= 1
             or (queued.get('started') is not None and (queued['cursor'] != 0 or queued['started'] != url))))):
            return self.result(url, 'review_required', reason='unexpected saved canary queue; no requests made')
        # Mandatory even without an existing circuit/cooldown. Failure stops
        # this scope immediately: no process/CDX/POST or extra recovery cycles.
        if not self.recover_transport(cycle=1):
            return self.result(url, 'deferred', reason='mandatory paced health gate did not pass; canary untouched')
        if prior_attempt():
            return self.result(url, 'review_required', reason='prior-attempt state appeared during health gate; canary held')
        self.run_queue([url], 'supplemental_probe')
        return self.results[-1] if self.results else None

    def diagnose_account(self):
        """Exactly one gated existing-auth GET; no capture/job/queue operations."""
        url = 'https://web.archive.org/save/status/user'
        try:
            code, headers, body, _ = self.client.paced_request(url, authenticated=True)
            try:
                obj = json.loads(body)
            except (ValueError, TypeError):
                obj = None
            observation = account_observation(code, {k.lower(): v for k, v in headers.items()}, obj)
            self.state['_account_diagnostic'] = observation
            self.save()
            state = 'diagnostic-observed' if code == 200 and observation['schema_valid'] else 'diagnostic-unavailable'
            return self.result(url, state, account_observation=observation,
                reason='single possibly-cached account-capacity observation; not capture coverage or historical failure attribution')
        except RequestDeferred:
            return self.result(url, 'deferred', reason='account diagnostic pacing/cooldown exceeds budget; no request made')
        except LookupFailure as exc:
            observation = {'checked_at': timestamp(time.time()), 'schema_valid': False,
                           'outcome': 'lookup_failed', 'error_class': type(exc).__name__}
            detail = exc.transport if isinstance(exc.transport, dict) else {}
            safe = safe_recovery_event({'transport_phase': detail.get('phase'),
                'reason_class': detail.get('reason_class'), 'errno': detail.get('errno')})
            if safe:
                observation['transport'] = safe
            self.state['_account_diagnostic'] = observation
            self.save()
            return self.result(url, 'diagnostic-unavailable', account_observation=observation,
                               reason='single account status lookup failed; no retry or capture action')

    def diagnose_saved_pacing_job(self):
        """Exactly one gated authenticated status GET; never verify/submit/retire."""
        url = PACING_PROBE
        pending = self.state.get(url)
        if not isinstance(pending, dict) or not isinstance(pending.get('job_id'), str) or not re.fullmatch(r'[A-Za-z0-9_-]+', pending['job_id']):
            return self.result(url, 'diagnostic-unavailable', reason='no valid saved job ID; no request made')
        self.client.archive_next_request_at = max(self.client.archive_next_request_at,
            pending.get('status_paused_until', 0), pending.get('server_retry_after_until', 0))
        try:
            code, headers, body, _ = self.client.paced_request(
                'https://web.archive.org/save/status/' + pending['job_id'], authenticated=True)
            try:
                obj = json.loads(body)
            except (ValueError, TypeError):
                obj = None
            if isinstance(obj, dict):
                observation = job_observation(code, {k.lower():v for k,v in headers.items()}, obj, url, pending)
                observation['schema_valid'] = True
            else:
                observation = {'http_status': code, 'schema_valid': False, 'checked_at': timestamp(time.time())}
            pending['job_diagnostic'] = observation
            self.save()
            return self.result(url, 'diagnostic-observed', reason='one saved-job status observation; no capture verification or resubmission')
        except RequestDeferred:
            return self.result(url, 'deferred', reason='saved-job diagnostic pacing/cooldown exceeds budget; no request made')
        except LookupFailure as exc:
            pending['job_diagnostic'] = {'checked_at': timestamp(time.time()), 'outcome': 'lookup_failed',
                                         'error_class': type(exc).__name__}
            detail = exc.transport if isinstance(exc.transport, dict) else {}
            safe = safe_recovery_event({'transport_phase': detail.get('phase'),
                                        'reason_class': detail.get('reason_class'), 'errno': detail.get('errno')})
            if safe:
                pending['job_diagnostic']['transport'] = safe
            self.save()
            return self.result(url, 'diagnostic-unavailable', reason='single saved-job status lookup failed; job retained')

    def _process(self, url):
        created_pending = False
        if time.time() + 35 >= self.deadline:
            return self.result(url, 'deferred', reason='run time budget', queue_untouched=True)
        try:
            evidence = self.client.verify(url, self.since)
            if evidence:
                return self.verified_result(url, evidence, 'existing_index')
            pending = self.state.get(url)
            created_pending = False
            if pending and pending.get('known_not_sent') is True and not pending.get('job_id') and not (self.verify_only or self.poll_only):
                # This explicit marker is written only by the instrumented new
                # attempt path. Historical no-job states are never promoted.
                self.retire_pending(url, 'known_not_sent_released')
                pending = None
            if pending and not pending.get('terminal_reused'):
                # Migrate saved successes (including earlier releases) by
                # read-only exact CDX/replay, without requiring a live job ID.
                resolved = self.classify_success(url, pending, check_daily=False)
                if resolved:
                    return resolved
            if self.verify_only:
                return self.result(url, 'unverified', reason='no usable indexed capture for UTC day')
            if pending and pending.get('terminal_reused'):
                if self.poll_only or time.time() < pending['retry_eligible_at']:
                    return self.result(url, 'deferred', reason='completed reused capture; awaiting next eligible UTC-day opportunity')
                # Preserve a bounded audit trail before the next regular attempt.
                self.retire_pending(url, 'reused_released')
                pending = None
            if pending and pending.get('review_required'):
                return self.result(url, 'review_required', reason='saved job outcome needs review; archive evidence still checked, no submission or status polling')
            if pending and pending.get('status_paused_until', 0) > time.time():
                return self.result(url, 'deferred', reason='status lookup cooldown; existing job retained')
            if not pending and self.poll_only:
                return self.result(url, 'unverified', reason='no saved job to poll; no submission made')
            if not pending:
                delay = max(0, self.next_submit - time.time())
                if time.time() + delay + 120 >= self.deadline:
                    return self.result(url, 'deferred', reason='host cooldown/time budget', queue_untouched=True)
                if delay:
                    print('Waiting %ss before next submission' % int(delay), flush=True)
                    time.sleep(delay)
                pending = {'submitted_at': time.time(), 'job_id': None,
                    'request_evidence': {'method': 'POST', 'endpoint': 'https://web.archive.org/save/',
                                         'if_not_archived_within': '1'}}
                # Persist BEFORE POST: a network timeout has an uncertain outcome.
                self.state[url] = pending
                created_pending = True
                self.state['_next_submit'] = self.next_submit = time.time() + self.interval
                self.save()
                self.client.post_retry_not_before = self.next_submit
                def record_attempt(attempt):
                    pending['request_evidence'].setdefault('attempts', []).append(attempt)
                    # Pace the next URL from the latest physical attempt, not
                    # the original attempt before a safe retry's delay.
                    self.state['_next_submit'] = self.next_submit = max(
                        self.next_submit, attempt['started_at'] + self.interval)
                    self.client.post_retry_not_before = self.next_submit
                    self.save()  # Audit each bounded attempt before any network IO.
                self.client.post_attempt_callback = record_attempt
                status, headers, obj = self.client.json('https://web.archive.org/save/',
                    {'url': url, 'if_not_archived_within': '1'}, True)
                if not isinstance(obj, dict):
                    raise LookupFailure('Invalid submission response')
                obj['_http'] = status
                pending['submission_response'] = response_metadata(status, headers, obj)
                if pending['submission_response'].get('retry_after_until'):
                    pending['server_retry_after_until'] = pending['submission_response']['retry_after_until']
                self.save()
                job_id = obj.get('job_id')
                if isinstance(job_id, str) and re.fullmatch(r'[A-Za-z0-9_-]+', job_id):
                    pending['job_id'] = job_id
                    self.save()
                if self.pause(headers, obj):
                    if pending.get('job_id'):
                        pending['status_paused_until'] = self.next_submit
                        self.save()
                        return self.result(url, 'deferred', reason='submission paused but job accepted; retained')
                    return self.terminal_result(url, 'deferred', reason='host crawling paused', response=pending['submission_response'])
                if not isinstance(job_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', job_id):
                    if obj.get('status') == 'error' or status in (400, 401, 403):
                        return self.terminal_failure(url, reason='submission explicitly rejected', http_status=status,
                                           status_ext=safe_status_ext(obj.get('status_ext')), response=pending['submission_response'])
                    return self.result(url, 'pending', reason='submission outcome unknown; no automatic duplicate')
                pending['job_id'] = job_id
                self.save()
            if not pending.get('job_id'):
                return self.result(url, 'pending', reason='earlier submission outcome unknown; needs review')
            for _ in range(12):
                if time.time() + 40 >= self.deadline:
                    break
                status, headers, obj = self.client.json('https://web.archive.org/save/status/' + pending['job_id'], authenticated=True)
                if not isinstance(obj, dict):
                    raise LookupFailure('Invalid status response')
                obj['_http'] = status
                pending['status_response'] = response_metadata(status, headers, obj)
                if pending['status_response'].get('retry_after_until'):
                    pending['server_retry_after_until'] = max(pending.get('server_retry_after_until', 0),
                        pending['status_response']['retry_after_until'])
                self.save()
                if self.pause(headers, obj):
                    # Throttling the status lookup does not prove the accepted
                    # capture was refused. Keep its job to prevent a duplicate.
                    pending['status_paused_until'] = self.next_submit
                    self.save()
                    return self.result(url, 'deferred', reason='status lookup paused; existing job retained', response=response_metadata(status, headers, obj))
                if status in (404, 410):
                    pending['review_required'] = True
                    pending['status_lookup_http'] = status
                    self.save()
                    return self.result(url, 'review_required', reason='job status unavailable or expired; no automatic resubmission')
                if status != 200:
                    raise LookupFailure('Status endpoint HTTP %s' % status)
                if obj.get('status') == 'success':
                    try:
                        started = latest_attempt_time(pending)
                        fresh = started is not None and started - 5 <= epoch(obj.get('timestamp', '')) <= time.time() + 60
                    except (ValueError, TypeError):
                        fresh = False
                    original_matches = identity(obj.get('original_url')) == identity(url)
                    returned_timestamp = obj.get('timestamp')
                    pending['status_evidence'] = {
                        'status': 'success', 'http_status': status,
                        'checked_at': timestamp(time.time()),
                        'timestamp_valid_format': isinstance(returned_timestamp, str)
                            and bool(re.fullmatch(r'[0-9]{14}', returned_timestamp)),
                        'timestamp_fresh': fresh,
                        'original_url_present': bool(obj.get('original_url')),
                        'original_url_matches': original_matches,
                    }
                    if pending['status_evidence']['timestamp_valid_format']:
                        pending['status_evidence']['timestamp'] = returned_timestamp
                    # Only echo the known requested URL, never an arbitrary
                    # response URL that could contain credentials or tokens.
                    if original_matches:
                        pending['status_evidence']['original_url'] = url
                    self.save()
                    resolved = self.classify_success(url, pending)
                    if resolved:
                        return resolved
                    if not fresh or not original_matches:
                        pending['review_required'] = True
                        self.save()
                        return self.result(url, 'review_required', reason='job success lacks usable matching archive evidence; retained for review')
                    # Indexing can lag. Keep job ID for later runs; do not resubmit.
                elif obj.get('status') == 'error':
                    # A live accepted job returned bad-request despite a usable
                    # new capture. Even recognized status errors cannot prove
                    # no capture was made. Preserve and independently reconcile.
                    pending['status_error'] = True
                    pending['review_required'] = True
                    self.save()
                    evidence = self.client.verify(url, self.since)
                    if evidence:
                        return self.verified_result(url, evidence, 'status_error_reconciled_index')
                    return self.result(url, 'review_required', reason='job reported error; no usable indexed capture yet, no automatic resubmission')
                time.sleep(10)
            return self.result(url, 'pending', reason='job or independent index verification pending')
        except RequestDeferred:
            pending = self.state.get(url, {})
            attempted = bool(pending.get('request_evidence', {}).get('attempts'))
            if created_pending and not attempted:
                self.retire_pending(url, 'budget_not_submitted')
            if created_pending and attempted:
                return self.result(url, 'pending', reason='pacing budget ended after submission; job retained')
            return self.result(url, 'deferred', reason='Archive pacing/time budget; untouched queue retained', queue_untouched=True)
        except (LookupFailure, ValueError, OSError) as exc:
            detail = getattr(exc, 'transport', None)
            if detail:
                self._operation_transport_failed = True
                pending = self.state.get(url)
                if pending:
                    pending['transport_failure'] = detail
                    if created_pending and not pending.get('job_id') and detail.get('known_not_sent') is True:
                        pending['known_not_sent'] = True
                        pending['retry_not_before'] = max(self.next_submit, time.time() + 60)
                        self.state['_next_submit'] = self.next_submit = pending['retry_not_before']
                    self.save()
                return self.result(url, 'unverified', reason='Archive transport failed', transport=detail)
            return self.result(url, 'unverified', reason=str(exc))

def main():
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--verify-only', action='store_true')
    mode.add_argument('--poll-only', action='store_true', help='Read existing job status and archive evidence; never submit')
    parser.add_argument('--scope', choices=('all', 'priority', 'pilot', 'supplemental', 'transport_probe', 'pacing_probe', 'pacing_job_diagnostic', 'account_diagnostic', 'supplemental_probe'), default='all')
    parser.add_argument('--state', default='.capture-state/pending.json')
    mode.add_argument('--inspect-state', action='store_true', help='Emit sanitized saved-state summary only; no network or state mutation')
    parser.add_argument('--budget', type=int, default=17400)
    args = parser.parse_args()
    if args.inspect_state:
        path = Path(args.state)
        state = json.loads(path.read_text()) if path.exists() else {}
        summary = operational_summary(state)
        output = Path('operational-report.json')
        if (output.resolve() != path.resolve()
                and not (output.exists() and path.exists() and output.samefile(path))):
            output.write_text(json.dumps(summary, indent=2))
        print(json.dumps(summary), flush=True)
        return 0
    client = Client()
    budget = min(args.budget, 1800) if args.scope in ('pacing_probe', 'pacing_job_diagnostic', 'account_diagnostic', 'supplemental_probe') else args.budget
    runner = Runner(client, args.state, budget, verify_only=args.verify_only, poll_only=args.poll_only)
    if args.scope == 'account_diagnostic':
        row = runner.diagnose_account()
        print('Read-only possibly-cached account-capacity observation; not authentication proof or capture coverage.')
        return 0 if row['state'] == 'diagnostic-observed' else 1
    if args.scope == 'pacing_job_diagnostic':
        row = runner.diagnose_saved_pacing_job()
        print('Read-only saved-job observation only; not capture coverage.')
        return 0 if row['state'] == 'diagnostic-observed' else 1
    urls = [] if args.scope == 'supplemental' else [BASE + p for p in (['/pages/warranty-policy'] if args.scope == 'pilot' else PRIORITY)]
    if args.scope == 'transport_probe':
        urls = [TRANSPORT_PROBE]
    if args.scope == 'pacing_probe':
        urls = [PACING_PROBE]
    if args.scope == 'supplemental':
        try:
            urls = discover_supplemental(client)
            if not urls:
                runner.result(BASE + '/sitemap.xml', 'no-additional-targets', reason='published sitemap is fully covered by existing all-scope inventory')
        except LookupFailure as exc:
            runner.result(BASE + '/sitemap.xml', 'discovery-incomplete', reason=str(exc))
    if args.scope == 'all':
        try:
            urls.extend(discover(client))
        except LookupFailure as exc:
            runner.result(BASE + '/collections/all', 'discovery-incomplete', reason=str(exc))
    if args.scope == 'supplemental_probe':
        runner.run_supplemental_probe()
    else:
        runner.run_queue(urls, args.scope)
    counts = {state: sum(r['state'] == state for r in runner.results) for state in sorted({r['state'] for r in runner.results})}
    summary = 'UTC date: %s\n\nResults: %s\n\nFull per-URL evidence is in capture-report.json.\n' % (timestamp(runner.since)[:8], json.dumps(counts))
    print(summary)
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        Path(os.environ['GITHUB_STEP_SUMMARY']).write_text(summary)
    return 0 if runner.results and all(r['state'] in ('verified', 'no-additional-targets') for r in runner.results) else 1

if __name__ == '__main__':
    raise SystemExit(main())
