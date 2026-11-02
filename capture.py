#!/usr/bin/env python3
"""Conservative daily SPN capture and independent CDX/replay verification."""
import argparse
import datetime as dt
import gzip
import html
import json
import os
from pathlib import Path
import re
import time
import urllib.error
import urllib.parse as up
import urllib.request as ur

BASE = 'https://somervillewatchco.com'
PRIORITY = ['/', '/policies/refund-policy', '/policies/shipping-policy',
 '/policies/terms-of-service', '/policies/privacy-policy',
 '/policies/contact-information', '/policies/legal-notice', '/pages/contact',
 '/pages/warranty-policy', '/pages/no-brand-affiliation',
 '/policies/subscription-policy', '/cart', '/cart/51183558787357:1?storefront=true']
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

class LookupFailure(Exception):
    pass

class NoRedirect(ur.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward Archive authorization to a redirect destination.

class Client:
    def request(self, url, data=None, authenticated=False):
        # Read endpoints can fail transiently. Never retry a POST whose outcome
        # might already have created a job. Pace public archive reads as well.
        for attempt in range(2 if data is None else 1):
            if data is None and up.urlsplit(url).hostname == 'web.archive.org':
                wait = max(0, getattr(self, '_last_read', 0) + 2 - time.time())
                if wait:
                    time.sleep(wait)
                self._last_read = time.time()
            try:
                response = self._request(url, data, authenticated)
                if data is None and not authenticated and response[0] in (429, 502, 503, 504) and attempt == 0:
                    time.sleep(30)
                    continue
                return response
            except LookupFailure:
                if data is not None or attempt == 1:
                    raise
                time.sleep(10)

    def _request(self, url, data=None, authenticated=False):
        headers = {'User-Agent': 'SWC-Archive-Verification/1.0', 'Accept-Encoding': 'identity'}
        if authenticated:
            access, secret = os.environ.get('ARCHIVE_ORG_ACCESS_KEY'), os.environ.get('ARCHIVE_ORG_SECRET_KEY')
            if not access or not secret:
                raise LookupFailure('Archive credentials are not configured')
            # Only the SPN endpoint receives this header; never log it.
            headers['Authorization'] = 'LOW ' + access + ':' + secret
            headers['Accept'] = 'application/json'
        req = ur.Request(url, data=up.urlencode(data).encode() if data else None, headers=headers)
        try:
            response = (ur.build_opener(NoRedirect()).open(req, timeout=30)
                        if authenticated else ur.urlopen(req, timeout=30))
        except urllib.error.HTTPError as exc:
            response = exc
        except (OSError, TimeoutError) as exc:
            reason = getattr(exc, 'reason', exc)
            raise LookupFailure('Network read failed: ' + type(reason).__name__) from None
        with response:
            body = response.read(8 * 1024 * 1024 + 1)
            if len(body) > 8 * 1024 * 1024:
                raise LookupFailure('Response exceeds verification size limit')
            if body[:2] == b'\x1f\x8b':
                body = gzip.decompress(body)
            return response.status, dict(response.headers.items()), body.decode('utf-8', errors='replace'), response.url

    def json(self, url, data=None, authenticated=False):
        status, headers, body, _ = self.request(url, data, authenticated)
        try:
            obj = json.loads(body)
        except ValueError:
            if status == 429:
                return status, {k.lower(): v for k, v in headers.items()}, {'status': 'error', 'status_ext': 'http-429'}
            raise LookupFailure('Non-JSON response (HTTP %s)' % status) from None
        return status, {k.lower(): v for k, v in headers.items()}, obj

    def verify(self, url, since):
        query = up.urlencode({'url': url, 'matchType': 'exact', 'from': timestamp(since),
            'to': timestamp(time.time() + 60), 'output': 'json',
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
                if not since <= epoch(row['timestamp']) <= time.time() + 60:
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
        # Shopify may canonicalize pagination to /collections/all; discovery
        # still checks the requested page response, not archive evidence.
        if status != 200 or identity(final) != identity(url) or not usable_html(body, BASE + '/collections/all'):
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

    def save(self):
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix('.tmp')
        tmp.write_text(json.dumps(self.state, indent=2))
        tmp.replace(self.state_path)

    def result(self, url, state, **extra):
        row = {'url': url, 'state': state, **extra}
        # Retain only allowlisted diagnostic fields, never raw authenticated
        # responses/headers or arbitrary returned URL/query strings.
        pending = self.state.get(url, {})
        job_id = pending.get('job_id')
        if isinstance(job_id, str) and re.fullmatch(r'[A-Za-z0-9_-]+', job_id):
            row['job_id'] = job_id
        if isinstance(pending.get('submitted_at'), (int, float)):
            row['submitted_at'] = pending['submitted_at']
        if pending.get('status_evidence'):
            row['status_evidence'] = pending['status_evidence']
        self.results.append(row)
        print(json.dumps(row), flush=True)
        Path('capture-report.json').write_text(json.dumps(self.results, indent=2))
        return row

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
        self.save()
        return True

    def process(self, url):
        if time.time() + 35 >= self.deadline:
            return self.result(url, 'deferred', reason='run time budget')
        try:
            evidence = self.client.verify(url, self.since)
            if evidence:
                self.state.pop(url, None)
                self.save()
                return self.result(url, 'verified', **evidence)
            if self.verify_only:
                return self.result(url, 'unverified', reason='no usable indexed capture for UTC day')
            pending = self.state.get(url)
            if not pending and self.poll_only:
                return self.result(url, 'unverified', reason='no saved job to poll; no submission made')
            if not pending:
                delay = max(0, self.next_submit - time.time())
                if time.time() + delay + 120 >= self.deadline:
                    return self.result(url, 'deferred', reason='host cooldown/time budget')
                if delay:
                    print('Waiting %ss before next submission' % int(delay), flush=True)
                    time.sleep(delay)
                pending = {'submitted_at': time.time(), 'job_id': None}
                # Persist BEFORE POST: a network timeout has an uncertain outcome.
                self.state[url] = pending
                self.state['_next_submit'] = self.next_submit = time.time() + self.interval
                self.save()
                status, headers, obj = self.client.json('https://web.archive.org/save/',
                    {'url': url, 'if_not_archived_within': '1'}, True)
                if not isinstance(obj, dict):
                    raise LookupFailure('Invalid submission response')
                obj['_http'] = status
                if self.pause(headers, obj):
                    self.state.pop(url, None)  # Explicit refusal, safe to try after cooldown.
                    self.save()
                    return self.result(url, 'deferred', reason='host crawling paused')
                job_id = obj.get('job_id')
                if not isinstance(job_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', job_id):
                    if obj.get('status') == 'error' or status in (400, 401, 403):
                        self.state.pop(url, None)
                        self.save()
                        return self.result(url, 'failed', reason='submission explicitly rejected', http_status=status,
                                           status_ext=obj.get('status_ext', 'unknown'))
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
                if self.pause(headers, obj):
                    self.state.pop(url, None)
                    self.save()
                    return self.result(url, 'deferred', reason='host crawling paused')
                if status != 200:
                    raise LookupFailure('Status endpoint HTTP %s' % status)
                if obj.get('status') == 'success':
                    try:
                        fresh = pending['submitted_at'] - 5 <= epoch(obj.get('timestamp', '')) <= time.time() + 60
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
                    if not fresh or not original_matches:
                        # A claimed success can still represent a real job.
                        # Preserve its ID so a later run rechecks, never reposts.
                        return self.result(url, 'pending', reason='job success has stale/missing timestamp or different URL; retained for review')
                    evidence = self.client.verify(url, max(self.since, pending['submitted_at'] - 5))
                    if evidence:
                        self.state.pop(url, None)
                        self.save()
                        return self.result(url, 'verified', **evidence)
                    # Indexing can lag. Keep job ID for later runs; do not resubmit.
                elif obj.get('status') == 'error':
                    self.state.pop(url, None)
                    self.save()
                    return self.result(url, 'failed', reason='SPN error', status_ext=obj.get('status_ext', 'unknown'))
                time.sleep(10)
            return self.result(url, 'pending', reason='job or independent index verification pending')
        except (LookupFailure, ValueError, OSError) as exc:
            return self.result(url, 'unverified', reason=str(exc))

def main():
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--verify-only', action='store_true')
    mode.add_argument('--poll-only', action='store_true', help='Read existing job status and archive evidence; never submit')
    parser.add_argument('--scope', choices=('all', 'priority', 'pilot'), default='all')
    parser.add_argument('--state', default='.capture-state/pending.json')
    parser.add_argument('--budget', type=int, default=17400)
    args = parser.parse_args()
    client = Client()
    runner = Runner(client, args.state, args.budget, verify_only=args.verify_only, poll_only=args.poll_only)
    urls = [BASE + p for p in (['/pages/warranty-policy'] if args.scope == 'pilot' else PRIORITY)]
    for url in urls:
        # A dispatch gets one submission opportunity per URL. A host refusal
        # preserves cooldown but never triggers another POST in this run.
        runner.process(url)
    if args.scope == 'all':
        try:
            for url in discover(client):
                runner.process(url)
        except LookupFailure as exc:
            runner.result(BASE + '/collections/all', 'discovery-incomplete', reason=str(exc))
    counts = {state: sum(r['state'] == state for r in runner.results) for state in sorted({r['state'] for r in runner.results})}
    summary = 'UTC date: %s\n\nResults: %s\n\nFull per-URL evidence is in capture-report.json.\n' % (timestamp(runner.since)[:8], json.dumps(counts))
    print(summary)
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        Path(os.environ['GITHUB_STEP_SUMMARY']).write_text(summary)
    return 0 if runner.results and all(r['state'] == 'verified' for r in runner.results) else 1

if __name__ == '__main__':
    raise SystemExit(main())
