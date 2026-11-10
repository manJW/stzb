import copy
import contextlib
import http.client
import json
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from archive import Archive
from bridge import ReadOnlyBridge
from defense import analyze_alert, availability, garrison_arrival, garrison_plans, historical_cases, replay, rule_evidence, summarize
from defense_model import estimate, validate
from evidence_reader import ReportEvidenceReader
from intelligence import MAIN_DETAIL_PRIORITY, report_references, wilson
from lineup_prediction import player_history, predict, predict_alerts, record_forecast
from server import ThreadingHTTPServer, make_handler


def battle(bid=1, at=300, result=0, *, main=False):
    skills = ''.join(f'{slot},{200000+slot},10,{200010+slot},1,{200020+slot},1;' if main
                     else f'{slot},{200000+slot},10;' for slot in range(1, 7))
    return {"battle_id": bid, "time": at, "wid": 5100610, "attack_name": "enemy", "attack_role_id": "enemy-role",
        "attack_unionid": 1008, "attack_idu": "0,11,12,13,0", "attack_all_hero_info": "100701,40,9000,0,0;100807,40,9000,8000,0;100703,40,9000,8000,0;",
        "defend_name": "ally", "defend_role_id": "ally-role", "defend_unionid": 1909,
        "defend_idu": "0,21,22,23,0", "defend_all_hero_info": "100035,40,9000,8500,0;100707,40,9000,8500,0;100783,40,9000,8500,0;",
        "all_skill_info": skills,
        "npc": 0, "result": result, "attacker_gear_info": "raw-gear-evidence", "weather": 3}


def world(bid=None, aid="9001", uid=200, name="enemy", union=1008, state=1, end=300):
    value = [None] * 34
    value[1] = {str(uid): [name, 0, union] + [0]*9 + [[0,0,"union"]] + [0]*15}
    raw = [0] * 33
    raw[0], raw[1], raw[2], raw[3], raw[4], raw[5] = state, uid, 5000600, 5100610, 200, end
    raw[31] = f"{bid},1,1,1,1,0;" if bid else ""
    value[6] = {aid: raw}
    return value


class IntelligenceTests(unittest.TestCase):
    def setUp(self):
        self.parent = ROOT / "runtime" / "intelligence-tests"
        self.parent.mkdir(exist_ok=True)
        self.folder = Path(tempfile.mkdtemp(prefix="case-", dir=self.parent)).resolve()
        (self.folder / "config.json").write_text(json.dumps({"relations":{"1008":{"relation":"enemy"}}}))
        self.archive = Archive(self.folder)

    def tearDown(self):
        self.archive.close()
        if self.folder.parent != self.parent.resolve() or not self.folder.name.startswith("case-"):
            raise RuntimeError("Unexpected test directory")
        shutil.rmtree(self.folder)

    def test_report_loadout_aligns_attack_and_reverse_defense_slots(self):
        row = battle()
        row.update(attack_advance='0,0,0,0,0,0;0,1,1,0,0,0;3,1,1,0,0,0;5,1,1,0,0,0;',
                   defend_advance='5,1,1,0,0,0;3,1,1,0,0,0;0,1,1,0,0,0;0,0,0,0,0,0;',
                   attacker_gear_info='0,0,0;1048,10,30408;1027,5,0;0,0,0;',
                   defender_gear_info='0,0,0;1027,5,0;1048,10,30408;0,0,0;',
                   attack_hero_type='0,23,11,32', defend_hero_type='32,11,23,0')
        self.archive.ingest(92, [[row]], 301)
        sides = self.archive.intelligence.report('1')['sides']
        self.assertEqual(len(sides), 2)
        for side in sides:
            heroes = side['heroes']
            self.assertEqual([h['red_stars'] for h in heroes], [0, 3, 5])
            self.assertEqual([h['gear']['id'] if h['gear'] else None for h in heroes], [1048, 1027, None])
            self.assertEqual(heroes[0]['gear']['forge_id'], 30408)
            self.assertTrue(heroes[2]['gear_reported'])
            self.assertEqual([(h['troop_code'], h['troop_name']) for h in heroes],
                             [(23, '骑兵'), (11, '弓兵'), (32, '步兵')])

    def test_main_matchup_win_rate_uses_main_cases_and_deduplicates_repeated_rounds(self):
        first = battle(1, 300, 0, main=True)
        first.update(attack_hero_type='0,23,11,32', defend_hero_type='32,11,23,0')
        repeated = {**first, 'battle_id': 2, 'time': 301, 'result': 1}
        draw = {**first, 'battle_id': 3, 'time': 901, 'result': 6, 'attack_hero_type': '0,13,11,32'}
        scout = {**first, 'battle_id': 4, 'time': 1200,
                 'attack_all_hero_info': first['attack_all_hero_info'].replace(',40,', ',39,')}
        self.archive.ingest(92, [[first, repeated, draw, scout]], 50000)
        self.archive.db.execute("UPDATE report_read_jobs SET priority=5 WHERE battle_id='4' AND kind='detail'")
        self.archive.intelligence.seed_alliance_details()
        jobs = self.archive.db.execute("SELECT battle_id,priority FROM report_read_jobs WHERE kind='detail' ORDER BY battle_id")
        self.assertEqual([tuple(j) for j in jobs], [('1', MAIN_DETAIL_PRIORITY), ('2', MAIN_DETAIL_PRIORITY),
                                                    ('3', MAIN_DETAIL_PRIORITY), ('4', 0)])
        result = self.archive.intelligence.query('main-matchups')
        self.assertEqual(result['coverage'], {'reports': 3, 'full_reports': 0, 'samples': 2, 'repeated': 1,
                                              'complete_builds': 0, 'known_troops': 2})
        self.assertEqual(result['total'], 1)
        row = result['items'][0]
        self.assertEqual((row['wins'], row['losses'], row['draws'], row['historical_win_rate']), (1, 0, 1, .5))
        self.assertEqual(row['battle_ids'], ['3', '1'])
        either = self.archive.intelligence.query('main-matchups', main_scope='either')
        self.assertEqual(either['total'], 2)
        self.assertEqual((either['items'][1]['own_main'], either['items'][1]['enemy_main']), (True, False))

    def test_official_campaign_uses_server_event_blocks_time_and_participants(self):
        parties = [1008, 'enemy', 0, 0, 0, 0, 1909, 'ally', 0, 0, 0, 0]
        event = json.dumps({'partake_union':parties, 'belong_block_ids':'51061,52062,'})
        self.archive.ingest(3807, [[139,1,3,5100610,250,350,event]], 300)
        self.archive.ingest(92, [[{**battle(main=True), 'block_id':51061},
            {**battle(2,351), 'block_id':51061}, {**battle(3), 'block_id':51062},
            {**battle(4), 'block_id':51061, 'attack_unionid':1010}]], 400)
        campaigns = self.archive.intelligence.query('official-campaigns')['items']
        self.assertEqual([(c['id'],c['name']) for c in campaigns], [('139','谯之战')])
        result = self.archive.intelligence.query('encounters', official_campaign_id='139', relation='all')
        self.assertEqual({r['battle_id'] for r in result['items']}, {'1'})
        self.assertEqual(self.archive.intelligence.report('1')['campaign']['name'], '谯之战')
        self.assertIsNone(self.archive.intelligence.report('2')['campaign'])
        main = self.archive.intelligence.query('main-matchups', official_campaign_id='139', relation='all')
        self.assertEqual(main['coverage']['reports'], 1)

    def test_combat_analysis_counts_arbitrary_skill_and_hero_matchups(self):
        won = battle(1, 300, 0, main=True)
        lost = battle(2, 1000, 1, main=True)
        lost['attack_all_hero_info'] = lost['attack_all_hero_info'].replace('100701', '100814')
        self.archive.ingest(92, [[won, lost]], 1100)
        result = self.archive.intelligence.query('combat-analysis', analysis_dimension='hero', analysis_subject=100035)
        target = next(r for r in result['items'] if r['id'] == 100701)
        self.assertEqual((target['direction'], target['wins'], target['baseline']['losses']), ('defend', 1, 1))
        skills = self.archive.intelligence.query('combat-analysis', analysis_dimension='skill', analysis_perspective='enemy')
        self.assertEqual({r['id'] for r in skills['items']},
                         {200000 + offset + slot for offset in (0, 10, 20) for slot in (1, 2, 3)})

    def test_incoming_never_borrows_another_players_lineup_or_main_classification(self):
        for bid, name in enumerate(('player one','player one','player two','player three'), 1):
            report = battle(bid, 300+bid, main=True)
            report.update(attack_name=name, attack_role_id=name, attack_hero_type='0,21,32,3,')
            self.archive.ingest(92, [[report]], 350+bid)
        march = {'id':'9003','player_name':'new player','union_id':'1008','troops':[{'code':21},{'code':32},{'code':3}]}
        snapshot = {'server_now':400, 'armies':[march], 'alerts':[{'army_id':'9003'}]}
        result = predict_alerts(self.archive, snapshot)['9003']
        self.assertEqual(result['items'], [])
        self.assertEqual(result['force_assessment'], {'kind':'unknown'})
        self.assertEqual(analyze_alert(self.archive, snapshot, '9003')['hypotheses'], [])

    def test_report_resolves_legacy_unknown_hero_name_by_id(self):
        row = battle()
        row['attack_all_hero_info'] = row['attack_all_hero_info'].replace('100701', '100814')
        self.archive.ingest(92, [[row]], 301)
        with self.archive.db:
            self.archive.db.execute("""UPDATE combat_sides SET heroes=json_set(heroes,'$[0].name','武将#100814')
                WHERE battle_id='1' AND side='attack'""")
        attack = next(side for side in self.archive.intelligence.report('1')['sides'] if side['side'] == 'attack')
        self.assertEqual((attack['heroes'][0]['id'], attack['heroes'][0]['name']), (100814, '刘徽'))

    def test_recent_battles_uses_exact_team_and_report_wounds_with_one_hour_limit(self):
        prior = battle(1, 300)
        prior.update(attack_idu='9001,11,12,13,0', defend_idu='9002,21,22,23,0')
        latest = battle(2, 400)
        latest.update(attack_idu='9001,11,12,13,0', defend_idu='9002,21,22,23,0',
            attack_all_hero_info='100701,40,9000,0,2000;100807,40,9000,8000,100;100703,40,9000,8000,50;')
        self.archive.ingest(92, [[prior, latest]], 401)
        post = world(state=4, end=600)
        post[6]['9001'][2], post[6]['9001'][3], post[6]['9001'][21] = 5100610, 5000600, 403
        self.archive.ingest(5028, post, 402)
        team = self.archive.intelligence.recent_battles('9001', '', now=410)['items']
        self.assertEqual([(r['battle_id'], r['association']) for r in team], [('2', 'direct')])
        attack, defend = team[0]['sides']
        self.assertEqual((attack['remaining_hp'], attack['wounded_hp']), (16000, 2150))
        self.assertNotIn('injury_observation', attack)
        self.assertNotIn('injury_observation', defend)
        target = self.archive.intelligence.recent_battles('', '510,610', now=410)['items']
        self.assertEqual([r['battle_id'] for r in target], ['2', '1'])
        self.assertEqual(self.archive.intelligence.recent_battles('9001', '', now=4001)['items'], [])

    def test_two_hero_spartan_keeps_historical_report_and_map_evidence(self):
        report = battle()
        report.update(attack_all_hero_info='100701,12,100,0,50;100807,12,100,0,50;0,0,0,0,0;',
                      attack_hp=200, attack_hero_type='0,2,2,0,')
        self.archive.ingest(92, [[report]], 301)
        march = {'id':'9001','owner':'200','troops':[{'code':2},{'code':2},{'code':0}],
                 'state_code':1,'begin':550,'end':700,'from':[500,600],'target':[510,610]}
        history = player_history(self.archive.db,self.archive.profile_id,'enemy','1008')
        result = predict(march, history, 600)
        self.assertIsNone(result['force'])
        evidence = result['force_assessment']
        self.assertEqual((evidence['kind'], evidence['confidence'], evidence['battle_id'], evidence['initial_hp'], evidence['event_at']),
                         ('spartan', 'inferred', '1', 200, 300))
        self.assertEqual(evidence['map_hero_count'], 2)
        record_forecast(self.archive.db, self.archive.profile_id, march, result, 600)
        saved = self.archive.db.execute('SELECT data FROM lineup_forecasts').fetchone()
        self.assertEqual(json.loads(saved['data'])['force_assessment'], evidence)
        other = predict({**march, 'player_name':'unseen'}, ([], {}, False), 600)
        self.assertEqual(other['force_assessment'], {'kind':'spartan', 'map_hero_count':2})
        incomplete = predict({**march, 'troops':[{'code':2}]}, ([], {}, False), 600)
        self.assertEqual(incomplete['force_assessment'], {'kind':'unknown'})

    def test_single_hero_map_evidence_overrides_conflicting_historical_force(self):
        previous = battle(1, 400)
        previous.update(attack_idu='9001,11,12,0,0', attack_hero_type='0,2,2,0,',
                        attack_all_hero_info='100701,12,100,0,50;100807,12,100,0,50;', attack_hp=200)
        compatible = battle(2, 450)
        compatible.update(attack_idu='9002,12,0,0,0', attack_hero_type='0,3,0,0,',
                          attack_all_hero_info='100807,12,100,0,50;', attack_hp=100)
        self.archive.ingest(92, [[previous, compatible]], 451)
        march = {'id':'9001','owner':'200','troops':[{'code':3},{'code':0},{'code':0}],
                 'state_code':1,'begin':550,'end':700,'from':[500,600],'target':[510,610]}
        history = player_history(self.archive.db, self.archive.profile_id, 'enemy', '1008')
        result = predict(march, history, 600)
        self.assertTrue(result['last_army_conflict'])
        self.assertIsNone(result['items'][0]['identity'])
        evidence = result['force_assessment']
        self.assertEqual((evidence['kind'], evidence['map_hero_count'], evidence['battle_id']), ('spartan', 1, '2'))

        mixed = dict(compatible, battle_id=3, time=500, attack_idu='9003,13,0,0,0',
                     attack_all_hero_info='100703,30,2500,500,0;', attack_hp=2500)
        self.archive.ingest(92, [[mixed]], 501)
        history = player_history(self.archive.db, self.archive.profile_id, 'enemy', '1008')
        mixed_result = predict(march, history, 600)['force_assessment']
        self.assertEqual((mixed_result['kind'], mixed_result['map_hero_count']), ('spartan', 1))

    def test_full_lineup_spartan_hint_counts_only_known_distinct_player_battles(self):
        def sample(bid, at, npc=0, hp=100, aid=0):
            row = battle(bid, at)
            row.update(npc=npc, attack_idu=f'{aid},11,12,13,0', attack_hero_type='0,2,2,2,',
                       attack_hp=3*hp, attack_all_hero_info=f'100701,12,{hp},0,0;100807,12,{hp},0,0;100703,12,{hp},0,0;')
            return row

        self.archive.ingest(92, [[sample(1, 100), sample(2, 100), sample(3, 300, npc=1, hp=900, aid=9002)]], 301)
        self.archive.ingest(92, [[sample(4, 200)]], 420)
        self.archive.ingest(92, [[sample(5, 350, hp=500)]], 460)
        march = {'id':'9001','owner':'200','troops':[{'code':2}]*3,
                 'state_code':1,'begin':400,'end':500,'from':[500,600],'target':[510,610]}
        history = player_history(self.archive.db, self.archive.profile_id, 'enemy', '1008')
        self.assertNotIn('spartan_hint', predict(march, history, 410)['force_assessment'])
        result = predict(march, history, 450)
        assessment = result['force_assessment']
        self.assertEqual((assessment['kind'], assessment['initial_hp'], assessment['npc']), ('unknown', 2700, 1))
        hint = assessment['spartan_hint']
        self.assertEqual((hint['sample_count'], hint['min_hp'], hint['max_hp']), (2, 300, 300))
        self.assertEqual(hint['reports'][0]['battle_id'], '4')
        self.assertIn(hint['reports'][1]['battle_id'], ('1', '2'))
        self.assertNotIn('spartan_hint', predict(march, history, 470)['force_assessment'])
        self.assertNotIn('spartan_hint', predict({**march, 'id':'9002'}, history, 450)['force_assessment'])
        self.assertEqual(result['lineup_confidence'], 'uncertain')
        record_forecast(self.archive.db, self.archive.profile_id, march, result, 450)
        saved = json.loads(self.archive.db.execute('SELECT data FROM lineup_forecasts').fetchone()['data'])
        self.assertEqual(saved['force_assessment']['spartan_hint'], hint)
        self.assertEqual((saved['lineup_confidence'], saved['items'][0]['initial_hp']), ('uncertain', 2700))

    def test_defense_statistics_deduplicate_and_use_guard_perspective(self):
        for bid, at, result in ((1, 300, 0), (2, 600, 1), (3, 1000, 6)):
            report = battle(bid, at, result, main=True)
            for side in ('attack', 'defend'):
                field = side + '_all_hero_info'
                report[field] = report[field].replace(',9000,', ',5000,').replace(',8000,', ',4000,').replace(',8500,', ',4500,')
            self.archive.ingest(92, [[report]], at + 1)
        cases = historical_cases(self.archive.db, self.archive.profile_id, '1909', 2000)
        stat = summarize(cases)
        self.assertEqual((stat['wins'], stat['losses'], stat['draws']), (1, 0, 1))
        self.assertEqual(stat['historical_win_rate'], .5)
        self.assertFalse(replay(cases)['validated'])

    def test_tactical_counter_detects_chase_and_notes_immunity(self):
        own = [{'skills': [{'id': 200228, 'name': '战必断金'}]}]
        enemy = [{'skills': [{'id': 200208, 'name': '温酒斩将'}, {'id': 200819, 'name': '疾战'}]}]
        evidence = rule_evidence(own, enemy, {'200208': {'type': '追击'}})
        self.assertEqual([row['key'] for row in evidence], ['cowardice'])
        self.assertIn('温酒斩将', evidence[0]['text'])
        self.assertIn('疾战', evidence[0]['text'])

    def test_early_arrival_requires_same_target_and_compatible_troops(self):
        target = [1010, 930]
        army = {'kind': 'garrison_move', 'target': target, 'end': 99, 'troops': [{'code': 21}]}
        self.assertEqual(availability(army, (21,), target, 100), 'arriving')
        self.assertEqual(availability(army, (21,), target, 98), 'late')
        self.assertEqual(availability(army, (21,), [1010, 931], 100), 'observed')
        self.assertEqual(availability(army, (31,), target, 100), 'changed')

    def test_garrison_eta_uses_conservative_same_team_samples_and_strict_deadline(self):
        target = [510, 610]
        army = {'kind':'stay', 'relation':'self', 'position':[505, 610]}
        history = {'attack':[(5, 300), (6, 240), (20, 100)]}
        eta = garrison_arrival(army, target, 220, 100, history)
        self.assertEqual((eta['arrival_at'], eta['speed_samples'], eta['source']), (210, 2, 'history_attack'))
        self.assertIsNone(garrison_arrival(army, target, 210, 100, history))
        self.assertIsNone(garrison_arrival({**army,'relation':'ally'}, target, 220, 100, history))
        moving = {'kind':'garrison_move','target':target,'end':200}
        self.assertEqual(garrison_arrival(moving, target, 201, 100, {})['source'], 'live_march')
        self.assertIsNone(garrison_arrival(moving, target, 200, 100, {}))
        holding = {'kind':'garrison','position':target,'begin':95,'end':95}
        arrival = garrison_arrival(holding, target, 220, 100, {})
        self.assertEqual((arrival['availability'], arrival['guard_since']), ('holding', 95))

    def test_only_main_incoming_gets_card_plan_using_live_reachable_defender(self):
        report = battle(1, 300, main=True)
        report.update(defend_idu='9001,21,22,23,0', defend_hero_type='3,32,21,0')
        self.archive.ingest(92, [[report]], 301)
        army = {'id':'9001','kind':'garrison_move','relation':'ally','stale':False,'player_name':'ally',
                'from':[505,610],'target':[510,610],'begin':500,'end':600,
                'troops':[{'code':21},{'code':32},{'code':3}]}
        incoming = {'army_id':'9100','coord':[510,610],'end':620}
        snapshot = {'server_now':520,'alerts':[incoming,{**incoming,'army_id':'9101'}],
                    'armies':[army],'config':{'state_rules':{}}}
        enemy = self.archive.intelligence.report(1)['sides'][0]['heroes']
        predictions = {'9100':{'force_assessment':{'kind':'main'},'items':[{'battle_id':'1','heroes':enemy}]},
                       '9101':{'force_assessment':{'kind':'spartan'},'items':[]}}
        plans = garrison_plans(self.archive, snapshot, predictions)
        self.assertNotIn('9101', plans)
        plan = plans['9100']
        self.assertEqual((plan['army_id'],plan['arrival_at'],plan['source']), ('9001',600,'live_march'))
        self.assertEqual(plan['matchup']['battle_id'], '1')
        self.assertIsNone(plan['matchup']['predicted_win_probability'])

    def test_similar_matchups_count_support_and_replay_respects_receipt(self):
        matchup = (frozenset((1, 2, 3)), frozenset((4, 5, 6)), (0, 0, 0))
        result = estimate([(matchup, True)] * 12, matchup, .3)
        self.assertGreater(result['probability'], .3)
        self.assertEqual(result['lineup_cases'], 12)
        self.assertFalse(result['strength_only'])
        self.assertEqual(validate(tuple((n, n + 100, matchup, True) for n in range(60)))['samples'], 0)

    def test_enemy_report_reference_links_without_any_self_index(self):
        self.archive.ingest(5028, world(), 200)
        self.archive.ingest(92, [[battle()]], 301)
        self.assertEqual(self.archive.intelligence.query("encounters")["items"][0]["army_id"], None)
        self.archive.ingest(5028, world(bid=1), 302)
        row = self.archive.intelligence.query("encounters")["items"][0]
        self.assertEqual(row["army_id"], "9001")
        self.assertEqual(row["features"]["binding_evidence"]["source"], "world_army_battle_reference")
        self.assertEqual(len(row["march_links"]), 1)
        self.assertEqual(self.archive.query("army_lineups", army_id="9001")["match"], "exact_army_id")
        self.assertEqual(self.archive.db.execute("SELECT COUNT(*) FROM own_report_indices").fetchone()[0], 0)

    def test_incoming_lineup_uses_prior_links_and_rejects_a_troop_change(self):
        troops = [{'code':21},{'code':32},{'code':3}]
        march = {'id':'9001','owner':'200','troops':troops,'state_code':1,'begin':600,'end':700,
                 'from':[500,600],'target':[510,610]}
        scene = world()
        scene[6]['9001'][16] = '2,21;2,32;3,3;'
        self.archive.ingest(5028, scene, 250)
        first = battle(1, 300)
        first.update(attack_idu='9001,11,12,13,0', attack_hero_type='0,21,32,3,')
        self.archive.ingest(92, [[first]], 310)

        changed = battle(2, 500)
        changed.update(attack_idu='9001,21,22,23,0', attack_hero_type='0,3,3,3,',
                       attack_all_hero_info=first['attack_all_hero_info'].replace('100701','100841'))
        self.archive.ingest(92, [[changed]], 520)
        history = player_history(self.archive.db,self.archive.profile_id,'enemy','1008')
        early = predict(march, history, 510)
        self.assertEqual(early['items'][0]['identity'], 'direct')
        self.assertEqual(early['lineup_confidence'], 'same_army')
        self.assertEqual(early['items'][0]['speed']['direct_samples'], 1)
        self.assertAlmostEqual(early['items'][0]['speed']['delta_pct'], 0)
        later = predict(march, history, 650)
        self.assertEqual([i['battle_id'] for i in later['items']], ['1'])
        self.assertIsNone(later['items'][0]['identity'])
        self.assertIsNone(later['same_army_report_id'])
        self.assertTrue(later['last_army_conflict'])
        self.assertEqual(later['lineup_confidence'], 'uncertain')

    def test_newer_exact_arrival_supersedes_an_older_conflicting_direct_report(self):
        scene = world(end=500)
        scene[6]['9001'][16] = '2,21;2,32;3,3;'
        self.archive.ingest(5028, scene, 250)
        old = battle(1, 300)
        old.update(attack_idu='9001,11,12,0,0', attack_hero_type='0,21,32,0,')
        self.archive.ingest(92, [[old]], 310)
        newer = battle(2, 500, main=True)
        newer.update(attack_hero_type='0,21,32,3,')
        self.archive.ingest(92, [[newer]], 510)
        unbound = {**newer, 'battle_id':3, 'time':530}
        self.archive.ingest(92, [[unbound]], 540)
        march = {'id':'9001','owner':'200','troops':[{'code':21},{'code':32},{'code':3}],
                 'state_code':1,'begin':600,'end':700,'from':[500,600],'target':[510,610]}
        history = player_history(self.archive.db,self.archive.profile_id,'enemy','1008')
        early = predict(march, history, 505)
        self.assertEqual(early['force_assessment']['kind'], 'unknown')
        result = predict(march, history, 610)
        self.assertEqual((result['force_assessment']['kind'],result['force_assessment']['source'],
                          result['force_assessment']['battle_id']), ('main','arrival','2'))
        self.assertEqual((result['items'][0]['battle_id'],result['items'][0]['identity']), ('2','arrival'))
        self.assertIsNone(result['same_army_report_id'])
        self.assertFalse(result['last_army_conflict'])

        later_direct = {**old, 'battle_id':4, 'time':550}
        self.archive.ingest(92, [[later_direct]], 560)
        changed = predict(march, player_history(self.archive.db,self.archive.profile_id,'enemy','1008'), 610)
        self.assertTrue(changed['last_army_conflict'])
        self.assertEqual(changed['force_assessment']['kind'], 'unknown')

    def test_latest_same_army_report_stays_first_when_other_armies_share_troop_codes(self):
        cases = ((1, '9001', 100701), (2, '9001', 100814), (3, '9002', 100814), (4, '9003', 100841))
        for bid, army_id, hero_id in cases:
            report = battle(bid, 100 * bid, main=True)
            report.update(attack_idu=f'{army_id},11,12,13,0', attack_hero_type='0,21,32,3,',
                          attack_all_hero_info=report['attack_all_hero_info'].replace('100701', str(hero_id)))
            if bid == 3:
                report['all_skill_info'] = report['all_skill_info'].replace('200011,1', '200011,9', 1)
            self.archive.ingest(92, [[report]], 100 * bid + 1)
        march = {'id':'9001', 'owner':'200', 'troops':[{'code':21},{'code':32},{'code':3}], 'begin':500, 'end':600}
        history = player_history(self.archive.db, self.archive.profile_id, 'enemy', '1008')
        result = predict(march, history, 550)
        self.assertEqual(result['same_army_report_id'], '2')
        self.assertEqual(result['force']['battle_id'], '2')
        self.assertEqual([(item['battle_id'], item['report_army_id'], item['identity']) for item in result['items']],
                         [('2', '9001', 'direct'), ('4', '9003', None), ('3', '9002', None)])
        self.assertEqual(result['items'][0]['heroes'][0]['skills'][1]['level'], 1)
        self.assertEqual(result['items'][2]['heroes'][0]['skills'][1]['level'], 9)

    def test_incoming_keeps_only_the_players_available_compatible_reports(self):
        for bid,name,union,code in ((1,'enemy',1008,'0,21,32,3,'),(2,'other',1010,'0,21,32,3,'),
                                    (3,'enemy',1008,'0,32,21,3,'),(4,'enemy',1008,'0,21,32,3,')):
            report = battle(bid, 300+bid)
            report.update(attack_name=name, attack_role_id=name, attack_unionid=union, attack_hero_type=code,
                          attack_all_hero_info=report['attack_all_hero_info'].replace('100701',str(100700+bid)))
            self.archive.ingest(92, [[report]], 400 if bid==4 else 350+bid)
        march = {'id':'9003','player_name':'enemy','union_id':'1008','troops':[{'code':21},{'code':32},{'code':3}]}
        history = player_history(self.archive.db,self.archive.profile_id,'enemy','1008')
        result = predict(march, history, 380)
        self.assertEqual([i['battle_id'] for i in result['items']], ['1'])
        later = predict(march, history, 401)
        self.assertEqual([i['battle_id'] for i in later['items']], ['4','1'])

    def test_two_cavalry_armies_of_one_player_keep_their_own_report_evidence(self):
        other_army = battle(1, 100, main=True)
        other_army.update(attack_idu='9001,11,12,13,0', attack_hero_type='0,23,53,13,')
        self.archive.ingest(92, [[other_army]], 101)
        scene = world(aid='9002')
        scene[6]['9002'][16] = '3,23;3,23;3,23;'
        self.archive.ingest(5028, scene, 250)
        march = {'id':'9002','owner':'200','state_code':1,'begin':200,'end':300,'troops':[{'code':23}]*3}
        history = player_history(self.archive.db, self.archive.profile_id, 'enemy', '1008')
        before = predict(march, history, 260)
        self.assertEqual(before['items'], [])
        self.assertEqual(before['force_assessment'], {'kind':'unknown'})

        actual = battle(2, 300, main=True)
        actual.update(attack_hero_type='0,23,23,23,',
                      attack_all_hero_info=actual['attack_all_hero_info'].replace('100701', '100498'))
        self.archive.ingest(92, [[actual]], 304)
        with self.archive.db:
            self.archive.db.execute("UPDATE march_samples SET straight_grids_per_hour=0 WHERE army_id='9002'")
        history = player_history(self.archive.db, self.archive.profile_id, 'enemy', '1008')
        later = predict({**march,'begin':400,'end':500}, history, 420)
        self.assertEqual((later['force_assessment']['kind'], later['force_assessment']['source']), ('main', 'arrival'))
        self.assertIsNone(later['same_army_report_id'])
        self.assertEqual([(item['battle_id'], item['inferred_report_army_id'], item['identity']) for item in later['items']],
                         [('2', '9002', 'arrival')])
        different_army = predict({**march,'id':'9003','begin':400,'end':500}, history, 420)
        self.assertEqual(different_army['items'][0]['inferred_report_army_id'], '9002')
        self.assertIsNone(different_army['items'][0]['identity'])

    def test_incoming_force_uses_latest_complete_prebattle_strength(self):
        march = {'id':'9001','owner':'200','troops':[{'code':21},{'code':32},{'code':3}],
                 'state_code':1,'begin':1000,'end':1200,'from':[500,600],'target':[510,610]}
        samples = (
            (1,100,[40,40,40],5000,3,'main'),
            (2,200,[40,40,39],5001,3,'standard'),
            (3,300,[40,40,40],5000,3,'standard'),
            (4,400,[40,40,40],100,3,'low'),
            (5,500,[40,40],100,2,None),
        )
        for bid,at,levels,health,slots,expected in samples:
            report = battle(bid, at, main=bid == 1)
            heroes = [100701,100807,100703][:slots]
            counts = [health]*slots
            if bid==3:
                counts[-1]-=1
            report.update(attack_idu='9001,11,12,13,0',attack_hero_type='0,21,32,3,' if slots==3 else '0,21,32,0,',
                          attack_hp=sum(counts),attack_all_hero_info=';'.join(
                              f'{hero},{level},{hp},50,0' for hero,level,hp in zip(heroes,levels,counts)))
            self.archive.ingest(92, [[report]], at+1)
            history = player_history(self.archive.db,self.archive.profile_id,'enemy','1008')
            prediction = predict(march, history, at+2)
            force = prediction['force']
            self.assertEqual(force['priority'] if force else None,expected)
            self.assertEqual(prediction['force_assessment']['kind'], {'main':'main','low':'spartan'}.get(expected, 'unknown'))
            if force:
                self.assertEqual(force['initial_hp'],sum(counts))
                self.assertEqual(force['remaining_hp'],150)
            if bid<=3:
                inferred = predict({**march, 'id':'9002'}, history, at+2)['force_assessment']
                self.assertEqual(inferred['kind'], 'main' if bid==1 else 'unknown')
            if bid==4:
                self.assertEqual(predict(march, history, at+3601)['force']['priority'],'standard')

    def test_main_requires_nine_skills_but_ignores_troops_and_skill_levels(self):
        march = {'id':'9001', 'owner':'200', 'player_name':'enemy', 'union_id':'1008',
                 'troops':[{'code':21}, {'code':32}, {'code':3}]}
        skill_rows = ['1,200001,1,200010,1,200011,1', '2,200002,1,200012,1,200013,1',
                      '3,200003,1,200014,1,200015,1']
        cases = [('missing_skill', 9000), ('level_39', 9000), ('qualified', 100), ('unverified_hp', 4000)]
        for bid, (variant, hp) in enumerate(cases, 1):
            at = 1000 * bid
            report = battle(bid, at, main=True)
            skills = skill_rows.copy()
            if variant == 'missing_skill':
                skills[0] = '1,200001,10,200010,10'
            report.update(attack_idu='9001,11,12,13,0', attack_hero_type='0,21,32,3,',
                          attack_hp=3 * hp - (variant == 'unverified_hp'),
                          attack_all_hero_info=report['attack_all_hero_info'].replace(',9000,', f',{hp},'),
                          all_skill_info=';'.join(skills) + ';' + report['all_skill_info'].split(';', 3)[3])
            if variant == 'level_39':
                report['attack_all_hero_info'] = report['attack_all_hero_info'].replace(',40,', ',39,', 1)
            self.archive.ingest(92, [[report]], at + 1)
            history = player_history(self.archive.db, self.archive.profile_id, 'enemy', '1008')
            direct = predict(march, history, at + 2)['force_assessment']
            inferred = predict({**march, 'id':'9002'}, history, at + 2)['force_assessment']
            if variant in ('qualified', 'unverified_hp'):
                self.assertEqual((direct['kind'], direct['confidence'], direct['main_basis']), ('main', 'linked', 'skills'))
                self.assertEqual(direct['levels'], [40, 40, 40])
                self.assertEqual(direct['initial_hp'], None if variant == 'unverified_hp' else 3 * hp)
                self.assertEqual((inferred['kind'], inferred['confidence']), ('main', 'inferred'))
            else:
                self.assertEqual((direct['kind'], inferred['kind']), ('unknown', 'unknown'))
        stats = self.archive.intelligence.query('main-matchups')
        self.assertEqual(stats['coverage']['reports'], 2)
        self.assertEqual([row['battle_id'] for row in historical_cases(self.archive.db, self.archive.profile_id, '1909', 6000)],
                         ['3', '4'])

    def test_report_review_uses_saved_forecast_for_exact_march(self):
        self.archive.ingest(5028, world(), 250)
        march = {'id':'9001','state_code':1,'from':[500,600],'target':[510,610],'begin':200,'end':300}
        actual = {'hero_key':'100701,100807,100703','heroes':[{'name':'actual'}], 'battle_id':'10','identity':None}
        alternative = {**actual,'hero_key':'100841,100807,100703','heroes':[{'name':'alternative'}]}
        with self.archive.db:
            for at, items in ((251,[alternative,actual]),(252,[alternative,actual]),(253,[actual,alternative]),(301,[])):
                record_forecast(self.archive.db,self.archive.profile_id,march,
                    {'items':items,'troop_codes':[1,2,3],'ambiguous':False},at)
            record_forecast(self.archive.db,self.archive.profile_id,{**march,'target':[511,610]},
                {'items':[alternative],'troop_codes':[1,2,3],'ambiguous':False},255)
        self.archive.ingest(92, [[battle()]], 302)
        review = self.archive.intelligence.report(1)['lineup_review']
        self.assertEqual((review['status'],review['method']), ('matched','unique_attack_arrival'))
        self.assertEqual((review['first']['issued_at'],review['first']['rank']), (251,2))
        self.assertEqual((review['last']['issued_at'],review['last']['rank']), (253,1))
        job = self.archive.db.execute("SELECT priority FROM report_read_jobs WHERE battle_id='1' AND kind='detail'").fetchone()
        self.assertEqual(job[0],60)
        partial = battle(2)
        partial['attack_all_hero_info'] = ';'.join(partial['attack_all_hero_info'].split(';')[:2])
        self.archive.ingest(92, [[partial]], 303)
        review = self.archive.intelligence.report(2)['lineup_review']
        self.assertEqual((review['status'],review['comparison_battle_id']),('partial_report','1'))
        self.assertEqual(self.archive.db.execute('SELECT COUNT(*) FROM lineup_forecasts').fetchone()[0],3)

    def test_same_army_loadout_audit_uses_saved_prebattle_fields_and_drops_conflicting_links(self):
        scene = world()
        scene[6]['9001'][16] = '2,21;2,32;3,3;'
        self.archive.ingest(5028, scene, 250)
        before = battle(1, 100, main=True)
        before.update(attack_idu='9001,11,12,13,0', attack_hero_type='0,21,32,3,',
                      attack_advance='0,0,0,0,0,0;0,0,0,0,0,0;2,0,0,0,0,0;5,0,0,0,0,0;')
        self.archive.ingest(92, [[before]], 251)
        march = {'id':'9001','owner':'200','state_code':1,'from':[500,600],'target':[510,610],
                 'troops':[{'code':21},{'code':32},{'code':3}],'begin':200,'end':300}
        history = player_history(self.archive.db, self.archive.profile_id, 'enemy', '1008')
        predicted = predict(march, history, 260)
        self.assertEqual(predicted['same_army_report_id'], '1')
        with self.archive.db:
            record_forecast(self.archive.db, self.archive.profile_id, march, predicted, 260)
        actual = {**before,'battle_id':2,'time':300,
                  'attack_advance':before['attack_advance'].replace(';2,', ';3,'),
                  'all_skill_info':before['all_skill_info'].replace('200011,1','200011,9').replace('200012,1','200099,1')
                      .replace('200023,1','0,0')}
        self.archive.ingest(92, [[actual]], 301)
        audit = self.archive.intelligence.report(2)['lineup_review']['same_army_audit']
        self.assertEqual((audit['army_id'], audit['method'], audit['last']['source_battle_id']),
                         ('9001', 'coordinate_time_match', '1'))
        comparison = audit['last']
        self.assertEqual(comparison['fields'], {'hero':'match','red_stars':'mismatch','skills':'mismatch','skill_levels':'mismatch'})
        self.assertEqual(comparison['positions'][0]['red_stars'], {'status':'match','predicted':0,'actual':0})
        self.assertEqual(comparison['positions'][2]['skills']['status'], 'unavailable')
        saved = self.archive.db.execute("SELECT data FROM same_army_audits WHERE battle_id='2'").fetchone()
        self.assertEqual(json.loads(saved['data']), audit)
        conflicting = {**actual,'battle_id':3,'attack_all_hero_info':actual['attack_all_hero_info'].replace('100701','100814')}
        self.archive.ingest(92, [[conflicting]], 302)
        self.assertEqual(self.archive.intelligence.report(2)['lineup_review']['status'], 'conflicting_reports')
        self.assertEqual(self.archive.db.execute('SELECT COUNT(*) FROM same_army_audits').fetchone()[0], 0)

    def test_brief_is_available_separately_and_can_be_backfilled(self):
        self.archive.ingest(92, [[battle()]], 300)
        self.archive.ingest(6231, [0, {'battle_id':1,'brief_report':'brief'}, 0], 310)
        with self.archive.db:
            self.archive.db.execute('DELETE FROM report_briefs')
        self.archive.backfill_report_briefs()
        self.archive.backfill_report_briefs()
        report = self.archive.intelligence.report(1)
        self.assertIsNone(report['full_report'])
        self.assertEqual((report['brief_report']['body'],report['brief_report']['observed_at']),('brief',310))
        self.assertEqual(self.archive.summary()['counts']['report_briefs'],1)

    def test_ambiguous_explicit_reference_retracts_current_claim(self):
        self.archive.ingest(92, [[battle()]], 301)
        self.archive.ingest(5028, world(bid=1), 302)
        self.assertEqual(self.archive.intelligence.query("encounters")["items"][0]["army_id"], "9001")
        self.archive.ingest(5028, world(bid=1, aid="9002"), 303)
        self.assertIsNone(self.archive.intelligence.query("encounters")["items"][0]["army_id"])
        self.assertEqual(self.archive.summary()["counts"]["bound_teams"], 0)

    def test_name_or_union_mismatch_never_links(self):
        self.archive.ingest(92, [[battle()]], 301)
        self.archive.ingest(5028, world(bid=1, union=1007), 302)
        self.assertIsNone(self.archive.intelligence.query("encounters")["items"][0]["army_id"])
        self.archive.ingest(5028, world(bid=1, name="another-enemy"), 303)
        self.assertIsNone(self.archive.intelligence.query("encounters")["items"][0]["army_id"])

    def test_arrival_inference_is_searchable_and_replaced_by_direct_evidence(self):
        self.archive.ingest(92, [[battle()]], 301)
        self.archive.ingest(5028, world(), 250)
        intel = self.archive.intelligence
        row = intel.query("encounters", army_id="9001")["items"][0]
        self.assertIsNone(row["army_id"])
        self.assertEqual(row["inferred_army_id"], "9001")
        self.assertEqual(row["march_links"][0]["method"], "unique_attack_arrival")
        self.assertEqual(row["march_links"][0]["duration_seconds"], 100)
        self.assertEqual(intel.report(1)["sides"][0]["inferred_army_id"], "9001")
        recent = intel.recent_battles("9001", "", now=303)["items"]
        self.assertEqual([(r["battle_id"],r["association"]) for r in recent], [("1", "arrival")])
        self.assertEqual(intel.query("summary")["observed_inferred_only_armies"], 1)
        self.assertEqual(self.archive.summary()["counts"]["bound_teams"], 0)
        self.archive.ingest(5028, world(bid=1, aid="9002"), 302)
        row = intel.query("encounters")["items"][0]
        self.assertEqual(row["army_id"], "9002")
        self.assertIsNone(row["inferred_army_id"])

    def test_arrival_inference_requires_unique_prearrival_attack_with_same_union(self):
        self.archive.ingest(5028, world(state=4), 250)
        self.archive.ingest(5028, world(aid="9002", end=301), 250)
        self.archive.ingest(5028, world(aid="9003", uid=201, union=1007), 250)
        self.archive.ingest(5028, world(aid="9004"), 301)
        self.archive.ingest(92, [[battle()]], 302)
        intel = self.archive.intelligence
        self.assertIsNone(intel.query("encounters")["items"][0]["inferred_army_id"])
        self.archive.ingest(5028, world(aid="9005"), 250)
        self.assertEqual(intel.query("encounters")["items"][0]["inferred_army_id"], "9005")
        self.archive.ingest(5028, world(aid="9006"), 251)
        self.assertIsNone(intel.query("encounters")["items"][0]["inferred_army_id"])

    def test_details_seed_existing_and_new_alliance_reports_without_an_army(self):
        self.archive.ingest(92, [[battle(1), battle(2)]], 301)
        unrelated = battle(3)
        unrelated["defend_unionid"] = 1100
        self.archive.ingest(92, [[unrelated]], 301)
        self.archive.ingest(6231, {"battle_id": 2, "report": "full"}, 302)
        with self.archive.db:
            self.archive.db.execute("DELETE FROM report_read_jobs")
        reader = ReportEvidenceReader(self.archive, SimpleNamespace(), lambda: None, False)
        try:
            self.assertEqual(reader.snapshot()["alliance_reports"], {"reports": 2, "full_reports": 1})
            self.archive.ingest(92, [[battle(4)]], 303)
            self.assertEqual([r[0] for r in self.archive.db.execute("""SELECT battle_id FROM report_read_jobs
                WHERE kind='detail' ORDER BY battle_id""")], ["1", "4"])
            self.assertEqual(self.archive.intelligence.seed_alliance_details(), 0)
        finally:
            reader.close()

    def test_report_first_or_reference_first_both_work_and_queue_only_seen_ids(self):
        self.archive.ingest(5028, world(bid=1), 200)
        jobs = self.archive.db.execute("SELECT kind,battle_id FROM report_read_jobs").fetchall()
        self.assertEqual([tuple(r) for r in jobs], [("profile", "1")])
        self.archive.ingest(10, [0,[battle()]], 301)
        self.assertEqual(self.archive.intelligence.query("encounters")["items"][0]["army_id"], "9001")
        with self.assertRaises(ValueError):
            self.archive.intelligence.queue_evidence("profile", 999)
        self.assertTrue(any(r[0]=="detail" for r in self.archive.db.execute("SELECT kind FROM report_read_jobs")))

    def test_march_matching_rejects_other_destination_time_and_retreat(self):
        for aid,state,end,target in (("9001",3,300,5100610),("9002",1,400,5100610),("9003",1,300,5200610)):
            w=world(bid=1,aid=aid,state=state,end=end);w[6][aid][3]=target
            self.archive.ingest(5028,w,200)
            # Different owners avoid deliberately conflicting army/battle references.
            self.archive.db.execute("DELETE FROM army_report_refs WHERE army_id<>?",(aid,));self.archive.db.commit()
            self.archive.ingest(10,[0,[battle()]],301)
            row=self.archive.intelligence.query("encounters")["items"][0]
            self.assertEqual(row["army_id"],aid)
            self.assertEqual(row["march_links"],[])

    def test_campaign_filters_queries_not_storage_and_persists(self):
        self.archive.ingest(92,[[battle()]],301)
        other=battle(2,500);other["wid"]=9990888
        self.archive.ingest(92,[[other]],501)
        campaign=self.archive.intelligence.save_campaign({"name":"sample","region":"500,600,520,620","from_at":250,"to_at":350})
        result=self.archive.intelligence.query("encounters",campaign_id=campaign["id"])
        self.assertEqual(result["total"],1)
        self.assertEqual(self.archive.summary()["counts"]["battle_reports"],2)
        with self.assertRaises(ValueError):self.archive.intelligence.query("encounters",region="999,600,500,620")
        other_profile=Archive(self.folder,{**self.archive.profile,"season":"another"})
        try:
            self.assertEqual(other_profile.intelligence.query("campaigns")["items"],[])
        finally:other_profile.close()

    def test_late_full_report_is_persisted_and_features_keep_raw_equipment(self):
        self.archive.ingest(92,[[battle()]],301)
        self.archive.ingest(6231,{"battle_id":1,"report":"original-full-text"},310)
        report=self.archive.intelligence.report(1)
        self.assertEqual(report["full_report"]["body"],"original-full-text")
        side=next(r for r in report["sides"] if r["side"]=="attack")
        self.assertEqual(side["features"]["attacker_gear_info"],"raw-gear-evidence")
        self.assertEqual(side["heroes"][0]["skills"][0]["id"],200001)

    def test_client_result_two_is_occupied_win_and_six_is_draw(self):
        self.archive.ingest(92,[[battle(1,300,2)],[battle(2,400,6)]],500)
        rows=self.archive.intelligence.query("encounters",relation="all")["items"]
        outcomes={(r["battle_id"],r["side"]):r["outcome"] for r in rows}
        self.assertEqual(outcomes[("1","attack")],"win")
        self.assertEqual(outcomes[("1","defend")],"loss")
        self.assertEqual(outcomes[("2","attack")],"draw")
        self.assertEqual(outcomes[("2","defend")],"draw")

    def test_statistics_count_once_exclude_npc_scouts_and_do_not_fake_prediction(self):
        valid=battle();npc=battle(2);npc["npc"]=1
        scout=battle(3);scout["attack_all_hero_info"]="100701,40,100,0;100807,40,100,0;100703,40,100,0;"
        self.archive.ingest(92,[[valid],[npc],[scout]],301)
        stats=self.archive.intelligence.query("matchups")
        self.assertEqual(stats["total"],1)
        self.assertEqual(stats["items"][0]["samples"],1)
        self.assertEqual(stats["items"][0]["wins"],1) # Our alliance defended successfully.
        self.assertIsNone(stats["items"][0]["predicted_win_probability"])
        self.assertEqual(stats["items"][0]["sample_status"],"insufficient")
        self.assertEqual(stats["excluded"]["scouting_or_low_troops"],1)

    def test_personnel_includes_world_only_names_and_preserves_identity_namespaces(self):
        self.archive.ingest(5028,world(),200)
        self.archive.ingest(5028,world(aid="9002",uid=201,name="world-only"),201)
        self.archive.ingest(92,[[battle()]],301)
        people=self.archive.intelligence.query("personnel")["items"]
        enemy=next(r for r in people if r["player_name"]=="enemy")
        self.assertEqual(set(enemy["identity_keys"]),{"world:200","battle:enemy-role"})
        self.assertEqual(len(people),2)
        self.assertFalse(self.archive.intelligence.query("summary")["all_members_verified"])

    def test_replaying_linked_and_unresolved_history_does_not_recreate_projections(self):
        self.archive.ingest(5028,world(),200)
        self.archive.ingest(92,[[battle(1,250)],[battle(2,300)]],301)
        self.archive.ingest(5028,world(bid=2),302)
        before=self.archive.summary()["counts"]
        with self.archive.lock,self.archive.db:
            for item in [battle(1,250),battle(2,300)]:self.archive._battle(item,301,"same-source",emit_change=False)
        after=self.archive.summary()["counts"]
        self.assertEqual(before,after)

    def test_historical_missing_skill_uses_prior_evidence_not_future(self):
        old=battle(1,100);old["attack_idu"]="9001,11,12,13,0"
        future=battle(3,300);future["attack_idu"]=old["attack_idu"];future["all_skill_info"]="1,299999,10;"
        middle=battle(2,200);middle["attack_idu"]=old["attack_idu"];middle.pop("all_skill_info")
        for b in [old,future,middle]:self.archive.ingest(10,[0,[b]],400)
        history=self.archive.query("team_history",team_key="army:9001")["items"]
        m=next(r for r in history if r["battle_id"]=="2")
        self.assertEqual(m["data"]["heroes"][0]["skills"][0]["id"],200001)
        self.assertEqual(m["data"]["heroes"][0]["field_times"]["skills"],100)

    def test_reader_requires_received_data_and_respects_budget_and_pause(self):
        self.archive.ingest(5028,world(bid=1),200)
        calls=[]
        def profiles(ids):
            calls.append(("profile",ids));self.archive.ingest(10,[0,[battle(x) for x in ids]],301)
        def detail(bid):
            calls.append(("detail",bid));self.archive.ingest(6231,{"battle_id":bid,"report":"full"},302)
        reader=ReportEvidenceReader(self.archive,SimpleNamespace(fetch_profiles=profiles,fetch_battle_detail=detail),lambda:None,False)
        reader.resume(2);reader._step();self.assertEqual(len(calls),1)
        reader._step();self.assertEqual(len(calls),1)
        reader.not_before=0;reader._step();reader.not_before=0;reader._step()
        self.assertFalse(reader.snapshot()["active"])
        self.assertEqual(calls,[("profile",[1]),("detail",1)])
        self.assertEqual(self.archive.intelligence.report(1)["full_report"]["body"],"full")
        with self.assertRaises(ValueError):reader.enqueue([999])
        reader.close()

    def test_ack_without_saved_data_cannot_complete_reader_job(self):
        self.archive.ingest(5028,world(bid=1),200)
        reader=ReportEvidenceReader(self.archive,SimpleNamespace(fetch_profiles=lambda ids:None),lambda:None,False)
        reader.RESPONSE_TIMEOUT=0
        reader.resume(1);reader._step()
        job=self.archive.db.execute("SELECT state FROM report_read_jobs WHERE kind='profile'").fetchone()[0]
        self.assertEqual(job,"failed")
        self.assertEqual(self.archive.summary()["counts"]["battle_reports"],0)
        reader.close()

    def test_failed_profile_batches_do_not_starve_known_or_manually_requested_details(self):
        self.archive.ingest(5028,world(bid=1),200)
        self.archive.ingest(92,[[battle(2)]],301)
        self.archive.ingest(5028,world(bid=2,aid="9002",uid=201,name="another-enemy"),302)
        with self.archive.lock,self.archive.db:
            self.archive.intelligence.queue_evidence("detail",2,retry=True)
        calls=[]
        def detail(bid):
            calls.append(bid);self.archive.ingest(6231,{"battle_id":bid,"report":"full"},303)
        reader=ReportEvidenceReader(self.archive,SimpleNamespace(fetch_profiles=lambda ids:None,fetch_battle_detail=detail),lambda:None,False)
        reader.resume(3);reader._step()
        self.assertEqual(calls,[2])
        reader.close()

    def test_bridge_read_allowlist_and_reference_shape(self):
        sent=[]
        def exchange(text):
            sent.append(text);return "OK:sent cmd="+text.split()[1]
        b=ReadOnlyBridge(exchange);b.fetch_profiles([1,2]);b.fetch_battle_detail(1)
        self.assertEqual(sent,["__net_send 10 [[1,2],0,0]","__net_send 6231 [1,1,0]"])
        for ids in ([True],[],list(range(1,17))):
            with self.assertRaises(ValueError):b.fetch_profiles(ids)
        raw=[0]*33;raw[31]="1,1,1,1,1,0;malformed;2,-1,1,1,1,0;"
        self.assertEqual(report_references(raw),[[1,1,1,1,1,0]])
        lo,hi=wilson(0,30);self.assertEqual(lo,0);self.assertLess(hi,.12)

    def test_continuous_reader_waits_for_new_data_and_cools_down_without_stopping(self):
        reader=ReportEvidenceReader(self.archive,SimpleNamespace(),lambda:None,False)
        reader.resume(1,continuous=True);reader._step()
        self.assertTrue(reader.snapshot()["active"])
        reader.requests=1;reader._step()
        self.assertTrue(reader.snapshot()["continuous"])
        self.assertEqual(reader.snapshot()["requests"],0)
        self.assertGreater(reader.not_before,time.monotonic()+59)
        reader.pause();self.assertFalse(reader.snapshot()["continuous"]);reader.close()

    def test_unavailable_profile_batches_defer_only_profiles_and_do_not_block_full_reports(self):
        w=world();w[6]["9001"][31]=";".join(f"{bid},1,1,1,1,0" for bid in range(100,131))+";"
        self.archive.ingest(5028,w,200);self.archive.ingest(92,[[battle(2)]],301)
        with self.archive.lock,self.archive.db:self.archive.intelligence.queue_evidence("detail",2)
        saved=[]
        def detail(bid):saved.append(bid);self.archive.ingest(6231,{"battle_id":bid,"report":"full"},302)
        reader=ReportEvidenceReader(self.archive,SimpleNamespace(fetch_profiles=lambda ids:None,fetch_battle_detail=detail),lambda:None,False)
        reader.RESPONSE_TIMEOUT=0;reader.resume(5,continuous=True)
        for _ in range(3):reader.not_before=0;reader._step()
        self.assertTrue(reader.snapshot()["profile_temporarily_blocked"])
        self.assertEqual(saved,[2]);self.assertTrue(reader.snapshot()["active"]);reader.close()

    @contextlib.contextmanager
    def http_service(self,reader=None):
        service=ThreadingHTTPServer(("127.0.0.1",0),make_handler(None,0))
        port=service.server_address[1]
        service.RequestHandlerClass=make_handler(None,port,archive=self.archive,evidence_reader=reader)
        thread=threading.Thread(target=service.serve_forever,daemon=True);thread.start()
        def request(method,path,payload=None,origin=True):
            headers={"Content-Type":"application/json"}
            if origin:headers["Origin"]=f"http://127.0.0.1:{port}"
            connection=http.client.HTTPConnection("127.0.0.1",port)
            connection.request(method,path,json.dumps(payload) if payload is not None else None,headers)
            reply=connection.getresponse();status,body=reply.status,reply.read();connection.close()
            return status,body
        try:yield request
        finally:service.shutdown();service.server_close();thread.join()

    def test_http_campaign_and_queries_validate_scope_and_origin(self):
        self.archive.ingest(92,[[battle()]],301)
        with self.http_service() as request:
            self.assertEqual(request("GET","/api/intel/encounters?region=invalid")[0],400)
            status,body=request("GET","/api/intel/personnel");self.assertEqual(status,200)
            self.assertEqual(json.loads(body)["items"][0]["player_name"],"enemy")
            self.assertEqual(request("POST","/api/intel/campaigns",{"name":"sample"},False)[0],403)
            status,body=request("POST","/api/intel/campaigns",{"name":"sample","region":"500,600,520,620"})
            self.assertEqual(status,200)
            cid=json.loads(body)["id"]
            status,body=request("GET","/api/intel/encounters?campaign_id="+cid)
            self.assertEqual(json.loads(body)["total"],1)

    def test_http_export_uses_fixed_generated_path_and_read_jobs_reject_unknown_ids(self):
        self.archive.ingest(92,[[battle()]],301)
        reader=ReportEvidenceReader(self.archive,SimpleNamespace(),lambda:None,False)
        with self.http_service(reader) as request:
            self.assertEqual(request("POST","/api/intel/report/read",{"battle_ids":[999]})[0],400)
            self.assertEqual(request("POST","/api/intel/report/read",{"battle_ids":[1]},False)[0],403)
            self.assertEqual(request("POST","/api/intel/send",{"command":999})[0],404)
            with patch("intelligence.write_json") as save:
                status,body=request("POST","/api/intel/export",{"kind":"encounters","target_path":r"C:\unrelated.json"})
                self.assertEqual(status,200)
                target,payload=save.call_args.args
                self.assertEqual(target.parent,self.folder.parent/"Exports")
                self.assertEqual(payload["items"][0]["battle_id"],"1")
        reader.close()


if __name__=="__main__":unittest.main()
