import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from garrison_trial import GarrisonTrial, RECRUIT_OPTION, TEAM, available_garrison_teams, own_hero_roster, read_arrival, read_duration, read_point, select_team


class TrialTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.connection = {'source':'pc'}
        self.army = {'id':TEAM, 'owner':'71021', 'relation':'self', 'current_connection':True,
                     'stale':False, 'state_code':5, 'target_type':0, 'kind':None, 'position':[1011,942],
                     'from':[1011,942], 'target':[1011,942], 'troops':[{'code':code} for code in (3,3,0)]}
        self.alert = {'army_id':'enemy', 'coord':[1006,941], 'end':1150}
        self.snapshot = {'reliable':True, 'health':{'connection':self.connection},
                         'alerts':[self.alert], 'armies':[self.army]}
        state = SimpleNamespace(snapshot=lambda: self.snapshot)
        archive = SimpleNamespace(profile={'account_role_id':'7900021'})
        self.trial = GarrisonTrial(state, archive, SimpleNamespace(lock=threading.Lock()),
                                  lambda: None, Path(self.directory.name))
        self.trial.status.update(phase='reading', until=1200)
        self.game = MagicMock()
        self.screen = MagicMock(captured_at=1000, width=1440)
        self.screen.checked_options.return_value = set()

    def run_attempt(self):
        with (patch('garrison_trial.WindowsOcr'), patch('garrison_trial.restore_map'),
              patch('garrison_trial.time.time', return_value=1000),
              patch('garrison_trial.open_garrison', return_value=(self.screen, 92, 1092))):
            self.trial.try_dispatch(self.game, self.connection, (1011,942), self.alert)

    def test_legacy_hero_without_troop_code_does_not_block_roster(self):
        archive = SimpleNamespace(profile_id='test', db=MagicMock())
        archive.db.execute.return_value = []
        heroes = [{'id':100004,'position':1,'name':'蔡文姬'},
                  {'id':100647,'position':2,'name':'华雄'}]
        legacy = {'army_id':TEAM,'hero_instances':'0,1,2,0,0','heroes':json.dumps(heroes)}
        current = {**legacy, 'heroes':json.dumps([{**hero,'troop_code':3} for hero in heroes])}
        self.assertEqual(own_hero_roster(archive, self.army, [legacy]), ([], None))
        self.assertEqual(own_hero_roster(archive, self.army, [legacy,current]), (['蔡文姬','华雄'], '同队历史'))

    def test_submit_once_then_disallow_retry(self):
        self.run_attempt()
        self.assertEqual(self.trial.snapshot()['phase'], 'submitting')
        self.assertEqual(self.trial.snapshot()['lead_seconds'], 58)
        with self.assertRaises(ValueError):
            self.trial.arm()
        self.run_attempt()
        self.screen.hit.assert_called_once()

    def test_reject_simultaneous_or_stale_incoming(self):
        self.alert['end'] = 1092
        self.run_attempt()
        self.assertEqual(self.trial.snapshot()['phase'], 'waiting')
        self.trial.status['phase'] = 'reading'
        self.alert['end'] = 1150
        self.snapshot['alerts'] = []
        with self.assertRaises(RuntimeError):
            self.run_attempt()
        self.screen.hit.assert_not_called()

    def test_cancel_and_require_designated_army(self):
        self.trial.cancel()
        self.run_attempt()
        self.screen.hit.assert_not_called()
        self.assertIs(select_team(self.snapshot), self.army)
        self.army['id'] = 'another'
        self.assertIsNone(select_team(self.snapshot))

    def test_observes_arrival_guard_and_return_as_distinct_states(self):
        self.trial.archive.lock = threading.Lock()
        self.trial.archive.profile_id = 'test-profile'
        self.trial.archive.db = MagicMock()
        self.trial.return_battle = MagicMock(return_value=None)
        def observation(code, start, end, origin, target, at, target_type):
            return dict(state_code=code, target_type=target_type, from_x=origin[0], from_y=origin[1],
                        to_x=target[0], to_y=target[1], begin_at=start, end_at=end, observed_at=at)
        origin, target = (1011,942), (1006,941)
        rows = [observation(3, 1000, 1092, origin, target, 1001, 14)]
        self.trial.archive.db.execute.return_value.fetchall.side_effect = lambda: list(rows)
        self.trial.status.update(phase='submitting', submitted_at=1000, enemy_arrival=1150,
                                 source=list(origin), target=list(target))

        def phase(at, expected):
            with patch('garrison_trial.time.time', return_value=at):
                self.trial.observe_dispatch(self.trial.snapshot())
            self.assertEqual(self.trial.snapshot()['phase'], expected)

        phase(1003, 'marching')
        self.assertNotIn('arrived_at', self.trial.snapshot())
        rows.append(observation(6, 1000, 1092, target, target, 1092, 14))
        phase(1092, 'arrived')
        self.army.update(kind='garrison', position=list(target))
        phase(1094, 'holding')
        self.snapshot['reliable'] = False
        phase(1095, 'arrived')
        rows.append(observation(4, 1096, 1188, target, origin, 1096, 0))
        phase(1097, 'returning')
        phase(1189, 'returning')
        rows.append(observation(5, 1096, 1188, origin, origin, 1188, 0))
        phase(1190, 'returned')
        result = self.trial.snapshot()
        self.assertEqual((result['arrived_at'], result['return_started_at'], result['returned_at']),
                         (1092, 1096, 1188))

    def test_web_target_stays_bound_to_the_selected_march(self):
        self.alert.update(army_id='9001', player_name='Selected')
        other = {**self.alert, 'army_id':'9002', 'end':1135, 'player_name':'Other'}
        self.snapshot.update(server_now=1000, alerts=[other, self.alert])
        self.trial.status['phase'] = 'off'
        predictions = {a['army_id']:{'force_assessment':{'kind':'main'}} for a in self.snapshot['alerts']}
        with (patch('garrison_trial.predict_alerts', return_value=predictions),
              patch('garrison_trial.time.time', return_value=1000)):
            with self.assertRaisesRegex(ValueError, '已更新'):
                self.trial.arm({**self.alert, 'end':self.alert['end']+1})
            request = self.trial.arm(self.alert)
            self.assertEqual(request['requested_alert']['army_id'], '9001')
            self.assertEqual(request['until'], 1060)
            with patch('garrison_trial.TrialGame') as game:
                game.return_value.__enter__.return_value.idle.return_value = 5000
                with patch.object(self.trial, 'try_dispatch') as run:
                    self.trial.check_alerts(request, set())
                    self.assertEqual(run.call_args.args[-1]['army_id'], '9001')
            self.snapshot['alerts'] = [other]
            self.trial.check_alerts(self.trial.snapshot(), set())
        self.assertEqual(self.trial.snapshot()['phase'], 'skipped')
        self.assertEqual(self.trial.snapshot()['enemy_id'], '9001')

    def test_late_web_preview_closes_the_targeted_request(self):
        self.trial.status['requested_alert'] = dict(self.alert)
        self.alert['end'] = 1092
        self.screen.checked_options.return_value = {RECRUIT_OPTION}
        self.run_attempt()
        self.assertEqual(self.trial.snapshot()['phase'], 'skipped')
        self.assertEqual(self.trial.snapshot()['preview_seconds'], 92)
        self.screen.checked_options.assert_not_called()
        self.screen.hit.assert_not_called()

    def test_preview_only_reports_late_arrival_without_touching_options(self):
        self.alert.update(army_id='9001', player_name='Selected', end=1015)
        self.snapshot['server_now'] = 1000
        self.trial.status['phase'] = 'off'
        with (patch('garrison_trial.predict_alerts', return_value={'9001':{'force_assessment':{'kind':'main'}}}),
              patch('garrison_trial.time.time', return_value=1000)):
            self.trial.arm(self.alert, TEAM, preview_only=True)
        self.trial.status.update(phase='reading', troop_codes=[3,3,0])
        self.run_attempt()
        result = self.trial.snapshot()
        self.assertEqual((result['phase'], result['preview_seconds'], result['lead_seconds']), ('previewed', 92, -77))
        self.screen.checked_options.assert_not_called()
        self.screen.toggle_recruit.assert_not_called()
        self.screen.hit.assert_not_called()

    def test_replaced_preview_never_uses_the_replacement_order(self):
        selection = dict(self.trial.status, preview_only=True, armed_at=999)
        self.trial.status['armed_at'] = 1000
        with (patch('garrison_trial.WindowsOcr'), patch('garrison_trial.restore_map'),
              patch('garrison_trial.open_garrison', return_value=(self.screen, 92, 1092))):
            self.trial.try_dispatch(self.game, self.connection, (1011,942), self.alert, selection=selection)
        self.assertEqual(self.trial.snapshot()['phase'], 'reading')
        self.screen.hit.assert_not_called()

    def test_default_web_order_clears_recruit_only_after_time_check(self):
        self.trial.status.update(requested_alert=self.alert, team_slot=5, troop_codes=[3,3,0])
        self.screen.checked_options.return_value = {RECRUIT_OPTION}
        after = MagicMock(captured_at=1000, width=1440)
        after.preview.return_value = (92, 1092)
        after.checked_options.return_value = set()
        with patch('garrison_trial.Screen', return_value=after):
            self.run_attempt()
        self.screen.toggle_recruit.assert_called_once_with(self.game)
        self.screen.hit.assert_not_called()
        after.hit.assert_called_once_with(self.game, 1219, 747)

    def test_blocked_option_preserves_preview_without_submitting(self):
        self.screen.checked_options.return_value = {RECRUIT_OPTION, '行军加速'}
        with self.assertRaisesRegex(RuntimeError, '行军加速'):
            self.run_attempt()
        self.assertEqual(self.trial.snapshot()['preview_seconds'], 92)
        self.screen.toggle_recruit.assert_not_called()
        self.screen.hit.assert_not_called()

    def test_web_team_candidates_and_order_use_the_selected_own_slot(self):
        own = {**self.army,'id':'79000213','position':[1008,947],'from':[1008,947],'target':[1008,947],
               'troops':[{'code':code} for code in (31,22,31)]}
        ally = {**own,'id':'79000214','owner':'55','relation':'ally'}
        stale = {**own,'id':'79000212','stale':True}
        self.snapshot.update(armies=[own, ally, stale], server_now=1000,
                             forts=[{'coord':[1008,947],'name':'fort'}])
        self.snapshot['health']['capture_source'] = 'pc'
        self.assertEqual([(a['id'], a['slot']) for a in available_garrison_teams(self.snapshot)],
                         [('79000213', 3)])
        self.alert.update(army_id='9001', player_name='Selected')
        self.trial.status['phase'] = 'off'
        with (patch('garrison_trial.predict_alerts', return_value={'9001':{'force_assessment':{'kind':'main'}}}),
              patch('garrison_trial.time.time', return_value=1000)):
            with self.assertRaisesRegex(ValueError, '待命状态'):
                self.trial.arm(self.alert, ally['id'])
            self.trial.arm(self.alert, own['id'], allow_recruit=True)
            self.trial.status.update(phase='reading', troop_codes=[31,22,31], source_name='fort')
            with (patch('garrison_trial.WindowsOcr'),
                  patch('garrison_trial.open_garrison', return_value=(self.screen,92,1092)) as preview):
                self.trial.try_dispatch(self.game,self.connection,(1008,947),self.alert)
        self.assertEqual(preview.call_args.kwargs, dict(slot=3,source_name='fort'))
        self.assertEqual(self.trial.snapshot()['team_id'], own['id'])
        self.screen.hit.assert_called_once()

    def test_preview_clock_and_coordinates(self):
        self.assertEqual(read_point('睢阳要塞2(1006,941)'), (1006,941))
        self.assertIsNone(read_point('(1006,941)(1007,941)'))
        self.assertEqual(read_duration('行军时长00:01:32'), 92)
        self.assertEqual(read_arrival('到达时刻2026/11/09 14:20:19'), 1794205219)
