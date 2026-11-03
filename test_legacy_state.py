"""Compatibility and daily-boundary regressions, using synthetic local fixtures."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import capture
from capture import BASE, Client, Runner, StateContinuityError, RequestDeferred, epoch
from test_support import seed_test_state

URL = BASE + '/pages/warranty-policy'

class LegacyStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        old = os.getcwd()
        os.chdir(self.tmp.name)
        self.addCleanup(os.chdir, old)
        self.path = Path('state.json')
        self.client = Mock()
        self.client.verify.return_value = None

    def reject(self, raw):
        if raw is not None:
            self.path.write_bytes(raw)
        with self.assertRaises(StateContinuityError):
            Runner(self.client, self.path)
        self.assertEqual(self.client.mock_calls, [])
        if raw is not None:
            self.assertEqual(self.path.read_bytes(), raw)
        else:
            self.assertFalse(self.path.exists())

    def test_missing_refused_without_initialization(self):
        self.reject(None)

    def test_corrupt_wrong_empty_duplicate_unknown_refused(self):
        for raw in (b'', b'{', b'[]', b'null', b'{}', b'{"unexpected":{}}',
                    b'{"_next_submit":0,"_next_submit":1}', b'{"_next_submit":NaN}',
                    b'{"_next_submit":Infinity}', b'\xff'):
            with self.subTest(raw=raw): self.reject(raw)

    def test_invalid_top_cooldowns_refused(self):
        for key in ('_next_submit', '_archive_next_request_at', '_transport_cooldown_until'):
            for value in ('0', None, True, [], {}, -1, 1e20):
                with self.subTest(key=key,value=value):
                    self.reject(json.dumps({key:value}).encode())

    def test_invalid_pending_cooldowns_refused(self):
        for key in ('submitted_at', 'retry_eligible_at', 'server_retry_after_until',
                    'status_paused_until', 'retry_not_before', 'next_status_at'):
            for value in ('0', None, True, [], {}, -1):
                with self.subTest(key=key,value=value):
                    self.reject(json.dumps({URL:{'job_id':'saved',key:value}}).encode())

    def test_legacy_roundtrip_preserves_all_fields_without_sidecar(self):
        state = {'_next_submit':0, '_queues':{}, '_completed_jobs':[],
            URL:{'job_id':'saved', 'submitted_at':123, 'extra_unknown_evidence':{'keep':[1,2,3]}}}
        seed_test_state(self.path,state)
        runner = Runner(self.client,self.path)
        runner.save()
        self.assertEqual(json.loads(self.path.read_text()),state)
        self.assertFalse(Path('state.json.manifest.json').exists())
        summary=json.loads(Path('operational-report.json').read_text())
        self.assertEqual(summary['state_continuity']['historical_completeness'],'not-established')
        self.assertEqual(self.client.mock_calls,[])

    def test_missing_state_main_refuses_before_client_network(self):
        with patch('capture.Client',return_value=self.client), patch('sys.argv',['capture.py','--state','state.json']):
            self.assertEqual(capture.main(),2)
        self.assertEqual(self.client.mock_calls,[])

    def test_inspection_does_not_initialize_missing_state(self):
        with patch('sys.argv',['capture.py','--inspect-state','--state','state.json']):
            self.assertEqual(capture.main(),2)
        self.assertFalse(self.path.exists())

    def test_deleted_state_not_recreated_on_save(self):
        seed_test_state(self.path)
        runner=Runner(self.client,self.path)
        self.path.unlink()
        with self.assertRaises(StateContinuityError): runner.save()
        self.assertFalse(self.path.exists())

    def test_daily_verify_has_exact_utc_upper_bound(self):
        seed_test_state(self.path)
        with patch('capture.time.time',return_value=epoch('20261103120000')):
            runner=Runner(self.client,self.path,verify_only=True)
            runner.process(URL)
        self.client.verify.assert_called_once_with(URL,epoch('20261103000000'),until=epoch('20261103235959'))

    def test_physical_post_after_pacing_crosses_midnight_is_refused(self):
        client=Client()
        now=[epoch('20261103235959')]
        client.post_utc_day_end=epoch('20261104000000')
        client.archive_interval=120
        client.archive_next_request_at=now[0]+2
        client._request=Mock()
        def sleep(delay): now[0]+=delay
        with patch('capture.time.time',side_effect=lambda:now[0]),patch('capture.time.sleep',side_effect=sleep):
            with self.assertRaises(RequestDeferred): client.paced_request('https://web.archive.org/save/',data=b'url=x')
        client._request.assert_not_called()

    def test_one_due_status_read_and_restart_preserves_cadence(self):
        seed_test_state(self.path,{URL:{'job_id':'saved','submitted_at':epoch('20261103110000')}})
        now=epoch('20261103120000')
        self.client.json.return_value=(200,{}, {'status':'pending'})
        with patch('capture.time.time',return_value=now):
            runner=Runner(self.client,self.path)
            runner.process(URL)
            self.assertEqual(self.client.json.call_count,1)
            self.assertGreaterEqual(runner.state[URL]['next_status_at'],now+900)
            restarted=Runner(self.client,self.path)
            restarted.process(URL)
            self.assertEqual(self.client.json.call_count,1)
        with patch('capture.time.time',return_value=now+901):
            restarted=Runner(self.client,self.path)
            restarted.process(URL)
            self.assertEqual(self.client.json.call_count,2)

    def test_old_queue_and_completed_history_not_discarded_by_save(self):
        state={'_queues':{'all:20261102:capture':{'urls':[URL],'cursor':0,'results':[],'complete':False}},
               '_completed_jobs':[{'url':URL,'state':'pending'} for _ in range(60)]}
        seed_test_state(self.path,state)
        runner=Runner(self.client,self.path,verify_only=True)
        runner.run_queue([URL],scope='all')
        saved=json.loads(self.path.read_text())
        self.assertEqual(saved['_queues']['all:20261102:capture'],state['_queues']['all:20261102:capture'])
        self.assertEqual(saved['_completed_jobs'],state['_completed_jobs'])

    def test_invalid_outlink_cooldown_refused(self):
        for value in ('0', True, None, -1):
            batch={'root_url':URL,'children':{},'child_order':[],
                   'verification_cursor':0,'utc_day':1,'server_paused_until':value,
                   'post_state':'outcome_unknown','expansion_state':'unknown'}
            with self.subTest(value=value):
                self.reject(json.dumps({'_outlink_batches':{URL:batch}}).encode())

    def test_status_reservation_survives_long_global_wait(self):
        now=epoch('20261103120000')
        ready=now+1800
        seed_test_state(self.path,{URL:{'job_id':'saved','submitted_at':now-100},
                                  '_archive_next_request_at':ready})
        def observe(*args,**kwargs):
            saved=json.loads(self.path.read_text())
            self.assertGreaterEqual(saved[URL]['next_status_at'],ready+900)
            return 200,{}, {'status':'pending'}
        self.client.json.side_effect=observe
        with patch('capture.time.time',return_value=now):
            runner=Runner(self.client,self.path)
            runner.process(URL)
        self.assertGreaterEqual(runner.state[URL]['next_status_at'],ready+900)

    def test_response_time_adds_full_poll_cadence_after_slow_status(self):
        now=[epoch('20261103120000')]
        seed_test_state(self.path,{URL:{'job_id':'saved','submitted_at':now[0]-100}})
        def observe(*args,**kwargs):
            now[0]+=1800
            return 200,{}, {'status':'pending'}
        self.client.json.side_effect=observe
        with patch('capture.time.time',side_effect=lambda:now[0]):
            runner=Runner(self.client,self.path)
            runner.process(URL)
        self.assertGreaterEqual(runner.state[URL]['next_status_at'],now[0]+900)

    def test_corrupt_replacement_refused_without_overwriting(self):
        seed_test_state(self.path)
        runner=Runner(self.client,self.path)
        self.path.write_bytes(b'{truncated')
        with self.assertRaises(StateContinuityError): runner.save()
        self.assertEqual(self.path.read_bytes(),b'{truncated')
        self.assertEqual(self.client.mock_calls,[])

    def test_valid_replacement_preserves_added_uncertain_job_and_journal(self):
        seed_test_state(self.path)
        runner=Runner(self.client,self.path)
        changed={'_next_submit':0,URL:{'job_id':None,'submitted_at':123},
            '_outlink_batches':{URL:{'root_url':URL,'children':{},'child_order':[],
                'verification_cursor':0,'utc_day':1,'post_state':'outcome_unknown',
                'expansion_state':'unknown'}}}
        raw=json.dumps(changed).encode();self.path.write_bytes(raw)
        with self.assertRaises(StateContinuityError): runner.save()
        self.assertEqual(self.path.read_bytes(),raw)
        self.assertEqual(self.client.mock_calls,[])

    def test_consecutive_normal_saves_update_loaded_digest(self):
        seed_test_state(self.path)
        runner=Runner(self.client,self.path)
        for value in (1,2,3):
            runner.state['_next_submit']=value;runner.save()
        self.assertEqual(json.loads(self.path.read_text())['_next_submit'],3)
        restarted=Runner(self.client,self.path);restarted.save()
        self.assertEqual(json.loads(self.path.read_text())['_next_submit'],3)

    def test_midnight_during_attempt_persistence_prevents_physical_post(self):
        client=Client();now=[epoch('20261103235959')]
        client.post_utc_day_end=epoch('20261104000000');client._request=Mock()
        attempts=[]
        def persist(attempt):
            attempts.append(attempt);now[0]+=2
        client.post_attempt_callback=persist
        with patch('capture.time.time',side_effect=lambda:now[0]):
            with self.assertRaises(RequestDeferred):
                client.paced_request('https://web.archive.org/save/',data=b'url=x')
        self.assertEqual(len(attempts),1);client._request.assert_not_called()

    def test_missing_or_invalid_outlink_dispatch_and_mapping_refused(self):
        for key in ('post_state','expansion_state'):
            for value in ('MISSING',None,'unexpected',[],True):
                batch={'root_url':URL,'children':{},'child_order':[],
                    'verification_cursor':0,'utc_day':1,'post_state':'outcome_unknown',
                    'expansion_state':'unknown'}
                if value=='MISSING':batch.pop(key)
                else:batch[key]=value
                with self.subTest(key=key,value=value):
                    self.reject(json.dumps({'_outlink_batches':{URL:batch}}).encode())
