import tempfile
import unittest
from pathlib import Path

from archive import Archive
from installed_mode import ACCOUNT_UPDATE, LOGIN_SNAPSHOT, InstalledCollector, account_update_owner, load_installation, logged_in_role
from model import WorldState, write_json


def login_snapshot(role=300, login_uid=200):
    user = [0] * 87
    user[0], user[6], user[10], user[11], user[17] = 200, "测试角色", 401, "测试盟", role
    login = [None] * 7
    login[4] = [[["Tb_user", [user]], ["Tb_user_login", [[login_uid]]]]]
    return login


class InstalledModeTests(unittest.TestCase):
    def test_fresh_profile_waits_for_verified_login_before_archiving(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            write_json(root / "install-settings.json", {"server": "测试区", "season": "第一赛季",
                "executable": "C:/Game/stzb.exe", "dumpcap": "C:/Wireshark/dumpcap.exe"})
            profile, settings = load_installation(root)
            state = WorldState(root)
            self.assertFalse(state.config["self"]["confirmed"])
            archive = Archive(root, profile=profile)
            try:
                collector = InstalledCollector(state, archive, settings)
                scene = [None] * 34
                scene[1] = {"200": ["测试角色", 300, 401] + [0] * 9 + [[0, 0, "测试盟"]]}
                scene[18] = 1
                collector._message(5026, scene, 1000)
                self.assertEqual(archive.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 0)
                collector._message(LOGIN_SNAPSHOT, login_snapshot(), 1001)
                self.assertEqual(archive.profile["account_role_id"], "300")
                self.assertEqual(archive.profile["account_name"], "测试角色")
                self.assertEqual(state.config["self"]["user_id"], "200")
                self.assertEqual(archive.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 0)
                collector._message(5026, scene, 1002)
                self.assertEqual(archive.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 1)
                collector._message(LOGIN_SNAPSHOT, login_snapshot(role=301), 1003)
                scene[1]["200"][1] = 301
                collector._message(5026, scene, 1004)
                self.assertEqual(archive.profile["account_role_id"], "300")
                self.assertTrue(state.health["identity_pending"])
                self.assertEqual(archive.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 1)
            finally:
                archive.close()

    def test_inconsistent_or_ambiguous_login_never_identifies_a_role(self):
        self.assertIsNone(logged_in_role(login_snapshot(login_uid=201)))
        snapshot = login_snapshot()
        snapshot[4][0].append(snapshot[4][0][0])
        self.assertIsNone(logged_in_role(snapshot))

    def test_already_online_matches_only_fresh_scene_for_private_account_update(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            write_json(root / "install-settings.json", {"server": "测试区", "season": "第一赛季",
                "executable": "C:/Game/stzb.exe", "dumpcap": "C:/Wireshark/dumpcap.exe"})
            profile, settings = load_installation(root)
            state = WorldState(root)
            archive = Archive(root, profile=profile)
            try:
                collector = InstalledCollector(state, archive, settings)
                scene = [None] * 34
                scene[1] = {"200": ["测试角色", 300, 401] + [0] * 9 + [[0, 0, "测试盟"]],
                            "201": ["其他角色", 301, 402] + [0] * 9 + [[0, 0, "其他盟"]]}
                scene[18] = 1
                own_update = [2, "Tb_user_res", [0, 200, 2, 100]]
                other_update = [2, "Tb_user_login", [0, 201, 2, 101]]
                self.assertIsNone(account_update_owner([own_update, other_update]))
                collector._message(5026, scene, 1000)
                collector._message(ACCOUNT_UPDATE, [own_update], 1061)
                self.assertFalse(collector.identity_ready)
                collector._message(5026, scene, 1062)
                self.assertTrue(collector.identity_ready)
                self.assertEqual(archive.profile["account_role_id"], "300")
                self.assertEqual([row[0] for row in archive.db.execute("SELECT message_id FROM messages")], [5026])
            finally:
                archive.close()
