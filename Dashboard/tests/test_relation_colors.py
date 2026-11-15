"""A game's palette follows its verified login and active color mode."""
import copy
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from game_army_focus import GameArmyFocus
from test_support import Archive, WorldState
from union_relations import ClientUnionRelations


class PaletteTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.archive=Archive(self.root);self.state=WorldState(self.root);self.at=time.time()
        self.connection={'source':'dashen','pid':7,'host_pid':8,'local_port':123,'connection_started':1}
        self.state.connection('live','test',self.connection);self.state.packet(self.at)
        self.sync=ClientUnionRelations(self.state,self.archive,lambda:self.connection)
        self.frame={'status':'ready','source':'client_map_palette','game_requests_sent':0,
            'server_id':6147,'user_id':12319,'union_id':1009,'read_at':self.at,'color_mode':0,
            'styles':{key:dict(inside='#30b8e5',outside='#85d8ea')
                      for key in ('self','union','other','enemy','bandit','friend_other','friend_bandit')}}

    def tearDown(self):
        self.archive.close();self.temp.cleanup()

    def apply(self,frame=None):
        return self.sync.palette(frame or self.frame,self.connection,self.at+.1)

    def test_palette_is_published_and_mode_change_replaces_it_without_network_requests(self):
        self.assertTrue(self.apply())
        self.assertEqual(self.state.health['relation_colors']['styles'],self.frame['styles'])
        before=(self.root/'runtime/client-relation-colors.json').stat().st_mtime_ns
        self.assertTrue(self.apply())
        self.assertEqual(before,(self.root/'runtime/client-relation-colors.json').stat().st_mtime_ns)
        self.frame.update(color_mode=2,read_at=self.at+.01)
        self.frame['styles']['union']['inside']='#123456'
        self.assertTrue(self.apply())
        saved=json.loads((self.root/'runtime/client-relation-colors.json').read_text(encoding='utf8'))
        self.assertEqual(saved['color_mode'],2)
        self.assertEqual(saved['styles']['union']['inside'],'#123456')

    def test_bad_scope_stale_incomplete_or_injected_styles_cannot_replace_palette(self):
        self.apply();expected=copy.deepcopy(self.state.health['relation_colors'])
        for change in ({'server_id':1},{'user_id':999},{'union_id':999},{'color_mode':4},
                       {'read_at':self.at-20},{'read_at':self.at+5},{'game_requests_sent':1},
                       {'source':'screenshot'},{'styles':{}},{'color_mode':True}):
            frame={**self.frame,**change}
            self.assertFalse(self.apply(frame),change)
            self.assertEqual(self.state.health['relation_colors'],expected)
        frame=copy.deepcopy(self.frame);frame['styles']['union']['inside']='red;}body{display:none'
        self.assertFalse(self.apply(frame))
        self.assertFalse(self.sync.palette(self.frame,{**self.connection,'pid':9},self.at+.1))

    def test_no_union_and_new_epoch_use_current_role_without_old_palette(self):
        self.apply();self.state.connection('disconnected','test')
        self.assertEqual(self.state.health['relation_colors'],{'status':'waiting'})
        self.state.config['self']['union_id']='0';self.archive.profile['union_id']='0'
        self.state.connection('live','test',self.connection)
        self.frame.update(union_id=0,read_at=self.at+.01)
        self.assertTrue(self.apply())
        self.assertEqual(self.state.health['relation_colors']['own_union_id'],'0')

    def test_poll_publishes_colors_even_without_selected_army(self):
        bridge=SimpleNamespace(read_map_selection=lambda:{'sequence':0,'game_requests_sent':0,'relation_colors':self.frame})
        worker=GameArmyFocus(self.state,bridge,lambda:self.connection,palette_observer=self.sync.palette)
        self.assertFalse(worker.step())
        self.assertEqual(self.state.health['relation_colors']['status'],'ready')


if __name__=='__main__':unittest.main()
