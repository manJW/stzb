"""A region-local removal must not erase the same-version update in another region."""
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_support import WorldState

FIXTURE = json.loads((Path(__file__).parent / 'fixtures/dashen_fort_region_transfer_20261113.json').read_text(encoding='utf-8'))


class RegionPresenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = WorldState(self.tmp.name)
        self.state.health['capture_source'] = 'dashen'
        self.state.config['relations']['1008'] = {'relation': 'enemy'}
        self.state.connection('live', 'fixture')
        self.apply('baseline')
        self.apply('inside')

    def tearDown(self):
        self.tmp.cleanup()

    def apply(self, name, scene=None):
        item = FIXTURE[name]
        self.state.packet(item['at'])
        self.state.apply(item['message_id'], copy.deepcopy(item['scene'] if scene is None else scene), item['at'])

    def row(self):
        return self.state.snapshot(FIXTURE['snapshot_at'])['armies'][0]

    def test_real_inside_update_survives_old_region_removal(self):
        self.apply('old_region_remove')
        row = self.row()
        self.assertEqual((row['state_code'], row['position']), (5, [1137, 976]))
        self.assertTrue(row['visible'])
        self.assertFalse(row['stale'])
        self.assertEqual(row['player_name'], '情绪乄高欢')

    def test_same_region_or_global_removal_still_hides_the_army(self):
        for scope in ([2, 72061], None, [3, 72062]):
            with self.subTest(scope=scope):
                self.apply('inside')
                scene = copy.deepcopy(FIXTURE['old_region_remove']['scene'])
                scene[20] = scope
                self.apply('old_region_remove', scene)
                self.assertFalse(self.row()['visible'])

    def test_a_later_removal_or_different_source_is_not_ignored(self):
        scene = copy.deepcopy(FIXTURE['old_region_remove']['scene'])
        scene[18] += 1
        self.apply('old_region_remove', scene)
        self.assertFalse(self.row()['visible'])
        self.apply('inside')
        self.state.health['capture_source'] = 'unknown'
        self.apply('old_region_remove')
        self.assertFalse(self.row()['visible'])

    def test_removal_before_the_new_region_update_converges_to_same_result(self):
        self.apply('baseline')
        self.apply('old_region_remove')
        self.apply('inside')
        self.assertTrue(self.row()['visible'])

    def test_two_updated_regions_keep_the_surviving_membership(self):
        scene = copy.deepcopy(FIXTURE['inside']['scene'])
        scene[20] = [2, 72062]
        self.apply('inside', scene)
        self.apply('old_region_remove')
        self.assertTrue(self.row()['visible'])
        scene = copy.deepcopy(FIXTURE['old_region_remove']['scene'])
        scene[20] = [2, 72061]
        self.apply('old_region_remove', scene)
        self.assertFalse(self.row()['visible'])

    def test_departure_and_new_connection_do_not_keep_stationary_ghosts(self):
        self.apply('old_region_remove')
        self.apply('retreat')
        row = self.state.snapshot(FIXTURE['retreat']['at']+.1)['armies'][0]
        self.assertEqual(row['kind'], 'retreat')
        self.assertIsNone(row['position'])
        self.state.gap()
        self.assertTrue(self.state.snapshot(FIXTURE['retreat']['at']+.2)['armies'][0]['stale'])


if __name__ == '__main__':
    unittest.main()
