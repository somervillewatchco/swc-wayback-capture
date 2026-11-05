import errno
import io
import json
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import Mock, patch
import urllib.error
import diagnose_transport as d


class DiagnosticTests(unittest.TestCase):
    def response(self, code=200, body=b'{"status":"ok"}', headers=None):
        response = Mock(code=code, headers=headers or {'Content-Type': 'application/json'})
        response.read.return_value = body
        return response

    def test_fixed_public_get_only_no_auth_headers(self):
        opener = Mock(); opener.open.return_value = self.response()
        for name, url in d.endpoints():
            result = d.probe(name, url, d.endpoints(), opener=opener)
            request = opener.open.call_args.args[0]
            self.assertEqual(request.get_method(), 'GET')
            self.assertIsNone(request.data)
            self.assertIsNone(request.get_header('Authorization'))
            self.assertIsNone(request.get_header('Cookie'))
            self.assertNotIn('secret', json.dumps(result))
            self.assertEqual(result['phase'], 'complete')
            opener.open.return_value.read.assert_called_with(d.READ_LIMIT)

    def test_cannot_add_arbitrary_url_or_capture_endpoint(self):
        for url in ('https://evil.example/', 'https://web.archive.org/save/', 'http://web.archive.org/save/status/system'):
            opener = Mock()
            with self.assertRaises(ValueError):
                d.probe('archive_system_status', url, [('archive_system_status', url)], opener=opener)
            opener.open.assert_not_called()

    def test_no_redirects(self):
        self.assertIsNone(d.NoRedirect().redirect_request(None, None, 302, None, {}, 'https://evil.example/'))

    def test_connection_reason_errno_no_message_or_paths(self):
        opener = Mock()
        opener.open.side_effect = urllib.error.URLError(ConnectionRefusedError(errno.ECONNREFUSED, 'SECRET_URL_PASSWORD'))
        result = d.probe(*d.endpoints()[0], d.endpoints(), opener=opener)
        self.assertEqual(result['outcome'], 'transport_error')
        self.assertEqual(result['phase'], 'open')
        self.assertEqual(result['errors'][1]['errno'], errno.ECONNREFUSED)
        self.assertEqual(result['errors'][1]['class'], 'ConnectionRefusedError')
        encoded = json.dumps(result)
        self.assertNotIn('SECRET_URL_PASSWORD', encoded)
        self.assertNotIn(__file__, encoded)

    def test_read_failure_has_read_phase(self):
        opener = Mock(); opener.open.return_value = self.response()
        opener.open.return_value.read.side_effect = TimeoutError('SECRET')
        result = d.probe(*d.endpoints()[0], d.endpoints(), opener=opener)
        self.assertEqual(result['phase'], 'read')
        self.assertEqual(result['errors'][0]['class'], 'TimeoutError')
        self.assertNotIn('SECRET', json.dumps(result))

    def test_http_error_retains_status_not_raw_body_headers(self):
        opener = Mock()
        opener.open.side_effect = urllib.error.HTTPError(d.endpoints()[0][1], 429, 'SECRET',
            {'Content-Type': 'SECRET', 'Retry-After': '120'}, io.BytesIO(b'SECRET'))
        result = d.probe(*d.endpoints()[0], d.endpoints(), opener=opener)
        self.assertEqual(result['http_status'], 429)
        self.assertEqual(result['retry_after_seconds'], 120)
        self.assertEqual(result['content_type'], 'other')
        self.assertNotIn('SECRET', json.dumps(result))

    def test_dns_reports_family_count_no_addresses(self):
        with patch('diagnose_transport.socket.getaddrinfo', return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('192.0.2.33', 443))]):
            result = d.dns_probe('web.archive.org')
        self.assertEqual(result['families'], ['AF_INET'])
        self.assertEqual(result['answer_count'], 1)
        self.assertNotIn('192.0.2.33', json.dumps(result))
        with self.assertRaises(ValueError): d.dns_probe('evil.example')

    def test_two_passes_presence_only_no_secrets_or_capture_cache_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = os.getcwd()
            try:
                os.chdir(tmp)
                with patch.dict(os.environ, {'HTTPS_PROXY': 'https://SECRET@proxy.invalid', 'ARCHIVE_ORG_SECRET_KEY': 'SECRET'}), patch('diagnose_transport.time.sleep') as sleep, patch('diagnose_transport.dns_probe', return_value={'phase': 'dns'}), patch('diagnose_transport.probe', return_value={'outcome': 'http_response', 'http_status': 200}) as probe:
                    result = d.run_diagnostics()
                self.assertEqual(probe.call_count, 8)
                self.assertEqual(len(result['passes']), 2)
                sleep.assert_any_call(30)
                self.assertTrue(result['proxy_environment_present']['HTTPS_PROXY'])
                self.assertNotIn('SECRET', Path('transport-report.json').read_text())
                self.assertEqual(sorted(p.name for p in Path('.').iterdir()), ['transport-report.json'])
            finally:
                os.chdir(old)

    def test_host_rate_limit_skips_remaining_archive_gets(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = os.getcwd()
            try:
                os.chdir(tmp)
                with patch('diagnose_transport.time.sleep'), patch('diagnose_transport.dns_probe', return_value={}), patch('diagnose_transport.probe', side_effect=[{'http_status': 429, 'retry_after_seconds': 120}, {'http_status': 200}, {'http_status': 200}]) as probe:
                    result = d.run_diagnostics()
                self.assertEqual(probe.call_count, 3)
                self.assertEqual(result['passes'][1]['requests'][0]['outcome'], 'skipped_host_retry_after')
            finally:
                os.chdir(old)

    def test_workflow_diagnostic_never_enters_capture_cache_steps(self):
        workflow = (Path(__file__).resolve().parent / '.github/workflows/wayback-snapshot.yml').read_text()
        self.assertIn("if: ${{ steps.schedule_gate.outputs.run_capture == 'true' && !inputs.diagnostic }}\n        env:\n          ARCHIVE_ORG_ACCESS_KEY", workflow)
        self.assertIn("Restore unresolved jobs and host cooldown\n        if: ${{ steps.schedule_gate.outputs.run_capture == 'true' && !inputs.diagnostic }}", workflow)
        self.assertIn("Preserve unresolved jobs even when verification fails\n        if: ${{ always() && steps.schedule_gate.outputs.run_capture == 'true' && !inputs.diagnostic }}", workflow)
        diagnostic = workflow.split('      - name: Public read-only transport diagnostics\n', 1)[1].split('      - name:', 1)[0]
        self.assertNotIn('ARCHIVE_ORG', diagnostic)
        self.assertNotIn('capture.py', diagnostic)
        self.assertIn('timeout-minutes: 5', diagnostic)
        self.assertIn('run: python diagnose_transport.py', diagnostic)


if __name__ == '__main__':
    unittest.main()
