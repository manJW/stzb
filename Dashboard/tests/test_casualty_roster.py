"""An omitted fallen hero is not an empty roster or another personal army."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_support import Archive
from test_intelligence import battle
from lineup_prediction import army_lineup,player_history,predict_alerts
from report_priority import ReportPriority


class CasualtyRosterTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.archive=Archive(Path(self.temp.name))
        self.row={'id':'9001','owner':'200','player_name':'enemy','union_id':'1008','state_code':4,
                  'from':[510,610],'target':[500,600],'begin':300,'end':800,'troops':[{'code':c}for c in (21,32,3)]}
        scene=[None]*34;scene[1]={'200':['enemy',0,1008]+[0]*9+[[0,0,'union']]}
        raw=[0]*33;raw[:6]=[4,200,5100610,5000600,300,800];raw[16]='2,21;2,32;2,3;';scene[6]={'9001':raw}
        self.archive.ingest(5028,scene,310)

    def tearDown(self):self.archive.close();self.temp.cleanup()

    def report(self,bid,*,partial=False,side='attack',time=300,army=None):
        value=battle(bid,time,main=True)
        if side=='defend':
            value={k:v for k,v in value.items()if not k.startswith(('attack','defend'))}
            value.update(defend_name='enemy',defend_unionid=1008)
        prefix=side
        value.update({prefix+'_idu':f'{army if army is not None else "0"if partial else "9001"},11,12,13,0',
            prefix+'_role_id':'same-role',prefix+'_hero_type':('0,21,0,3,'if partial else'0,21,32,3,')if side=='attack'else('3,0,21,0,'if partial else'3,32,21,0,'),
            prefix+'_advance':'0,0,0,0,0,0;0,0,0,0,0,0;5,0,0,0,0,0;0,0,0,0,0,0;'if side=='attack'else'0,0,0,0,0,0;5,0,0,0,0,0;0,0,0,0,0,0;0,0,0,0,0,0;',
            prefix+'_all_hero_info':('100701,43,1000,300,400;0,0,0,0,0;100703,44,500,0,200;'if partial else'100701,43,9000,1000,2000;100807,45,9000,0,3000;100703,44,9000,500,4000;'),
            prefix+'_hp':1500 if partial else 27000})
        slots=(1,2,3)if side=='attack'else(6,5,4)
        value['all_skill_info']=''.join((f'{slot},0,0,0,0,0,0;'if partial and i==2 else f'{slot},{200001+i},10,{200011+i},8,{200021+i},7;')for i,slot in enumerate(slots,1))
        return value

    def ingest(self,full=None,partial=None):
        self.archive.ingest(92,[[full or self.report(1)]],311)
        self.archive.ingest(92,[[partial or self.report(2,partial=True)]],312)

    def views(self,at=350):
        snapshot={'server_now':at,'armies':[self.row],'alerts':[{'army_id':'9001'}]}
        prediction=predict_alerts(self.archive,snapshot)['9001']
        live=army_lineup(self.archive,snapshot,'9001')
        with patch('archive.time.time',return_value=at):archived=self.archive.query('army_lineups',army_id='9001')
        return prediction,live,archived

    def test_latest_consecutive_battle_recovers_fallen_middle_without_mutating_raw_reports(self):
        self.ingest();before=self.archive.db.execute('SELECT heroes FROM combat_sides WHERE battle_id="2"AND side="attack"').fetchone()[0]
        prediction,live,archived=self.views()
        for value in (live,archived):
            self.assertEqual(value['current_report_id'],'2');self.assertEqual(value['match'],'same_card_instances')
            self.assertNotIn('unresolved',value['current_resolution'])
            self.assertEqual([h['id']for h in value['items'][0]['data']['heroes']],[100701,100807,100703])
        heroes=live['items'][0]['data']['heroes'];self.assertEqual(heroes[1]['red_stars'],5)
        self.assertEqual(heroes[1]['level'],45);self.assertEqual(heroes[1]['skill_slot_ids'],[200003,200013,200023])
        self.assertEqual((heroes[1]['initial_hp'],heroes[1]['remaining_hp'],heroes[1]['wounded_hp']),(0,0,0))
        self.assertEqual(heroes[0]['initial_hp'],1000);self.assertEqual(heroes[0]['remaining_hp'],300)
        self.assertEqual(prediction['display_report']['battle_id'],'2');self.assertTrue(prediction['display_verified'])
        self.assertEqual(ReportPriority.ready(archived)['battle_id'],'2')
        self.assertEqual(self.archive.db.execute('SELECT heroes FROM combat_sides WHERE battle_id="2"AND side="attack"').fetchone()[0],before)

    def test_same_rule_handles_defending_heroes_and_position_order(self):
        self.ingest(self.report(1,side='defend'),self.report(2,partial=True,side='defend'))
        _,live,_=self.views();heroes=live['items'][0]['data']['heroes']
        self.assertEqual([h['position']for h in heroes],[1,2,3]);self.assertEqual(heroes[1]['red_stars'],5)

    def test_audit_compares_the_displayed_casualty_roster_and_still_detects_wrong_loadouts(self):
        from lineup_audit import army_rows,check_army
        from lineup_prediction import predict
        self.ingest()
        row=army_rows(self.archive.db,self.archive.profile_id,['9001'])[0]
        history=player_history(self.archive.db,self.archive.profile_id,'enemy','1008')
        result=check_army(self.archive.db,self.archive.profile_id,row,history,350)
        self.assertEqual(result['issues'],[])
        self.assertEqual(result['category'],'resolved')
        for field,value in (('red_stars',1),('level',20),('skill_slot_ids',[200001,200002,200003])):
            def wrong(*args):
                answer=predict(*args)
                answer['items'][0]['heroes'][1][field]=value
                return answer
            result=check_army(self.archive.db,self.archive.profile_id,row,history,350,predictor=wrong)
            self.assertIn('displayed_loadout_mismatch',result['issues'],field)

    def test_multiple_consecutive_losses_keep_roster_and_latest_combat_values(self):
        self.ingest();third=self.report(3,partial=True)
        third['attack_all_hero_info']='100701,43,300,0,100;0,0,0,0,0;0,0,0,0,0;'
        third['attack_hero_type']='0,21,0,0,';third['attack_hp']=300
        self.archive.ingest(92,[[third]],313)
        _,live,_=self.views();self.assertEqual(live['current_report_id'],'3')
        self.assertEqual(len(live['items'][0]['data']['heroes']),3)
        self.assertEqual(live['items'][0]['data']['heroes'][0]['wounded_hp'],100)
        self.assertEqual(live['items'][0]['data']['heroes'][1]['roster_source_battle_id'],'1')

    def test_five_minute_battle_rest_and_following_casualty_reports_keep_the_same_cards(self):
        self.ingest(partial=self.report(2,partial=True,time=600))
        third=self.report(3,partial=True,time=900)
        third.update(attack_all_hero_info='100701,43,300,0,100;0,0,0,0,0;0,0,0,0,0;',attack_hero_type='0,21,0,0,',attack_hp=300)
        self.archive.ingest(92,[[third]],913)
        _,live,_=self.views(at=950)
        self.assertEqual(live['current_report_id'],'3');self.assertEqual(len(live['items'][0]['data']['heroes']),3)
        self.assertEqual(live['items'][0]['data']['heroes'][1]['roster_source_battle_id'],'1')

    def test_missing_source_or_unproven_casualty_stays_unresolved(self):
        cases=[]
        full=self.report(1);full['attack_all_hero_info']=full['attack_all_hero_info'].replace('9000,0,3000','9000,1,3000');cases.append((full,self.report(2,partial=True)))
        partial=self.report(2,partial=True);partial['attack_all_hero_info']=partial['attack_all_hero_info'].replace('43,1000,300','43,999,300');cases.append((self.report(1),partial))
        partial=self.report(2,partial=True);partial['attack_all_hero_info']='100701,43,1000,300,400;';cases.append((self.report(1),partial))
        partial=self.report(2,partial=True,time=700);cases.append((self.report(1),partial))
        partial=self.report(2,partial=True);partial['wid']=5100611;cases.append((self.report(1),partial))
        for full,partial in cases:
            with self.subTest(full=full['attack_all_hero_info'],time=partial['time'],wid=partial['wid']):
                self.ingest(full,partial);prediction,live,_=self.views(at=800)
                self.assertIsNone(prediction['display_report']);self.assertEqual(live['items'],[])

    def test_same_player_other_army_or_cards_never_supply_missing_hero(self):
        for changes in ({'attack_idu':'9002,11,12,13,0'},{'attack_idu':'0,11,99,13,0'},
                        {'attack_idu':'0,12,11,13,0'},{'attack_role_id':'another-role'}):
            with self.subTest(changes=changes):
                partial={**self.report(2,partial=True),**changes};self.ingest(partial=partial)
                result=predict_alerts(self.archive,{'server_now':350,'armies':[self.row],'alerts':[{'army_id':'9001'}]})['9001']
                self.assertFalse((result.get('current_resolution')or{}).get('casualty_recovery'))

    def test_a_full_report_not_received_yet_cannot_repair_an_earlier_replay(self):
        self.archive.ingest(92,[[self.report(2,partial=True)]],312)
        self.archive.ingest(92,[[self.report(1)]],340)
        prediction,live,_=self.views(at=330)
        self.assertFalse((prediction.get('current_resolution')or{}).get('casualty_recovery'))
        self.assertEqual(live['items'],[])


if __name__=='__main__':unittest.main()
