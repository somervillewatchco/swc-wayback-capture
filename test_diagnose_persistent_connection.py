import http.client
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch
import diagnose_persistent_connection as d

class Response:
 def __init__(self,body=b'{"status":"ok"}',status=200,headers=None,remaining=0):
  self.status=status;self.version=11;self.will_close=False;self.headers=headers or {};self.parts=[body,b''];self.length=remaining
 def getheader(self,key,default=None):return self.headers.get(key,default)
 def read(self,n):return self.parts.pop(0) if self.parts else b''
 def close(self):pass

class FakeConnection:
 def __init__(self,responses):self.responses=responses;self.sock=object();self.connect_count=0;self.calls=[];self.now=lambda:0
 def request(self,*args,**kwargs):
  self.connect_count=1;self.calls.append((args,kwargs,self.now()))
 def getresponse(self):
  response=self.responses.pop(0)
  if isinstance(response,Exception):raise response
  return response
 def close(self):pass

class PersistentTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.old=os.getcwd();os.chdir(self.tmp.name);self.now=0
  self.env=patch.dict(os.environ,{},clear=True);self.env.start()
  self.clock=patch.object(d.time,'monotonic',side_effect=lambda:self.now);self.clock.start()
  self.sleep=patch.object(d.time,'sleep',side_effect=self.advance);self.sleep.start()
 def tearDown(self):
  self.sleep.stop();self.clock.stop();self.env.stop();os.chdir(self.old);self.tmp.cleanup()
 def advance(self,n):self.now+=n
 def run_with(self,responses):
  conn=FakeConnection(responses);conn.now=lambda:self.now
  with patch.object(d,'OneConnection',return_value=conn):r=d.run()
  return r,conn
 def test_valid_pair_one_connection_fixed_get_noauth_and_fullread_wait(self):
  first=Response();original=first.read
  def read(n):self.now+=5;return original(n)
  first.read=read
  r,c=self.run_with([first,Response(b'[]')])
  self.assertEqual(r['outcome'],'reuse_observed');self.assertEqual(r['connect_count'],1)
  self.assertEqual(len(c.calls),2);self.assertGreaterEqual(c.calls[1][2],130)
  self.assertEqual(c.calls[0][0],('GET','/save/status/system'))
  self.assertTrue(c.calls[1][0][1].startswith('/cdx/search/cdx?'))
  for args,kwargs,_ in c.calls:
   self.assertEqual(args[0],'GET');self.assertNotIn('Authorization',kwargs['headers']);self.assertNotIn('Connection',kwargs['headers'])
  self.assertTrue(all(x['socket_identity_same'] for x in r['requests']))
 def test_proxy_presence_even_empty_aborts_before_constructor(self):
  for name in d.PROXY_NAMES:
   with patch.dict(os.environ,{name:''}),patch.object(d,'OneConnection') as conn:
    r=d.run();conn.assert_not_called();self.assertEqual(r['connect_count'],0)
 def test_server_refusal_retryafter_and_close_stop_all_followups(self):
  for response in [Response(status=403),Response(status=429),Response(status=503),Response(headers={'Retry-After':'900'}),Response(headers={'Connection':'close'})]:
   r,c=self.run_with([response]);self.assertEqual(len(c.calls),1);self.assertEqual(r['outcome'],'inconclusive')
  response=Response();response.will_close=True
  r,c=self.run_with([response]);self.assertEqual(len(c.calls),1)
 def test_oversized_incomplete_malformed_and_wrong_schema_stop(self):
  for response in [Response(b'x'*(d.LIMIT+1)),Response(remaining=4),Response(b'SECRET malformed'),Response(b'[]')]:
   r,c=self.run_with([response]);self.assertEqual(len(c.calls),1);self.assertEqual(r['outcome'],'inconclusive')
   self.assertNotIn('SECRET',json.dumps(r))
 def test_incomplete_chunked_read_and_read_timeout_stop(self):
  for exc in (http.client.IncompleteRead(b'SECRET'),TimeoutError('SECRET')):
   response=Response();response.read=Mock(side_effect=exc)
   r,c=self.run_with([response]);self.assertEqual(len(c.calls),1);self.assertNotIn('SECRET',json.dumps(r))
 def test_idle_close_no_reconnect_or_shorter_wait(self):
  conn=FakeConnection([Response()]);conn.now=lambda:self.now
  def sleep(n):self.now+=n;conn.sock=None
  with patch.object(d,'OneConnection',return_value=conn),patch.object(d.time,'sleep',side_effect=sleep):r=d.run()
  self.assertEqual(len(conn.calls),1);self.assertEqual(self.now,120)
  self.assertEqual(r['stop_reason'],'socket_not_preserved_no_reconnect')
 def test_silent_idle_eof_and_send_failure_never_retry(self):
  r,c=self.run_with([Response(),http.client.RemoteDisconnected('SECRET')])
  self.assertEqual(len(c.calls),2);self.assertEqual(c.connect_count,1);self.assertNotIn('SECRET',json.dumps(r))
  conn=FakeConnection([]);conn.request=Mock(side_effect=BrokenPipeError('SECRET'))
  with patch.object(d,'OneConnection',return_value=conn):r=d.run()
  self.assertEqual(conn.request.call_count,1);self.assertNotIn('SECRET',json.dumps(r))
 def test_real_connection_class_forbids_second_connect_before_network(self):
  conn=d.OneConnection()
  with patch.object(http.client.HTTPSConnection,'connect') as parent:
   conn.connect();self.assertEqual(conn.auto_open,0)
   with self.assertRaises(d.ReconnectForbidden):conn.connect()
   parent.assert_called_once()
  with self.assertRaises(http.client.NotConnected):conn.request('GET','/save/status/system')
 def test_multiple_dns_addresses_abort_before_any_tcp_attempt(self):
  conn=d.OneConnection()
  answers=[(2,1,6,'',('192.0.2.1',443)),(2,1,6,'',('192.0.2.2',443))]
  with patch.object(d.socket,'getaddrinfo',return_value=answers),patch.object(d.socket,'socket') as factory:
   with self.assertRaises(d.UnsupportedResolution):conn.create_single_socket(('web.archive.org',443))
   factory.assert_not_called();self.assertEqual(conn.connect_count,0)
 def test_one_distinct_dns_address_exactly_one_socket_attempt_no_fallback(self):
  conn=d.OneConnection();answer=(2,1,6,'',('192.0.2.1',443));sock=Mock()
  sock.connect.side_effect=TimeoutError('SECRET')
  with patch.object(d.socket,'getaddrinfo',return_value=[answer,answer]) as dns,patch.object(d.socket,'socket',return_value=sock):
   with self.assertRaises(TimeoutError):conn.create_single_socket(('web.archive.org',443))
   with self.assertRaises(d.ReconnectForbidden):conn.create_single_socket(('web.archive.org',443))
   dns.assert_called_once_with('web.archive.org',443,0,d.socket.SOCK_STREAM)
   sock.connect.assert_called_once_with(('192.0.2.1',443));sock.close.assert_called_once()
   self.assertEqual(conn.connect_count,1)

 def test_real_httpresponse_incomplete_length_detectable(self):
  sock=Mock();sock.makefile.return_value=io.BytesIO(b'HTTP/1.1 200 OK\r\nContent-Length: 20\r\n\r\n{}')
  response=http.client.HTTPResponse(sock);response.begin()
  r,c=self.run_with([response]);self.assertEqual(r['stop_reason'],'incomplete_declared_body');self.assertEqual(len(c.calls),1)
 def test_real_httpresponse_content_length_and_chunked_fully_consumed(self):
  body=b'{"status":"ok"}'
  for wire in (b'HTTP/1.1 200 OK\r\nContent-Length: '+str(len(body)).encode()+b'\r\n\r\n'+body,
               b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n'+format(len(body),'x').encode()+b'\r\n'+body+b'\r\n0\r\n\r\n'):
   sock=Mock();sock.makefile.return_value=io.BytesIO(wire)
   response=http.client.HTTPResponse(sock);response.begin()
   r,c=self.run_with([response,Response(b'[]')])
   self.assertEqual(r['outcome'],'reuse_observed');self.assertTrue(r['requests'][0]['body_fully_consumed'])
   self.assertEqual(r['requests'][0]['bytes_read'],len(body));self.assertEqual(len(c.calls),2)
 def test_http10_even_keepalive_is_not_this_http11_experiment(self):
  response=Response(headers={'Connection':'keep-alive'});response.version=10
  r,c=self.run_with([response]);self.assertEqual(len(c.calls),1);self.assertEqual(r['outcome'],'inconclusive')

 def test_workflow_is_manual_no_state_or_secrets(self):
  workflow=(Path(__file__).parent/'.github/workflows/wayback-persistent-diagnostic.yml').read_text()
  self.assertIn('timeout-minutes: 10',workflow);self.assertIn('group: wayback-snapshot',workflow)
  for bad in ('secrets.','actions/cache','schedule:','python capture.py'):self.assertNotIn(bad,workflow)

if __name__=='__main__':unittest.main()
