import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock,patch
import compare_archive_clients as c

class ComparisonTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.old=os.getcwd();os.chdir(self.tmp.name);self.now=0;self.calls=[]
  self.clock=patch.object(c.time,'monotonic',side_effect=lambda:self.now);self.clock.start()
  self.sleep=patch.object(c.time,'sleep',side_effect=self.advance);self.sleep.start()
  self.version=patch.object(c.subprocess,'run',return_value=Mock(stdout='curl 8.1 OpenSSL/3.1',returncode=0));self.version.start()
 def tearDown(self):
  self.version.stop();self.sleep.stop();self.clock.stop();os.chdir(self.old);self.tmp.cleanup()
 def advance(self,n):self.now+=n
 def response(self,client):
  def get(url):
   self.calls.append((client,url,self.now));self.now+=1
   return {'client':client,'http_status':200,'outcome':'http_response'}
  return get
 def test_exact_eight_order_and_pacing(self):
  with patch.object(c,'python_get',side_effect=self.response('python')),patch.object(c,'curl_get',side_effect=self.response('curl')):
   r=c.run_comparison()
  self.assertEqual(len(self.calls),8)
  self.assertEqual([x[0] for x in self.calls],['python','curl','python','curl','curl','python','curl','python'])
  self.assertTrue(all(b[2]-a[2]>=120 for a,b in zip(self.calls,self.calls[1:])))
  self.assertEqual(len(r['requests']),8)
 def test_transport_failure_allows_only_preplanned_other_client_pair(self):
  with patch.object(c,'python_get',return_value={'client':'python','outcome':'transport_error'}),patch.object(c,'curl_get',side_effect=self.response('curl')) as curl:
   r=c.run_comparison()
  self.assertEqual(len(r['requests']),2);curl.assert_called_once_with(c.FIXED_ENDPOINTS[0][1])
  self.assertGreaterEqual(self.calls[0][2],120)
 def test_second_member_failure_stops_without_extra_comparison(self):
  with patch.object(c,'python_get',side_effect=self.response('python')),patch.object(c,'curl_get',return_value={'outcome':'transport_error'}):r=c.run_comparison()
  self.assertEqual(len(r['requests']),2)
 def test_refusal_or_any_retryafter_has_no_paired_followup(self):
  for row in ({'http_status':403},{'http_status':429},{'http_status':503,'retry_after_seconds':900}):
   with patch.object(c,'python_get',return_value=row),patch.object(c,'curl_get') as curl:r=c.run_comparison()
   curl.assert_not_called();self.assertEqual(len(r['requests']),1)
 def test_curl_command_no_secrets_post_redirect_retries_and_sanitized_peer(self):
  self.version.stop()
  def run(args,**kwargs):
   Path(args[args.index('--dump-header')+1]).write_text('HTTP/2 503\nRetry-After: 900\nX-secret: SECRET\n')
   return subprocess.CompletedProcess(args,0,'503|2|0|93.184.216.34','SECRET')
  with patch.object(c.subprocess,'run',side_effect=run) as run:
   row=c.curl_get(c.FIXED_ENDPOINTS[0][1])
  args=run.call_args.args[0]
  self.assertEqual(args[:2],['curl','-q']);self.assertEqual(args[args.index('--request')+1],'GET')
  self.assertEqual(args[args.index('--retry')+1],'0')
  for forbidden in ('--location','--insecure','--netrc','--user','--data','--verbose'):self.assertNotIn(forbidden,args)
  self.assertEqual(row['http_version'],'2');self.assertEqual(row['peer_family'],'IPv4')
  self.assertEqual(row['retry_after_seconds'],900)
  self.assertNotIn('SECRET',json.dumps(row));self.assertNotIn('93.184',json.dumps(row))
 def test_python_headers_no_auth_and_http_version(self):
  response=Mock(code=200,version=11,headers={});response.read.return_value=b''
  response.fp.raw._sock.getpeername.return_value=('2001:db8::1',443)
  response.fp.raw._sock.version.return_value='TLSv1.3'
  opener=Mock();opener.open.return_value=response
  with patch.object(c.urllib.request,'build_opener',return_value=opener):row=c.python_get(c.FIXED_ENDPOINTS[0][1])
  req=opener.open.call_args.args[0]
  self.assertEqual(req.get_method(),'GET');self.assertIsNone(req.data)
  self.assertEqual(req.get_header('User-agent'),c.UA);self.assertNotIn('Authorization',dict(req.header_items()))
  self.assertEqual(row['http_version'],'1.1');self.assertEqual(row['tls_version'],'TLSv1.3')
  self.assertEqual(row['peer_family'],'IPv6');self.assertNotIn('2001:',json.dumps(row))
 def test_arbitrary_endpoints_rejected_before_io(self):
  for method in (c.python_get,c.curl_get):
   with self.assertRaises(ValueError):method('https://example.org/SECRET')
 def test_workflow_no_credentials_cache_or_schedules(self):
  text=(Path(__file__).parent/'.github/workflows/wayback-client-comparison.yml').read_text()
  self.assertIn('group: wayback-snapshot',text)
  for bad in ('secrets.','actions/cache','schedule:','python capture.py'):self.assertNotIn(bad,text)

if __name__=='__main__':unittest.main()
