"""Live diplomacy uses client-computed semantics and the current role/session."""
import copy
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from test_support import Archive
from collector import Collector
from game_army_focus import GameArmyFocus
from test_support import WorldState
from test_dashboard import scene
from test_intelligence import battle
from union_relations import ClientUnionRelations,effective,records


def row(ident=1,origin=1009,target=1008,kind=2):
    return dict(id=ident,union_id=origin,target_union_id=target,relationship=kind,next_time=0,cron_time=0)


class RelationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.archive=Archive(self.root);self.state=WorldState(self.root);self.at=time.time()
        self.connection={'source':'dashen','pid':7,'host_pid':8,'local_port':123,'connection_started':1}
        self.state.connection('live','test',self.connection);self.state.packet(self.at)
        self.sync=ClientUnionRelations(self.state,self.archive,lambda:self.connection)

    def tearDown(self):
        self.archive.close();self.temp.cleanup()

    def frame(self,items=None,directional=True,at=None):
        items=[row()]if items is None else items
        return {'status':'ready','game_requests_sent':0,'server_id':45122,'user_id':12319,'union_id':1009,
                'read_at':self.at if at is None else at,'directional':directional,'records':items,
                'effective':[[int(k),v]for k,v in effective(records(items),1009,directional).items()]}

    def cache(self,items=None,**kwargs):
        f=self.frame(items,**kwargs)
        return self.sync.cache(f,self.connection,f['read_at']+.01)

    def test_directional_and_symmetric_semantics_follow_client(self):
        items=records([row(kind=1)])
        self.assertEqual(effective(items,1009,True),{'1008':0})
        self.assertEqual(effective(items,1009,False),{'1008':1})
        items=records([row(kind=1),row(2,1008,1009,1)])
        self.assertEqual(effective(items,1009,True),{'1008':1})
        items[2]['relationship']=2
        self.assertEqual(effective(items,1009,True),{'1008':2})
        self.assertEqual(effective(items,0,True),{})

    def test_full_snapshot_replaces_legacy_and_keeps_no_phantom_enemies(self):
        self.state.config['relations']={'888':{'relation':'enemy'}}
        self.assertTrue(self.cache([row(kind=1),row(2,1008,1009,1)]))
        self.assertEqual(self.state._relation('200','1008'),'friendly')
        self.assertNotIn('888',self.state.config['relations'])
        self.assertTrue(self.cache([],at=self.at+1))
        self.assertEqual(self.state._relation('200','1008'),'unknown')
        self.assertNotIn('1008',self.state.config['relations'])

    def test_notify_changes_remove_and_stale_replies_do_not_restore_old_relation(self):
        self.cache()
        self.assertEqual(self.state._relation('200','1008'),'enemy')
        packet=[[[],[row(kind=1),row(2,1008,1009,1)]],[[],[]]]
        self.assertTrue(self.sync.packet(6327,packet,self.connection,self.at+1))
        self.assertEqual(self.state._relation('200','1008'),'friendly')
        self.assertFalse(self.sync.cache(self.frame(),self.connection,self.at+1.1))
        self.assertEqual(self.state._relation('200','1008'),'friendly')
        self.sync.packet(6327,[[[],[]],[[],[2]]],self.connection,self.at+2)
        self.assertEqual(self.state._relation('200','1008'),'unknown')
        self.sync.packet(6326,[[[],[]],[[],[]]],self.connection,self.at+3)
        self.assertEqual(self.state.config['relations'],{})

    def test_manual_override_and_switching_back_to_automatic(self):
        self.cache()
        self.state.calibrate({'action':'relation','union_id':'1008','relation':'friendly'})
        self.cache(at=self.at+1)
        self.assertEqual(self.state._relation('200','1008'),'friendly')
        self.state.calibrate({'action':'relation','union_id':'1008','relation':'auto'})
        self.assertEqual(self.state._relation('200','1008'),'enemy')
        self.assertFalse(self.state.config['relations']['1008'].get('manual_override'))

    def test_unverified_scope_or_inconsistent_cache_does_not_change_relations(self):
        self.cache();before=copy.deepcopy(self.state.config['relations'])
        for change in ({'user_id':999},{'union_id':999},{'effective':[[1008,1]]},{'game_requests_sent':1},
                       {'records':[row(),row()]},{'directional':None}):
            f=self.frame(at=self.at+1);f.update(change)
            self.assertFalse(self.sync.cache(f,self.connection,self.at+1.1))
            self.assertEqual(self.state.config['relations'],before)
        self.assertFalse(self.sync.cache(self.frame(at=self.at+1),{**self.connection,'pid':9},self.at+1.1))

    def test_change_updates_map_alerts_and_archive_filters_together(self):
        value=scene();value[1]['200'][2]=1008;value[6]['army'][0]=1;value[6]['army'][9]=13
        value[6]['army'][4:6]=[int(self.at)-10,int(self.at)+100];value[17]=[995,890,1030,950]
        self.state.health['capture_source']='dashen';self.state.apply(5026,value,self.at)
        self.cache()
        self.assertTrue(self.state.snapshot(self.at+.1)['alerts'])
        self.assertIn('1008',self.archive.intelligence.unions({'union_id':'','relation':'enemy'}))
        self.cache([row(kind=1),row(2,1008,1009,1)],at=self.at+1)
        self.assertEqual(self.state.snapshot(self.at+1.1)['alerts'],[])
        self.assertNotIn('1008',self.archive.intelligence.unions({'union_id':'','relation':'enemy'}))
        self.cache([row(kind=1),row(2,1008,1009,2)],at=self.at+2)
        self.assertTrue(self.state.snapshot(self.at+2.1)['alerts'])

    def test_same_snapshot_does_not_rewrite_history_and_reconnect_revalidates(self):
        self.cache();count=self.archive.db.execute("SELECT count(*)FROM observations WHERE kind='client_union_relations'").fetchone()[0]
        self.assertFalse(self.cache(at=self.at+1))
        self.assertEqual(self.archive.db.execute("SELECT count(*)FROM observations WHERE kind='client_union_relations'").fetchone()[0],count)
        self.state.connection('waiting','reconnect')
        self.assertEqual(self.state._relation('200','1008'),'unknown')
        self.state.connection('live','test',self.connection)
        self.cache(at=self.at+2)
        self.assertEqual(self.state._relation('200','1008'),'enemy')
        self.assertIn('1008',self.state.client_relations,'Same-socket reconnect must restore automatic choices')
        self.state.calibrate({'action':'relation','union_id':'1008','relation':'friendly'})
        self.state.calibrate({'action':'relation','union_id':'1008','relation':'auto'})
        self.assertEqual(self.state._relation('200','1008'),'enemy')

    def test_relationship_is_polled_even_when_no_army_is_selected(self):
        bridge=SimpleNamespace(read_map_selection=lambda:{'sequence':0,'union_relations':self.frame()})
        task=GameArmyFocus(self.state,bridge,lambda:self.connection,relation_observer=self.sync.cache)
        task.step()
        self.assertEqual(self.state._relation('200','1008'),'enemy')

    def test_delta_waits_for_full_baseline_and_known_client_mode(self):
        self.assertFalse(self.sync.packet(6327,[[[],[row()]],[[],[]]],self.connection,self.at))
        self.assertEqual(self.state.config['relations'],{})

    def test_official_notification_fixture_matches_directional_rules(self):
        fixture=json.loads((Path(__file__).parent/'fixtures/client_union_relations_20261114.json').read_text(encoding='utf-8'))
        self.cache([])
        self.sync.packet(6326,fixture,self.connection,self.at+1)
        resolved=self.state.config['relations']
        self.assertEqual(resolved['1008']['relation'],'enemy')
        self.assertEqual(resolved['1005']['relation'],'friendly')
        self.assertEqual(resolved['1135']['relation'],'unknown','Incoming-only friendship is not mutual friendship')

    def collector(self,observer):
        collector=object.__new__(Collector)
        collector.archive=None;collector.state=self.state;collector.source='dashen'
        collector.report_observer=collector.game_click_focus=None;collector.relation_observer=observer
        return collector

    def test_relation_write_failure_does_not_interrupt_map_capture(self):
        def fail(*args):raise OSError('temporary write failure')
        collector=self.collector(fail)
        collector._message(6327,[[[],[]],[[],[]]],self.at)
        self.assertEqual(self.state.health['status'],'live')
        self.assertEqual(self.state.health['union_relations']['status'],'retrying')

    def test_unrelated_packets_do_not_read_relation_configuration(self):
        calls=[];collector=self.collector(lambda *args:calls.append(args))
        collector._message(1234,{},self.at)
        self.assertEqual(calls,[])


if __name__=='__main__':unittest.main()
