import json
import tempfile
import time
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from test_support import Archive
from harvest import BattleHarvester
from evidence_reader import ReportEvidenceReader
from background_reports import BackgroundReports
from android_report_bridge import AndroidReadOnlyBridge


def page(*ids):
    return [[{'battle_id': bid, 'attack_name': 'sample', 'attack_unionid': 1009, 'defend_unionid': 1008}]
            for bid in ids]


class BackgroundTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.archive = Archive(self.root)
        self.calls = []
        self.bridge = SimpleNamespace(fetch_union_page=lambda cursor: self.calls.append(cursor), snapshot=lambda: {})
        self.harvester = BattleHarvester(self.root, self.archive, self.bridge, lambda: None, False)
        self.harvester.not_before = 0
        self.harvester.continuous = True

    def tearDown(self):
        self.harvester.close()
        self.archive.close()
        self.tmp.cleanup()

    def reply(self, *ids):
        pending = self.harvester.pending
        at = pending['sent_at'] + .01
        self.harvester.on_request(pending['body'], at)
        stats = self.archive.ingest(92, page(*ids), at)
        self.harvester.on_page(page(*ids), at, stats)
        self.harvester._step()

    def step(self):
        self.harvester.not_before = 0
        self.harvester._step()

    def test_head_catches_multiple_new_pages_without_overwriting_history_cursor(self):
        self.harvester.start()
        self.step(); self.reply(200, 199)
        self.step(); self.reply(198, 197)
        self.harvester.latest_due = 0
        self.step(); self.assertEqual(self.calls[-1], 0)
        self.reply(205, 204)
        self.step(); self.assertEqual(self.calls[-1], 204)
        self.reply(203, 202)
        self.step(); self.assertEqual(self.calls[-1], 202)
        self.reply(201, 200)
        self.assertEqual(self.harvester.snapshot()['cursor'], 197)
        self.assertEqual(self.harvester.snapshot()['latest_highwater'], 205)
        self.step(); self.assertEqual(self.calls[-1], 197)
        self.assertEqual(self.harvester.snapshot()['new_reports'], 9)

    def test_urgent_head_preempts_minute_cooldown_without_skipping_history_or_gaps(self):
        self.harvester.start();self.step();self.reply(100,99)
        self.harvester.data['run_pages']=self.harvester.data['max_pages']
        self.harvester.request_latest();self.step()
        self.assertEqual(self.harvester.pending['cursor'],0)
        self.assertTrue(self.harvester.pending['urgent'])
        self.reply(200,199)
        self.assertEqual(self.harvester.snapshot()['cursor'],99)
        self.assertEqual(self.harvester.snapshot()['fresh_ranges'],[{'cursor':199,'boundary':100,'top':200}])
        self.assertIn('received_at',self.harvester.snapshot()['priority_head'])

    def test_urgent_request_waits_for_existing_page_and_preserves_history_checkpoint(self):
        self.harvester.start();self.step();self.reply(100,99)
        self.step();pending=self.harvester.pending
        self.harvester.request_latest();self.harvester._step()
        self.assertIs(self.harvester.pending,pending)
        self.reply(98,97);self.step()
        self.assertTrue(self.harvester.pending['urgent'])
        self.assertEqual(self.harvester.snapshot()['cursor'],97)

    def test_history_yields_to_detail_but_urgent_head_still_runs(self):
        self.harvester.start();self.step();self.reply(100,99)
        self.harvester.yield_history(10);self.step()
        self.assertIsNone(self.harvester.pending)
        self.harvester.request_latest();self.step()
        self.assertTrue(self.harvester.pending['urgent'])

    def test_available_history_end_keeps_polling_newest_after_restart(self):
        self.harvester.start(); self.step(); self.reply(100)
        self.step(); self.reply()
        self.assertTrue(self.harvester.snapshot()['available_end'])
        self.assertEqual(self.harvester.snapshot()['status'], 'running')
        self.harvester.pause()
        other = BattleHarvester(self.root, self.archive, self.bridge, lambda: None, False)
        try:
            other.continuous = True
            other.start(resume=True)
            other.not_before = 0
            other._step()
            self.assertEqual(other.pending['cursor'], 0)
            self.assertTrue(other.pending['latest'])
            self.assertEqual(other.snapshot()['cursor'], 100)
        finally:
            other.close()

    def test_newest_page_preempts_long_catchup_without_skipping_either_gap(self):
        self.harvester.start(); self.step(); self.reply(100, 99)
        self.harvester.latest_due = 0
        self.step(); self.reply(300, 299)
        self.assertEqual(self.harvester.snapshot()['latest_cursor'], 299)
        self.harvester.fresh_due = 0
        self.step(); self.assertEqual(self.calls[-1], 0)
        self.reply(400, 399)
        saved = self.harvester.snapshot()
        self.assertEqual((saved['cursor'], saved['latest_cursor'], saved['latest_highwater']), (99, 299, 100))
        self.assertEqual(saved['fresh_ranges'], [{'cursor':399, 'boundary':300, 'top':400}])
        self.step(); self.assertEqual(self.calls[-1], 399)
        self.reply(398, 300)
        self.assertEqual(self.harvester.snapshot()['latest_highwater'], 100)
        self.step(); self.assertEqual(self.calls[-1], 299)
        self.reply(298, 100)
        self.assertEqual(self.harvester.snapshot()['latest_highwater'], 400)
        self.assertEqual(self.harvester.snapshot()['fresh_ranges'], [])
        self.assertEqual(self.harvester.snapshot()['cursor'], 99)

    def test_fresh_gaps_persist_and_failed_page_never_advances_checkpoint(self):
        self.harvester.start(); self.step(); self.reply(100, 99)
        self.harvester.latest_due = 0
        self.step(); self.reply(300, 299)
        self.harvester.fresh_due = 0
        self.step(); self.reply(400, 399)
        self.step(); self.harvester.pending['deadline'] = 0; self.harvester._step()
        self.assertEqual(self.harvester.snapshot()['fresh_ranges'][0]['cursor'], 399)
        other = BattleHarvester(self.root, self.archive, self.bridge, lambda:None, False)
        try:
            self.assertEqual(other.snapshot()['fresh_ranges'], [{'cursor':399, 'boundary':300, 'top':400}])
            self.assertEqual(other.snapshot()['latest_cursor'], 299)
            other.continuous = True; other.not_before = 0; other.start(resume=True); other._step()
            self.assertEqual(other.pending['cursor'], 0, 'Restart first checks the current newest page')
        finally:
            other.close()

    def test_timeout_does_not_checkpoint_and_head_checkpoint_survives_restart(self):
        self.harvester.start(); self.step(); self.reply(100, 99)
        self.harvester.latest_due = 0
        self.step(); self.reply(110, 109)
        self.assertEqual(self.harvester.snapshot()['latest_cursor'], 109)
        self.step()
        self.harvester.pending['deadline'] = 0
        self.harvester._step()
        self.assertEqual(self.harvester.snapshot()['latest_cursor'], 109)
        other = BattleHarvester(self.root, self.archive, self.bridge, lambda: None, False)
        try:
            other.continuous = True; other.not_before = 0
            other.start(resume=True); other._step()
            self.assertEqual(other.pending['cursor'], 0, 'Check new arrivals before resuming the old gap')
            self.assertEqual(other.snapshot()['latest_cursor'], 109)
            self.assertEqual(other.snapshot()['cursor'], 99)
        finally:
            other.close()

    def test_supervisor_preserves_enabled_and_checkpoint_across_service_close(self):
        reader = ReportEvidenceReader(self.archive, self.bridge, lambda: None, False)
        service = BackgroundReports(self.root, self.archive, self.bridge, self.harvester, reader, lambda: None)
        service._step()
        self.assertTrue(reader.active)
        self.assertEqual(self.harvester.snapshot()['status'], 'running')
        service.close()
        self.assertTrue(json.loads(service.path.read_text(encoding='utf8'))['enabled'])
        service.pause()
        other = BackgroundReports(self.root, self.archive, self.bridge, self.harvester, reader, lambda: None)
        self.assertFalse(other.data['enabled'])
        other.close(); reader.close()

    def test_no_organization_never_requests_alliance_zero_history(self):
        self.archive.profile.update(union_id='0',force_type=1,battle_role_id='own')
        reader=ReportEvidenceReader(self.archive,self.bridge,lambda:None,False)
        service=BackgroundReports(self.root,self.archive,self.bridge,self.harvester,reader,lambda:None)
        try:
            service._step();self.harvester._step()
            self.assertTrue(reader.active)
            self.assertNotEqual(self.harvester.snapshot()['status'],'running')
            self.assertEqual(self.calls,[])
            self.assertIn('本人可访问战报',service.snapshot()['scope'])
        finally:service.close();reader.close()

    def test_members_are_requested_without_opening_ui_and_ack_is_not_receipt(self):
        calls=[];self.bridge.fetch_members=lambda:calls.append(103)or{'command':103}
        reader=ReportEvidenceReader(self.archive,self.bridge,lambda:None,False)
        service=BackgroundReports(self.root,self.archive,self.bridge,self.harvester,reader,lambda:None)
        try:
            service._step();service._step()
            self.assertEqual(calls,[103]);self.assertEqual(service.snapshot()['members']['status'],'requested')
            observed=time.time()+1
            with self.archive.lock,self.archive.db:
                self.archive.db.execute('INSERT INTO attendance_roster VALUES(?,?,?,?,?,?)',
                    (self.archive.profile_id,self.archive.profile['union_id'],'11','任意成员','',observed))
            service._step();self.assertEqual(service.snapshot()['members']['status'],'ready')
            self.assertEqual(service.snapshot()['members']['count'],1)
        finally:service.close();reader.close()

    def test_bundled_history_import_is_not_counted_as_new_collection_after_restart(self):
        reader = ReportEvidenceReader(self.archive, self.bridge, lambda: None, False)
        seed = {'battle_reports':2,'report_bodies':0}
        service = BackgroundReports(self.root, self.archive, self.bridge, self.harvester, reader, lambda: None, seed_baseline=seed)
        self.archive.ingest(92, page(100,101), time.time())
        self.assertEqual(service.snapshot()['new_reports'],0)
        self.archive.ingest(92, page(102), time.time())
        self.assertEqual(service.snapshot()['new_reports'],1)
        service.close()
        other = BackgroundReports(self.root, self.archive, self.bridge, self.harvester, reader, lambda: None, seed_baseline=seed)
        self.assertEqual(other.snapshot()['new_reports'],1)
        other.close();reader.close()

    def test_failed_job_retries_are_bounded_and_dedup_skips_existing_full(self):
        self.archive.ingest(92, page(100), time.time())
        reader = ReportEvidenceReader(self.archive, self.bridge, lambda: None, False)
        reader.resume(10, continuous=True)
        with self.archive.lock, self.archive.db:
            self.archive.db.execute("UPDATE report_read_jobs SET state='failed',attempts=3,updated_at=0")
        reader._step()
        row = self.archive.db.execute('SELECT state,attempts FROM report_read_jobs').fetchone()
        self.assertEqual(tuple(row), ('failed', 3))
        self.archive.ingest(11, {'battle_id': 100, 'report': 'actual full text'}, time.time())
        with self.archive.lock, self.archive.db:
            self.archive.db.execute("UPDATE report_read_jobs SET state='queued'")
        reader._step()
        self.assertEqual(self.archive.db.execute('SELECT state FROM report_read_jobs').fetchone()[0], 'done')
        reader.close()

    def test_old_checkpoint_is_retained_and_bootstrap_rescan_happens_only_once(self):
        reader = ReportEvidenceReader(self.archive, self.bridge, lambda: None, False)
        self.harvester.data.update(cursor=50, pages=375, status='paused')
        service = BackgroundReports(self.root, self.archive, self.bridge, self.harvester, reader, lambda: None)
        self.assertEqual(service.data['previous_checkpoint']['cursor'], 50)
        self.assertEqual(self.harvester.snapshot()['cursor'], 0)
        service.close()
        self.harvester.data.update(cursor=90, pages=2)
        other = BackgroundReports(self.root, self.archive, self.bridge, self.harvester, reader, lambda: None)
        self.assertEqual(self.harvester.snapshot()['cursor'], 90)
        other.close(); reader.close()

    def test_android_adapter_only_accepts_report_reads(self):
        bridge = AndroidReadOnlyBridge(self.root, self.archive, lambda: None)
        with patch.object(bridge, '_submit', return_value={'command': 92}) as submit:
            bridge.fetch_union_page(123)
            submit.assert_called_once_with(2, 123)
        with patch.object(bridge,'_submit',return_value={'command':103})as submit:
            self.assertEqual(bridge.fetch_members()['body'],[]);submit.assert_called_once_with(3,0)
        for cursor in (-1, True, '1', 2**31):
            with self.assertRaises(ValueError): bridge.fetch_union_page(cursor)
        for ids in ([], [True], [1, 1], list(range(1, 17))):
            with self.assertRaises(ValueError): bridge.fetch_profiles(ids)
        for bid in (0, True, -1):
            with self.assertRaises(ValueError): bridge.fetch_battle_detail(bid)
        bridge.close()

    def test_user_pause_wins_over_a_concurrent_automatic_resume(self):
        reader = ReportEvidenceReader(self.archive, self.bridge, lambda: None, False)
        service = BackgroundReports(self.root, self.archive, self.bridge, self.harvester, reader, lambda: None)
        entered, release = threading.Event(), threading.Event()
        original = self.harvester.start
        def slow_start(*args, **kwargs):
            entered.set()
            if not release.wait(3): raise RuntimeError('test scheduler timed out')
            return original(*args, **kwargs)
        with patch.object(self.harvester, 'start', side_effect=slow_start):
            automatic = threading.Thread(target=service._step)
            automatic.start()
            self.assertTrue(entered.wait(2))
            paused = threading.Thread(target=service.pause)
            paused.start()
            release.set()
            automatic.join(3); paused.join(3)
            self.assertFalse(automatic.is_alive() or paused.is_alive())
        self.assertFalse(service.data['enabled'])
        self.assertFalse(reader.active)
        self.assertEqual(self.harvester.snapshot()['status'], 'paused')
        service.close(); reader.close()


if __name__ == '__main__':
    unittest.main()
