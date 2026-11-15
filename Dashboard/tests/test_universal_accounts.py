"""Fresh installs and perspective transitions; no real account fixtures."""
import json
import tempfile
import unittest
import threading
import http.client
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from archive import Archive
from installed_mode import load_installation,LOGIN_SNAPSHOT
from model import WorldState,write_json
from portable_mode import prepare_installation,validated_identity,PortableCollector
from role_context import context,perspective_id
from harvest import BattleHarvester
from test_distribution import identity
from test_installed_mode import login_snapshot
from android_report_bridge import AndroidReadOnlyBridge


class UniversalAccounts(unittest.TestCase):
    def prepare(self,root,value,settings=None):
        with patch('portable_mode.discover_game',return_value=('dashen','G:/不同目录/GLEmulator.exe',{})),patch('portable_mode.inspect_identity',return_value=value):
            return prepare_installation(root,settings or {},timeout=0)

    def test_fresh_role_matrix(self):
        for force,union,title,label in [(0,71,'同盟考勤','本盟'),(0,0,'个人战绩','自己'),
                (1,83,'流浪军战绩','同聚义'),(1,0,'流浪军战绩','自己')]:
            with self.subTest(force=force,union=union),tempfile.TemporaryDirectory()as folder:
                root=Path(folder);value={**identity(uid=str(500+force+union),server=81234,season=9),
                    'force_type':force,'union_id':union,'union_name':None if not union else '任意组织'}
                profile,settings=self.prepare(root,value,{'account_confirmation':'旧账号','server':'旧服','season':'旧季'})
                role=context(profile);config=json.loads((root/'config.json').read_text(encoding='utf8'))
                self.assertEqual((role['statistics_title'],role['own_label']),(title,label))
                self.assertEqual(config['self']['is_wanderer'],force==1)
                self.assertEqual(profile['server'],'server-81234');self.assertEqual(profile['season'],'season-9')
                self.assertTrue(settings['auto_identity']);self.assertNotIn('account_confirmation',settings)
                self.assertFalse(config['relations']);self.assertFalse(config['targets'])

    def test_account_server_and_season_all_isolate_profiles(self):
        with tempfile.TemporaryDirectory()as folder:
            root=Path(folder);seen=set()
            for uid,server,season in [('501',81234,9),('502',81234,9),('501',81235,9),('501',81234,10)]:
                _,settings=self.prepare(root,identity(uid=uid,server=server,season=season))
                seen.add(settings['profile_id'])
            self.assertEqual(len(seen),4)

    def test_same_account_organization_and_force_preferences_roundtrip(self):
        with tempfile.TemporaryDirectory()as folder:
            root=Path(folder);base={**identity(uid='501'),'union_id':'71'}
            profile,settings=self.prepare(root,base)
            config=json.loads((root/'config.json').read_text(encoding='utf8'))
            config['relations']={'99':{'manual_override':True,'own_user_id':'501','relation':'friendly'}}
            config['state_rules']['3:14:123']={'kind':'other','verified':True}
            write_json(root/'config.json',config)
            for changed in ({'union_id':'72'},{'union_id':'71','force_type':1}):
                new,new_settings=self.prepare(root,{**base,**changed})
                self.assertEqual(settings['profile_id'],new_settings['profile_id'],'Battle history stays under the same role')
                self.assertNotEqual(perspective_id(profile),perspective_id(new))
                self.assertFalse(json.loads((root/'config.json').read_text(encoding='utf8'))['relations'])
                self.assertNotIn('3:14:123',json.loads((root/'config.json').read_text(encoding='utf8'))['state_rules'])
                self.prepare(root,base)
                self.assertIn('99',json.loads((root/'config.json').read_text(encoding='utf8'))['relations'])
                self.assertIn('3:14:123',json.loads((root/'config.json').read_text(encoding='utf8'))['state_rules'])

    def test_incomplete_identity_waits_instead_of_guessing(self):
        for key in ('force_type','battle_role_id','season_id','server_id'):
            value=identity();value.pop(key)
            with self.subTest(missing=key),self.assertRaises(ValueError):validated_identity(value)

    def test_offline_install_starts_then_discovers_login_without_manual_settings(self):
        with tempfile.TemporaryDirectory()as folder:
            root=Path(folder);write_json(root/'install-settings.json',{'distribution_v2':True,'executable':'auto','dumpcap':'G:/dumpcap.exe'})
            with patch('portable_mode.discover_game',side_effect=RuntimeError('等候登录')):
                profile,settings=load_installation(root)
            self.assertEqual(profile['account_role_id'],'0')
            state=WorldState(root);archive=Archive(root,profile=profile)
            try:
                collector=PortableCollector(state,archive,settings);changes=[];collector.identity_change_callback=changes.append
                with patch('portable_mode.discover_game',return_value=('dashen','G:/Game/GLEmulator.exe',{'source':'dashen'})):
                    self.assertEqual(collector._discover(),{'source':'dashen'})
                with patch('portable_mode.inspect_identity',return_value=identity(uid='501')):
                    collector._prepare_connection({'source':'dashen'})
                self.assertEqual(changes[0]['account_role_id'],'501')
                self.assertFalse(collector.archive_ready)
                self.assertTrue(state.health['scope_pending'])
            finally:archive.close()

    def test_force_or_union_transition_freezes_old_workers_before_restart(self):
        for change in ({'union_id':'72'},{'force_type':1}):
            with self.subTest(change=change),tempfile.TemporaryDirectory()as folder:
                root=Path(folder);raw={**identity(uid='501'),'union_id':'71'};profile,settings=self.prepare(root,raw)
                archive=Archive(root,profile=profile);state=WorldState(root)
                try:
                    collector=PortableCollector(state,archive,settings);changes=[];collector.identity_change_callback=changes.append
                    self.assertFalse(collector._confirm_identity(validated_identity({**raw,**change}),'test',1000))
                    self.assertEqual(len(changes),1);self.assertFalse(collector.archive_ready)
                    self.assertEqual(archive.profile['union_id'],'71');self.assertEqual(archive.profile['force_type'],0)
                finally:archive.close()

    def test_partial_login_preserves_verified_force_and_battle_identity(self):
        with tempfile.TemporaryDirectory()as folder:
            root=Path(folder);raw={**identity(),'force_type':1};profile,settings=self.prepare(root,raw)
            archive=Archive(root,profile=profile);state=WorldState(root)
            try:
                collector=PortableCollector(state,archive,settings)
                collector._confirm_identity(validated_identity(raw),'test',1000)
                message=login_snapshot();message[5]=45122;message[4][0].append(['Tb_season_user',[[200,4]]])
                collector._message(LOGIN_SNAPSHOT,message,1001)
                self.assertTrue(state.config['self']['is_wanderer'])
                self.assertEqual(state.config['self']['battle_role_id'],raw['battle_role_id'])
            finally:archive.close()

    def test_history_checkpoint_is_per_perspective_and_can_resume_after_switchback(self):
        with tempfile.TemporaryDirectory()as folder:
            root=Path(folder);profile,settings=self.prepare(root,identity(uid='501'))
            archive=Archive(root,profile=profile);bridge=SimpleNamespace()
            first=second=third=None
            try:
                first=BattleHarvester(root,archive,bridge,lambda:None,False)
                first.data.update(cursor=123,status='paused');first._save();first.close()
                archive.profile={**profile,'union_id':'72'}
                second=BattleHarvester(root,archive,bridge,lambda:None,False)
                self.assertEqual(second.data['cursor'],0);self.assertNotEqual(first.path,second.path)
                second.close();archive.profile=profile
                third=BattleHarvester(root,archive,bridge,lambda:None,False)
                self.assertEqual(third.data['cursor'],123)
            finally:
                for job in (first,second,third):
                    if job:job.close()
                archive.close()

    def test_every_read_rechecks_role_organization_force_and_season(self):
        with tempfile.TemporaryDirectory()as folder:
            root=Path(folder);raw=identity(uid='501');profile,settings=self.prepare(root,raw)
            archive=SimpleNamespace(profile=profile,profile_id=settings['profile_id']);observed=[]
            bridge=AndroidReadOnlyBridge(root,archive,lambda:None,identity_observer=lambda *args:observed.append(args))
            try:
                for changes in ({'union_id':'72'},{'force_type':1},{'season_id':10},{'server_id':81235},
                        {'user_id':'502','account_role_id':'502'}):
                    bridge.script=SimpleNamespace(exports_sync=SimpleNamespace(identify=lambda:{**raw,**changes}))
                    with self.subTest(changes=changes),self.assertRaisesRegex(RuntimeError,'暂停旧范围'):bridge._verify_identity()
                self.assertEqual(len(observed),5)
                bridge.script=SimpleNamespace(exports_sync=SimpleNamespace(identify=lambda:raw))
                bridge._verify_identity();self.assertIn('identity_verified_at',bridge.stats)
            finally:bridge.script=None;bridge.close()

    def test_pending_identity_blocks_archive_queries_and_recovers_automatically(self):
        from server import make_handler
        with tempfile.TemporaryDirectory()as folder:
            root=Path(folder);state=WorldState(root);state.health['identity_pending']=True
            archive=Archive(root);httpd=ThreadingHTTPServer(('127.0.0.1',0),make_handler(state,0,archive=archive))
            port=httpd.server_address[1];httpd.RequestHandlerClass=make_handler(state,port,archive=archive)
            thread=threading.Thread(target=httpd.serve_forever,daemon=True);thread.start()
            try:
                connection=http.client.HTTPConnection('127.0.0.1',port,timeout=3)
                connection.request('GET','/api/intel/formations');reply=connection.getresponse()
                self.assertEqual(reply.status,409);self.assertTrue(json.loads(reply.read())['identity_pending']);connection.close()
                state.health['identity_pending']=False
                connection=http.client.HTTPConnection('127.0.0.1',port,timeout=3)
                connection.request('GET','/api/role-context');reply=connection.getresponse()
                self.assertEqual(reply.status,200);self.assertTrue(json.loads(reply.read())['ready']);connection.close()
            finally:httpd.shutdown();httpd.server_close();thread.join(3);archive.close()


if __name__=='__main__':unittest.main()
