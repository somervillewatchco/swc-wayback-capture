import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import urllib.error
import diagnose_transport_paced as d

class PacedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.old = os.getcwd(); os.chdir(self.tmp.name)
        self.now = 0
        self.clock = patch.object(d.time, 'monotonic', side_effect=lambda: self.now); self.clock.start()
        self.sleep = patch.object(d.time, 'sleep', side_effect=self.advance); self.sleep.start()
        self.dns = patch.object(d, 'dns_probe', return_value={'outcome': 'resolved'}); self.dns.start()
    def tearDown(self):
        self.dns.stop(); self.sleep.stop(); self.clock.stop(); os.chdir(self.old); self.tmp.cleanup()
    def advance(self, seconds): self.now += seconds
    def success(self, name, url, allowed):
        self.now += 2
        return {'endpoint': name, 'method': 'GET', 'http_status': 200, 'outcome': 'http_response'}
    def test_six_archive_gets_same_order_two_minute_minimum_then_one_control(self):
        with patch.object(d, 'probe', side_effect=self.success) as probe:
            report = d.run_diagnostics()
        expected = list(d.endpoints()[:3]) * 2 + [d.endpoints()[3]]
        self.assertEqual([(c.args[0], c.args[1]) for c in probe.call_args_list], expected)
        starts = [r['started_offset_seconds'] for r in report['requests'][:6]]
        self.assertTrue(all(b-a >= 120 for a,b in zip(starts, starts[1:])))
        self.assertGreaterEqual(report['elapsed_seconds'], 600)
        self.assertLess(report['elapsed_seconds'], 900)
        self.assertEqual(report['policy']['automatic_retries'], 0)
    def test_each_stop_condition_prevents_every_remaining_archive_request(self):
        for failure in ({'outcome':'transport_error'}, {'outcome':'http_response', 'http_status':403, 'retry_after_seconds':1800}, {'outcome':'http_response', 'http_status':429, 'retry_after_seconds':7200}, {'outcome':'http_response', 'http_status':503, 'retry_after_seconds':600}, {'outcome':'http_response', 'http_status':200, 'close_errors':[{'class':'TimeoutError'}]}):
            with patch.object(d, 'probe', side_effect=[failure, {'outcome':'http_response', 'http_status':200}]) as probe:
                report = d.run_diagnostics()
            self.assertEqual(probe.call_count, 2)
            self.assertEqual(probe.call_args_list[-1].args[0], 'storefront_refund')
            self.assertTrue(report['archive_stopped'])
            self.assertEqual(report['archive_remaining_not_sent'], 5)
            if 'retry_after_seconds' in failure:
                self.assertEqual(report['archive_retry_after_seconds'], failure['retry_after_seconds'])
    def test_failure_on_later_request_stops_before_third(self):
        with patch.object(d, 'probe', side_effect=[{'http_status':200}, {'outcome':'transport_error'}, {'http_status':200}]) as probe:
            report = d.run_diagnostics()
        self.assertEqual(probe.call_count, 3)
        self.assertEqual(report['archive_remaining_not_sent'], 4)
    def test_probe_is_get_no_auth_no_body_and_existing_user_agent(self):
        response = Mock(code=200, headers={'Content-Type':'text/html'}); response.read.return_value=b'hello'
        opener=Mock(); opener.open.return_value=response
        with patch.dict(os.environ, {'ARCHIVE_ORG_ACCESS_KEY':'SECRET', 'ARCHIVE_ORG_SECRET_KEY':'SECRET'}):
            row=d.probe(*d.endpoints()[2], d.endpoints(), opener=opener)
        request=opener.open.call_args.args[0]
        self.assertEqual(request.get_method(),'GET'); self.assertIsNone(request.data)
        self.assertEqual(request.get_header('User-agent'),'SWC-Archive-Verification/1.0')
        self.assertNotIn('Authorization',dict(request.header_items()))
        self.assertNotIn('SECRET',json.dumps(row))
        response.read.assert_called_once_with(d.READ_LIMIT)
    def test_error_errno_sanitized_without_message_and_single_open(self):
        opener=Mock(); opener.open.side_effect=urllib.error.URLError(ConnectionRefusedError(111,'SECRET'))
        row=d.probe(*d.endpoints()[0],d.endpoints(),opener=opener)
        self.assertEqual(opener.open.call_count,1)
        self.assertEqual(row['outcome'],'transport_error')
        self.assertEqual(row['errors'][1]['errno'],111)
        self.assertNotIn('SECRET',json.dumps(row))
    def test_403_retry_after_parsed_and_no_redirect(self):
        response=Mock(code=403,headers={'Retry-After':'1800'});response.read.return_value=b''
        opener=Mock();opener.open.return_value=response
        row=d.probe(*d.endpoints()[0],d.endpoints(),opener=opener)
        self.assertEqual(row['retry_after_seconds'],1800)
        self.assertIsNone(d.NoRedirect().redirect_request(None,None,302,'',{},'https://example.com'))
    def test_any_response_retry_after_is_parsed_including_http_date(self):
        from email.utils import formatdate
        for code in (200, 503):
            response=Mock(code=code,headers={'Retry-After':formatdate(d.time.time()+1800,usegmt=True)})
            response.read.return_value=b''
            opener=Mock();opener.open.return_value=response
            row=d.probe(*d.endpoints()[2],d.endpoints(),opener=opener)
            self.assertGreaterEqual(row['retry_after_seconds'],1798)

    def test_endpoint_allowlist_rejects_arbitrary_url(self):
        with self.assertRaises(ValueError): d.probe('evil','https://example.com/',(('evil','https://example.com/'),))
    def test_workflow_read_only_no_cache_credentials_or_capture(self):
        workflow=(Path(__file__).parent/'.github/workflows/wayback-transport-paced.yml').read_text()
        self.assertIn('timeout-minutes: 15',workflow)
        self.assertIn('group: wayback-snapshot',workflow)
        for forbidden in ('secrets.', 'actions/cache', 'python capture.py', 'schedule:'):
            self.assertNotIn(forbidden,workflow)

if __name__=='__main__': unittest.main()
