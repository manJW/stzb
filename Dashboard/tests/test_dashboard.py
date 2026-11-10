import copy
import http.client
import io
import json
import shutil
import struct
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from model import WorldState
from collector import MessageFramer, TCPStream, pcap_packets, tcp_packet
from server import ThreadingHTTPServer, make_handler


def scene(order=10):
    value = [None] * 34
    value[1] = {"200": ["test", 0, 2000] + [0] * 9 + [[0, 0, "test union"]]}
    value[3], value[7] = {}, []
    tile = [0] * 21
    tile[2], tile[3] = 71021, 1909
    value[14] = {"10150926": {"0": tile}}
    raw = [0] * 33
    raw[0], raw[1], raw[2], raw[3] = 3, 200, 10000900, 10150926
    raw[4], raw[5], raw[9], raw[32] = 900, 1100, 14, 7
    value[6] = {"army": raw}
    value[17], value[18] = [995, 912, 1014, 931], order
    return value


class ModelTests(unittest.TestCase):
    def setUp(self):
        self.test_root = ROOT / "runtime" / "test-runs"
        self.test_root.mkdir(parents=True, exist_ok=True)
        self.folder = Path(tempfile.mkdtemp(prefix="case-", dir=self.test_root)).resolve()
        self.state = WorldState(self.folder)
        self.state.config["targets"] = [{"name": "test bookmark", "x": 1015, "y": 926}]
        self.state.connection("live", "test")
        self.state.packet(1000)
        self.state.apply(5026, scene(), 1000)
        self.state.config["state_rules"]["3:14:7"] = {"kind": "attack", "verified": True}

    def tearDown(self):
        if self.folder.parent != self.test_root.resolve() or not self.folder.name.startswith("case-"):
            raise RuntimeError("Unexpected test cleanup target")
        shutil.rmtree(self.folder)

    def hostile(self):
        self.state.config["relations"]["2000"] = {"relation": "enemy"}

    def test_fort_construction_and_enemy_relation_are_independent(self):
        self.hostile()
        update = scene(11)
        tile = update[14]['10150926']['0']
        tile[0], tile[2], tile[3], tile[4], tile[8] = 4, 200, 0, 1004, 1
        update[17][2] = 1015
        self.state.packet(1001)
        self.state.apply(5028, update, 1001)
        fort = self.state.snapshot(1002)['forts'][0]
        self.assertEqual((fort['relation'], fort['status'], fort['end_at']), ('enemy', 'building', 1004))
        self.assertEqual(self.state.snapshot(1005)['forts'][0]['status'], 'transition')
        tile[8], update[18] = 0, 12
        self.state.apply(5028, update, 1006)
        self.assertEqual(self.state.snapshot(1006)['forts'][0]['status'], 'ready')

    def test_snapshot_uses_the_troop_model_code_and_preserves_empty_slots(self):
        self.hostile()
        update = scene(11)
        update[6]["army"][16] = "2,21;3,23;-1,-1;"
        self.state.apply(5028, update, 1001)
        snapshot = self.state.snapshot(1002)
        self.assertEqual(snapshot["armies"][0]["troops"], [
            {"position": 1, "code": 21, "name": "弓兵"},
            {"position": 2, "code": 23, "name": "骑兵"},
            {"position": 3, "code": 0, "name": "空位"},
        ])
        self.assertEqual(snapshot["alerts"][0]["map_hero_count"], 2)
        unknown = scene(12)
        unknown[6]["army"][16] = "2,unknown;3,23;-1,-1;"
        self.state.apply(5028, unknown, 1003)
        snapshot = self.state.snapshot(1004)
        self.assertEqual(snapshot["armies"][0]["troops"], [])
        self.assertIsNone(snapshot["alerts"][0]["map_hero_count"])

    def test_unknown_union_is_not_enemy(self):
        data = self.state.snapshot(1001)
        self.assertEqual(data["armies"][0]["relation"], "unknown")
        self.assertEqual(data["alerts"], [])
        self.assertEqual(len(data["candidates"]), 1)

    def test_alert_scope_is_friendly_land_not_bookmark(self):
        self.hostile()
        self.assertEqual(len(self.state.snapshot(1001)["alerts"]), 1)
        self.state.config["targets"][0]["x"] = 1016
        self.assertEqual(len(self.state.snapshot(1001)["alerts"]), 1)
        self.state.tiles["10150926"]["union_id"] = "2000"
        self.state.tiles["10150926"]["owner"] = "200"
        self.assertEqual(self.state.snapshot(1001)["alerts"], [])

    def test_member_owned_target_with_no_tile_union_can_alert(self):
        self.hostile()
        update = scene(11)
        update[1]["201"] = ["ally owner", 0, 1909] + [0] * 9 + [[0, 0, "own union"]]
        tile = update[14]["10150926"]["0"]
        tile[2], tile[3] = 201, 0
        self.state.apply(5028, update, 1001)
        self.assertEqual([a["army_id"] for a in self.state.snapshot(1002)["alerts"]], ["army"])

        tile[3] = 2000
        self.state.apply(5028, update, 1002)
        self.assertEqual(self.state.snapshot(1002)["alerts"], [])

        tile[2], tile[3] = 202, 0
        self.state.apply(5028, update, 1003)
        self.assertEqual(self.state.snapshot(1003)["alerts"], [])

    def test_uncalibrated_state_cannot_alert(self):
        self.hostile()
        self.state.config["state_rules"] = {}
        data = self.state.snapshot(1001)
        self.assertEqual(data["alerts"], [])
        self.assertEqual(data["candidates"][0]["pending_reason"], "进攻状态待核对")

    def test_pc_force_attacks_are_alerted_individually_and_returning_is_not_pending(self):
        self.hostile()
        self.state.health["capture_source"] = "pc"
        update = scene(11)
        original = update[6]["army"]
        force_attack = original.copy()
        force_attack[0], force_attack[9] = 1, 13
        returning = original.copy()
        returning[0], returning[9] = 4, 0
        update[6] = {"first": force_attack, "second": force_attack.copy(), "returning": returning}
        update[7] = ["army"]
        self.state.apply(5028, update, 1001)
        snapshot = self.state.snapshot(1002)
        self.assertEqual([row["army_id"] for row in snapshot["alerts"]], ["first", "second"])
        self.assertEqual([(row["army_id"], row.get("pending_reason")) for row in snapshot["candidates"]],
                         [("returning", None)])
        self.state.tiles["10150926"].update(owner="200", union_id="2000")
        snapshot = self.state.snapshot(1002)
        self.assertEqual(snapshot["alerts"], [])
        self.assertFalse(any(row.get("pending_reason") for row in snapshot["candidates"]))

    def test_reconnected_army_cannot_use_old_land_ownership(self):
        self.hostile()
        self.state.gap()
        update = scene(11)
        update[14] = {}
        self.state.apply(5028, update, 1002)
        data = self.state.snapshot(1002)
        self.assertEqual(data["alerts"], [])
        self.assertEqual(len(data["candidates"]), 1)
        self.assertEqual(data["candidates"][0]["pending_reason"], "地块归属待核对")

    def test_expired_offscreen_ownership_cannot_trigger_formal_alert(self):
        self.hostile()
        update = scene(11)
        update[14] = {}
        self.state.packet(1020)
        self.state.apply(5028, update, 1020)
        self.assertEqual(self.state.snapshot(1020)["alerts"], [])

    def test_expired_and_old_observations_do_not_alert(self):
        self.hostile()
        self.assertEqual(self.state.snapshot(1101)["alerts"], [])
        self.state.connection("waiting", "test")
        self.assertEqual(self.state.snapshot(1020)["alerts"], [])
        self.assertTrue(self.state.snapshot(1061)["armies"][0]["stale"])

    def test_removed_is_out_of_view_not_dead(self):
        self.hostile()
        update = scene(11)
        update[6], update[7] = {}, ["army"]
        self.state.apply(5028, update, 1002)
        data = self.state.snapshot(1002)
        self.assertFalse(data["armies"][0]["visible"])
        self.assertEqual(data["alerts"], [])

    def test_gap_invalidates_old_rows_until_individually_refreshed(self):
        self.hostile()
        self.state.gap()
        self.assertEqual(self.state.snapshot(1002)["alerts"], [])
        self.state.apply(5028, scene(11), 1003)
        self.assertEqual(len(self.state.snapshot(1003)["alerts"]), 1)
        self.state.decode_error()
        self.assertEqual(self.state.snapshot(1003)["alerts"], [])

    def test_wrong_schema_pauses_judgement(self):
        self.hostile()
        self.state.apply(5028, [None] * 31, 1002)
        self.assertEqual(self.state.health["schema_errors"], 1)
        self.assertEqual(self.state.snapshot(1002)["alerts"], [])

    def test_old_delta_cannot_overwrite_new_snapshot(self):
        update = scene(9)
        update[6]["army"][3] = 20002000
        self.state.apply(5028, update, 1002)
        self.assertEqual(self.state.snapshot(1002)["armies"][0]["target"], [1015, 926])

    def test_stationary_position_is_not_home_fort(self):
        raw = self.state.armies["army"]
        raw.update({"from": [1009, 931], "target": [1009, 931], "reside": [1005, 935]})
        self.state.config["state_rules"][raw["signature"]] = {"kind": "garrison", "verified": True}
        self.assertEqual(self.state.snapshot(1001)["armies"][0]["position"], [1009, 931])
        raw["from"] = [1008, 931]
        self.assertIsNone(self.state.snapshot(1001)["armies"][0]["position"])

    def test_moving_position_is_not_interpolated(self):
        row = self.state.snapshot(1001)["armies"][0]
        self.assertIsNone(row["position"])
        self.assertEqual(row["position_label"], "出发坐标")

    def test_calibration_rejects_own_enemy_and_old_records(self):
        with self.assertRaises(ValueError):
            self.state.calibrate({"action": "relation", "union_id": "1909", "relation": "enemy"})
        with self.assertRaises(ValueError):
            self.state.calibrate({"action": "state", "army_id": "army", "kind": "attack"})

    def test_cache_load_is_not_live(self):
        self.state.armies["army"]["seen_at"] = time.time()
        self.state.persist()
        loaded = WorldState(self.folder)
        self.assertFalse(loaded.snapshot()["live"])
        self.assertEqual(loaded.snapshot()["alerts"], [])

    def test_watch_events_deduplicate_and_persist(self):
        self.assertEqual(len(self.state.events), 1)
        self.state.apply(5028, scene(11), 1002)
        self.assertEqual(len(self.state.events), 1)
        changed = scene(12)
        changed[6]["army"][3] = 10160926
        self.state.apply(5028, changed, 1003)
        self.assertEqual(len(self.state.events), 2)
        self.assertEqual(self.state.events[-1]["label"], "部队状态变化")
        self.state.armies["army"]["seen_at"] = time.time()
        self.state.persist()
        self.assertEqual(len(WorldState(self.folder).events), 2)

    def test_stationary_watch_observation_is_not_incoming(self):
        value = scene(11)
        value[6]["army"][2] = value[6]["army"][3]
        self.state.apply(5028, value, 1002)
        self.assertEqual(self.state.snapshot(1002)["candidates"][0]["observation"], "据点部队观测")

    def test_removed_watch_event_is_not_reported_as_arrival(self):
        value = scene(11)
        value[6], value[7] = {}, ["army"]
        self.state.apply(5028, value, 1002)
        self.assertEqual(self.state.events[-1]["label"], "离开当前观测")


    def test_unchanged_subscribed_army_stays_fresh_until_stream_gap(self):
        self.hostile()
        self.state.armies["army"]["end"] = 3000
        update = scene(11)
        update[6] = {}
        self.state.packet(1200)
        self.state.apply(5028, update, 1200)
        data = self.state.snapshot(1201)
        self.assertEqual(data["armies"][0]["age"], 1)
        self.assertEqual(len(data["alerts"]), 1)
        self.state.gap()
        self.assertTrue(self.state.snapshot(1201)["armies"][0]["stale"])

    def test_adding_watch_target_preserves_original(self):
        self.state.calibrate({"action": "target_add", "name": "test", "x": 1004, "y": 928})
        self.assertEqual(len(self.state.config["targets"]), 2)
        self.assertEqual(self.state.config["targets"][0]["x"], 1015)

    def test_state_pair_rule_checks_coordinate_constraints(self):
        self.state.config["state_rules"] = {"3:14:*": {"kind": "attack", "verified": True,
            "require_different_coordinate": True}}
        self.assertEqual(self.state.snapshot(1001)["armies"][0]["kind"], "attack")
        self.state.armies["army"]["from"] = [1015, 926]
        self.assertIsNone(self.state.snapshot(1001)["armies"][0]["kind"])

    def test_completed_baseline_retires_previous_view(self):
        value = scene(0)
        value[6] = {}
        self.state.apply(5026, value, 1002)
        self.assertTrue(self.state.armies["army"]["visible"])
        value[18] = 12
        self.state.apply(5026, value, 1003)
        self.assertFalse(self.state.armies["army"]["visible"])


class StreamTests(unittest.TestCase):
    def setUp(self):
        self.messages, self.errors, self.gaps = [], [], []
        self.framer = MessageFramer(lambda *args: self.messages.append(args), lambda: self.errors.append(1))
        self.stream = TCPStream(self.framer, lambda: self.gaps.append(1))
        body = b'{"test":1}'
        self.frame = struct.pack(">III", 9 + len(body), 5028, 0) + b'\x02' + body

    def test_split_retransmit_overlap_and_reorder(self):
        self.stream.feed(100, self.frame[:8], 1)
        self.stream.feed(115, self.frame[15:], 2)
        self.stream.feed(100, self.frame[:8], 2)
        self.stream.feed(106, self.frame[6:15], 3)
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0][1], {"test": 1})
        self.assertEqual(self.errors, [])

    def test_sequence_wraparound(self):
        self.stream.feed(0xFFFFFFFA, self.frame[:8], 1)
        self.stream.feed(2, self.frame[8:], 2)
        self.assertEqual(len(self.messages), 1)

    def test_syn_consumes_one_sequence_number(self):
        self.stream.feed(100, self.frame, 1, syn=True)
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.stream.next_seq, 101 + len(self.frame))

    def test_gap_resynchronizes_and_reports(self):
        self.stream.feed(100, self.frame[:4], 1)
        self.stream.hole_since = time.monotonic() - 4
        self.stream.feed(200, self.frame, 2)
        self.assertEqual(self.gaps, [1])
        self.assertEqual(len(self.messages), 1)

    def test_ack_and_empty_frame_are_not_decode_errors(self):
        self.framer.feed(struct.pack(">III", 8, 0, 0) + struct.pack(">III", 9, 4159, 0) + b'\x02' + self.frame, 1)
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.errors, [])

    def test_pcap_endianness_and_timestamp_precision(self):
        for endian, magic, scale in [("<", b'\xd4\xc3\xb2\xa1', 1e6), (">", b'\xa1\xb2\x3c\x4d', 1e9)]:
            header = magic + struct.pack(endian + "HHIIII", 2, 4, 0, 0, 65535, 1)
            data = header + struct.pack(endian + "IIII", 5, int(scale / 2), 3, 3) + b'abc'
            self.assertEqual(list(pcap_packets(io.BytesIO(data))), [(5.5, b'abc')])

    def test_non_tcp_is_ignored_and_fragments_rejected(self):
        self.assertIsNone(tcp_packet(b'\0' * 50))
        ip = bytearray(40)
        ip[0], ip[9], ip[2:4], ip[6:8] = 0x45, 6, b'\0(', b'\x20\0'
        with self.assertRaises(ValueError):
            tcp_packet(b'\0' * 12 + b'\x08\0' + ip)


class HttpTests(unittest.TestCase):
    def setUp(self):
        class State:
            def snapshot(self):
                return {"ok": True}
            def calibrate(self, value):
                pass
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(State(), 0))
        self.port = self.server.server_address[1]
        self.server.RequestHandlerClass = make_handler(State(), self.port)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        conn.request(method, path, body, headers or {})
        response = conn.getresponse()
        status, content = response.status, response.read()
        conn.close()
        return status, content

    def test_local_read_and_static_whitelist(self):
        self.assertEqual(self.request("GET", "/api/state")[0], 200)
        self.assertEqual(self.request("GET", "/config.json")[0], 404)
        self.assertEqual(self.request("GET", "/../../config.json")[0], 404)

    def test_host_and_origin_guards(self):
        self.assertEqual(self.request("GET", "/api/state", headers={"Host": "evil.example"})[0], 403)
        self.assertEqual(self.request("POST", "/api/calibrate", "{}", {"Content-Type": "application/json"})[0], 403)

    def test_invalid_request_does_not_break_service(self):
        headers = {"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{self.port}"}
        self.assertEqual(self.request("POST", "/api/calibrate", "[]", headers)[0], 400)
        self.assertEqual(self.request("POST", "/api/calibrate", "{}", headers)[0], 200)
        self.assertEqual(self.request("POST", "/api/calibrate", "{}", {**headers, "Content-Length": "bad"})[0], 400)


if __name__ == "__main__":
    unittest.main()
