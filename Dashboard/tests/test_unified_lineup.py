"""Live cards share one prediction without changing archived identity evidence."""
import tempfile
import unittest
from pathlib import Path

from test_support import Archive
from lineup_prediction import army_lineup, predict_alerts
from test_garrison_lineup_current import ARMY, report


class UnifiedLineupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.archive = Archive(Path(self.temp.name))
        scene = [None]*34
        scene[1] = {'12276':['player',0,1009]+[0]*9+[[0,0,'alliance']]}
        raw = [0]*33
        raw[:6] = [1,12276,9770916,9820923,200,500]
        raw[16] = '2,21;1,11;2,31;'
        scene[6] = {ARMY:raw}
        self.archive.ingest(5028, scene, 220)
        self.row = dict(id=ARMY,owner='12276',player_name='player',union_id='1009',
                        state_code=1,from_ignored=None,begin=200,end=500,
                        target=[982,923],troops=[{'code':c} for c in (21,11,31)])
        self.row['from'] = [977,916]
        self.snapshot = {'server_now':600,'armies':[self.row],'alerts':[{'army_id':ARMY}]}
        self.archive.ingest(92, [[report(1,100)]],110)

    def tearDown(self):
        self.archive.close()
        self.temp.cleanup()

    def views(self):
        batch = predict_alerts(self.archive,self.snapshot)[ARMY]
        with self.archive.read_snapshot() as reader:
            single = army_lineup(reader,self.snapshot,ARMY)
        self.assertEqual(batch,single['prediction'])
        return batch,single

    def test_unverified_personal_reference_is_identical_in_both_views(self):
        prediction, single = self.views()
        self.assertFalse(prediction['display_verified'])
        self.assertEqual(prediction['display_report']['battle_id'],'1')
        self.assertEqual(single['items'][0]['data']['heroes'],prediction['display_report']['heroes'])
        self.assertEqual(single['match'],'live_reference')
        # Showing a reference never creates a confirmed personal-army link.
        self.assertIsNone(self.archive.db.execute('SELECT army_id FROM combat_sides').fetchone()[0])

    def test_new_direct_report_updates_both_views_and_all_fields(self):
        old, _ = self.views()
        latest = report(2,400,army=ARMY)
        latest['attack_advance']='0,0,0,0,0,0;1,0,0,0,0,0;2,0,0,0,0,0;3,0,0,0,0,0;'
        latest['all_skill_info']=latest['all_skill_info'].replace('200011','200099')
        self.archive.ingest(92,[[latest]],410)
        prediction,single = self.views()
        self.assertTrue(prediction['display_verified'])
        self.assertEqual(single['current_report_id'],'2')
        self.assertGreater(prediction['source_revision'],old['source_revision'])
        self.assertEqual([h['red_stars'] for h in prediction['display_report']['heroes']],[1,2,3])
        self.assertEqual(single['items'][0]['data']['heroes'],prediction['display_report']['heroes'])
        self.assertIn(200099,[s['id'] for s in prediction['display_report']['heroes'][0]['skills']])

    def test_two_same_player_same_troop_armies_do_not_share_unverified_roster(self):
        self.snapshot['armies'].append({**self.row,'id':'another'})
        self.snapshot['alerts'].append({'army_id':'another'})
        for prediction in predict_alerts(self.archive,self.snapshot).values():
            self.assertIsNone(prediction['display_report'])
            self.assertEqual(prediction['force_assessment']['kind'],'unknown')
            self.assertEqual(prediction['display_reason'],'multiple_personal_armies')
        self.archive.ingest(92,[[report(2,400,army=ARMY)]],410)
        batch=predict_alerts(self.archive,self.snapshot)
        self.assertEqual(batch[ARMY]['display_report']['battle_id'],'2')
        self.assertIsNone(batch['another']['display_report'])

    def test_report_explicitly_bound_to_another_army_is_never_borrowed(self):
        with self.archive.db:
            self.archive.db.execute('DELETE FROM combat_sides')
        self.archive.ingest(92,[[report(2,400,army='99887766')]],410)
        prediction,single=self.views()
        self.assertIsNone(prediction['display_report'])
        self.assertEqual(single['items'],[])

    def test_latest_incomplete_roster_does_not_restore_old_reference(self):
        self.archive.ingest(92,[[report(2,300,army=ARMY)]],310)
        latest=report(3,400,army=ARMY)
        latest.pop('attack_all_hero_info')
        self.archive.ingest(92,[[latest]],410)
        prediction,single=self.views()
        self.assertIsNone(prediction['display_report'])
        self.assertEqual(single['items'],[])

    def test_legion_members_keep_personal_identity_and_badge(self):
        self.row['army_group_id']='legion'
        prediction,single=self.views()
        self.assertIsNone(prediction['display_report'])
        self.assertEqual(single['items'],[])
        latest=report(2,400,army=ARMY)
        latest['attack_army_group']='测试军团'
        self.archive.ingest(92,[[latest]],410)
        prediction,single=self.views()
        self.assertTrue(prediction['display_verified'])
        self.assertEqual(single['items'][0]['data']['army_group_name'],'测试军团')
        self.assertEqual(prediction['army_id'],ARMY)

    def test_no_live_army_does_not_expose_old_roster(self):
        self.snapshot['armies']=[]
        self.assertEqual(army_lineup(self.archive,self.snapshot,ARMY)['items'],[])

    def test_changed_owner_is_retried_instead_of_exposing_other_player(self):
        self.row['owner']='another-owner'
        with self.assertRaises(TimeoutError):
            army_lineup(self.archive,self.snapshot,ARMY)


if __name__ == '__main__':
    unittest.main()
