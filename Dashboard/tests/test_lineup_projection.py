import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from test_support import Archive
from lineup_prediction import player_history
from test_garrison_lineup_current import report


A, B = '113709493', '113709494'


class ProjectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.archive = Archive(self.root)
        self.observe(A); self.observe(B)
        self.archive.ingest(92, [[report(1,100,army=A)], [report(2,110,army=B,cards='31,32,33')]], 120)

    def tearDown(self):
        self.archive.close(); self.temp.cleanup()

    def observe(self, army=A, *, name='player', uid='12276', at=220, reference=None):
        value = [None]*34
        value[1] = {uid:[name,0,1009]+[0]*9+[[0,0,'alliance']]}
        raw = [0]*33; raw[:6] = [5,int(uid),9820923,9820923,200,210]
        raw[16] = '2,21;1,11;2,31;'
        if reference: raw[31] = f'{reference},1,1,1,1,0;'
        value[6] = {army:raw}
        self.archive.ingest(5028,value,at)

    def query(self, army=A):
        return self.archive.query('army_lineups',army_id=army)

    def test_warm_queries_skip_history_and_unrelated_player_does_not_invalidate(self):
        with patch('lineup_prediction.player_history',wraps=player_history) as scan:
            self.assertEqual(self.query()['current_report_id'],'1')
            self.query(); self.assertEqual(scan.call_count,1)
            record=report(9,300);record.update(attack_name='other-player',attack_role_id='other-role')
            self.archive.ingest(92,[[record]],310)
            scans=scan.call_count
            self.assertEqual(self.query()['current_report_id'],'1')
            self.assertEqual(scan.call_count,scans)

    def test_same_player_same_troops_new_report_updates_only_the_right_cards(self):
        self.assertEqual(self.query()['current_report_id'],'1')
        self.assertEqual(self.query(B)['current_report_id'],'2')
        self.archive.ingest(92,[[report(3,300,side='defend',cards='31,32,33')]],310)
        self.assertEqual(self.query(B)['current_report_id'],'3')
        self.assertEqual(self.query()['current_report_id'],'1')

    def test_new_missing_skills_replace_old_complete_configuration(self):
        self.query();new=report(3,300);new.pop('all_skill_info')
        self.archive.ingest(92,[[new]],310)
        answer=self.query()
        self.assertEqual(answer['current_report_id'],'3')
        self.assertTrue(all(h['skills'] is None for h in answer['items'][0]['data']['heroes']))

    def test_late_snapshot_cannot_commit_over_new_evidence(self):
        with self.archive.read_snapshot() as reader:
            old=reader.query('army_lineups',army_id=A)
            self.archive.ingest(92,[[report(3,300)]],310)
            self.assertEqual(reader.query('army_lineups',army_id=A)['current_report_id'],'1')
        self.archive.lineup_projection.flush()
        self.assertGreater(self.archive.lineup_projection.metrics['rejected_stale'],0)
        self.assertEqual(self.query()['current_report_id'],'3')
        self.archive.lineup_projection.flush()
        self.assertEqual(self.archive.db.execute('SELECT report_id FROM army_lineup_projection WHERE army_id=?',(A,)).fetchone()[0],'3')

    def test_uncommitted_results_are_not_published_to_shared_cache(self):
        self.query();before=self.archive.lineup_projection.metrics['computations']
        self.archive.db.execute("UPDATE combat_sides SET event_at=999 WHERE battle_id='1'")
        self.query()
        self.assertEqual(self.archive.lineup_projection.metrics['computations'],before)
        self.archive.db.rollback()
        self.assertEqual(self.query()['current_report_id'],'1')

    def test_reused_army_id_does_not_show_another_owners_old_roster(self):
        self.query();self.observe(name='new-player',uid='333',at=400)
        answer=self.query()
        self.assertEqual(answer['items'],[])
        self.assertNotIn('current_report_id',answer)

    def test_future_report_expires_cache_at_eligibility_boundary(self):
        future=time.time()+100
        self.archive.ingest(92,[[report(3,future)]],time.time()-1)
        self.assertEqual(self.query()['current_report_id'],'1')
        with patch('archive.time.time',return_value=future+1):
            self.assertEqual(self.query()['current_report_id'],'3')

    def test_persistent_projection_survives_restart_without_history_scan(self):
        self.query();self.archive.lineup_projection.flush();self.archive.close()
        self.archive=Archive(self.root)
        with patch('lineup_prediction.player_history',side_effect=AssertionError('unexpected scan')):
            self.assertEqual(self.query()['current_report_id'],'1')
        self.assertEqual(self.archive.lineup_projection.metrics['disk_hits'],1)

    def test_replay_bypasses_current_projection(self):
        self.archive.ingest(92,[[report(3,300)]],310);self.query()
        rows,match,evidence=self.archive._army_lineups(A,known_at=200)
        self.assertEqual(evidence['current_report_id'],'1')

    def test_duplicate_map_links_and_freshness_do_not_force_history_scan(self):
        self.query();before=self.archive.lineup_projection.metrics['computations']
        self.observe(at=240)
        answer=self.query()
        self.assertEqual(answer['troops_observed_at'],240)
        self.assertEqual(self.archive.lineup_projection.metrics['computations'],before)

    def test_received_full_and_linked_have_separate_denominators_and_dedup(self):
        self.observe(A,reference=4,at=230)
        for _ in range(2):self.archive.ingest(11,{'battle_id':1,'report':'actual body'},240)
        self.archive.ingest(11,{'battle_id':5,'report':'body before summary'},250)
        self.archive.lineup_projection.refresh_group('1009','player')
        status=self.archive.lineup_projection.collect_status()
        self.assertEqual(status['received'],{'count':2,'total':4,'ratio':.5})
        self.assertEqual(status['full'],{'count':1,'total':2,'ratio':.5,'body_only':1})
        self.assertEqual(status['linked']['count'],2)
        self.assertEqual(status['linked']['total'],2)
        self.assertEqual(status['linked']['pending'],0)
        self.archive.ingest(92,[[report(5,350)]],360)
        status=self.archive.lineup_projection.collect_status()
        self.assertEqual((status['received']['count'],status['full']['count']),(3,2))

    def test_unknown_army_is_checked_without_being_counted_as_linked(self):
        self.observe('113709495')
        self.archive.lineup_projection.refresh_group('1009','player')
        status=self.archive.lineup_projection.collect_status()
        self.assertEqual((status['linked']['count'],status['linked']['total'],status['linked']['checked']),(2,3,3))

    def test_background_group_does_not_queue_itself_again_for_every_army(self):
        self.archive.lineup_projection.refresh_group('1009','player')
        self.assertFalse(self.archive.lineup_projection.requested)

    def test_duplicate_report_receipt_refreshes_metadata_without_recomputing_roster(self):
        self.query();before=self.archive.lineup_projection.metrics['computations']
        self.archive.ingest(92,[[report(1,100,army=A)]],500)
        answer=self.query()
        self.assertEqual(answer['items'][0]['observed_at'],500)
        self.assertEqual(self.archive.lineup_projection.metrics['computations'],before)

    def test_cached_query_never_changes_original_evidence(self):
        tables=('battle_reports','team_versions','combat_sides','army_states')
        counts=lambda:[self.archive.db.execute('SELECT COUNT(*) FROM '+t).fetchone()[0] for t in tables]
        before=counts();self.query();self.query();self.archive.lineup_projection.flush()
        self.assertEqual(counts(),before)


if __name__=='__main__':unittest.main()
