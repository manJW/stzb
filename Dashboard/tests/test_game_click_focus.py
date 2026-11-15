import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from game_click_focus import ClickTracker,GameClickFocus,city_request,city_selection
from test_support import WorldState


class FocusTests(unittest.TestCase):
    def setUp(self):
        self.parent = ROOT/'runtime'/'game-click-tests'
        self.parent.mkdir(exist_ok=True)
        self.folder = Path(tempfile.mkdtemp(prefix='case-',dir=self.parent)).resolve()
        self.state = WorldState(self.folder)
        self.connection = {'source':'dashen','pid':7,'host_pid':8,'peer':'game',
                           'local_port':123,'connection_started':1}
        self.state.connection('live','test',self.connection)
        self.now = time.time()
        self.state.packet(self.now)
        self.watcher = GameClickFocus(self.state)

    def tearDown(self):
        if self.folder.parent!=self.parent.resolve() or not self.folder.name.startswith('case-'):
            raise RuntimeError('Unexpected test directory')
        shutil.rmtree(self.folder)

    def city(self,world_id=9860923):
        value = [0]*66
        value[0:3] = ['弋阳要塞2','时运丨渊虹剑','風丨金球凯恩']
        value[4:6],value[65] = [204,325],world_id
        return value

    def test_clicks_exclude_drags_outside_clicks_window_switch_and_holds(self):
        def track(sequence):
            tracker = ClickTracker()
            return [tracker.sample(*sample) for sample in sequence]
        press,release = (True,(100,100),1,True,0),(False,(101,101),1,True,.1)
        self.assertEqual(track([press,release]),[False,True])
        for middle in ((True,(115,100),1,True,.05),(True,(100,100),2,True,.05),
                       (True,(100,100),1,False,.05),(True,(100,100),1,True,2)):
            with self.subTest(middle=middle):
                self.assertFalse(any(track([press,middle,release])))
        self.assertFalse(any(track([(True,(100,100),1,False,0),release])))

    def test_protocol_rejects_unknown_shapes_and_competing_coordinates(self):
        self.assertEqual(city_request([9860923,0]),[986,923])
        for body in ([9860923,9870923],['9860923'],[True],{'wid':9860923}):
            self.assertIsNone(city_request(body))
        self.assertEqual(city_selection(self.city()),([986,923],'弋阳要塞2'))
        self.assertIsNone(city_selection(self.city()[:-1]))
        bad = self.city();bad[65]='9860923'
        self.assertIsNone(city_selection(bad))

    def test_verified_packets_can_focus_before_full_map_without_ocr_or_map_bounds(self):
        self.assertTrue(self.state.health['resync_required'])
        self.assertIsNone(self.state.area)
        self.watcher.submit(self.connection)
        self.watcher.on_request(21,[9860923,0],self.now+.01)
        self.assertTrue(self.watcher.on_message(21,self.city(),self.now+.02))
        focus = self.state.snapshot()['game_focus']
        self.assertEqual(focus['coord'],[986,923])
        self.assertEqual(focus['evidence'],'selected_tile_request_response_21')
        self.assertEqual(focus['name'],'弋阳要塞2')

    def test_receive_only_single_click_keeps_the_verified_fast_path(self):
        self.watcher.submit(self.connection)
        self.assertTrue(self.watcher.on_message(21,self.city(),self.now+.02))
        self.assertEqual(self.state.game_focus['evidence'],'selected_tile_response_21')
        self.assertFalse(self.watcher.on_message(21,self.city(9870923),self.now+.03))
        self.assertEqual(self.state.game_focus['coord'],[986,923])

    def test_latest_requested_tile_wins_over_a_late_previous_reply(self):
        self.watcher.submit(self.connection)
        self.watcher.on_request(21,[9860923],self.now+.01)
        self.watcher.submit(self.connection)
        self.watcher.on_request(21,[9870923],self.now+.02)
        self.assertFalse(self.watcher.on_message(21,self.city(9860923),self.now+.03))
        self.assertIsNone(self.state.game_focus)
        self.assertTrue(self.watcher.on_message(21,self.city(9870923),self.now+.04))
        self.assertEqual(self.state.game_focus['coord'],[987,923])
        self.assertFalse(self.watcher.on_message(21,self.city(9860923),self.now+.05))
        self.assertEqual(self.state.game_focus['coord'],[987,923])

    def test_request_and_response_just_before_button_up_are_buffered(self):
        self.watcher.on_request(21,[9860923],self.now)
        self.watcher.on_message(21,self.city(),self.now+.01)
        self.assertIsNone(self.state.game_focus)
        self.assertTrue(self.watcher.submit(self.connection))
        self.assertEqual(self.state.game_focus['coord'],[986,923])

    def test_background_or_expired_responses_and_lost_requests_do_not_focus(self):
        self.assertFalse(self.watcher.on_message(21,self.city(),self.now-5))
        self.watcher.submit(self.connection)
        self.assertIsNone(self.state.game_focus)
        self.watcher.on_request(21,[9860923],self.now+.01)
        self.watcher.request_gap()
        self.assertFalse(self.watcher.on_message(21,self.city(),self.now+.02))
        self.assertIsNone(self.state.game_focus)

    def test_new_click_on_same_tile_is_a_new_event_and_source_changes_invalidate_it(self):
        self.watcher.submit(self.connection)
        self.watcher.on_request(21,[9860923],self.now+.01)
        self.watcher.on_message(21,self.city(),self.now+.02)
        first = self.state.game_focus['event_id']
        self.watcher.submit(self.connection)
        self.assertNotEqual(first,self.state.game_focus['event_id'])
        self.state.connection('waiting','test')
        self.assertIsNone(self.state.game_focus)
        self.assertFalse(self.watcher.on_message(21,self.city(),self.now+.03))

    def test_other_role_and_stale_transport_are_rejected(self):
        self.state.config['self']['confirmed']=False
        self.assertIsNone(self.watcher.connection())
        self.state.config['self']['confirmed']=True
        self.state.packet(self.now-20)
        self.assertIsNone(self.watcher.connection())


if __name__=='__main__':
    unittest.main()
