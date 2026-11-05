import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
from test_support import seed_test_state
from capture import Client, Runner, LookupFailure, BASE, identity, timestamp, usable_html, discover

URL = BASE + '/'
GOOD = '<html><title>Somerville Watch Co.</title><body>' + ('Watch collection ' * 60) + '</body></html>'
FIELDS = ['timestamp', 'original', 'statuscode', 'mimetype', 'digest']

class VerificationTests(unittest.TestCase):
    @patch('capture.time.sleep')
    def test_transient_get_retried(self, sleep):
        c = Client(); c._request = Mock(side_effect=[LookupFailure('timeout'), (200, {}, GOOD, URL)])
        self.assertEqual(c.request(URL)[0], 200)
        self.assertEqual(c._request.call_count, 2)

    @patch('capture.time.sleep')
    def test_post_timeout_not_retried(self, sleep):
        c = Client(); c._request = Mock(side_effect=LookupFailure('timeout'))
        with self.assertRaises(LookupFailure): c.request('https://web.archive.org/save/', {'url':URL}, True)
        self.assertEqual(c._request.call_count, 1)

    @patch('capture.time.sleep')
    def test_public_read_429_gets_bounded_backoff(self, sleep):
        c = Client(); c._request = Mock(side_effect=[(429, {}, '', URL), (200, {}, GOOD, URL)])
        self.assertEqual(c.request(URL)[0], 200)
        sleep.assert_any_call(30)

    def client(self, code='200', mime='text/html', body=GOOD, final=None):
        c = Client()
        ts = timestamp(time.time() - 10)
        replay = 'https://web.archive.org/web/' + ts + 'id_/' + URL
        c.json = Mock(return_value=(200, {}, [FIELDS, [ts, URL, code, mime, 'digest']]))
        c.request = Mock(return_value=(200, {'Content-Type': 'text/html'}, body, final or replay))
        return c

    def test_challenge_heading_with_store_title_rejected(self):
        body = GOOD.replace('<body>', '<body><h1>Verify <span>you are</span> human</h1>')
        self.assertFalse(usable_html(body, URL))
        self.assertIsNone(self.client(body=body).verify(URL, time.time()-60))

    def test_embedded_challenge_text_is_not_a_document_challenge(self):
        body = GOOD.replace('<body>', '<body><script>"<h1>Verify you are human</h1>"</script>'
            '<!-- <h1>Robot check</h1> --><div class="h-captcha"></div>')
        self.assertTrue(usable_html(body, URL))

    def test_exact_challenge_prompt_variants_rejected(self):
        prompts = ('Verify you are a human', 'Verify that you are a human',
                   'Please verify you are human to continue', 'Verify you are not a robot',
                   'Security verification', 'Checking your browser', 'Bot verification')
        for prompt in prompts:
            with self.subTest(prompt=prompt):
                self.assertFalse(usable_html(GOOD.replace('<body>', '<body><h2>'+prompt+'</h2>'), URL))

    def test_nested_entity_encoded_challenge_heading_rejected(self):
        body = GOOD.replace('<body>', '<body><h2>Ver<span>ify</span>&nbsp;that you are '
            '<strong>a</strong> human&#33;</h2>')
        self.assertFalse(usable_html(body, URL))

    def test_inert_markup_challenge_headings_ignored(self):
        fixtures = ('<template><h1>Bot verification</h1></template>',
                    '<template><template><h1>Verify you are human</h1></template></template>',
                    '<script>"<h1>Checking your browser</h1>"</script>',
                    '<style>/* <h1>Security verification</h1> */</style>',
                    '<!-- <h1>Verify you are a human</h1> -->')
        for fixture in fixtures:
            with self.subTest(fixture=fixture):
                self.assertTrue(usable_html(GOOD.replace('<body>', '<body>'+fixture), URL))

    def test_real_heading_after_inert_markup_rejected(self):
        body = GOOD.replace('<body>', '<body><template><h1>Watch guide</h1></template>'
            '<h1>Bot verification</h1>')
        self.assertFalse(usable_html(body, URL))

    def test_normal_craftsmanship_and_security_prose_accepted(self):
        body = GOOD.replace('<body>', '<body><h1>Human craftsmanship</h1>'
            '<h2>How we verify you are human</h2><p>Our security policy uses bot verification '
            'to protect forms. Each watch is assembled by a human.</p>')
        self.assertTrue(usable_html(body, URL))

    def test_ordinary_captcha_widget_accepted(self):
        body = GOOD.replace('<body>', '<body><form><div class="h-captcha"></div>'
            '<script src="https://js.hcaptcha.com/1/api.js"></script></form>')
        self.assertTrue(usable_html(body, URL))

    def test_challenge_form_without_instruction_accepted(self):
        body = GOOD.replace('<body>', '<body><form action="/challenge/verify">'
            '<input name="contact"></form>')
        self.assertTrue(usable_html(body, URL))

    def test_real_html_verified(self):
        self.assertEqual(self.client().verify(URL, time.time()-60)['statuscode'], '200')

    def test_fresh_204_rejected(self):
        c = self.client('204', 'application/http')
        self.assertIsNone(c.verify(URL, time.time()-60))
        c.request.assert_not_called()

    def test_exact_timestamp_query_rejects_newer_capture(self):
        c = self.client()
        target = time.time() - 60
        self.assertIsNone(c.verify(URL, target, until=target))
        c.request.assert_not_called()
        self.assertIn('to=' + timestamp(target), c.json.call_args.args[0])

    def test_stale_capture_rejected(self):
        self.assertIsNone(self.client().verify(URL, time.time()))

    def test_wrong_replay_timestamp_rejected(self):
        self.assertIsNone(self.client(final='https://web.archive.org/web/20261001000000id_/' + URL).verify(URL, time.time()-60))

    def test_wrong_original_rejected(self):
        c = self.client()
        c.json.return_value[2][1][1] = BASE + '/other'
        self.assertIsNone(c.verify(URL, time.time()-60))

    def test_cdx_error_is_unknown_not_absence(self):
        c = self.client()
        c.json.return_value = (503, {}, [])
        with self.assertRaises(LookupFailure): c.verify(URL, time.time()-60)

    def test_empty_index_is_absence(self):
        c = self.client(); c.json.return_value = (200, {}, [])
        self.assertIsNone(c.verify(URL, time.time()-60))

    def test_revisit_requires_usable_replay(self):
        self.assertIsNotNone(self.client(mime='warc/revisit').verify(URL, time.time()-60))
        self.assertIsNone(self.client(mime='warc/revisit', body='').verify(URL, time.time()-60))

    def test_error_and_canonical_pages_rejected(self):
        self.assertFalse(usable_html(GOOD.replace('Somerville Watch Co.', 'Access Denied'), URL))
        self.assertFalse(usable_html(GOOD + '<link rel="canonical" href="https://somervillewatchco.com/other">', URL))
        self.assertTrue(usable_html(GOOD + '<link rel="canonical" href="http://www.somervillewatchco.com/">', URL))

    def test_identity_retains_query(self):
        self.assertNotEqual(identity(BASE+'/collections/all?page=2'), identity(BASE+'/collections/all'))
        self.assertIsNone(identity('https://evil.example/'))

class SharedIndexVerificationTests(unittest.TestCase):
    """Read-only proposal component, with real parsing/pacing and fake IO."""
    def setUp(self):
        from capture import epoch
        self.now = epoch('20261104120000')
        self.since = epoch('20261104000000')
        self.ts = timestamp(self.now - 60)
        self.calls = []
        self.rows = []
        self.status = 200
        self.body_override = None
        self.redirect = False
        self.clock = patch('capture.time.time', side_effect=lambda: self.now)
        self.clock.start(); self.addCleanup(self.clock.stop)
        self.sleep = patch('capture.time.sleep', side_effect=self.advance)
        self.sleep.start(); self.addCleanup(self.sleep.stop)
        self.client = Client()
        self.client.archive_interval = 120
        self.client._request = Mock(side_effect=self.request)

    def advance(self, seconds):
        self.assertGreaterEqual(seconds, 0)
        self.now += seconds

    def row(self, url=URL, ts=None):
        return [ts or self.ts, url, '200', 'text/html', 'digest']

    def request(self, url, data=None, authenticated=False):
        self.calls.append((url, data, authenticated, self.now))
        self.assertIsNone(data)
        self.assertFalse(authenticated)
        if '/cdx/' in url:
            return self.status, {}, json.dumps(self.rows), url
        original = url.split('id_/', 1)[1]
        body = GOOD
        if '/products/' in original:
            name = original.rsplit('/', 1)[-1].replace('-', ' ').title()
            node = {'@type': 'Product', 'url': original, '@id': original + '#product',
                    'name': name, 'offers': {'price': '339.99', 'priceCurrency': 'USD'}}
            body = ('<html><title>' + name + ' – Somerville Watch Co.</title>'
                    '<script type="application/ld+json">' + json.dumps(node) + '</script>'
                    '<body><h1>' + name + '</h1>' + 'Watch details ' * 80 + '</body></html>')
        return (200, {'content-type': 'text/html'},
                self.body_override if self.body_override is not None else body,
                url.replace(self.ts, '20261103000000') if self.redirect else url)

    def test_all_166_targets_use_one_index_and_166_independent_replays(self):
        from capture import PRIORITY
        from urllib.parse import parse_qs, urlsplit
        targets = [BASE + p for p in PRIORITY] + [BASE + '/products/watch-' + str(i) for i in range(154)]
        self.rows = [FIELDS] + [self.row(url) for url in targets]
        results = dict(self.client.verify_many(targets, self.since))
        self.assertEqual(set(results), set(targets))
        self.assertTrue(all(evidence and evidence['original'] == url for url, evidence in results.items()))
        self.assertEqual(len(self.calls), 167)
        queries = [url for url, _, _, _ in self.calls if '/cdx/' in url]
        self.assertEqual(len(queries), 1)
        query = parse_qs(urlsplit(queries[0]).query)
        self.assertEqual(query['matchType'], ['host'])
        self.assertEqual(query['showResumeKey'], ['true'])
        self.assertEqual(query['limit'], ['5000'])
        self.assertEqual(query['from'], ['20261104000000'])
        self.assertEqual(query['to'], ['20261104120100'])
        self.assertEqual(self.calls[-1][3] - self.calls[0][3], 166 * 120)

    def test_duplicate_targets_reuse_index_without_duplicate_replays(self):
        self.rows = [FIELDS, self.row()]
        self.assertEqual(len(list(self.client.verify_many([URL, URL], self.since))), 1)
        self.assertEqual(len(self.calls), 2)

    def test_complete_empty_snapshot_returns_no_evidence_without_replay(self):
        self.assertEqual(list(self.client.verify_many([URL], self.since)), [(URL, None)])
        self.assertEqual(len(self.calls), 1)

    def test_resumption_marker_rejects_whole_batch_before_any_replay(self):
        self.rows = [FIELDS, self.row(), [], ['continuation-key']]
        with self.assertRaises(LookupFailure):
            next(self.client.verify_many([URL], self.since))
        self.assertEqual(len(self.calls), 1)

    def test_row_limit_rejects_response_even_without_continuation_marker(self):
        self.rows = [FIELDS] + [self.row()] * 5000
        with self.assertRaises(LookupFailure):
            next(self.client.verify_many([URL], self.since))
        self.assertEqual(len(self.calls), 1)

    def test_malformed_unrequested_row_invalidates_whole_snapshot(self):
        self.rows = [FIELDS, self.row(), ['20261104115900', BASE + '/unknown']]
        with self.assertRaises(LookupFailure):
            next(self.client.verify_many([URL], self.since))
        self.assertEqual(len(self.calls), 1)

    def test_wrong_schema_and_http_failure_are_never_absence(self):
        for status, rows in ((200, [['original'], [URL]]), (503, [])):
            with self.subTest(status=status):
                self.status, self.rows = status, rows
                with self.assertRaises(LookupFailure):
                    next(self.client.verify_many([URL], self.since))
        self.assertTrue(all('/cdx/' in call[0] for call in self.calls))

    def test_exact_original_and_day_bounds_reject_aliases_and_wrong_dates(self):
        self.rows = [FIELDS, self.row(BASE), self.row('http://somervillewatchco.com/'),
                     self.row('https://www.somervillewatchco.com/'),
                     self.row(ts='20261103235959'), self.row(ts='20261104140000')]
        self.assertEqual(list(self.client.verify_many([URL], self.since)), [(URL, None)])
        self.assertEqual(len(self.calls), 1)

    def test_unrequested_index_urls_are_never_replayed_or_returned(self):
        self.rows = [FIELDS, self.row(), self.row(BASE + '/unknown?private=value')]
        result = list(self.client.verify_many([URL], self.since))
        self.assertEqual(len(result), 1)
        self.assertIsNotNone(result[0][1])
        self.assertNotIn('private=value', json.dumps(result))
        self.assertEqual(len(self.calls), 2)

    def test_each_replay_still_rejects_redirects_and_challenge_pages(self):
        self.rows = [FIELDS, self.row()]
        self.redirect = True
        self.assertEqual(list(self.client.verify_many([URL], self.since)), [(URL, None)])
        self.redirect = False
        self.body_override = GOOD.replace('<body>', '<body><h1>Verify you are human</h1>')
        self.assertEqual(list(self.client.verify_many([URL], self.since)), [(URL, None)])

    def test_product_without_valid_identity_and_price_is_not_coverage(self):
        product = BASE + '/products/example-watch'
        self.rows = [FIELDS, self.row(product)]
        self.body_override = GOOD
        self.assertEqual(list(self.client.verify_many([product], self.since)), [(product, None)])

    def test_invalid_targets_and_cross_day_windows_do_no_network_work(self):
        for targets, since, until in ((['https://evil.example/'], self.since, None),
                                     ([URL + '?private=x'], self.since, None),
                                     ([URL], self.since + 1, None),
                                     ([URL], self.since, self.since + 86400),
                                     ([URL], True, None)):
            with self.subTest(targets=targets, since=since, until=until):
                with self.assertRaises(LookupFailure):
                    list(self.client.verify_many(targets, since, until))
        self.assertEqual(self.calls, [])

    def test_new_batch_reads_index_again_and_does_not_cache_absence(self):
        self.assertEqual(list(self.client.verify_many([URL], self.since)), [(URL, None)])
        self.rows = [FIELDS, self.row()]
        self.assertIsNotNone(dict(self.client.verify_many([URL], self.since))[URL])
        self.assertEqual(sum('/cdx/' in call[0] for call in self.calls), 2)

    def test_cooldowns_breaker_and_accepted_jobs_survive_batch_failures(self):
        from capture import RequestDeferred
        with tempfile.TemporaryDirectory() as folder:
            old = os.getcwd()
            try:
                os.chdir(folder)
                seed_test_state('state.json')
                with patch.dict(os.environ, {'GITHUB_RUN_ID': '12345', 'GITHUB_RUN_ATTEMPT': '1'}):
                    runner = Runner(self.client, 'state.json', budget=20000)
                    pending = {'job_id': 'accepted-contact', 'submitted_at': self.now - 60}
                    runner.state[URL] = dict(pending)
                    runner.save()
                    self.status = 429
                    for _ in range(2):
                        with self.assertRaises((LookupFailure, RequestDeferred)):
                            next(self.client.verify_many([URL], self.since))
                    self.assertTrue(runner.pause_circuit_open)
                    self.assertEqual(runner.paused_responses, 3)
                    self.assertEqual(runner.state[URL], pending)
                    self.assertEqual(len(self.calls), 3)
                    self.assertTrue(all(b[3] - a[3] >= 1200 for a, b in zip(self.calls, self.calls[1:])))
            finally:
                os.chdir(old)

class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.old = os.getcwd(); os.chdir(self.tmp.name); self.addCleanup(os.chdir, self.old)
        seed_test_state()
        seed_test_state('.capture-state/pending.json')
        self.c = Mock(); self.c.verify.return_value = None
        self.r = Runner(self.c, 'state.json', budget=200, interval=60)
        self.sleep = patch('capture.time.sleep').start(); self.addCleanup(patch.stopall)
        # main() is exercised offline; never write into a real CI step summary.
        patch.dict(os.environ, {'GITHUB_STEP_SUMMARY': ''}).start()

    def test_pending_never_verified_and_saved(self):
        self.c.json.side_effect = [(200, {}, {'job_id': 'job'})] + [(200, {}, {'status': 'pending'})]*12
        self.assertEqual(self.r.process(URL)['state'], 'pending')
        self.assertEqual(json.loads(Path('state.json').read_text())[URL]['job_id'], 'job')

    def test_old_success_rejected(self):
        self.c.json.side_effect = [(200, {}, {'job_id': 'job'}), (200, {}, {'status':'success','original_url':URL,'timestamp':'20200101000000'})]
        self.assertEqual(self.r.process(URL)['state'], 'review_required')

    def test_job_success_without_index_is_pending(self):
        self.c.json.side_effect = [(200, {}, {'job_id':'job'})] + [(200, {}, {'status':'success','original_url':URL,'timestamp':timestamp(time.time())})]*12
        self.assertEqual(self.r.process(URL)['state'], 'pending')

    def test_delayed_index_eventually_verifies(self):
        self.c.verify.side_effect = [None, None, {'timestamp':timestamp(time.time())}]
        self.c.json.side_effect = [(200, {}, {'job_id':'job'})] + [(200, {}, {'status':'success','original_url':URL,'timestamp':timestamp(time.time())})]*2
        self.assertEqual(self.r.process(URL)['state'], 'pending')
        self.assertEqual(self.r.process(URL)['state'], 'verified')
        self.assertNotIn(URL, self.r.state)

    def test_unknown_lookup_does_not_submit(self):
        self.c.verify.side_effect = LookupFailure('CDX timeout')
        self.assertEqual(self.r.process(URL)['state'], 'unverified'); self.c.json.assert_not_called()

    def test_uncertain_post_not_duplicated(self):
        self.c.json.side_effect = LookupFailure('timeout')
        self.assertEqual(self.r.process(URL)['state'], 'unverified')
        other = Runner(self.c, 'state.json')
        self.assertEqual(other.process(URL)['state'], 'pending')
        self.assertEqual(self.c.json.call_count, 1)

    def test_existing_pending_polled_without_post(self):
        self.r.state[URL] = {'job_id':'old-job', 'submitted_at':time.time()}
        self.c.json.return_value = (200, {}, {'status':'pending'})
        self.r.process(URL)
        for call in self.c.json.call_args_list:
            self.assertIn('/save/status/old-job', call.args[0])

    def test_host_pause_persists_cooldown(self):
        self.c.json.return_value = (429, {'retry-after':'900'}, {'status_ext':'error:host-crawling-paused'})
        self.assertEqual(self.r.process(URL)['state'], 'deferred')
        self.assertGreaterEqual(self.r.next_submit, time.time()+899)
        self.assertNotIn(URL, self.r.state)
        self.assertEqual(self.r.process(BASE+'/cart')['state'], 'deferred')
        self.assertEqual(self.c.json.call_count, 1)

    def test_actual_spn_human_pause_message(self):
        self.c.json.side_effect = [(200, {}, {'job_id':'job'}), (200, {}, {'status':'error',
            'status_ext':'please try again in ~10 min. crawling this host is paused because they notified us that they are overloaded right now. (http status=429)'})]
        self.assertEqual(self.r.process(URL)['state'], 'deferred')
        self.assertGreater(self.r.state['_next_submit'], time.time()+659)
        self.assertEqual(self.r.state[URL]['job_id'], 'job')

    def test_time_budget_is_deferred(self):
        self.r.deadline = time.time()
        self.assertEqual(self.r.process(URL)['state'], 'deferred'); self.c.verify.assert_not_called()

    def test_verify_only_never_submits(self):
        self.r.verify_only = True
        self.assertEqual(self.r.process(URL)['state'], 'unverified'); self.c.json.assert_not_called()

    def test_midnight_is_utc_coverage_boundary(self):
        self.assertEqual(timestamp(self.r.since)[8:], '000000')

    def test_failed_catalog_is_not_empty_success(self):
        self.c.request.return_value = (503, {}, '', BASE+'/collections/all')
        with self.assertRaises(LookupFailure): discover(self.c)

    def test_partial_catalog_is_incomplete(self):
        page = GOOD + '<a href="/products/watch">Watch</a><a href="?page=2">Next</a>'
        self.c.request.side_effect = [(200, {}, page, BASE+'/collections/all'),
                                     (429, {}, '', BASE+'/collections/all?page=2')]
        with self.assertRaises(LookupFailure): discover(self.c)

    def test_explicit_rejection_does_not_leave_uncertain_job(self):
        self.c.json.return_value = (401, {}, {'status':'error'})
        self.assertEqual(self.r.process(URL)['state'], 'failed')
        self.assertNotIn(URL, self.r.state)

    def test_terminal_error_retained_then_later_index_reconciles_without_post(self):
        self.c.json.side_effect = [(200, {}, {'job_id': 'accepted-job'}),
            (200, {}, {'status': 'error', 'status_ext': 'error:bad-request',
                       'message': 'SECRET raw server failure', 'url': 'https://private/SECRET'})]
        row = self.r.process(URL)
        self.assertEqual(row['state'], 'review_required')
        self.assertEqual(row['job_id'], 'accepted-job')
        self.assertIn('submitted_at', row)
        self.assertEqual(row['request_evidence']['if_not_archived_within'], '1')
        self.assertEqual(row['submission_response']['http_status'], 200)
        self.assertEqual(row['status_response']['status_ext'], 'error:bad-request')
        saved = json.loads(Path('state.json').read_text())
        self.assertIn(URL, saved)
        self.assertNotIn('SECRET', json.dumps(saved) + json.dumps(row))
        self.assertEqual(self.c.json.call_count, 2)
        other = Runner(self.c, 'state.json')
        self.assertEqual(other.process(URL)['state'], 'review_required')
        self.assertEqual(self.c.json.call_count, 2)
        self.c.verify.return_value = {'timestamp': timestamp(time.time()), 'replay': 'verified-replay'}
        row = other.process(URL)
        self.assertEqual(row['state'], 'verified')
        self.assertTrue(row['status_conflict'])
        self.assertTrue(row['new_capture'])
        self.assertEqual(row['job_id'], 'accepted-job')
        self.assertNotIn(URL, other.state)
        self.assertEqual(other.state['_completed_jobs'][-1]['status_response']['status_ext'], 'error:bad-request')
        self.assertEqual(self.c.json.call_count, 2)
    def test_status_error_immediately_reconciles_exact_verified_evidence(self):
        self.c.json.side_effect = [(200, {}, {'job_id': 'accepted-job'}),
            (200, {}, {'status': 'error', 'status_ext': 'error:bad-request'})]
        self.c.verify.side_effect = [None, {'timestamp': timestamp(time.time()), 'replay': 'verified-replay'}]
        row = self.r.process(URL)
        self.assertEqual(row['state'], 'verified')
        self.assertEqual(row['coverage_source'], 'status_error_reconciled_index')
        self.assertTrue(row['status_conflict'])
        self.assertEqual(row['job_id'], 'accepted-job')
        self.assertNotIn(URL, self.r.state)
        self.assertEqual(self.c.json.call_count, 2)
    def test_status_error_index_lookup_failure_preserves_job(self):
        self.c.json.side_effect = [(200, {}, {'job_id': 'accepted-job'}),
            (200, {}, {'status': 'error', 'status_ext': 'error:bad-request'})]
        self.c.verify.side_effect = [None, LookupFailure('CDX unavailable')]
        self.assertEqual(self.r.process(URL)['state'], 'unverified')
        self.assertTrue(self.r.state[URL]['review_required'])
        self.assertTrue(self.r.state[URL]['status_error'])
        self.assertEqual(self.r.state[URL]['job_id'], 'accepted-job')
    def test_preexisting_daily_capture_not_labeled_new_and_retains_diagnostics(self):
        self.r.state[URL] = {'submitted_at': time.time(), 'job_id': 'accepted-job',
                            'status_response': {'status_ext': 'error:bad-request'}, 'status_error': True}
        self.c.verify.return_value = {'timestamp': timestamp(time.time()-120), 'replay': 'verified-replay'}
        row = self.r.process(URL)
        self.assertFalse(row['new_capture'])
        self.assertEqual(row['job_id'], 'accepted-job')
        self.assertEqual(row['status_response']['status_ext'], 'error:bad-request')
    def test_submission_host_pause_audits_before_clear(self):
        self.c.json.return_value = (429, {'retry-after': '660'}, {'status_ext': 'error:host-crawling-paused'})
        row = self.r.process(URL)
        self.assertEqual(row['state'], 'deferred')
        self.assertIn('request_evidence', row)
        self.assertEqual(row['submission_response']['http_status'], 429)
        self.assertNotIn(URL, self.r.state)
        self.assertEqual(self.r.state['_completed_jobs'][-1]['submission_response'], row['submission_response'])
    def test_submission_pause_with_accepted_job_never_clears(self):
        self.c.json.return_value = (429, {'retry-after': '660'}, {'job_id': 'accepted-job', 'status_ext': 'error:host-crawling-paused'})
        row = self.r.process(URL)
        self.assertEqual(row['job_id'], 'accepted-job')
        self.assertEqual(self.r.state[URL]['job_id'], 'accepted-job')
        self.assertEqual(self.c.json.call_count, 1)

    def test_terminal_failure_audit_is_append_only_and_excludes_arbitrary_state(self):
        self.r.state['_completed_jobs'] = [{'old': i} for i in range(50)]
        self.r.state[URL] = {'job_id': 'accepted-job', 'submitted_at': time.time(), 'raw_secret': 'SECRET'}
        self.r.terminal_failure(URL, reason='SPN error', status_ext='error:bad-request')
        self.assertEqual(len(self.r.state['_completed_jobs']), 51)
        self.assertEqual(self.r.state['_completed_jobs'][0], {'old': 0})
        self.assertNotIn('SECRET', json.dumps(self.r.state['_completed_jobs']))
    def test_rejected_submission_retains_request_audit_without_job(self):
        self.c.json.return_value = (401, {}, {'status': 'error', 'message': 'SECRET'})
        row = self.r.process(URL)
        self.assertIn('request_evidence', row)
        self.assertNotIn('job_id', row)
        self.assertNotIn(URL, self.r.state)
        self.assertEqual(self.r.state['_completed_jobs'][-1]['http_status'], 401)
        self.assertNotIn('SECRET', json.dumps(self.r.state))

    def test_missing_original_url_is_not_success(self):
        self.c.json.side_effect = [(200, {}, {'job_id':'job'}),
            (200, {}, {'status':'success','timestamp':timestamp(time.time())})]
        self.assertEqual(self.r.process(URL)['state'], 'review_required')

    def test_ambiguous_success_preserved_without_duplicate_post(self):
        self.c.json.side_effect = [(200, {}, {'job_id': 'job'}),
            (200, {}, {'status': 'success', 'timestamp': '20200101000000',
                       'original_url': URL})]
        result = self.r.process(URL)
        self.assertEqual(result['state'], 'review_required')
        self.assertEqual(result['job_id'], 'job')
        self.assertFalse(result['status_evidence']['timestamp_fresh'])
        self.assertEqual(result['status_evidence']['timestamp'], '20200101000000')
        saved = json.loads(Path('state.json').read_text())
        self.assertEqual(saved[URL]['job_id'], 'job')
        self.c.json.side_effect = None
        self.c.json.return_value = (200, {}, {'status': 'success'})
        self.c.json.reset_mock()
        again = Runner(self.c, 'state.json')
        self.assertEqual(again.process(URL)['state'], 'review_required')
        self.c.json.assert_not_called()
        self.assertIn(URL, again.state)

    def test_ambiguous_success_diagnostics_exclude_secrets(self):
        self.c.json.side_effect = [(200, {}, {'job_id': 'job'}),
            (200, {'Authorization': 'secret'}, {'status': 'success',
             'timestamp': 'secret', 'original_url': 'https://evil.example/?token=secret',
             'message': 'secret', 'access_key': 'secret'})]
        result = self.r.process(URL)
        self.assertEqual(result['state'], 'review_required')
        self.assertFalse(result['status_evidence']['original_url_matches'])
        self.assertFalse(result['status_evidence']['timestamp_valid_format'])
        self.assertNotIn('secret', Path('state.json').read_text())
        self.assertNotIn('secret', Path('capture-report.json').read_text())

    def test_malformed_original_url_remains_pending(self):
        self.c.json.side_effect = [(200, {}, {'job_id': 'job'}),
            (200, {}, {'status': 'success', 'timestamp': timestamp(time.time()),
                       'original_url': {'unexpected': 'object'}})]
        self.assertEqual(self.r.process(URL)['state'], 'review_required')
        self.assertIn(URL, self.r.state)

    def test_read_only_recovery_preserves_ambiguous_job(self):
        self.r.state[URL] = {'job_id': 'job', 'submitted_at': time.time(),
                            'status_evidence': {'status': 'success'}}
        self.r.save()
        other = Runner(self.c, 'state.json', verify_only=True)
        result = other.process(URL)
        self.assertEqual(result['state'], 'unverified')
        self.assertEqual(result['job_id'], 'job')
        self.c.json.assert_not_called()
        self.assertIn(URL, other.state)
        self.c.verify.return_value = {'timestamp': timestamp(time.time())}
        self.assertEqual(other.process(URL)['state'], 'verified')
        self.c.json.assert_not_called()
        self.assertNotIn(URL, other.state)

    def test_poll_only_empty_state_never_submits(self):
        other = Runner(self.c, 'state.json', poll_only=True)
        self.assertEqual(other.process(URL)['state'], 'unverified')
        self.c.json.assert_not_called()
        self.assertNotIn(URL, other.state)

    def test_poll_only_existing_job_reads_status_without_post(self):
        self.r.state[URL] = {'job_id': 'saved-job', 'submitted_at': time.time()}
        self.r.save()
        self.c.json.return_value = (200, {}, {'status': 'success',
            'original_url': URL, 'timestamp': timestamp(time.time())})
        self.c.verify.side_effect = [None, {'timestamp': timestamp(time.time())}]
        other = Runner(self.c, 'state.json', poll_only=True)
        self.assertEqual(other.process(URL)['state'], 'verified')
        self.c.json.assert_called_once_with('https://web.archive.org/save/status/saved-job', authenticated=True)
        self.assertNotIn(URL, other.state)

    def test_poll_only_ambiguous_status_keeps_job(self):
        self.r.state[URL] = {'job_id': 'saved-job', 'submitted_at': time.time()}
        self.r.save()
        self.c.json.return_value = (200, {}, {'status': 'success',
            'original_url': URL, 'timestamp': '20200101000000'})
        other = Runner(self.c, 'state.json', poll_only=True)
        result = other.process(URL)
        self.assertEqual(result['state'], 'review_required')
        self.assertEqual(result['job_id'], 'saved-job')
        self.assertFalse(result['status_evidence']['timestamp_fresh'])
        self.c.json.assert_called_once_with('https://web.archive.org/save/status/saved-job', authenticated=True)
        self.assertIn(URL, other.state)

    def test_poll_only_unknown_job_outcome_never_submits(self):
        self.r.state[URL] = {'job_id': None, 'submitted_at': time.time()}
        self.r.save()
        other = Runner(self.c, 'state.json', poll_only=True)
        self.assertEqual(other.process(URL)['state'], 'pending')
        self.c.json.assert_not_called()
        self.assertIn(URL, other.state)

    def test_main_test_does_not_pollute_ci_summary(self):
        from capture import main
        path = Path('ci-summary.md')
        path.write_text('real summary')
        # The suite's setUp deliberately clears the inherited variable.
        self.assertEqual(os.environ.get('GITHUB_STEP_SUMMARY'), '')
        with patch('sys.argv', ['capture.py', '--verify-only', '--scope', 'pilot']), patch('capture.Client', return_value=self.c):
            main()
        self.assertEqual(path.read_text(), 'real summary')

    def test_pilot_main_host_pause_stops_after_three_responses(self):
        from capture import main
        self.c.json.return_value = (429, {'retry-after': '660'},
                                    {'status_ext': 'error:host-crawling-paused'})
        with patch('sys.argv', ['capture.py', '--scope', 'pilot']), patch('capture.Client', return_value=self.c):
            self.assertEqual(main(), 1)
        self.assertEqual(self.c.json.call_count, 3)
        self.assertEqual(self.c.verify.call_count, 3)
        self.assertEqual(self.c.json.call_args.args[0], 'https://web.archive.org/save/')
        saved = json.loads(Path('.capture-state/pending.json').read_text())
        self.assertGreater(saved['_next_submit'], time.time() + 1199)
        report = json.loads(Path('capture-report.json').read_text())
        self.assertEqual(len(report), 4)
        self.assertEqual(report[0]['state'], 'deferred')

    def old_success_state(self):
        ts = timestamp(self.r.since - 60)
        self.r.state[URL] = {'job_id': 'saved-job', 'submitted_at': time.time(),
            'status_evidence': {'status': 'success', 'original_url_matches': True,
                               'timestamp': ts}}
        self.r.save()
        evidence = {'timestamp': ts, 'original': URL, 'statuscode': '200',
                    'mimetype': 'text/html', 'replay_url': 'https://web.archive.org/web/' + ts + 'id_/' + URL}
        self.c.verify.side_effect = lambda url, since, until=None: evidence if until == since else None
        return evidence

    def test_saved_success_migrates_read_only_and_no_forever_poll(self):
        evidence = self.old_success_state()
        self.r.verify_only = True
        self.assertEqual(self.r.process(URL)['state'], 'reused')
        self.assertEqual(self.r.state[URL]['terminal_reused'], evidence)
        self.assertEqual(self.r.state[URL]['retry_eligible_at'], self.r.since + 86400)
        self.c.json.assert_not_called()
        self.r.verify_only = False
        self.assertEqual(self.r.process(URL)['state'], 'deferred')
        self.c.json.assert_not_called()
        self.assertEqual(len([c for c in self.c.verify.call_args_list if c.kwargs.get('until') == c.args[1]]), 1)

    def test_terminal_reused_next_day_one_regular_post_and_audit(self):
        self.old_success_state()
        self.r.process(URL)
        self.r.state[URL]['retry_eligible_at'] = self.r.since - 1
        self.c.json.side_effect = [(200, {}, {'job_id': 'new-job'})] + [(200, {}, {'status': 'pending'})] * 12
        self.assertEqual(self.r.process(URL)['state'], 'pending')
        posts = [call for call in self.c.json.call_args_list if call.args[0] == 'https://web.archive.org/save/']
        self.assertEqual(len(posts), 1)
        self.assertEqual(self.r.state['_completed_jobs'][-1]['job_id'], 'saved-job')
        self.assertEqual(self.r.state[URL]['job_id'], 'new-job')

    def test_terminal_reused_poll_only_never_releases_even_when_due(self):
        self.old_success_state(); self.r.process(URL)
        self.r.state[URL]['retry_eligible_at'] = self.r.since - 1
        self.r.poll_only = True
        self.assertEqual(self.r.process(URL)['state'], 'deferred')
        self.assertEqual(self.r.state[URL]['job_id'], 'saved-job')
        self.c.json.assert_not_called()

    def test_same_utc_day_reuse_is_coverage_not_new_capture(self):
        ts = timestamp(self.r.since + 1)
        self.c.verify.side_effect = [None, {'timestamp': ts}]
        self.c.json.side_effect = [(200, {}, {'job_id': 'job'}),
            (200, {}, {'status': 'success', 'original_url': URL, 'timestamp': ts})]
        result = self.r.process(URL)
        self.assertEqual(result['state'], 'verified')
        self.assertFalse(result['new_capture'])
        self.assertNotIn(URL, self.r.state)

    def test_prior_day_capture_requires_exact_evidence(self):
        self.old_success_state()
        self.c.verify.side_effect = None
        self.c.verify.return_value = None
        self.r.poll_only = True
        self.c.json.return_value = (200, {}, {'status': 'success', 'original_url': URL,
                                             'timestamp': timestamp(self.r.since - 60)})
        self.assertEqual(self.r.process(URL)['state'], 'review_required')
        self.assertNotIn('terminal_reused', self.r.state[URL])

    def test_terminal_reuse_honors_longer_server_retry_after(self):
        ts = timestamp(self.r.since - 60)
        evidence = {'timestamp': ts}
        self.c.verify.side_effect = [None, None, evidence]
        self.c.json.side_effect = [(200, {}, {'job_id': 'job'}),
            (200, {'retry-after': '172800'}, {'status': 'success', 'original_url': URL, 'timestamp': ts})]
        self.assertEqual(self.r.process(URL)['state'], 'reused')
        self.assertGreater(self.r.state[URL]['retry_eligible_at'], time.time() + 172799)

    def test_status_expired_unknown_is_review_required_not_reposted(self):
        self.r.state[URL] = {'job_id': 'expired', 'submitted_at': time.time() - 10000}
        self.c.json.return_value = (404, {}, {'status': 'error'})
        self.assertEqual(self.r.process(URL)['state'], 'review_required')
        self.c.json.reset_mock()
        self.assertEqual(self.r.process(URL)['state'], 'review_required')
        self.c.json.assert_not_called()
        self.assertEqual(self.r.state[URL]['job_id'], 'expired')

    def test_unknown_error_text_is_not_echoed(self):
        self.c.json.return_value = (401, {}, {'status': 'error', 'status_ext': 'secret-token-123'})
        result = self.r.process(URL)
        self.assertEqual(result['status_ext'], 'unknown')
        self.assertNotIn('secret-token-123', Path('capture-report.json').read_text())

    def test_terminal_reuse_honors_http_date_retry_after(self):
        from email.utils import formatdate
        ts = timestamp(self.r.since - 60)
        deadline = time.time() + 172800
        self.c.verify.side_effect = [None, None, {'timestamp': ts}]
        self.c.json.side_effect = [(200, {}, {'job_id': 'job'}),
            (200, {'retry-after': formatdate(deadline, usegmt=True)},
             {'status': 'success', 'original_url': URL, 'timestamp': ts})]
        self.assertEqual(self.r.process(URL)['state'], 'reused')
        self.assertAlmostEqual(self.r.state[URL]['retry_eligible_at'], deadline, delta=1)

    def test_submission_response_diagnostics_are_sanitized_and_preserved(self):
        self.c.json.side_effect = [(200, {'retry-after': '172800'},
            {'job_id': 'job', 'status': 'success', 'message': 'Same snapshot secret-token', 'secret': 'secret-token'}),
            (200, {}, {'status': 'success', 'original_url': URL, 'timestamp': '20200101000000'})]
        result = self.r.process(URL)
        response = result['submission_response']
        self.assertEqual(response['message_classification'], 'same_snapshot')
        self.assertEqual(response['http_status'], 200)
        self.assertGreater(response['retry_after_until'], time.time() + 172799)
        self.assertNotIn('secret-token', Path('state.json').read_text())
        self.assertNotIn('secret-token', Path('capture-report.json').read_text())

    def test_existing_daily_index_never_claims_new_capture(self):
        self.c.verify.return_value = {'timestamp': timestamp(time.time())}
        result = self.r.process(URL)
        self.assertFalse(result['new_capture'])
        self.assertEqual(result['coverage_source'], 'existing_index')
        self.c.json.assert_not_called()

    def test_status_429_retains_job_and_next_run_never_reposts(self):
        self.c.json.side_effect = [(200, {}, {'job_id': 'accepted-job'}),
                                    (429, {'retry-after': '900'}, {'status': 'error'})]
        self.assertEqual(self.r.process(URL)['state'], 'deferred')
        saved = json.loads(Path('state.json').read_text())
        self.assertEqual(saved[URL]['job_id'], 'accepted-job')
        self.assertGreater(saved[URL]['status_paused_until'], time.time() + 899)
        self.c.json.reset_mock()
        other = Runner(self.c, 'state.json')
        self.assertEqual(other.process(URL)['state'], 'deferred')
        self.c.json.assert_not_called()
        other.state[URL]['status_paused_until'] = 0
        self.c.json.side_effect = None
        self.c.json.return_value = (200, {}, {'status': 'pending'})
        self.assertEqual(other.process(URL)['state'], 'pending')
        self.assertTrue(all(c.args[0].endswith('/status/accepted-job') for c in self.c.json.call_args_list))

    def test_unknown_status_error_preserves_review_required_job(self):
        self.r.state[URL] = {'job_id': 'saved-job', 'submitted_at': time.time()}
        self.c.json.return_value = (200, {}, {'status': 'error', 'status_ext': 'unknown-or-expired'})
        self.assertEqual(self.r.process(URL)['state'], 'review_required')
        self.assertEqual(self.r.state[URL]['job_id'], 'saved-job')

    def test_catalog_page_two_canonical_matches_requested_query(self):
        page1 = GOOD + '<link rel="canonical" href="' + BASE + '/collections/all"><a href="/products/first">First</a><a href="?page=2">Next</a>'
        page2 = GOOD + '<link rel="canonical" href="' + BASE + '/collections/all?page=2"><a href="/products/second">Second</a>'
        self.c.request.side_effect = [(200, {}, page1, BASE + '/collections/all'),
                                     (200, {}, page2, BASE + '/collections/all?page=2')]
        self.assertEqual(discover(self.c), [
                                          BASE + '/products/first', BASE + '/products/second'])

    def test_catalog_page_two_wrong_final_query_rejected(self):
        page1 = GOOD + '<a href="/products/first">First</a><a href="?page=2">Next</a>'
        page2 = GOOD + '<link rel="canonical" href="' + BASE + '/collections/all?page=2"><a href="/products/second">Second</a>'
        self.c.request.side_effect = [(200, {}, page1, BASE + '/collections/all'),
                                     (200, {}, page2, BASE + '/collections/all')]
        with self.assertRaises(LookupFailure):
            discover(self.c)

    def test_catalog_page_two_wrong_canonical_query_rejected(self):
        page1 = GOOD + '<a href="/products/first">First</a><a href="?page=2">Next</a>'
        page2 = GOOD + '<link rel="canonical" href="' + BASE + '/collections/all"><a href="/products/second">Second</a>'
        self.c.request.side_effect = [(200, {}, page1, BASE + '/collections/all'),
                                     (200, {}, page2, BASE + '/collections/all?page=2')]
        with self.assertRaises(LookupFailure):
            discover(self.c)

    def test_new_known_not_sent_marker_can_retry_but_legacy_unknown_cannot(self):
        failure = LookupFailure('Network failure', transport={'known_not_sent': True, 'phase': 'tcp_connect'})
        self.c.json.side_effect = failure
        self.assertEqual(self.r.process(URL)['state'], 'unverified')
        self.assertTrue(self.r.state[URL]['known_not_sent'])
        self.c.json.side_effect = [(200, {}, {'job_id': 'new-job'})] + [(200, {}, {'status': 'pending'})] * 12
        self.assertEqual(self.r.process(URL)['state'], 'pending')
        self.assertEqual(self.r.state[URL]['job_id'], 'new-job')
        legacy_url = BASE + '/pages/legacy'
        self.r.state[legacy_url] = {'job_id': None, 'submitted_at': time.time() - 3600}
        self.c.json.reset_mock()
        self.assertEqual(self.r.process(legacy_url)['state'], 'pending')
        self.c.json.assert_not_called()
        self.assertNotIn('known_not_sent', self.r.state[legacy_url])

    def test_three_get_transport_failed_urls_open_breaker_and_skip_reads(self):
        self.c.verify.side_effect = LookupFailure('Network failure', transport={'known_not_sent': True, 'phase': 'tcp_connect'})
        for path in ('/one', '/two', '/three'):
            self.assertEqual(self.r.process(BASE + path)['state'], 'unverified')
        self.assertTrue(self.r.transport_circuit_open)
        self.assertGreater(self.r.state['_transport_cooldown_until'], time.time() + 899)
        self.c.verify.reset_mock()
        self.assertEqual(self.r.process(BASE + '/four')['state'], 'deferred')
        self.c.verify.assert_not_called(); self.c.json.assert_not_called()
        self.assertTrue(all(r['state'] != 'verified' for r in self.r.results))

    def test_intermediate_healthy_cdx_does_not_mask_three_post_failures(self):
        self.c.archive_transport_failures = 0
        self.c.archive_healthy_responses = 0
        def healthy_cdx(*args, **kwargs):
            self.c.archive_healthy_responses += 1
            return None
        self.c.verify.side_effect = healthy_cdx
        self.c.json.side_effect = LookupFailure('Network failure', transport={'known_not_sent': False, 'phase': 'read'})
        for path in ('/one', '/two', '/three'):
            self.assertEqual(self.r.process(BASE + path)['state'], 'unverified')
        self.assertTrue(self.r.transport_circuit_open)
        for path in ('/one', '/two', '/three'):
            self.assertIn(BASE + path, self.r.state)
            self.assertNotIn('known_not_sent', self.r.state[BASE + path])

    def test_completed_healthy_archive_operation_resets_streak(self):
        self.c.archive_transport_failures = 0
        self.c.archive_healthy_responses = 0
        self.c.verify.side_effect = LookupFailure('Network failure', transport={'known_not_sent': False})
        self.r.process(BASE + '/one'); self.r.process(BASE + '/two')
        self.assertEqual(self.r.transport_failure_streak, 2)
        def healthy(*args, **kwargs):
            self.c.archive_healthy_responses += 1
            return {'timestamp': timestamp(time.time())}
        self.c.verify.side_effect = healthy
        self.assertEqual(self.r.process(BASE + '/healthy')['state'], 'verified')
        self.assertEqual(self.r.transport_failure_streak, 0)

    def test_persisted_transport_cooldown_defers_without_mutating_legacy_pending(self):
        self.r.state['_transport_cooldown_until'] = time.time() + 900
        self.r.state[URL] = {'job_id': None, 'submitted_at': time.time() - 3600}
        self.r.save()
        other = Runner(self.c, 'state.json')
        self.assertEqual(other.process(URL)['state'], 'deferred')
        self.c.verify.assert_not_called(); self.c.json.assert_not_called()
        self.assertEqual(other.state[URL]['job_id'], None)
        self.assertNotIn('known_not_sent', other.state[URL])

    def test_safe_retry_audit_paces_next_url_from_latest_attempt(self):
        late = time.time() + 60
        def submit_or_status(endpoint, *args, **kwargs):
            if endpoint.endswith('/save/'):
                self.c.post_attempt_callback({'attempt': 1, 'started_at': late - 60})
                self.c.post_attempt_callback({'attempt': 2, 'started_at': late})
                return (200, {}, {'job_id': 'job'})
            return (200, {}, {'status': 'pending'})
        self.c.json.side_effect = submit_or_status
        self.assertEqual(self.r.process(URL)['state'], 'pending')
        self.assertGreaterEqual(self.r.state['_next_submit'], late + self.r.interval)
        self.assertEqual([a['attempt'] for a in self.r.state[URL]['request_evidence']['attempts']], [1, 2])

    def test_incomplete_main_returns_nonzero(self):
        from capture import main
        with patch('sys.argv', ['capture.py', '--verify-only', '--scope', 'pilot']), patch('capture.Client', return_value=self.c):
            self.assertEqual(main(), 1)

class TransportResilienceTests(unittest.TestCase):
    def failure(self, known=True, phase='tcp_connect'):
        return LookupFailure('Network operation failed: ConnectionRefusedError', transport={
            'known_not_sent': known, 'phase': phase, 'reason_class': 'ConnectionRefusedError', 'errno': 111})

    @patch('capture.time.sleep')
    def test_known_tcp_not_sent_has_at_most_one_retry(self, sleep):
        c = Client(); c.post_retry_deadline = time.time() + 300
        c._request = Mock(side_effect=[self.failure(), self.failure()])
        with self.assertRaises(LookupFailure): c.request('https://web.archive.org/save/', {'url': URL})
        self.assertEqual(c._request.call_count, 2)
        sleep.assert_called_once_with(60)
        self.assertEqual(c.archive_transport_failures, 1)

    @patch('capture.time.sleep')
    def test_first_safe_second_uncertain_overrides_proof(self, sleep):
        c = Client(); c.post_retry_deadline = time.time() + 300
        c._request = Mock(side_effect=[self.failure(), self.failure(False, 'read')])
        with self.assertRaises(LookupFailure) as raised: c.request('https://web.archive.org/save/', {'url': URL})
        self.assertFalse(raised.exception.transport['known_not_sent'])
        self.assertEqual(raised.exception.transport['phase'], 'read')
        self.assertEqual(c._request.call_count, 2)

    @patch('capture.time.sleep')
    def test_after_send_or_unknown_post_never_retried(self, sleep):
        for error in (self.failure(False, 'read'), LookupFailure('unknown failure')):
            c = Client(); c.post_retry_deadline = time.time() + 300
            c._request = Mock(side_effect=error)
            with self.assertRaises(LookupFailure): c.request('https://web.archive.org/save/', {'url': URL})
            self.assertEqual(c._request.call_count, 1)
        sleep.assert_not_called()

    @patch('capture.time.sleep')
    def test_post_http_status_never_transport_retried(self, sleep):
        for status in (301, 307, 429, 503):
            c = Client(); c.post_retry_deadline = time.time() + 300
            c._request = Mock(return_value=(status, {}, '', 'https://web.archive.org/save/'))
            self.assertEqual(c.request('https://web.archive.org/save/', {'url': URL})[0], status)
            self.assertEqual(c._request.call_count, 1)
        sleep.assert_not_called()

    @patch('capture.time.sleep')
    def test_post_retry_budget_and_longer_cooldown(self, sleep):
        c = Client(); c.post_retry_deadline = time.time() + 60
        c._request = Mock(side_effect=self.failure())
        with self.assertRaises(LookupFailure): c.request('https://web.archive.org/save/', {'url': URL})
        self.assertEqual(c._request.call_count, 1); sleep.assert_not_called()
        c.post_retry_deadline = time.time() + 1000
        c.post_retry_not_before = time.time() + 900
        c._request = Mock(side_effect=[self.failure(), (200, {}, '{}', 'https://web.archive.org/save/')])
        c.request('https://web.archive.org/save/', {'url': URL})
        self.assertGreater(sleep.call_args.args[0], 899)
        self.assertEqual(c._request.call_count, 2)

    def test_physical_tcp_proof_is_narrow_and_per_connection(self):
        from capture import TrackedHTTPSConnection
        tracker = {'tcp_established': False}
        with patch('socket.create_connection', side_effect=ConnectionRefusedError(111, 'SECRET')):
            connection = TrackedHTTPSConnection('web.archive.org', transport_tracker=tracker)
            with self.assertRaises(ConnectionRefusedError): connection._create_connection(('web.archive.org', 443))
        self.assertTrue(tracker['tcp_connect_failed'])
        self.assertFalse(tracker['tcp_established'])
        next_tracker = {'tcp_established': False}
        with patch('socket.create_connection', return_value=object()):
            connection = TrackedHTTPSConnection('web.archive.org', transport_tracker=next_tracker)
            connection._create_connection(('web.archive.org', 443))
        self.assertTrue(next_tracker['tcp_established'])
        self.assertNotIn('tcp_connect_failed', next_tracker)

    def test_proxy_connect_tls_and_send_failures_remain_uncertain(self):
        import urllib.error
        for phase_message in ('proxy CONNECT failed SECRET', 'TLS failed SECRET', 'partial send SECRET'):
            def opener(*handlers):
                tracked = next(h for h in handlers if hasattr(h, 'tracker'))
                tracked.tracker['tcp_established'] = True
                result = Mock(); result.open.side_effect = urllib.error.URLError(OSError(phase_message))
                return result
            with patch('capture.ur.build_opener', side_effect=opener):
                with self.assertRaises(LookupFailure) as raised:
                    Client()._request('https://web.archive.org/save/', {'url': URL})
            self.assertFalse(raised.exception.transport['known_not_sent'])
            self.assertNotIn('SECRET', str(raised.exception))

    def test_tcp_connection_refusal_proof_from_real_handler(self):
        with patch('socket.create_connection', side_effect=ConnectionRefusedError(111, 'SECRET')):
            with self.assertRaises(LookupFailure) as raised:
                Client()._request('https://web.archive.org/save/', {'url': URL})
        self.assertTrue(raised.exception.transport['known_not_sent'])
        self.assertEqual(raised.exception.transport['phase'], 'tcp_connect')
        self.assertEqual(raised.exception.transport['errno'], 111)
        self.assertNotIn('SECRET', str(raised.exception))

    @patch('capture.time.sleep')
    def test_each_post_attempt_is_audited_before_network_io(self, sleep):
        c = Client(); c.post_retry_deadline = time.time() + 300
        audit = []
        c.post_attempt_callback = lambda entry: audit.append(entry.copy())
        def perform(*args):
            if len(audit) == 1:
                raise self.failure()
            self.assertEqual(len(audit), 2)
            return (200, {}, '{}', 'https://web.archive.org/save/')
        c._request = Mock(side_effect=perform)
        c.request('https://web.archive.org/save/', {'url': URL})
        self.assertEqual([entry['attempt'] for entry in audit], [1, 2])
        self.assertTrue(all(isinstance(entry['started_at'], float) for entry in audit))

    @patch('capture.time.sleep')
    def test_actual_response_read_failure_never_retries_post(self, sleep):
        import http.client
        for failure in (TimeoutError('SECRET'), http.client.IncompleteRead(b'SECRET')):
            class BrokenResponse:
                status = 200
                headers = {}
                url = 'https://web.archive.org/save/'
                def __enter__(self): return self
                def __exit__(self, *args): return False
                def read(self, limit): raise failure
            opener = Mock(); opener.open.return_value = BrokenResponse()
            client = Client(); client.post_retry_deadline = time.time() + 300
            with patch('capture.ur.build_opener', return_value=opener):
                with self.assertRaises(LookupFailure) as raised:
                    client.request('https://web.archive.org/save/', {'url': URL})
            self.assertEqual(raised.exception.transport['phase'], 'read')
            self.assertFalse(raised.exception.transport['known_not_sent'])
            self.assertNotIn('SECRET', str(raised.exception))
            self.assertEqual(opener.open.call_count, 1)
        sleep.assert_not_called()

    def test_audit_callback_failure_prevents_network_attempt(self):
        client = Client(); client.post_retry_deadline = time.time() + 300
        client.post_attempt_callback = Mock(side_effect=OSError('audit unavailable'))
        client._request = Mock()
        with self.assertRaises(OSError): client.request('https://web.archive.org/save/', {'url': URL})
        client._request.assert_not_called()

    def test_authenticated_scope_rejects_non_spn_before_reading_keys(self):
        for target in ('https://evil.example/save/', 'https://web.archive.org/cdx/search/cdx',
                       'http://web.archive.org/save/', 'https://web.archive.org/save/?token=x'):
            with patch('capture.os.environ.get', side_effect=AssertionError('must not read credentials')):
                with self.assertRaises(LookupFailure): Client()._request(target, {'url': URL}, True)


class SitemapTests(unittest.TestCase):
    def setUp(self):
        self.client = Mock()
        self.sleep = patch('capture.time.sleep').start()
        self.addCleanup(patch.stopall)
        self.ns = 'http://www.sitemaps.org/schemas/sitemap/0.9'

    def xml(self, urls, index=False):
        root, child = ('sitemapindex', 'sitemap') if index else ('urlset', 'url')
        return '<' + root + ' xmlns="' + self.ns + '">' + ''.join('<' + child + '><loc>' + u.replace('&', '&amp;') + '</loc></' + child + '>' for u in urls) + '</' + root + '>'

    def response(self, body, url=None):
        return (200, {}, body, url or BASE + '/sitemap.xml')

    def test_nested_indexes_pages_products_collections_blogs_and_posts(self):
        from capture import discover_sitemap
        root = BASE + '/sitemap.xml'; nested = BASE + '/nested.xml'; pages = BASE + '/pages.xml'
        urls = [BASE + '/pages/about', BASE + '/products/watch', BASE + '/collections/new',
                BASE + '/blogs/news', BASE + '/blogs/news/post']
        self.client.request.side_effect = [self.response(self.xml([nested], True), root),
            self.response(self.xml([pages], True), nested), self.response(self.xml(urls), pages)]
        self.assertEqual(discover_sitemap(self.client), sorted(urls))
        self.assertEqual(self.client.request.call_count, 3)

    def test_image_external_and_duplicate_locations_are_not_targets(self):
        from capture import discover_sitemap
        body = '<urlset xmlns="' + self.ns + '" xmlns:image="http://www.google.com/schemas/sitemap-image/1.1">'
        body += '<url><loc>' + BASE + '/pages/about</loc><image:image><image:loc>https://cdn.shopify.com/a.png</image:loc></image:image></url>'
        body += '<url><loc>' + BASE + '/pages/about/</loc></url><url><loc>https://other.example/x</loc></url></urlset>'
        self.client.request.return_value = self.response(body)
        self.assertEqual(len(discover_sitemap(self.client)), 1)

    def test_external_index_not_fetched(self):
        from capture import discover_sitemap
        local = BASE + '/pages.xml'
        self.client.request.side_effect = [self.response(self.xml(['https://other.example/map.xml', local], True)),
                                          self.response(self.xml([BASE + '/pages/a']), local)]
        self.assertEqual(discover_sitemap(self.client), [BASE + '/pages/a'])
        self.assertEqual(self.client.request.call_count, 2)

    def test_cycle_is_deduplicated(self):
        from capture import discover_sitemap
        pages = BASE + '/pages.xml'
        self.client.request.side_effect = [self.response(self.xml([BASE + '/sitemap.xml', pages, pages], True)),
                                          self.response(self.xml([BASE + '/pages/a']), pages)]
        self.assertEqual(discover_sitemap(self.client), [BASE + '/pages/a'])
        self.assertEqual(self.client.request.call_count, 2)

    def test_bounded_depth_map_count_and_url_count(self):
        from capture import discover_sitemap
        for kwargs in ({'max_depth': 0}, {'max_sitemaps': 1}):
            with self.subTest(kwargs=kwargs):
                self.client.request.return_value = self.response(self.xml([BASE + '/next.xml'], True))
                with self.assertRaises(LookupFailure): discover_sitemap(self.client, **kwargs)
        self.client.request.return_value = self.response(self.xml([BASE + '/pages/a', BASE + '/pages/b']))
        with self.assertRaises(LookupFailure): discover_sitemap(self.client, max_urls=1)

    def test_malformed_dtd_unexpected_root_and_redirect_fail_closed(self):
        from capture import discover_sitemap
        for body in ('<broken', '<!DOCTYPE x><urlset/>', '<urlset xmlns="https://wrong.example"/>', '<html/>'):
            with self.subTest(body=body):
                self.client.request.return_value = self.response(body)
                with self.assertRaises(LookupFailure): discover_sitemap(self.client)
        self.client.request.return_value = self.response(self.xml([BASE + '/pages/a']), 'https://other.example/map.xml')
        with self.assertRaises(LookupFailure): discover_sitemap(self.client)

    def test_supplemental_excludes_entire_all_scope_not_just_successes(self):
        from capture import discover_supplemental
        with patch('capture.discover', return_value=[BASE + '/collections/all', BASE + '/products/watch']), patch('capture.discover_sitemap', return_value=[BASE + '/', BASE + '/pages/warranty-policy', BASE + '/products/watch', BASE + '/blogs/news/post']):
            self.assertEqual(discover_supplemental(self.client), [BASE + '/blogs/news/post'])

    def test_supplemental_discovery_failure_does_not_process_priority(self):
        from capture import main
        with tempfile.TemporaryDirectory() as tmp, patch('sys.argv', ['capture.py', '--scope', 'supplemental', '--state', tmp + '/state.json']), patch('capture.discover_supplemental', side_effect=LookupFailure('failed')), patch('capture.Runner.process') as process, patch('capture.Runner.result') as result, patch.dict(os.environ, {'GITHUB_STEP_SUMMARY': ''}):
            seed_test_state(tmp + '/state.json')
            self.assertEqual(main(), 1)
            process.assert_not_called()
            self.assertEqual(result.call_args.args[1], 'discovery-incomplete')

    def test_known_markdown_target_reported_without_submission(self):
        from capture import main
        with tempfile.TemporaryDirectory() as tmp:
            old = os.getcwd()
            try:
                os.chdir(tmp)
                seed_test_state('.capture-state/pending.json')
                with patch('sys.argv', ['capture.py', '--scope', 'supplemental']), patch('capture.discover_supplemental', return_value=[BASE + '/agents.md', BASE + '/pages/about']), patch('capture.Runner.process') as process, patch.dict(os.environ, {'GITHUB_STEP_SUMMARY': ''}):
                    self.assertEqual(main(), 1)
                    process.assert_called_once_with(BASE + '/pages/about')
                    report = json.loads(Path('capture-report.json').read_text())
                    self.assertEqual(report[0]['url'], BASE + '/agents.md')
                    self.assertEqual(report[0]['state'], 'unsupported-document-type')
            finally:
                os.chdir(old)

    def test_empty_supplemental_inventory_is_not_fake_capture_success(self):
        from capture import main
        with tempfile.TemporaryDirectory() as tmp:
            old = os.getcwd()
            try:
                os.chdir(tmp)
                seed_test_state('.capture-state/pending.json')
                with patch('sys.argv', ['capture.py', '--scope', 'supplemental']), patch('capture.discover_supplemental', return_value=[]), patch('capture.Runner.process') as process, patch.dict(os.environ, {'GITHUB_STEP_SUMMARY': ''}):
                    self.assertEqual(main(), 0)
                    process.assert_not_called()
                    self.assertEqual(json.loads(Path('capture-report.json').read_text())[0]['state'], 'no-additional-targets')
            finally:
                os.chdir(old)


class ResumableQueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = os.getcwd(); os.chdir(self.tmp.name)
        self.now = 1793589000.0
        self.clock = patch('capture.time.time', side_effect=lambda: self.now); self.clock.start()
        self.sleep = patch('capture.time.sleep', side_effect=self.advance); self.sleep.start()
        seed_test_state()
        seed_test_state('.capture-state/pending.json')
        self.c = Client(); self.r = Runner(self.c, 'state.json', budget=10000)
        self.urls = [BASE + '/pages/test-' + str(i) for i in range(7)]
    def tearDown(self):
        self.sleep.stop(); self.clock.stop(); os.chdir(self.old); self.tmp.cleanup()
    def advance(self, seconds): self.now += seconds
    def operation(self, url): return self.r.result(url, 'verified')
    def health(self, url):
        return (200, {}, '{}' if '/save/status/' in url else '[]', url)
    def test_pause_recovers_only_untouched_suffix(self):
        calls = []
        def process(url):
            calls.append(url)
            if len(calls) == 3:
                self.r.transport_circuit_open = True
                self.r.state['_transport_cooldown_until'] = self.now + 900
            return self.r.result(url, 'unverified' if len(calls) <= 3 else 'verified')
        self.r.process = Mock(side_effect=process)
        self.c._request = Mock(side_effect=self.health)
        self.r.run_queue(self.urls, 'all')
        self.assertEqual(calls, self.urls)
        self.assertEqual(self.c._request.call_count, 4)
        self.assertGreaterEqual(self.now, 1793589000 + 930)
        self.assertTrue(next(iter(self.r.state['_queues'].values()))['complete'])
        self.assertEqual(sum(r['state'] == 'unverified' for r in self.r.results), 3)
        self.assertTrue(all(len(c.args) == 1 and not c.kwargs for c in self.c._request.call_args_list))
    def test_failed_probes_bounded_and_checkpoint_resumes_suffix(self):
        self.r.state['_transport_cooldown_until'] = self.now + 900
        self.c._request = Mock(side_effect=LookupFailure('offline'))
        self.r.process = Mock()
        self.r.run_queue(self.urls, 'all')
        queue = next(iter(self.r.state['_queues'].values()))
        self.assertEqual(queue['cursor'], 0); self.assertFalse(queue['complete'])
        self.assertEqual(self.c._request.call_count, 2); self.r.process.assert_not_called()
        self.assertEqual(len(self.r.results), len(self.urls))
        other = Runner(self.c, 'state.json', budget=10000)
        self.c._request.side_effect = self.health
        other.process = Mock(side_effect=lambda u: other.result(u, 'verified'))
        other.run_queue([BASE + '/ignored-new-discovery'], 'all')
        self.assertEqual([c.args[0] for c in other.process.call_args_list], self.urls)
        self.assertTrue(all(r['state'] == 'verified' for r in other.results))
    def test_budget_does_not_consume_untouched_cursor(self):
        self.r.deadline = self.now + 100
        self.r.process = Mock(); self.c._request = Mock()
        self.r.run_queue(self.urls, 'all')
        queue = next(iter(self.r.state['_queues'].values()))
        self.assertEqual(queue['cursor'], 0); self.assertFalse(queue['complete'])
        self.r.process.assert_not_called(); self.c._request.assert_not_called()
    def test_expired_cooldown_still_requires_probe_and_429_honored(self):
        self.r.state['_transport_cooldown_until'] = self.now - 1
        self.c._request = Mock(side_effect=lambda u: (429, {'Retry-After': '7200'}, '{}', u))
        self.r.deadline = self.now + 3000
        self.r.process = Mock(); self.r.run_queue(self.urls, 'all')
        self.r.process.assert_not_called()
        self.assertGreaterEqual(self.r.state['_transport_cooldown_until'], self.now + 7200)
    def test_crash_marker_skipped_pending_untouched(self):
        key = 'all:' + timestamp(self.r.since)[:8] + ':capture:daily-v2'
        unknown = {'submitted_at': self.now - 100, 'job_id': None}
        self.r.state[self.urls[0]] = dict(unknown)
        self.r.state['_queues'] = {key: {'urls': self.urls, 'cursor': 0, 'results': [], 'complete': False, 'started': self.urls[0]}}
        self.r.process = Mock(side_effect=self.operation)
        self.r.run_queue(self.urls, 'all')
        self.assertEqual([c.args[0] for c in self.r.process.call_args_list], self.urls[1:])
        self.assertEqual(self.r.state[self.urls[0]], unknown)
        self.assertEqual(self.r.results[0]['state'], 'unverified')
    def test_namespaces_and_completed_queue_rebuild(self):
        self.r.deadline = self.now + 100
        self.r.run_queue(self.urls, 'all')
        original = json.loads(json.dumps(self.r.state['_queues']))
        self.r.run_queue([BASE + '/probe'], 'transport_probe')
        for key, value in original.items(): self.assertEqual(self.r.state['_queues'][key], value)
        self.assertEqual(len(self.r.state['_queues']), 2)
        self.r.deadline = self.now + 10000
        self.r.process = Mock(side_effect=self.operation)
        self.r.run_queue(self.urls, 'all')
        self.r.results = []
        self.r.run_queue(self.urls, 'all')
        self.assertEqual(self.r.process.call_count, len(self.urls)*2)
    def test_probe_rejects_redirect_or_bad_json(self):
        for response in [(200, {}, '{}', 'https://example.org/'), (200, {}, 'not-json', '')]:
            self.c._request = Mock(return_value=response)
            self.assertFalse(self.r.recover_transport())
            self.assertEqual(self.c._request.call_count, 1)
    def test_recovery_event_classifies_failure_without_raw_exception(self):
        self.c._request = Mock(side_effect=LookupFailure('SECRET raw message', transport={
            'phase': 'tcp_connect', 'reason_class': 'TimeoutError', 'errno': 110,
            'raw': 'SECRET'}))
        self.assertFalse(self.r.recover_transport(cycle=2))
        event = self.r.state['_recovery_events'][-1]
        self.assertEqual(event['cycle'], 2)
        self.assertEqual(event['endpoint'], 'system_status')
        self.assertEqual(event['transport_phase'], 'tcp_connect')
        self.assertEqual(event['reason_class'], 'TimeoutError')
        self.assertFalse(event['healthy'])
        report = json.loads(Path('operational-report.json').read_text())
        self.assertNotIn('SECRET', json.dumps(report))
        self.assertEqual(self.r.results, [])
        self.assertEqual(report['transport_cooldown_until'], self.r.state['_transport_cooldown_until'])
    def test_health_events_preserve_http_cooldown_and_success_passes(self):
        self.c._request = Mock(side_effect=lambda u: (429, {'Retry-After': '7200', 'Secret': 'SECRET'}, '{}', u))
        self.assertFalse(self.r.recover_transport(cycle=1))
        event = self.r.state['_recovery_events'][-1]
        self.assertEqual(event['http_status'], 429)
        self.assertGreaterEqual(event['cooldown_until'], self.now + 7200)
        self.assertNotIn('SECRET', json.dumps(event))
        self.r.deadline = self.now + 20000
        self.c._request.side_effect = self.health
        self.assertTrue(self.r.recover_transport(cycle=2))
        events = self.r.state['_recovery_events']
        self.assertEqual(events[-1]['phase'], 'resumed')
        probes = [e for e in events if e.get('cycle') == 2 and e['phase'] == 'probe']
        self.assertEqual(len(probes), 4)
        self.assertEqual([e['pass_number'] for e in probes], [1, 1, 2, 2])
    def test_operational_summary_queue_counts_separate_and_bounded(self):
        self.r.deadline = self.now + 100
        self.r.run_queue(self.urls, 'all')
        self.r.state['_recovery_events'] = [{'phase': 'probe', 'healthy': False, 'message': 'SECRET'}] * 120
        self.r.write_operational_report()
        report = json.loads(Path('operational-report.json').read_text())
        self.assertEqual(len(report['recovery_events']), 100)
        self.assertNotIn('SECRET', json.dumps(report))
        queue = report['queues'][0]
        self.assertEqual(queue['cursor'], 0)
        self.assertEqual(queue['remaining'], len(self.urls))
        self.assertEqual(queue['processed_result_counts'], {})
        self.assertNotIn('urls', queue)
        self.assertEqual(len(self.r.results), len(self.urls))
    def test_state_summary_redacts_invalid_metadata_values(self):
        from capture import operational_summary
        summary = operational_summary({'_next_submit': 'SECRET', '_transport_cooldown_until': 'SECRET',
            '_recovery_events': [{'phase': 'SECRET', 'endpoint': 'SECRET', 'raw': 'SECRET', 'error_class': 'bad SECRET text'}],
            URL: {'job_id': 'SECRET-job', 'review_required': True}})
        self.assertNotIn('SECRET', json.dumps(summary))
        self.assertEqual(summary['pending_counts']['total'], 1)
        self.assertEqual(summary['pending_counts']['review_required'], 1)

    def test_state_summary_malformed_shapes_do_not_leak_or_crash(self):
        from capture import operational_summary
        key = 'all:20261102:capture'
        for state in [None, [], {'_queues': 'SECRET', '_recovery_events': {'raw': 'SECRET'}},
                      {'_queues': {key: {'urls': 'SECRET', 'cursor': {'raw': 'SECRET'}, 'results': [None, 'SECRET', {'state': ['SECRET']}]}}},
                      {'_queues': {key: 'SECRET', 123: None}, '_next_submit': {'raw': 'SECRET'}}]:
            self.assertNotIn('SECRET', json.dumps(operational_summary(state)))

    def test_inspect_state_output_collision_never_overwrites_input(self):
        from capture import main
        source = Path('saved-state.json')
        source.write_text(json.dumps({'_next_submit': self.now}))
        output = Path('operational-report.json')
        output.symlink_to(source)
        before = source.read_bytes()
        for name in ('saved-state.json', 'operational-report.json'):
            with patch('sys.argv', ['capture.py', '--inspect-state', '--state', name]), patch('capture.Client') as client:
                self.assertEqual(main(), 2 if Path(name).is_symlink() else 0)
                client.assert_not_called()
            self.assertEqual(source.read_bytes(), before)
        output.unlink()
        os.link(source, output)
        with patch('sys.argv', ['capture.py', '--inspect-state', '--state', str(source)]):
            self.assertEqual(main(), 0)
        self.assertEqual(source.read_bytes(), before)

    def test_inspect_state_never_initializes_client_or_mutates_state(self):
        from capture import main
        self.r.state[URL] = {'job_id': 'SECRET-job', 'raw_message': 'SECRET'}
        self.r.save()
        before = Path('state.json').read_bytes()
        with patch('sys.argv', ['capture.py', '--inspect-state', '--state', 'state.json']), patch('capture.Client') as client, patch('capture.Runner') as runner:
            self.assertEqual(main(), 0)
            client.assert_not_called(); runner.assert_not_called()
        self.assertEqual(Path('state.json').read_bytes(), before)
        self.assertNotIn('SECRET', Path('operational-report.json').read_text())

    def test_two_recovery_cycles_global_even_when_both_succeed(self):
        urls = self.urls * 1 + [BASE + '/pages/test-' + str(i) for i in range(7, 12)]
        calls = []
        def operation(url):
            calls.append(url)
            if len(calls) % 3 == 0:
                self.r.transport_circuit_open = True
                self.r.state['_transport_cooldown_until'] = self.now + 900
            return self.r.result(url, 'unverified')
        self.r.process = Mock(side_effect=operation)
        self.c._request = Mock(side_effect=self.health)
        self.r.run_queue(urls, 'all')
        queue = next(iter(self.r.state['_queues'].values()))
        self.assertEqual(len(calls), 9)
        self.assertEqual(self.c._request.call_count, 8)
        self.assertEqual(queue['cursor'], 9); self.assertFalse(queue['complete'])
        self.assertEqual([r['url'] for r in self.r.results if r['state'] == 'deferred'], urls[9:])

    def test_operation_deferred_does_not_consume_cursor(self):
        self.r.process = Mock(side_effect=lambda u: self.r.result(u, 'deferred', reason='budget', queue_untouched=True))
        self.r.run_queue(self.urls, 'all')
        queue = next(iter(self.r.state['_queues'].values()))
        self.assertEqual(queue['cursor'], 0)
        self.assertFalse(queue['complete'])
        self.assertEqual(queue['results'], [])
        self.assertEqual(len(self.r.results), len(self.urls))
    def test_per_url_deferral_does_not_block_untouched_pages(self):
        self.r.process = Mock(side_effect=lambda u: self.r.result(u, 'deferred', reason='completed reused capture awaiting eligibility'))
        self.r.run_queue(self.urls, 'all')
        queue = next(iter(self.r.state['_queues'].values()))
        self.assertEqual(queue['cursor'], len(self.urls))
        self.assertTrue(queue['complete'])
        self.assertEqual(self.r.process.call_count, len(self.urls))
        self.assertTrue(all(r['state'] == 'deferred' for r in queue['results']))

    def test_next_day_does_not_reuse_yesterday_cursor(self):
        key = 'all:20261101:capture'
        self.r.state['_queues'] = {key: {'urls': self.urls, 'cursor': 4, 'results': [], 'complete': False}}
        self.r.process = Mock(side_effect=self.operation)
        self.r.run_queue(self.urls, 'all')
        self.assertIn(key, self.r.state['_queues'])
        self.assertEqual(self.r.state['_queues'][key]['cursor'], 4)
        self.assertEqual(self.r.process.call_count, len(self.urls))

    def test_fixed_probe_scope(self):
        from capture import main, TRANSPORT_PROBE
        with patch('sys.argv', ['capture.py', '--scope', 'transport_probe']), patch('capture.Runner.run_queue') as queue, patch.dict(os.environ, {'GITHUB_STEP_SUMMARY': ''}):
            main()
        queue.assert_called_once_with([TRANSPORT_PROBE], 'transport_probe')
        self.assertEqual(TRANSPORT_PROBE, BASE + '/products/date-mod-ice-blue-arabic-watch')

class GlobalArchivePacingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.old = os.getcwd(); os.chdir(self.tmp.name)
        self.now = 1793600000.0; self.calls = []
        self.clock = patch('capture.time.time', side_effect=lambda: self.now); self.clock.start()
        self.sleep = patch('capture.time.sleep', side_effect=self.advance); self.sleep.start()
        seed_test_state()
        seed_test_state('.capture-state/pending.json')
        self.c = Client(); self.r = Runner(self.c, 'state.json', budget=10000)
        self.c._request = Mock(side_effect=self.response)
    def tearDown(self):
        self.sleep.stop(); self.clock.stop(); os.chdir(self.old); self.tmp.cleanup()
    def advance(self, seconds): self.now += seconds
    def response(self, url, data=None, authenticated=False):
        self.calls.append((url, data, self.now)); self.now += 5
        return 200, {}, '{}', url
    def test_every_request_type_uses_completion_floor(self):
        urls = ['https://web.archive.org/cdx/search/cdx?x=1',
                'https://web.archive.org/web/20261102015923id_/' + URL,
                'https://web.archive.org/save/status/job', 'https://web.archive.org/save/']
        for index, url in enumerate(urls):
            self.c.request(url, {'url': URL} if index == 3 else None, index >= 2)
        self.assertEqual(len(self.calls), 4)
        self.assertTrue(all(b[2]-a[2] >= 125 for a,b in zip(self.calls,self.calls[1:])))
        self.assertEqual(self.r.state['_archive_next_request_at'], self.now+120)
    def test_get_retry_and_failed_connection_share_floor(self):
        count=0
        def fail_once(*args):
            nonlocal count
            self.calls.append((args[0], None, self.now)); self.now+=5; count+=1
            if count==1: raise LookupFailure('offline', transport={'phase':'tcp_connect','known_not_sent':True})
            return 200, {}, '{}', args[0]
        self.c._request.side_effect=fail_once
        self.c.request('https://web.archive.org/cdx/search/cdx')
        self.assertEqual(len(self.calls),2)
        self.assertGreaterEqual(self.calls[1][2]-self.calls[0][2],125)
    def test_safe_post_retry_one_only_and_audit_after_wait(self):
        audit=[]; self.c.post_attempt_callback=lambda row:audit.append(row.copy())
        def failure(url,*args):
            self.calls.append((url,None,self.now)); self.now+=5
            raise LookupFailure('offline',transport={'phase':'tcp_connect','known_not_sent':True})
        self.c._request.side_effect=failure
        with self.assertRaises(LookupFailure): self.c.request('https://web.archive.org/save/',{'url':URL},True)
        self.assertEqual(len(self.calls),2)
        self.assertGreaterEqual(self.calls[1][2]-self.calls[0][2],125)
        self.assertEqual([a['started_at'] for a in audit],[x[2] for x in self.calls])
    def test_read_error_persists_floor_no_post_retry(self):
        self.c._request.side_effect=LookupFailure('read failed',transport={'phase':'read','known_not_sent':False})
        with self.assertRaises(LookupFailure):self.c.request('https://web.archive.org/save/',{'url':URL},True)
        self.assertEqual(self.c._request.call_count,1)
        self.assertEqual(json.loads(Path('state.json').read_text())['_archive_next_request_at'],self.now+120)
    def test_any_response_retry_after_extends_persisted_gate(self):
        self.c._request.return_value=(503,{'Retry-After':'900'},'{}','https://web.archive.org/save/')
        self.c._request.side_effect=None
        self.c.request('https://web.archive.org/save/',{'url':URL},True)
        gate=self.now+900
        self.assertEqual(self.r.state['_archive_next_request_at'],gate)
        self.c._request.side_effect=self.response
        self.c.request('https://web.archive.org/cdx/search/cdx')
        self.assertGreaterEqual(self.calls[0][2],gate)
    def test_restarted_runner_does_not_reset_gate(self):
        self.c.request('https://web.archive.org/cdx/search/cdx')
        gate=self.r.state['_archive_next_request_at']
        other=Client(); runner=Runner(other,'state.json',budget=10000)
        other._request=Mock(side_effect=self.response)
        other.request('https://web.archive.org/save/status/job',authenticated=True)
        self.assertGreaterEqual(self.calls[-1][2],gate)
    def test_budget_failure_no_sleep_no_network_and_not_transport_failure(self):
        from capture import RequestDeferred
        self.c.archive_next_request_at=self.now+500
        self.c.archive_deadline=self.now+400
        import capture
        capture.time.sleep.reset_mock()
        with self.assertRaises(RequestDeferred):self.c.request('https://web.archive.org/cdx/search/cdx')
        self.c._request.assert_not_called(); capture.time.sleep.assert_not_called()
        self.assertEqual(self.c.archive_transport_failures,0)
    def test_oversleep_inside_gate_rechecks_budget_before_io_or_audit(self):
        from capture import RequestDeferred
        self.c.archive_next_request_at=self.now+120
        self.c.archive_deadline=self.now+1000
        self.c.post_attempt_callback=Mock()
        with patch('capture.time.sleep',side_effect=lambda n:self.advance(n+1000)):
            with self.assertRaises(RequestDeferred):self.c.request('https://web.archive.org/save/',{'url':URL},True)
        self.c._request.assert_not_called();self.c.post_attempt_callback.assert_not_called()
    def test_storefront_redirect_cannot_enter_archive_outside_gate(self):
        from capture import NoRedirectIntoArchive
        import urllib.request
        handler=NoRedirectIntoArchive();request=urllib.request.Request(BASE+'/old')
        self.assertIsNone(handler.redirect_request(request,None,302,'',{},'https://web.archive.org/save/status/system'))
        self.assertIsNone(handler.redirect_request(request,None,302,'',{},'https://archive.org/wayback/available'))
        redirected=handler.redirect_request(request,None,302,'',{},BASE+'/new')
        self.assertEqual(redirected.full_url,BASE+'/new')

    def test_post_requires_spaced_verification_reserve(self):
        from capture import RequestDeferred
        self.c.archive_deadline=self.now+499
        with self.assertRaises(RequestDeferred):self.c.request('https://web.archive.org/save/',{'url':URL},True)
        self.c._request.assert_not_called()
    def test_new_pending_budget_deferral_not_permanent_unknown(self):
        self.c.verify=Mock(return_value=None)
        self.c.archive_deadline=self.now+300
        row=self.r.process(URL)
        self.assertEqual(row['state'],'deferred');self.assertTrue(row['queue_untouched'])
        self.assertNotIn(URL,self.r.state);self.c._request.assert_not_called()
        self.assertEqual(self.r.state['_completed_jobs'][-1]['state'],'budget_not_submitted')
    def test_after_actual_post_budget_deferral_keeps_job_and_no_duplicate(self):
        self.c.verify=Mock(return_value=None)
        self.c.archive_deadline=self.now+600
        def accepted(url,*args):
            self.calls.append((url,args,self.now));self.now+=480
            return 200,{},'{"job_id":"accepted"}',url
        self.c._request.side_effect=accepted
        row=self.r.process(URL)
        self.assertEqual(row['state'],'pending')
        self.assertEqual(self.r.state[URL]['job_id'],'accepted')
        self.assertEqual(self.c._request.call_count,1)
    def test_historical_unknowns_are_unchanged(self):
        self.c.verify=Mock(return_value=None)
        for i in range(11):self.r.state[BASE+'/old-'+str(i)]={'job_id':None,'submitted_at':self.now-1000}
        before=json.loads(json.dumps(self.r.state))
        for url in before:
            if url.startswith(BASE + '/'):
                self.r.process(url)
        for url in before:self.assertEqual(self.r.state[url],before[url])
        self.c._request.assert_not_called()
    def test_safe_first_post_budget_prevents_retry_and_retains_proof(self):
        self.c.verify=Mock(return_value=None)
        self.c.archive_deadline=self.now+600
        self.c.post_retry_deadline=self.now+600
        self.c._request.side_effect=LookupFailure('refused',transport={'phase':'tcp_connect','known_not_sent':True})
        row=self.r.process(URL)
        self.assertEqual(self.c._request.call_count,1)
        self.assertTrue(self.r.state[URL]['known_not_sent'])
        self.assertEqual(row['state'],'unverified')
    def test_retry_gate_recheck_preserves_proof_after_unexpected_clock_delay(self):
        self.c.verify=Mock(return_value=None)
        self.c.archive_deadline=self.now+1000
        self.c.post_retry_deadline=self.now+1000
        self.c._request.side_effect=LookupFailure('refused',transport={'phase':'tcp_connect','known_not_sent':True})
        def oversleep(seconds):self.now+=seconds+(1000 if seconds>0 else 0)
        with patch('capture.time.sleep',side_effect=oversleep):row=self.r.process(URL)
        self.assertEqual(self.c._request.call_count,1)
        self.assertTrue(self.r.state[URL]['known_not_sent'])
    def test_retry_after_headers_survive_body_read_failure(self):
        from unittest.mock import MagicMock
        response=MagicMock();response.__enter__.return_value=response
        response.status=503;response.headers={'Retry-After':'900'}
        response.read.side_effect=TimeoutError('SECRET read timeout')
        opener=Mock();opener.open.return_value=response
        self.c._request=Client._request.__get__(self.c)
        with patch('capture.ur.build_opener',return_value=opener):
            with self.assertRaises(LookupFailure):self.c.paced_request('https://web.archive.org/cdx/search/cdx')
        self.assertGreaterEqual(self.r.state['_archive_next_request_at'],self.now+900)
        self.assertNotIn('SECRET',Path('state.json').read_text())
    def test_old_saved_host_cooldown_is_respected_on_restart(self):
        self.r.state['_next_submit']=self.now+900;self.r.save()
        other=Client();runner=Runner(other,'state.json',budget=10000)
        other._request=Mock(side_effect=self.response)
        other.request('https://web.archive.org/cdx/search/cdx')
        self.assertGreaterEqual(self.calls[0][2],1793600900)

    def test_known_job_diagnostic_one_get_preserves_pending_review_and_queues(self):
        from capture import PACING_PROBE
        pending={'job_id':'known-job','submitted_at':self.now-300,'review_required':True,
                 'request_evidence':{'attempts':[{'started_at':self.now-60}]}}
        self.r.state[PACING_PROBE]=pending
        self.r.state['_queues']={'all:20261102:capture':{'cursor':5,'urls':[URL]*10,'results':[],'complete':False}}
        queues=json.loads(json.dumps(self.r.state['_queues']))
        obj={'status':'error','status_ext':'error:bad-request','job_id':'known-job',
             'message':'SECRET invalid syntax (HTTP status=401)', 'exception':'TimeoutError SECRET',
             'original_url':'https://example.invalid/SECRET','timestamp':timestamp(self.now-120),
             'resources':[{'secret':'SECRET'}]}
        self.c._request.side_effect=None
        self.c._request.return_value=(200,{},json.dumps(obj),'unused')
        row=self.r.diagnose_saved_pacing_job()
        self.assertEqual(row['state'],'diagnostic-observed')
        self.assertEqual(self.c._request.call_count,1)
        self.c._request.assert_called_once_with('https://web.archive.org/save/status/known-job',None,True)
        self.assertTrue(pending['review_required']);self.assertEqual(pending['job_id'],'known-job')
        self.assertEqual(self.r.state['_queues'],queues)
        detail=pending['job_diagnostic']
        self.assertEqual(detail['message_reported_http_status'],401)
        self.assertEqual(detail['message_category'],'invalid_syntax')
        self.assertEqual(detail['exception_category'],'timeout')
        self.assertTrue(detail['response_job_id_matches']);self.assertFalse(detail['original_url_matches'])
        self.assertFalse(detail['timestamp_fresh']);self.assertEqual(detail['resources_count'],1)
        self.assertNotIn('SECRET',Path('state.json').read_text()+json.dumps(row))
    def test_known_job_diagnostic_does_not_retry_failure_or_guess_missing_id(self):
        from capture import PACING_PROBE
        self.assertEqual(self.r.diagnose_saved_pacing_job()['state'],'diagnostic-unavailable')
        self.c._request.assert_not_called()
        self.r.state[PACING_PROBE]={'job_id':'known-job','submitted_at':self.now,'review_required':True}
        self.c._request.side_effect=LookupFailure('SECRET',transport={'phase':'tcp_connect','reason_class':'TimeoutError','errno':110,'message':'SECRET'})
        self.assertEqual(self.r.diagnose_saved_pacing_job()['state'],'diagnostic-unavailable')
        self.assertEqual(self.c._request.call_count,1)
        self.assertTrue(self.r.state[PACING_PROBE]['review_required'])
        self.assertEqual(self.r.state[PACING_PROBE]['job_diagnostic']['transport']['errno'],110)
        self.assertEqual(self.r.state[PACING_PROBE]['job_diagnostic']['transport']['transport_phase'],'tcp_connect')
        self.assertNotIn('SECRET',Path('state.json').read_text())
    def test_known_job_diagnostic_respects_pending_cooldown(self):
        from capture import PACING_PROBE
        self.r.state[PACING_PROBE]={'job_id':'known-job','submitted_at':self.now,
                                   'status_paused_until':self.now+900,'review_required':True}
        start=self.now;self.r.diagnose_saved_pacing_job()
        self.assertEqual(len(self.calls),1);self.assertGreaterEqual(self.calls[0][2],start+900)
    def test_known_job_diagnostic_budget_does_not_send_or_clear(self):
        from capture import PACING_PROBE
        self.r.state[PACING_PROBE]={'job_id':'known-job','submitted_at':self.now,'review_required':True}
        self.c.archive_deadline=self.now+100
        self.c.archive_next_request_at=self.now+900
        row=self.r.diagnose_saved_pacing_job()
        self.assertEqual(row['state'],'deferred');self.c._request.assert_not_called()
        self.assertTrue(self.r.state[PACING_PROBE]['review_required'])
    def test_actual_last_attempt_freshness_and_legacy_fallback(self):
        from capture import latest_attempt_time,job_observation
        pending={'submitted_at':self.now-300,'job_id':'job',
                 'request_evidence':{'attempts':[{'started_at':self.now-200},{'started_at':self.now-30}]}}
        self.assertEqual(latest_attempt_time(pending),self.now-30)
        obj={'timestamp':timestamp(self.now-100),'original_url':URL,'job_id':'job'}
        self.assertFalse(job_observation(200,{},obj,URL,pending)['timestamp_fresh'])
        self.r.state[URL]=pending
        row=self.r.verified_result(URL,{'timestamp':obj['timestamp']},'existing_index')
        self.assertFalse(row['new_capture'])
        for bad in (None,{'attempts':[{'started_at':True}]},{'attempts':[{'started_at':float('inf')}]}, {'attempts':['invalid']}):
            self.assertEqual(latest_attempt_time({'submitted_at':self.now-300,'request_evidence':bad}),self.now-300)
    def test_message_reported_status_only_explicit_valid_pattern(self):
        from capture import response_metadata
        for message in ('SECRET 401', 'HTTP status=999', 'HTTP status=099','HTTP status=4012'):
            self.assertNotIn('message_reported_http_status',response_metadata(200,{}, {'message':message}))
        self.assertEqual(response_metadata(200,{}, {'message':'HTTP status=503'})['message_reported_http_status'],503)
        self.assertEqual(response_metadata(200,{}, {'message':'HTTP status: 401'})['message_reported_http_status'],401)
    def test_job_diagnostic_scope_bypasses_discovery_queue_and_verify_flags(self):
        from capture import main
        for flags in ([],['--verify-only'],['--poll-only']):
            with patch('sys.argv',['capture.py','--scope','pacing_job_diagnostic']+flags),patch('capture.Runner') as runner,patch('capture.discover') as discover,patch('capture.discover_supplemental') as supplemental:
                runner.return_value.diagnose_saved_pacing_job.return_value={'state':'diagnostic-observed'}
                self.assertEqual(main(),0)
                self.assertEqual(runner.call_args.args[2],1800)
                runner.return_value.diagnose_saved_pacing_job.assert_called_once_with()
                runner.return_value.run_queue.assert_not_called();discover.assert_not_called();supplemental.assert_not_called()

    def test_account_diagnostic_one_authenticated_get_preserves_jobs_and_queues(self):
        self.r.state[URL]={'job_id':'saved-job','submitted_at':self.now-900,'review_required':True}
        self.r.state['_queues']={'all:20261102:capture':{'urls':[URL]*5,'cursor':5,'results':[],'complete':False}}
        before=json.loads(json.dumps(self.r.state))
        self.c._request.side_effect=None
        self.c._request.return_value=(200,{'Age':'15','Date':'Mon, 02 Nov 2026 15:00:00 GMT'},
            json.dumps({'available':12,'processing':3,'daily_captures':11,'daily_captures_limit':100,
                        'email':'SECRET@example.invalid','user':'SECRET','message':'SECRET'}),'unused')
        row=self.r.diagnose_account()
        self.c._request.assert_called_once_with('https://web.archive.org/save/status/user',None,True)
        self.assertEqual(row['state'],'diagnostic-observed')
        data=row['account_observation']
        self.assertEqual(data['available'],12);self.assertEqual(data['processing'],3)
        self.assertEqual(data['response_age_seconds'],15)
        self.assertEqual(data['response_date'],'Mon, 02 Nov 2026 15:00:00 GMT')
        self.assertEqual(data['cache_freshness'],'reported_headers_only')
        self.assertTrue(data['schema_valid'])
        self.assertEqual(self.r.state[URL],before[URL]);self.assertEqual(self.r.state['_queues'],before['_queues'])
        self.assertNotIn('SECRET',Path('state.json').read_text()+json.dumps(row))
    def test_account_schema_rejects_bool_negative_noninteger_unknown_quota(self):
        from capture import account_observation
        data=account_observation(200,{}, {'available':12,'processing':3})
        self.assertTrue(data['schema_valid']);self.assertEqual(data['cache_freshness'],'unknown');self.assertNotIn('daily_captures',data)
        self.assertNotIn('daily_captures_limit',data)
        for bad in (True,False,-1,1.5,'12',None):
            data=account_observation(200,{}, {'available':bad,'processing':3,'daily_captures_limit':bad})
            self.assertFalse(data['schema_valid'])
            self.assertNotIn('available',data);self.assertNotIn('daily_captures_limit',data)
    def test_account_headers_and_secret_fields_are_strictly_redacted(self):
        from capture import account_observation
        for body in (None,[],{'available':12,'processing':True,'id':'SECRET','token':'SECRET'}):
            data=account_observation(200,{'age':'SECRET','date':'SECRET'},body)
            self.assertFalse(data['schema_valid']);self.assertNotIn('response_age_seconds',data)
            self.assertNotIn('response_date',data);self.assertNotIn('SECRET',json.dumps(data))
        for age in ('-1','1.5','true','9999999999999999999999999999'):
            self.assertNotIn('response_age_seconds',account_observation(200,{'age':age},{}))
    def test_account_error_response_observed_once_with_cooldown_no_retry(self):
        self.c._request.side_effect=None
        self.c._request.return_value=(503,{'Retry-After':'900'},'{"message":"SECRET","available":12,"processing":3}','unused')
        row=self.r.diagnose_account()
        self.assertEqual(row['state'],'diagnostic-unavailable');self.assertEqual(self.c._request.call_count,1)
        self.assertGreaterEqual(self.r.state['_archive_next_request_at'],self.now+900)
        self.assertNotIn('SECRET',json.dumps(row))
    def test_account_transport_failure_single_call_safe_metadata(self):
        self.c._request.side_effect=LookupFailure('SECRET',transport={'phase':'read','reason_class':'TimeoutError','errno':110})
        row=self.r.diagnose_account()
        self.assertEqual(self.c._request.call_count,1);self.assertEqual(row['state'],'diagnostic-unavailable')
        self.assertEqual(row['account_observation']['transport']['errno'],110)
        self.assertNotIn('SECRET',json.dumps(row))
    def test_account_diagnostic_budget_zero_network(self):
        self.c.archive_next_request_at=self.now+900;self.c.archive_deadline=self.now+300
        self.assertEqual(self.r.diagnose_account()['state'],'deferred');self.c._request.assert_not_called()
    def test_account_scope_early_return_all_flags_no_inventory_queue_or_submission(self):
        from capture import main
        for flags in ([],['--verify-only'],['--poll-only']):
            with patch('sys.argv',['capture.py','--scope','account_diagnostic']+flags),patch('capture.Runner') as runner,patch('capture.discover') as discover,patch('capture.discover_supplemental') as supplemental:
                runner.return_value.diagnose_account.return_value={'state':'diagnostic-observed'}
                self.assertEqual(main(),0);self.assertEqual(runner.call_args.args[2],1800)
                runner.return_value.diagnose_account.assert_called_once_with()
                runner.return_value.run_queue.assert_not_called();runner.return_value.process.assert_not_called()
                discover.assert_not_called();supplemental.assert_not_called()

    def test_supplemental_probe_always_forces_gate_and_only_about_us(self):
        from capture import SUPPLEMENTAL_PROBE
        self.r.recover_transport=Mock(return_value=True)
        self.r.run_queue=Mock()
        self.r.run_supplemental_probe()
        self.r.recover_transport.assert_called_once_with(cycle=1)
        self.r.run_queue.assert_called_once_with([SUPPLEMENTAL_PROBE],'supplemental_probe')
        self.assertEqual(SUPPLEMENTAL_PROBE,BASE+'/pages/about-us')
    def test_supplemental_probe_failed_gate_never_checks_cdx_or_posts(self):
        self.r.recover_transport=Mock(return_value=False)
        self.c.verify=Mock()
        row=self.r.run_supplemental_probe()
        self.assertEqual(row['state'],'deferred')
        self.c.verify.assert_not_called();self.c._request.assert_not_called()
        self.assertNotIn('_queues',self.r.state)
    def test_supplemental_probe_any_saved_entry_is_untouched(self):
        from capture import SUPPLEMENTAL_PROBE
        self.r.recover_transport=Mock()
        for pending in (None,{}, {'job_id':'existing','review_required':True},
                        {'known_not_sent':True,'job_id':None},
                        {'terminal_reused':{'timestamp':'20261101000000'},'retry_eligible_at':0}):
            self.r.state[SUPPLEMENTAL_PROBE]=pending
            before=json.loads(json.dumps(self.r.state))
            self.assertEqual(self.r.run_supplemental_probe()['state'],'review_required')
            self.assertEqual(self.r.state,before)
        self.r.recover_transport.assert_not_called();self.c._request.assert_not_called()
    def test_supplemental_probe_completed_audit_also_prevents_repost(self):
        from capture import SUPPLEMENTAL_PROBE
        self.r.state['_completed_jobs']=[{'url':SUPPLEMENTAL_PROBE,'state':'failed'}]
        self.r.recover_transport=Mock()
        self.assertEqual(self.r.run_supplemental_probe()['state'],'review_required')
        self.r.recover_transport.assert_not_called();self.c._request.assert_not_called()
    def test_supplemental_probe_rechecks_protected_state_after_gate(self):
        from capture import SUPPLEMENTAL_PROBE
        def gate(**kwargs):
            self.r.state[SUPPLEMENTAL_PROBE]={'known_not_sent':True};return True
        self.r.recover_transport=Mock(side_effect=gate)
        self.assertEqual(self.r.run_supplemental_probe()['state'],'review_required')
        self.c._request.assert_not_called()
        self.assertEqual(self.r.state[SUPPLEMENTAL_PROBE],{'known_not_sent':True})
    def test_supplemental_probe_fresh_cdx_skips_post_and_preserves_other_queues(self):
        from capture import SUPPLEMENTAL_PROBE
        self.r.recover_transport=Mock(return_value=True)
        self.c.verify=Mock(return_value={'timestamp':timestamp(self.now-60),'replay':'verified'})
        queues={'all:20261101:capture':{'urls':[URL]*5,'cursor':5,'results':[],'complete':False},
                'pacing_probe:20261102:capture':{'urls':[URL],'cursor':1,'results':[],'complete':True}}
        self.r.state['_queues']=json.loads(json.dumps(queues))
        self.assertEqual(self.r.run_supplemental_probe()['state'],'verified')
        self.c.verify.assert_called_once_with(SUPPLEMENTAL_PROBE,self.r.since,until=self.r.until)
        self.c._request.assert_not_called()
        for key in queues:self.assertEqual(self.r.state['_queues'][key],queues[key])
        own=[q for key,q in self.r.state['_queues'].items() if key.startswith('supplemental_probe:')]
        self.assertEqual(len(own),1);self.assertEqual(own[0]['urls'],[SUPPLEMENTAL_PROBE])
    def test_supplemental_probe_rejects_saved_queue_with_other_targets(self):
        from capture import SUPPLEMENTAL_PROBE
        self.r.recover_transport=Mock()
        key='supplemental_probe:'+timestamp(self.r.since)[:8]+':capture'
        for urls in ([URL],[SUPPLEMENTAL_PROBE,URL],[],None):
            queue={'urls':urls,'cursor':0,'results':[],'complete':False}
            self.r.state['_queues']={key:queue}
            before=json.loads(json.dumps(queue))
            self.assertEqual(self.r.run_supplemental_probe()['state'],'review_required')
            self.assertEqual(self.r.state['_queues'][key],before)
        self.r.recover_transport.assert_not_called();self.c._request.assert_not_called()

    def test_supplemental_probe_main_budget_no_inventory_or_broad_queue(self):
        from capture import main
        with patch('sys.argv',['capture.py','--scope','supplemental_probe']),patch('capture.Runner') as runner,patch('capture.discover') as discover,patch('capture.discover_supplemental') as supplemental,patch.dict(os.environ,{'GITHUB_STEP_SUMMARY':''}):
            runner.return_value.results=[]
            main()
            self.assertEqual(runner.call_args.args[2],1800)
            runner.return_value.run_supplemental_probe.assert_called_once_with()
            runner.return_value.run_queue.assert_not_called();discover.assert_not_called();supplemental.assert_not_called()

    def test_health_requests_obey_same_gate(self):
        def health(url,*args):
            self.calls.append((url,None,self.now));self.now+=5
            return 200,{},'{}' if '/save/status/' in url else '[]',url
        self.c._request.side_effect=health
        self.assertTrue(self.r.recover_transport(cycle=1))
        self.assertEqual(len(self.calls),4)
        self.assertTrue(all(b[2]-a[2]>=125 for a,b in zip(self.calls,self.calls[1:])))
    def test_pacing_save_failure_prevents_actual_io(self):
        self.c.archive_pacing_callback=Mock(side_effect=OSError('save failed'))
        with self.assertRaises(OSError):self.c.request('https://web.archive.org/cdx/search/cdx')
        self.c._request.assert_not_called()
    def test_storefront_requests_not_delayed_by_archive_gate(self):
        self.c.archive_next_request_at=self.now+1000
        start=self.now;self.c.request(BASE+'/collections/all')
        self.assertEqual(self.calls[0][2],start)
    def test_new_canary_scope_is_fixed_and_budget_capped(self):
        from capture import main,PACING_PROBE
        with patch('sys.argv',['capture.py','--scope','pacing_probe']),patch('capture.Runner') as runner,patch.dict(os.environ,{'GITHUB_STEP_SUMMARY':''}):
            runner.return_value.results=[]
            main()
        self.assertEqual(runner.call_args.args[2],1800)
        runner.return_value.run_queue.assert_called_once_with([PACING_PROBE],'pacing_probe')
        self.assertEqual(PACING_PROBE,BASE+'/products/date-mod-olive-green-watch')

class OutlinkBatchTests(unittest.TestCase):
    def setUp(self):
        from capture import load_outlink_inventory,OUTLINK_PROBE
        self.inventory,self.digest=load_outlink_inventory();self.root=OUTLINK_PROBE
        self.product=self.inventory['product_urls'][0];self.second=self.inventory['product_urls'][1]
        self.tmp=tempfile.TemporaryDirectory();self.old=os.getcwd();os.chdir(self.tmp.name)
        seed_test_state()
        seed_test_state('.capture-state/pending.json')
        self.c=Mock(spec=Client);self.c.post_attempt_callback=None;self.c.verify.return_value=None
        self.r=Runner(self.c,'state.json',budget=1800);self.r.recover_transport=Mock(return_value=True)
        self.post={'job_id':'new-root','status':'pending'}
        self.status={'job_id':'new-root','status':'success','outlinks':{self.product:'child-job'}}
        self.physical_posts=0;self.calls=[]
        self.c.paced_request.side_effect=self.request
    def tearDown(self):os.chdir(self.old);self.tmp.cleanup()
    def request(self,url,data=None,authenticated=False):
        self.calls.append((url,data,authenticated))
        if data is not None:
            saved=json.loads(Path('state.json').read_text())
            assert self.root in saved['_outlink_batches']
            self.physical_posts+=1
            self.c.post_attempt_callback({'attempt':1,'started_at':time.time()})
            return 200,{},json.dumps(self.post),url
        return 200,{},json.dumps(self.status),url
    def batch(self):return self.r.state['_outlink_batches'][self.root]
    def evidence(self):return {'timestamp':timestamp(time.time()),'replay':'verified-exact-replay'}
    def test_exact_one_root_post_fixed_options_and_journal_before_io(self):
        row=self.r.run_outlink_probe()
        self.assertEqual(self.physical_posts,1)
        self.r.recover_transport.assert_called_once_with(cycle=1)
        post=[call for call in self.calls if call[1] is not None][0]
        self.assertEqual(post,('https://web.archive.org/save/',{'url':self.root,'capture_outlinks':'1','if_not_archived_within':'1,1d'},True))
        self.assertEqual(row['state'],'outlinks-partial');self.assertEqual(row['verified_products'],0)
        self.assertEqual(self.batch()['root_job_id'],'new-root')
    def test_direct_pending_root_and_children_never_overwritten_or_cleared(self):
        urls=[self.root,self.product]+[BASE+'/old-'+str(i) for i in range(19)]
        for u in urls:self.r.state[u]={'job_id':'legacy','submitted_at':time.time()-300,'review_required':True}
        before=json.loads(json.dumps(self.r.state));self.r.state['_queues']={'all:20261102:capture':{'urls':[URL]*5,'cursor':5,'results':[],'complete':False}}
        self.c.verify.return_value=self.evidence()
        row=self.r.run_outlink_probe()
        for u in urls:self.assertEqual(self.r.state[u],before[u])
        self.assertEqual(self.r.state['_queues'],{'all:20261102:capture':{'urls':[URL]*5,'cursor':5,'results':[],'complete':False}})
        self.assertTrue(self.batch()['direct_pending_reference'])
        self.assertTrue(next(iter(self.batch()['children'].values()))['direct_pending_reference'])
        self.assertNotIn('job_id',row) # Do not mislabel legacy direct ID as new batch job.
        self.assertEqual(self.physical_posts,1)
    def test_mapping_retained_from_submission_pending_and_status_error(self):
        self.post['outlinks']={self.product:'from-pending'}
        self.status={'status':'error','job_id':'new-root','outlinks':{self.second:'from-error'},'message':'SECRET'}
        self.r.run_outlink_probe()
        ids={job for child in self.batch()['children'].values() for job in child['job_ids']}
        self.assertEqual(ids,{'from-pending','from-error'})
        self.assertEqual(self.batch()['expansion_state'],'unknown')
        self.assertNotIn('SECRET',Path('state.json').read_text())
    def test_external_userinfo_query_and_unknown_path_redacted(self):
        self.status['outlinks']['https://user:SECRET@other.example/SECRET?token=SECRET']='external-job'
        self.r.run_outlink_probe()
        external=[c for c in self.batch()['children'].values() if c.get('hostname')=='other.example'][0]
        self.assertNotIn('url',external);self.assertEqual(external['job_ids'],['external-job'])
        self.assertEqual(external['coverage'],'unverifiable_redacted_url')
        self.assertNotIn('SECRET',Path('state.json').read_text())
        self.assertTrue(all(call.args[0].startswith(BASE) for call in self.c.verify.call_args_list))
    def test_missing_mapping_is_unknown_not_zero_and_holds_direct_posts(self):
        self.status={'status':'error','job_id':'new-root'}
        row=self.r.run_outlink_probe()
        self.assertEqual(self.batch()['expansion_state'],'unknown')
        self.assertEqual(row['verified_products'],0)
        held=self.r.process(self.second)
        self.assertEqual(held['state'],'outlink-held');self.assertEqual(self.physical_posts,1)
        self.c.json.assert_not_called()
    def test_unknown_expansion_allows_existing_job_read_only_reconciliation(self):
        self.r.state['_outlink_batches'] = {self.root: {
            'root_url':self.root, 'children':{}, 'child_order':[], 'verification_cursor':0, 'utc_day':self.r.since,
            'post_state': 'outcome_unknown', 'expansion_state': 'unknown'}}
        self.r.state[self.second] = {'job_id': 'saved-child', 'submitted_at': time.time()-300}
        self.c.json.return_value = (404, {}, {})
        row = self.r.process(self.second)
        self.assertEqual(row['state'], 'review_required')
        self.c.json.assert_called_once_with(
            'https://web.archive.org/save/status/saved-child', authenticated=True)
        self.c.paced_request.assert_not_called()
        self.assertEqual(self.r.state[self.second]['job_id'], 'saved-child')
        self.assertEqual(self.r.state['_outlink_batches'][self.root]['post_state'], 'outcome_unknown')

    def test_unknown_expansion_verify_only_does_not_report_submission_hold(self):
        self.r.state['_outlink_batches'] = {self.root: {
            'root_url':self.root, 'children':{}, 'child_order':[], 'verification_cursor':0, 'utc_day':self.r.since,
            'post_state': 'outcome_unknown', 'expansion_state': 'unknown'}}
        self.r.verify_only = True
        self.assertEqual(self.r.process(self.second)['state'], 'unverified')
        self.c.json.assert_not_called()
        self.c.paced_request.assert_not_called()

    def test_known_unverified_child_holds_direct_post_but_verified_child_resolves(self):
        self.r.run_outlink_probe()
        self.assertIn('unresolved outlink child',self.r.outlink_submission_hold(self.product))
        child=next(iter(self.batch()['children'].values()));child['coverage']='verified';child['evidence']=self.evidence()
        self.assertIsNone(self.r.outlink_submission_hold(self.product))
    def test_transport_failure_never_retries_and_existing_batch_never_posts(self):
        self.c.paced_request.side_effect=LookupFailure('SECRET',transport={'known_not_sent':False})
        self.r.run_outlink_probe();self.assertEqual(self.c.paced_request.call_count,1)
        self.assertEqual(self.batch()['post_state'],'outcome_unknown')
        self.r.run_outlink_probe();self.assertEqual(self.c.paced_request.call_count,1)
        self.assertNotIn('SECRET',Path('state.json').read_text())
    def test_proven_not_sent_does_not_retry_or_create_global_unknown_hold(self):
        self.c.paced_request.side_effect=LookupFailure('offline',transport={'known_not_sent':True})
        self.r.run_outlink_probe();self.r.run_outlink_probe()
        self.assertEqual(self.c.paced_request.call_count,1)
        self.assertEqual(self.batch()['post_state'],'not_sent_proven')
        self.assertIsNone(self.r.outlink_submission_hold(self.product))
    def test_invalid_existing_journal_never_dispatches(self):
        for journals in (None,[],{self.root:None},{self.root:{}},{self.root:{'inventory_sha256':self.digest}}):
            self.r.state['_outlink_batches']=journals
            row=self.r.run_outlink_probe();self.assertEqual(row['state'],'review_required')
        self.r.recover_transport.assert_not_called();self.c.paced_request.assert_not_called()
    def test_mismatched_root_id_retains_mapping_but_not_expansion_claim(self):
        self.status['job_id']='unexpected-root'
        self.r.run_outlink_probe()
        self.assertEqual(self.batch()['root_job_id'],'new-root')
        self.assertEqual(self.batch()['root_job_ids'],['new-root','unexpected-root'])
        self.assertEqual(self.batch()['expansion_state'],'unknown')
        self.assertTrue(self.batch()['children'])
    def test_fresh_root_evidence_does_not_imply_expansion_or_child_coverage(self):
        self.c.verify.side_effect=lambda url,*args,**kwargs:self.evidence() if url==self.root else None
        row=self.r.run_outlink_probe()
        self.assertEqual(self.physical_posts,1);self.assertTrue(row['root_verified'])
        self.assertEqual(row['verified_products'],0);self.assertEqual(row['state'],'outlinks-partial')
        self.assertTrue(self.batch()['root_evidence_before'])
    def test_budget_interruption_preserves_child_cursor_and_readonly_resume(self):
        from capture import RequestDeferred
        self.status['outlinks'][self.second]='second-child';allow=False
        def verify(url,*args,**kwargs):
            if url==self.second and not allow:raise RequestDeferred('budget')
            return self.evidence() if url in (self.product,self.second) else None
        self.c.verify.side_effect=verify
        self.r.run_outlink_probe();self.assertEqual(self.batch()['verification_cursor'],1)
        allow=True;self.r.run_outlink_probe()
        self.assertEqual(self.physical_posts,1);self.assertEqual(self.batch()['verification_cursor'],2)
        self.assertTrue(all(c['coverage']=='verified' for c in self.batch()['children'].values()))
    def test_readonly_modes_without_batch_never_healthcheck_or_submit(self):
        for mode in ('verify_only','poll_only'):
            setattr(self.r,mode,True);self.r.run_outlink_probe();setattr(self.r,mode,False)
        self.r.recover_transport.assert_not_called();self.c.paced_request.assert_not_called()
    def test_gate_or_preverification_failure_prevents_journal_post(self):
        self.r.recover_transport.return_value=False
        self.r.run_outlink_probe();self.c.verify.assert_not_called();self.c.paced_request.assert_not_called()
        self.r.recover_transport.return_value=True;self.c.verify.side_effect=LookupFailure('offline')
        self.r.run_outlink_probe();self.c.paced_request.assert_not_called()
        self.assertNotIn(self.root,self.r.state['_outlink_batches'])
    def test_inventory_roots_are_only_shop_all_and_products_are_frozen(self):
        self.assertEqual(self.inventory['root_urls'],[self.root]+[self.root+'?page='+str(n) for n in range(2,7)])
        self.assertEqual(len(self.inventory['product_urls']),154)
        self.assertNotIn(BASE+'/pages/about-us',self.inventory['root_urls'])
        self.assertNotIn(BASE+'/policies/legal-notice',self.inventory['root_urls'])
    def test_success_shaped_http_error_never_promotes_unknown_expansion(self):
        self.status['status']='pending'
        self.r.run_outlink_probe()
        self.status['status']='success'
        batch=self.batch()
        self.r.record_outlink_response(batch,503,{},self.status,{self.product},'root_status')
        self.assertEqual(batch['expansion_state'],'unknown')
        self.assertTrue(batch['children']);self.assertIsNotNone(self.r.outlink_submission_hold(self.second))
    def test_redacted_same_site_alias_child_holds_site_but_external_does_not(self):
        self.status['outlinks']={'http://www.somervillewatchco.com/products/unknown?variant=SECRET':'alias-job'}
        self.r.run_outlink_probe()
        self.assertEqual(self.batch()['expansion_state'],'mapping_reported')
        self.assertIn('redacted same-site',self.r.outlink_submission_hold(self.second))
        self.assertNotIn('SECRET',Path('state.json').read_text())
        child=next(iter(self.batch()['children'].values()));child['hostname']='other.example'
        self.assertIsNone(self.r.outlink_submission_hold(self.second))
    def test_host_pause_response_preserves_mappings_and_stops(self):
        self.post={'job_id':'new-root','status':'error','status_ext':'error:host-crawling-paused',
                   'outlinks':{self.product:'child-job'}}
        row=self.r.run_outlink_probe()
        self.assertEqual(self.physical_posts,1);self.assertEqual(len(self.calls),1)
        self.assertEqual(self.batch()['root_job_id'],'new-root');self.assertTrue(self.batch()['children'])
        self.assertGreater(self.r.state['_next_submit'],time.time()+650)
        self.assertEqual(row['state'],'outlinks-partial')

    def test_nonjson_429_stops_with_host_cooldown_without_more_requests(self):
        def refused(url,data=None,authenticated=False):
            self.assertIsNotNone(data)
            self.physical_posts+=1
            self.c.post_attempt_callback({'attempt':1,'started_at':time.time()})
            return 429,{},'SECRET html refusal',url
        self.c.paced_request.side_effect=refused
        row=self.r.run_outlink_probe()
        self.assertEqual(self.physical_posts,1);self.assertEqual(self.c.paced_request.call_count,1)
        self.assertEqual(self.c.verify.call_count,1) # Only pre-POST evidence check.
        self.assertGreater(self.r.state['_next_submit'],time.time()+650)
        self.assertEqual(self.batch()['expansion_state'],'unknown')
        self.assertEqual(row['state'],'outlinks-partial')
        self.assertNotIn('SECRET',Path('state.json').read_text())

    def test_page_two_is_single_explicit_root_and_preserves_page_one(self):
        self.r.run_outlink_probe()
        original=json.loads(json.dumps(self.batch()))
        self.root=self.inventory['root_urls'][1]
        self.r.run_outlink_probe(2)
        self.assertEqual(self.physical_posts,2)
        self.assertEqual(self.r.state['_outlink_batches'][self.inventory['root_urls'][0]],original)
        self.assertEqual(self.batch()['root_url'],self.root)
        self.assertEqual([c[1]['url'] for c in self.calls if c[1]],self.inventory['root_urls'][:2])
        self.assertEqual(self.r.recover_transport.call_count,2)
        self.r.run_outlink_probe(2)
        self.assertEqual(self.physical_posts,2)
        self.assertEqual(self.r.recover_transport.call_count,2)

    def test_invalid_page_and_readonly_new_page_never_dispatch(self):
        for page in (0,7,True,None,'2','?page=2'):
            self.assertEqual(self.r.run_outlink_probe(page)['state'],'review_required')
        self.r.verify_only=True
        self.r.run_outlink_probe(2)
        self.r.recover_transport.assert_not_called();self.c.paced_request.assert_not_called()

    def test_terminal_pause_resumes_index_only_without_renewal_or_post(self):
        self.status={'job_id':'new-root','status':'error','status_ext':'error:host-crawling-paused'}
        self.r.run_outlink_probe()
        batch=self.batch();deadline=self.r.state['_next_submit']
        self.assertTrue(batch['terminal_paused_status'])
        self.c.paced_request.reset_mock();self.c.verify.reset_mock()
        self.r.run_outlink_probe()
        self.c.paced_request.assert_not_called()
        self.c.verify.assert_called_once_with(self.root,batch['utc_day'],until=batch['utc_day']+86400-1)
        self.assertEqual(self.r.state['_next_submit'],deadline)
        self.assertEqual(self.physical_posts,1)
        self.assertIsNotNone(self.r.outlink_submission_hold(self.product))
        self.assertEqual(batch['root_job_id'],'new-root')

    def test_legacy_terminal_pause_migrates_without_missing_raw_cause(self):
        self.status={'job_id':'new-root','status':'error','status_ext':'error:host-crawling-paused'}
        self.r.run_outlink_probe();batch=self.batch();batch.pop('terminal_paused_status')
        last=batch['responses'][-1];last['status_ext']='unknown';last.pop('pause_triggers',None)
        deadline=self.r.state['_next_submit'];self.c.paced_request.reset_mock()
        self.r.run_outlink_probe()
        self.c.paced_request.assert_not_called();self.assertTrue(batch['terminal_paused_status'])
        self.assertEqual(self.r.state['_next_submit'],deadline)

    def test_mismatched_or_http429_observation_does_not_mark_terminal(self):
        self.r.run_outlink_probe();batch=self.batch()
        for code,job in ((429,'new-root'),(200,'other-root')):
            self.r.record_outlink_response(batch,code,{},
                {'job_id':job,'status':'error','status_ext':'error:host-crawling-paused'}, {self.product},'root_status')
            self.assertNotIn('terminal_paused_status',batch)
            self.assertGreater(self.r.state['_next_submit'],time.time()+650)

    def test_explicit_page_cli_and_workflow_choice(self):
        from capture import main
        with patch('sys.argv',['capture.py','--scope','outlink_probe','--outlink-page','2']),patch('capture.Runner') as runner:
            runner.return_value.run_outlink_probe.return_value={'state':'outlinks-partial'}
            self.assertEqual(main(),1)
            runner.return_value.run_outlink_probe.assert_called_once_with(2)
            runner.return_value.run_queue.assert_not_called()
        workflow=(Path(__file__).resolve().parent/'.github/workflows/wayback-snapshot.yml').read_text()
        self.assertIn("options: ['1', '2', '3', '4', '5', '6']",workflow)
        self.assertIn('--outlink-page "$OUTLINK_PAGE"',workflow)

    def test_scope_only_fixed_probe_budget_and_no_discovery_or_broad_queue(self):
        from capture import main
        with patch('sys.argv',['capture.py','--scope','outlink_probe']),patch('capture.Runner') as runner,patch('capture.discover') as discover,patch('capture.discover_supplemental') as supplemental:
            runner.return_value.run_outlink_probe.return_value={'state':'outlinks-partial'}
            self.assertEqual(main(),1);self.assertEqual(runner.call_args.args[2],1800)
            runner.return_value.run_outlink_probe.assert_called_once_with(1)
            runner.return_value.run_queue.assert_not_called();discover.assert_not_called();supplemental.assert_not_called()

class WorkflowTests(unittest.TestCase):
    def test_operational_artifact_is_explicit_and_tests_are_isolated(self):
        workflow = (Path(__file__).resolve().parent / '.github/workflows/wayback-snapshot.yml').read_text()
        self.assertIn('test_dir=$(mktemp -d)', workflow)
        self.assertIn('cd "$test_dir"', workflow)
        self.assertIn('PYTHONPATH="$GITHUB_WORKSPACE"', workflow)
        self.assertIn('path: |\n            capture-report.json\n            operational-report.json', workflow)
        self.assertNotIn('path: .capture-state\n          if-no-files-found', workflow)

    def test_recovery_cache_is_not_promoted_to_production(self):
        workflow = (Path(__file__).resolve().parent / '.github/workflows/wayback-snapshot.yml').read_text()
        self.assertIn("key: ${{ inputs.recovery_cache_key && 'wayback-recovery-v1' || 'wayback-state-v1' }}", workflow)
        self.assertIn("restore-keys: ${{ !inputs.recovery_cache_key && 'wayback-state-v1-' || '' }}", workflow)
        self.assertIn('args+=(--poll-only)', workflow)

class PauseMetadataTests(unittest.TestCase):
    def test_extended_error_field_keeps_safe_cause_without_raw_text(self):
        from capture import response_metadata
        detail = response_metadata(200, {}, {'status': 'error', 'status_ext':
            'SECRET crawling this host is paused; HTTP status=429; overloaded'})
        self.assertEqual(detail['http_status'], 200)
        self.assertEqual(detail['status_ext'], 'unknown')
        self.assertEqual(detail['message_category'], 'host_paused')
        self.assertEqual(detail['provider_reported_http_statuses'], [429])
        self.assertEqual(detail['pause_reported_in'], ['status_ext'])
        self.assertNotIn('SECRET', json.dumps(detail))

    def test_provider_status_conflicts_are_visible_not_silently_selected(self):
        from capture import response_metadata
        detail = response_metadata(200, {}, {'status_ext': 'HTTP status=503',
            'message': 'HTTP status=429; HTTP status=429'})
        self.assertEqual(detail['provider_reported_http_statuses'], [429, 503])
        self.assertEqual(detail['pause_reported_in'], ['message'])
        malformed = response_metadata(200, {}, {'status_ext': ['SECRET HTTP status=429'],
            'message': {'private': 'SECRET HTTP status=503'}})
        self.assertNotIn('provider_reported_http_statuses', malformed)
        self.assertEqual(malformed['pause_reported_in'], [])
        self.assertNotIn('SECRET', json.dumps(malformed))

    def test_each_existing_pause_predicate_has_safe_distinct_observation(self):
        from capture import response_metadata
        cases = [
            (429, {}, ['http_429']),
            (200, {'status_ext': 'error:host-crawling-paused'}, ['host_crawling_paused']),
            (200, {'message': 'Crawling this host is paused'}, ['host_pause_message']),
            (200, {'message': 'HTTP status=429'}, ['reported_http_429']),
            (200, {'message': 'HTTP status: 429'}, ['reported_http_429']),
            (200, {'status_ext': 'HTTP status=429'}, ['reported_http_429']),
            (200, {'status_ext': 'crawling this host', 'message': 'is paused'}, ['host_pause_message']),
        ]
        for code, obj, expected in cases:
            with self.subTest(code=code, obj=obj):
                result = response_metadata(code, {}, obj)
                self.assertEqual(result['pause_triggers'], expected)
        self.assertEqual(response_metadata(200, {}, cases[1][1])['status_ext'], 'error:host-crawling-paused')

    def test_raw_unknown_and_malformed_fields_are_never_retained(self):
        from capture import response_metadata
        for obj in (None, [], 'SECRET', {'status_ext': ['SECRET'], 'message': {'secret': 'SECRET'}},
                    {'status_ext': 'error:host-crawling-paused SECRET', 'message': 'SECRET HTTP status=429'}):
            result = response_metadata(200, {}, obj)
            self.assertNotIn('SECRET', json.dumps(result))
            self.assertEqual(result['status_ext'], 'unknown')
        self.assertEqual(response_metadata(200, {}, {'message': 'unknown SECRET'})['pause_triggers'], [])

    def test_response_cache_headers_are_safe_observations_not_freshness_proof(self):
        from capture import response_metadata, account_observation
        headers = {'age': '120', 'date': 'Mon, 02 Nov 2026 17:25:00 GMT'}
        result = response_metadata(200, headers, {})
        self.assertEqual(result['response_age_seconds'], 120)
        self.assertEqual(result['response_date'], headers['date'])
        self.assertEqual(result['cache_freshness'], 'reported_headers_only')
        account = account_observation(200, headers, {})
        for key in ('response_age_seconds', 'response_date', 'cache_freshness'):
            self.assertEqual(result[key], account[key])
        for bad_headers in ({}, {'date': headers['date']}, {'age': True, 'date': 'SECRET'},
                            {'age': '-1', 'date': ['SECRET']}, {'age': 'SECRET', 'date': 'SECRET'}):
            result = response_metadata(200, bad_headers, {})
            self.assertEqual(result['cache_freshness'], 'unknown')
            self.assertNotIn('response_age_seconds', result)
            self.assertNotIn('SECRET', json.dumps(result))

    def test_observation_tracks_pause_predicate(self):
        from capture import response_metadata
        import re
        cases = [{'message': 'HTTP status=4299'}, {'status_ext': ['host-crawling-paused']},
                 {'message': 'crawling this host is paused SECRET'}, {}, {'message': None}]
        for obj in cases:
            detail = (str(obj.get('status_ext', '')) + ' ' + str(obj.get('message', ''))).lower()
            expected = ('host-crawling-paused' in detail or 'crawling this host is paused' in detail
                        or 'overloaded' in detail or bool(re.search(r'http status\s*[=:]\s*429\b', detail)))
            self.assertEqual(bool(response_metadata(200, {}, obj)['pause_triggers']), expected)


class DailyPolicyEvidenceTests(unittest.TestCase):
    """Only synthetic CDX/replay responses; never invoke a live endpoint."""
    product = BASE + '/products/ice-blue-watch'
    name = 'Ice Blue Watch'
    now = 1793793600.0  # 2026-11-04 12:00 UTC

    def product_html(self, changes=None, heading=None, title=None, extra=''):
        node = {'@type': 'Product', '@id': self.product + '#product', 'url': self.product,
                'name': self.name, 'offers': {'@type': 'Offer', 'price': '339.99',
                'priceCurrency': 'USD', 'url': self.product + '?variant=123'}}
        node.update(changes or {})
        return ('<html><head><title>' + (title or self.name + ' – Somerville Watch Co.')
                + '</title><script type="application/ld+json">' + json.dumps({'@graph': [node]})
                + '</script></head><body><h1>' + (heading or self.name) + '</h1>'
                + 'Product details ' * 50 + extra + '</body></html>')

    def archive(self, url, body, original=None, replay=None, code='200', replay_code=200, ts=None):
        from capture import epoch
        ts = ts or timestamp(self.now - 60)
        client = Client()
        client.json = Mock(return_value=(200, {}, [FIELDS, [ts, original or url, code, 'text/html', 'digest']]))
        client.request = Mock(return_value=(replay_code, {'Content-Type': 'text/html'}, body,
            replay or 'https://web.archive.org/web/' + ts + 'id_/' + url))
        with patch('capture.time.time', return_value=self.now):
            result = client.verify(url, epoch('20261104000000'), until=epoch('20261104235959'))
        return result, client

    def test_product_metadata_title_heading_and_offer_match(self):
        self.assertIsNotNone(self.archive(self.product, self.product_html())[0])
        self.assertTrue(usable_html(self.product_html({'@id': self.product}), self.product))

    def test_exact_product_name_must_match_title_and_heading(self):
        for body in (self.product_html(title='Another Watch – Somerville Watch Co.'),
                     self.product_html(heading='Another Watch'),
                     self.product_html({'name': 'Another Watch'}),
                     self.product_html({'name': 'Somerville Watch Co.'}, heading='Somerville Watch Co.')):
            with self.subTest(body=body[:80]):
                self.assertFalse(usable_html(body, self.product))

    def test_cart_or_recommendation_price_is_not_product_price(self):
        for changes in ({'offers': []}, {'offers': {'price': '0', 'priceCurrency': 'USD'}},
                        {'url': BASE + '/products/another-watch'},
                        {'offers': {'price': '339.99', 'priceCurrency': 'USD', 'url': BASE + '/products/another-watch'}},
                        {'@id': self.product + '?variant=999#product'}, {'@type': 'BreadcrumbList'}):
            with self.subTest(changes=changes):
                self.assertFalse(usable_html(self.product_html(changes, extra='<nav>Cart $99.00</nav>'), self.product))

    def test_malformed_prices_or_currency_fail_closed(self):
        for price in (None, True, 'NaN', 'Infinity', '-1', '0', '$339.99', '1e6', {}, []):
            with self.subTest(price=price):
                self.assertFalse(usable_html(self.product_html({'offers': {'price': price, 'priceCurrency': 'USD'}}), self.product))
        self.assertFalse(usable_html(self.product_html({'offers': {'price': '339.99'}}), self.product))

    def test_fake_document_titles_and_headings_do_not_count(self):
        fake = '<title>Somerville Watch Co.</title>'
        for wrapper in ('<!-- %s -->', '<script>%s</script>', '<template>%s</template>', '<style>%s</style>', '<svg>%s</svg>', '<noscript>%s</noscript>'):
            body = GOOD.replace('<title>Somerville Watch Co.</title>', '<title>Unrelated Store</title>' + wrapper % fake)
            self.assertFalse(usable_html(body, URL))
        body = self.product_html().replace('<h1>Ice Blue Watch</h1>', '<template><h1>Ice Blue Watch</h1></template>')
        self.assertFalse(usable_html(body, self.product))
        body = self.product_html().replace('<script type="application/ld+json">', '<template><script type="application/ld+json">').replace('</script>', '</script></template>')
        self.assertFalse(usable_html(body, self.product))

    def test_brand_required_in_actual_title(self):
        self.assertFalse(usable_html(GOOD.replace('<title>Somerville Watch Co.</title>', '<title>Watch shop</title>'), URL))
        self.assertFalse(usable_html(self.product_html(title=self.name), self.product))
        self.assertFalse(usable_html(GOOD + '<title>Somerville Watch Co.</title>', URL))

    def test_cdx_exact_url_rejects_normalized_aliases(self):
        for original in ('http://somervillewatchco.com/', 'https://www.somervillewatchco.com/', BASE,
                         BASE + '/?utm_source=x'):
            with self.subTest(original=original):
                evidence, client = self.archive(URL, GOOD, original=original)
                self.assertIsNone(evidence)
                client.request.assert_not_called()
        target = BASE + '/cart?a=1&b=2'
        self.assertIsNone(self.archive(target, GOOD, original=BASE + '/cart?b=2&a=1')[0])

    def test_replay_must_be_exact_id_timestamp_and_url(self):
        prefix = 'https://web.archive.org/web/' + timestamp(self.now - 60)
        for replay in (prefix + '/' + URL, prefix + 'id_/http://somervillewatchco.com/',
                       prefix.replace('https:', 'http:') + 'id_/' + URL,
                       'https://web.archive.org/web/' + timestamp(self.now - 120) + 'id_/' + URL):
            with self.subTest(replay=replay):
                self.assertIsNone(self.archive(URL, GOOD, replay=replay)[0])

    def test_any_2xx_requires_valid_html(self):
        for code in ('200', '201', '202', '203', '206', '299'):
            self.assertIsNotNone(self.archive(URL, GOOD, code=code, replay_code=int(code))[0])
        for code in ('301', '404', '429', '500'):
            self.assertIsNone(self.archive(URL, GOOD, code=code)[0])
        self.assertIsNone(self.archive(URL, '', code='204', replay_code=204)[0])

    def test_prior_day_within_twenty_hours_never_daily_success(self):
        evidence, client = self.archive(URL, GOOD, ts='20261103235959')
        self.assertIsNone(evidence)
        client.request.assert_not_called()
        self.assertIn('from=20261104&', client.json.call_args.args[0])

    def test_summary_counts_current_verified_evidence_only(self):
        from capture import PRIORITY, daily_summary, epoch
        urls = [BASE + p for p in PRIORITY] + [self.product, BASE + '/products/other']
        rows = [{'url': url, 'state': 'verified', 'timestamp': '20261104115900'} for url in urls]
        text, missing = daily_summary(urls, rows, epoch('20261104000000'))
        self.assertEqual(missing, [])
        self.assertIn('Products with no verified capture: 0', text)
        rows[0]['timestamp'] = '20261103235959'
        rows[-1]['state'] = 'success'
        text, missing = daily_summary(urls, rows, epoch('20261104000000'))
        self.assertEqual(missing, ['/'])
        self.assertIn('Products with no verified capture: 1', text)
        self.assertIn('Home (/)', text)
        text, _ = daily_summary(urls, rows, epoch('20261104000000'), False)
        self.assertIn('unknown (catalog discovery incomplete)', text)


class DailyPolicyRunnerTests(unittest.TestCase):
    """Real orchestration with a fake clock and fully stubbed network transport."""
    product = BASE + '/products/ice-blue-watch'

    def setUp(self):
        from capture import epoch
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        old = os.getcwd(); os.chdir(self.tmp.name); self.addCleanup(os.chdir, old)
        self.now = epoch('20261104120000')
        self.clock = patch('capture.time.time', side_effect=lambda: self.now); self.clock.start(); self.addCleanup(self.clock.stop)
        self.sleeper = patch('capture.time.sleep', side_effect=self.advance); self.sleeper.start(); self.addCleanup(self.sleeper.stop)
        self.env = patch.dict(os.environ, {'GITHUB_STEP_SUMMARY': '', 'GITHUB_RUN_ID': '12345', 'GITHUB_RUN_ATTEMPT': '1'})
        self.env.start(); self.addCleanup(self.env.stop)
        seed_test_state('state.json'); seed_test_state('.capture-state/pending.json')
        self.calls = []
        self.post_response = (401, {}, {'status': 'error'})
        self.status_response = (200, {}, {'status': 'pending'})
        self.c = Client(); self.c._request = Mock(side_effect=self.request)
        self.r = Runner(self.c, 'state.json', budget=20000)

    def advance(self, seconds):
        self.assertGreaterEqual(seconds, 0)
        self.now += seconds

    def request(self, url, data=None, authenticated=False):
        self.calls.append((url, data, self.now))
        if data is not None:
            saved = json.loads(Path(self.r.state_path).read_text())
            self.assertIn(data['url'], saved)
            self.assertEqual(saved[data['url']]['request_evidence']['attempts'][-1]['started_at'], self.now)
            response = self.post_response(data['url']) if callable(self.post_response) else self.post_response
        elif '/save/status/' in url:
            response = self.status_response
        else:
            response = (200, {}, [])
        status, headers, body = response
        return status, headers, body if isinstance(body, str) else json.dumps(body), url

    def posts(self):
        return [(data['url'], at) for _, data, at in self.calls if data is not None]

    def restart(self, budget=20000):
        self.r = Runner(self.c, 'state.json', budget=budget)
        return self.r

    def test_priority_exact_requested_order(self):
        from capture import PRIORITY
        self.assertEqual(PRIORITY, ['/', '/policies/refund-policy', '/policies/shipping-policy',
            '/policies/terms-of-service', '/policies/privacy-policy', '/policies/contact-information',
            '/policies/legal-notice', '/pages/contact', '/pages/warranty-policy',
            '/pages/no-brand-affiliation', '/policies/subscription-policy', '/cart'])

    def test_verified_products_survive_repeated_same_day_passes_and_new_inventory(self):
        second = BASE + '/products/second-watch'
        ts = timestamp(self.now - 60)
        def verify(runner):
            runner.process = Mock(side_effect=lambda url: runner.result(
                url, 'verified', timestamp=ts, original=url, statuscode='200'))
        verify(self.r)
        self.r.run_queue([self.product], 'all')
        self.r.process.assert_called_once_with(self.product)
        original = json.loads(json.dumps(self.r.state['_queues']['all:20261104:capture:daily-v2']))
        verify(self.restart())
        self.r.run_queue([self.product, second], 'all')
        self.r.process.assert_called_once_with(second)
        self.assertEqual({r['url'] for r in self.r.results}, {self.product, second})
        verify(self.restart())
        self.r.run_queue([self.product, second], 'all')
        self.r.process.assert_not_called()
        self.assertEqual({r['url'] for r in self.r.results}, {self.product, second})
        self.assertIn(original, [q for key, q in self.r.state['_queues'].items() if ':pass:' in key])
        self.assertEqual(self.calls, [])

    def test_prior_day_and_malformed_evidence_do_not_skip_products(self):
        second = BASE + '/products/second-watch'
        key = 'all:20261104:capture:daily-v2'
        self.r.state['_queues'] = {key: {'urls': [self.product, second], 'cursor': 2,
            'complete': True, 'results': [
                {'url': self.product, 'state': 'verified', 'timestamp': '20261103235959'},
                {'url': second, 'state': 'verified', 'timestamp': '20261104-invalid'}]}}
        self.r.save()
        self.r.process = Mock(side_effect=lambda url: self.r.result(url, 'unverified'))
        self.r.run_queue([self.product, second], 'all')
        self.assertEqual([c.args[0] for c in self.r.process.call_args_list], [self.product, second])
        self.assertFalse(any(r['state'] == 'verified' for r in self.r.results))

    def test_verified_products_are_rechecked_after_utc_day_change(self):
        self.r.process = Mock(side_effect=lambda url: self.r.result(
            url, 'verified', timestamp=timestamp(self.now), original=url))
        self.r.run_queue([self.product], 'all')
        self.now += 86400
        self.restart()
        self.r.process = Mock(side_effect=lambda url: self.r.result(url, 'unverified'))
        self.r.run_queue([self.product], 'all')
        self.r.process.assert_called_once_with(self.product)
        self.assertFalse(any(r['state'] == 'verified' for r in self.r.results))

    def test_reused_evidence_does_not_hide_unresolved_products_or_other_modes(self):
        second = BASE + '/products/second-watch'
        def first_pass(url):
            if url == self.product:
                return self.r.result(url, 'verified', timestamp=timestamp(self.now), original=url)
            return self.r.result(url, 'pending')
        self.r.process = Mock(side_effect=first_pass)
        self.r.run_queue([self.product, second], 'all')
        self.restart()
        self.r.process = Mock(side_effect=lambda url: self.r.result(url, 'pending'))
        self.r.run_queue([self.product, second], 'all')
        self.r.process.assert_called_once_with(second)
        self.assertEqual({r['url'] for r in self.r.results if r['state'] == 'verified'}, {self.product})
        self.r = Runner(self.c, 'state.json', verify_only=True)
        self.r.process = Mock(side_effect=lambda url: self.r.result(url, 'unverified'))
        self.r.run_queue([self.product, second], 'all')
        self.assertEqual(self.r.process.call_count, 2)

    def test_retained_evidence_survives_interrupted_new_product(self):
        second = BASE + '/products/second-watch'
        key = 'all:20261104:capture:daily-v2'
        verified = {'url': self.product, 'state': 'verified', 'timestamp': timestamp(self.now)}
        self.r.state['_queues'] = {key: {'urls': [second], 'cursor': 0,
            'complete': False, 'started': second, 'results': [verified]}}
        pending = {'job_id': 'accepted-second', 'submitted_at': self.now - 60}
        self.r.state[second] = dict(pending)
        self.r.save()
        self.restart().run_queue([self.product, second], 'all')
        self.assertIn(verified, self.r.state['_queues'][key]['results'])
        self.assertEqual(self.r.state[second], pending)
        self.assertEqual(self.calls, [])

    def test_http_200_payload_pause_stops_then_retains_job_for_review(self):
        # Transferred reproduction, now asserts the bounded review transition.
        contact = BASE + '/pages/contact'
        warranty = BASE + '/pages/warranty-policy'
        self.post_response = (200, {}, {'job_id': 'accepted-contact'})
        self.status_response = (200, {}, {'status': 'error', 'message':
            'crawling this host is paused; HTTP status=429; overloaded'})
        self.r.run_queue([contact, warranty], 'priority')
        self.r.run_queue([self.product], 'all')
        self.assertTrue(self.r.pause_circuit_open)
        self.assertEqual(self.r.state[contact]['job_id'], 'accepted-contact')
        self.assertTrue(self.r.state[contact]['review_required'])
        self.assertEqual(self.r.state[contact]['paused_job_errors'], 3)
        self.assertEqual(self.r.state[contact]['status_response']['http_status'], 200)
        self.assertEqual(self.r.state[contact]['status_response']['pause_triggers'],
            ['host_pause_message', 'reported_http_429', 'overloaded'])
        os.environ['GITHUB_RUN_ID'] = '12346'
        self.restart()
        before = len(self.calls)
        due = self.r.next_submit
        self.assertEqual(self.r.process(contact)['state'], 'review_required')
        self.assertGreaterEqual(self.calls[before][2], due)
        self.assertEqual(self.r.state[contact]['job_id'], 'accepted-contact')
        self.assertFalse(self.r.pause_circuit_open)
        self.assertEqual([url for url, _ in self.posts()], [contact])
        statuses = [(u, at) for u, _, at in self.calls if '/save/status/' in u]
        self.assertEqual(len(statuses), 3)
        self.assertTrue(all(b[1] - a[1] >= 1200 for a, b in zip(statuses, statuses[1:])))
        self.assertFalse(any(warranty in u or self.product in u for u, _, _ in self.calls))

    def test_same_day_rejected_product_only_one_post_and_next_day_allowed(self):
        self.assertEqual(self.r.process(self.product)['state'], 'failed')
        self.assertEqual(self.restart().process(self.product)['state'], 'deferred')
        self.assertEqual(len(self.posts()), 1)
        self.now += 86400
        self.assertEqual(self.restart().process(self.product)['state'], 'failed')
        self.assertEqual(len(self.posts()), 2)

    def test_all_legacy_attempt_times_count_not_only_latest(self):
        self.r.state['_completed_jobs'] = [{'url': self.product, 'state': 'failed',
            'submitted_at': self.now - 86400, 'request_evidence': {'attempts': [
                {'started_at': self.now - 10}, {'started_at': self.now - 86400}]}}]
        self.r.save()
        self.assertEqual(self.restart().process(self.product)['state'], 'deferred')
        self.assertEqual(self.posts(), [])

    def test_known_not_sent_product_not_retried_same_day(self):
        original = self.c._request.side_effect
        def transport(url, data=None, authenticated=False):
            if data is not None:
                self.calls.append((url, data, self.now))
                raise LookupFailure('connect failed', transport={'phase': 'tcp_connect', 'known_not_sent': True})
            return original(url, data, authenticated)
        self.c._request.side_effect = transport
        self.assertEqual(self.r.process(self.product)['state'], 'unverified')
        self.assertEqual(len(self.posts()), 1)
        self.assertTrue(self.r.state[self.product]['known_not_sent'])
        self.assertEqual(self.restart().process(self.product)['state'], 'deferred')
        self.assertEqual(len(self.posts()), 1)

    def test_uncertain_product_preserved_across_day_and_restart(self):
        self.post_response = (200, {}, {})
        self.assertEqual(self.r.process(self.product)['state'], 'pending')
        saved = dict(self.r.state[self.product])
        self.now += 86400
        self.assertEqual(self.restart().process(self.product)['state'], 'pending')
        self.assertEqual(self.r.state[self.product], saved)
        self.assertEqual(len(self.posts()), 1)

    def test_budget_reservation_without_attempt_does_not_use_product_day(self):
        self.r.state['_completed_jobs'] = [{'url': self.product, 'state': 'budget_not_submitted',
            'submitted_at': self.now, 'request_evidence': {}}]
        self.r.save()
        self.assertEqual(self.r.process(self.product)['state'], 'failed')
        self.assertEqual(len(self.posts()), 1)

    def test_reused_product_same_day_still_capped_after_retirement(self):
        self.r.state[self.product] = {'submitted_at': self.now - 100, 'job_id': 'old',
            'terminal_reused': {'timestamp': '20261103235959'}, 'retry_eligible_at': self.now - 1}
        self.r.save()
        self.assertEqual(self.r.process(self.product)['state'], 'deferred')
        self.assertEqual(self.posts(), [])
        self.assertEqual(self.r.state['_completed_jobs'][-1]['state'], 'reused_released')

    def test_paused_product_rechecks_without_second_post(self):
        self.post_response = (429, {}, {'status': 'error'})
        self.r.run_queue([self.product], 'all')
        self.assertEqual(len(self.posts()), 1)
        self.assertGreaterEqual(self.now - self.posts()[0][1], 1200)
        self.assertIn('product already attempted', self.r.results[-1]['reason'])

    def test_three_pauses_retry_same_tier_one_and_stop_immediately(self):
        self.post_response = (429, {}, {'status': 'error'})
        self.r.run_queue([URL, BASE + '/cart'], 'all')
        posts = self.posts()
        self.assertEqual([url for url, _ in posts], [URL] * 3)
        self.assertTrue(all(b[1] - a[1] >= 1200 for a, b in zip(posts, posts[1:])))
        self.assertEqual(self.now, posts[-1][1])
        self.assertTrue(self.r.pause_circuit_open)
        self.assertGreaterEqual(self.r.state['_next_submit'], self.now + 1200)
        self.assertTrue(all(row['capture_timestamp'] is None for row in self.r.results))
        self.assertEqual(self.r.results[-1]['url'], BASE + '/cart')
        self.assertEqual(self.r.state['_pause_responses']['count'], 3)

    def test_pause_breaker_survives_same_run_restart_but_not_new_run(self):
        self.post_response = (429, {}, {'status': 'error'})
        self.r.run_queue([URL], 'all')
        self.restart().run_queue([URL], 'all')
        self.assertEqual(len(self.posts()), 3)
        os.environ['GITHUB_RUN_ID'] = '12346'
        self.post_response = (401, {}, {'status': 'error'})
        gate = self.r.state['_next_submit']
        self.restart().run_queue([URL], 'all')
        self.assertEqual(len(self.posts()), 4)
        self.assertGreaterEqual(self.posts()[-1][1], gate)

    def test_short_budget_keeps_same_url_pause_cursor(self):
        self.post_response = (429, {}, {'status': 'error'})
        self.r.deadline = self.c.archive_deadline = self.now + 1000
        self.r.run_queue([URL, BASE + '/cart'], 'all')
        queue = self.r.state['_queues']['all:20261104:capture:daily-v2']
        self.assertEqual(queue['cursor'], 0)
        self.assertEqual(len(self.posts()), 1)
        self.assertEqual(self.restart().paused_responses, 1)
        self.r.run_queue([URL, BASE + '/cart'], 'all')
        self.assertEqual([u for u, _ in self.posts()], [URL] * 3)

    def test_accepted_paused_jobs_only_poll_never_resubmit(self):
        self.post_response = (200, {}, {'job_id': 'accepted-job'})
        self.status_response = (429, {}, {'status': 'error', 'message': 'overloaded'})
        self.r.run_queue([URL, BASE + '/cart'], 'all')
        self.assertEqual(len(self.posts()), 1)
        self.assertEqual(self.r.state[URL]['job_id'], 'accepted-job')
        statuses = [u for u, data, _ in self.calls if '/save/status/' in u]
        self.assertEqual(len(statuses), 3)
        self.assertTrue(self.r.pause_circuit_open)

    def test_paused_accepted_submission_counted_once(self):
        self.post_response = (429, {}, {'job_id': 'accepted-job', 'status': 'error'})
        self.status_response = (429, {}, {'status': 'error'})
        self.r.run_queue([URL], 'all')
        self.assertEqual(len(self.posts()), 1)
        self.assertEqual(len([u for u, _, _ in self.calls if '/save/status/' in u]), 2)
        self.assertEqual(self.r.paused_responses, 3)

    def test_non_json_overload_keeps_unknown_acceptance(self):
        self.post_response = (200, {}, 'Server overloaded, try later')
        self.r.run_queue([URL], 'all')
        self.assertEqual(len(self.posts()), 1)
        self.assertIn(URL, self.r.state)
        self.assertIsNone(self.r.state[URL]['job_id'])
        self.assertGreaterEqual(self.now - self.posts()[0][1], 1200)

    def test_longer_retry_after_and_saved_global_gate_are_preserved(self):
        from email.utils import formatdate
        gate = self.now + 7200
        self.r.state['_archive_next_request_at'] = gate
        self.r.state['_next_submit'] = gate
        self.r.next_submit = self.c.archive_next_request_at = gate
        self.r.save()
        self.r.pause({'retry-after': formatdate(self.now + 3600, usegmt=True)}, {'_http': 429})
        self.assertEqual(self.r.next_submit, gate)
        self.r.pause({'retry-after': '10800'}, {'message': 'overloaded'})
        self.assertEqual(self.r.next_submit, self.now + 10800)

    def test_nonpaused_spn_resets_streak_but_healthy_cdx_does_not(self):
        self.r.observe_archive_response('https://web.archive.org/cdx/search/cdx', 429, {}, {})
        self.r.observe_archive_response('https://web.archive.org/cdx/search/cdx', 200, {}, [])
        self.assertEqual(self.r.paused_responses, 1)
        self.r.observe_archive_response('https://web.archive.org/save/status/job', 200, {}, {'status': 'pending'})
        self.assertEqual(self.r.paused_responses, 0)

    def test_cdx_429_get_retry_waits_twenty_minutes_before_any_post(self):
        original = self.c._request.side_effect
        first = [True]
        def request(url, data=None, authenticated=False):
            if '/cdx/' in url and first[0]:
                first[0] = False
                self.calls.append((url, data, self.now))
                return 429, {}, 'overloaded', url
            return original(url, data, authenticated)
        self.c._request.side_effect = request
        self.r.process(URL)
        self.assertEqual(len(self.posts()), 1)
        self.assertGreaterEqual(self.posts()[0][1] - self.calls[0][2], 1200)

    def test_three_public_cdx_pauses_stop_before_fourth_request(self):
        def request(url, data=None, authenticated=False):
            self.calls.append((url, data, self.now))
            return 429, {}, 'overloaded', url
        self.c._request.side_effect = request
        self.r.run_queue([URL, BASE + '/cart'], 'all')
        self.assertEqual(len(self.calls), 3)
        self.assertTrue(all('/cdx/' in u for u, _, _ in self.calls))
        self.assertEqual(self.posts(), [])
        self.assertTrue(self.r.pause_circuit_open)
        self.assertEqual(self.now, self.calls[-1][2])

    def test_429_headers_survive_body_read_failure_and_count_once(self):
        from capture import LookupFailure
        response = Mock(status=429, headers={}, url='https://web.archive.org/cdx/search/cdx')
        response.__enter__ = Mock(return_value=response); response.__exit__ = Mock(return_value=False)
        response.read.side_effect = OSError('broken read')
        self.c._request = Client._request.__get__(self.c, Client)
        with patch('capture.ur.build_opener') as opener:
            opener.return_value.open.return_value = response
            with self.assertRaises(LookupFailure):
                self.c.paced_request(response.url)
        self.assertEqual(self.r.paused_responses, 1)
        self.assertGreaterEqual(self.r.state['_archive_next_request_at'], self.now + 1200)

    def test_global_floor_at_least_120_seconds_between_posts(self):
        self.r.run_queue([URL, BASE + '/cart'], 'all')
        self.assertEqual(len(self.posts()), 2)
        self.assertGreaterEqual(self.posts()[1][1] - self.posts()[0][1], 120)

    def test_old_daily_queue_preserved_but_removed_urls_never_executed(self):
        old_key = 'all:20261104:capture'
        old = {'urls': [BASE + '/cart/51183558787357:1?storefront=true', BASE + '/collections/all?page=2', self.product, URL],
            'cursor': 0, 'results': [{'url': URL, 'state': 'verified', 'timestamp': '20261104110000'}], 'complete': False}
        self.r.state['_queues'] = {old_key: old}
        self.r.save()
        self.r.run_queue([URL, self.product], 'all')
        self.assertEqual(self.r.state['_queues'][old_key], old)
        self.assertEqual([u for u, _ in self.posts()], [URL, self.product])
        self.assertFalse(any(r['state'] == 'verified' for r in self.r.results))

    def test_completed_new_queue_pass_keeps_earlier_history(self):
        self.r.run_queue([self.product], 'all')
        original = json.loads(json.dumps(self.r.state['_queues']['all:20261104:capture:daily-v2']))
        self.restart().run_queue([self.product], 'all')
        histories = [v for k, v in self.r.state['_queues'].items() if ':pass:' in k]
        self.assertIn(original, histories)
        self.assertEqual(len(self.posts()), 1)

    def test_main_prioritizes_all_twelve_and_reports_missing_names(self):
        from capture import main, PRIORITY
        with patch('sys.argv', ['capture.py', '--state', 'state.json']), patch('capture.Client', return_value=self.c), \
                patch('capture.discover', return_value=[self.product]):
            self.assertEqual(main(), 1)
        self.assertEqual([u for u, _ in self.posts()], [BASE + p for p in PRIORITY] + [self.product])

    def test_discovery_failure_still_runs_tier_one_and_count_unknown(self):
        from capture import main, PRIORITY
        os.environ['GITHUB_STEP_SUMMARY'] = 'summary.txt'
        with patch('sys.argv', ['capture.py', '--state', 'state.json']), patch('capture.Client', return_value=self.c), \
                patch('capture.discover', side_effect=LookupFailure('catalog unavailable')):
            self.assertEqual(main(), 1)
        self.assertEqual([u for u, _ in self.posts()], [BASE + p for p in PRIORITY])
        summary = Path('summary.txt').read_text()
        self.assertIn('Products with no verified capture: unknown', summary)
        self.assertIn('/policies/refund-policy', summary)

    def test_malformed_pause_checkpoint_fails_closed(self):
        from capture import StateContinuityError
        for value in ({'run': '12345:1', 'count': True}, {'run': '12345:1', 'count': 4}, {'day': '20261104', 'count': 1}):
            state = {'_next_submit': 0, '_pause_responses': value}
            Path('bad.json').write_text(json.dumps(state))
            with self.assertRaises(StateContinuityError):
                Runner(self.c, 'bad.json')
        self.assertEqual(self.calls, [])

    def test_replay_429_waits_twenty_minutes_and_never_submits(self):
        from urllib.parse import parse_qs, urlsplit
        ts = timestamp(self.now - 60)
        replay_count = [0]
        def request(url, data=None, authenticated=False):
            self.calls.append((url, data, self.now))
            if '/cdx/' in url:
                target = parse_qs(urlsplit(url).query)['url'][0]
                return 200, {}, json.dumps([FIELDS, [ts, target, '200', 'text/html', 'digest']]), url
            replay_count[0] += 1
            if replay_count[0] == 1:
                return 429, {}, 'overloaded', url
            return 200, {'Content-Type': 'text/html'}, GOOD, url
        self.c._request.side_effect = request
        row = self.r.process(URL)
        self.assertEqual(row['state'], 'verified')
        self.assertEqual(row['capture_timestamp'], ts)
        self.assertEqual(self.posts(), [])
        self.assertGreaterEqual(self.calls[-1][2] - self.calls[-2][2], 1200)
        self.assertEqual(self.r.paused_responses, 1)

    def test_cdx_pause_budget_preserves_unexecuted_cursor_and_gate(self):
        self.r.deadline = self.c.archive_deadline = self.now + 1000
        def request(url, data=None, authenticated=False):
            self.calls.append((url, data, self.now))
            return 429, {'Retry-After': '3600'}, 'overloaded', url
        self.c._request.side_effect = request
        self.r.run_queue([URL, self.product], 'all')
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.posts(), [])
        queue = self.r.state['_queues']['all:20261104:capture:daily-v2']
        self.assertEqual(queue['cursor'], 0)
        self.assertEqual(queue['retry_same_url'], URL)
        self.assertGreaterEqual(self.r.state['_archive_next_request_at'], self.now + 3600)

    def test_overloaded_200_cdx_counts_toward_three_response_breaker(self):
        def request(url, data=None, authenticated=False):
            self.calls.append((url, data, self.now))
            return 200, {}, json.dumps({'status': 'error', 'message': 'overloaded'}), url
        self.c._request.side_effect = request
        self.r.run_queue([URL], 'all')
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(self.posts(), [])
        self.assertTrue(self.r.pause_circuit_open)

    def test_health_and_account_responses_do_not_reset_pause_streak(self):
        self.r.observe_archive_response('https://web.archive.org/cdx/search/cdx', 429, {}, {})
        for path in ('/save/status/system', '/save/status/user'):
            self.r.observe_archive_response('https://web.archive.org' + path, 200, {}, {'status': 'success'})
        self.assertEqual(self.r.paused_responses, 1)
        self.r.observe_archive_response('https://web.archive.org/save/', 200, {}, {})
        self.assertEqual(self.r.paused_responses, 1)

    def test_interrupted_pause_marker_reconciles_same_accepted_job(self):
        key = 'priority:20261104:capture:daily-v2'
        self.r.state['_queues'] = {key: {'urls': [URL], 'cursor': 0, 'results': [],
            'complete': False, 'started': URL, 'retry_same_url': URL}}
        self.r.state[URL] = {'submitted_at': self.now - 60, 'job_id': 'accepted-job'}
        self.r.state['_pause_responses'] = {'run': '12345:1', 'count': 1}
        self.r.state['_archive_next_request_at'] = self.now + 1200
        self.r.save()
        self.restart().run_queue([URL], 'priority')
        self.assertEqual(self.posts(), [])
        self.assertEqual(len([u for u, _, _ in self.calls if '/save/status/accepted-job' in u]), 1)
        self.assertEqual(self.r.state[URL]['job_id'], 'accepted-job')
        self.assertEqual(self.r.state['_queues'][key]['cursor'], 1)
        self.assertNotIn('retry_same_url', self.r.state['_queues'][key])

    def test_mapped_outlink_product_attempt_counts_even_if_previously_verified(self):
        root = BASE + '/collections/all'
        batch = {'root_url': root, 'post_state': 'response_received', 'expansion_state': 'mapping_reported',
            'utc_day': self.r.since, 'attempt': {'started_at': self.now - 60},
            'children': {'child': {'url': self.product, 'job_ids': ['child-job'], 'coverage': 'verified'}},
            'child_order': ['child'], 'verification_cursor': 1}
        self.r.state['_outlink_batches'] = {root: batch}
        self.r.save()
        self.assertEqual(self.restart().process(self.product)['state'], 'deferred')
        self.assertEqual(self.posts(), [])
        self.assertEqual(self.r.state['_outlink_batches'][root], batch)

    def test_resumed_product_cursor_reconciles_missing_tier_one_first(self):
        from capture import main, PRIORITY
        from urllib.parse import parse_qs, urlsplit
        product_two = BASE + '/products/second-watch'
        ts = timestamp(self.now - 60)
        rows = [{'url': BASE + path, 'state': 'verified', 'timestamp': ts,
                 'capture_timestamp': ts} for path in PRIORITY[1:]]
        rows.insert(0, {'url': URL, 'state': 'pending', 'capture_timestamp': None})
        self.r.state['_queues'] = {
            'priority:20261104:capture:daily-v2': {'urls': [BASE + p for p in PRIORITY],
                'cursor': len(PRIORITY), 'complete': True, 'results': rows},
            'all:20261104:capture:daily-v2': {'urls': [self.product, product_two], 'cursor': 1,
                'complete': False, 'results': [{'url': self.product, 'state': 'failed', 'capture_timestamp': None}]}}
        self.r.state[URL] = {'submitted_at': self.now - 120, 'job_id': 'accepted-job'}
        self.r.save()
        original = self.c._request.side_effect
        def request(url, data=None, authenticated=False):
            if '/cdx/' in url and parse_qs(urlsplit(url).query)['url'][0] == URL:
                self.calls.append((url, data, self.now))
                return 200, {}, json.dumps([FIELDS, [ts, URL, '200', 'text/html', 'digest']]), url
            if '/web/' in url:
                self.calls.append((url, data, self.now))
                return 200, {'Content-Type': 'text/html'}, GOOD, url
            return original(url, data, authenticated)
        self.c._request.side_effect = request
        os.environ['GITHUB_STEP_SUMMARY'] = 'summary.txt'
        with patch('sys.argv', ['capture.py', '--state', 'state.json']), patch('capture.Client', return_value=self.c), \
                patch('capture.discover', return_value=[self.product, product_two]):
            self.assertEqual(main(), 0)  # Missing products are counted, tier 1 determines failure.
        self.assertEqual([u for u, _ in self.posts()], [product_two])
        self.assertEqual(parse_qs(urlsplit(self.calls[0][0]).query)['url'][0], URL)
        self.assertIn('Tier 1 pages with no verified capture: none', Path('summary.txt').read_text())
        self.assertIn('Products with no verified capture: 2', Path('summary.txt').read_text())
        state = json.loads(Path('state.json').read_text())
        self.assertNotIn(URL, state)
        self.assertEqual(state['_queues']['all:20261104:capture:daily-v2']['cursor'], 2)
        self.assertTrue(any(':pass:' in key for key in state['_queues']))

    def test_priority_only_summary_does_not_claim_zero_missing_products(self):
        from capture import main
        os.environ['GITHUB_STEP_SUMMARY'] = 'summary.txt'
        with patch('sys.argv', ['capture.py', '--scope', 'priority', '--state', 'state.json']), patch('capture.Client', return_value=self.c):
            self.assertEqual(main(), 1)
        self.assertIn('Products with no verified capture: not assessed (priority-only scope)', Path('summary.txt').read_text())

    def test_verified_retired_product_attempt_still_prevents_second_post(self):
        self.r.state[self.product] = {'submitted_at': self.now - 60, 'job_id': 'accepted-job'}
        self.r.save()
        original = self.c.verify
        self.c.verify = Mock(return_value={'timestamp': timestamp(self.now), 'original': self.product})
        self.assertEqual(self.r.process(self.product)['state'], 'verified')
        self.c.verify = original
        self.assertEqual(self.restart().process(self.product)['state'], 'deferred')
        self.assertEqual(self.posts(), [])


    def test_partial_priority_prefix_revisited_after_saved_pause_target(self):
        from capture import main, PRIORITY
        from urllib.parse import parse_qs, urlsplit
        shipping = BASE + PRIORITY[2]
        ts = timestamp(self.now - 60)
        self.r.state['_queues'] = {'priority:20261104:capture:daily-v2': {
            'urls': [BASE + p for p in PRIORITY], 'cursor': 2, 'complete': False,
            'retry_same_url': shipping, 'results': [
                {'url': URL, 'state': 'pending', 'capture_timestamp': None},
                {'url': BASE + PRIORITY[1], 'state': 'verified', 'timestamp': ts, 'capture_timestamp': ts}]}}
        self.r.state[URL] = {'submitted_at': self.now - 120, 'job_id': 'accepted-home'}
        self.r.state[shipping] = {'submitted_at': self.now - 120, 'job_id': None}
        self.r.save()
        original = self.c._request.side_effect
        def request(url, data=None, authenticated=False):
            if '/cdx/' in url and parse_qs(urlsplit(url).query)['url'][0] == URL:
                self.calls.append((url, data, self.now))
                return 200, {}, json.dumps([FIELDS, [ts, URL, '200', 'text/html', 'digest']]), url
            if '/web/' in url:
                self.calls.append((url, data, self.now))
                return 200, {'Content-Type': 'text/html'}, GOOD, url
            return original(url, data, authenticated)
        self.c._request.side_effect = request
        with patch('sys.argv', ['capture.py', '--state', 'state.json']), patch('capture.Client', return_value=self.c), \
                patch('capture.discover', return_value=[self.product]):
            self.assertEqual(main(), 1)
        checked = [parse_qs(urlsplit(u).query)['url'][0] for u, _, _ in self.calls if '/cdx/' in u]
        self.assertEqual(checked[:2], [shipping, URL])
        self.assertNotIn(shipping, [u for u, _ in self.posts()])
        self.assertEqual(self.posts()[-1][0], self.product)
        state = json.loads(Path('state.json').read_text())
        self.assertNotIn(URL, state)
        self.assertIsNone(state[shipping]['job_id'])
        self.assertTrue(any(':pass:' in key for key in state['_queues']))

    def test_malformed_attempt_history_never_grants_product_dispatch(self):
        for request in (None, {'attempts': [None]}, {'attempts': [{'started_at': 'unknown'}]}, {'attempts': 42}):
            with self.subTest(request=request):
                self.r.state['_completed_jobs'] = [{'url': self.product, 'state': 'failed',
                    'submitted_at': self.now - 86400, 'request_evidence': request}]
                self.r.save()
                row = self.restart().process(self.product)
                self.assertEqual(row['state'], 'unverified')
                self.assertIn('Malformed product', row['reason'])
                self.assertEqual(self.posts(), [])

    def test_malformed_mapped_outlink_attempt_is_unverified_not_crash(self):
        root = BASE + '/collections/all'
        for attempt in (None, [], 'unknown', {'started_at': 'unknown'}):
            with self.subTest(attempt=attempt):
                batch = {'root_url': root, 'post_state': 'response_received', 'expansion_state': 'mapping_reported',
                    'utc_day': 0, 'attempt': attempt,
                    'children': {'child': {'url': self.product, 'job_ids': ['job'], 'coverage': 'verified'}},
                    'child_order': ['child'], 'verification_cursor': 1}
                self.r.state['_outlink_batches'] = {root: batch}
                self.r.save()
                row = self.restart().process(self.product)
                self.assertEqual(row['state'], 'unverified')
                self.assertIn('Malformed outlink', row['reason'])
                self.assertEqual(self.posts(), [])


    def test_only_valid_job_id_can_reset_pause_streak_without_status(self):
        endpoint = 'https://web.archive.org/save/'
        self.r.observe_archive_response(endpoint, 429, {}, {})
        self.r.observe_archive_response(endpoint, 429, {}, {})
        for job_id in ({'job': 'invalid'}, ['invalid'], 123, True, 'bad job', 'bad/job', '', None):
            with self.subTest(job_id=job_id):
                self.r.observe_archive_response(endpoint, 200, {}, {'job_id': job_id})
                self.assertEqual(self.r.paused_responses, 2)
        self.r.observe_archive_response(endpoint, 200, {}, {'job_id': 'valid-job_123'})
        self.assertEqual(self.r.paused_responses, 0)

    def outlink_day_journal(self, started, children=None, **changes):
        root = BASE + '/collections/all'
        children = {} if children is None else children
        batch = {'root_url': root, 'post_state': 'outcome_unknown', 'expansion_state': 'unknown',
            'utc_day': self.r.since, 'created_at': self.now, 'attempt': {'started_at': started},
            'children': children, 'child_order': list(children), 'verification_cursor': 0}
        batch.update(changes)
        self.r.state['_outlink_batches'] = {root: batch}
        return batch

    def test_yesterday_jobless_outlink_allows_homepage_and_product_without_erasing_history(self):
        self.outlink_day_journal(self.r.since - 1)
        history = json.dumps(self.r.state['_outlink_batches'], sort_keys=True)
        self.r.save()
        for url in (URL, self.product):
            self.assertEqual(self.restart().process(url)['state'], 'failed')
        self.assertEqual([url for url, _ in self.posts()], [URL, self.product])
        self.assertEqual(json.dumps(json.loads(Path('state.json').read_text())['_outlink_batches'], sort_keys=True), history)

    def test_today_jobless_outlink_still_holds_homepage_and_product(self):
        self.outlink_day_journal(self.r.since)
        self.r.save()
        original = json.dumps(self.r.state['_outlink_batches'], sort_keys=True)
        for url in (URL, self.product):
            self.assertEqual(self.restart().process(url)['state'], 'outlink-held')
        self.assertEqual(self.posts(), [])
        self.assertEqual(json.dumps(json.loads(Path('state.json').read_text())['_outlink_batches'], sort_keys=True), original)

    def test_outlink_hold_uses_actual_utc_midnight_even_when_runner_day_is_stale(self):
        from capture import outlink_attempt_before_today
        midnight = self.r.since + 86400
        batch = self.outlink_day_journal(midnight - 0.001, children={
            'child': {'url': self.product, 'job_ids': ['child-job'], 'coverage': 'unverified'}})
        self.now = midnight - 0.001
        self.assertFalse(outlink_attempt_before_today(batch))
        self.assertIsNotNone(self.r.outlink_submission_hold(URL))
        self.assertTrue(self.r.product_attempted_today(self.product))
        self.now = midnight
        self.assertLess(self.r.since, midnight)  # No Runner restart or stale-day shortcut.
        self.assertTrue(outlink_attempt_before_today(batch))
        self.assertIsNone(self.r.outlink_submission_hold(URL))
        self.assertFalse(self.r.product_attempted_today(self.product))
        batch['attempt']['started_at'] = midnight
        self.assertFalse(outlink_attempt_before_today(batch))
        self.assertIsNotNone(self.r.outlink_submission_hold(URL))
        self.assertTrue(self.r.product_attempted_today(self.product))

    def test_outlink_age_uses_attempt_not_created_at_or_utc_day(self):
        for started, other_day, expired in ((self.r.since - 1, self.now, True),
                                           (self.r.since, self.r.since - 86400, False)):
            with self.subTest(started=started):
                self.outlink_day_journal(started, utc_day=other_day, created_at=other_day)
                for url in (URL, self.product):
                    self.assertEqual(self.r.outlink_submission_hold(url) is None, expired)
        self.assertEqual(self.posts(), [])

    def test_missing_or_invalid_outlink_start_falls_back_to_utc_day(self):
        from capture import outlink_attempt_before_today, outlink_attempt_time
        attempts = [None, [], 'unknown', {}] + [{'started_at': value} for value in
            (None, True, False, 0, -1, float('nan'), float('inf'), float('-inf'),
             '20261103', {}, [], 1e15, 10**1000, -(10**1000))]
        for attempt in attempts:
            with self.subTest(attempt=attempt):
                batch = self.outlink_day_journal(None, utc_day=self.r.since - 86400,
                    created_at=self.now, attempt=attempt)
                self.assertEqual(outlink_attempt_time(batch), self.r.since - 86400)
                self.assertTrue(outlink_attempt_before_today(batch))
                for url in (URL, self.product):
                    self.assertIsNone(self.r.outlink_submission_hold(url))
        batch.pop('attempt')
        self.assertTrue(outlink_attempt_before_today(batch))
        self.assertEqual(self.posts(), [])

    def test_missing_or_invalid_mapped_start_uses_same_utc_day_fallback(self):
        children = {'child': {'url': self.product, 'job_ids': ['child-job'], 'coverage': 'verified'}}
        for attempt in (None, {}, {'started_at': None}, {'started_at': True}, {'started_at': 0},
                        {'started_at': -1}, {'started_at': float('nan')}, {'started_at': float('inf')},
                        {'started_at': 10**1000}, {'started_at': -(10**1000)}):
            for day, attempted in ((self.r.since - 86400, False), (self.r.since, True)):
                with self.subTest(attempt=attempt, day=day):
                    self.outlink_day_journal(None, children=children,
                        expansion_state='mapping_reported', utc_day=day, attempt=attempt)
                    self.assertEqual(self.r.product_attempted_today(self.product), attempted)
        self.assertEqual(self.posts(), [])

    def test_no_attempt_start_with_yesterday_utc_day_allows_homepage_and_product(self):
        for attempt in (None, {}):
            with self.subTest(attempt=attempt):
                batch = self.outlink_day_journal(None, utc_day=self.r.since - 86400)
                if attempt is None:
                    batch.pop('attempt')
                else:
                    batch['attempt'] = attempt
                history = json.dumps(self.r.state['_outlink_batches'], sort_keys=True)
                seed_test_state('state.json', {'_outlink_batches': self.r.state['_outlink_batches']})
                self.calls.clear()
                for url in (URL, self.product):
                    self.assertEqual(self.restart().process(url)['state'], 'failed')
                self.assertEqual([url for url, _ in self.posts()], [URL, self.product])
                saved = json.loads(Path('state.json').read_text())
                self.assertEqual(json.dumps(saved['_outlink_batches'], sort_keys=True), history)

    def test_invalid_start_with_yesterday_utc_day_allows_homepage_and_product(self):
        self.outlink_day_journal('invalid', utc_day=self.r.since - 86400)
        history = json.dumps(self.r.state['_outlink_batches'], sort_keys=True)
        self.r.save()
        for url in (URL, self.product):
            self.assertEqual(self.restart().process(url)['state'], 'failed')
        self.assertEqual([url for url, _ in self.posts()], [URL, self.product])
        self.assertEqual(json.dumps(self.r.state['_outlink_batches'], sort_keys=True), history)

    def test_created_at_fallback_with_missing_or_invalid_utc_day_survives_reload(self):
        for day in (None, 'invalid', 0, False, -1, 10**1000):
            with self.subTest(day=day):
                batch = self.outlink_day_journal(None, utc_day=day, created_at=self.r.since - 1)
                batch.pop('attempt')
                if day is None:
                    batch.pop('utc_day')
                history = json.dumps(self.r.state['_outlink_batches'], sort_keys=True)
                seed_test_state('state.json', {'_outlink_batches': self.r.state['_outlink_batches']})
                self.calls.clear()
                for url in (URL, self.product):
                    self.assertEqual(self.restart().process(url)['state'], 'failed')
                self.assertEqual([url for url, _ in self.posts()], [URL, self.product])
                saved = json.loads(Path('state.json').read_text())
                self.assertEqual(json.dumps(saved['_outlink_batches'], sort_keys=True), history)

    def test_created_at_fallback_is_shared_by_mapped_product_accounting(self):
        import hashlib
        for child in ({'url': self.product}, {'url_sha256': hashlib.sha256(self.product.encode()).hexdigest()}):
            child = {**child, 'job_ids': ['mapped-job'], 'coverage': 'verified'}
            for day, expected in ((self.r.since - 1, 'failed'), (self.r.since, 'deferred')):
                with self.subTest(child=child, day=day):
                    batch = self.outlink_day_journal(None, children={'mapped': child},
                        expansion_state='mapping_reported', utc_day='invalid', created_at=day)
                    history = json.dumps(self.r.state['_outlink_batches'], sort_keys=True)
                    seed_test_state('state.json', {'_outlink_batches': self.r.state['_outlink_batches']})
                    self.calls.clear()
                    self.assertEqual(self.restart().process(self.product)['state'], expected)
                    self.assertEqual([url for url, _ in self.posts()], [self.product] if expected == 'failed' else [])
                    self.assertEqual(json.dumps(self.r.state['_outlink_batches'], sort_keys=True), history)

    def test_today_or_future_primary_never_falls_back_to_older_fields(self):
        from capture import outlink_attempt_time, outlink_attempt_before_today
        for started in (self.r.since, self.now, self.now + 86400, 1e15 - 1):
            with self.subTest(started=started):
                batch = self.outlink_day_journal(started, utc_day=self.r.since - 86400,
                    created_at=self.r.since - 86400)
                self.assertEqual(outlink_attempt_time(batch), started)
                self.assertFalse(outlink_attempt_before_today(batch))
                for url in (URL, self.product):
                    self.assertIsNotNone(self.r.outlink_submission_hold(url))
        self.assertEqual(self.posts(), [])

    def test_current_or_future_utc_day_precedes_older_created_at(self):
        from capture import outlink_attempt_time, outlink_attempt_before_today
        for day in (self.r.since, self.r.since + 86400):
            with self.subTest(day=day):
                batch = self.outlink_day_journal('invalid', utc_day=day, created_at=self.r.since - 1)
                self.assertEqual(outlink_attempt_time(batch), day)
                self.assertFalse(outlink_attempt_before_today(batch))
                for url in (URL, self.product):
                    self.assertIsNotNone(self.r.outlink_submission_hold(url))
        self.assertEqual(self.posts(), [])

    def test_future_selected_timestamp_does_not_release_verified_mapped_product(self):
        import hashlib
        for child in ({'url': self.product}, {'url_sha256': hashlib.sha256(self.product.encode()).hexdigest()}):
            for source in ('attempt', 'utc_day', 'created_at'):
                with self.subTest(child=child, source=source):
                    batch = self.outlink_day_journal(None, children={'mapped': {
                        **child, 'job_ids': ['mapped-job'], 'coverage': 'verified'}},
                        expansion_state='mapping_reported', utc_day=0, created_at=self.r.since - 1)
                    if source == 'attempt':
                        batch['attempt']['started_at'] = self.r.since + 86400
                    else:
                        batch[source] = self.r.since + 86400
                    history = json.dumps(self.r.state['_outlink_batches'], sort_keys=True)
                    seed_test_state('state.json', {'_outlink_batches': self.r.state['_outlink_batches']})
                    self.calls.clear()
                    row = self.restart().process(self.product)
                    self.assertEqual(row['state'], 'unverified')
                    self.assertIn('Future outlink', row['reason'])
                    self.assertEqual(self.posts(), [])
                    self.assertEqual(json.dumps(self.r.state['_outlink_batches'], sort_keys=True), history)

    def test_no_valid_timestamp_preserves_jobless_hold_and_mapped_protection(self):
        from capture import outlink_attempt_time, outlink_attempt_before_today
        for invalid in (None, 'invalid', True, False, 0, -1, float('nan'), float('inf'),
                        float('-inf'), 1e15, 10**1000, -(10**1000), {}, []):
            with self.subTest(invalid=invalid):
                batch = self.outlink_day_journal(invalid, utc_day=invalid, created_at=invalid)
                self.assertIsNone(outlink_attempt_time(batch))
                self.assertFalse(outlink_attempt_before_today(batch))
                for url in (URL, self.product):
                    self.assertIsNotNone(self.r.outlink_submission_hold(url))
                batch['children'] = {'mapped': {'url': self.product, 'job_ids': ['job'], 'coverage': 'verified'}}
                with self.assertRaisesRegex(LookupFailure, 'Malformed outlink'):
                    self.r.product_attempted_today(self.product)
        self.assertEqual(self.posts(), [])

    def test_zero_day_without_valid_fallback_remains_held_after_reload(self):
        self.outlink_day_journal(None, utc_day=0, created_at=None)
        history = json.dumps(self.r.state['_outlink_batches'], sort_keys=True)
        self.r.save()
        for url in (URL, self.product):
            self.assertEqual(self.restart().process(url)['state'], 'outlink-held')
        self.assertEqual(self.posts(), [])
        self.assertEqual(json.dumps(self.r.state['_outlink_batches'], sort_keys=True), history)

    def test_missing_or_invalid_all_dates_still_fail_closed_at_state_load(self):
        from capture import StateContinuityError
        for day in (None, 'invalid', False, -1, 10**1000):
            with self.subTest(day=day):
                batch = self.outlink_day_journal(None, utc_day=day, created_at=None)
                if day is None:
                    batch.pop('utc_day')
                seed_test_state('state.json', self.r.state)
                original = Path('state.json').read_bytes()
                with self.assertRaises(StateContinuityError):
                    self.restart()
                self.assertEqual(Path('state.json').read_bytes(), original)
        self.assertEqual(self.calls, [])

    def test_old_mapped_and_redacted_child_holds_expire_but_today_still_holds(self):
        import hashlib
        children = {
            'mapped': {'url': self.product, 'job_ids': ['mapped-job'], 'coverage': 'unverified'},
            'redacted': {'hostname': 'somervillewatchco.com', 'job_ids': ['redacted-job'], 'coverage': 'unverified'},
            'hashed': {'url_sha256': hashlib.sha256(self.product.encode()).hexdigest(),
                       'job_ids': ['hashed-job'], 'coverage': 'unverified'}}
        self.outlink_day_journal(self.r.since - 1, children=children, expansion_state='mapping_reported')
        self.r.save()
        history = json.dumps(self.r.state['_outlink_batches'], sort_keys=True)
        for url in (URL, self.product):
            self.assertEqual(self.restart().process(url)['state'], 'failed')
        self.assertEqual([url for url, _ in self.posts()], [URL, self.product])
        self.assertEqual(json.dumps(self.r.state['_outlink_batches'], sort_keys=True), history)
        batch = next(iter(self.r.state['_outlink_batches'].values()))
        batch['attempt']['started_at'] = self.r.since
        self.assertIsNotNone(self.r.outlink_submission_hold(URL))
        self.assertIsNotNone(self.r.outlink_submission_hold(self.product))
        self.assertTrue(self.r.product_attempted_today(self.product))

    def test_hashed_outlink_mapping_ages_out_without_losing_same_day_product_cap(self):
        import hashlib
        child = {'url_sha256': hashlib.sha256(self.product.encode()).hexdigest(),
                 'job_ids': ['hashed-job'], 'coverage': 'unverified'}
        batch = self.outlink_day_journal(self.r.since - 1, children={'hash': child},
            expansion_state='mapping_reported')
        self.assertIsNone(self.r.outlink_submission_hold(self.product))
        self.assertFalse(self.r.product_attempted_today(self.product))
        batch['attempt']['started_at'] = self.r.since
        self.assertIsNotNone(self.r.outlink_submission_hold(self.product))
        self.assertTrue(self.r.product_attempted_today(self.product))
        child['coverage'] = 'verified'
        self.assertIsNone(self.r.outlink_submission_hold(self.product))
        self.assertTrue(self.r.product_attempted_today(self.product))
        self.assertEqual(self.posts(), [])

    def test_historical_outlink_does_not_hide_another_current_day_hold(self):
        self.outlink_day_journal(self.r.since - 1)
        root = BASE + '/collections/all?page=2'
        self.r.state['_outlink_batches'][root] = {
            **next(iter(self.r.state['_outlink_batches'].values())), 'root_url': root,
            'attempt': {'started_at': self.r.since}}
        for url in (URL, self.product):
            self.assertEqual(self.r.process(url)['state'], 'outlink-held')
        self.assertEqual(self.posts(), [])

    def test_historical_outlink_does_not_release_unknown_direct_pending_jobs(self):
        self.outlink_day_journal(self.r.since - 1)
        direct = {'job_id': None, 'submitted_at': self.r.since - 60}
        for url in (URL, self.product):
            self.r.state[url] = dict(direct)
        self.r.save()
        for url in (URL, self.product):
            self.assertEqual(self.restart().process(url)['state'], 'pending')
            self.assertEqual(self.r.state[url], direct)
        self.assertEqual(self.posts(), [])

    def test_historical_outlink_does_not_replace_accepted_direct_pending_jobs(self):
        self.outlink_day_journal(self.r.since - 1)
        for url in (URL, self.product):
            self.r.state[url] = {'job_id': 'accepted-job', 'submitted_at': self.r.since - 60}
        self.r.save()
        for url in (URL, self.product):
            self.assertEqual(self.restart().process(url)['state'], 'pending')
            self.assertEqual(self.r.state[url]['job_id'], 'accepted-job')
        self.assertEqual(self.posts(), [])
        self.assertEqual(len([url for url, _, _ in self.calls if '/save/status/accepted-job' in url]), 2)

    def test_historical_outlink_does_not_clear_independent_cooldowns(self):
        for key in ('_next_submit', '_archive_next_request_at', '_transport_cooldown_until'):
            with self.subTest(key=key):
                self.outlink_day_journal(self.r.since - 1)
                until = self.now + 30000
                seed_test_state('state.json', {'_outlink_batches': self.r.state['_outlink_batches'], key: until})
                self.restart()
                self.assertIsNone(self.r.outlink_submission_hold(URL))
                self.assertEqual(self.r.process(URL)['state'], 'deferred')
                self.assertEqual(self.r.state[key], until)
                self.assertEqual(self.posts(), [])

    def test_historical_outlink_does_not_bypass_state_schema_validation(self):
        from capture import StateContinuityError
        for changes in ({'children': []}, {'post_state': 'invalid'}, {'expansion_state': 'invalid'},
                        {'server_paused_until': 'unknown'}, {'verification_cursor': -1}):
            with self.subTest(changes=changes):
                self.outlink_day_journal(self.r.since - 1, **changes)
                seed_test_state('state.json', self.r.state)
                original = Path('state.json').read_bytes()
                with self.assertRaises(StateContinuityError):
                    self.restart()
                self.assertEqual(Path('state.json').read_bytes(), original)
        self.assertEqual(self.calls, [])

    def test_priority_and_all_never_dispatch_or_create_outlink_batches(self):
        from capture import main, PRIORITY
        for scope in ('priority', 'all'):
            for has_history in (False, True):
                with self.subTest(scope=scope, has_history=has_history):
                    self.outlink_day_journal(self.r.since - 1)
                    journals = self.r.state['_outlink_batches'] if has_history else {}
                    history = json.dumps(journals, sort_keys=True)
                    seed_test_state('state.json', {'_next_submit': 0, '_outlink_batches': journals})
                    self.calls.clear()
                    with patch('sys.argv', ['capture.py', '--scope', scope, '--state', 'state.json']), \
                            patch('capture.Client', return_value=self.c), \
                            patch('capture.discover', return_value=[self.product]), \
                            patch.object(Runner, 'run_outlink_probe', side_effect=AssertionError('daily outlink probe')) as probe:
                        self.assertEqual(main(), 1)
                    probe.assert_not_called()
                    expected = [BASE + path for path in PRIORITY] + ([self.product] if scope == 'all' else [])
                    self.assertEqual([url for url, _ in self.posts()], expected)
                    for _, data, _ in self.calls:
                        if data is not None:
                            self.assertEqual(set(data), {'url', 'if_not_archived_within'})
                    saved = json.loads(Path('state.json').read_text())
                    self.assertEqual(json.dumps(saved['_outlink_batches'], sort_keys=True), history)


# Keep additional regressions discoverable through the unchanged production CI command.
from test_verification_candidate import CDXSchemaCandidateTests, CanonicalParserCandidateTests
from test_legacy_state import LegacyStateTests

class DailyBatchPipelineTests(unittest.TestCase):
    product = BASE + '/products/ice-blue-watch'
    advance = DailyPolicyRunnerTests.advance
    posts = DailyPolicyRunnerTests.posts
    restart = DailyPolicyRunnerTests.restart

    def setUp(self):
        from capture import epoch
        self.jobs = {}
        self.calls = []
        self.pause_url = None
        self.pause_status = 200
        self.index_unavailable = False
        self.hide_index = set()
        self.crash_after_post = False
        self.fail_replay = None
        self.index_lag = 0
        DailyPolicyRunnerTests.setUp(self)
        self.now = epoch('20261104000700')
        self.since = epoch('20261104000000')
        self.restart(budget=19800)

    def request(self, url, data=None, authenticated=False):
        from urllib.parse import parse_qs, urlsplit
        self.calls.append((url, data, self.now))
        self.now += 2  # Completion-based gate must include request duration.
        if data is not None:
            saved = json.loads(Path(self.r.state_path).read_text())
            target = data['url']
            self.assertEqual(saved[target]['request_evidence']['attempts'][-1]['started_at'], self.calls[-1][2])
            self.assertNotIn(target, self.jobs, 'duplicate physical submission')
            self.jobs[target] = {'id': 'accepted-' + str(len(self.jobs)), 'at': self.now}
            if self.crash_after_post:
                self.crash_after_post = False
                raise KeyboardInterrupt('synthetic interruption after acceptance')
            return 200, {}, json.dumps({'job_id': self.jobs[target]['id']}), url
        if '/save/status/' in url:
            target = next(u for u, row in self.jobs.items() if row['id'] == url.rsplit('/', 1)[-1])
            if target == self.pause_url:
                return self.pause_status, {}, json.dumps({'status': 'error', 'message':
                    'crawling this host is paused; HTTP status=429; overloaded'}), url
            return 200, {}, json.dumps({'status': 'success', 'timestamp': timestamp(self.jobs[target]['at']),
                                        'original_url': target}), url
        if '/cdx/' in url:
            if self.index_unavailable:
                return 200, {}, json.dumps([FIELDS, ['bad-row']]), url
            query = parse_qs(urlsplit(url).query)
            rows = [FIELDS]
            lower, upper = query['from'][0], query['to'][0]
            for target, row in self.jobs.items():
                ts = timestamp(row['at'])
                if (target not in self.hide_index and self.now - row['at'] >= self.index_lag
                        and lower <= ts <= upper
                        and (query['matchType'][0] == 'host' or query['url'][0] == target)):
                    rows.append([ts, target, '200', 'text/html', 'digest'])
            return 200, {}, json.dumps(rows), url
        original = url.split('id_/', 1)[1]
        if self.fail_replay == original:
            raise LookupFailure('synthetic replay transport failure')
        body = GOOD
        if '/products/' in original:
            name = original.rsplit('/', 1)[-1].replace('-', ' ').title()
            node = {'@type': 'Product', 'url': original, '@id': original + '#product', 'name': name,
                    'offers': {'price': '339.99', 'priceCurrency': 'USD'}}
            body = ('<html><title>' + name + ' – Somerville Watch Co.</title>'
                    '<script type="application/ld+json">' + json.dumps(node) + '</script>'
                    '<body><h1>' + name + '</h1>' + 'Watch details ' * 80 + '</body></html>')
        return 200, {'content-type': 'text/html'}, body, url

    def verified(self):
        return {r['url'] for r in self.r.results if r.get('state') == 'verified'
                and r.get('timestamp', '').startswith(timestamp(self.r.since)[:8])}

    def test_all_166_fresh_targets_complete_with_four_330_minute_windows(self):
        from capture import PRIORITY
        priority = [BASE + path for path in PRIORITY]
        products = [BASE + '/products/watch-' + str(i) for i in range(154)]
        targets = priority + products
        windows = []
        for i in range(4):
            self.now = self.since + i * 21600 + 420
            os.environ['GITHUB_RUN_ID'] = str(50000 + i)
            self.restart(budget=19800)
            started = self.now
            self.r.run_daily_queue(priority, 'priority')
            self.r.run_daily_queue(products, 'all')
            windows.append(self.now - started)
            self.assertLessEqual(windows[-1], 19800)
        self.assertEqual(self.verified(), set(targets))
        self.assertEqual(len(self.posts()), 166)
        self.assertEqual(len({u for u, _ in self.posts()}), 166)
        self.assertLess(len(self.calls), 560)
        self.assertTrue(all(at < self.since + 86400 for _, at in self.posts()))
        self.assertTrue(all(b[2] - a[2] >= 122 for a, b in zip(self.calls, self.calls[1:])))
        print('PIPELINE_METRIC ' + json.dumps({'targets': len(targets), 'requests': len(self.calls),
            'posts': len(self.posts()), 'window_seconds': windows, 'finished_utc': timestamp(self.now)}))

    def test_policy_scope_never_discovers_or_reads_products_and_reports_only_its_targets(self):
        from capture import main, POLICIES
        for i, path in enumerate(POLICIES):
            self.jobs[BASE + path] = {'id': 'existing-' + str(i), 'at': self.since + 60}
        # A saved product job/queue must remain untouched and never be polled.
        self.r.state[self.product] = {'submitted_at': self.since, 'job_id': 'accepted-product'}
        self.r.state['_queues'] = {'all:20261104:capture:daily-v2': {
            'urls': [self.product], 'cursor': 0, 'results': [], 'complete': False}}
        self.r.save()
        with patch('sys.argv', ['capture.py', '--scope', 'policies', '--daily-batches', '--verify-only', '--state', 'state.json']), \
                patch('capture.Client', return_value=self.c), patch('capture.discover') as discover_products, \
                patch('capture.discover_supplemental') as discover_other, patch('builtins.print') as output:
            self.assertEqual(main(), 0)
        discover_products.assert_not_called()
        discover_other.assert_not_called()
        self.assertEqual(self.posts(), [])
        replayed = [url.split('id_/', 1)[1] for url, data, _ in self.calls if 'id_/' in url]
        self.assertEqual(set(replayed), {BASE + path for path in POLICIES})
        self.assertEqual(len(POLICIES), 9)
        self.assertNotIn(BASE + '/', replayed)
        self.assertNotIn(BASE + '/pages/contact', replayed)
        self.assertNotIn(BASE + '/pages/no-brand-affiliation', replayed)
        self.assertFalse(any('/products/' in url for url, _, _ in self.calls))
        state = json.loads(Path('state.json').read_text())
        self.assertEqual(state[self.product]['job_id'], 'accepted-product')
        self.assertEqual(state['_queues']['all:20261104:capture:daily-v2']['cursor'], 0)
        self.assertIn('paused (policy/cart-only scope)', '\n'.join(str(x) for x in output.call_args_list))
        from capture import operational_summary
        self.assertTrue(any(q['key'].startswith('policies:') for q in operational_summary(state)['queues']))

    def test_missing_policy_fails_even_when_every_other_policy_is_verified(self):
        from capture import main, POLICIES
        for i, path in enumerate(POLICIES):
            if path != '/cart':
                self.jobs[BASE + path] = {'id': 'existing-' + str(i), 'at': self.since + 60}
        with patch('sys.argv', ['capture.py', '--scope', 'policies', '--daily-batches', '--verify-only', '--state', 'state.json']), \
                patch('capture.Client', return_value=self.c):
            self.assertEqual(main(), 1)
        self.assertEqual(self.posts(), [])

    def test_policy_retry_preserves_verified_work_and_only_submits_missing_targets(self):
        from capture import POLICIES
        targets = [BASE + path for path in POLICIES]
        for i, url in enumerate(targets[:-1]):
            self.jobs[url] = {'id': 'existing-' + str(i), 'at': self.since + 60}
        self.r.verify_only = True
        self.r.run_daily_queue(targets, 'policies')
        self.assertEqual(self.posts(), [])
        self.advance(8 * 3600)
        os.environ['GITHUB_RUN_ID'] = '50001'
        self.restart(budget=19800)
        self.r.run_daily_queue(targets, 'policies')
        self.assertEqual([url for url, _ in self.posts()], [BASE + '/cart'])
        self.assertEqual(self.verified(), set(targets))

    def test_incomplete_index_stops_all_later_work_without_submission(self):
        self.index_unavailable = True
        self.r.run_daily_queue([URL], 'priority')
        self.r.run_daily_queue([self.product], 'all')
        self.assertEqual(self.posts(), [])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.r.state['_queues']['priority:20261104:capture:daily-v3']['phase'], 'index_before')
        self.assertFalse(self.verified())

    def test_restart_after_unknown_acceptance_never_resubmits(self):
        self.crash_after_post = True
        self.hide_index.add(self.product)
        with self.assertRaises(KeyboardInterrupt):
            self.r.run_daily_queue([self.product], 'all')
        pending = json.loads(json.dumps(self.r.state[self.product]))
        self.restart().run_daily_queue([self.product], 'all')
        self.assertEqual(len(self.posts()), 1)
        self.assertEqual(self.r.state[self.product], pending)
        self.assertFalse(self.verified())

    def test_restart_recovers_partial_replay_proof_without_recapturing(self):
        second = BASE + '/products/second-watch'
        self.fail_replay = second
        self.r.run_daily_queue([self.product, second], 'all')
        self.assertEqual(self.verified(), {self.product})
        key = 'all:20261104:capture:daily-v3'
        self.assertEqual(self.r.state['_queues'][key]['phase'], 'index_after')
        self.fail_replay = None
        self.restart().run_daily_queue([self.product, second], 'all')
        self.assertEqual(self.verified(), {self.product, second})
        self.assertEqual(len(self.posts()), 2)

    def test_index_lag_retains_jobs_and_reconciles_next_pass(self):
        self.index_lag = 10000
        self.r.run_daily_queue([self.product], 'all')
        self.assertFalse(self.verified())
        self.assertIn(self.product, self.r.state)
        self.now += 10000
        self.restart().run_daily_queue([self.product], 'all')
        self.assertEqual(self.verified(), {self.product})
        self.assertEqual(len(self.posts()), 1)

    def test_new_inventory_joins_unfinished_queue(self):
        second = BASE + '/products/second-watch'
        self.restart(budget=300).run_daily_queue([self.product], 'all')
        self.assertEqual(self.posts(), [])
        self.restart().run_daily_queue([self.product, second], 'all')
        self.assertEqual(self.verified(), {self.product, second})
        self.assertEqual(len(self.posts()), 2)

    def test_verified_daily_v2_evidence_is_reused_and_history_preserved(self):
        key = 'all:20261104:capture:daily-v2'
        prior = {'urls': [self.product], 'cursor': 1, 'complete': True,
                 'results': [{'url': self.product, 'state': 'verified', 'timestamp': '20261104000100'}]}
        self.r.state['_queues'] = {key: prior}
        self.r.save()
        self.r.run_daily_queue([self.product], 'all')
        self.assertEqual(self.verified(), {self.product})
        self.assertEqual(self.calls, [])
        self.assertEqual(self.r.state['_queues'][key], prior)

    def test_outlink_hold_stops_before_any_other_submission(self):
        reason = 'current outlink expansion unresolved'
        self.r.outlink_submission_hold = Mock(return_value=reason)
        self.r.run_daily_queue([URL, BASE + '/cart'], 'priority')
        self.r.run_daily_queue([self.product], 'all')
        self.assertEqual(self.posts(), [])
        self.assertEqual(self.r.outlink_hold['url'], URL)
        self.assertEqual(self.r.outlink_hold['reason'], reason)

    def test_three_paused_job_errors_stop_then_allow_later_pages_after_cooldown(self):
        contact, warranty = BASE + '/pages/contact', BASE + '/pages/warranty-policy'
        self.pause_url = contact
        self.hide_index.add(contact)
        self.r.run_daily_queue([contact, warranty], 'priority')
        self.assertTrue(self.r.pause_circuit_open)
        self.assertTrue(self.r.state[contact]['review_required'])
        self.assertEqual(self.r.state[contact]['paused_job_errors'], 3)
        held = self.r.state[contact]['job_id']
        self.assertEqual([u for u, _ in self.posts()], [contact])
        due, before = self.r.next_submit, len(self.calls)
        os.environ['GITHUB_RUN_ID'] = '12346'
        self.restart().run_daily_queue([contact, warranty], 'priority')
        self.assertGreaterEqual(self.calls[before][2], due)
        self.assertIn(warranty, self.verified())
        self.assertEqual(self.r.state[contact]['job_id'], held)
        self.assertEqual(sum('/save/status/' + held in u for u, _, _ in self.calls), 3)
        self.assertEqual([u for u, _ in self.posts()], [contact, warranty])

    def test_actual_status_http_429_does_not_quarantine_the_accepted_job(self):
        contact = BASE + '/pages/contact'
        self.pause_url, self.pause_status = contact, 429
        self.hide_index.add(contact)
        self.r.run_daily_queue([contact], 'priority')
        self.assertTrue(self.r.pause_circuit_open)
        self.assertFalse(self.r.state[contact].get('review_required'))
        self.assertEqual(self.r.state[contact]['paused_job_errors'], 0)
        self.assertEqual(len(self.posts()), 1)

    def test_verify_and_poll_modes_never_post(self):
        for mode in ('verify', 'poll'):
            with self.subTest(mode=mode):
                self.r = Runner(self.c, 'state.json', verify_only=mode == 'verify', poll_only=mode == 'poll')
                self.r.run_daily_queue([self.product], 'all')
        self.assertEqual(self.posts(), [])
        self.assertTrue(all('/save/status/' not in u for u, _, _ in self.calls))

    def test_utc_rollover_stops_new_submissions_and_does_not_reuse_old_coverage(self):
        self.now = self.since + 86400 - 600
        products = [BASE + '/products/watch-' + str(i) for i in range(8)]
        self.restart(budget=3600).run_daily_queue(products, 'all')
        self.assertTrue(all(at < self.since + 86400 for _, at in self.posts()))
        self.assertLess(len(self.posts()), len(products))
        self.now = self.since + 86400 + 420
        self.r = Runner(self.c, 'state.json', verify_only=True)
        self.r.run_daily_queue(products, 'all')
        self.assertFalse(self.verified())

    def test_invalid_batch_checkpoint_is_held_before_io(self):
        from capture import StateContinuityError
        key = 'all:20261104:capture:daily-v3'
        self.r.state['_queues'] = {key: {'urls': [self.product], 'cursor': 0, 'complete': False,
            'results': [], 'phase': 'capture', 'batch_start': 0, 'batch_end': 99}}
        with self.assertRaises(StateContinuityError):
            self.r.save()
        self.assertEqual(self.calls, [])

    def test_main_reports_failure_when_only_products_are_missing(self):
        from capture import main, PRIORITY
        for i, path in enumerate(PRIORITY):
            self.jobs[BASE + path] = {'id': 'existing-' + str(i), 'at': self.since + 60}
        with patch('sys.argv', ['capture.py', '--scope', 'all', '--daily-batches', '--verify-only', '--state', 'state.json']), \
                patch('capture.Client', return_value=self.c), patch('capture.discover', return_value=[self.product]):
            self.assertEqual(main(), 1)
        self.assertEqual(self.posts(), [])


class PolicyScheduleTests(unittest.TestCase):
    def primary(self, **changes):
        from capture_schedule import PRIMARY_TITLE
        row = {'id': 123, 'event': 'schedule', 'head_branch': 'main',
               'display_title': PRIMARY_TITLE, 'created_at': '2026-11-05T09:01:00Z',
               'status': 'completed', 'conclusion': 'failure'}
        row.update(changes)
        return row

    def test_retry_only_for_same_day_failed_primary(self):
        import datetime as dt
        from capture_schedule import retry_decision
        today = dt.date(2026, 11, 5)
        cases = [({}, True), ({'conclusion': 'timed_out'}, True),
                 ({'conclusion': 'success'}, False), ({'conclusion': 'cancelled'}, False),
                 ({'status': 'in_progress', 'conclusion': None}, False),
                 ({'status': 'queued', 'conclusion': None}, False),
                 ({'created_at': '2026-11-04T09:01:00Z'}, False),
                 ({'display_title': 'Cart and policies - retry'}, False),
                 ({'event': 'workflow_dispatch'}, False), ({'head_branch': 'other'}, False)]
        for changes, expected in cases:
            with self.subTest(changes=changes):
                self.assertEqual(retry_decision([self.primary(**changes)], today)[0], expected)
        self.assertFalse(retry_decision([], today)[0])

    def test_latest_primary_success_prevents_retry_of_older_failure(self):
        import datetime as dt
        from capture_schedule import retry_decision
        runs = [self.primary(), self.primary(id=124, created_at='2026-11-05T10:00:00Z', conclusion='success')]
        self.assertFalse(retry_decision(runs, dt.date(2026, 11, 5))[0])

    def test_primary_manual_and_superseded_schedule_gate_without_network(self):
        from capture_schedule import main, PRIMARY_CRON
        with tempfile.TemporaryDirectory() as temp:
            for event, cron, expected in [('schedule', PRIMARY_CRON, True), ('workflow_dispatch', '', True),
                                           ('schedule', '7 0,6,12,18 * * *', False)]:
                output = Path(temp) / 'output'
                output.write_text('')
                with patch.dict(os.environ, {'GITHUB_EVENT_NAME': event, 'SCHEDULE': cron,
                        'GITHUB_OUTPUT': str(output), 'GITHUB_STEP_SUMMARY': ''}), \
                        patch('capture_schedule.urllib.request.urlopen') as request:
                    self.assertEqual(main(), 0)
                request.assert_not_called()
                self.assertEqual(output.read_text(), 'run_capture=' + str(expected).lower() + '\n')

    def test_retry_api_failure_never_authorizes_capture(self):
        from capture_schedule import main, RETRY_CRON
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / 'output'
            with patch.dict(os.environ, {'GITHUB_EVENT_NAME': 'schedule', 'SCHEDULE': RETRY_CRON,
                    'GITHUB_REPOSITORY': 'owner/repo', 'GITHUB_TOKEN': 'test',
                    'GITHUB_OUTPUT': str(output), 'GITHUB_STEP_SUMMARY': ''}), \
                    patch('capture_schedule.urllib.request.urlopen', side_effect=OSError('unavailable')):
                with self.assertRaises(OSError):
                    main()
            self.assertFalse(output.exists())

    def test_retry_reads_github_once_and_exports_decision(self):
        import datetime as dt
        from capture_schedule import main, RETRY_CRON
        today = dt.datetime.now(dt.timezone.utc).date().isoformat()
        with tempfile.TemporaryDirectory() as temp:
            for conclusion, expected in [('failure', True), ('success', False)]:
                output = Path(temp) / 'output'
                output.write_text('')
                response = Mock()
                response.__enter__ = Mock(return_value=response)
                response.__exit__ = Mock(return_value=False)
                response.read.return_value = json.dumps({'workflow_runs': [
                    self.primary(created_at=today + 'T09:00:00Z', conclusion=conclusion)]}).encode()
                with patch.dict(os.environ, {'GITHUB_EVENT_NAME': 'schedule', 'SCHEDULE': RETRY_CRON,
                        'GITHUB_REPOSITORY': 'owner/repo', 'GITHUB_TOKEN': 'test',
                        'GITHUB_OUTPUT': str(output), 'GITHUB_STEP_SUMMARY': ''}), \
                        patch('capture_schedule.urllib.request.urlopen', return_value=response) as request:
                    self.assertEqual(main(), 0)
                self.assertEqual(request.call_count, 1)
                self.assertEqual(output.read_text(), 'run_capture=' + str(expected).lower() + '\n')


if __name__ == '__main__': unittest.main()

