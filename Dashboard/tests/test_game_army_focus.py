import json
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from game_army_focus import GameArmyFocus
from test_support import WorldState


ROOT = Path(__file__).resolve().parents[1]


class GameArmyFocusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT/'runtime')
        self.state = WorldState(self.temp.name)
        self.connection = {'source':'dashen','pid':7,'host_pid':8,'local_port':123,'connection_started':1}
        self.state.connection('live','test',self.connection)
        self.at = time.time()
        self.state.packet(self.at)
        self.state.health['resync_required'] = False
        self.state.armies['113709493'] = {'visible':True,'epoch':self.state.epoch}
        self.task = GameArmyFocus(self.state,SimpleNamespace(read_map_selection=lambda:None),lambda:self.connection)
        self.value = {'reader_id':'a'*32,'sequence':1,'selected_at':self.at,'status':'selected',
                      'army_id':'113709493','game_requests_sent':0}

    def tearDown(self):
        self.temp.cleanup()

    def test_exact_selected_army_is_published_once_without_touching_tile_focus(self):
        self.state.game_focus = {'coord':[986,923]}
        self.assertTrue(self.task.apply(self.value,self.connection,self.at+.1))
        focus = self.state.game_army_focus
        self.assertEqual(focus['army_id'],'113709493')
        self.assertEqual(focus['source'],'client_selected_army')
        self.assertFalse(self.task.apply(self.value,self.connection,self.at+.2))
        self.assertEqual(self.state.game_army_focus['event_id'],focus['event_id'])
        self.assertEqual(self.state.game_army_focus['validated_at'],self.at+.2)
        self.assertEqual(self.state.game_focus,{'coord':[986,923]})

    def test_unseen_army_waits_for_its_own_current_map_update(self):
        self.state.armies.clear()
        self.assertFalse(self.task.apply(self.value,self.connection,self.at+.1))
        self.assertIsNone(self.state.game_army_focus)
        self.state.armies['113709493'] = {'visible':True,'epoch':self.state.epoch}
        self.assertTrue(self.task.apply(self.value,self.connection,self.at+.2))

    def test_cancellation_and_group_selection_do_not_masquerade_as_an_army(self):
        for status in ('cancelled','unsupported_selection'):
            self.assertTrue(self.task.apply({**self.value,'status':status,'army_id':None},self.connection,self.at+.1))
            self.assertIsNone(self.state.game_army_focus['army_id'])
            self.task.published = None

    def test_stale_hidden_other_session_role_or_non_readonly_events_are_rejected(self):
        for value in ({**self.value,'selected_at':-10},{**self.value,'selected_at':self.at+2},
                      {**self.value,'army_id':'bad'},{**self.value,'game_requests_sent':1},
                      {**self.value,'sequence':True},{**self.value,'status':'selected_group'}):
            self.assertFalse(self.task.apply(value,self.connection,self.at+.1))
        for change in ({'visible':False},{'epoch':self.state.epoch-1}):
            self.state.armies['113709493'] = {'visible':True,'epoch':self.state.epoch,**change}
            self.assertFalse(self.task.apply(self.value,self.connection,self.at+.1))
        self.state.armies['113709493'] = {'visible':True,'epoch':self.state.epoch}
        self.assertFalse(self.task.apply(self.value,{**self.connection,'pid':99},self.at+.1))
        self.state.config['self']['confirmed'] = False
        self.assertFalse(self.task.apply(self.value,self.connection,self.at+.1))

    def test_a_still_selected_army_is_validated_even_after_the_original_click_is_old(self):
        value={**self.value,'selected_at':self.at-60}
        self.assertTrue(self.task.apply(value,self.connection,self.at))
        self.assertFalse(self.task.apply(value,self.connection,self.at+2))
        self.assertEqual(self.state.game_army_focus['validated_at'],self.at+2)

    def test_fast_game_highlight_cancel_does_not_lose_the_clicked_personal_army(self):
        value={**self.value,'army_id':None,'sequence':2,'status':'cancelled',
               'last_army_id':'113709493','last_sequence':1,'last_selected_at':self.at}
        self.assertTrue(self.task.apply(value,self.connection,self.at+2))
        self.assertEqual(self.state.game_army_focus['army_id'],'113709493')
        self.assertFalse(self.task.apply(value,self.connection,self.at+3))

    def test_record_is_cleared_when_game_connection_changes(self):
        self.task.apply(self.value,self.connection,self.at+.1)
        self.state.connection('waiting','test')
        self.assertIsNone(self.state.game_army_focus)

    def test_group_selection_uses_its_own_namespace_and_waits_for_visible_members(self):
        value={**self.value,'army_id':None,'group_id':'113310331','status':'selected_group'}
        self.assertFalse(self.task.apply(value,self.connection,self.at+.1))
        self.state.armies['113709493']['army_group_id']=113310331
        self.assertTrue(self.task.apply(value,self.connection,self.at+.2))
        self.assertIsNone(self.state.game_army_focus['army_id'])
        self.assertEqual(self.state.game_army_focus['group_id'],'113310331')
        self.assertFalse(self.task.apply(value,self.connection,self.at+.3))
        cancelled={**value,'sequence':2,'status':'cancelled','group_id':None,
                   'last_sequence':1,'last_group_id':'113310331','last_selected_at':self.at}
        self.assertFalse(self.task.apply(cancelled,self.connection,self.at+.4))
        self.assertEqual(self.state.game_army_focus['group_id'],'113310331')

    def test_component_manifest_is_hash_verified_and_has_no_game_commands(self):
        import hashlib
        assets = ROOT/'android_reader_assets'
        manifest = json.loads((assets/'map-selection-manifest.json').read_text(encoding='utf-8-sig'))
        self.assertEqual(hashlib.sha256((assets/manifest['file']).read_bytes()).hexdigest(),manifest['sha256'])
        source = (assets/'MapSelectionReader.cs').read_text(encoding='utf8')
        for forbidden in ('SendRawObject','Send(', 'SetPersonalArmySelected(', 'GetInstance('):
            self.assertNotIn(forbidden,source)


class ManagedSelectionTests(unittest.TestCase):
    @unittest.skipUnless(Path('C:/Windows/Microsoft.NET/Framework64/v4.0.30319/csc.exe').is_file(),'Requires Windows compiler')
    def test_actual_managed_listener_handles_selection_cancel_scope_change_and_cleanup(self):
        csc='C:/Windows/Microsoft.NET/Framework64/v4.0.30319/csc.exe'
        with tempfile.TemporaryDirectory(dir=ROOT/'runtime') as folder:
            folder=Path(folder)
            mock=folder/'Game.Mock.cs'
            mock.write_text('''using System;
namespace Tenth.Network { public class Cmd { public int _userId=12319,_serverId=6147,_networkState=1,sends=0; }
 public class NetManager { public static NetManager sInstance=new NetManager(); public Cmd mCmdMgr=new Cmd(); } }
namespace Tenth.Map { public enum ArmySelectedType { PersonalArmy,GroupArmy }
 public class MapInterface { public static bool Is3dMapSelected=false; }
 public class FacadeBase { private static MapCommonFacade sInstance=new MapCommonFacade(); }
 public class MapCommonFacade:FacadeBase { private int mColorType=0;
 public static void ChangeMode(int mode){typeof(FacadeBase).GetField("sInstance",System.Reflection.BindingFlags.Static|System.Reflection.BindingFlags.NonPublic).GetValue(null).GetType().GetField("mColorType",System.Reflection.BindingFlags.Instance|System.Reflection.BindingFlags.NonPublic).SetValue(typeof(FacadeBase).GetField("sInstance",System.Reflection.BindingFlags.Static|System.Reflection.BindingFlags.NonPublic).GetValue(null),mode);} }
 public class MapEventManager { public static MapEventManager sInstance=new MapEventManager();
 public event Action<int,object> EventMapArmySelected; public event Action<int,object> EventMapArmyCancelSelected;
 public void Select(int id,object type) { if(EventMapArmySelected!=null)EventMapArmySelected(id,type); }
 public void Cancel(int id) { if(EventMapArmyCancelSelected!=null)EventMapArmyCancelSelected(id,ArmySelectedType.PersonalArmy); }
 public int Count {get {return EventMapArmySelected==null?0:EventMapArmySelected.GetInvocationList().Length;}} } }
namespace Tenth.Map.Map2d { public class Base { protected int? selectedId=113709493; }
 public class MapArmyAnimationMgr:Base { public static MapArmyAnimationMgr _sInstance=new MapArmyAnimationMgr();
 protected Tenth.Map.ArmySelectedType? selectedType=Tenth.Map.ArmySelectedType.PersonalArmy; } }
namespace Tenth.Data {
 public struct MapColor { public byte R,G,B; public MapColor(byte r,byte g,byte b){R=r;G=g;B=b;} }
 public static class MapResDefine {
 public static System.Collections.Generic.Dictionary<string,string[]> sRelationDef=new System.Collections.Generic.Dictionary<string,string[]>();
 static System.Collections.Generic.Dictionary<string,MapColor> colors=new System.Collections.Generic.Dictionary<string,MapColor>();
 public static System.Collections.Generic.Dictionary<string,MapColor> sColorGridDef {get{return colors;}}
 public static System.Collections.Generic.Dictionary<string,MapColor> sColorGridDef_P {get{return colors;}}
 static MapResDefine(){foreach(string key in new string[]{"self","union","other","enemy","bandit","friend_other","friend_bandit"})sRelationDef[key]=new string[]{"a","b","c"};colors["a"]=new MapColor(48,184,229);colors["b"]=new MapColor(133,216,234);colors["c"]=new MapColor(100,220,255);} }
 public static class UserData { public static int UserId {get{return 12319;}} public static int UnionID {get{return 1009;}} }
 public static class SysParamData { public static bool IsRealMarchOpen {get{return true;}} }
 public enum UnionRelation { None,Friendly,Hostility,SignedContract }
 public class UnionRelationCache { public int union_id,target_union_id; public UnionRelation relationship; public long next_time,cron_time; }
 public class UnionRelationData { public static UnionRelationData sInstance=new UnionRelationData();
 public System.Collections.Generic.Dictionary<int,UnionRelationCache> mCacheDict=new System.Collections.Generic.Dictionary<int,UnionRelationCache>();
 public System.Collections.Generic.Dictionary<int,UnionRelation> mMyToOtherRealRelations=new System.Collections.Generic.Dictionary<int,UnionRelation>();
 public UnionRelationData(){mCacheDict[1]=new UnionRelationCache{union_id=1009,target_union_id=1008,relationship=UnionRelation.Friendly};
 mCacheDict[2]=new UnionRelationCache{union_id=1008,target_union_id=1009,relationship=UnionRelation.Friendly};mMyToOtherRealRelations[1008]=UnionRelation.Friendly;} }
}''',encoding='utf8')
            test=folder/'Test.cs'
            test.write_text('''using System; using Tenth.Map; using STLab.MapSelectionReader;
class Test { static void Assert(bool b) { if(!b)throw new Exception("Selection regression"); }
static void Main(){ Entry.Start(12319,9320906,6147); var m=MapEventManager.sInstance;
Assert(Entry.Read().Contains("\\\"army_id\\\":\\\"113709493\\\""));
Assert(Entry.Read().Contains("\\\"union_relations\\\":{\\\"status\\\":\\\"ready\\\""));
Assert(Entry.Read().Contains("\\\"effective\\\":[[1008,1]]"));
Assert(Entry.Read().Contains("\\\"relation_colors\\\":{\\\"status\\\":\\\"ready\\\""));
Assert(Entry.Read().Contains("\\\"inside\\\":\\\"#30b8e5\\\""));
MapCommonFacade.ChangeMode(1); Assert(Entry.Read().Contains("\\\"color_mode\\\":1"));
Tenth.Data.UnionRelationData.sInstance.mCacheDict[1].relationship=Tenth.Data.UnionRelation.Hostility;
Tenth.Data.UnionRelationData.sInstance.mMyToOtherRealRelations[1008]=Tenth.Data.UnionRelation.Hostility;
Assert(Entry.Read().Contains("\\\"effective\\\":[[1008,2]]"));
Assert(m.Count==1); m.Select(113709493,ArmySelectedType.PersonalArmy);
Assert(Entry.Read().Contains("\\\"army_id\\\":\\\"113709493\\\""));
m.Select(113310331,ArmySelectedType.GroupArmy); Assert(Entry.Read().Contains("selected_group"));
Assert(Entry.Read().Contains("\\\"group_id\\\":\\\"113310331\\\""));
Assert(Entry.Read().Contains("\\\"army_id\\\":null"));
m.Select(113709493,ArmySelectedType.PersonalArmy); m.Cancel(113709493); Assert(Entry.Read().Contains("cancelled"));
Assert(Entry.Read().Contains("\\\"last_army_id\\\":\\\"113709493\\\""));
Tenth.Network.NetManager.sInstance.mCmdMgr._userId=99; bool rejected=false;
try {Entry.Read();}catch(InvalidOperationException){rejected=true;} Assert(rejected);
Entry.Stop(); Assert(m.Count==0); Assert(Tenth.Network.NetManager.sInstance.mCmdMgr.sends==0);
Console.WriteLine("Managed selection/cancel/scope/cleanup passed"); } }''',encoding='utf8')
            manifest=json.loads((ROOT/'android_reader_assets/map-selection-manifest.json').read_text(encoding='utf-8-sig'))
            dll=folder/manifest['file'];shutil.copyfile(ROOT/'android_reader_assets'/manifest['file'],dll)
            commands=([csc,'/nologo','/target:library','/out:'+str(folder/'Game.Mock.dll'),str(mock)],
                      [csc,'/nologo','/out:'+str(folder/'Test.exe'),'/reference:'+str(folder/'Game.Mock.dll'),'/reference:'+str(dll),str(test)],
                      [str(folder/'Test.exe')])
            for command in commands:
                result=subprocess.run(command,cwd=folder,capture_output=True,text=True,encoding='utf8',errors='replace',timeout=20)
                self.assertEqual(result.returncode,0,result.stdout+result.stderr)


if __name__=='__main__':unittest.main()
