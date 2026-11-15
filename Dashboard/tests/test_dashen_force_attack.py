"""Real Dashen force-attack regression: state -> own-land alert -> saved main forecast."""
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from test_support import Archive
from lineup_prediction import predict_alerts
from test_support import WorldState

FIXTURE = json.loads((Path(__file__).parent / 'fixtures/dashen_force_attack_20261113.json').read_text(encoding='utf-8'))


class DashenForceAttackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = WorldState(self.root)
        self.state.config['relations']['1008'] = {'relation': 'enemy'}
        self.state.health['capture_source'] = 'dashen'
        self.state.connection('live', 'fixture')
        for name in ('ownership', 'baseline', 'attack'):
            item = FIXTURE[name]
            self.state.packet(item['received_at'])
            self.state.apply(item['message_id'], copy.deepcopy(item['scene']), item['received_at'])
        self.at = FIXTURE['snapshot_at']

    def tearDown(self):
        self.tmp.cleanup()

    def row(self, snapshot):
        return next(row for row in snapshot['armies'] if row['id'] == FIXTURE['army_id'])

    def test_real_force_attack_uses_refreshed_member_owned_land(self):
        snapshot = self.state.snapshot(self.at)
        row = self.row(snapshot)
        self.assertTrue(snapshot['live'] and snapshot['reliable'])
        self.assertEqual((row['signature'], row['kind'], row['relation']), ('1:13:600076', 'attack', 'enemy'))
        self.assertEqual((row['from'], row['target']), ([980, 895], [972, 899]))
        self.assertEqual((row['target_owner_id'], row['target_union_id'], row['target_side']), ('11404', '1009', 'own'))
        self.assertTrue(row['target_ownership_fresh'])
        self.assertEqual(row['target_observed_at'], FIXTURE['ownership']['received_at'])
        self.assertEqual([alert['army_id'] for alert in snapshot['alerts']], [FIXTURE['army_id']])
        self.assertTrue(snapshot['config']['state_rules']['1:13:*']['verified'])

    def test_alert_produces_a_main_forecast_from_preexisting_report(self):
        archive = Archive(self.root)
        try:
            archive.ingest(92, [[FIXTURE['prior_report']]], FIXTURE['prior_report_observed_at'])
            scene = FIXTURE['attack']
            archive.ingest(scene['message_id'], scene['scene'], scene['received_at'], source=scene['source'])
            result = predict_alerts(archive, self.state.snapshot(self.at), record=True)
            forecast = result[FIXTURE['army_id']]
            self.assertEqual(forecast['force_assessment']['kind'], 'main')
            self.assertEqual(forecast['items'][0]['battle_id'], '5946641')
            saved = archive.db.execute('SELECT issued_at,data FROM lineup_forecasts WHERE army_id=?',
                                       (FIXTURE['army_id'],)).fetchall()
            self.assertEqual(len(saved), 1)
            self.assertLess(saved[0]['issued_at'], FIXTURE['battle_evidence']['at'])
            self.assertEqual(json.loads(saved[0]['data'])['force_assessment']['kind'], 'main')
        finally:
            archive.close()

    def test_stale_or_other_side_land_still_cannot_raise_an_own_land_alert(self):
        tile = self.state.tiles['9720899']
        tile['epoch'] = -1
        snapshot = self.state.snapshot(self.at)
        self.assertEqual(self.row(snapshot)['kind'], 'attack')
        self.assertEqual(snapshot['alerts'], [])
        self.assertIn('地块归属', snapshot['candidates'][0]['pending_reason'])
        tile.update(epoch=self.state.epoch, owner='15207', union_id='1008')
        snapshot = self.state.snapshot(self.at)
        self.assertEqual(snapshot['alerts'], [])
        self.assertEqual(self.row(snapshot)['target_side'], 'other')

    def test_same_army_real_retreat_is_not_another_incoming_attack(self):
        item = FIXTURE['retreat']
        self.state.packet(item['received_at'])
        self.state.apply(item['message_id'], item['scene'], item['received_at'])
        snapshot = self.state.snapshot(item['received_at'] + .1)
        self.assertEqual((self.row(snapshot)['state_code'], self.row(snapshot)['kind']), (4, 'retreat'))
        self.assertEqual(snapshot['alerts'], [])
        self.assertFalse(any(row.get('pending_reason') for row in snapshot['candidates']))

    def test_source_allowlist_and_explicit_calibration_remain_independent(self):
        for source, expected in (('pc', 'attack'), ('dashen', 'attack'), ('mumu', None), ('unknown', None)):
            with self.subTest(source=source):
                self.state.health['capture_source'] = source
                self.assertEqual(self.row(self.state.snapshot(self.at))['kind'], expected)
        self.state.health['capture_source'] = 'dashen'
        self.state.config['state_rules']['1:13:*'] = {'kind': 'other', 'verified': True}
        self.assertEqual(self.row(self.state.snapshot(self.at))['kind'], 'other')
        self.assertEqual(self.state.snapshot(self.at)['alerts'], [])
        del self.state.config['state_rules']['1:13:*']
        self.state.armies[FIXTURE['army_id']]['from'] = [972, 899]
        self.assertIsNone(self.row(self.state.snapshot(self.at))['kind'])

    def test_existing_type_zero_calibration_is_preserved(self):
        self.state.config['state_rules']['1:0:*'] = {'kind': 'attack', 'verified': True, 'require_different_coordinate': True}
        self.state.armies[FIXTURE['army_id']].update(target_type=0, signature='1:0:600076')
        self.assertEqual(self.row(self.state.snapshot(self.at))['kind'], 'attack')
        self.assertEqual(len(self.state.snapshot(self.at)['alerts']), 1)


if __name__ == '__main__':
    unittest.main()
