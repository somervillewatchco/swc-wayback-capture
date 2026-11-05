#!/usr/bin/env python3
"""Conservative daily SPN capture and independent CDX/replay verification."""
import argparse
import datetime as dt
import gzip
import hashlib
import html
from html.parser import HTMLParser
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
import math

BASE = 'https://somervillewatchco.com'
PRIORITY = ['/', '/policies/refund-policy', '/policies/shipping-policy',
 '/policies/terms-of-service', '/policies/privacy-policy',
 '/policies/contact-information', '/policies/legal-notice', '/pages/contact',
 '/pages/warranty-policy', '/pages/no-brand-affiliation',
 '/policies/subscription-policy', '/cart']
# Temporary user-authorized scope: cart plus policy/legal pages, with no product discovery.
POLICIES = [path for path in PRIORITY if path not in ('/', '/pages/contact', '/pages/no-brand-affiliation')]
TRANSPORT_PROBE = BASE + '/products/date-mod-ice-blue-arabic-watch'
PACING_PROBE = BASE + '/products/date-mod-olive-green-watch'
SUPPLEMENTAL_PROBE = BASE + '/pages/about-us'
OUTLINK_PROBE = BASE + '/collections/all'
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

def is_product(url):
    return bool(re.fullmatch(re.escape(BASE) + r'/products/[A-Za-z0-9-]+', url))

def load_outlink_inventory():
    path = Path(__file__).with_name('outlink_inventory.json')
    obj = json.loads(path.read_text())
    roots, products = obj.get('root_urls'), obj.get('product_urls')
    if not isinstance(roots, list) or not 1 <= len(roots) <= 20:
        raise LookupFailure('Invalid frozen Shop All root inventory')
    expected = [OUTLINK_PROBE] + [OUTLINK_PROBE + '?page=' + str(i) for i in range(2, len(roots)+1)]
    if roots != expected:
        raise LookupFailure('Outlink roots must be exact Shop All pagination; no other roots allowed')
    if (not isinstance(products, list) or not 1 <= len(products) <= 5000
        or len(set(products)) != len(products)
        or any(not isinstance(u, str) or not re.fullmatch(re.escape(BASE) + r'/products/[a-z0-9][a-z0-9-]*', u) for u in products)):
        raise LookupFailure('Invalid frozen public product inventory')
    canonical = {'root_urls': roots, 'product_urls': products}
    digest = hashlib.sha256(json.dumps(canonical, sort_keys=True).encode()).hexdigest()
    return canonical, digest


def outlink_child_reference(raw_url, known):
    """Unknown/external paths, queries and userinfo are never persisted."""
    if not isinstance(raw_url, str):
        return None
    digest = hashlib.sha256(raw_url.encode()).hexdigest()
    reference = {'url_sha256': digest}
    try:
        parsed = up.urlsplit(raw_url)
        if parsed.scheme not in ('http', 'https'):
            return reference
        host = parsed.hostname
        if isinstance(host, str) and re.fullmatch(r'[a-z0-9.-]{1,253}', host):
            reference['hostname'] = host
        if raw_url in known and parsed.username is None and parsed.password is None:
            reference['url'] = raw_url
    except ValueError:
        pass
    return reference


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
        'max-daily-bandwidth-host host-crawling-paused').split()}
    return value if isinstance(value, str) and value in allowed else 'unknown'

def response_metadata(http_status, headers, obj):
    """Allowlisted diagnostics only; never return raw messages, URLs or headers."""
    obj = obj if isinstance(obj, dict) else {}
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
    # Observations mirror pause's existing combined-detail predicates; they do
    # not decide control flow or retain raw server text (including malformed fields).
    detail = (str(obj.get('status_ext', '')) + ' ' + str(obj.get('message', ''))).lower()
    result['pause_triggers'] = [name for name, matched in (
        ('http_429', http_status == 429),
        ('host_crawling_paused', 'host-crawling-paused' in detail),
        ('host_pause_message', 'crawling this host is paused' in detail),
        ('reported_http_429', bool(re.search(r'http status\s*[=:]\s*429\b', detail))),
        ('overloaded', 'overloaded' in detail),
    ) if matched]
    # The provider can put an explanatory error in either text field. Preserve
    # only its allowlisted meaning, even when status_ext is not a known enum.
    safe_detail = ' '.join(value.lower() for value in
                          (obj.get('status_ext'), obj.get('message'))
                          if isinstance(value, str))
    result['pause_reported_in'] = [field for field in ('status_ext', 'message')
        if isinstance(obj.get(field), str) and (
            'host-crawling-paused' in obj[field].lower()
            or 'crawling this host is paused' in obj[field].lower()
            or 'overloaded' in obj[field].lower()
            or re.search(r'http status\s*[=:]\s*429\b', obj[field], re.I))]
    if 'host-crawling-paused' in safe_detail or 'crawling this host is paused' in safe_detail:
        result['message_category'] = 'host_paused'
    reported_codes = sorted({int(code) for code in
        re.findall(r'\bhttp status\s*[:=]\s*([1-5][0-9]{2})\b', safe_detail)})
    if reported_codes:
        result['provider_reported_http_statuses'] = reported_codes
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
    result.update(safe_cache_metadata(headers))
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


def safe_cache_metadata(headers):
    """Reported cache headers are observations, never proof of freshness."""
    result = {'cache_freshness': 'unknown'}
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
    result.update(safe_cache_metadata(headers))
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
        if not re.fullmatch(r'(all|priority|policies|pilot|supplemental|transport_probe|pacing_probe|supplemental_probe):[0-9]{8}:(capture|poll|verify)(?::daily-v[23])?(?::pass:[0-9]+)?', key):
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
            if label not in ('verified', 'unverified', 'deferred', 'pending', 'failed', 'review_required', 'reused', 'unsupported-document-type', 'discovery-incomplete', 'no-additional-targets', 'outlink-held', 'state-held'):
                label = 'unknown'
            counts[label] = counts.get(label, 0) + 1
        queues.append({'key': key, 'total': total, 'cursor': cursor,
                       'remaining': max(0, total - cursor), 'complete': queue.get('complete') is True,
                       'in_flight': queue.get('started') is not None, 'processed_result_counts': counts})
        if queue.get('phase') in ('index_before', 'capture', 'index_after'):
            queues[-1]['phase'] = queue['phase']
    def safe_time(key):
        value = state.get(key, 0)
        return value if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value < 1e15 else 0
    pending = [v for k, v in state.items() if isinstance(k, str) and k.startswith(BASE + '/') and isinstance(v, dict)]
    events = state.get('_recovery_events', [])
    events = events if isinstance(events, list) else []
    journals = state.get('_outlink_batches', {})
    journals = journals if isinstance(journals, dict) else {}
    journal_manifests = []
    for batch in journals.values():
        if not isinstance(batch, dict):
            continue
        children = batch.get('children', {})
        children = children if isinstance(children, dict) else {}
        journal_manifests.append({'sha256': hashlib.sha256(json.dumps(batch, sort_keys=True).encode()).hexdigest(),
            'children': len(children),
            'mapped_children': sum(bool(c.get('job_ids')) for c in children.values() if isinstance(c, dict)),
            'verified_children': sum(c.get('coverage') == 'verified' for c in children.values() if isinstance(c, dict)),
            'mapping_reported': batch.get('expansion_state') == 'mapping_reported'})
    return {'checked_at': timestamp(time.time()), 'queues': queues,
            'outlink_journals': {'total': len(journals), 'manifests': journal_manifests},
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
        self.archive_response_callback = None
        self.archive_requests_stopped = False
        self.post_utc_day_end = float('inf')

    def paced_request(self, url, data=None, authenticated=False, attempt=1):
        is_archive = up.urlsplit(url).hostname in ('web.archive.org', 'archive.org')
        gated = is_archive and self.archive_interval > 0
        if gated:
            if self.archive_requests_stopped:
                raise RequestDeferred('Three paused Archive responses; run stopped')
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
        # Recheck after every pacing wait and safe retry, before physical POST.
        if data is not None and time.time() >= self.post_utc_day_end:
            raise RequestDeferred('Intended UTC day ended; no new submission')
        # Timestamp/audit must follow pacing and precede actual network IO.
        if data is not None and self.post_attempt_callback is not None:
            self.post_attempt_callback({'attempt': attempt, 'started_at': time.time()})
        # Persistence callbacks can take time: never cross midnight while
        # journaling and then physically dispatch an obsolete-day POST.
        if data is not None and time.time() >= self.post_utc_day_end:
            raise RequestDeferred('Intended UTC day ended during attempt persistence; no submission')
        response = None
        self.archive_observed_retry_after = 0
        self.archive_observed_status = None
        try:
            response = self._request(url) if data is None and not authenticated else self._request(url, data, authenticated)
            return response
        finally:
            if gated:
                code, headers, obj = self.archive_observed_status, {}, {}
                until = max(time.time() + self.archive_interval, self.archive_observed_retry_after)
                if response is not None:
                    code = response[0]
                    headers = {k.lower(): v for k, v in response[1].items()}
                    try:
                        obj = json.loads(response[2])
                    except (ValueError, TypeError, RecursionError):
                        obj = {'message': response[2]}
                    detail = response_metadata(response[0], headers, obj)
                    until = max(until, detail.get('retry_after_until', 0))
                    if detail['pause_triggers']:
                        until = max(until, time.time() + 1200)
                self.archive_next_request_at = max(self.archive_next_request_at, until)
                if self.archive_pacing_callback:
                    self.archive_pacing_callback(self.archive_next_request_at)
                if code is not None and self.archive_response_callback:
                    self.archive_response_callback(url, code, headers, obj)

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
                    self.archive_observed_status = response.status
                    safe_headers = {k.lower(): v for k, v in response.headers.items()}
                    self.archive_observed_retry_after = response_metadata(response.status, safe_headers, {}).get('retry_after_until', 0)
                    if response.status == 429:
                        # A failed body read must not discard an observed throttle.
                        self.archive_observed_retry_after = max(self.archive_observed_retry_after, time.time() + 1200)
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
            if status == 429 or 'overloaded' in body.lower():
                # A non-JSON overload has no proof of non-acceptance unless 429.
                return status, {k.lower(): v for k, v in headers.items()}, {
                    'status': 'error' if status == 429 else 'unknown',
                    'status_ext': 'http-429' if status == 429 else 'overloaded'}
            raise LookupFailure('Non-JSON response (HTTP %s)' % status) from None
        return status, {k.lower(): v for k, v in headers.items()}, obj

    def verify(self, url, since, until=None):
        upper = min(until, time.time() + 60) if until is not None else time.time() + 60
        query = up.urlencode({'url': url, 'matchType': 'exact', 'from': timestamp(since)[:8] if timestamp(since).endswith('000000') else timestamp(since),
            'to': timestamp(upper), 'output': 'json',
            'fl': 'timestamp,original,statuscode,mimetype,digest'})
        status, _, data = self.json('https://web.archive.org/cdx/search/cdx?' + query)
        return self.verify_rows(url, since, upper, self.cdx_rows(status, data))

    def verify_many(self, urls, since, until=None):
        """Read-only daily evidence iterator; never authorizes a submission.

        One bounded, complete host index query serves the known daily targets.
        Every positive result still needs an individual exact replay. A None
        result means only that this snapshot did not yield usable evidence;
        it is not proof that a pending or uncertain job can be resubmitted.
        The capture scheduler does not use this proposal component yet.
        """
        if (not isinstance(urls, (list, tuple)) or len(urls) > 5000
                or any(not isinstance(url, str) or not (
                    url in {BASE + path for path in PRIORITY} or is_product(url))
                    for url in urls)):
            raise LookupFailure('Shared index requires known daily target URLs')
        targets = list(dict.fromkeys(urls))
        if not targets:
            return
        if (not _number(since) or not timestamp(since).endswith('000000')
                or since != epoch(timestamp(since))
                or (until is not None and (not _number(until)
                    or not since <= until < since + 86400))):
            raise LookupFailure('Shared index requires one exact UTC-day window')
        upper = min(since + 86399, time.time() + 60,
                    until if until is not None else since + 86399)
        if upper < since:
            raise LookupFailure('Shared index day has not begun')
        # showResumeKey also reveals a server-side cap below our requested cap.
        # A continuation or a full-size response fails closed; no pagination,
        # partial evidence, or negative-cache assumption leaks to callers.
        limit = 5000
        query = up.urlencode({'url': BASE + '/', 'matchType': 'host',
            'from': timestamp(since), 'to': timestamp(upper), 'output': 'json',
            'fl': 'timestamp,original,statuscode,mimetype,digest',
            'filter': ['statuscode:2[0-9]{2}',
                       r'mimetype:(text/html|application/xhtml\+xml|warc/revisit)'],
            'limit': limit, 'showResumeKey': 'true'}, doseq=True)
        status, _, data = self.json('https://web.archive.org/cdx/search/cdx?' + query)
        if isinstance(data, list) and len(data) > limit:
            raise LookupFailure('Shared CDX response reached its bounded row limit')
        rows = self.cdx_rows(status, data)
        # Validate every row before the first replay or emitted result, even
        # when the malformed row is for a URL outside the supplied inventory.
        by_url = {url: [] for url in targets}
        for row, captured in rows:
            if row['original'] in by_url:
                by_url[row['original']].append((row, captured))
        for url in targets:
            yield url, self.verify_rows(url, since, upper, by_url[url])

    @staticmethod
    def cdx_rows(status, data):
        if status != 200 or not isinstance(data, list):
            raise LookupFailure('CDX unavailable')
        if not data:
            return []
        fields = data[0]
        expected = {'timestamp', 'original', 'statuscode', 'mimetype', 'digest'}
        if (not isinstance(fields, list) or len(fields) != len(expected)
            or any(not isinstance(field, str) for field in fields)
            or set(fields) != expected):
            raise LookupFailure('Invalid CDX schema')
        rows = []
        # Validate the entire response before replaying any row. Malformed
        # evidence is a lookup failure, never absence or partial success.
        for values in data[1:]:
            if not isinstance(values, list) or len(values) != len(fields):
                raise LookupFailure('Incomplete CDX row')
            if any(not isinstance(value, str) or not value
                   or re.search(r'[\x00-\x1f\x7f]', value) for value in values):
                raise LookupFailure('Invalid CDX field')
            row = dict(zip(fields, values))
            if not re.fullmatch(r'[0-9]{14}', row['timestamp']):
                raise LookupFailure('Invalid CDX timestamp')
            try:
                captured = epoch(row['timestamp'])
                if timestamp(captured) != row['timestamp']:
                    raise ValueError('Noncanonical timestamp')
                original = up.urlsplit(row['original'])
                if (original.scheme not in ('http', 'https') or not original.hostname
                    or original.username is not None or original.password is not None
                    or original.port not in (None, 80 if original.scheme == 'http' else 443)
                    or re.search(r'\s', row['original'])):
                    raise ValueError('Invalid original URL')
            except (ValueError, OverflowError, OSError):
                raise LookupFailure('Invalid CDX timestamp or original URL') from None
            if not re.fullmatch(r'(?:[1-5][0-9]{2}|-)', row['statuscode']):
                raise LookupFailure('Invalid CDX status code')
            if not re.fullmatch(r"(?:[A-Za-z0-9!#$&^_.+\-]+/[A-Za-z0-9!#$&^_.+\-]+|-)", row['mimetype']):
                raise LookupFailure('Invalid CDX MIME type')
            rows.append((row, captured))
        return rows

    def verify_rows(self, url, since, upper, rows):
        for row, captured in reversed(rows):
            if not row['statuscode'].startswith('2') or row['mimetype'] not in ('text/html', 'application/xhtml+xml', 'warc/revisit'):
                continue
            if row['original'] != url or not identity(url):
                continue
            if not since <= captured <= upper:
                continue
            replay = 'https://web.archive.org/web/' + row['timestamp'] + 'id_/' + row['original']
            code, headers, body, final = self.request(replay)
            if final != replay:
                continue  # Never accept Wayback's redirect to a different capture/page.
            lower = {k.lower(): v for k, v in headers.items()}
            if not 200 <= code < 300 or 'html' not in lower.get('content-type', '').lower() or not usable_html(body, url):
                continue
            return {**row, 'replay_url': replay, 'verified_at': timestamp(time.time())}
        return None

class ChallengeHeadings(HTMLParser):
    """Extract document headings, excluding inert markup; not a CSS renderer."""
    IGNORED = frozenset(('script', 'style', 'template'))
    HEADINGS = frozenset(('h1', 'h2', 'h3', 'h4', 'h5', 'h6'))

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.ignored = []
        self.heading = None
        self.parts = []
        self.headings = []

    def handle_starttag(self, tag, attrs):
        if tag in self.IGNORED:
            self.ignored.append(tag)
        if self.ignored:
            return
        if tag in self.HEADINGS:
            self.finish_heading()
            self.heading = tag
        elif tag == 'br' and self.heading:
            self.parts.append(' ')

    def handle_endtag(self, tag):
        if self.ignored:
            if tag == self.ignored[-1]:
                self.ignored.pop()
            return
        if tag in self.HEADINGS:
            self.finish_heading()

    def handle_data(self, data):
        if not self.ignored and self.heading:
            self.parts.append(data)

    def finish_heading(self):
        if self.heading:
            self.headings.append(' '.join(''.join(self.parts).split()))
        self.heading, self.parts = None, []


def has_challenge_heading(body):
    parser = ChallengeHeadings()
    parser.feed(body)
    parser.close()
    parser.finish_heading()
    # Match prompts, not explanatory prose that happens to mention humans.
    prompt = (r'(?:verify (?:that )?you are (?:a )?human|'
              r'please verify you are human to continue|verify you are not a robot|'
              r'security verification|checking your browser|bot verification|'
              r'robot check|just a moment)[.!?…]*')
    return any(re.fullmatch(prompt, heading, re.I) for heading in parser.headings)


class CanonicalLinks(HTMLParser):
    """Read active canonical links using HTML attribute and entity semantics."""
    IGNORED = frozenset(('script', 'style', 'template'))

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.ignored = []
        self.links = []
        self.base = None
        self.invalid = False

    def handle_starttag(self, tag, attrs):
        if tag in self.IGNORED:
            self.ignored.append(tag)
        if self.ignored:
            return
        if tag == 'base' and self.base is None:
            bases = [value for name, value in attrs if name == 'href']
            if bases:
                self.base = bases[0] or ''
                self.invalid |= len(bases) != 1
        if tag != 'link':
            return
        rels = [value for name, value in attrs if name == 'rel']
        if not any('canonical' in (value or '').lower().split() for value in rels):
            return
        hrefs = [value for name, value in attrs if name == 'href']
        if len(rels) != 1 or len(hrefs) != 1 or not hrefs[0] or not hrefs[0].strip():
            self.invalid = True
        else:
            self.links.append(hrefs[0].strip())

    def handle_endtag(self, tag):
        if self.ignored and tag == self.ignored[-1]:
            self.ignored.pop()


def canonical_links_match(body, url):
    parser = CanonicalLinks()
    try:
        parser.feed(body)
        parser.close()
        if parser.invalid:
            return False
        base = up.urljoin(url, parser.base) if parser.base is not None else url
        for href in parser.links:
            target = up.urljoin(base, href)
            parsed = up.urlsplit(target)
            if (parsed.username is not None or parsed.password is not None
                or parsed.port not in (None, 80 if parsed.scheme == 'http' else 443)
                or identity(target) != identity(url) or identity(url) is None):
                return False
    except ValueError:
        return False
    return True


class PageContent(HTMLParser):
    """Active title/H1 text and bounded JSON-LD, never script/template lookalikes."""
    IGNORED = frozenset(('script', 'style', 'template', 'noscript', 'svg', 'math', 'textarea'))
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.ignored = []
        self.tag = None
        self.parts = []
        self.titles, self.headings, self.products = [], [], []
        self.ld = None

    def handle_starttag(self, tag, attrs):
        if self.ignored:
            if tag in self.IGNORED:
                self.ignored.append(tag)
            return
        if tag in self.IGNORED:
            self.ignored.append(tag)
            if tag == 'script' and (dict(attrs).get('type') or '').lower() == 'application/ld+json':
                self.ld = []
        elif tag in ('title', 'h1'):
            self.tag, self.parts = tag, []

    def handle_data(self, data):
        if self.ld is not None:
            self.ld.append(data)
        elif not self.ignored and self.tag:
            self.parts.append(data)

    def handle_endtag(self, tag):
        if self.ignored:
            if tag != self.ignored[-1]:
                return
            self.ignored.pop()
            if tag == 'script' and self.ld is not None:
                raw, self.ld = ''.join(self.ld), None
                if len(raw) <= 1024 * 1024 and len(self.products) < 128:
                    try:
                        obj = json.loads(raw)
                        nodes = obj if isinstance(obj, list) else [obj]
                        for node in nodes[:128]:
                            if isinstance(node, dict):
                                graph = node.get('@graph', [node])
                                if isinstance(graph, list):
                                    self.products.extend(n for n in graph[:max(0, 128 - len(self.products))] if isinstance(n, dict))
                    except (ValueError, RecursionError):
                        pass
        elif tag == self.tag:
            (self.titles if tag == 'title' else self.headings).append(''.join(self.parts))
            self.tag, self.parts = None, []

    def product_matches(self, url, title):
        normalize = lambda text: ' '.join(html.unescape(text).split()).casefold()
        for node in self.products:
            kind = node.get('@type')
            if kind != 'Product' and not (isinstance(kind, list) and 'Product' in kind):
                continue
            refs = [node[k] for k in ('url', '@id') if k in node]
            if not refs or any(not isinstance(ref, str) or up.urldefrag(up.urljoin(url, ref))[0] != url for ref in refs):
                continue
            name = node.get('name')
            if (not isinstance(name, str) or not name.strip() or normalize(name) == 'somerville watch co.'
                    or normalize(name) not in normalize(title)
                    or normalize(name) not in [normalize(h) for h in self.headings]):
                continue
            offers = node.get('offers', [])
            for offer in (offers if isinstance(offers, list) else [offers]):
                if not isinstance(offer, dict):
                    continue
                price = str(offer.get('price', ''))
                currency = offer.get('priceCurrency', '')
                target = offer.get('url', url)
                if not isinstance(target, str):
                    continue
                target = up.urlsplit(up.urljoin(url, target))
                if (up.urlunsplit((target.scheme, target.netloc, target.path, '', '')) != url
                        or any(k != 'variant' for k, _ in up.parse_qsl(target.query, keep_blank_values=True))):
                    continue
                if (re.fullmatch(r'[0-9]{1,12}(?:\.[0-9]{1,4})?', price) and float(price) > 0
                        and isinstance(currency, str) and re.fullmatch(r'[A-Z]{3}', currency)):
                    return True
        return False


def usable_html(body, url):
    page = PageContent()
    try:
        page.feed(body)
        page.close()
        if len(body) < 512 or len(page.titles) != 1:
            return False
        title = html.unescape(page.titles[0]).strip()
        if 'Somerville Watch Co.' not in title:
            return False
        if re.search(r'just a moment|access denied|temporarily unavailable|robot check|page not found|verify you are human', title, re.I):
            return False
        if has_challenge_heading(body) or not canonical_links_match(body, url):
            return False
        if is_product(url):
            return page.product_matches(url, title)
    except (ValueError, RecursionError):
        return False
    # Legacy controlled-cart reconciliation remains conservative, but it is no
    # longer a daily target. Do not confuse an empty cart shell with that SKU.
    if '/cart/51183558787357:1' in url:
        return bool(re.search(r'name=[\'"]id[\'"][^>]*value=[\'"]51183558787357', body))
    return True

def discover(client):
    seen = set()
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
            return [BASE + p for p in sorted(seen)]
        if not paths - seen:
            raise LookupFailure('Catalog pagination repeats products')
        seen.update(paths)
        # Stop only on a page that has no link to a later collection page.
        linked = [int(n) for n in re.findall(r'[?&](?:amp;)?page=(\d+)', html.unescape(body))]
        if not any(n > page for n in linked):
            return [BASE + p for p in sorted(seen)]
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

class StateContinuityError(RuntimeError):
    """No requests or state replacement are safe until reviewed recovery."""


def _reject(message):
    raise StateContinuityError(message)


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and 0 <= value < 1e15


def outlink_attempt_time(batch):
    """Choose the first valid timestamp, without aging out a newer primary."""
    if not isinstance(batch, dict):
        return None
    attempt = batch.get('attempt')
    started = attempt.get('started_at') if isinstance(attempt, dict) else None
    for value in (started, batch.get('utc_day'), batch.get('created_at')):
        # The existing epoch domain also rejects NaN, infinity and huge ints
        # without converting them to float (which could overflow).
        if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 < value < 1e15:
            return value
    return None


def outlink_attempt_before_today(batch):
    """A valid selected timestamp before today's UTC midnight is historical."""
    started = outlink_attempt_time(batch)
    today = dt.datetime.fromtimestamp(time.time(), UTC).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    return started is not None and started < today


def decode(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                _reject('Duplicate state JSON key; preserve bytes for review')
            result[key] = value
        return result
    try:
        return json.loads(raw, object_pairs_hook=pairs,
            parse_constant=lambda _: _reject('Non-finite state JSON value'))
    except (ValueError, UnicodeError):
        _reject('Unreadable state JSON; preserve bytes for review')


def validate_state(state):
    if not isinstance(state, dict):
        _reject('State must be a JSON object')
    if not state:
        _reject('Empty state requires reviewed recovery; no initialization permitted')
    metadata = {'_queues', '_completed_jobs', '_outlink_batches', '_next_submit',
                '_transport_cooldown_until', '_archive_next_request_at',
                '_recovery_events', '_account_diagnostic', '_pause_responses'}
    for key, value in state.items():
        if key in ('_next_submit', '_transport_cooldown_until', '_archive_next_request_at'):
            if not _number(value):
                _reject('Invalid persisted cooldown')
        elif key == '_pause_responses':
            if (not isinstance(value, dict) or not isinstance(value.get('run'), str)
                    or not re.fullmatch(r'[0-9]+:[0-9]+', value['run'])
                    or not isinstance(value.get('count'), int) or isinstance(value['count'], bool)
                    or not 0 <= value['count'] <= 3):
                _reject('Invalid persisted pause breaker')
        elif key == '_queues':
            if not isinstance(value, dict):
                _reject('Invalid queue map')
            for qkey, queue in value.items():
                if not isinstance(qkey, str) or not isinstance(queue, dict):
                    _reject('Invalid queue entry')
                urls, cursor = queue.get('urls'), queue.get('cursor')
                if (not isinstance(urls, list) or any(not isinstance(u, str) or not u.startswith(BASE + '/') for u in urls)
                        or not isinstance(cursor, int) or isinstance(cursor, bool) or not 0 <= cursor <= len(urls)
                        or not isinstance(queue.get('results'), list) or any(not isinstance(r, dict) for r in queue['results'])
                        or not isinstance(queue.get('complete'), bool)):
                    _reject('Invalid queue checkpoint')
                for marker in ('started', 'retry_same_url'):
                    if queue.get(marker) is not None and (cursor >= len(urls) or queue[marker] != urls[cursor]):
                        _reject('Inconsistent interrupted queue operation')
                if ':daily-v3' in qkey:
                    start, end = queue.get('batch_start'), queue.get('batch_end')
                    if (queue.get('phase') not in ('index_before', 'capture', 'index_after')
                            or type(start) is not int or type(end) is not int
                            or not 0 <= start <= cursor <= end <= len(urls)
                            or end - start > 8
                            or (queue['phase'] == 'index_after' and cursor != end)
                            or (queue['complete'] and cursor != len(urls))):
                        _reject('Invalid daily batch checkpoint')
        elif key == '_outlink_batches':
            if not isinstance(value, dict):
                _reject('Invalid outlink journal map')
            for root, batch in value.items():
                if (not isinstance(root, str) or not isinstance(batch, dict)
                        or batch.get('root_url') != root or not isinstance(batch.get('children'), dict)
                        or not isinstance(batch.get('child_order'), list)):
                    _reject('Invalid outlink journal')
                if batch.get('post_state') not in ('not_dispatched', 'not_dispatched_budget', 'not_sent_proven',
                        'dispatch_started', 'response_received', 'outcome_unknown'):
                    _reject('Invalid or missing outlink dispatch state')
                if batch.get('expansion_state') not in ('unknown', 'mapping_reported'):
                    _reject('Invalid or missing outlink mapping confidence')
                children, order = batch['children'], batch['child_order']
                if (any(not isinstance(c, dict) or not isinstance(c.get('job_ids'), list)
                        or any(not isinstance(j, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', j) for j in c['job_ids']) for c in children.values())
                        or any(not isinstance(k, str) or k not in children for k in order)):
                    _reject('Invalid outlink child mapping')
                if 'server_paused_until' in batch and not _number(batch['server_paused_until']):
                    _reject('Invalid outlink cooldown')
                cursor = batch.get('verification_cursor')
                day = batch.get('utc_day')
                # Permit legacy date-field fallback, retaining the old zero-day
                # sentinel as a protected hold when no valid date is available.
                valid_date = outlink_attempt_time(batch) is not None or (
                    isinstance(day, (int, float)) and not isinstance(day, bool) and day == 0)
                if (not isinstance(cursor, int) or isinstance(cursor, bool) or not 0 <= cursor <= len(order)
                        or not valid_date):
                    _reject('Invalid outlink verification checkpoint')
        elif key in ('_completed_jobs', '_recovery_events'):
            if not isinstance(value, list) or any(not isinstance(v, dict) for v in value):
                _reject('Invalid retained history')
        elif key == '_account_diagnostic':
            if not isinstance(value, dict):
                _reject('Invalid account observation')
        elif key.startswith(BASE + '/'):
            if not isinstance(value, dict) or not value:
                _reject('Invalid pending job record')
            job = value.get('job_id')
            if job is not None and (not isinstance(job, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', job)):
                _reject('Invalid retained job ID')
            if ('paused_job_errors' in value and (type(value['paused_job_errors']) is not int
                    or not 0 <= value['paused_job_errors'] <= 3)):
                _reject('Invalid paused-job observation count')
            for name in ('submitted_at', 'retry_eligible_at', 'server_retry_after_until', 'status_paused_until', 'retry_not_before', 'next_status_at'):
                if name in value and not _number(value[name]):
                    _reject('Invalid pending job time')
            for name in ('request_evidence', 'status_evidence', 'terminal_reused'):
                if name in value and not isinstance(value[name], dict):
                    _reject('Invalid pending job evidence')
        elif key not in metadata:
            _reject('Unexpected state key; preserve for review')
    return state


def load_legacy_state(path, with_digest=False):
    """Validate shape only: legacy JSON does not establish historical continuity."""
    if path.is_symlink():
        _reject('State symlink requires reviewed recovery')
    try:
        raw = path.read_bytes()
    except OSError:
        _reject('Missing/unreadable state; no initialization permitted')
    state = validate_state(decode(raw))
    return (state, hashlib.sha256(raw).hexdigest()) if with_digest else state


class Runner:
    def __init__(self, client, state_path, budget=17400, interval=60, verify_only=False, poll_only=False):
        self.client, self.state_path = client, Path(state_path)
        self.state, self._state_digest = load_legacy_state(self.state_path, with_digest=True)
        self.started = time.time()
        self.since = dt.datetime.fromtimestamp(self.started, UTC).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        self.until = self.since + 86400 - 1
        self.deadline, self.interval = self.started + budget, max(60, interval)
        self.verify_only = verify_only
        self.poll_only = poll_only
        self.next_submit = self.state.get('_next_submit', 0)
        self.results = []
        self.transport_failure_streak = 0
        self.transport_circuit_open = False
        run_key = os.environ.get('GITHUB_RUN_ID', '') + ':' + os.environ.get('GITHUB_RUN_ATTEMPT', '1')
        self.run_key = run_key if re.fullmatch(r'[0-9]+:[0-9]+', run_key) else None
        pauses = self.state.get('_pause_responses', {})
        self.paused_responses = pauses.get('count', 0) if self.run_key and pauses.get('run') == self.run_key else 0
        self.pause_circuit_open = self.paused_responses >= 3
        self._spn_response_observed = False
        self.client.archive_requests_stopped = self.pause_circuit_open
        self.client.archive_response_callback = self.observe_archive_response
        self.client.post_retry_deadline = self.deadline
        self.client.post_utc_day_end = self.since + 86400
        self.client.archive_interval = 120
        self.client.archive_next_request_at = max(self.state.get('_archive_next_request_at', 0),
                                                 self.state.get('_transport_cooldown_until', 0), self.state.get('_next_submit', 0))
        self.client.archive_deadline = self.deadline
        def persist_archive_gate(until):
            self.state['_archive_next_request_at'] = until
            self.save()
        self.client.archive_pacing_callback = persist_archive_gate

    def save(self):
        validate_state(self.state)
        if not self.state_path.is_file() or self.state_path.is_symlink():
            _reject('State disappeared or changed type; no initialization permitted')
        try:
            existing = self.state_path.read_bytes()
        except OSError:
            _reject('State became unreadable; preserve for reviewed recovery')
        if hashlib.sha256(existing).hexdigest() != self._state_digest:
            _reject('State changed since load; refusing to overwrite unknown history')
        raw = json.dumps(self.state, indent=2, allow_nan=False).encode()
        tmp = self.state_path.with_suffix('.tmp')
        tmp.write_bytes(raw)
        tmp.replace(self.state_path)
        self._state_digest = hashlib.sha256(raw).hexdigest()
        self.write_operational_report()

    def write_operational_report(self):
        output = Path('operational-report.json')
        if (output.resolve() != self.state_path.resolve()
                and not (output.exists() and self.state_path.exists() and output.samefile(self.state_path))):
            summary = operational_summary(self.state)
            summary['state_continuity'] = {'mode': 'legacy-shape-validation-only',
                'historical_completeness': 'not-established'}
            output.write_text(json.dumps(summary, indent=2))

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
        for key in ('status_evidence', 'request_evidence', 'terminal_reused', 'retry_eligible_at', 'review_required', 'submission_response', 'status_response', 'transport_failure', 'known_not_sent', 'status_error', 'server_retry_after_until', 'status_paused_until', 'next_status_at', 'job_diagnostic', 'paused_job_errors'):
            if key in pending:
                row[key] = pending[key]
        return row

    def result(self, url, state, include_pending=True, **extra):
        row = {'url': url, 'state': state, 'capture_timestamp': extra.get('timestamp') if state == 'verified' else None, **extra, **(self.diagnostic_snapshot(url) if include_pending else {})}
        self.results.append(row)
        print(json.dumps(row), flush=True)
        Path('capture-report.json').write_text(json.dumps(self.results, indent=2))
        return row

    def retire_pending(self, url, state, **details):
        # Audit before removing, including transitions that do not emit a row.
        completed = self.state.setdefault('_completed_jobs', [])
        completed.append({'url': url, 'state': state, **details,
                          **self.diagnostic_snapshot(url), 'completed_at': timestamp(time.time())})
        self.state['_completed_jobs'] = completed  # Append-only; never erase uncertain history.
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

    def observe_archive_response(self, url, code, headers, obj):
        path = up.urlsplit(url).path
        spn = path in ('/save', '/save/') or bool(re.fullmatch(r'/save/status/(?!system$|user$)[A-Za-z0-9_-]+', path))
        self._spn_response_observed = spn
        job_id = obj.get('job_id') if isinstance(obj, dict) else None
        valid_job = isinstance(job_id, str) and re.fullmatch(r'[A-Za-z0-9_-]+', job_id)
        paused = self.pause(headers, {**(obj if isinstance(obj, dict) else {}), '_http': code},
                            observed=True, reset_streak=spn and isinstance(obj, dict)
                            and (obj.get('status') in ('success', 'pending', 'error') or valid_job))
        if paused:
            self._operation_paused = True

    def pause(self, headers, obj, observed=False, reset_streak=True):
        detail = response_metadata(obj.get('_http'), headers, obj)
        if not observed and self._spn_response_observed:
            self._spn_response_observed = False
            return bool(detail['pause_triggers'])  # Physical response already counted.
        if not detail['pause_triggers']:
            if not reset_streak:
                return False
            self.paused_responses = 0  # Only a nonpaused SPN response resets this.
            if self.run_key:
                self.state['_pause_responses'] = {'run': self.run_key, 'count': 0}
                self.save()
            return False
        self.paused_responses = min(3, self.paused_responses + 1)
        if self.run_key:
            self.state['_pause_responses'] = {'run': self.run_key, 'count': self.paused_responses}
        self.pause_circuit_open = self.paused_responses >= 3
        self.client.archive_requests_stopped = self.pause_circuit_open
        self.next_submit = max(time.time() + 1200, detail.get('retry_after_until', 0),
            self.next_submit, self.state.get('_next_submit', 0),
            self.state.get('_archive_next_request_at', 0), self.client.archive_next_request_at,
            self.state.get('_transport_cooldown_until', 0))
        self.state['_next_submit'] = self.next_submit
        self.client.archive_next_request_at = self.next_submit
        self.state['_archive_next_request_at'] = self.next_submit
        queue = getattr(self, 'active_queue', None)
        if queue and queue.get('started') is not None:
            queue['retry_same_url'] = queue['started']
        self.save()
        return True

    def product_attempted_today(self, url):
        # Reuse the append-only request journal, including pre-policy failures.
        # Reservation is saved before I/O, so an interrupted attempt also counts.
        records = [self.state.get(url, {})] + [row for row in self.state.get('_completed_jobs', []) if row.get('url') == url]
        for row in records:
            request = row.get('request_evidence', {})
            if not isinstance(request, dict):
                raise LookupFailure('Malformed product request history; no submission')
            attempts = request.get('attempts', [])
            if row.get('state') == 'budget_not_submitted' and not attempts:
                continue
            if (not isinstance(attempts, list) or any(not isinstance(a, dict) or not _number(a.get('started_at')) for a in attempts)
                    or ('submitted_at' in row and not _number(row['submitted_at']))):
                raise LookupFailure('Malformed product attempt history; no submission')
            times = [row.get('submitted_at')] + [a.get('started_at') for a in attempts if isinstance(a, dict)]
            if any(isinstance(t, (int, float)) and not isinstance(t, bool) and self.since <= t < self.since + 86400 for t in times):
                return True
        for batch in self.state.get('_outlink_batches', {}).values():
            if outlink_attempt_before_today(batch):
                continue
            mapped = any(child.get('job_ids') and (child.get('url') == url
                         or child.get('url_sha256') == hashlib.sha256(url.encode()).hexdigest())
                         for child in batch.get('children', {}).values())
            if not mapped:
                continue
            started = outlink_attempt_time(batch)
            if started is None:
                raise LookupFailure('Malformed outlink attempt history; no submission')
            today = dt.datetime.fromtimestamp(time.time(), UTC).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
            if started >= today + 86400:
                raise LookupFailure('Future outlink attempt history; no submission')
            if today <= started:
                return True
        return False

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
            evidence = self.client.verify(url, self.since, until=self.until)
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

    def process(self, url, *, shared_index=False):
        # A per-run circuit counts URL operations with exhausted Archive
        # transport failures; successful intermediate CDX reads cannot erase a
        # subsequent failed POST/status lookup for the same URL.
        if self.pause_circuit_open:
            return self.result(url, 'deferred', reason='three consecutive paused Archive responses; run stopped', queue_untouched=True)
        if self.transport_circuit_open or self.state.get('_transport_cooldown_until', 0) > time.time():
            return self.result(url, 'deferred', reason='local transport circuit cooldown; no network requests made', queue_untouched=True)
        before_failed = getattr(self.client, 'archive_transport_failures', 0)
        before_healthy = getattr(self.client, 'archive_healthy_responses', 0)
        self._operation_transport_failed = False
        self._operation_paused = False
        row = self._process(url, shared_index=True) if shared_index else self._process(url)
        if self._operation_paused and row.get('state') != 'verified':
            row['retry_same_url'] = True
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
            'matchType': 'exact', 'from': timestamp(self.since), 'to': timestamp(self.until), 'output': 'json',
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
        if scope in ('all', 'priority'):
            key += ':daily-v2'  # Old order, removed targets and weaker evidence stay history only.
        queues = self.state.setdefault('_queues', {})
        # Keep old checkpoints as evidence, without changing the daily scheduler.
        queue = queues.get(key)
        revisit_priority = scope == 'priority' and queue and (queue['cursor'] > 0 or queue.get('started') is not None)
        if not queue or queue.get('complete') or revisit_priority:
            retry_url = queue.get('retry_same_url') if queue else None
            verified = [r for r in queue['results'] if r.get('state') == 'verified'
                        and r.get('url') in urls
                        and re.fullmatch(timestamp(self.since)[:8] + r'[0-9]{6}',
                                         str(r.get('timestamp', '')))] if queue and scope in ('all', 'priority') else []
            if queue:
                history_key = key + ':pass:' + str(len(queues))
                while history_key in queues:
                    history_key += '0'
                queues[history_key] = queue
            covered = {r['url'] for r in verified}
            remaining = [u for u in dict.fromkeys(urls) if u not in covered]
            if retry_url in remaining:
                remaining.remove(retry_url)
                remaining.insert(0, retry_url)
            queue = {'urls': remaining, 'cursor': 0,
                     'results': verified, 'complete': False}
            queues[key] = queue
        # Retain carried same-day evidence even when its URL is no longer in
        # the remaining queue. Otherwise processing one new product drops it.
        report_urls = set(queue['urls']) | {r.get('url') for r in queue['results']
                                          if r.get('state') == 'verified'}
        self.results.extend(queue['results'])
        for row in queue['results']:
            print(json.dumps(row), flush=True)
        Path('capture-report.json').write_text(json.dumps(self.results, indent=2))
        # Save before dispatch. A crash after start cannot lead to a blind
        # replay of a possibly sent submission, even without a job ID.
        if queue.get('started') is not None and queue.get('retry_same_url') != queue['started']:
            url = queue['urls'][queue['cursor']]
            self.result(url, 'unverified', reason='interrupted URL operation; resume skips it, pending state preserved')
            queue['results'] = [r for r in self.results if r.get('url') in report_urls]
            queue['cursor'] += 1
            queue.pop('started', None)
        queue.pop('started', None)
        self.save()
        cycles = 0
        while queue['cursor'] < len(queue['urls']):
            if self.pause_circuit_open or time.time() + 120 >= self.deadline:
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
            row = None
            if scope == 'supplemental' and url == BASE + '/agents.md':
                self.result(url, 'unsupported-document-type', content_type='text/markdown',
                            reason='published text document is outside the HTML verifier; no submission made')
            else:
                self.active_queue = queue
                try:
                    row = self.process(url)
                finally:
                    self.active_queue = None
                if isinstance(row, dict) and row.get('queue_untouched') is True:
                    # No completed operation: retain this untouched cursor.
                    # Its transient report is not checkpointed as processed.
                    self.results.pop()
                    queue.pop('started', None)
                    self.save()
                    break
            if isinstance(row, dict) and row.get('retry_same_url'):
                queue.pop('started', None)
                queue['results'] = [r for r in self.results if r.get('url') in report_urls]
                self.save()
                delay = max(0, self.next_submit - time.time(), self.client.archive_next_request_at - time.time())
                if self.pause_circuit_open or time.time() + delay + 120 >= self.deadline:
                    break
                time.sleep(delay)
                continue  # Reconcile/retry this URL before advancing the queue.
            queue['cursor'] += 1
            queue.pop('started', None)
            queue.pop('retry_same_url', None)
            queue['results'] = [r for r in self.results if r.get('url') in report_urls]
            self.save()
        queue['complete'] = queue['cursor'] == len(queue['urls'])
        self.save()
        # These reporting rows do not advance the saved cursor/results.
        for url in queue['urls'][queue['cursor']:]:
            self.result(url, 'deferred', reason='three consecutive paused Archive responses; not captured' if self.pause_circuit_open else 'untouched queue checkpoint saved; recovery cycles or run budget exhausted')

    def run_daily_queue(self, urls, scope):
        """Resume bounded capture/status batches between shared index reads.

        The old queues remain history. Negative index results are never reused
        across invocations; accepted/uncertain jobs always follow _process's
        retained-state checks. Every stage uses the same Archive request gate.
        """
        if scope not in ('all', 'priority', 'policies'):
            raise ValueError('Daily batches require all, priority or policies scope')
        mode = 'verify' if self.verify_only else 'poll' if self.poll_only else 'capture'
        prefix = scope + ':' + timestamp(self.since)[:8] + ':' + mode
        key = prefix + ':daily-v3'
        queues = self.state.setdefault('_queues', {})
        queue = queues.get(key)
        # Daily-v2 evidence already passed the same strict verifier. Carry
        # positive evidence only; never import an old cursor or absent result.
        previous = queue or queues.get(prefix + ':daily-v2')
        targets = list(dict.fromkeys(urls))
        rebuild = not queue or queue['complete'] or scope in ('priority', 'policies')
        if rebuild:
            verified = [r for r in (previous or {}).get('results', [])
                        if r.get('state') == 'verified' and r.get('url') in targets
                        and re.fullmatch(timestamp(self.since)[:8] + r'[0-9]{6}', str(r.get('timestamp', '')))]
            if queue:
                history = key + ':pass:' + str(len(queues))
                while history in queues:
                    history += '0'
                queues[history] = queue
            covered = {r['url'] for r in verified}
            remaining = [u for u in targets if u not in covered]
            retry = (previous or {}).get('retry_same_url')
            if retry in remaining:
                remaining.remove(retry)
                remaining.insert(0, retry)
            queue = {'urls': remaining, 'cursor': 0, 'results': verified, 'complete': False,
                     'phase': 'index_before', 'batch_start': 0, 'batch_end': min(8, len(remaining))}
            queues[key] = queue
        else:
            # Newly discovered products join untouched work. Removed targets
            # remain in history but are not submitted by this invocation.
            known = set(queue['urls']) | {r.get('url') for r in queue['results']}
            queue['urls'].extend(u for u in targets if u not in known)
        report_urls = set(queue['urls']) | {r.get('url') for r in queue['results']}
        self.results.extend(queue['results'])
        for row in queue['results']:
            print(json.dumps(row), flush=True)
        if queue.get('started') is not None and queue.get('retry_same_url') != queue['started']:
            self.result(queue['started'], 'unverified', reason='interrupted URL operation; pending state preserved')
            queue['cursor'] += 1
            queue.pop('started', None)
        # No stale negative snapshot grants a new capture after a restart.
        if queue['phase'] == 'capture':
            queue['phase'] = 'index_before'

        def checkpoint():
            queue['results'] = [r for r in self.results if r.get('url') in report_urls]
            self.save()

        checkpoint()
        while not queue['complete']:
            if (getattr(self, 'daily_stopped', False) or getattr(self, 'outlink_hold', None)
                    or self.pause_circuit_open or self.transport_circuit_open
                    or self.state.get('_transport_cooldown_until', 0) > time.time()
                    or time.time() + 120 >= min(self.deadline, self.since + 86400)):
                break
            start, end = queue['batch_start'], queue['batch_end']
            covered = {r['url'] for r in queue['results'] if r.get('state') == 'verified'}
            if queue['phase'] in ('index_before', 'index_after'):
                before = queue['phase'] == 'index_before'
                selection = queue['urls'][queue['cursor'] if before else start:end]
                selection = [u for u in selection if u in targets and u not in covered]
                try:
                    for url, evidence in self.client.verify_many(selection, self.since, until=self.until):
                        if evidence:
                            self.verified_result(url, evidence, 'shared_index')
                        else:
                            self.result(url, 'unverified', reason='no usable capture in complete shared index snapshot')
                        checkpoint()
                except (LookupFailure, RequestDeferred, ValueError, OSError) as exc:
                    # Preserve phase/cursor and positive proof. No partial
                    # index is treated as permission for a new submission.
                    for url in selection:
                        if not any(r.get('url') == url and r.get('state') == 'verified' for r in queue['results']):
                            self.result(url, 'deferred', reason='shared index/replay unavailable; batch checkpoint retained',
                                        error_class=type(exc).__name__)
                    checkpoint()
                    break
                if before and not self.verify_only:
                    queue['phase'] = 'capture'
                    checkpoint()
                    continue
                # A verification-only stage never performs a second index
                # query or touches a submission/status endpoint.
                queue['cursor'] = end
                queue.pop('started', None)
                queue.pop('retry_same_url', None)
                queue['batch_start'] = end
                queue['batch_end'] = min(end + 8, len(queue['urls']))
                queue['phase'] = 'index_before'
                queue['complete'] = end == len(queue['urls'])
                checkpoint()
                continue
            if queue['cursor'] == end:
                queue['phase'] = 'index_after'
                checkpoint()
                continue
            url = queue['urls'][queue['cursor']]
            if url in covered or url not in targets:
                queue['cursor'] += 1
                queue.pop('started', None)
                queue.pop('retry_same_url', None)
                checkpoint()
                continue
            queue['started'] = url
            checkpoint()
            self.active_queue = queue
            try:
                row = self.process(url, shared_index=True)
            finally:
                self.active_queue = None
            queue.pop('started', None)
            if row.get('state') == 'outlink-held':
                self.outlink_hold = row
                checkpoint()
                break
            if row.get('queue_untouched'):
                checkpoint()
                break
            if row.get('retry_same_url'):
                queue['retry_same_url'] = url
                checkpoint()
                delay = max(0, self.next_submit - time.time(), self.client.archive_next_request_at - time.time())
                if self.pause_circuit_open or time.time() + delay + 120 >= min(self.deadline, self.since + 86400):
                    break
                time.sleep(delay)
                continue
            queue['cursor'] += 1
            queue.pop('retry_same_url', None)
            checkpoint()
        if not queue['complete']:
            self.daily_stopped = True
        for url in queue['urls'][queue['cursor']:]:
            if not any(r.get('url') == url and r.get('state') == 'verified' for r in queue['results']):
                self.result(url, 'deferred', reason='daily batch checkpoint retained; coverage incomplete')

    def outlink_submission_hold(self, url):
        journals = self.state.get('_outlink_batches', {})
        if not isinstance(journals, dict):
            return 'outlink journal needs review; no direct submission'
        for batch in journals.values():
            if outlink_attempt_before_today(batch):
                continue
            if not isinstance(batch, dict):
                return 'outlink journal needs review; no direct submission'
            if (batch.get('post_state') not in ('not_dispatched', 'not_dispatched_budget', 'not_sent_proven',
                    'dispatch_started', 'response_received', 'outcome_unknown')
                    or batch.get('expansion_state') not in ('unknown', 'mapping_reported')):
                return 'outlink dispatch/mapping state needs review; no direct submission'
            sent = batch.get('post_state') in ('dispatch_started', 'response_received', 'outcome_unknown')
            if sent and batch.get('expansion_state') != 'mapping_reported' and identity(url):
                return 'outlink expansion unknown; run outlink_probe --verify-only, then obtain reviewed resolution if still unknown'
            for child in batch.get('children', {}).values():
                if (not child.get('url') and child.get('hostname') in ('somervillewatchco.com', 'www.somervillewatchco.com')
                    and child.get('job_ids') and child.get('coverage') != 'verified' and identity(url)):
                    return 'unresolved redacted same-site outlink; read-only reconciliation and reviewed resolution required before direct submissions'
                matches = child.get('url') == url or child.get('url_sha256') == hashlib.sha256(url.encode()).hexdigest()
                if matches and child.get('job_ids') and child.get('coverage') != 'verified':
                    return 'unresolved outlink child job; run outlink_probe --verify-only, no automatic duplicate submission'
        return None

    def record_outlink_response(self, batch, code, headers, obj, known, phase):
        # Persist valid mappings BEFORE interpreting status or doing another IO.
        detail = response_metadata(code, headers, obj if isinstance(obj, dict) else {})
        detail.update(phase=phase, checked_at=timestamp(time.time()), schema_valid=isinstance(obj, dict))
        batch['responses'] = (batch.get('responses', []) + [detail])[-12:]
        # Mapping knowledge is independent of later status/verification errors.
        batch.setdefault('expansion_state', 'unknown')
        if isinstance(obj, dict):
            job = obj.get('job_id')
            if isinstance(job, str) and re.fullmatch(r'[A-Za-z0-9_-]+', job):
                ids = batch.setdefault('root_job_ids', [])
                if job not in ids:
                    ids.append(job)
                if not batch.get('root_job_id'):
                    batch['root_job_id'] = job
                detail['root_job_id_matches'] = job == batch['root_job_id']
            mapping = obj.get('outlinks')
            detail['outlinks_present'] = 'outlinks' in obj
            detail['mapping_valid_shape'] = isinstance(mapping, dict)
            if isinstance(mapping, dict):
                detail['reported_mapping_count'] = len(mapping)
                detail['mapping_truncated'] = len(mapping) > 100
                valid = 0
                for raw_url, value in list(mapping.items())[:100]:
                    child_job = value.get('job_id') if isinstance(value, dict) else value
                    if not isinstance(child_job, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', child_job):
                        continue
                    reference = outlink_child_reference(raw_url, known)
                    if reference is None:
                        continue
                    key = reference['url_sha256']
                    child = batch['children'].setdefault(key, {**reference, 'job_ids': [], 'coverage': 'unverified'})
                    if child_job not in child['job_ids']:
                        child['job_ids'].append(child_job)
                    child['direct_pending_reference'] = raw_url in self.state
                    if key not in batch['child_order']:
                        batch['child_order'].append(key)
                    valid += 1
                detail['valid_child_mappings'] = valid
                matching_job = 'job_id' not in obj or obj.get('job_id') == batch.get('root_job_id')
                if code == 200 and obj.get('status') == 'success' and matching_job and 0 < valid == len(mapping) <= 100:
                    batch['expansion_state'] = 'mapping_reported'  # Not capture coverage or completeness.
            batch['root_reported_status'] = obj.get('status') if obj.get('status') in ('success', 'pending', 'error') else 'unknown'
        self.save()
        if self.pause(headers, {**(obj if isinstance(obj, dict) else {}), '_http': code}):
            batch['server_paused_until'] = self.next_submit
            if (phase == 'root_status' and code == 200 and detail.get('status') == 'error'
                    and detail.get('root_job_id_matches') is True):
                batch['terminal_paused_status'] = {'checked_at': detail['checked_at'],
                    'resolution': 'strict_index_replay_only', 'pause_until_recorded': self.next_submit}
            self.save()
            return True
        return False

    def outlink_report(self, batch, inventory, reason):
        products = set(inventory['product_urls'])
        mapped = {child.get('url') for child in batch['children'].values() if child.get('url') in products}
        verified = {child.get('url') for child in batch['children'].values()
                    if child.get('url') in products and child.get('coverage') == 'verified'}
        root_verified = bool(batch.get('root_evidence'))
        complete = root_verified and len(verified) == len(products)
        return self.result(batch['root_url'], 'outlinks-verified' if complete else 'outlinks-partial',
            include_pending=False, reason=reason, root_verified=root_verified, expected_products=len(products),
            mapped_products=len(mapped), verified_products=len(verified),
            unreported_products=len(products - mapped), expansion_attribution='unproven', outlink_batch=batch)

    def run_outlink_probe(self, page=1):
        """One physical root POST total; subsequent invocations are read-only."""
        if isinstance(page, bool) or not isinstance(page, int) or not 1 <= page <= 6:
            return self.result(OUTLINK_PROBE, 'review_required', reason='outlink page must be an integer from 1 to 6; no request made')
        root = OUTLINK_PROBE if page == 1 else OUTLINK_PROBE + '?page=' + str(page)
        try:
            inventory, digest = load_outlink_inventory()
        except (LookupFailure, ValueError, OSError, TypeError):
            return self.result(root, 'review_required', reason='frozen outlink inventory invalid; no request made')
        if root not in inventory['root_urls']:
            return self.result(root, 'review_required', reason='selected root absent from frozen inventory; no request made')
        known = set(inventory['root_urls'] + inventory['product_urls'] + [BASE+p for p in PRIORITY])
        journals = self.state.setdefault('_outlink_batches', {})
        if not isinstance(journals, dict):
            return self.result(root, 'review_required', reason='outlink journal invalid; no request made')
        exists = root in journals
        batch = journals.get(root)
        if exists and (not isinstance(batch, dict) or batch.get('inventory_sha256') != digest
                or batch.get('root_url') != root
                or not isinstance(batch.get('children'), dict)
                or not isinstance(batch.get('child_order'), list)
                or any(key not in batch['children'] for key in batch['child_order'])
                or not isinstance(batch.get('verification_cursor'), int)
                or not 0 <= batch['verification_cursor'] <= len(batch['child_order'])
                or not isinstance(batch.get('utc_day'), (int, float))
                or not 0 < batch['utc_day'] <= time.time() + 86400):
            return self.result(root, 'review_required', reason='outlink journal/inventory mismatch; no request made')
        if not exists:
            if time.time() >= self.since + 86400:
                return self.result(root, 'deferred', reason='intended UTC day ended; no new outlink submission')
            if self.verify_only or self.poll_only:
                return self.result(root, 'outlinks-partial', reason='no saved outlink batch; read-only mode never submits')
            if not self.recover_transport(cycle=1):
                return self.result(root, 'deferred', reason='mandatory paced health gate failed; no outlink submission')
            try:
                before = self.client.verify(root, self.since, until=self.until)
            except (LookupFailure, ValueError, OSError):
                return self.result(root, 'deferred', reason='pre-submission archive evidence unavailable; no outlink submission')
            batch = {'root_url': root, 'inventory_sha256': digest,
                     'utc_day': self.since, 'created_at': time.time(), 'root_evidence_before': before,
                     'direct_pending_reference': root in self.state,
                     'request': {'capture_outlinks': '1', 'if_not_archived_within': '1,1d'},
                     'post_state': 'not_dispatched', 'expansion_state': 'unknown',
                     'children': {}, 'child_order': [], 'verification_cursor': 0, 'responses': []}
            journals[root] = batch
            self.save()  # Atomic journal BEFORE any potentially accepted root POST.
            old_callback = self.client.post_attempt_callback
            def record_attempt(attempt):
                batch['post_state'] = 'dispatch_started'
                batch['attempt'] = attempt
                self.save()
            self.client.post_attempt_callback = record_attempt
            try:
                code, headers, body, _ = self.client.paced_request('https://web.archive.org/save/',
                    {'url': root, 'capture_outlinks': '1', 'if_not_archived_within': '1,1d'}, True)
                try:
                    obj = json.loads(body)
                except (ValueError, TypeError):
                    obj = None
                batch['post_state'] = 'response_received'
                if self.record_outlink_response(batch, code, {k.lower():v for k,v in headers.items()}, obj, known, 'submission'):
                    return self.outlink_report(batch, inventory, 'server paused submission; IDs retained, no further requests')
            except (LookupFailure, ValueError, OSError) as exc:
                proof = getattr(exc, 'transport', None) or {}
                batch['post_state'] = ('not_dispatched_budget' if isinstance(exc, RequestDeferred) else
                    'not_sent_proven' if proof.get('known_not_sent') is True else 'outcome_unknown')
                batch['expansion_state'] = 'unknown'
                batch['submission_error_class'] = type(exc).__name__
                self.save()
                return self.outlink_report(batch, inventory, 'root submission unresolved; journal retained, no automatic retry')
            finally:
                self.client.post_attempt_callback = old_callback
        # Existing batch, including no-ID or proven-unsent outcomes, never POSTs.
        # Migrate already-recorded terminal paused observations (including older
        # sanitized records without the precise pause cause). Never reread that
        # terminal status merely to restart its local fallback cooldown.
        if not batch.get('terminal_paused_status'):
            responses = batch.get('responses', [])
            last = responses[-1] if isinstance(responses, list) and responses else None
            pause_until = batch.get('server_paused_until')
            if (isinstance(last, dict) and last.get('phase') == 'root_status'
                    and last.get('http_status') == 200 and last.get('status') == 'error'
                    and last.get('root_job_id_matches') is True
                    and isinstance(pause_until, (int, float)) and not isinstance(pause_until, bool)
                    and 0 < pause_until < float('inf')):
                batch['terminal_paused_status'] = {'checked_at': last.get('checked_at'),
                    'resolution': 'strict_index_replay_only', 'pause_until_recorded': pause_until}
                self.save()
        try:
            job = batch.get('root_job_id')
            if not batch.get('terminal_paused_status') and isinstance(job, str) and re.fullmatch(r'[A-Za-z0-9_-]+', job):
                code, headers, body, _ = self.client.paced_request('https://web.archive.org/save/status/'+job, authenticated=True)
                try:
                    obj = json.loads(body)
                except (ValueError, TypeError):
                    obj = None
                if self.record_outlink_response(batch, code, {k.lower():v for k,v in headers.items()}, obj, known, 'root_status'):
                    return self.outlink_report(batch, inventory, 'server paused status lookup; IDs retained, no further requests')
            start, end = batch['utc_day'], batch['utc_day'] + 86400 - 1
            evidence = self.client.verify(root, start, until=end)
            if evidence:
                batch['root_evidence'] = evidence
                batch['root_capture_after_attempt'] = bool(batch.get('attempt')) and epoch(evidence['timestamp']) >= batch['attempt']['started_at'] - 5
                self.save()
            order = batch['child_order']
            if batch['verification_cursor'] >= len(order):
                batch['verification_cursor'] = 0  # A later read-only pass can resolve indexing lag.
            while batch['verification_cursor'] < len(order):
                child = batch['children'][order[batch['verification_cursor']]]
                if child.get('url') in known:
                    evidence = self.client.verify(child['url'], start, until=end)
                    child['coverage'] = 'verified' if evidence else 'unverified'
                    if evidence:
                        child['evidence'] = evidence
                else:
                    child['coverage'] = 'unverifiable_redacted_url'
                batch['verification_cursor'] += 1
                self.save()
        except (LookupFailure, ValueError, OSError) as exc:
            batch['verification_interrupted'] = type(exc).__name__
            self.save()
            return self.outlink_report(batch, inventory, 'partial read-only verification; IDs and cursor retained')
        return self.outlink_report(batch, inventory, 'root and child evidence assessed separately; no completeness inferred from root success')

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

    def _process(self, url, *, shared_index=False):
        created_pending = False
        if time.time() + 35 >= self.deadline:
            return self.result(url, 'deferred', reason='run time budget', queue_untouched=True)
        try:
            evidence = None if shared_index else self.client.verify(url, self.since, until=self.until)
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
                if is_product(url) and self.product_attempted_today(url):
                    return self.result(url, 'deferred', reason='product already attempted this UTC day; no second submission')
                if time.time() >= self.since + 86400:
                    return self.result(url, 'deferred', reason='intended UTC day ended; no new submission')
                # An unknown outlink expansion forbids a new POST, but must not
                # prevent read-only reconciliation of an already saved job.
                hold = self.outlink_submission_hold(url)
                if hold:
                    return self.result(url, 'outlink-held', reason=hold)
                delay = max(0, self.next_submit - time.time())
                if time.time() + delay + 120 >= self.deadline:
                    return self.result(url, 'deferred', reason='host cooldown/time budget', queue_untouched=True)
                if delay:
                    print('Waiting %ss before next submission' % int(delay), flush=True)
                    time.sleep(delay)
                if time.time() >= self.since + 86400:
                    return self.result(url, 'deferred', reason='intended UTC day ended while waiting; no new submission')
                pending = {'submitted_at': time.time(), 'job_id': None,
                    'request_evidence': {'method': 'POST', 'endpoint': 'https://web.archive.org/save/',
                                         'if_not_archived_within': '1'}}
                # Persist BEFORE POST: a network timeout has an uncertain outcome.
                self.state[url] = pending
                created_pending = True
                self.state['_next_submit'] = self.next_submit = time.time() + self.interval
                self.save()
                self.client.post_retry_not_before = self.next_submit
                self.client.post_retry_deadline = 0 if is_product(url) else self.deadline
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
                        return self.result(url, 'deferred', reason='submission paused but job accepted; retained', retry_same_url=True)
                    if obj.get('status') == 'error' or status == 429:
                        return self.terminal_result(url, 'deferred', reason='host crawling paused', response=pending['submission_response'], retry_same_url=True)
                    return self.result(url, 'pending', reason='paused response has uncertain acceptance; no duplicate submission', retry_same_url=True)
                if not isinstance(job_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', job_id):
                    if obj.get('status') == 'error' or status in (400, 401, 403):
                        return self.terminal_failure(url, reason='submission explicitly rejected', http_status=status,
                                           status_ext=safe_status_ext(obj.get('status_ext')), response=pending['submission_response'])
                    return self.result(url, 'pending', reason='submission outcome unknown; no automatic duplicate')
                pending['job_id'] = job_id
                self.save()
            if not pending.get('job_id'):
                return self.result(url, 'pending', reason='earlier submission outcome unknown; needs review')
            due = max(pending.get('next_status_at', 0), pending.get('status_paused_until', 0),
                      pending.get('server_retry_after_until', 0))
            if time.time() < due:
                return self.result(url, 'pending', reason='saved status observation not yet due; job retained')
            # One observation per URL/pass. Persist reservation BEFORE I/O so
            # restart/interruption cannot reset the polling cadence.
            for _ in range(1):
                if time.time() + 40 >= self.deadline:
                    break
                pending['next_status_at'] = max(time.time(), due,
                    self.next_submit, self.state.get('_archive_next_request_at', 0),
                    self.state.get('_transport_cooldown_until', 0)) + 900
                self.save()
                status, headers, obj = self.client.json('https://web.archive.org/save/status/' + pending['job_id'], authenticated=True)
                if not isinstance(obj, dict):
                    raise LookupFailure('Invalid status response')
                obj['_http'] = status
                pending['status_response'] = response_metadata(status, headers, obj)
                if status == 200 and obj.get('status') == 'error' and pending['status_response']['pause_triggers']:
                    pending['paused_job_errors'] = min(3, pending.get('paused_job_errors', 0) + 1)
                    if pending['paused_job_errors'] == 3:
                        # Repeated job-error payloads are not new submissions or
                        # proof of non-acceptance. Quarantine this job for review
                        # instead of polling it forever on every later run.
                        # The current response still invokes the same host pause
                        # and breaker below; the job/history are never removed.
                        pending['status_error'] = True
                        pending['review_required'] = True
                else:
                    pending['paused_job_errors'] = 0
                if pending['status_response'].get('retry_after_until'):
                    pending['server_retry_after_until'] = max(pending.get('server_retry_after_until', 0),
                        pending['status_response']['retry_after_until'])
                pending['next_status_at'] = max(pending['next_status_at'], time.time() + 900,
                    pending.get('server_retry_after_until', 0),
                    self.state.get('_archive_next_request_at', 0))
                self.save()
                if self.pause(headers, obj):
                    # Throttling the status lookup does not prove the accepted
                    # capture was refused. Keep its job to prevent a duplicate.
                    pending['status_paused_until'] = self.next_submit
                    self.save()
                    return self.result(url, 'deferred', reason='status lookup paused; existing job retained', response=response_metadata(status, headers, obj), retry_same_url=True)
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
                    resolved = self.classify_success(url, pending, check_daily=not shared_index)
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
                    evidence = None if shared_index else self.client.verify(url, self.since, until=self.until)
                    if evidence:
                        return self.verified_result(url, evidence, 'status_error_reconciled_index')
                    return self.result(url, 'review_required', reason='job reported error; no usable indexed capture yet, no automatic resubmission')
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

def daily_summary(urls, results, since, inventory_complete=True, priority_paths=None):
    day = timestamp(since)[:8]
    verified = {row['url'] for row in results if row.get('state') == 'verified'
                and isinstance(row.get('timestamp'), str)
                and re.fullmatch(day + r'[0-9]{6}', row['timestamp'])}
    missing = [path for path in (PRIORITY if priority_paths is None else priority_paths) if BASE + path not in verified]
    products = {url for url in urls if is_product(url)}
    product_count = ('not assessed (priority-only scope)' if inventory_complete is None else
                     str(len(products - verified)) if inventory_complete else 'unknown (catalog discovery incomplete)')
    names = ['Home (/)' if path == '/' else path for path in missing]
    return ('Tier 1 pages with no verified capture: ' + (', '.join(names) if names else 'none')
            + '\nProducts with no verified capture: ' + product_count + '\n'), missing


def main():
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--verify-only', action='store_true')
    mode.add_argument('--poll-only', action='store_true', help='Read existing job status and archive evidence; never submit')
    parser.add_argument('--scope', choices=('all', 'priority', 'policies', 'pilot', 'supplemental', 'transport_probe', 'pacing_probe', 'pacing_job_diagnostic', 'account_diagnostic', 'supplemental_probe', 'outlink_probe'), default='all')
    parser.add_argument('--outlink-page', type=int, choices=range(1, 7), default=1, help='One frozen Shop All page; only used with outlink_probe')
    parser.add_argument('--state', default='.capture-state/pending.json')
    mode.add_argument('--inspect-state', action='store_true', help='Emit sanitized saved-state summary only; no network or state mutation')
    parser.add_argument('--budget', type=int, default=17400)
    parser.add_argument('--daily-batches', action='store_true',
                        help='Use resumable shared-index batches for daily all/priority/policies scope')
    args = parser.parse_args()
    if args.daily_batches and args.scope not in ('all', 'priority', 'policies'):
        parser.error('--daily-batches requires all, priority or policies scope')
    if args.inspect_state:
        path = Path(args.state)
        try:
            state = load_legacy_state(path)
        except StateContinuityError as exc:
            print(json.dumps({'state': 'state-held', 'reason': str(exc), 'network_requests': 0}), flush=True)
            return 2
        summary = operational_summary(state)
        output = Path('operational-report.json')
        if (output.resolve() != path.resolve()
                and not (output.exists() and path.exists() and output.samefile(path))):
            output.write_text(json.dumps(summary, indent=2))
        print(json.dumps(summary), flush=True)
        return 0
    client = Client()
    budget = min(args.budget, 1800) if args.scope in ('pacing_probe', 'pacing_job_diagnostic', 'account_diagnostic', 'supplemental_probe', 'outlink_probe') else args.budget
    try:
        runner = Runner(client, args.state, budget, verify_only=args.verify_only, poll_only=args.poll_only)
    except StateContinuityError as exc:
        print(json.dumps({'state': 'state-held', 'reason': str(exc), 'network_requests': 0}), flush=True)
        return 2
    if args.scope == 'outlink_probe':
        row = runner.run_outlink_probe(args.outlink_page)
        print('Outlink expansion and child coverage are separate; partial is not complete.')
        return 0 if row['state'] == 'outlinks-verified' else 1
    if args.scope == 'account_diagnostic':
        row = runner.diagnose_account()
        print('Read-only possibly-cached account-capacity observation; not authentication proof or capture coverage.')
        return 0 if row['state'] == 'diagnostic-observed' else 1
    if args.scope == 'pacing_job_diagnostic':
        row = runner.diagnose_saved_pacing_job()
        print('Read-only saved-job observation only; not capture coverage.')
        return 0 if row['state'] == 'diagnostic-observed' else 1
    urls = [] if args.scope == 'supplemental' else [BASE + p for p in (POLICIES if args.scope == 'policies' else ['/pages/warranty-policy'] if args.scope == 'pilot' else PRIORITY)]
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
    inventory_complete = True
    if args.scope == 'all':
        try:
            urls.extend(discover(client))
        except LookupFailure as exc:
            inventory_complete = False
            runner.result(BASE + '/collections/all', 'discovery-incomplete', reason=str(exc))
    if args.scope == 'supplemental_probe':
        runner.run_supplemental_probe()
    elif args.scope == 'all':
        # Revisit unresolved tier 1 on every scheduled pass before resuming the
        # product checkpoint; earlier strict same-day evidence remains valid.
        run_daily = runner.run_daily_queue if args.daily_batches else runner.run_queue
        run_daily([BASE + p for p in PRIORITY], 'priority')
        run_daily([url for url in urls if is_product(url)], 'all')
    elif args.scope in ('priority', 'policies') and args.daily_batches:
        runner.run_daily_queue(urls, args.scope)
    else:
        runner.run_queue(urls, args.scope)
    counts = {state: sum(r['state'] == state for r in runner.results) for state in sorted({r['state'] for r in runner.results})}
    summary = 'UTC date: %s\n\nResults: %s\n\nFull per-URL evidence is in capture-report.json.\n' % (timestamp(runner.since)[:8], json.dumps(counts))
    missing_priority = []
    if args.scope in ('all', 'priority', 'policies'):
        coverage, missing_priority = daily_summary(urls, runner.results, runner.since,
            inventory_complete if args.scope == 'all' else None,
            priority_paths=POLICIES if args.scope == 'policies' else None)
        if args.scope == 'policies':
            coverage = coverage.replace('not assessed (priority-only scope)', 'paused (policy/cart-only scope)')
            summary += 'Scope: cart and 8 policy/legal pages; product reads/captures paused.\n'
        summary += coverage
    print(summary)
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        Path(os.environ['GITHUB_STEP_SUMMARY']).write_text(summary)
    if args.scope in ('all', 'priority', 'policies'):
        missing_products = False
        if args.daily_batches and args.scope == 'all':
            verified = {row['url'] for row in runner.results if row.get('state') == 'verified'
                        and re.fullmatch(timestamp(runner.since)[:8] + r'[0-9]{6}', str(row.get('timestamp', '')))}
            missing_products = any(is_product(url) and url not in verified for url in urls)
        return 1 if missing_priority or missing_products or not inventory_complete else 0
    return 0 if runner.results and all(r['state'] in ('verified', 'no-additional-targets') for r in runner.results) else 1

if __name__ == '__main__':
    raise SystemExit(main())

