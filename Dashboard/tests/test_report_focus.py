import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from test_support import Archive
from evidence_reader import ReportEvidenceReader
from test_intelligence import battle, world


class SelectedReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.archive = Archive(Path(self.temp.name))
        self.reader = ReportEvidenceReader(self.archive, SimpleNamespace(), lambda:None, False)
        scene = world(bid=3)
        scene[6]['9001'][16] = '2,21;1,32;2,3;'
        self.archive.ingest(5028, scene, 200)
        for bid, name, troops in ((1, 'enemy', '0,21,32,3,'), (2, 'enemy', '0,3,3,3,'),
                                   (9, 'another-player', '0,21,32,3,')):
            record = battle(bid, 100+bid)
            record.update(attack_name=name, attack_hero_type=troops)
            self.archive.ingest(92, [[record]], 220)

    def tearDown(self):
        self.reader.close(); self.archive.close(); self.temp.cleanup()

    def test_selected_army_prioritizes_its_references_and_matching_personal_reports(self):
        self.reader.prioritize_army('9001')
        jobs = {(r['kind'], r['battle_id']):r['priority'] for r in self.archive.db.execute(
            'SELECT kind,battle_id,priority FROM report_read_jobs')}
        self.assertEqual(jobs['profile', '3'], 130)
        self.assertEqual(jobs['detail', '3'], 120)
        self.assertEqual(jobs['detail', '1'], 120)
        self.assertLess(jobs['detail', '2'], 120, 'Different troop positions do not consume the focused queue')
        self.assertLess(jobs['detail', '9'], 120, 'Another player must never enter focused evidence')
        self.assertEqual(self.reader.snapshot()['selected_army']['battle_ids'], ['1', '3'])
        self.assertIsNone(self.archive.db.execute(
            "SELECT army_id FROM combat_sides WHERE battle_id='1' AND side='attack'").fetchone()[0],
            'Queueing personal evidence must not establish an army binding')

    def test_same_selection_refreshes_new_reports_without_repeated_poll_writes(self):
        self.reader.prioritize_army('9001')
        previous = self.reader.focus_checked[1]
        record = battle(4, 120); record['attack_hero_type'] = '0,21,32,3,'
        self.archive.ingest(92, [[record]], 221)
        self.reader.prioritize_army('9001')
        self.assertNotIn('4', self.reader.focus_status['battle_ids'])
        with patch('evidence_reader.time.monotonic', return_value=previous+16):
            self.reader.prioritize_army('9001')
        self.assertIn('4', self.reader.focus_status['battle_ids'])

    def test_missing_selection_does_not_fall_back_to_player_or_other_account(self):
        for army in (None, '', 'not-an-army', '999999'):
            self.reader.prioritize_army(army)
        self.assertIsNone(self.reader.focus_status)

    def test_urgent_focus_bypasses_batch_cooldown_but_not_failure_backoff(self):
        self.reader.budget_cooldown=True;self.reader.last_completed=0
        self.reader.not_before=time.monotonic()+60
        self.reader.prioritize_army('9001',urgent=True)
        self.assertLess(self.reader.not_before,time.monotonic())
        priorities={r['battle_id']:r['priority'] for r in self.archive.db.execute(
            "SELECT battle_id,priority FROM report_read_jobs WHERE kind='detail'")}
        self.assertEqual(priorities['1'],200)
        self.assertLess(priorities['9'],200)
        self.reader.not_before=time.monotonic()+60;old=self.reader.not_before
        self.reader.prioritize_army('9001',urgent=True)
        self.assertEqual(self.reader.not_before,old,'Error backoff must remain intact')


if __name__ == '__main__':
    unittest.main()
