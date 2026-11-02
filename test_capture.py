import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
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

class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.old = os.getcwd(); os.chdir(self.tmp.name); self.addCleanup(os.chdir, self.old)
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

    def test_pilot_main_host_pause_has_one_submission_only(self):
        from capture import main
        self.c.json.return_value = (429, {'retry-after': '660'},
                                    {'status_ext': 'error:host-crawling-paused'})
        with patch('sys.argv', ['capture.py', '--scope', 'pilot']), patch('capture.Client', return_value=self.c):
            self.assertEqual(main(), 1)
        self.assertEqual(self.c.json.call_count, 1)
        self.assertEqual(self.c.verify.call_count, 1)
        self.assertEqual(self.c.json.call_args.args[0], 'https://web.archive.org/save/')
        saved = json.loads(Path('.capture-state/pending.json').read_text())
        self.assertGreater(saved['_next_submit'], time.time() + 659)
        report = json.loads(Path('capture-report.json').read_text())
        self.assertEqual(len(report), 1)
        self.assertEqual(report[0]['state'], 'deferred')

    def old_success_state(self):
        ts = timestamp(self.r.since - 60)
        self.r.state[URL] = {'job_id': 'saved-job', 'submitted_at': time.time(),
            'status_evidence': {'status': 'success', 'original_url_matches': True,
                               'timestamp': ts}}
        self.r.save()
        evidence = {'timestamp': ts, 'original': URL, 'statuscode': '200',
                    'mimetype': 'text/html', 'replay_url': 'https://web.archive.org/web/' + ts + 'id_/' + URL}
        self.c.verify.side_effect = lambda url, since, until=None: evidence if until is not None else None
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
        self.assertEqual(len([c for c in self.c.verify.call_args_list if c.kwargs.get('until')]), 1)

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
        self.assertEqual(discover(self.c), [BASE + '/collections/all', BASE + '/collections/all?page=2',
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

    def test_incomplete_main_returns_nonzero(self):
        from capture import main
        with patch('sys.argv', ['capture.py', '--verify-only', '--scope', 'pilot']), patch('capture.Client', return_value=self.c):
            self.assertEqual(main(), 1)

class WorkflowTests(unittest.TestCase):
    def test_recovery_cache_is_not_promoted_to_production(self):
        workflow = (Path(__file__).resolve().parent / '.github/workflows/wayback-snapshot.yml').read_text()
        self.assertIn("key: ${{ inputs.recovery_cache_key && 'wayback-recovery-v1' || 'wayback-state-v1' }}", workflow)
        self.assertIn("restore-keys: ${{ !inputs.recovery_cache_key && 'wayback-state-v1-' || '' }}", workflow)
        self.assertIn('args+=(--poll-only)', workflow)

if __name__ == '__main__': unittest.main()
