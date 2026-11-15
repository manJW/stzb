import json
import sqlite3
import tempfile
import time
import unittest
import zlib
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from observation_codec import report_system_npc, report_has_npc
from archive import Archive
from model import WorldState, default_config, write_json
from attendance import query, save_rules, classify, export_csv, TZ, wuxun
from retention import prune, sanitize
from harvest import BattleHarvester

FLOOR=datetime(2026,11,12,tzinfo=TZ).timestamp()
PROFILE=dict(server="server-45122",season="season-4",account_role_id="p1",account_name="本方",union_id="801",union_name="甲盟")

def battle(bid=1,at=FLOOR+100,main=True):
    heroes="100701,40,10000,8000,1000;100807,40,10000,7000,2000;100703,40,10000,9000,500;"
    skills=";".join(f"{i},200001,10,200002,10,{200003 if main else 0},{10 if main else 0}" for i in range(1,7))+";"
    return dict(battle_id=bid,time=at,attack_name="甲",attack_role_id="role-a",attack_unionid=801,
     defend_name="乙",defend_role_id="role-b",defend_unionid=802,npc=0,result=1,wid=10050930,
     attack_all_hero_info=heroes,defend_all_hero_info=heroes,all_skill_info=skills,
     attacker_gongxun=0,defender_gongxun=0,attacker_xwc=77803,defender_xwc=69803,attack_idu="11,101,102,103",defend_idu="22,201,202,203")

class Cases(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.archive=Archive(self.root,profile=dict(PROFILE));self.archive.attendance.close()
    def tearDown(self):self.archive.close();self.tmp.cleanup()
    def ingest(self,value):
        self.archive.ingest(92,[[value]],FLOOR+10000);self.archive.attendance.drain()
    def test_main_spartan_classified_at_battle_time_and_duplicate_receipts(self):
        self.ingest(battle());self.ingest(battle());self.ingest(battle(2,main=False))
        result=query(self.archive,dict(from_="",**{"from":"2026-11-12","to":"2026-11-12"}))
        row=result['items'][0]
        self.assertEqual((row['participations'],row['main'],row['spartan'],row['honor']),(2,1,1,200))
        self.assertEqual(row['main_win_rate'],100);self.assertEqual(row['exchange'],1)
        self.assertIsNone(row['score'])
    def test_win_rate_and_exchange_require_both_sides_main(self):
        self.ingest(battle())
        scout=battle(2);scout['result']=0
        scout['defend_all_hero_info']=scout['defend_all_hero_info'].replace(',40,',',20,')
        self.ingest(scout)
        unknown=battle(3);unknown['result']=0
        unknown['all_skill_info']=';'.join(unknown['all_skill_info'].split(';')[:3])+';'
        self.ingest(unknown)
        row=query(self.archive,{'from':'2026-11-12','to':'2026-11-12'})['items'][0]
        self.assertEqual((row['participations'],row['main'],row['main_duels']),(3,3,1))
        self.assertEqual((row['main_wins'],row['main_decided'],row['main_win_rate'],row['exchange']),(1,1,100,1))
        self.archive.profile['union_id']='802'
        reverse=query(self.archive,{'from':'2026-11-12','to':'2026-11-12'})['items'][0]
        self.assertEqual((reverse['main_duels'],reverse['main_win_rate'],reverse['exchange']),(1,0,1))
    def test_missing_opponent_fact_does_not_enter_main_statistics(self):
        self.ingest(battle())
        with self.archive.db:self.archive.db.execute("DELETE FROM attendance_facts WHERE side='defend'")
        row=query(self.archive,{'from':'2026-11-12','to':'2026-11-12'})['items'][0]
        self.assertEqual(row['main'],1);self.assertEqual(row['main_duels'],0)
        self.assertIsNone(row['main_win_rate']);self.assertIsNone(row['exchange'])
    def test_opponent_correction_updates_eligibility_incrementally(self):
        value=battle();value['defend_all_hero_info']=value['defend_all_hero_info'].replace(',40,',',20,')
        self.ingest(value)
        self.assertIsNone(query(self.archive,{'from':'2026-11-12','to':'2026-11-12'})['items'][0]['main_win_rate'])
        self.ingest(battle())
        row=query(self.archive,{'from':'2026-11-12','to':'2026-11-12'})['items'][0]
        self.assertEqual((row['main_duels'],row['main_win_rate'],row['exchange']),(1,100,1))
    def test_details_use_this_reports_own_and_opposing_lineups(self):
        self.ingest(battle())
        later=battle(2,FLOOR+200)
        later['attack_all_hero_info']=later['attack_all_hero_info'].replace('100701,','100814,')
        later['defend_name']='后一个对手'
        self.ingest(later)
        items=query(self.archive,{'from':'2026-11-12','to':'2026-11-12','player_key':'battle:role-a'})['items']
        self.assertEqual([r['battle_id'] for r in items],['2','1'])
        self.assertEqual([r['own_heroes'][0]['id'] for r in items],[100814,100701])
        self.assertEqual([r['opponent_name'] for r in items],['后一个对手','乙'])
        self.assertEqual(items[0]['opponent_heroes'][0]['id'],100701)
        self.archive.profile['union_id']='802'
        reverse=query(self.archive,{'from':'2026-11-12','to':'2026-11-12','player_key':'battle:role-b'})['items']
        self.assertEqual(reverse[0]['own_heroes'][0]['id'],100701)
        self.assertEqual(reverse[0]['opponent_heroes'][0]['id'],100814)
        self.assertEqual(reverse[0]['opponent_name'],'甲')
    def test_system_guard_uses_landowners_name_but_never_generates_their_team(self):
        value=battle();value['npc']=1;value['defend_idu']='0,0,0,0,0'
        value['world_npc_army']='1951056,509,0,0';value['defend_name']='本方'
        value['defend_unionid']=801
        self.ingest(value)
        self.assertEqual(self.archive.db.execute('SELECT COUNT(*) FROM attendance_facts').fetchone()[0],0)
        self.assertFalse(self.archive.db.execute("SELECT 1 FROM team_versions WHERE player_key='battle:role-b'").fetchone())
        self.assertTrue(self.archive.db.execute("SELECT 1 FROM team_versions WHERE player_key='battle:role-a'").fetchone())
        data=query(self.archive,{'from':'2026-11-12','to':'2026-11-12'})
        self.assertEqual(data['items'],[]);self.assertEqual(data['summary']['sides'],0)
        self.assertEqual(query(self.archive,{'from':'2026-11-12','to':'2026-11-12','player_key':'battle:role-b'})['items'],[])
    def test_low_level_human_army_is_not_a_system_guard(self):
        value=battle(main=False);value['defend_all_hero_info']=value['defend_all_hero_info'].replace(',40,',',20,')
        self.assertFalse(report_system_npc(value,'defend'));self.ingest(value)
        self.assertEqual(self.archive.db.execute('SELECT COUNT(*) FROM attendance_facts').fetchone()[0],2)
        self.assertTrue(self.archive.db.execute("SELECT 1 FROM team_versions WHERE player_key='battle:role-b'").fetchone())
        row=query(self.archive,{'from':'2026-11-12','to':'2026-11-12'})['items'][0]
        self.assertEqual((row['participations'],row['spartan']),(1,1))
    def test_world_guard_marker_and_explicit_npc_force_override_missing_npc_flag(self):
        for side in ('attack','defend'):
            value=battle(10 if side=='attack' else 11)
            value[side+'er_force']=1000001
            self.assertTrue(report_has_npc(value));self.ingest(value)
        value=battle(12);value.pop('defend_idu');value['world_npc_army']='123,509,0,0'
        self.assertTrue(report_system_npc(value,'defend'));self.ingest(value)
        self.assertEqual(self.archive.db.execute('SELECT COUNT(*) FROM attendance_facts').fetchone()[0],0)
        self.assertEqual(self.archive.db.execute('SELECT DISTINCT npc FROM combat_sides').fetchone()[0],1)
    def test_corrected_npc_report_retracts_old_team_and_attendance(self):
        value=battle();value.pop('defend_idu');self.ingest(value)
        self.assertTrue(self.archive.db.execute("SELECT 1 FROM team_versions WHERE player_key='battle:role-b'").fetchone())
        value['npc']=1;value['world_npc_army']='123,509,0,0';self.ingest(value)
        self.assertFalse(self.archive.db.execute("SELECT 1 FROM team_versions WHERE player_key='battle:role-b'").fetchone())
        self.assertFalse(self.archive.db.execute('SELECT 1 FROM attendance_facts').fetchone())
    def test_migration_removes_legacy_guards_and_restores_previous_human_version(self):
        human=battle();human.pop('defend_idu');self.ingest(human)
        guard=battle(2,FLOOR+200);guard.pop('defend_idu');guard['npc']=1
        with patch('archive.report_system_npc',return_value=False):self.ingest(guard)
        with self.archive.db:self.archive.db.execute("DELETE FROM lineup_rule_migrations WHERE rule='system-npc-teams-1'")
        self.archive.remove_system_npc_teams()
        self.assertFalse(self.archive.db.execute("SELECT 1 FROM team_versions WHERE player_key='battle:role-b' AND battle_id='2'").fetchone())
        latest=self.archive.db.execute("SELECT v.battle_id FROM team_current c JOIN team_versions v ON v.id=c.version_ref WHERE v.player_key='battle:role-b'").fetchone()
        self.assertEqual(latest[0],'1')
        self.assertTrue(self.archive.db.execute("SELECT 1 FROM team_versions WHERE player_key='battle:role-a' AND battle_id='2'").fetchone())
        self.archive.remove_system_npc_teams()
    def test_real_hero_instances_are_not_relabelled_as_npc_by_name_or_level(self):
        value=battle();value['npc']=1
        self.assertFalse(report_system_npc(value,'defend'))
        self.assertFalse(report_system_npc(value,'attack'))
        self.ingest(value)
        self.assertTrue(self.archive.db.execute("SELECT 1 FROM team_versions WHERE player_key='battle:role-b'").fetchone())
        self.assertFalse(self.archive.db.execute('SELECT 1 FROM attendance_facts').fetchone())
    def test_rules_are_explicit_and_scoped_to_profile_and_alliance(self):
        self.ingest(battle());save_rules(self.archive,dict(enabled=True,weights={'participations':2,'honor':.01}))
        result=query(self.archive,{'from':'2026-11-12','to':'2026-11-12'})
        self.assertEqual(result['items'][0]['score'],3)
        self.archive.profile['union_id']='802'
        other=query(self.archive,{'from':'2026-11-12','to':'2026-11-12'})
        self.assertEqual(other['items'][0]['player_name'],'乙');self.assertIsNone(other['items'][0]['score'])
        for invalid in ({'enabled':True,'weights':{'missing':1}},{'enabled':True,'weights':{'wins':float('nan')}},{'enabled':True,'weights':{}}):
            with self.assertRaises(ValueError):save_rules(self.archive,invalid)
    def test_missing_fields_not_zero_and_no_name_only_merging(self):
        value=battle();value.pop('attacker_xwc');self.ingest(value)
        with self.archive.db:
            self.archive.db.execute('INSERT INTO attendance_roster VALUES(?,?,?,?,?,?)',(self.archive.profile_id,'801','999','甲','一组',FLOOR))
        result=query(self.archive,{'from':'2026-11-12','to':'2026-11-12'})
        self.assertEqual(len(result['items']),1);self.assertIsNone(result['items'][0]['honor'])
        self.assertEqual(result['items'][0]['player_key'],'battle:role-a')
        other=battle(2);other['attack_role_id']='different-role';self.ingest(other)
        result=query(self.archive,{'from':'2026-11-12','to':'2026-11-12'})
        self.assertEqual(len(result['items']),3)
        self.assertIsNone(result['items'][-1].get('participations'))
    def test_correction_replaces_fact_and_retracts_old_alliance(self):
        self.ingest(battle());v=battle();v['attack_unionid']=802;self.ingest(v)
        self.assertEqual(query(self.archive,{'from':'2026-11-12','to':'2026-11-12'})['items'],[])
    def test_csv_prevents_formulas_and_unknown_values_stay_blank(self):
        raw=export_csv({'items':[{'player_name':'=evil','score':None}]})
        self.assertTrue(raw.startswith(b'\xef\xbb\xbf'));self.assertIn("'=evil",raw.decode('utf8'))
    def test_wuxun_uses_official_client_encoding_not_gongxun(self):
        self.assertEqual(wuxun(317325),2473)
        self.assertEqual(wuxun(658491),5818)
        self.assertEqual(wuxun(0),0)
        self.assertIsNone(wuxun(None))
    def test_date_validation_and_profile_time_index(self):
        with self.assertRaises(ValueError):query(self.archive,{'from':'bad'})
        plan=' '.join(str(tuple(r)) for r in self.archive.db.execute('EXPLAIN QUERY PLAN SELECT * FROM attendance_facts WHERE profile_id=? AND union_id=? AND event_at>=?',(self.archive.profile_id,'801',FLOOR)))
        self.assertIn('attendance_period',plan)
    def test_prune_keeps_exact_boundary_removes_derived_and_old_raw(self):
        self.ingest(battle(1,FLOOR-1));self.ingest(battle(2,FLOOR));
        self.archive.ingest(11,[{'battle_id':1,'report':'old'},{'battle_id':2,'report':'new'}],FLOOR+20000)
        self.archive.lineup_projection.close()
        with self.archive.db:result=prune(self.archive.db,self.archive.profile_id,FLOOR)
        self.assertEqual(result['after']['battle_reports'],1)
        for table in ('battle_reports','report_bodies','team_versions','combat_sides','attendance_facts'):
            self.assertFalse(self.archive.db.execute(f'SELECT 1 FROM {table} WHERE battle_id=?',('1',)).fetchone(),table)
        stats=self.archive.ingest(92,[[battle(1,FLOOR-1),battle(3,FLOOR+10)]],FLOOR+30000)
        self.assertEqual(stats['excluded_battle_ids'],['1']);self.assertEqual(stats['battle_ids'],['3'])
        self.archive.ingest(11,[{'battle_id':1,'report':'old again'}],FLOOR+30001)
        self.assertFalse(self.archive.db.execute('SELECT 1 FROM report_bodies WHERE battle_id=1').fetchone())
        for r in self.archive.db.execute('SELECT payload FROM messages'):
            self.assertNotIn('old',json.dumps(json.loads(zlib.decompress(r[0]))))
    def test_retention_does_not_collapse_map_positions(self):
        with self.archive.db:self.archive.db.execute('INSERT INTO retention_policy VALUES(?,?,?)',(self.archive.profile_id,FLOOR,time.time()))
        scene=[None]*34;scene[1]={};scene[6]={};scene[17]=[1,2,3,4]
        self.assertEqual(sanitize(self.archive,scene),scene)
    def test_policy_floor_stops_history_but_keeps_fresh_lane(self):
        with self.archive.db:self.archive.db.execute('INSERT INTO retention_policy VALUES(?,?,?)',(self.archive.profile_id,FLOOR,time.time()))
        calls=[];h=BattleHarvester(self.root,self.archive,SimpleNamespace(fetch_union_page=lambda c:calls.append(c)),lambda:None,False)
        try:
            h.not_before=0;h.continuous=True;h.start({'max_pages':20});h._step();pending=h.pending
            at=pending['sent_at']+.01;h.on_request(pending['body'],at)
            values=[[battle(2,FLOOR+1),battle(1,FLOOR-1)]]
            stats=self.archive.ingest(92,values,at);h.on_page(values,at,stats);h._step()
            self.assertTrue(h.data['available_end']);self.assertTrue(h.data['retention_end']);self.assertEqual(len(calls),1)
            h.not_before=0;h.latest_due=0;h._step();self.assertEqual(calls[-1],0)
        finally:h.close()
    def test_fresh_defaults_and_opposing_buyers_use_their_own_alliance(self):
        self.assertFalse(default_config()['self']['confirmed']);self.assertEqual(default_config()['self']['union_id'],'0')
        for own,enemy,uid in (('801','802','1'),('802','801','2')):
            with tempfile.TemporaryDirectory() as folder:
                state=WorldState(Path(folder));state.config['self'].update(user_id=uid,union_id=own,confirmed=True)
                state.config['relations'][enemy]={'relation':'enemy','manual_override':True,'own_user_id':uid}
                self.assertEqual(state._relation('100',own),'ally');self.assertEqual(state._relation('101',enemy),'enemy')
                state.config['relations'][enemy]['own_user_id']='other-role'
                self.assertEqual(state._relation('101',enemy),'unknown')
