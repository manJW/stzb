"""The statistics worker may not use the writer before initialization finishes."""
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from archive import Archive as RealArchive
from attendance import Attendance
from test_support import Archive


class ArchiveStartupTests(unittest.TestCase):
    def test_statistics_worker_waits_for_identity_seed_to_finish(self):
        started,seeded=threading.Event(),threading.Event()
        violations=[]
        original_seed=RealArchive.seed_player_traits
        def worker(attendance):
            started.set()
            with attendance.archive.lock:
                if not seeded.is_set():violations.append('Statistics reached the writer before initialization ended')
        def seed(archive):
            self.assertTrue(started.wait(2))
            time.sleep(.02)
            original_seed(archive)
            seeded.set()
        with tempfile.TemporaryDirectory()as folder:
            with patch.object(Attendance,'_run',worker),patch.object(RealArchive,'seed_player_traits',seed):
                archive=Archive(Path(folder))
                try:
                    archive.attendance.thread.join(2)
                    self.assertTrue(seeded.is_set());self.assertFalse(archive.attendance.thread.is_alive())
                    self.assertEqual(violations,[])
                    self.assertEqual(archive.db.execute('PRAGMA quick_check').fetchone()[0],'ok')
                finally:archive.close()
