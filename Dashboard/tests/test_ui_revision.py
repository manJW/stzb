"""UI updates settle before publication and do not depend on a capture restart."""
import tempfile
import unittest
from pathlib import Path

from server import UiRevision


class UiRevisionTests(unittest.TestCase):
    def test_changes_settle_and_missing_files_keep_last_good_revision(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root/'static').mkdir()
            for name in ('index.html','app.js','app.css'):
                (root/'static'/name).write_text('original',encoding='utf-8')
            tracker = UiRevision(root)
            before = tracker.current(now=0)
            self.assertTrue(before)
            self.assertEqual(tracker.current(now=1), before)
            css = root/'static/app.css'
            css.write_text('updated stylesheet',encoding='utf-8')
            self.assertEqual(tracker.current(now=2), before)
            js = root/'static/app.js'
            js.write_text('updated script',encoding='utf-8')
            self.assertEqual(tracker.current(now=3), before)
            self.assertEqual(tracker.current(now=4.9), before)
            after = tracker.current(now=5)
            self.assertNotEqual(after, before)
            self.assertEqual(tracker.current(now=6), after)
            css.unlink()
            self.assertEqual(tracker.current(now=7), after)


if __name__ == '__main__':
    unittest.main()
