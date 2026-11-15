import json
import tempfile
import unittest
from pathlib import Path

from archive import Archive
from test_attendance_retention import battle, FLOOR, PROFILE
import team_catalog as teams
import territory_events as land
import attendance


class Contributions(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.a=Archive(Path(self.tmp.name),profile=dict(PROFILE));self.a.attendance.close()

    def tearDown(self):
        self.a.close();self.tmp.cleanup()

    def add(self,b):
        self.a.ingest(92,[[b]],FLOOR+5000);self.a.attendance.drain()

    def query(self,view='teams',**kw):
        return teams.query(self.a,dict(view=view,player_key='battle:role-a',**kw))['items']

    def test_same_ordered_heroes_merge_ids_but_position_change_is_another_team(self):
        self.add(battle())
        b=battle(2,FLOOR+200);b['attack_idu']='99,501,502,503';self.add(b)
        self.assertEqual(len(self.query()),1);self.assertEqual(self.query()[0]['samples'],2)
        b=battle(3,FLOOR+300);raw=b['attack_all_hero_info'].strip(';').split(';')
        b['attack_all_hero_info']=';'.join([raw[1],raw[0],raw[2]])+';';self.add(b)
        self.assertEqual(len(self.query()),2)
        self.add(battle(4,FLOOR+400))
        self.assertEqual(sorted(r['samples']for r in self.query()),[1,3])

    def test_config_changes_and_latest_use_battle_time_not_receipt(self):
        self.add(battle(1,FLOOR+300))
        scout=battle(2,FLOOR+400,main=False);self.add(scout)
        self.add(battle(3,FLOOR+100))
        row=self.query()[0];self.assertEqual(row['category'],'spartan')
        self.assertEqual((row['samples'],row['main_samples']),(3,2))
        self.assertEqual(len(self.query('configs')),2)
        self.assertEqual(self.query(to_at=FLOOR+350)[0]['category'],'main')
        self.assertEqual(len(self.query('events')),3)
        matches=self.query('matchups');self.assertEqual(matches[0]['samples'],2)

    def test_hp_and_dictionary_changes_do_not_create_config(self):
        self.add(battle())
        b=battle(2,FLOOR+200);b['attack_all_hero_info']=b['attack_all_hero_info'].replace('10000,8000,1000','7000,5000,1000');self.add(b)
        self.assertEqual(len(self.query('configs')),1)
        data=self.query()[0]['heroes'];changed=json.loads(json.dumps(data));changed[0]['name']='字典修正'
        self.assertEqual(teams.config_signature(data),teams.config_signature(changed))

    def test_npc_side_never_becomes_player_team_or_strength_sample(self):
        b=battle();b.update(npc=1,defend_idu='0,0,0,0',world_npc_army='123,509,0,0')
        self.add(b)
        row=self.query()[0];self.assertEqual((row['pve_samples'],row['main_samples']),(1,0))
        self.assertIsNone(row['main_win_rate']);self.assertIsNone(row['exchange'])
        self.assertFalse(teams.query(self.a,dict(view='teams',player_key='battle:role-b'))['items'])
        self.assertEqual(attendance.query(self.a,dict(include_pve='1',**{'from':'2026-11-12','to':'2026-11-12'}))['items'][0]['pve_samples'],1)

    def test_shared_classification_boundaries_and_unknown_slots(self):
        self.add(battle())
        self.assertEqual(self.query()[0]['category'],'main')
        b=battle(2,FLOOR+200);b['attack_all_hero_info']=b['attack_all_hero_info'].replace(',40,',',39,');self.add(b)
        self.assertEqual(self.query()[0]['category'],'spartan')
        b=battle(3,FLOOR+300);b.pop('all_skill_info');self.add(b)
        self.assertEqual(self.query()[0]['category'],'unknown')

    def tile(self,owner,union,at,city=2):
        raw=[0]*21;raw[0]=city;raw[2]=owner;raw[3]=union
        land.observe_tile(self.a,10050930,raw,at,str(at))

    def player(self,uid,role,name,union):
        self.a.db.execute('INSERT OR REPLACE INTO players(profile_id,player_key,world_uid,role_id,name,union_id,union_name,observed_at,source,event_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
            (self.a.profile_id,'world:'+str(uid),str(uid),role,name,str(union),'',FLOOR,'verified_test',FLOOR))

    def test_capture_lost_owner_dedup_and_npc_victory_is_not_capture(self):
        self.player(1,'role-a','甲',801);self.player(2,'role-b','乙',802)
        self.tile(2,802,FLOOR+90)
        self.add(battle())
        self.assertEqual(land.query(self.a,{})['items'],[])
        b=battle(2);b.update(result=2,city_type=2,wid_name='土地Lv.5');self.add(b)
        self.tile(1,801,FLOOR+101)
        self.assertEqual(len(land.query(self.a,{})['items']),1)
        self.add(dict(b,battle_id=3))
        self.assertEqual(len(land.query(self.a,{})['items']),1)
        self.assertEqual(land.player_stats(self.a,'801',FLOOR,FLOOR+200)['battle:role-a']['capture_high'],1)
        self.assertEqual(land.player_stats(self.a,'802',FLOOR,FLOOR+200)['battle:role-b']['lost_land'],1)

    def test_offline_gap_transfer_and_building_change_never_fake_contributions(self):
        self.tile(1,801,FLOOR+100,city=4);self.tile(2,801,FLOOR+110)
        self.assertEqual({r['kind']for r in land.query(self.a,{})['items']},{'transfer','building_change'})
        self.tile(3,802,FLOOR+500)
        self.assertEqual(land.query(self.a,{})['items'][0]['kind'],'ownership_gap')
        totals=land.player_stats(self.a,'801',FLOOR,FLOOR+1000)
        self.assertFalse(any(r['capture_low']or r['lost_land']for r in totals.values()))

    def test_metadata_indices_are_explicit_and_missing_boundary_honor_is_unknown(self):
        self.player(1,'role-a','甲',801)
        land.observe_tables(self.a,[[1,'Tb_user_union_attr',[1,int(FLOOR)]]],FLOOR+100,'1')
        raw=[0]*102;raw[0]=1;raw[100]=999;raw[101]=12345
        land.observe_tables(self.a,[[1,'Tb_user_res',raw]],FLOOR+101,'2')
        self.add(battle())
        row=attendance.query(self.a,{'from':'2026-11-12','to':'2026-11-12'})['items'][0]
        self.assertEqual((row['joined_at'],row['cumulative_honor']),(FLOOR,12345))
        self.assertIsNone(row['new_honor'])
        land.observe_tables(self.a,[[2,'Tb_user_res',[0,1,101,12399]]],FLOOR+102,'3')
        row=attendance.query(self.a,{'from':'2026-11-12','to':'2026-11-12'})['items'][0]
        self.assertEqual(row['cumulative_honor'],12399)

    def test_query_filters_scope_and_pagination(self):
        self.add(battle())
        self.assertEqual(teams.query(self.a,dict(view='players',relation='ally'))['items'][0]['player_name'],'甲')
        self.assertEqual(teams.query(self.a,dict(view='players',union_id='802'))['items'][0]['player_name'],'乙')
        self.assertEqual(self.query(coord='1005,930')[0]['samples'],1)
        self.assertEqual(self.query(coord='1006,930'),[])
        self.assertEqual(teams.query(self.a,dict(view='players',limit=1))['has_more'],True)
        self.assertEqual(len(teams.query(self.a,dict(view='players',limit=1,offset=1))['items']),1)
        summary=teams.query(self.a,dict(view='summary',union_id='801'))
        self.assertEqual((summary['observed_names'],summary['battle_reports']),(1,1))

    def test_exchange_is_ratio_of_sums_and_partial_data_never_becomes_zero(self):
        self.add(battle())
        b=battle(2,FLOOR+200)
        b['defend_all_hero_info']=b['defend_all_hero_info'].replace('10000,8000,1000','10000,9000,500')
        self.add(b)
        row=self.query()[0];self.assertAlmostEqual(row['main_exchange'],11000/12000)
        b=battle(3,FLOOR+300);b['attack_all_hero_info']='100701,40;100807,40;100703,40;'
        self.add(b)
        self.assertIsNone(self.query()[0]['exchange'])

    def test_world_to_role_identity_and_report_map_dedup_after_late_bridge(self):
        self.player(2,'role-b','乙',802)
        self.tile(2,802,FLOOR+90);self.tile(1,801,FLOOR+100)
        self.player(1,'role-a','甲',801)
        b=battle(1,FLOOR+100);b.update(result=2,city_type=2,wid_name='土地Lv.4');self.add(b)
        self.assertEqual(len(land.query(self.a,{})['items']),1)
        self.assertEqual(land.player_stats(self.a,'801',FLOOR,FLOOR+200)['battle:role-a']['capture_low'],1)
        self.assertEqual(len(land.query(self.a,{'player_key':'battle:role-a'})['items']),1)

    def test_map_durability_is_recorded_without_inventing_personal_demolition(self):
        land.observe_durability(self.a,10050930,{'17':[1,90,0,0,0,0,'要塞']},FLOOR+100,'a')
        land.observe_durability(self.a,10050930,{'17':[1,60,0,0,0,0,'要塞']},FLOOR+101,'b')
        self.assertEqual(self.a.db.execute('SELECT COUNT(*) FROM durability_snapshots').fetchone()[0],2)
        self.assertEqual(land.player_stats(self.a,'801',FLOOR,FLOOR+200),{})

    def test_missing_opponent_heroes_are_still_verified_player_combat(self):
        b=battle();b['defend_all_hero_info']='';self.add(b)
        row=self.query()[0]
        self.assertEqual((row['pvp_samples'],row['pve_samples'],row['main_samples']),(1,0,0))
        self.assertIsNone(row['exchange']);self.assertIsNone(row['main_win_rate'])
        # Simulate a previously indexed row and ensure only affected reports requeue.
        self.a.db.execute("UPDATE formation_facts SET pvp=0")
        self.a.db.execute("DELETE FROM lineup_rule_migrations WHERE rule='formation-player-identity-2'")
        self.a.attendance.drain()
        self.assertEqual(self.query()[0]['pvp_samples'],1)

    def test_unflagged_bandit_camp_is_retained_without_counting_player_attendance(self):
        b=battle();b.update(npc=0,defend_role_id=None,defend_name='流寇营地',defend_all_hero_info='')
        self.add(b)
        self.assertEqual(self.query()[0]['pve_samples'],1)
        self.assertFalse(self.a.db.execute('SELECT 1 FROM attendance_facts').fetchone())
        self.assertTrue(self.a.db.execute('SELECT 1 FROM battle_reports').fetchone())


if __name__=='__main__':unittest.main()
