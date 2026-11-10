import json
import shutil
import tempfile
import unittest
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from archive import Archive


def battle(bid=1, timestamp=100, hero=100701, skills=True):
    data = {"battle_id": bid, "time": timestamp, "attack_name": "player", "attack_role_id": "opaque",
        "attack_unionid": 1008, "attack_union_name": "enemy", "attack_idu": "456,11,12,13,0",
        "attack_all_hero_info": f"{hero},40,9000,8000,0;100807,40,9000,8000,0;100703,40,9000,8000,0;",
        "wid": 5000600, "wid_name": "another battlefield"}
    if skills:
        data["all_skill_info"] = "1,200001,10;2,200002,10;3,200003,10;"
    return data


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.test_root = ROOT / "runtime" / "archive-tests"
        self.test_root.mkdir(parents=True, exist_ok=True)
        self.folder = Path(tempfile.mkdtemp(prefix="case-", dir=self.test_root)).resolve()
        self.archive = Archive(self.folder)

    def tearDown(self):
        self.archive.close()
        if self.folder.parent != self.test_root.resolve() or not self.folder.name.startswith("case-"):
            raise RuntimeError("Unexpected test directory")
        shutil.rmtree(self.folder)

    def current(self):
        return self.archive.query("teams")["items"][0]

    def test_older_battle_received_later_cannot_replace_new_config(self):
        self.archive.ingest(10, [0, [battle(2, 200, 100703)]], 300)
        self.archive.ingest(92, [[battle(1, 100)]], 500)
        self.assertEqual(self.current()["data"]["heroes"][0]["id"], 100703)
        self.assertEqual(len(self.archive.query("team_history", team_key="army:456")["items"]), 2)

    def test_missing_skills_do_not_erase_previous_evidence(self):
        self.archive.ingest(10, [0, [battle()]], 300)
        self.archive.ingest(10, [0, [battle(2, 200, skills=False)]], 400)
        hero = self.current()["data"]["heroes"][0]
        self.assertEqual(hero["skills"][0]["id"], 200001)
        self.assertEqual(hero["field_times"]["skills"], 100)
        self.assertEqual(hero["field_times"]["identity"], 200)

    def test_swapped_hero_does_not_inherit_previous_hero_skills(self):
        self.archive.ingest(10, [0, [battle()]], 300)
        self.archive.ingest(10, [0, [battle(2, 200, 100841, skills=False)]], 400)
        hero = self.current()["data"]["heroes"][0]
        self.assertEqual(hero["id"], 100841)
        self.assertIsNone(hero["skills"])

    def test_arbitrary_battlefield_is_archived(self):
        self.archive.ingest(10, [0, [battle()]], 300)
        self.assertEqual(self.archive.query("battles")["items"][0]["wid"], 5000600)

    def test_season_profiles_do_not_mix(self):
        self.archive.ingest(10, [0, [battle()]], 300)
        other = Archive(self.folder, {**self.archive.profile, "season": "next-season"})
        try:
            self.assertEqual(other.summary()["counts"]["battle_reports"], 0)
            self.assertNotEqual(other.profile_id, self.archive.profile_id)
        finally:
            other.close()

    def test_business_message_excludes_session_keys(self):
        data = battle()
        data["session_token"] = "do-not-store"
        self.archive.ingest(10, [0, [data]], 300)
        raw = self.archive.query("battles")["items"][0]["data"]
        self.assertNotIn("session_token", raw)

    def test_unbound_formation_is_not_claimed_as_exact_army(self):
        data = battle()
        data["attack_idu"] = "0,11,12,13,0"
        self.archive.ingest(10, [0, [data]], 300)
        self.assertEqual(self.current()["binding"], "unbound_formation")
        self.assertEqual(self.archive.summary()["counts"]["bound_teams"], 0)

    def test_world_march_preserves_source_duration_and_metric_label(self):
        value = [None] * 34
        row = [0] * 33
        row[0], row[1], row[2], row[3], row[4], row[5] = 1, 10, 5000600, 5300640, 100, 400
        row[16] = "0,3,3,0"
        value[6] = {"456": row}
        self.archive.ingest(5028, value, 200)
        march = self.archive.query("marches")["items"][0]
        self.assertEqual(march["duration_seconds"], 300)
        self.assertEqual(march["straight_distance"], 50)
        self.assertEqual(march["straight_grids_per_hour"], 600)

    def test_data_survives_reopening(self):
        self.archive.ingest(10, [0, [battle()]], 300)
        self.archive.close()
        self.archive = Archive(self.folder)
        self.assertEqual(self.archive.summary()["counts"]["battle_reports"], 1)

    def test_importing_older_source_preserves_field_observation_range(self):
        self.archive.ingest(92, [[battle()]], 500, source="pc-live")
        self.archive.ingest(92, [[battle()]], 100, source="capture")
        row = self.archive.db.execute("SELECT MIN(first_seen),MAX(last_seen) FROM message_fields WHERE message_id=92").fetchone()
        self.assertEqual(tuple(row), (100, 500))

    def test_legacy_cache_is_idempotent_and_cannot_cross_seasons(self):
        cache = self.folder / "legacy.json"
        cache.write_text(json.dumps({"profile_id": self.archive.profile_id,
            "players": {"10": {"name": "player", "union_id": "1008"}},
            "armies": {"456": {"owner": "10", "from": [500,600], "target": [530,640],
                "begin": 100, "end": 400, "seen_at": 200, "state_code": 1}}}), encoding="utf-8")
        self.archive.seed_cache(cache)
        self.archive.seed_cache(cache)
        self.assertEqual(self.archive.summary()["counts"]["world_armies"], 1)
        self.assertEqual(self.archive.summary()["counts"]["march_samples"], 1)
        self.archive.activate_profile({**self.archive.profile, "season": "next"})
        self.archive.seed_cache(cache)
        self.assertEqual(self.archive.summary()["counts"]["world_armies"], 0)

    def test_roster_evidence_is_not_downgraded_by_scene_updates(self):
        with self.archive.lock, self.archive.db:
            self.archive._player("world:10", "player", "1008", "enemy", 100, "alliance_roster", uid="10")
            self.archive._player("world:10", "player", "1008", "enemy", 200, "world_scene", uid="10")
        evidence = self.archive.db.execute("SELECT source FROM roster_observations WHERE player_key='world:10'").fetchone()
        self.assertEqual(evidence[0], "alliance_roster")

    def test_same_name_lineup_candidates_are_explicitly_uncertain(self):
        data = battle()
        data["attack_idu"] = "0,11,12,13,0"
        self.archive.ingest(92, [[data]], 300)
        value = [None] * 34
        value[1] = {"10": ["player", 99, 1008] + [0]*9 + [[0,0,"enemy"]]}
        raw = [0] * 33
        raw[0], raw[1], raw[2], raw[3] = 1, 10, 5000600, 5300640
        value[6] = {"456": raw}
        self.archive.ingest(5028, value, 400)
        answer = self.archive.query("army_lineups", army_id="456")
        self.assertEqual(answer["match"], "name_union_candidates")
        self.assertEqual(answer["items"][0]["binding"], "unbound_formation")

    def observe_troops(self, raw_type, at=300):
        value = [None] * 34
        value[1] = {"10": ["player", 99, 1008] + [0]*9 + [[0,0,"enemy"]]}
        raw = [0] * 33
        raw[0], raw[1], raw[2], raw[3] = 1, 10, 5000600, 5300640
        raw[16] = raw_type
        value[6] = {"456": raw}
        self.archive.ingest(5028, value, at)

    def test_troop_candidates_keep_distinct_historical_lineups_and_compare_slots(self):
        code = "0,21,32,3,"
        for bid, hero in enumerate((100701, 100807, 100020, 100024, 100841), 1):
            row = battle(bid, 100+bid, hero)
            # Several lineups were played by the same other army at different times.
            row.update(attack_idu="789,11,12,13,0", attack_hero_type=code)
            self.archive.ingest(92, [[row]], 200+bid)
        repeat = battle(6, 106, 100841)
        repeat.update(attack_idu="789,11,12,13,0", attack_hero_type=code)
        self.archive.ingest(92, [[repeat]], 206)
        wrong_order = battle(7, 107, 100035)
        wrong_order.update(attack_idu="0,11,12,13,0", attack_hero_type="0,32,21,3,")
        self.archive.ingest(92, [[wrong_order]], 207)
        other_union = battle(8, 108, 100123)
        other_union.update(attack_unionid=1007, attack_idu="0,11,12,13,0", attack_hero_type=code)
        self.archive.ingest(92, [[other_union]], 208)
        future = battle(9, 400, 100138)
        future.update(attack_idu="0,11,12,13,0", attack_hero_type=code)
        self.archive.ingest(92, [[future]], 401)
        self.observe_troops("2,21;2,32;3,3;")

        result = self.archive.query("army_lineups", army_id="456")
        self.assertEqual(result["match"], "name_union_troop_candidate")
        self.assertEqual([row["battle_id"] for row in result["items"]], ["6", "4", "3", "2", "1"])
        self.assertEqual([t["name"] for t in result["troops"]], ["弓兵", "步兵", "骑兵"])

    def test_defender_troops_are_reversed_and_family_match_is_weaker_than_exact_army(self):
        row = {"battle_id": 1, "time": 100, "wid": 5000600, "defend_name": "player",
            "defend_unionid": 1008, "defend_role_id": "opaque", "defend_idu": "0,11,12,13,0",
            "defend_hero_type": "3,32,21,0,", "defend_all_hero_info": "100701,40,9000,8000,0;100807,40,9000,8000,0;100703,40,9000,8000,0;"}
        self.archive.ingest(92, [[row]], 200)
        self.observe_troops("2,21;2,32;3,3;")
        result = self.archive.query("army_lineups", army_id="456")
        self.assertEqual(result["match"], "name_union_troop_candidate")
        self.assertEqual([t["code"] for t in result["items"][0]["report_troops"]], [21, 32, 3])
        self.observe_troops("1,11;2,22;3,23;", 310)
        self.assertEqual(self.archive.query("army_lineups", army_id="456")["match"], "name_union_troop_family_candidate")
        self.observe_troops("3,3;3,3;-1,-1;", 320)
        result = self.archive.query("army_lineups", army_id="456")
        self.assertEqual((result["match"], result["items"]), ("no_troop_match", []))

        self.archive.ingest(92, [[battle(2, 250, 100020)]], 321)
        result = self.archive.query("army_lineups", army_id="456")
        self.assertEqual(result["match"], "exact_army_id")
        self.assertEqual([item["battle_id"] for item in result["items"]], ["2"])

    def test_troop_changes_produce_a_new_configuration(self):
        data = battle()
        data["attack_hero_type"] = "0,1,1,1"
        self.archive.ingest(92, [[data]], 300)
        first = self.current()["configuration_hash"]
        data.update(battle_id=2, time=200, attack_hero_type="0,2,2,2")
        self.archive.ingest(92, [[data]], 400)
        self.assertNotEqual(first, self.current()["configuration_hash"])

    def test_defender_skill_slots_follow_full_report_header_order(self):
        data = {"battle_id": 3574463, "time": 200, "defend_name": "defender",
            "defend_role_id": "defender-role", "defend_unionid": 1008,
            "defend_idu": "0,974956,974943,974953,0",
            "defend_all_hero_info": "100020,40,9400,9400,0;100707,40,10000,10000,0;100769,40,9400,9400,0;",
            "all_skill_info": "4,200993,10;5,200989,10;6,200020,10;"}
        self.archive.ingest(92, [[data]], 300)
        heroes = self.current()["data"]["heroes"]
        self.assertEqual([(h["id"],h["skills"][0]["id"]) for h in heroes],
            [(100020,200020),(100707,200989),(100769,200993)])

    def test_zero_skill_placeholders_are_unknown_and_keep_last_evidence(self):
        self.archive.ingest(92, [[battle()]], 300)
        data = battle(2, 200)
        data["all_skill_info"] = "1,0,0,0,0,0,0;2,0,0,0,0,0,0;3,0,0,0,0,0,0;"
        self.archive.ingest(92, [[data]], 400)
        hero = self.current()["data"]["heroes"][0]
        self.assertEqual(hero["skills"][0]["id"], 200001)
        self.assertEqual(hero["field_times"]["skills"], 100)

    def own_link_sample(self, bid=1, timestamp=100, hero=100701):
        (self.folder / "config.json").write_text(json.dumps({"self": {"confirmed": True, "user_id": "71021",
            "name": self.archive.profile["account_name"], "union_id": "1909"}}), encoding="utf-8")
        data = battle(bid, timestamp, hero)
        data.update(attack_name=self.archive.profile["account_name"], attack_unionid=1909, attack_idu="0,11,12,13,0")
        raw = [0] * 33
        raw[0], raw[1], raw[2], raw[3] = 1, 71021, 5000600, 5000600
        scene = [None] * 34
        scene[6] = {"79000213": raw}
        self.archive.ingest(5028, scene, 200)
        index = [bid, 71021, 0, 79000213, 90, 0, 1, 5000600, 0, 0, f"{hero},100807,100703", "", 0, 0, 0]
        return data, [1, "Tb_battle_report_attack", index]

    def test_own_report_index_links_exact_army_after_report_and_preserves_source(self):
        data, index = self.own_link_sample()
        self.archive.ingest(10, [0, [data]], 300)
        unbound = self.current()["team_key"]
        self.archive.ingest(90005, [index], 400)
        exact = self.archive.query("army_lineups", army_id="79000213")
        self.assertEqual(exact["match"], "exact_army_id")
        self.assertEqual(exact["items"][0]["data"]["binding_evidence"]["world_uid"], "71021")
        self.assertEqual(self.archive.summary()["counts"]["bound_teams"], 1)
        self.assertEqual(self.archive.summary()["counts"]["unbound_formations"], 0)
        self.assertEqual(len(self.archive.query("team_history", team_key=unbound)["items"]), 1)
        saved = self.archive.query("battles")["items"][0]["data"]
        self.assertEqual(saved["attack_idu"], "0,11,12,13,0")

    def test_index_before_report_and_swap_use_battle_event_time(self):
        newer, new_index = self.own_link_sample(2, 200, 100841)
        older, old_index = self.own_link_sample(1, 100)
        self.archive.ingest(90005, [new_index, old_index], 250)
        self.archive.ingest(10, [0, [newer]], 300)
        self.archive.ingest(10, [0, [older]], 400)
        current = self.archive.query("army_lineups", army_id="79000213")["items"][0]
        self.assertEqual(current["data"]["heroes"][0]["id"], 100841)
        self.assertEqual(current["event_at"], 200)
        self.assertEqual(len(self.archive.query("team_history", team_key="army:79000213")["items"]), 2)

    def test_link_rejects_wrong_owner_hero_coordinate_or_identity(self):
        for field, value in ((1, 999), (7, 5000601), (10, "100841,100807,100703")):
            data, index = self.own_link_sample()
            index[2][field] = value
            self.archive.ingest(90005, [index], 300)
            self.archive.ingest(10, [0, [data]], 400)
            self.assertEqual(self.archive.summary()["counts"]["bound_teams"], 0)
        data, index = self.own_link_sample(bid=2)
        data["attack_name"] = "another player"
        self.archive.ingest(90005, [index], 500)
        self.archive.ingest(10, [0, [data]], 600)
        self.assertEqual(self.archive.summary()["counts"]["bound_teams"], 0)

    def test_unknown_table_layout_is_saved_without_guessed_binding(self):
        data, index = self.own_link_sample()
        index[2].append(0)
        self.archive.ingest(90005, [index, [2, "Tb_hero", [0, 123, 71, "unverified-field"]]], 300)
        self.archive.ingest(10, [0, [data]], 400)
        self.assertEqual(self.archive.summary()["counts"]["bound_teams"], 0)
        self.assertEqual(self.archive.db.execute("SELECT COUNT(*) FROM client_table_events").fetchone()[0], 2)
        self.assertEqual(self.archive.db.execute("SELECT COUNT(*) FROM own_report_indices").fetchone()[0], 0)

if __name__ == "__main__":
    unittest.main()
