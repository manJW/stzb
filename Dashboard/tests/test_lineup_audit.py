import json
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from lineup_audit import LineupAudit,army_rows,audit_group,check_army
from lineup_prediction import player_history,predict
import test_garrison_lineup_current as fixture
report,ARMY = fixture.report,fixture.ARMY


class LineupAuditTests(unittest.TestCase):
    setUp = fixture.GarrisonCurrentTests.setUp
    tearDown = fixture.GarrisonCurrentTests.tearDown
    observe = fixture.GarrisonCurrentTests.observe
    def data(self):
        self.archive.ingest(92,[[report(2,300)]],310)
        self.archive.ingest(92,[[report(3,400,side='defend')]],410)
        rows=army_rows(self.archive.db,self.archive.profile_id)
        history=player_history(self.archive.db,self.archive.profile_id,'player','1009')
        return rows,history

    def test_matching_paths_have_no_issues(self):
        rows,_=self.data()
        result=audit_group(self.archive.db,self.archive.profile_id,rows,700)
        self.assertEqual(result[0]['issues'],[])
        self.assertEqual(result[0]['category'],'resolved')

    def test_stale_prediction_report_is_detected(self):
        rows,history=self.data()
        def stale(*args):
            value=predict(*args)
            value['current_report_id']='1'
            return value
        result=check_army(self.archive.db,self.archive.profile_id,rows[0],history,700,predictor=stale)
        self.assertIn('current_report_mismatch',result['issues'])

    def test_wrong_redness_or_skill_is_detected(self):
        rows,history=self.data()
        def wrong(*args):
            value=predict(*args)
            value['items'][0]['heroes'][0]['red_stars']=0
            return value
        result=check_army(self.archive.db,self.archive.profile_id,rows[0],history,700,predictor=wrong)
        self.assertIn('displayed_loadout_mismatch',result['issues'])

    def test_wrong_main_label_is_detected(self):
        rows,history=self.data()
        def wrong(*args):
            value=predict(*args)
            value['force_assessment']['kind']='unknown'
            return value
        result=check_army(self.archive.db,self.archive.profile_id,rows[0],history,700,predictor=wrong)
        self.assertIn('force_label_mismatch',result['issues'])

    def test_missing_skill_data_is_not_reported_as_a_program_mismatch(self):
        rows,_=self.data()
        new=report(4,500)
        new.pop('all_skill_info')
        self.archive.ingest(92,[[new]],510)
        result=audit_group(self.archive.db,self.archive.profile_id,rows,700)[0]
        self.assertEqual(result['issues'],[])
        self.assertEqual(result['category'],'source_fields_missing')
        self.assertIn('skill_slots',result['missing_fields'])

    def test_incomplete_roster_is_not_restored_from_an_older_main(self):
        rows,_=self.data()
        new=report(4,500)
        new['attack_all_hero_info']=new['attack_all_hero_info'].split(';')[0]+';'
        self.archive.ingest(92,[[new]],510)
        result=audit_group(self.archive.db,self.archive.profile_id,rows,700)[0]
        self.assertEqual(result['issues'],[])
        self.assertEqual(result['category'],'incomplete_current_report')

    def test_confirmed_empty_slots_do_not_become_unknown_merely_from_age(self):
        rows,_=self.data()
        new=report(4,500)
        new['all_skill_info']=''.join(f'{slot},200001,10,200011,8,0,0;' for slot in (1,2,3))
        self.archive.ingest(92,[[new]],510)
        at=500+7*24*3600
        result=audit_group(self.archive.db,self.archive.profile_id,rows,at)[0]
        self.assertEqual(result['issues'],[])
        history=player_history(self.archive.db,self.archive.profile_id,'player','1009')
        value=predict({'id':ARMY,'owner':'12276','troops':[{'code':21},{'code':11},{'code':31}]},history,at)
        self.assertEqual(value['force_assessment']['kind'],'spartan')

    def test_background_audit_records_results_without_writing_battle_data(self):
        self.data()
        before=self.archive.db.execute('SELECT COUNT(*) FROM messages').fetchone()[0]
        task=LineupAudit(self.archive,interval=60,group_pause=0)
        task.start()
        try:
            deadline=time.monotonic()+4
            while time.monotonic()<deadline:
                if task.path.exists():
                    data=json.loads(task.path.read_text(encoding='utf-8'))
                    if not data['running']:
                        break
                task.stop_event.wait(.01)
            else:
                self.fail('Background audit did not save its result')
            self.assertEqual(data['checked_armies'],1)
            self.assertEqual(data['problem_armies'],0)
            self.assertEqual(self.archive.db.execute('SELECT COUNT(*) FROM messages').fetchone()[0],before)
        finally:
            task.close()

    def test_state_and_report_are_checked_in_the_same_group_snapshot(self):
        self.data()
        from observation_codec import digest
        changed = False
        def update_before_group(value):
            nonlocal changed
            if not changed:
                changed = True
                scene = [None]*34
                raw = [0]*33
                raw[0:6] = [5,12276,9800910,9800910,450,460]
                raw[16] = '1,11;2,31;2,31;'
                scene[6] = {ARMY:raw}
                self.archive.ingest(5028,scene,470)
                self.archive.ingest(92,[[report(4,500,old=True,army=ARMY)]],510)
            return digest(value)
        task=LineupAudit(self.archive,group_pause=0)
        with patch('lineup_audit.digest',side_effect=update_before_group):
            task.check_round(self.archive.db,self.archive.profile_id,{})
        answer=json.loads(task.path.read_text(encoding='utf-8'))
        self.assertEqual(answer['problem_armies'],0,
            'An update received during a sweep must not compare an old march with a new report snapshot')


if __name__=='__main__':unittest.main()
