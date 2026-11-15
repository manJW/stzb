import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_support import WorldState
from presence_audit import PresenceAudit

FIXTURE = json.loads((Path(__file__).parent/'fixtures/dashen_fort_region_transfer_20261113.json').read_text(encoding='utf-8'))


class PresenceAuditTests(unittest.TestCase):
    def test_resident_audit_checks_real_handoff_and_tracks_departure(self):
        with tempfile.TemporaryDirectory() as folder:
            state = WorldState(folder)
            state.health['capture_source'] = 'dashen'
            state.connection('live', 'fixture')
            for name in ('baseline', 'inside', 'old_region_remove', 'retreat'):
                frame = FIXTURE[name]
                state.apply(frame['message_id'], frame['scene'], frame['at'])
            result = state.presence_audit.report()
            self.assertEqual((result['checked'], result['preserved_cross_region'], result['mismatches']), (1, 1, 0))
            self.assertEqual(result['transitions'], {'4->5': 1, '5->4': 1})
            state.persist()
            saved = json.loads((Path(folder)/'runtime/presence-audit.json').read_text(encoding='utf-8'))
            self.assertEqual(saved['mismatches'], 0)
            self.assertEqual(len(saved['recent_transitions']), 2)

    def test_independent_notification_oracle_detects_a_hidden_valid_region(self):
        monitor = PresenceAudit()
        state = SimpleNamespace(health={'capture_source': 'dashen'}, epoch=0, armies={})
        for name in ('inside', 'old_region_remove'):
            frame = FIXTURE[name]
            if name == 'inside':
                state.armies[FIXTURE['army_id']] = {'update_order': frame['scene'][18], 'state_code': 5, 'visible': True}
            else:
                # Inject the old bug: a real surviving region is globally hidden.
                state.armies[FIXTURE['army_id']]['visible'] = False
            monitor.observe(state, frame['scene'], frame['at'])
        self.assertEqual(monitor.summary()['mismatches'], 1)
        self.assertEqual(monitor.report()['issues'][0]['regions'], {'72061': True, '72062': False})
        self.assertTrue(monitor.summary()['last_issue']['expected_visible'])

    def test_scope_state_is_bounded_and_reset_on_connection_epoch(self):
        monitor = PresenceAudit()
        state = SimpleNamespace(health={'capture_source': 'dashen'}, epoch=0, armies={})
        template = FIXTURE['inside']['scene']
        raw = template[6][FIXTURE['army_id']]
        for index in range(2200):
            value = copy.deepcopy(template)
            value[6] = {str(index): raw}
            monitor.observe(state, value, index)
        self.assertLessEqual(len(monitor.groups), 1024)
        self.assertLessEqual(len(monitor.states), 2048)
        state.epoch = 1
        value = [None]*34
        monitor.observe(state, value, 3000)
        self.assertEqual(len(monitor.groups), 0)
        self.assertEqual(monitor.summary()['tracked_armies'], 0)


if __name__ == '__main__':
    unittest.main()
