"""Slow UI reads must not stop capture writes or fabricate completed analysis."""
import sqlite3
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_archive as archive_fixtures
from defense_model import analysis_budget, estimate
from lineup_prediction import predict_alerts


class QueryIsolationTests(unittest.TestCase):
    setUp = archive_fixtures.ArchiveTests.setUp
    tearDown = archive_fixtures.ArchiveTests.tearDown

    def test_wal_reader_keeps_a_snapshot_while_capture_commits(self):
        self.archive.ingest(92, [[archive_fixtures.battle(1)]], 200)
        done = threading.Event()
        errors = []

        def capture():
            try:
                self.archive.ingest(92, [[archive_fixtures.battle(2, 201)]], 202)
            except Exception as error:
                errors.append(error)
            finally:
                done.set()

        with self.archive.read_snapshot() as reader, reader.lock:
            self.assertIsNot(reader.lock, self.archive.lock)
            self.assertEqual(reader.db.execute('SELECT COUNT(*) FROM battle_reports').fetchone()[0], 1)
            worker = threading.Thread(target=capture)
            worker.start()
            completed = done.wait(2)
            self.assertEqual(reader.db.execute('SELECT COUNT(*) FROM battle_reports').fetchone()[0], 1)
        worker.join(timeout=3)
        self.assertTrue(completed, 'A stalled reader must not own the capture writer lock')
        self.assertFalse(errors)
        with self.archive.read_snapshot() as reader:
            self.assertEqual(reader.db.execute('SELECT COUNT(*) FROM battle_reports').fetchone()[0], 2)
            with self.assertRaises(sqlite3.OperationalError):
                reader.db.execute('DELETE FROM battle_reports')

    def test_slow_prediction_does_not_hold_the_capture_lock(self):
        entered, resume, finished = threading.Event(), threading.Event(), threading.Event()
        errors = []
        snapshot = {'alerts': [{'army_id': '1'}], 'armies': [
            {'id': '1', 'player_name': 'player', 'union_id': '1008'}], 'server_now': 300}

        def history(*_):
            entered.set()
            resume.wait(3)
            return [], {}, False

        def run():
            try:
                predict_alerts(self.archive, snapshot)
            except Exception as error:
                errors.append(error)
            finally:
                finished.set()

        with patch('lineup_prediction.player_history', side_effect=history), patch('lineup_prediction.current_prediction', return_value={}):
            worker = threading.Thread(target=run)
            worker.start()
            try:
                self.assertTrue(entered.wait(2))
                self.assertTrue(self.archive.lock.acquire(timeout=.2), 'Prediction must release the writer lock before reading')
                self.archive.lock.release()
                self.archive.ingest(92, [[archive_fixtures.battle()]], 301)
            finally:
                resume.set()
                worker.join(timeout=3)
        self.assertTrue(finished.is_set())
        self.assertFalse(errors)

    def test_sql_and_cpu_budgets_abort_without_a_fake_result(self):
        started = time.monotonic()
        with self.archive.read_snapshot(timeout=.01) as reader:
            with self.assertRaisesRegex(sqlite3.OperationalError, 'interrupted'):
                reader.db.execute('WITH RECURSIVE n(x) AS (VALUES(0) UNION ALL SELECT x+1 FROM n WHERE x<100000000) SELECT SUM(x) FROM n').fetchone()
        self.assertLess(time.monotonic()-started, 1)
        matchup = (frozenset((1, 2, 3)), frozenset((4, 5, 6)), (0, 0, 0))
        with analysis_budget(0), self.assertRaises(TimeoutError):
            estimate([(matchup, True)]*20, matchup, .5)
        self.assertIsNotNone(estimate([(matchup, True)]*20, matchup, .5))


if __name__ == '__main__':
    unittest.main()
