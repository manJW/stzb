"""Only a scope-verified, wire-correlated singleton can be rejected permanently."""
import json
import tempfile
import http.client
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

from test_support import Archive
from test_intelligence import battle, complete_battle, world
from evidence_reader import ReportEvidenceReader
from report_access import rejection, request_ids, ReportAccess


class ReportAccessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.archive = Archive(Path(self.temp.name))
        self.access = self.archive.intelligence.report_access
        self.calls = []
        self.reader = ReportEvidenceReader(self.archive,SimpleNamespace(fetch_profiles=self.reply),lambda:None,False)
        self.reader.RESPONSE_TIMEOUT = 0
        self.reader.resume(200,continuous=True)
        self.mode = 'expired'

    def tearDown(self):
        self.reader.close(); self.archive.close(); self.temp.cleanup()

    def listed(self,bid):
        self.archive.ingest(92,[[battle(bid)]],time.time())

    def state(self,bid):
        row = self.archive.db.execute("SELECT state FROM report_read_jobs WHERE battle_id=? AND kind='detail'",(str(bid),)).fetchone()
        return row[0] if row else None

    def step(self):
        self.reader.not_before = 0
        self.reader._step()

    def reply(self,ids):
        self.calls.append(ids)
        at = time.time()
        if self.mode != 'unobserved':
            self.reader.on_request(10,[ids,0,0],at)
        if self.mode == 'gap':self.reader.on_gap()
        if self.mode == 'interleaved':self.reader.on_request(10,[[987654321],0,0],at+.001)
        if self.mode == 'scope_changed':self.archive.profile['union_id'] = 'different-organization'
        if self.mode == 'success':
            data = [[complete_battle(bid) for bid in ids]]
            self.archive.ingest(10,data,at+.01);self.reader.on_message(10,data,at+.01)
        elif self.mode == 'partial':
            data = [[battle(bid) for bid in ids]]
            self.archive.ingest(10,data,at+.01);self.reader.on_message(10,data,at+.01)
        elif self.mode != 'timeout':
            self.reader.on_message(90002,[10,'无权查看该战报' if self.mode=='denied' else '战报已过期'],at+.01)

    def test_map_reference_is_not_proof_of_query_permission(self):
        self.archive.ingest(5028,world(bid=1),time.time())
        self.step()
        self.assertEqual(self.calls,[])
        self.assertEqual(self.archive.db.execute("SELECT state FROM report_read_jobs WHERE kind='profile'").fetchone()[0],'awaiting_scope')
        self.assertFalse(self.access.blocked(1))

    def test_current_list_admits_previously_unconfirmed_reference(self):
        self.archive.ingest(5028,world(bid=1),time.time())
        self.listed(1);self.mode='success'
        for _ in range(3):self.step()
        self.assertEqual(self.calls,[[1]])
        self.assertTrue(self.archive.intelligence.detail_received(1))

    def test_singleton_error_stops_only_that_report_and_preserves_all_data(self):
        self.listed(1);self.listed(2)
        self.archive.intelligence.queue_evidence('detail',1,priority=200)
        self.archive.ingest(11,{'battle_id':1,'report':'existing numeric body'},time.time())
        raw = self.archive.db.execute("SELECT data FROM battle_reports WHERE battle_id='1'").fetchone()[0]
        self.step();self.assertEqual(self.calls,[[1]])
        self.assertEqual(self.state(1),'unavailable');self.assertTrue(self.access.blocked(1))
        self.assertFalse(self.archive.intelligence.queue_evidence('detail',1,retry=True,priority=500))
        self.mode='success';self.step()
        self.assertEqual(self.calls,[[1],[2]]);self.assertEqual(self.state(2),'done')
        self.assertEqual(self.archive.db.execute("SELECT data FROM battle_reports WHERE battle_id='1'").fetchone()[0],raw)
        self.assertEqual(self.archive.intelligence.report(1)['full_report']['body'],'existing numeric body')
        audit=self.access.snapshot()['recent_attempts']
        self.assertEqual({r['battle_id']:r['outcome']for r in audit},{'1':'expired','2':'received'})
        self.assertEqual(json.loads(audit[0]['evidence'])['source'],'account_report_list')

    def test_repeated_focus_seed_and_list_cannot_restart_rejected_id(self):
        self.listed(1);self.step()
        for _ in range(4):
            self.listed(1)
            self.archive.intelligence.queue_evidence('detail',1,retry=True,priority=210)
            self.archive.intelligence.seed_alliance_details();self.step()
        self.assertEqual(self.calls,[[1]]);self.assertEqual(self.state(1),'unavailable')

    def test_positive_reply_recovers_rejected_report_without_an_extra_read(self):
        self.listed(1);self.step()
        self.archive.ingest(10,[[complete_battle(1)]],time.time())
        self.assertFalse(self.access.blocked(1));self.assertEqual(self.state(1),'done')
        self.step();self.assertEqual(self.calls,[[1]])

    def test_explicit_permission_denial_is_distinct_from_expiry(self):
        self.listed(1);self.mode='denied';self.step()
        self.assertEqual(self.access.row(1)['status'],'denied')

    def test_plain_timeout_never_proves_expiry_and_remains_retryable(self):
        self.listed(1);self.mode='timeout';self.step()
        self.assertEqual(self.state(1),'failed');self.assertFalse(self.access.blocked(1))
        with self.archive.db:self.archive.db.execute("UPDATE report_read_jobs SET updated_at=0")
        self.reader.retry_at=0;self.mode='success';self.step()
        self.assertEqual(self.state(1),'done');self.assertEqual(self.calls,[[1],[1]])

    def test_partial_reply_is_kept_and_does_not_disable_new_reports(self):
        self.listed(1);self.mode='partial';self.step()
        self.assertFalse(self.access.blocked(1));self.assertEqual(self.state(1),'failed')
        self.assertEqual(self.reader.failures,0)
        self.listed(2);self.mode='success';self.step()
        self.assertEqual(self.state(2),'done')

    def test_unobserved_outbound_cannot_blacklist_a_report(self):
        self.listed(1);self.mode='unobserved';self.step()
        self.assertFalse(self.access.blocked(1));self.assertEqual(self.state(1),'failed')
        self.assertFalse(self.reader.snapshot()['server_errors'][-1]['correlated'])

    def test_interleaved_manual_request_cannot_blacklist_the_owned_read(self):
        self.listed(1);self.mode='interleaved';self.step()
        self.assertFalse(self.access.blocked(1));self.assertEqual(self.state(1),'failed')

    def test_stream_gap_invalidates_error_attribution(self):
        self.listed(1);self.mode='gap';self.step()
        self.assertFalse(self.access.blocked(1))

    def test_role_scope_change_cannot_reject_the_new_scope(self):
        self.listed(1);self.mode='scope_changed';self.step()
        self.assertIsNone(self.access.row(1))
        self.assertEqual(self.archive.db.execute("SELECT COUNT(*) FROM report_read_access WHERE status='expired'").fetchone()[0],0)

    def test_rejection_survives_restart_but_visibility_requires_fresh_scope_check(self):
        self.listed(1);self.listed(2)
        self.archive.intelligence.queue_evidence('detail',1,priority=200);self.step()
        restarted=ReportAccess(self.archive)
        self.assertTrue(restarted.blocked(1));self.assertFalse(restarted.allowed(2))
        self.archive.profile['union_id']='new-organization'
        self.assertFalse(restarted.blocked(1));self.assertFalse(restarted.allowed(1))

    def test_returning_to_rejected_scope_cannot_reuse_another_scopes_queued_job(self):
        self.listed(1);self.step();original=self.archive.profile['union_id']
        self.archive.profile['union_id']='another-organization'
        self.listed(1);self.assertEqual(self.state(1),'queued')
        self.archive.profile['union_id']=original
        self.step()
        self.assertEqual(self.state(1),'unavailable');self.assertEqual(self.calls,[[1]])
        self.listed(2);self.mode='success';self.step()
        self.assertEqual(self.state(2),'done');self.assertEqual(self.calls,[[1],[2]])

    def test_import_or_member_upload_does_not_establish_local_query_permission(self):
        for bid,source in ((1,'replay'),(2,'member:sample')):
            self.archive.ingest(92,[[battle(bid)]],time.time(),source=source)
            self.assertFalse(self.access.allowed(bid))

    def test_old_but_currently_listed_report_is_not_banned_by_age(self):
        report=battle(1);report['time']=1
        self.archive.ingest(92,[[report]],time.time());self.mode='success';self.step()
        self.assertEqual(self.calls,[[1]]);self.assertEqual(self.state(1),'done')

    def test_error_and_request_allowlists_are_strict(self):
        self.assertEqual(rejection([10,'战报已过期'])[0],'expired')
        for value in ([11,'战报已过期'],[10,'服务器繁忙'],[92,'战报已过期'],{'error':'战报已过期'}):
            self.assertIsNone(rejection(value))
        self.assertEqual(request_ids([[1],0,0]),['1'])
        for value in ([[1,1],0,0],[[0],0,0],[[True],0,0],[[1],1,0],['wrong',0,0]):
            self.assertIsNone(request_ids(value))

    def test_previous_or_unrelated_error_does_not_finish_the_new_read(self):
        self.reader.on_message(90002,[10,'战报已过期'],time.time())
        self.listed(1);self.mode='success';self.step()
        self.assertEqual(self.state(1),'done');self.assertFalse(self.access.blocked(1))

    def test_failure_of_old_reports_never_puts_latest_details_on_a_global_cooldown(self):
        self.listed(1);self.listed(2);self.mode='timeout'
        self.step();self.step()
        self.assertLess(self.reader.not_before,time.monotonic()+5)
        self.listed(3);self.mode='success';self.step()
        self.assertEqual(self.state(3),'done')

    def test_recent_selected_timeout_can_retry_but_confirmed_expiry_cannot(self):
        self.listed(1);self.mode='timeout';self.step()
        with self.archive.db:self.archive.db.execute("UPDATE report_read_jobs SET updated_at=?",(time.time()-20,))
        self.assertTrue(self.archive.intelligence.queue_evidence('detail',1,retry=True,priority=200))
        self.mode='expired';self.step()
        self.assertEqual(self.state(1),'unavailable')
        with self.archive.db:self.archive.db.execute("UPDATE report_read_jobs SET updated_at=0")
        self.assertFalse(self.archive.intelligence.queue_evidence('detail',1,retry=True,priority=500))

    def test_other_account_personal_index_does_not_grant_permission(self):
        self.access.personal_index(1,'not-the-current-account',time.time())
        self.assertFalse(self.access.allowed(1))

    def test_scope_refresh_is_coalesced_without_querying_unverified_ids(self):
        self.archive.ingest(5028,world(bid=1),time.time())
        self.archive.intelligence.queue_evidence('profile',1,retry=True,priority=210)
        self.assertTrue(self.reader.scope_refresh_due())
        self.assertFalse(self.reader.scope_refresh_due())
        self.step();self.assertEqual(self.calls,[])

    def test_explicit_scope_diagnostic_gets_one_correlated_attempt(self):
        self.archive.ingest(5028,world(bid=1),time.time())
        self.reader.enqueue([1],verify_access=True);self.step()
        self.assertEqual(self.calls,[[1]]);self.assertEqual(self.state(1),'unavailable')
        for _ in range(3):self.reader.enqueue([1],verify_access=True);self.step()
        self.assertEqual(self.calls,[[1]])

    def test_timed_out_diagnostic_does_not_grant_automatic_access_or_repeat(self):
        self.archive.ingest(5028,world(bid=1),time.time());self.mode='timeout'
        self.reader.enqueue([1],verify_access=True);self.step()
        self.assertFalse(self.access.allowed(1));self.assertFalse(self.access.blocked(1))
        for _ in range(3):self.reader.enqueue([1],verify_access=True);self.step()
        self.assertEqual(self.calls,[[1]])

    def test_diagnostic_does_not_accept_enumeration_or_unconfirmed_role(self):
        with self.assertRaises(ValueError):self.reader.enqueue([999],verify_access=True)
        with self.assertRaises(ValueError):self.reader.enqueue([1,2],verify_access=True)
        self.archive.ingest(5028,world(bid=1),time.time());self.reader.ready=lambda:'not logged in'
        with self.assertRaises(ValueError):self.reader.enqueue([1],verify_access=True)
        self.assertFalse(self.access.allowed(1))

    def test_original_socket_observer_routes_rejection_to_the_owned_singleton(self):
        from collector import Collector
        self.listed(1)
        collector=Collector.__new__(Collector)
        collector.archive=self.archive;collector.source='dashen'
        collector.report_observer=collector.game_click_focus=collector.relation_observer=None
        collector.evidence_observer=self.reader
        collector.state=SimpleNamespace(lock=threading.RLock(),health={},apply=lambda *args:None)
        def through_collector(ids):
            self.calls.append(ids);at=time.time()
            collector._request(10,[ids,0,0],at)
            collector._message(90002,[10,'战报已过期'],at+.01)
        self.reader.bridge.fetch_profiles=through_collector
        self.step()
        self.assertTrue(self.access.blocked(1));self.assertEqual(self.state(1),'unavailable')
        self.assertIsNotNone(self.access.snapshot()['recent_attempts'][0]['sent_at'])

    def test_http_diagnostic_is_known_id_only_and_does_not_repeat_rejected_reads(self):
        from server import make_handler
        self.archive.ingest(5028,world(bid=1),time.time())
        service=ThreadingHTTPServer(('127.0.0.1',0),make_handler(None,0))
        port=service.server_address[1]
        service.RequestHandlerClass=make_handler(None,port,archive=self.archive,evidence_reader=self.reader)
        thread=threading.Thread(target=service.serve_forever,daemon=True);thread.start()
        def post(value):
            conn=http.client.HTTPConnection('127.0.0.1',port)
            conn.request('POST','/api/intel/report/read',json.dumps(value),
                         {'Content-Type':'application/json','Origin':f'http://127.0.0.1:{port}'})
            response=conn.getresponse();status=response.status;response.read();conn.close();return status
        try:
            self.assertEqual(post({'battle_ids':[999],'verify_access':True}),400)
            self.assertEqual(post({'battle_ids':[1,2],'verify_access':True}),400)
            self.assertEqual(post({'battle_ids':[1],'verify_access':'true'}),400)
            self.assertEqual(post({'battle_ids':[1],'verify_access':True}),200)
            self.step();self.assertTrue(self.access.blocked(1))
            self.assertEqual(post({'battle_ids':[1],'verify_access':True}),200)
            self.step();self.assertEqual(self.calls,[[1]])
        finally:service.shutdown();service.server_close();thread.join(2)
