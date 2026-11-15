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
from collector import Collector, DASHEN_EXECUTABLE, PC_EXECUTABLE, discover_dashen_connection, discover_pc_connection


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

    def test_capture_probe_requires_same_socket_live_process_and_recent_packets(self):
        observer = Collector(self.state, self.archive, settings={"mode": "pc"})
        verified = []
        self.state.verify_connection = lambda *args: verified.append(args)
        self.state.health["last_packet_at"] = 1000
        connection = {**self.connection(), "connection_started": 10}
        process = SimpleNamespace(poll=lambda: None)
        with patch.object(observer, "_discover", return_value=connection), patch("collector.time.time", return_value=1020):
            self.assertTrue(observer._verify_capture(connection, process, 999))
        self.assertEqual(verified, [(connection, 1020)])
        for current, code, moment in (({**connection, "local_port": 1}, None, 1021),
                                      ({**connection, "connection_started": 11}, None, 1021),
                                      (None, None, 1021), (connection, 1, 1021), (connection, None, 1061)):
            with self.subTest(current=current, code=code, moment=moment), \
                    patch.object(observer, "_discover", return_value=current), \
                    patch("collector.time.time", return_value=moment):
                self.assertFalse(observer._verify_capture(connection, SimpleNamespace(poll=lambda: code), 999))
        self.assertEqual(len(verified), 1)

    def dashen_connection(self):
        return {**self.connection(), "executable":DASHEN_EXECUTABLE, "source":"dashen",
                "worker_executable":str(Path(DASHEN_EXECUTABLE).parent.parent / "vbox" / "VBoxHeadless.exe"),
                "host_pid":123}

    def test_dashen_receive_checks_host_and_worker_paths(self):
        data = self.dashen_connection()
        with patch("collector.subprocess.run", return_value=SimpleNamespace(stdout=json.dumps(data))) as run:
            self.assertEqual(discover_dashen_connection()["source"], "dashen")
            script = run.call_args.args[0][-1]
            self.assertIn("ParentProcessId -eq $hostProcess.ProcessId", script)
            self.assertIn("$links.Count -eq 1", script)
        for key in ("executable", "worker_executable"):
            with self.subTest(field=key):
                invalid = {**data, key:r"C:\unrelated.exe"}
                with patch("collector.subprocess.run", return_value=SimpleNamespace(stdout=json.dumps(invalid))):
                    with self.assertRaises(ValueError):
                        discover_dashen_connection()

    def test_dashen_waits_without_a_unique_game_connection(self):
        with patch("collector.subprocess.run", return_value=SimpleNamespace(stdout="")):
            self.assertIsNone(discover_dashen_connection())

    def test_dashen_source_preserves_provenance_and_uses_its_own_adapter(self):
        self.settings(mode="dashen", profile_id="season-a", emulator_executable=DASHEN_EXECUTABLE)
        observer = Collector(self.state, self.archive)
        with (patch("collector.discover_dashen_connection", return_value=self.dashen_connection()) as dashen,
              patch("collector.discover_pc_connection") as pc,
              patch("collector.discover_connection") as mumu):
            self.assertEqual(observer._discover()["source"], "dashen")
            dashen.assert_called_once_with(DASHEN_EXECUTABLE)
            pc.assert_not_called()
            mumu.assert_not_called()
        observer._message(5028, [], 100)
        self.assertEqual(self.saved, [((5028, [], 100), {"source":"dashen-live"})])
        self.assertEqual(self.state.health["capture_source"], "dashen")


if __name__ == "__main__":
    unittest.main()
