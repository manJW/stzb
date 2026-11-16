"""Only structured field gaps may create automatic detail reads."""
import copy,json,tempfile,time,unittest
from pathlib import Path
from types import SimpleNamespace
from combat_loadout import structured_report_complete
from evidence_reader import ReportEvidenceReader
from test_support import Archive
from test_intelligence import battle,complete_battle,world


class StructuredDetailTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.archive=Archive(Path(self.temp.name));self.calls=[]
        self.bridge=SimpleNamespace(fetch_profiles=self.reply,
            fetch_battle_detail=lambda bid:self.fail('Numeric turn text must never be requested by the automatic reader'))

    def tearDown(self):self.archive.close();self.temp.cleanup()

    def reply(self,ids):
        self.calls.append(list(ids));self.archive.ingest(10,[[complete_battle(bid)for bid in ids]],time.time())

    def test_complete_report_without_body_does_not_create_a_detail_job(self):
        self.archive.ingest(92,[[complete_battle()]],301)
        self.assertTrue(self.archive.intelligence.detail_received(1))
        self.assertFalse(self.archive.intelligence.queue_evidence('detail',1,retry=True))
        self.archive.intelligence.seed_alliance_details()
        self.assertEqual(self.archive.db.execute("SELECT COUNT(*) FROM report_read_jobs WHERE kind='detail'").fetchone()[0],0)
        reader=ReportEvidenceReader(self.archive,self.bridge,lambda:None,False)
        try:
            reader.resume(3,continuous=True);reader._step()
            self.assertEqual(self.calls,[]);self.assertFalse(reader.snapshot()['automatic_full_text'])
            self.assertIsNone(self.archive.intelligence.report(1)['full_report'])
        finally:reader.close()

    def test_body_alone_does_not_skip_needed_fields_and_existing_body_is_retained(self):
        self.archive.ingest(92,[[battle()]],301)
        self.archive.ingest(11,{'battle_id':1,'report':'old numeric body'},302)
        reader=ReportEvidenceReader(self.archive,self.bridge,lambda:None,False)
        try:
            reader.resume(3);reader._step()
            self.assertEqual(self.calls,[[1]])
            self.assertEqual(self.archive.db.execute("SELECT state FROM report_read_jobs WHERE kind='detail'").fetchone()[0],'done')
            self.assertEqual(self.archive.intelligence.report(1)['full_report']['body'],'old numeric body')
            reader.not_before=0;reader._step();self.assertEqual(self.calls,[[1]])
        finally:reader.close()

    def test_known_result_red_zero_and_empty_skill_slots_are_data_not_missing_fields(self):
        report=complete_battle();self.assertTrue(structured_report_complete(report))
        report['all_skill_info']=''.join(f'{slot},200001,1,0,0,0,0;'for slot in range(1,7))
        self.assertTrue(structured_report_complete(report))
        for field in ('attack_advance','defend_advance','all_skill_info','result','attack_all_hero_info'):
            incomplete=copy.deepcopy(report);incomplete.pop(field)
            self.assertFalse(structured_report_complete(incomplete),field)

    def test_casualty_zero_placeholders_are_complete_but_truncated_rows_are_not(self):
        report=complete_battle()
        report['attack_all_hero_info']='100701,40,100,0,0;0,0,0,0,0;100703,40,500,100,0;'
        self.assertTrue(structured_report_complete(report))
        report['attack_all_hero_info']='100701,40,100,0,0;'
        self.assertFalse(structured_report_complete(report))
        report['attack_idu']='0,11,0,0,0'
        self.assertTrue(structured_report_complete(report),'A confirmed one-hero army does not require invented extra heroes')

    def test_migration_retires_text_only_work_and_reopens_missing_fields_with_saved_text(self):
        self.archive.ingest(92,[[complete_battle(1),battle(2)]],301)
        with self.archive.db:
            self.archive.db.execute("INSERT INTO report_read_jobs VALUES(?,'detail','1','failed',2,1,1,'old text timeout',80)",(self.archive.profile_id,))
            self.archive.db.execute("UPDATE report_read_jobs SET state='done',attempts=1 WHERE battle_id='2'")
        self.archive.ingest(11,{'battle_id':2,'report':'existing'},302)
        self.archive.intelligence.seed_alliance_details()
        states={r[0]:r[1]for r in self.archive.db.execute("SELECT battle_id,state FROM report_read_jobs WHERE kind='detail'")}
        self.assertEqual(states,{'1':'done','2':'queued'})
        self.assertEqual(self.archive.intelligence.report(2)['full_report']['body'],'existing')

    def test_profile_reply_with_fields_finishes_without_an_extra_text_read(self):
        self.archive.ingest(5028,world(bid=1),200)
        reader=ReportEvidenceReader(self.archive,self.bridge,lambda:None,False)
        try:
            reader.resume(3);reader._step();reader.not_before=0;reader._step()
            self.assertEqual(self.calls,[[1]])
            self.assertTrue(reader._received('detail',1))
            self.assertFalse(reader.active)
        finally:reader.close()

    def test_batch_missing_details_preserves_priority_and_never_rebuilds_complete_reports(self):
        self.archive.ingest(92,[[battle(1),battle(2),complete_battle(3)]],301)
        self.archive.intelligence.queue_evidence('detail',2,priority=200)
        reader=ReportEvidenceReader(self.archive,self.bridge,lambda:None,False)
        try:
            reader.resume(3);reader._step()
            self.assertEqual(self.calls,[[2]],'Urgent selected-army work never waits for an ordinary history batch')
            reader.not_before=0;reader._step();self.assertEqual(self.calls,[[2],[1]])
            self.assertTrue(reader._received('detail',1));self.assertTrue(reader._received('detail',2))
            self.assertFalse(self.archive.intelligence.queue_evidence('detail',3))
        finally:reader.close()

    def test_npc_loadout_does_not_block_human_fields_or_land_result(self):
        report=complete_battle();report.update(npc=1,defend_role_id='0',defend_unionid=0,defend_idu='0,0,0,0,0')
        for field in ('defend_advance','defend_all_hero_info'):report.pop(field)
        self.assertTrue(structured_report_complete(report))
        report.pop('attack_advance');self.assertFalse(structured_report_complete(report))
