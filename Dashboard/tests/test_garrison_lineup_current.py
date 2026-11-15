"""Regression: a verified new march must replace a stale army roster anchor."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_support import Archive


ARMY = '113709493'


def report(bid, at, *, old=False, side='attack', army='0', cards=None):
    ids = (100814, 100035, 100783) if old else (100474, 100814, 100783)
    cards = cards or ('11,12,13' if old else '21,11,13')
    stars = ['0,0,0,0,0,0', '5,0,0,0,0,0', '3,0,0,0,0,0', '4,0,0,0,0,0']
    if side == 'defend':
        stars.reverse()
    slots = (1, 2, 3) if side == 'attack' else (6, 5, 4)
    return {'battle_id':bid, 'time':at, 'wid':9820923, 'npc':0,
            side+'_name':'player', side+'_role_id':'player-role', side+'_unionid':1009,
            side+'_idu':f'{army},{cards},0',
            side+'_all_hero_info':';'.join(f'{ident},43,9000,8000,0' for ident in ids)+';',
            side+'_hero_type':('0,11,31,31,' if old else '0,21,11,31,') if side == 'attack' else '31,11,21,0,',
            side+'_advance':';'.join(stars)+';',
            'all_skill_info':''.join(f'{slot},{200001+i},10,{200011+i},8,{200021+i},7;'
                                     for i, slot in enumerate(slots))}


class GarrisonCurrentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.archive = Archive(Path(self.temp.name))
        self.observe(at=220)
        self.archive.ingest(92, [[report(1, 100, old=True, army=ARMY)]], 110)

    def tearDown(self):
        self.archive.close()
        self.temp.cleanup()

    def observe(self, at=430, *, army=ARMY, state=1, end=300, target=9820923):
        scene = [None]*34
        scene[1] = {'12276':['player', 0, 1009]+[0]*9+[[0, 0, 'alliance']]}
        raw = [0]*33
        raw[0:6] = [state, 12276, 9770916, target, 200, end]
        raw[16] = '2,21;1,11;2,31;'
        scene[6] = {army:raw}
        self.archive.ingest(5028, scene, at)

    def query(self, at=700):
        with patch('archive.time.time', return_value=at):
            return self.archive.query('army_lineups', army_id=ARMY)

    def test_new_arrival_reanchors_changed_heroes_and_latest_defense_supplies_all_fields(self):
        self.archive.ingest(92, [[report(2, 300)]], 310)
        self.archive.ingest(92, [[report(3, 400, side='defend')]], 410)
        self.observe(state=6, end=430, target=9800910)
        before = self.archive.db.execute('SELECT COUNT(*) FROM team_versions').fetchone()[0]
        answer = self.query()
        self.assertEqual(answer['match'], 'same_card_instances')
        self.assertEqual(answer['current_report_id'], '3')
        self.assertEqual(answer['current_resolution']['anchor_battle_id'], '2')
        self.assertEqual(answer['current_resolution']['anchor_source'], 'unique_attack_arrival')
        heroes = answer['items'][0]['data']['heroes']
        self.assertEqual([h['id'] for h in heroes], [100474, 100814, 100783])
        self.assertEqual([h['red_stars'] for h in heroes], [5, 3, 4])
        self.assertEqual([h['troop_code'] for h in heroes], [21, 11, 31])
        self.assertEqual([[s['id'] for s in h['skills']] for h in heroes],
                         [[200001+i, 200011+i, 200021+i] for i in range(3)])
        self.assertEqual(self.archive.db.execute('SELECT COUNT(*) FROM team_versions').fetchone()[0], before)
        self.assertEqual(self.archive.db.execute("SELECT COUNT(*) FROM team_versions WHERE battle_id='1'").fetchone()[0], 1)

    def test_arrival_alone_is_verified_even_without_an_old_direct_reference(self):
        with self.archive.db:
            self.archive.db.execute('DELETE FROM team_current')
        self.archive.ingest(92, [[report(2, 300)]], 310)
        answer = self.query()
        self.assertEqual((answer['match'], answer['current_report_id']), ('matched_arrival', '2'))

    def test_another_army_same_troops_or_two_shared_cards_cannot_refresh_this_army(self):
        self.archive.ingest(92, [[report(2, 300)]], 310)
        self.archive.ingest(92, [[report(3, 400, army='other-army')]], 410)
        self.archive.ingest(92, [[report(4, 500, cards='21,11,99')]], 510)
        answer = self.query()
        self.assertEqual(answer['current_report_id'], '2')

    def test_unreceived_or_future_reports_cannot_refresh_current(self):
        self.archive.ingest(92, [[report(2, 300)]], 310)
        self.archive.ingest(92, [[report(3, 400, side='defend')]], 900)
        self.archive.ingest(92, [[report(4, 800, side='defend')]], 500)
        self.assertEqual(self.query()['current_report_id'], '2')

    def test_ambiguous_arrival_does_not_steal_another_armys_roster(self):
        self.observe(at=221, army='another-army')
        self.archive.ingest(92, [[report(2, 300)]], 310)
        answer = self.query()
        self.assertEqual(answer['current_resolution']['anchor_battle_id'], '1')
        self.assertEqual(answer['items'], [])

    def test_newest_missing_skills_are_not_backfilled_from_an_older_report(self):
        self.archive.ingest(92, [[report(2, 300)]], 310)
        latest = report(3, 400)
        latest.pop('all_skill_info')
        self.archive.ingest(92, [[latest]], 410)
        answer = self.query()
        self.assertEqual(answer['current_report_id'], '3')
        self.assertTrue(all(h['skills'] is None for h in answer['items'][0]['data']['heroes']))

    def test_newer_explicit_different_roster_replaces_the_arrival_reference(self):
        self.archive.ingest(92, [[report(2, 300)]], 310)
        self.archive.ingest(92, [[report(3, 400, old=True, army=ARMY)]], 410)
        answer = self.query()
        self.assertEqual(answer['current_resolution']['anchor_battle_id'], '3')
        self.assertEqual(answer['items'], [])

    def test_equal_battle_times_use_numeric_report_id_for_both_paths(self):
        from lineup_prediction import player_history,predict
        self.archive.ingest(92,[[report(2,300,old=True,army=ARMY)]],310)
        self.archive.ingest(92,[[report(10,300)]],311)
        answer=self.query()
        prediction=predict({'id':ARMY,'owner':'12276','troops':[{'code':21},{'code':11},{'code':31}]},
            player_history(self.archive.db,self.archive.profile_id,'player','1009'),700)
        self.assertEqual(answer['current_report_id'],'10')
        self.assertEqual(prediction['current_report_id'],'10')


if __name__ == '__main__':
    unittest.main()
