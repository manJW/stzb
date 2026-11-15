import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from archive import Archive
from model import WorldState,write_json
from portable_mode import validated_identity,prepare_installation,PortableCollector
from installed_mode import LOGIN_SNAPSHOT
from test_installed_mode import login_snapshot
from game_locator import WindowsOcr


def identity(uid='200',name='测试角色',server=45122,season=4):
    return dict(user_id=uid,account_role_id=uid,account_name=name,union_id='401',union_name='测试盟',
                server_id=server,season_id=season,game_requests_sent=0,force_type=0,battle_role_id='role-'+uid)


class DistributionTests(unittest.TestCase):
    def test_installed_ocr_uses_workspace_without_program_runtime_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);program=root/'program';program.mkdir()
            runtime=root/'workspace'/'runtime'
            with patch('game_locator.ROOT',program),patch.dict('os.environ',{'STZB_RUNTIME_ROOT':str(runtime)}),patch('game_locator.subprocess.Popen'):
                with WindowsOcr() as ocr:
                    temporary=Path(ocr.directory.name)
                    self.assertEqual(temporary.parent,runtime)
                    self.assertTrue(temporary.is_dir())
                    self.assertFalse((program/'runtime').exists())
                self.assertFalse(temporary.exists())

    def test_identity_rejects_partial_or_mismatched_scope(self):
        self.assertEqual(validated_identity(identity())['scope']['season'],'season-4')
        for changes in ({'server_id':0},{'season_id':None},{'account_role_id':'300'},
                        {'user_id':'0','account_role_id':'0'},{'account_name':''},{'game_requests_sent':1}):
            with self.subTest(changes=changes),self.assertRaises(ValueError):validated_identity({**identity(),**changes})

    def test_new_machine_needs_no_saved_role_or_paths(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);assets=root/'assets';assets.mkdir()
            write_json(assets/'capture-defaults.json',{'state_rules':{'1:0:*':{'kind':'attack','verified':True}}})
            with patch('portable_mode.ROOT',assets),patch('portable_mode.discover_game',return_value=('dashen','D:/New/Game/GLEmulator.exe',{})),patch('portable_mode.inspect_identity',return_value=identity()):
                profile,settings=prepare_installation(root,{'dumpcap':'D:/Capture/dumpcap.exe'})
            self.assertEqual(profile['account_role_id'],'200')
            self.assertEqual(profile['server'],'server-45122')
            self.assertEqual(settings['emulator_executable'],'D:/New/Game/GLEmulator.exe')
            self.assertEqual(json.loads((root/'config.json').read_text())['self']['name'],'测试角色')

    def test_changing_role_does_not_reuse_old_relations_or_identity(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);write_json(root/'archive-profile.json',{'server':'server-1','season':'season-1','account_role_id':'10'})
            write_json(root/'config.json',{'self':{'name':'旧角色'},'relations':{'1008':{'relation':'enemy'}},'targets':[{'name':'旧坐标'}],'state_rules':{'1:0:*':{'kind':'attack','verified':True}}})
            with patch('portable_mode.discover_game',return_value=('dashen','D:/Game/GLEmulator.exe',{})),patch('portable_mode.inspect_identity',return_value=identity()):prepare_installation(root,{'dumpcap':'D:/dumpcap.exe'})
            config=json.loads((root/'config.json').read_text())
            self.assertEqual(config['self']['user_id'],'200');self.assertFalse(config['relations']);self.assertFalse(config['targets'])

    def test_same_collector_keeps_latest_marches_and_cannot_bind_city_id_as_role(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);profile={'server':'server-45122','season':'season-4','account_role_id':'200','account_name':'测试角色','union_id':'401','union_name':'测试盟'}
            write_json(root/'config.json',{'self':{'user_id':'200','name':'测试角色','union_id':'401','union_name':'测试盟','confirmed':True},'state_rules':{},'relations':{},'targets':[]})
            write_json(root/'capture-source.json',{'mode':'pc','dumpcap':'D:/dumpcap.exe'})
            state=WorldState(root);archive=Archive(root,profile=profile)
            try:
                collector=PortableCollector(state,archive,{'mode':'pc','dumpcap':'D:/dumpcap.exe'})
                with patch('portable_mode.inspect_identity',return_value=identity()):collector._prepare_connection({'source':'pc'})
                self.assertTrue(collector.archive_ready)
                message=login_snapshot(role=9320906);message[5]=45122;message[4][0].append(['Tb_season_user',[[200,4]]])
                collector._message(LOGIN_SNAPSHOT,message,1000)
                self.assertFalse(state.health['identity_pending'])
                self.assertEqual(archive.profile['account_role_id'],'200')
                self.assertEqual(collector.incoming_ids,None)
                self.assertEqual(archive.db.execute('SELECT COUNT(*) FROM messages WHERE message_id=99991').fetchone()[0],1)
            finally:archive.close()

if __name__=='__main__':unittest.main()
