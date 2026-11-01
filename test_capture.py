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

    def test_pending_never_verified_and_saved(self):
        self.c.json.side_effect = [(200, {}, {'job_id': 'job'})] + [(200, {}, {'status': 'pending'})]*12
        self.assertEqual(self.r.process(URL)['state'], 'pending')
        self.assertEqual(json.loads(Path('state.json').read_text())[URL]['job_id'], 'job')

    def test_old_success_rejected(self):
        self.c.json.side_effect = [(200, {}, {'job_id': 'job'}), (200, {}, {'status':'success','original_url':URL,'timestamp':'20200101000000'})]
        self.assertEqual(self.r.process(URL)['state'], 'failed')

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
        self.assertEqual(self.r.process(URL)['state'], 'failed')

    def test_incomplete_main_returns_nonzero(self):
        from capture import main
        with patch('sys.argv', ['capture.py', '--verify-only', '--scope', 'pilot']), patch('capture.Client', return_value=self.c):
            self.assertEqual(main(), 1)

if __name__ == '__main__': unittest.main()
