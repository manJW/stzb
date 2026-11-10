import http.client
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from archive import Archive
from member_client import MemberCollector, Outbox, Uploader
from member_receiver import enroll, receiver, sources
from share_protocol import UPLOAD_PATH, project_report, project_scene
from test_intelligence import battle, world


class MemberSharingTests(unittest.TestCase):
    def setUp(self):
        parent = ROOT / "runtime"
        parent.mkdir(exist_ok=True)
        self.folder = tempfile.TemporaryDirectory(prefix="member-pilot-test-", dir=parent)
        self.addCleanup(self.folder.cleanup)
        self.archive = Archive(Path(self.folder.name))
        self.addCleanup(self.archive.close)
        self.now = int(time.time())
        self.service = receiver(self.archive, 0)
        self.thread = threading.Thread(target=self.service.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_service)
        self.port = self.service.server_address[1]
        for name, uid, role in (("member-a", "201", "9201"), ("member-b", "202", "9202")):
            with self.archive.db:
                self.archive._player("world:"+uid, name, 1909, "alliance", self.now, "world_scene", role, uid)
        self.a = enroll(self.archive, "member-a", f"http://127.0.0.1:{self.port}")
        self.b = enroll(self.archive, "member-b", f"http://127.0.0.1:{self.port}")

    def stop_service(self):
        self.service.shutdown()
        self.service.server_close()
        self.thread.join()

    def send(self, invitation, events, authorized=True, **override):
        payload = {key: invitation[key] for key in ("version", "scope", "identity")}
        payload.update(events=events, **override)
        headers = {"Content-Type": "application/json"}
        if authorized:
            headers["Authorization"] = "Bearer " + invitation["key"]
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=4)
        try:
            conn.request("POST", UPLOAD_PATH, json.dumps(payload).encode(), headers)
            response = conn.getresponse()
            return response.status, json.loads(response.read())
        finally:
            conn.close()

    def test_two_sources_merge_with_local_data_and_preserve_each_observation_time(self):
        at = self.now
        scene = world(end=at+40)
        scene[6]["9001"][4] = at-60
        self.archive.ingest(5026, scene, at-5, source="pc-live")
        report = battle(at=at-10)
        self.archive.ingest(92, [report], at-5, source="pc-live")
        for invitation, observed in ((self.a, at), (self.b, at-1), (self.a, at-2)):
            events = [[5026, observed, project_scene(scene)], [92, observed, project_report(report, "1909")]]
            self.assertEqual(self.send(invitation, events), (200, {"accepted": 2}))
        remote = world(aid="9002", end=at+60)
        remote[6]["9002"][4] = at
        self.assertEqual(self.send(self.b, [[5028, at, project_scene(remote)],
            [6231, at, {"battle_id": 1, "report": "complete battle text"}]]), (200, {"accepted": 2}))

        ops = self.archive.intelligence.query("operations", relation="all")
        self.assertEqual(ops["total"], 2)
        overlap = next(row for row in ops["items"] if row["army_id"] == "9001")
        self.assertEqual({r["name"]: r["observed_at"] for r in overlap["sources"]},
            {"member-a": at, "member-b": at-1, self.archive.profile["account_name"]: at-5})
        self.assertEqual(overlap["last_observed_at"], at)
        self.assertEqual(self.archive.summary()["counts"]["battle_reports"], 1)
        saved = self.archive.intelligence.report(1)
        self.assertEqual(len(saved["sources"]), 3)
        self.assertEqual(saved["full_report"]["sources"][0]["name"], "member-b")
        self.assertEqual({row["name"] for row in sources(self.archive) if row["last_received_at"]}, {"member-a", "member-b"})

    def test_scope_invitation_and_full_report_guards(self):
        report = project_report(battle(at=self.now-5), "1909")
        event = [[92, self.now, report]]
        self.assertEqual(self.send(self.a, event, authorized=False)[0], 401)
        self.assertEqual(self.send(self.a, event, scope={**self.a["scope"], "season": "another"})[0], 400)
        self.assertEqual(self.send(self.a, event, identity=self.b["identity"])[0], 400)
        self.assertEqual(self.send(self.a, [[92, self.now, {**report, "defend_unionid": 333}]])[0], 400)
        self.assertEqual(self.send(self.a, [[6231, self.now, {"battle_id": 99, "report": "unknown"}]])[0], 400)
        self.assertEqual(self.send(self.a, [[92, self.now, {**report, "password": "unexpected"}]])[0], 400)
        self.assertEqual(self.archive.summary()["counts"]["battle_reports"], 0)
        self.assertEqual(self.send(self.a, event)[0], 200)
        self.assertEqual(self.send(self.b, [[92, self.now, {**report, "wid": 5200610}]])[0], 400)
        with self.archive.db:
            self.archive.db.execute("UPDATE pilot_members SET enabled=0 WHERE name='member-a'")
        self.assertEqual(self.send(self.a, event)[0], 401)

    def test_member_filter_and_real_http_uploader(self):
        root = Path(self.folder.name)
        outbox = Outbox(root / "outbox.sqlite3")
        self.addCleanup(outbox.db.close)
        collector = MemberCollector(root, "stzb.exe", "dumpcap.exe", self.a, outbox)
        at = self.now
        scene = world(end=at+40)
        scene[6]["9001"][4] = at-40
        scene[18] = 1
        scene[1]["201"] = ["member-a", 9201, 1909] + [0]*10
        report = battle(at=at-2)
        collector._message(92, [report], at)
        self.assertFalse(outbox.batch())
        collector._message(5028, scene, at)
        self.assertFalse(outbox.batch())
        collector._message(5026, scene, at)
        collector._message(92, [report, {**report, "battle_id": 3, "defend_unionid": 333}], at)
        collector._message(6231, [{"battle_id": 1, "report": "full"}, {"battle_id": 3, "report": "foreign"}], at)
        self.assertEqual(len(outbox.batch()), 3)
        stop = threading.Event()
        thread = threading.Thread(target=Uploader(self.a, outbox).run, args=(stop,), daemon=True)
        thread.start()
        try:
            deadline = time.monotonic()+4
            while outbox.batch() and time.monotonic() < deadline:
                time.sleep(.05)
            self.assertFalse(outbox.batch())
            self.assertEqual(self.archive.intelligence.report(1)["full_report"]["body"], "full")
            self.assertEqual(self.archive.summary()["counts"]["battle_reports"], 1)
            collector.invalidate()
            collector._message(92, [battle(2, at=at-1)], at)
            self.assertFalse(outbox.batch())
        finally:
            stop.set()
            thread.join(2)


if __name__ == "__main__":
    unittest.main()
