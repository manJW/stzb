import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from archive import Archive
from model import WorldState,write_json
from portable_mode import PortableCollector,validated_identity,prepare_installation
from role_context import context
from test_distribution import identity
from test_attendance_retention import battle,PROFILE,FLOOR
from test_wanderers import wanderer_scene
import attendance
import team_catalog
import territory_events
import time


class WandererOwner(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        profile={**PROFILE,'union_id':'0','union_name':'无所属势力','force_type':1,'is_wanderer':True,'battle_role_id':'role-a'}
        self.a=Archive(self.root,profile=profile);self.a.attendance.close()
    def tearDown(self):self.a.close();self.tmp.cleanup()
    def add(self,value):self.a.ingest(92,[[value]],FLOOR+5000);self.a.attendance.drain()

    def test_unorganized_statistics_and_catalog_only_include_verified_self(self):
        own=battle();own.update(attack_unionid=0,attacker_force=1);self.add(own)
        other=battle(2,FLOOR+200);other.update(attack_unionid=0,attacker_force=1,attack_role_id='other',attack_name='其他流浪军');self.add(other)
        q={'from':'2026-11-12','to':'2026-11-12','include_pve':'1'}
        data=attendance.query(self.a,q)
        self.assertEqual([r['player_key']for r in data['items']],['battle:role-a'])
        self.assertEqual(data['summary']['pvp'],1)
        self.assertEqual(data['role_context']['statistics_title'],'流浪军战绩')
        self.assertEqual(attendance.query(self.a,{**q,'player_key':'battle:other'})['items'],[])
        self.assertEqual([r['player_key']for r in team_catalog.query(self.a,{'view':'players','relation':'ally'})['items']],['battle:role-a'])
        self.assertEqual([r['player_key']for r in self.a.intelligence.query('encounters',relation='ally')['items']],['battle:role-a'])
        filters=self.a.intelligence.filters({'relation':'all'});filters['main_scope']='both'
        samples,_=self.a.intelligence.main_samples(filters)
        self.assertEqual([r['player_key']for r in samples],['battle:role-a'])

    def test_unknown_normal_attacker_to_wanderer_own_land_is_alerted(self):
        state=WorldState(self.root);state.config['self'].update(user_id='100',union_id='0',confirmed=True,is_wanderer=True,force_type=1)
        state.health['capture_source']='dashen';state.connection('live','test');state.packet(1000)
        value=wanderer_scene(force=0,union=1008);value[14]['10150926']['0'][2]=100;value[14]['10150926']['0'][3]=0
        state.apply(5026,value,1000);data=state.snapshot(1001)
        self.assertEqual(data['armies'][0]['relation'],'unknown');self.assertEqual(len(data['alerts']),1)
        state.config['relations']['1008']={'relation':'friendly'}
        self.assertEqual(state.snapshot(1001)['alerts'],[])
        state.config['relations']={};value=wanderer_scene(force=1,union=0,order=11);value[14]['10150926']['0'][2]=200;value[14]['10150926']['0'][3]=0
        state.apply(5026,value,1002);data=state.snapshot(1003)
        self.assertNotEqual(data['armies'][0]['relation'],'ally');self.assertEqual(data['alerts'],[])

    def test_identity_contains_explicit_force_and_battle_role(self):
        value=validated_identity({**identity(),'force_type':1,'battle_role_id':'role_a_123'})
        self.assertTrue(value['is_wanderer']);self.assertEqual(value['battle_role_id'],'role_a_123')
        self.assertFalse(context({'union_id':'0','account_name':'流浪军玩家'})['is_wanderer'])

    def test_role_change_blocks_old_scope_before_requesting_restart(self):
        write_json(self.root/'capture-source.json',{'mode':'pc'})
        state=WorldState(self.root);collector=PortableCollector(state,self.a,{'mode':'pc'})
        changes=[];collector.identity_change_callback=changes.append
        new=validated_identity(identity(uid='201'))
        self.assertFalse(collector._confirm_identity(new,'test',1000))
        self.assertEqual(len(changes),1);self.assertEqual(self.a.profile['account_role_id'],PROFILE['account_role_id'])
        self.assertFalse(collector.archive_ready);self.assertTrue(state.health['scope_pending'])

    def test_role_settings_roundtrip_and_capture_binding_match(self):
        with patch('portable_mode.discover_game',return_value=('dashen','D:/Game/GLEmulator.exe',{})),patch('portable_mode.inspect_identity',return_value=identity()):
            profile,settings=prepare_installation(self.root,{'dumpcap':'D:/dumpcap.exe'},timeout=0)
        config=json.loads((self.root/'config.json').read_text(encoding='utf8'));config['relations']={'10':{'manual_override':True,'relation':'friendly','own_user_id':'200'}}
        write_json(self.root/'config.json',config)
        with patch('portable_mode.discover_game',return_value=('dashen','D:/Game/GLEmulator.exe',{})),patch('portable_mode.inspect_identity',return_value=identity(uid='201')):
            _,new=prepare_installation(self.root,settings,timeout=0)
        self.assertFalse(json.loads((self.root/'config.json').read_text(encoding='utf8'))['relations'])
        self.assertNotEqual(new['profile_id'],settings['profile_id'])
        with patch('portable_mode.discover_game',return_value=('dashen','D:/Game/GLEmulator.exe',{})),patch('portable_mode.inspect_identity',return_value=identity()):
            _,restored=prepare_installation(self.root,new,timeout=0)
        self.assertEqual(restored['profile_id'],settings['profile_id'])
        self.assertIn('10',json.loads((self.root/'config.json').read_text(encoding='utf8'))['relations'])

    def member(self,uid=1,name='甲'):
        row=[0]*34;row[0]=uid;row[1]=name;row[13]='';row[15]=224627;row[30]=int(FLOOR)
        return row

    def test_roster_metrics_and_no_battle_members_are_visible_without_creating_teams(self):
        self.a.profile.update(union_id='801')
        self.add(battle())
        attendance.roster(self.a,[self.member(),self.member(2,'只有名册')],'801',FLOOR+6000)
        data=attendance.query(self.a,{'from':'2026-11-12','to':'2026-11-12'})
        by_name={r['player_name']:r for r in data['items']}
        for name in ('甲','只有名册'):
            self.assertEqual(by_name[name]['cumulative_honor'],224627)
            self.assertEqual(by_name[name]['joined_at'],FLOOR)
            self.assertIsNone(by_name[name]['new_honor'])
        self.assertEqual(self.a.db.execute('SELECT COUNT(*) FROM formation_teams').fetchone()[0],2)
        self.assertEqual(context(self.a.profile)['own_label'],'同聚义')

    def test_name_collision_and_other_group_never_attach_member_metrics(self):
        self.a.profile.update(union_id='801')
        self.add(battle())
        other=battle(2);other['attack_role_id']='role-c';self.add(other)
        attendance.roster(self.a,[self.member()],'801',FLOOR+6000)
        data=attendance.query(self.a,{'from':'2026-11-12','to':'2026-11-12'})
        for row in data['items']:
            if row['player_key'].startswith('battle:'):self.assertIsNone(row['cumulative_honor'])
        territory_events.observe_roster(self.a,[self.member(3,'他盟')],'802',FLOOR+6001)
        self.assertFalse(self.a.db.execute("SELECT 1 FROM member_metric_snapshots WHERE player_key='world:3'").fetchone())

    def test_join_update_does_not_refresh_old_honor_observation(self):
        self.a.profile.update(account_role_id='1',force_type=0,is_wanderer=False)
        raw=[0]*102;raw[0]=1;raw[101]=100
        territory_events.observe_tables(self.a,[[1,'Tb_user_res',raw]],FLOOR,'honor')
        territory_events.observe_tables(self.a,[[1,'Tb_user_union_attr',[1,int(FLOOR)]]],FLOOR+1000,'join')
        latest=self.a.db.execute('SELECT honor FROM member_metric_snapshots ORDER BY observed_at DESC LIMIT 1').fetchone()
        self.assertIsNone(latest['honor'])
        with patch('attendance.time.time',return_value=FLOOR+1000):
            result=attendance.member_metrics(self.a,['battle:role-a'],FLOOR,FLOOR+1001)
        self.assertEqual(result['cumulative_honor'],100);self.assertIsNone(result['new_honor'])

    def test_regular_total_never_overrides_verified_ranger_counter(self):
        self.a.profile.update(union_id='801',account_role_id='1')
        attendance.roster(self.a,[self.member()],'801',FLOOR+1000)
        raw=[0]*102;raw[0]=1;raw[101]=999999
        territory_events.observe_tables(self.a,[[1,'Tb_user_res',raw]],FLOOR+1001,'regular-total')
        result=attendance.member_metrics(self.a,['world:1'],FLOOR,FLOOR+2000)
        self.assertEqual(result['cumulative_honor'],224627)

    def test_authoritative_own_roster_removes_departed_members(self):
        self.a.profile.update(union_id='801',account_role_id='1')
        attendance.roster(self.a,[self.member(),self.member(2,'离开的成员')],'801',FLOOR+1000)
        self.assertEqual(self.a.db.execute('SELECT COUNT(*) FROM attendance_roster').fetchone()[0],2)
        attendance.roster(self.a,[self.member()],'801',FLOOR+1001)
        self.assertEqual(self.a.db.execute('SELECT COUNT(*) FROM attendance_roster').fetchone()[0],1)

    def test_workspace_history_floor_applies_to_new_roles(self):
        with tempfile.TemporaryDirectory()as folder:
            root=Path(folder);write_json(root/'archive-policy.json',{'retain_from':FLOOR})
            archive=Archive(root,profile=PROFILE)
            try:self.assertEqual(archive.retention_from,FLOOR)
            finally:archive.close()


if __name__=='__main__':unittest.main()
