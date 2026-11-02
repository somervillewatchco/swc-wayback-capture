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

    def test_terminal_failure_audit_is_bounded_and_excludes_arbitrary_state(self):
        self.r.state['_completed_jobs'] = [{'old': i} for i in range(50)]
        self.r.state[URL] = {'job_id': 'accepted-job', 'submitted_at': time.time(), 'raw_secret': 'SECRET'}
        self.r.terminal_failure(URL, reason='SPN error', status_ext='error:bad-request')
        self.assertEqual(len(self.r.state['_completed_jobs']), 50)
        self.assertEqual(self.r.state['_completed_jobs'][0], {'old': 1})
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
        def healthy_cdx(*args):
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
        def healthy(*args):
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
            self.assertEqual(main(), 1)
            process.assert_not_called()
            self.assertEqual(result.call_args.args[1], 'discovery-incomplete')

    def test_known_markdown_target_reported_without_submission(self):
        from capture import main
        with tempfile.TemporaryDirectory() as tmp:
            old = os.getcwd()
            try:
                os.chdir(tmp)
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
        key = 'all:' + timestamp(self.r.since)[:8] + ':capture'
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
                self.assertEqual(main(), 0)
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
        self.assertNotIn(key, self.r.state['_queues'])
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
        for url in before:self.r.process(url)
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
        self.r.state['_queues']={'all:20261102:capture':{'cursor':5,'complete':False}}
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

if __name__ == '__main__': unittest.main()
