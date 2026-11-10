import json
import http.client
import ctypes
import shutil
import struct
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
from bridge import ReadOnlyBridge, union_report_request, _exchange
from collector import Collector, MessageFramer
from harvest import BattleHarvester
from server import ThreadingHTTPServer, make_handler


def page(*ids, union=1909):
    return [[{"battle_id": bid, "attack_name": "sample", "attack_unionid": union,
        "defend_unionid": 1008}] for bid in ids]


class BridgeTests(unittest.TestCase):
    def test_wrong_pipe_owner_is_rejected_before_any_command_write(self):
        calls = []
        class Api:
            def __init__(self, fn):
                self.fn = fn
            def __call__(self, *args):
                return self.fn(*args)
        def owner(handle, pointer):
            ctypes.cast(pointer, ctypes.POINTER(ctypes.wintypes.ULONG)).contents.value = 999
            return True
        kernel = SimpleNamespace(**{name: Api(lambda *args: True) for name in
            ("WaitNamedPipeW", "CreateFileW", "GetNamedPipeServerProcessId", "WriteFile", "PeekNamedPipe", "ReadFile", "CloseHandle")})
        kernel.CreateFileW.fn = lambda *args: 123
        kernel.GetNamedPipeServerProcessId.fn = owner
        kernel.WriteFile.fn = lambda *args: calls.append("write") or True
        kernel.CloseHandle.fn = lambda *args: calls.append("close") or True
        with patch("bridge.ctypes.WinDLL", return_value=kernel):
            with self.assertRaisesRegex(RuntimeError, "进程"):
                _exchange("__pipe_ping", connection_provider=lambda: {"pid": 7764})
        self.assertEqual(calls, ["close"])

    def test_only_verified_read_request_can_be_constructed(self):
        commands = []
        bridge = ReadOnlyBridge(lambda text: commands.append(text) or "OK:sent cmd=92")
        bridge.fetch_union_page(123)
        self.assertEqual(commands, ['__net_send 92 [[1,2],[1,3],[-1],123,0]'])
        for invalid in (True, -1, 1.5, "123", 2**63):
            with self.assertRaises(ValueError):
                bridge.fetch_union_page(invalid)
        self.assertEqual(len(commands), 1)

    def test_transport_ack_is_required(self):
        with self.assertRaises(RuntimeError):
            ReadOnlyBridge(lambda _: "ERR:busy").fetch_union_page(0)

    def test_client_header_and_unrelated_bodies_are_not_emitted(self):
        messages, errors = [], []
        def frame(mid, body):
            header = bytearray(57)
            struct.pack_into(">I", header, 0, 53 + len(body))
            struct.pack_into(">I", header, 44, mid)
            header[56] = 2
            return header + body
        framer = MessageFramer(lambda *args: messages.append(args), lambda: errors.append(1), True, {92})
        # An unrelated invalid body is skipped without decoding or retaining its credentials.
        payload = frame(99991, b"private-invalid-json") + frame(92, json.dumps(union_report_request(0)).encode())
        for block in (payload[:47], payload[47:80], payload[80:]):
            framer.feed(block, 10)
        self.assertEqual(messages, [(92, union_report_request(0), 10)])
        self.assertEqual(errors, [])
        self.assertEqual(framer.buffer, b"")


class HarvestTests(unittest.TestCase):
    def setUp(self):
        self.parent = ROOT / "runtime" / "harvest-tests"
        self.parent.mkdir(exist_ok=True)
        self.folder = Path(tempfile.mkdtemp(prefix="case-", dir=self.parent)).resolve()
        self.archive = Archive(self.folder)
        self.sent = []
        self.harvester = BattleHarvester(self.folder, self.archive,
            SimpleNamespace(fetch_union_page=lambda cursor: self.sent.append(cursor)), lambda: None, False)
        self.harvester.not_before = 0

    def tearDown(self):
        self.harvester.close()
        self.archive.close()
        if self.folder.parent != self.parent.resolve() or not self.folder.name.startswith("case-"):
            raise RuntimeError("Unexpected test directory")
        shutil.rmtree(self.folder)

    def begin(self, pages=10):
        self.harvester.start({"max_pages": pages})
        self.harvester._step()

    def reply(self, value):
        pending = self.harvester.pending
        at = pending["sent_at"] + .01
        self.harvester.on_request(pending["body"], at)
        stats = self.archive.ingest(92, value, at)
        self.harvester.on_page(value, at, stats)
        return stats

    def test_commit_then_cursor_and_dedup_counts(self):
        self.begin()
        first = self.reply(page(200, 199))
        self.assertEqual(first["new_battle_ids"], ["200", "199"])
        self.assertEqual(self.harvester.snapshot()["cursor"], 0)
        self.harvester._step()
        saved = self.harvester.snapshot()
        self.assertEqual((saved["cursor"], saved["pages"], saved["new_reports"]), (199, 1, 2))
        self.assertEqual(self.sent, [0])  # Rate limit prevents the next immediate request.
        duplicate = self.archive.ingest(92, page(200, 199), time.time())
        self.assertEqual(duplicate["new_battle_ids"], [])
        self.harvester.not_before = 0
        self.harvester._step()
        self.assertEqual(self.sent, [0, 199])

    def test_no_advance_from_unobserved_or_uncommitted_reply(self):
        self.begin()
        at = time.time() + .01
        self.harvester.on_page(page(200), at, {"battle_ids": ["200"], "new_battle_ids": ["200"]})
        self.harvester._step()
        self.assertEqual(self.harvester.snapshot()["cursor"], 0)
        self.harvester.fail("战报保存失败，游标未推进")
        self.assertEqual(self.harvester.snapshot()["status"], "error")
        self.assertEqual(self.harvester.snapshot()["cursor"], 0)

    def test_manual_paging_pauses_without_advancing(self):
        self.begin()
        self.harvester.on_request(union_report_request(500), time.time() + .01)
        self.assertEqual(self.harvester.snapshot()["status"], "paused")
        self.assertEqual(self.harvester.snapshot()["cursor"], 0)

    def test_wrong_union_and_wrong_cursor_cannot_advance(self):
        self.begin()
        self.reply(page(200, union=999))
        self.assertEqual(self.harvester.snapshot()["status"], "error")
        self.harvester.start(resume=True)
        self.harvester.not_before = 0
        self.harvester._step()
        self.reply(page(200))
        self.harvester._step()
        self.harvester.not_before = 0
        self.harvester._step()
        self.reply(page(201))
        self.assertEqual(self.harvester.snapshot()["cursor"], 200)
        self.assertEqual(self.harvester.snapshot()["status"], "error")

    def test_limits_restart_and_resume_preserve_position(self):
        self.begin(pages=1)
        self.reply(page(200))
        self.harvester._step()
        self.assertEqual(self.harvester.snapshot()["status"], "paused")
        restored = BattleHarvester(self.folder, self.archive, self.harvester.bridge, lambda: None, False)
        self.assertEqual(restored.snapshot()["cursor"], 200)
        restored.start({"max_pages": 3}, resume=True)
        restored.not_before = 0
        restored._step()
        self.assertEqual(self.sent, [0, 200])
        self.assertEqual(restored.snapshot()["run_pages"], 0)
        restored.close()

    def test_empty_page_ends_only_available_filter(self):
        self.begin()
        self.reply([])
        self.harvester._step()
        self.assertTrue(self.harvester.snapshot()["available_end"])
        self.assertEqual(self.harvester.snapshot()["status"], "available_end")

    def test_timeout_and_pause_do_not_advance_and_drain_late_response(self):
        self.begin()
        self.harvester.pending["deadline"] = time.monotonic() - 1
        self.harvester._step()
        self.assertEqual(self.harvester.snapshot()["status"], "paused")
        self.assertEqual(self.harvester.snapshot()["cursor"], 0)
        self.harvester.start(resume=True)
        self.harvester._step()
        self.harvester.pause()
        self.harvester.start(resume=True)
        self.harvester._step()
        self.assertEqual(len(self.sent), 2)

    def test_readiness_profile_and_request_limits_are_checked(self):
        for payload in ({"command": 93}, {"max_pages": True}, {"max_pages": 0}, {"max_minutes": 121}):
            with self.assertRaises(ValueError):
                self.harvester.start(payload)
        self.harvester.ready = lambda: "未连接"
        with self.assertRaisesRegex(ValueError, "未连接"):
            self.harvester.start()
        self.harvester.ready = lambda: None
        self.archive.profile_id = "other-season"
        with self.assertRaises(ValueError):
            self.harvester.start()

    def test_armed_resume_waits_for_map_before_using_saved_cursor(self):
        self.begin(pages=1)
        self.reply(page(200))
        self.harvester._step()
        self.harvester.ready = lambda: '等待完整地图'
        self.harvester.start({'wait_for_ready':True}, resume=True)
        self.harvester.not_before = 0
        self.harvester._step()
        self.assertEqual(self.sent, [0])
        self.assertEqual(self.harvester.snapshot()['cursor'], 200)
        self.assertEqual(self.harvester.snapshot()['reason'], '等待完整地图')
        self.harvester.ready = lambda: None
        self.harvester._step()
        self.assertEqual(self.sent, [0, 200])

    def test_fast_response_before_transport_ack_is_accepted(self):
        def send(cursor):
            self.reply(page(200))
            self.harvester._step()
            self.assertEqual(self.harvester.snapshot()["cursor"], 0)
        self.harvester.bridge = SimpleNamespace(fetch_union_page=send)
        self.begin(pages=1)
        self.harvester._step()
        self.assertEqual(self.harvester.snapshot()["cursor"], 200)

    def test_manual_request_between_pages_pauses_and_prestart_request_drains(self):
        self.begin()
        self.reply(page(200))
        self.harvester._step()
        self.harvester.on_request(union_report_request(100), time.time() + .01)
        self.assertEqual(self.harvester.snapshot()["status"], "paused")
        self.assertEqual(self.harvester.snapshot()["cursor"], 200)
        self.harvester.on_request(union_report_request(100), time.time() + .02)
        self.harvester.start(resume=True)
        self.harvester._step()
        self.assertEqual(self.sent, [0])

    def test_collector_commit_failure_stops_harvest(self):
        self.begin()
        (self.folder / "runtime").mkdir(exist_ok=True)
        (self.folder / "capture-source.json").write_text(json.dumps({"mode": "pc", "profile_id": self.archive.profile_id}))
        state = SimpleNamespace(root=self.folder, health={}, lock=threading.RLock(), apply=lambda *args: None)
        observer = Collector(state, self.archive)
        observer.report_observer = self.harvester
        with patch.object(self.archive, "ingest", side_effect=RuntimeError("disk")):
            observer._message(92, page(200), time.time())
        self.assertEqual(self.harvester.snapshot()["cursor"], 0)
        self.assertEqual(self.harvester.snapshot()["status"], "error")


class HarvestHttpTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.harvester = SimpleNamespace(snapshot=lambda: {"status": "idle"},
            start=lambda payload, resume=False: self.calls.append((payload, resume)) or {"status": "running"},
            pause=lambda: self.calls.append("pause") or {"status": "paused"})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(None, 0))
        self.port = self.server.server_address[1]
        self.server.RequestHandlerClass = make_handler(None, self.port, harvester=self.harvester)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def request(self, method, path, body=None, origin=True):
        headers = {"Content-Type": "application/json"}
        if origin:
            headers["Origin"] = f"http://127.0.0.1:{self.port}"
        connection = http.client.HTTPConnection("127.0.0.1", self.port)
        connection.request(method, path, body, headers)
        response = connection.getresponse()
        status, content = response.status, response.read()
        connection.close()
        return status, content

    def test_readonly_controls_reject_cross_origin_and_unknown_routes(self):
        self.assertEqual(self.request("GET", "/api/harvest/status")[0], 200)
        self.assertEqual(self.request("POST", "/api/harvest/start", "{}", False)[0], 403)
        self.assertEqual(self.request("POST", "/api/harvest/send", "{}")[0], 404)
        self.assertEqual(self.request("POST", "/api/harvest/start", "[]")[0], 400)
        self.assertEqual(self.calls, [])

    def test_start_pause_resume_routes_are_concrete(self):
        for action in ("start", "pause", "resume"):
            self.assertEqual(self.request("POST", "/api/harvest/" + action, '{"max_pages":2}')[0], 200)
        self.assertEqual(self.calls, [({"max_pages": 2}, False), "pause", ({"max_pages": 2}, True)])


if __name__ == "__main__":
    unittest.main()
