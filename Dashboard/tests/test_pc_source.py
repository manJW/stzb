import json
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from collector import Collector, PC_EXECUTABLE, discover_pc_connection


class PcSourceTests(unittest.TestCase):
    def setUp(self):
        self.parent = ROOT / "runtime" / "pc-source-tests"
        self.parent.mkdir(exist_ok=True)
        self.folder = Path(tempfile.mkdtemp(prefix="case-", dir=self.parent)).resolve()
        self.applied = []
        self.saved = []
        self.state = SimpleNamespace(root=self.folder, health={}, lock=threading.RLock(),
            apply=lambda *args: self.applied.append(args))
        self.archive = SimpleNamespace(profile_id="season-a",
            ingest=lambda *args, **kwargs: self.saved.append((args, kwargs)))

    def tearDown(self):
        if self.folder.parent != self.parent.resolve() or not self.folder.name.startswith("case-"):
            raise RuntimeError("Unexpected test directory")
        shutil.rmtree(self.folder)

    def settings(self, **value):
        (self.folder / "capture-source.json").write_text(json.dumps(value), encoding="utf-8")

    def connection(self):
        return {"peer": "45.253.137.49", "local": "192.168.5.203", "local_port": 54777,
            "pid": 7764, "executable": PC_EXECUTABLE, "source": "pc", "interface": "\\Device\\NPF_{test}"}

    def test_pc_receive_is_bound_to_the_verified_executable(self):
        data = self.connection()
        with patch("collector.subprocess.run", return_value=SimpleNamespace(stdout=json.dumps(data))):
            self.assertEqual(discover_pc_connection()["pid"], 7764)
        data["executable"] = r"C:\unrelated.exe"
        with patch("collector.subprocess.run", return_value=SimpleNamespace(stdout=json.dumps(data))):
            with self.assertRaises(ValueError):
                discover_pc_connection()

    def test_no_pc_connection_waits_without_guessing_a_port(self):
        with patch("collector.subprocess.run", return_value=SimpleNamespace(stdout="")):
            self.assertIsNone(discover_pc_connection())

    def test_source_cannot_silently_write_to_another_season(self):
        self.settings(mode="pc", profile_id="season-b")
        with self.assertRaises(ValueError):
            Collector(self.state, self.archive)

    def test_pc_source_routes_and_preserves_message_provenance(self):
        self.settings(mode="pc", profile_id="season-a")
        observer = Collector(self.state, self.archive)
        with (patch("collector.discover_pc_connection", return_value=self.connection()) as pc,
              patch("collector.discover_connection") as emulator):
            self.assertEqual(observer._discover()["source"], "pc")
            pc.assert_called_once()
            emulator.assert_not_called()
        observer._message(92, [], 100)
        self.assertEqual(self.saved, [((92, [], 100), {"source": "pc-live"})])
        self.assertEqual(self.applied, [(92, [], 100)])

    def test_existing_default_remains_the_emulator(self):
        observer = Collector(self.state, self.archive)
        self.assertEqual(observer.source, "mumu")


if __name__ == "__main__":
    unittest.main()
