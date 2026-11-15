import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from test_support import Archive
from report_priority import ReportPriority
from test_garrison_lineup_current import report

A, B = '113709493', '113709494'


class PriorityTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.archive=Archive(self.root);self.now=time.time()
        self.heads=[];self.enriched=[];self.yields=[]
        self.snapshot={'armies':[],'focus':None}
        self.harvester=SimpleNamespace(request_latest=lambda:self.heads.append(True),
                                       yield_history=lambda n:self.yields.append(n))
        self.reader=SimpleNamespace(prioritize_army=lambda aid,**kw:self.enriched.append(aid))
        self.priority=ReportPriority(self.archive,self.harvester,self.reader,lambda:self.snapshot)
        self.observe(A,1,begin=int(self.now)-100,end=int(self.now)+10)
        self.observe(B,1,begin=int(self.now)-100,end=int(self.now)+10)
        self.archive.ingest(92,[[report(1,self.now-200,army=A)],
                                [report(2,self.now-190,army=B,cards='31,32,33')]],self.now-100)

    def tearDown(self):
        self.archive.close();self.temp.cleanup()

    def observe(self,aid,state,*,begin=None,end=None,origin=9820923):
        begin=int(self.now) if begin is None else begin
        end=begin+300 if end is None else end
        scene=[None]*34;scene[1]={'12276':['player',0,1009]+[0]*9+[[0,0,'alliance']]}
        raw=[0]*33;raw[:6]=[state,12276,origin,9820923,begin,end]
        raw[16]='2,21;1,11;2,31;';scene[6]={aid:raw}
        self.archive.ingest(5028,scene,self.now)
        row=dict(id=aid,owner='12276',state_code=state,state_id=begin,target_type=0,
                 begin=begin,end=end,seen_at=self.now)
        self.snapshot['armies']=[r for r in self.snapshot['armies'] if r['id']!=aid]+[row]
        return row

    def test_rest_requests_head_once_and_old_lineup_does_not_complete_new_battle(self):
        self.observe(A,7);self.priority.step()
        e=self.priority.events[A]
        self.assertEqual(e['reasons'],['battle_rest']);self.assertEqual(e['status'],'waiting')
        self.assertEqual(self.heads,[True]);self.assertEqual(self.enriched,[A])
        self.priority.step();self.assertEqual(len(self.heads),1)
        self.assertNotIn('lineup_ready_at',e)

    def test_arrival_return_triggers_but_early_manual_recall_does_not(self):
        self.priority.step()
        self.observe(A,4);self.priority.step()
        self.assertIn(A,self.priority.events)
        self.assertEqual(self.priority.events[A]['reasons'],['post_battle_retreat'])
        self.assertNotIn(B,self.priority.events)
        self.observe(B,4,begin=int(self.now)-80);self.priority.step()
        self.assertNotIn(B,self.priority.events)

    def test_selected_incomplete_refreshes_head_but_complete_selection_does_not(self):
        self.snapshot['focus']=A;self.priority.step()
        self.assertFalse(self.heads)
        newer=report(3,self.now-5,army=A);newer.pop('all_skill_info')
        self.archive.ingest(92,[[newer]],self.now-.1)
        self.snapshot['focus']=None;self.priority.step()
        self.snapshot['focus']=A;self.priority.step()
        self.assertEqual(self.priority.events[A]['reasons'],['selected_incomplete'])
        self.assertEqual(len(self.heads),1)

    def test_new_report_only_completes_its_own_army_and_full_is_separate(self):
        self.observe(A,7);self.observe(B,7);self.priority.step()
        new=report(3,int(self.now),army=A)
        self.archive.ingest(92,[[new]],time.time())
        for e in self.priority.events.values():e['next_check']=0
        self.priority.step()
        self.assertEqual(self.priority.events[A]['battle_id'],'3')
        self.assertIn('lineup_ready_at',self.priority.events[A])
        self.assertNotIn('lineup_ready_at',self.priority.events[B])
        self.assertEqual(self.priority.events[A]['status'],'waiting')
        version=self.priority.updates[A]
        self.assertFalse(self.priority.displayed(B,version))
        self.assertFalse(self.priority.displayed(A,'another-version'))
        self.assertTrue(self.priority.displayed(A,version))
        self.assertFalse(self.priority.displayed(A,version))
        self.archive.ingest(11,{'battle_id':3,'report':'full'},time.time())
        self.priority.events[A]['next_check']=0;self.priority.step()
        self.assertEqual(self.priority.events[A]['status'],'done')

    def test_old_initial_rest_and_changed_owner_do_not_trigger_or_keep_old_identity(self):
        self.observe(A,7,begin=int(self.now)-1000);self.priority.step()
        self.assertNotIn(A,self.priority.events)
        row=self.observe(A,7);self.priority.step()
        row['owner']='another-owner';self.priority.events[A]['next_check']=0
        self.priority.step();self.assertEqual(self.priority.events[A]['status'],'owner_changed')

    def test_unverified_candidates_cannot_finish_or_emit_display_version(self):
        data=self.archive.query('army_lineups',army_id=A)
        data['match']='name_union_troop_candidate'
        self.assertIsNone(self.priority.ready(data))

    def test_urgent_enrichment_reuses_current_cache_outside_the_writer_transaction(self):
        from evidence_reader import ReportEvidenceReader
        self.archive.query('army_lineups',army_id=A)
        reader=ReportEvidenceReader(self.archive,SimpleNamespace(),lambda:None,False)
        try:
            with patch('lineup_prediction.player_history',side_effect=AssertionError('Repeated history scan')):
                reader.prioritize_army(A,urgent=True)
        finally:reader.close()

    def test_group_focus_only_prioritizes_incomplete_member_without_merging_other_member(self):
        new=report(3,self.now-5,army=B,cards='31,32,33');new.pop('all_skill_info')
        self.archive.ingest(92,[[new]],self.now-.1)
        self.snapshot.update(focus_members=[A,B],focus_event='group-event')
        self.priority.step()
        self.assertNotIn(A,self.priority.events)
        self.assertIn(B,self.priority.events)
        self.assertEqual(self.enriched,[B])

    def test_legion_prediction_cannot_borrow_another_member_formation(self):
        from lineup_prediction import player_history,predict
        new=report(3,self.now-20,cards='41,42,43')
        self.archive.ingest(92,[[new]],self.now-.1)
        history=player_history(self.archive.db,self.archive.profile_id,'player','1009')
        row={'id':'113709499','owner':'12276','army_group_id':113310331,
             'troops':[{'code':21},{'code':11},{'code':31}]}
        unknown=predict(row,history,time.time())
        self.assertIsNone(unknown['current_report_id'])
        self.assertNotIn('lead_loadout',unknown)
        self.assertEqual(unknown['force_assessment']['kind'],'unknown')
        linked=predict({**row,'id':A},history,time.time())
        self.assertEqual(linked['current_report_id'],'1')

    def test_legion_expedition_unique_arrival_links_each_personal_army(self):
        captured=self.now;self.now-=10
        row=self.observe(A,12,begin=int(captured)-20,end=int(captured)-5,origin=9780916)
        self.now=captured
        new=report(8,row['end'])
        self.archive.ingest(92,[[new]],time.time())
        links=self.archive.db.execute("SELECT method FROM battle_march_links WHERE battle_id='8' AND side='attack'").fetchall()
        self.assertEqual([r[0]for r in links],['unique_attack_arrival'])

    def test_two_legion_members_of_same_player_at_same_arrival_remain_unbound(self):
        captured=self.now;self.now-=10
        row=self.observe(A,12,begin=int(captured)-20,end=int(captured)-5,origin=9780916)
        self.observe(B,12,begin=row['begin'],end=row['end'],origin=9780916)
        self.now=captured
        self.archive.ingest(92,[[report(8,row['end'])]],time.time())
        self.assertFalse(self.archive.db.execute("SELECT 1 FROM battle_march_links WHERE battle_id='8' AND method='unique_attack_arrival'").fetchone())


if __name__=='__main__':unittest.main()
