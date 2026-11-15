import copy
import json
import tempfile
import unittest
from pathlib import Path

from test_support import Archive
from lineup_prediction import player_history, predict, record_forecast
from evidence_reader import ReportEvidenceReader
from types import SimpleNamespace
from test_intelligence import battle


class LatestLiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.archive = Archive(Path(self.tmp.name))
        self.march = {'id':'9001', 'owner':'200', 'player_name':'enemy', 'union_id':'1008',
            'troops':[{'code':21},{'code':32},{'code':3}], 'begin':200, 'end':800,
            'from':[500,600], 'target':[510,610], 'state_code':1}

    def tearDown(self):
        self.archive.close(); self.tmp.cleanup()

    def report(self, bid, at, *, army='9001', main=True, cards='11,12,13', role='enemy-role'):
        row = battle(bid, at, main=main)
        row.update(attack_idu=f'{army},{cards},0', attack_hero_type='0,21,32,3,', attack_role_id=role)
        return row

    def prediction(self, at):
        return predict(self.march, player_history(self.archive.db, self.archive.profile_id, 'enemy', '1008'), at)

    def assert_selected(self, result, bid, kind):
        bid = str(bid)
        self.assertEqual(result['force_assessment']['kind'], kind)
        self.assertEqual(result['force_assessment'].get('battle_id'), bid)
        self.assertEqual(result['current_report_id'], bid)
        self.assertEqual(result['items'][0]['battle_id'], bid)
        self.assertEqual(result['lead_loadout']['battle_id'], bid)

    def test_after_departure_latest_same_team_updates_live_but_original_forecast_stays(self):
        self.archive.ingest(92, [[self.report(1, 100)]], 110)
        first = self.prediction(210)
        self.assert_selected(first, 1, 'main')
        record_forecast(self.archive.db, self.archive.profile_id, self.march, first, 210)
        original = self.archive.db.execute('SELECT data FROM lineup_forecasts WHERE issued_at=210').fetchone()[0]
        # The same cards are seen after departure, now with an explicit empty slot.
        new = self.report(2, 250, army='0')
        new['all_skill_info'] = new['all_skill_info'].replace(',200021,1;', ',0,0;', 1)
        new['attack_advance'] = '2,3,4'
        self.archive.ingest(92, [[new]], 260)
        current = self.prediction(270)
        self.assert_selected(current, 2, 'spartan')
        self.assertEqual(current['current_resolution']['source'], 'same_card_instances')
        record_forecast(self.archive.db, self.archive.profile_id, self.march, current, 270)
        self.assertEqual(self.archive.db.execute('SELECT data FROM lineup_forecasts WHERE issued_at=210').fetchone()[0], original)
        self.assertEqual(self.archive.db.execute('SELECT COUNT(*) FROM lineup_forecasts').fetchone()[0], 2)
        replayed = self.prediction(210)
        self.assert_selected(replayed, 1, 'main')

    def test_newest_report_uses_battle_time_and_id_not_ingestion_order(self):
        self.archive.ingest(92, [[self.report(3, 350, main=False)]], 360)
        self.archive.ingest(92, [[self.report(1, 100)]], 400)
        result = self.prediction(410)
        self.assertEqual(result['current_report_id'], '3')
        self.assertNotEqual(result['force_assessment']['kind'], 'main')
        self.archive.ingest(92, [[self.report(4, 350)]], 420)
        self.assert_selected(self.prediction(430), 4, 'main')

    def test_future_or_unreceived_report_does_not_update_live(self):
        self.archive.ingest(92, [[self.report(1, 100)]], 110)
        self.archive.ingest(92, [[self.report(2, 250, main=False)]], 500)
        self.archive.ingest(92, [[self.report(3, 600, main=False)]], 260)
        self.assert_selected(self.prediction(300), 1, 'main')

    def test_same_troops_other_army_other_cards_do_not_replace_this_team(self):
        self.archive.ingest(92, [[self.report(1, 100)]], 110)
        other = self.report(2, 250, army='9002', cards='21,22,23', main=False)
        self.archive.ingest(92, [[other]], 260)
        self.assert_selected(self.prediction(270), 1, 'main')
        another = self.report(3, 260, army='0', cards='31,32,33', main=False)
        another['attack_all_hero_info'] = another['attack_all_hero_info'].replace('100701', '100814')
        self.archive.ingest(92, [[another]], 270)
        self.assert_selected(self.prediction(280), 1, 'main')

    def test_latest_partial_or_missing_skills_does_not_restore_old_main(self):
        self.archive.ingest(92, [[self.report(1, 100)]], 110)
        partial = self.report(2, 250, army='0')
        partial['all_skill_info'] = ''
        self.archive.ingest(92, [[partial]], 260)
        self.assert_selected(self.prediction(270), 2, 'unknown')
        partial = self.report(3, 300, army='0')
        partial['attack_all_hero_info'] = partial['attack_all_hero_info'].split(';')[0] + ';'
        self.archive.ingest(92, [[partial]], 310)
        result = self.prediction(320)
        self.assertEqual(result['current_report_id'], '3')
        self.assertEqual(result['force_assessment']['kind'], 'unknown')
        self.assertIn('unresolved', result['current_resolution'])
        self.assertNotIn('lead_loadout', result)

    def test_all_observed_battles_remain_regardless_of_class_and_result(self):
        records = []
        for bid, result in enumerate((0, 1, 6, 0, 1, 6), 1):
            row = self.report(bid, 100 + bid * 10, main=bid % 2 == 0)
            row['result'] = result
            if bid == 5: row['npc'] = 1
            if bid == 6: row['all_skill_info'] = ''
            records.append(row)
        self.archive.ingest(92, [[r] for r in records], 180)
        for row in records:
            self.archive.ingest(11, {'battle_id':row['battle_id'], 'report':f'full {row["battle_id"]}'}, 190)
        before = {t:self.archive.db.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]
                  for t in ('battle_reports','report_bodies','combat_sides','messages')}
        for at in (210, 250, 300): self.prediction(at)
        self.archive.ingest(92, [[r] for r in records], 310)
        after = {t:self.archive.db.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0] for t in before}
        self.assertEqual(before, after)
        self.assertEqual((after['battle_reports'], after['report_bodies'], after['combat_sides']), (6, 6, 12))
        self.assertEqual(self.archive.db.execute('SELECT COUNT(*) FROM report_read_jobs WHERE kind=\'detail\'').fetchone()[0], 6)
        outcomes = {r[0] for r in self.archive.db.execute('SELECT DISTINCT outcome FROM combat_sides')}
        self.assertEqual(outcomes, {'win','loss','draw'})

    def test_ordinary_battle_full_text_is_not_starved_by_priority_main_jobs(self):
        for bid in range(1, 10):
            self.archive.ingest(92, [[self.report(bid, 100 + bid, main=bid != 1)]], 120)
        saved = []
        def fetch(bid):
            saved.append(bid)
            self.archive.ingest(11, {'battle_id':bid, 'report':f'full {bid}'}, 130)
        reader = ReportEvidenceReader(self.archive, SimpleNamespace(fetch_battle_detail=fetch), lambda:None, False)
        try:
            # Use explicit priorities so this tests scheduling independently of classifiers.
            with self.archive.db:
                self.archive.db.execute("UPDATE report_read_jobs SET priority=80 WHERE battle_id<>'1'")
            reader.resume(30, continuous=True)
            for _ in range(6):
                reader.not_before = 0; reader._step()
            self.assertEqual(saved[:5], [9,8,7,6,5])
            self.assertEqual(saved[5], 1)
            self.assertEqual(self.archive.db.execute('SELECT COUNT(*) FROM battle_reports').fetchone()[0], 9)
        finally:
            reader.close()

    def test_legacy_linked_report_without_projection_key_still_has_a_serializable_primary(self):
        self.archive.ingest(92, [[self.report(1, 100)]], 110)
        with self.archive.db:
            self.archive.db.execute("UPDATE combat_sides SET hero_key='' WHERE side='attack'")
        current = self.prediction(210)
        self.assert_selected(current, 1, 'main')
        self.assertEqual(current['items'][0]['hero_key'], '100701,100807,100703')
        record_forecast(self.archive.db, self.archive.profile_id, self.march, current, 210)
        self.assertEqual(self.archive.db.execute('SELECT COUNT(*) FROM lineup_forecasts').fetchone()[0], 1)


if __name__ == '__main__': unittest.main()
