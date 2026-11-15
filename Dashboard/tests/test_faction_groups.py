"""Display factions are explicit diplomacy, independent of the threat fallback."""
import copy
from contextlib import closing
import tempfile
import time
import unittest
from pathlib import Path

from test_support import Archive
from test_support import WorldState
from union_relations import ClientUnionRelations, effective, records
from test_wanderers import wanderer_scene


class FactionGroupsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state = WorldState(self.root)
        self.state.health['capture_source'] = 'dashen'
        self.state.connection('live', 'test')
        self.at = time.time()

    def tearDown(self):
        self.temp.cleanup()

    def apply(self, force=0, union=2000):
        value = wanderer_scene(force=force, union=union)
        value[6]['army'][4], value[6]['army'][5] = int(self.at)-20, int(self.at)+60
        fort = copy.deepcopy(value[14]['10150926']['0'])
        fort[0], fort[2], fort[3] = 4, 200, union
        value[14]['10100925'] = {'0':fort}
        land = copy.deepcopy(fort)
        land[0] = 0
        value[14]['10100924'] = {'0':land}
        self.state.packet(self.at)
        self.state.apply(5026, value, self.at)
        return self.state.snapshot(self.at + .1)

    def manual(self, relation, **target):
        self.state.calibrate(dict(action='relation', relation=relation, **target))

    def test_all_display_groups_and_own_identity(self):
        cases = [(0, 1009, None, 'own'), (0, 2000, None, 'neutral'),
                 (0, 2000, 'enemy', 'enemy_union'), (0, 2000, 'friendly', 'friendly_union'),
                 (1, 2000, None, 'wanderer'), (1, 2000, 'enemy', 'enemy_wanderer'),
                 (1, 2000, 'friendly', 'friendly_wanderer'), (1, 1009, None, 'own')]
        for force, union, relation, expected in cases:
            with self.subTest(expected=expected, force=force):
                self.state.config['relations'] = {} if relation is None else {str(union):{'relation':relation}}
                result = self.apply(force, union)
                self.assertEqual(result['armies'][0]['faction_group'], expected)
                self.assertEqual(result['forts'][0]['faction_group'], expected)
                land = next(t for t in result['land_tiles'] if t['owner_id'] == '200')
                self.assertEqual(land['faction_group'], expected)
                if result['alerts']:
                    self.assertEqual(result['alerts'][0]['faction_group'], expected)

    def test_unmarked_wanderer_keeps_monitoring_without_hostile_label(self):
        result = self.apply(1)
        row = result['armies'][0]
        self.assertEqual((row['relation'], row['declared_relation'], row['faction_group']),
                         ('enemy', 'unknown', 'wanderer'))
        self.assertTrue(row['incoming'])
        self.assertEqual(row['relation_source'], 'unmarked')

    def test_individual_override_does_not_relabel_other_unaffiliated_players(self):
        self.apply(1, 0)
        self.state.players['201'] = {**self.state.players['200'], 'id':'201'}
        self.manual('friendly', scope='player', player_id='200')
        self.assertEqual(self.state._faction('200','0')['faction_group'], 'friendly_wanderer')
        self.assertEqual(self.state._faction('201','0')['faction_group'], 'wanderer')
        self.assertEqual(self.state._relation('200','0'), 'friendly')
        self.assertEqual(self.state._relation('201','0'), 'enemy')
        self.assertEqual(self.state._faction('200','0')['relation_source'], 'manual_player')
        self.assertNotIn('0', self.state.config['relations'])

    def test_personal_overrides_group_until_auto_and_persists(self):
        self.apply(1)
        self.manual('enemy', union_id='2000')
        self.manual('friendly', scope='player', player_id='200')
        restored = WorldState(self.root)
        restored.players = copy.deepcopy(self.state.players)
        self.assertEqual(restored._relation('200','2000'), 'friendly')
        self.manual('auto', scope='player', player_id='200')
        self.assertEqual(self.state._faction('200','2000')['faction_group'], 'enemy_wanderer')
        self.manual('unknown', scope='player', player_id='200')
        self.assertEqual(self.state._relation('200','2000'), 'unknown')
        self.assertEqual(self.state._faction('200','2000')['faction_group'], 'wanderer')

    def test_other_role_preferences_and_invalid_clients_are_not_used(self):
        self.apply(1)
        self.manual('friendly', scope='player', player_id='200')
        self.state.config['self']['user_id'] = '999'
        self.assertEqual(self.state._faction('200','2000')['faction_group'], 'wanderer')
        self.state.config['relations']['2000'] = dict(source='client', relation='friendly',
            own_union_id='999', user_id='999')
        self.state.health['union_relations'] = {'status':'ready'}
        self.assertEqual(self.state._faction('200','2000')['faction_group'], 'wanderer')

    def test_invalid_and_own_targets_cannot_be_overridden(self):
        self.apply()
        for target in ({'union_id':'0'}, {'union_id':'-1'}, {'union_id':'player:200'},
                       {'scope':'player','player_id':'999'}, {'union_id':'1009'}):
            with self.subTest(target=target), self.assertRaises(ValueError):
                self.manual('enemy', **target)
        self.state.players['200']['union_id'] = '1009'
        with self.assertRaises(ValueError):
            self.manual('enemy', scope='player', player_id='200')
        self.state.config['relations']['0'] = {'relation':'friendly', 'manual_override':True}
        self.assertEqual(self.state._relation('201','0'), 'unknown')

    def test_client_changes_reclassify_but_preserve_manual_and_archive_scope(self):
        self.apply(1)
        connection = dict(source='dashen', pid=7, host_pid=8, local_port=123, connection_started=1)
        self.state.connection('live','test',connection)
        with closing(Archive(self.root)) as archive:
            sync = ClientUnionRelations(self.state,archive,lambda:connection)
            def cache(kind, at):
                rows = [dict(id=1,union_id=1009,target_union_id=2000,relationship=kind,next_time=0,cron_time=0)]
                frame = dict(status='ready',game_requests_sent=0,server_id=45122,user_id=12319,union_id=1009,
                             read_at=at,directional=False,records=rows,
                             effective=[[int(k),v] for k,v in effective(records(rows),1009,False).items()])
                sync.cache(frame,connection,at+.01)
            cache(2,self.at)
            self.assertEqual(self.state._faction('200','2000')['faction_group'],'enemy_wanderer')
            self.manual('enemy',scope='player',player_id='200')
            cache(1,self.at+1)
            self.assertEqual(self.state._relation('200','2000'),'enemy')
            self.assertIn('player:200',self.state.config['relations'])
            self.assertNotIn('player:200',archive.intelligence.unions({'union_id':'','relation':'enemy'}))
            self.manual('auto',scope='player',player_id='200')
            self.assertEqual(self.state._faction('200','2000')['faction_group'],'friendly_wanderer')

if __name__ == '__main__':
    unittest.main()
