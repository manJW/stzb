import copy
import json
from pathlib import Path
import tempfile
import unittest
import zlib

from archive import Archive
from test_support import LEGACY_PROFILE,WorldState
from test_intelligence import battle,world
import reported_archive as policy


class ReportedArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.archive=Archive(self.root,profile=dict(LEGACY_PROFILE))
        self.archive.attendance.close()
    def tearDown(self):self.archive.close();self.temp.cleanup()
    def count(self,table):return self.archive.db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]
    def report(self,bid=1,at=300,army='9001'):
        record=battle(bid,at,main=True)
        record['attack_idu']=f'{army},11,12,13,0'
        self.archive.ingest(92,[[record]],at+1)
        return record

    def test_unreported_is_volatile_and_live_map_is_unchanged(self):
        scene=world();scene[18]=1
        live=WorldState(self.root);live.apply(5026,scene,250)
        self.archive.ingest(5026,scene,250)
        self.assertIn('9001',live.armies)
        for table in ('army_current','army_states','march_samples'):
            self.assertEqual(self.count(table),0)
            self.assertEqual(self.count('pending_'+table),1)
        self.assertEqual(self.archive.db.execute("SELECT COUNT(*) FROM observations WHERE kind IN ('army_state','out_of_view')").fetchone()[0],0)
        payload=json.loads(zlib.decompress(self.archive.db.execute('SELECT payload FROM messages WHERE message_id=5026').fetchone()[0]))
        self.assertEqual(payload[6],{})
        self.assertIn('9001',scene[6],'filtering the archive must not mutate the live scene')
        self.assertEqual(self.archive.intelligence.query('operations',relation='all')['items'],[])
        with self.archive.read_snapshot() as reader:
            self.assertEqual(reader.db.execute('SELECT COUNT(*) FROM live_army_current').fetchone()[0],1)
            self.assertEqual(reader.db.execute('SELECT COUNT(*) FROM main.army_current').fetchone()[0],0)
            data=reader.query('army_lineups',army_id='9001')
            self.assertEqual(data['items'],[])

    def test_direct_report_promotes_only_its_army_and_future_history(self):
        self.archive.ingest(5026,world(aid='9001'),250)
        self.archive.ingest(5026,world(aid='9002',end=400),251)
        self.report()
        self.assertEqual(self.count('army_current'),1)
        self.assertEqual(self.count('march_samples'),1)
        self.assertEqual(self.count('pending_army_current'),1)
        self.assertTrue(policy.admitted(self.archive.db,self.archive.profile_id,'9001'))
        self.assertFalse(policy.admitted(self.archive.db,self.archive.profile_id,'9002'))
        self.assertEqual(self.count('battle_march_links'),1)
        self.archive.ingest(5028,world(aid='9001',state=5,end=500),350)
        self.assertEqual(self.count('army_states'),2)
        self.assertEqual(self.archive.db.execute("SELECT COUNT(*) FROM observations WHERE kind='army_state'").fetchone()[0],1)

    def test_unique_arrival_can_establish_an_unbound_report(self):
        self.archive.ingest(5026,world(),250)
        self.report(army='0')
        self.assertEqual(self.count('reported_armies'),1)
        self.assertEqual(self.count('march_samples'),1)
        self.assertEqual(self.archive.db.execute('SELECT method FROM battle_march_links').fetchone()[0],'unique_attack_arrival')

    def test_ambiguous_arrival_does_not_merge_two_armies(self):
        scene=world();scene[6]['9002']=copy.deepcopy(scene[6]['9001'])
        self.archive.ingest(5026,scene,250)
        self.report(army='0')
        self.assertEqual(self.count('reported_armies'),0)
        self.assertEqual(self.count('march_samples'),0)
        self.assertEqual(self.count('pending_march_samples'),2)
        self.assertEqual(self.count('battle_reports'),1)
        self.assertGreater(self.count('team_versions'),0,'the actual report/formation is retained even without an army binding')

    def test_late_world_reference_promotes_after_header_received(self):
        self.report(army='0')
        self.assertEqual(self.count('reported_armies'),0)
        self.archive.ingest(5026,world(bid=1),310)
        self.assertEqual(self.count('reported_armies'),1)
        self.assertEqual(self.count('army_current'),1)
        self.assertEqual(self.count('army_report_refs'),1)

    def test_unknown_cache_is_gone_on_reopen(self):
        self.archive.ingest(5026,world(),250)
        self.archive.close()
        self.archive=Archive(self.root)
        self.assertTrue(self.archive.report_only)
        for table in ('army_current','army_states','march_samples'):
            self.assertEqual(self.count(table),0)
            self.assertEqual(self.count('pending_'+table),0)

    def test_snapshot_deduplicates_an_army_promoted_after_copy(self):
        self.archive.ingest(5026,world(),250)
        values=policy.live_snapshot(self.archive.db)
        self.report()
        with self.archive.read_snapshot() as reader:
            # Simulate promotion between the pending copy and the main snapshot.
            reader.db.execute('PRAGMA query_only=OFF')
            reader.db.executemany('INSERT INTO pending_army_current VALUES(?,?,?,?)',values['army_current'])
            reader.db.executemany('INSERT INTO pending_army_states VALUES('+','.join('?'for _ in range(16))+')',values['army_states'])
            self.assertEqual(reader.db.execute('SELECT COUNT(*) FROM live_army_current').fetchone()[0],1)
            self.assertEqual(reader.db.execute('SELECT COUNT(*) FROM live_army_states').fetchone()[0],1)

    def test_offline_cleanup_keeps_actual_reports_and_redacts_unknown_rows(self):
        self.archive.db.execute('UPDATE team_archive_policy SET reported_only=0')
        self.archive.db.commit()
        self.archive.ingest(5026,world(aid='9001'),250)
        self.archive.ingest(5026,world(aid='9002',end=400),251)
        self.report()
        reports=self.count('battle_reports');versions=self.count('team_versions')
        self.archive.db.execute('UPDATE team_archive_policy SET reported_only=1')
        policy.seed_admissions(self.archive.db,self.archive.profile_id)
        result=policy.prune_history(self.archive.db,self.archive.profile_id)
        self.assertEqual(result['army_current'],1)
        self.assertEqual(result['march_samples'],1)
        self.assertGreater(result['filtered_messages'],0)
        self.assertEqual(self.count('battle_reports'),reports)
        self.assertEqual(self.count('team_versions'),versions)
        self.assertEqual(self.count('army_current'),1)
        self.assertEqual(self.archive.db.execute('SELECT army_id FROM army_current').fetchone()[0],'9001')

    def test_trim_keeps_current_idle_state_until_its_live_sighting_expires(self):
        scene=world(state=5,end=210)
        self.archive.ingest(5026,scene,250)
        self.archive.ingest(5026,scene,4200)
        self.assertEqual(self.count('pending_army_current'),1)
        self.assertEqual(self.count('pending_army_states'),1)
        policy.trim(self.archive.db,self.archive.profile_id,8000)
        self.assertEqual(self.count('pending_army_current'),0)
        self.assertEqual(self.count('pending_army_states'),0)

    def test_unknown_forecast_is_promoted_only_when_its_report_arrives(self):
        from lineup_prediction import predict,record_forecast
        self.archive.ingest(5026,world(),250)
        row=dict(id='9001',owner='200',player_name='enemy',union_id='1008',state_code=1,
                 begin=200,end=300,seen_at=250,**{'from':[500,600]},target=[510,610],
                 troops=[{'position':i,'code':23}for i in (1,2,3)])
        prediction=predict(row,([],{},False),250)
        record_forecast(self.archive.db,self.archive.profile_id,row,prediction,250)
        self.assertEqual(self.count('lineup_forecasts'),0)
        self.assertEqual(self.count('pending_lineup_forecasts'),1)
        self.archive.db.commit()
        self.report()
        self.assertEqual(self.count('lineup_forecasts'),1)
        self.assertEqual(self.count('pending_lineup_forecasts'),0)

    def test_unresolved_lineup_cache_is_not_persisted(self):
        self.archive.ingest(5026,world(),250)
        with self.archive.read_snapshot() as reader:reader.query('army_lineups',army_id='9001')
        self.archive.lineup_projection.flush()
        self.assertEqual(self.count('army_lineup_projection'),0)

    def test_pending_report_reference_is_volatile_but_still_queues_header(self):
        self.archive.ingest(5026,world(bid=12),250)
        self.assertEqual(self.count('army_report_refs'),0)
        self.assertEqual(self.count('pending_army_report_refs'),1)
        self.assertEqual(self.archive.db.execute("SELECT battle_id FROM report_read_jobs WHERE kind='profile'").fetchone()[0],'12')
        self.assertEqual(self.archive.db.execute('SELECT received FROM report_collection_state WHERE battle_id=12').fetchone()[0],0)


if __name__=='__main__':unittest.main()
