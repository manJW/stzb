"""Account-scoped, passive collection for the local Windows installation."""
import json
import os
import re
import sqlite3
import time
from pathlib import Path

from archive import table_updates
from collector import Collector
from model import default_config, write_json
from observation_codec import digest
from season_seed import merge_seed

LOGIN_SNAPSHOT = 99991
ACCOUNT_UPDATE = 90005
ROLE_DETAIL = 3686
IDENTITY_WINDOW = 60
IDENTITY_WAIT = "等待读取当前登录角色。已在游戏中时，可以打开自己的个人主页完成核对。"
SCOPE_WAIT = "正在核对区服和赛季。若已在地图，可在游戏中打开自己的个人主页补读。"
WINDOWS_EPOCH_TICKS = 621355968000000000


def load_installation(root):
    settings = json.loads((root / "install-settings.json").read_text(encoding="utf-8-sig"))
    for key in ("executable", "dumpcap"):
        if not isinstance(settings.get(key), str) or not settings[key].strip():
            raise ValueError("首次设置缺少必要信息")
    if settings.get("distribution_v2"):
        from portable_mode import prepare_installation,waiting_installation
        try:return prepare_installation(root, settings,timeout=0)
        except (RuntimeError,ValueError,OSError)as error:return waiting_installation(root,settings,error)
    legacy = all(settings.get(key) for key in ("server", "season"))
    profile = {key: settings[key] for key in ("server", "season")} if legacy else {
        "server": "等待读取区服", "season": "等待读取赛季"}
    profile.update(account_name="等待读取角色", account_role_id="0", union_id="0", union_name="待核对")
    profile_path = root / "archive-profile.json"
    if profile_path.exists():
        saved = json.loads(profile_path.read_text(encoding="utf-8-sig"))
        if legacy and any(saved[key] != profile[key] for key in ("server", "season")):
            raise ValueError("区服或赛季与已有档案不同，请使用新的本机工作目录")
        profile = saved
    config_path = root / "config.json"
    if not config_path.exists():
        config = default_config()
        config["self"] = {"name": profile["account_name"], "user_id": "0", "union_id": "0",
                          "union_name": "待核对", "confirmed": False}
        config["state_rules"]["1:0:*"] = {"kind": "attack", "verified": True,
                                            "require_different_coordinate": True}
        write_json(config_path, config)
    return profile, settings


def logged_in_role(value):
    """Read only the matching user and login rows from the PC login snapshot."""
    if not isinstance(value, list) or len(value) != 7:
        return None
    snapshot = value[4]
    if not isinstance(snapshot, list) or not snapshot or not isinstance(snapshot[0], list):
        return None
    rows = {}
    for table in snapshot[0]:
        if not isinstance(table, list) or len(table) != 2 or table[0] not in ("Tb_user", "Tb_user_login", "Tb_season_user"):
            continue
        name, records = table
        if name == "Tb_season_user" and records == []:
            continue
        if name in rows or not isinstance(records, list) or len(records) != 1 or not isinstance(records[0], list):
            return None
        rows[name] = records[0]
    user, login = rows.get("Tb_user", []), rows.get("Tb_user_login", [])
    if len(user) < 18 or not login:
        return None
    uid, name, union, union_name, role = (user[i] for i in (0, 6, 10, 11, 17))
    if (type(uid) is not int or uid <= 0 or type(role) is not int or role <= 0
            or type(login[0]) is not int or login[0] != uid or type(union) is not int or union < 0
            or not isinstance(name, str) or not name.strip()
            or (not isinstance(union_name, str) and (union or union_name not in (None, 0)))):
        return None
    identity = {"user_id": str(uid), "account_name": name, "account_role_id": str(role),
                "union_id": str(union), "union_name": union_name or ("无同盟" if not union else "未识别同盟")}
    season = rows.get("Tb_season_user", [])
    if len(season) > 1 and season[0] == uid:
        identity["scope"] = archive_scope(value[5], season[1])
    return identity


def archive_scope(server_id, season_id, server_name=None):
    if type(server_id) is not int or server_id <= 0 or type(season_id) is not int or season_id <= 0:
        return None
    return {"server": f"server-{server_id}", "season": f"season-{season_id}", "season_label": f"赛季 {season_id}",
            **({"server_label": server_name.strip()} if isinstance(server_name, str) and server_name.strip() else {})}


def role_detail_scope(value):
    if not isinstance(value, list) or len(value) != 2 or not isinstance(value[1], dict):
        return None
    person, server = value[1].get("personal"), value[1].get("server")
    if not isinstance(person, list) or len(person) <= 41 or not isinstance(server, list) or len(server) != 4:
        return None
    uid, role = person[26], person[41]
    if type(uid) is not int or uid <= 0 or type(role) is not int or role <= 0:
        return None
    scope = archive_scope(server[2], server[3], server[0])
    return (str(uid), str(role), scope) if scope else None


def account_update_owner(value):
    """Identify the owner of the connection's private resource and login updates."""
    owners = {body[1] for operation, table, body in table_updates(value)
              if table in ("Tb_user_res", "Tb_user_login") and operation == 2
              and len(body) >= 4 and len(body) % 2 == 0 and body[0] == 0
              and type(body[1]) is int and body[1] > 0}
    return str(owners.pop()) if len(owners) == 1 else None


def scene_role(value, uid):
    if not isinstance(value, list) or len(value) != 34 or not isinstance(value[1], dict):
        return None
    row = value[1].get(uid)
    if not isinstance(row, list) or len(row) < 13:
        return None
    name, role, union = row[:3]
    if (not isinstance(name, str) or not name.strip() or type(role) is not int or role <= 0
            or type(union) is not int or union < 0):
        return None
    details = row[12]
    union_name = details[2] if isinstance(details, list) and len(details) > 2 else None
    return {"user_id": uid, "account_name": name, "account_role_id": str(role), "union_id": str(union),
            "union_name": union_name if isinstance(union_name, str) and union_name else
            ("无同盟" if not union else "未识别同盟")}


def connection_scope_key(connection):
    if not connection.get("connection_started"):
        return None
    return digest([connection.get(key) for key in (
        "pid", "connection_started", "local_port", "local", "peer", "executable")])


def client_role_marker(connection):
    """Read only the current role marker and scene timestamp from game preferences."""
    started = connection.get("connection_started")
    appdata = os.environ.get("LOCALAPPDATA")
    if type(started) is not int or started <= WINDOWS_EPOCH_TICKS or not appdata:
        return None
    try:
        config = json.loads((Path(appdata) / "stzb" / "user_default.json").read_text(encoding="utf-8-sig"))
        entered_at = int(config["_key_last_enter_main_scene_time_"])
        marker = config["_key_resfile_login_roleid"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if (not isinstance(marker, str) or not marker.strip() or len(marker) > 100
            or not (started - WINDOWS_EPOCH_TICKS) / 10_000_000 - 2 <= entered_at <= time.time() + 30
            or config.get("CommunityLastRoleid", marker) != marker):
        return None
    return marker


def own_profile_identity(value, detail, marker):
    if not marker:
        return None
    profile = value[1]
    log, union = profile.get("log"), profile.get("union")
    if (not isinstance(log, dict) or log.get("role_id") != marker
            or not isinstance(union, list) or len(union) < 4 or type(union[2]) is not int or union[2] < 0):
        return None
    name = profile["personal"][0]
    if not isinstance(name, str) or not name.strip() or name != log.get("role_name"):
        return None
    name = re.sub(r"#\d+$", "", name).strip()
    if not name:
        return None
    uid, role, scope = detail
    union_name = union[3] if isinstance(union[3], str) and union[3].strip() else ("未识别同盟" if union[2] else "无同盟")
    return {"user_id": uid, "account_role_id": role, "account_name": name, "union_id": str(union[2]),
            "union_name": union_name, "scope": scope}


class InstalledCollector(Collector):
    def __init__(self, state, archive, settings, seed_path=None):
        super().__init__(state, archive, {**settings, "mode": settings.get("mode", "pc")})
        self.seed_path = seed_path
        self.loaded_seed_profile = None
        self.auto_scope = not all(settings.get(key) for key in ("server", "season"))
        self.incoming_ids = {10, 11, 92, 103, 301, 700, 5026, 5028, 6231, 6243, ACCOUNT_UPDATE, LOGIN_SNAPSHOT, ROLE_DETAIL}
        self.identity_ready = False
        self.connection_key = self.cached_scope = None
        self.connection = {}
        self.scope_path = state.root / "runtime" / "verified-scope.json"
        self.state.health["local_install"] = True
        self._wait_for_identity()

    def capture(self, connection):
        self._prepare_connection(connection)
        super().capture(connection)

    def _prepare_connection(self, connection):
        self._wait_for_identity()
        self.connection = connection
        self.connection_key = connection_scope_key(connection)
        self.cached_scope = self._read_connection_scope()
        if not self.cached_scope or not self.cached_scope.get("login_marker"):
            return
        marker = client_role_marker(connection)
        own = self.state.config["self"]
        profile = self.archive.profile
        if (marker and digest(marker) == self.cached_scope["login_marker"] and own.get("confirmed")
                and own.get("user_id") == self.cached_scope.get("user_id")
                and profile["account_role_id"] == self.cached_scope.get("account_role_id")):
            self.verified_marker = digest(marker)
            identity = {key: profile[key] for key in ("account_role_id", "account_name", "union_id", "union_name")}
            self._confirm_identity({**identity, "user_id": own["user_id"]},
                                   "本机角色标记与当前游戏连接的已核对档案一致", time.time())

    def _read_connection_scope(self):
        if not self.auto_scope or not self.connection_key:
            return None
        try:
            record = json.loads(self.scope_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if (isinstance(record, dict) and record.get("connection") == self.connection_key
                and record.get("profile_id") == self.archive.profile_id):
            return record
        return None

    def _message(self, mid, value, observed_at):
        if mid == LOGIN_SNAPSHOT:
            identity = logged_in_role(value)
            if identity is None:
                self._wait_for_identity("未能核对登录角色，请回角色选择后重新进入地图。")
            else:
                if self.auto_scope:
                    self.archive_ready = False
                    with self.state.lock:
                        self.state.health["scope_pending"] = True
                self._confirm_identity(identity, "PC 客户端登录信息中的角色编号与本人记录一致", observed_at)
            return
        if mid == ROLE_DETAIL:
            detail = role_detail_scope(value)
            if detail:
                marker = client_role_marker(self.connection)
                identity = own_profile_identity(value, detail, marker)
                if identity:
                    self.verified_marker = digest(marker)
                    self._confirm_identity(identity, "游戏个人主页与本机当前登录的角色标记一致", observed_at)
                    return
                if not self.auto_scope:
                    return
                self.pending_detail = (detail, observed_at)
                self._use_role_detail(observed_at)
            return
        if mid == ACCOUNT_UPDATE:
            if not self.identity_ready:
                uid = account_update_owner(value)
                if uid:
                    self.pending_owner = (uid, observed_at)
                    if self.pending_scene:
                        self._use_pending_scene(*self.pending_scene)
            return
        if not self.identity_ready and mid in (5026, 5028):
            if mid == 5026 and isinstance(value, list) and len(value) == 34 and value[1]:
                self.pending_scene = (mid, value, observed_at)
            self._use_pending_scene(mid, value, observed_at)
        elif self.identity_ready:
            self._observe(mid, value, observed_at)

    def _observe(self, mid, value, observed_at):
        if self.archive_ready:
            super()._message(mid, value, observed_at)
        elif mid in (5026, 5028):
            self.state.apply(mid, value, observed_at)

    def _use_role_detail(self, observed_at):
        if not self.identity_ready or not self.pending_detail:
            return
        (uid, role, scope), seen_at = self.pending_detail
        if (abs(observed_at - seen_at) <= IDENTITY_WINDOW and uid == self.current_identity["user_id"]
                and role == self.current_identity["account_role_id"]):
            if self.archive_ready and any(self.archive.profile[key] != scope[key] for key in ("server", "season")):
                return
            self._bind_scope(scope)

    def _use_pending_scene(self, mid, value, observed_at):
        if self.pending_owner is None:
            return
        uid, owner_at = self.pending_owner
        if abs(observed_at - owner_at) > IDENTITY_WINDOW:
            return
        identity = scene_role(value, uid)
        if identity and self._confirm_identity(identity, "本人资料更新与地图上的角色编号一致", observed_at):
            self._observe(mid, value, observed_at)

    def _wait_for_identity(self, message=IDENTITY_WAIT):
        if self.identity_ready:
            self.state.gap()
        self.identity_ready = False
        self.archive_ready = False
        self.verified_marker = None
        self.current_identity = self.pending_owner = self.pending_scene = self.pending_detail = None
        with self.state.lock:
            self.state.health.update(identity_pending=True, identity_message=message, scope_pending=self.auto_scope)

    def _confirm_identity(self, identity, evidence, observed_at):
        profile = self.archive.profile
        role = identity["account_role_id"]
        if profile["account_role_id"] not in ("0", role):
            self._wait_for_identity("当前登录角色与此档案不同，请切回已绑定角色。")
            return False
        own = {"user_id": identity["user_id"], "name": identity["account_name"], "union_id": identity["union_id"],
               "union_name": identity["union_name"], "confirmed": True, "evidence": evidence}
        with self.state.lock:
            previous = self.state.config['self']
            if (str(previous.get('user_id')) != own['user_id'] or str(previous.get('union_id')) != own['union_id']):
                self.state.config['relations'] = {}
                self.state.config['targets'] = []
            if self.state.config["self"] != own:
                self.state.config["self"] = own
                write_json(self.state.config_path, self.state.config)
            self.state.health["identity_pending"] = False
            self.state.health.pop("identity_message", None)
        self.identity_ready = True
        self.current_identity = identity
        self.pending_owner = self.pending_scene = None
        scope = identity.get("scope")
        if not scope and self.cached_scope and all(self.cached_scope.get(key) == identity[key]
                                                    for key in ("user_id", "account_role_id")):
            profile = self.archive.profile
            if profile["account_role_id"] == identity["account_role_id"]:
                scope = {key: profile[key] for key in ("server", "season", "season_label", "server_label") if key in profile}
        if not self.auto_scope or scope:
            self._bind_scope(scope if self.auto_scope else {})
        if self.auto_scope:
            self._use_role_detail(observed_at)
        return self.identity_ready

    def _bind_scope(self, scope):
        profile = self.archive.profile
        if (self.auto_scope and profile["account_role_id"] != "0"
                and any(profile[key] != scope[key] for key in ("server", "season"))):
            self._wait_for_identity("当前区服或赛季与此档案不同，已暂停入库。")
            return
        fields = {key: self.current_identity[key] for key in ("account_role_id", "account_name", "union_id", "union_name")}
        changes = {**scope, **fields}
        if any(profile.get(key) != value for key, value in changes.items()):
            self.archive.activate_profile({**profile, **changes})
        if self.seed_path and self.loaded_seed_profile != self.archive.profile_id:
            self._load_season_seed()
        self.archive_ready = True
        with self.state.lock:
            self.state.health["scope_pending"] = False
        if self.auto_scope and self.connection_key:
            record = {"connection": self.connection_key, "user_id": self.current_identity["user_id"],
                      "account_role_id": self.current_identity["account_role_id"], "profile_id": self.archive.profile_id}
            if self.verified_marker:
                record["login_marker"] = self.verified_marker
            if record != self.cached_scope:
                write_json(self.scope_path, record)
                self.cached_scope = record

    def _load_season_seed(self):
        try:
            manifest = merge_seed(self.archive, self.seed_path)
            if manifest and manifest["union_id"] == self.archive.profile["union_id"]:
                with self.state.lock:
                    relations = self.state.config["relations"]
                    new_relations = {key: value for key, value in manifest["relations"].items()
                                     if key not in relations and key != manifest["union_id"]}
                    if new_relations:
                        config = {**self.state.config, "relations": {**relations, **new_relations}}
                        write_json(self.state.config_path, config)
                        self.state.config["relations"].update(new_relations)
        except (OSError, ValueError, sqlite3.Error) as exc:
            print(f"赛季档案导入失败：{exc}", flush=True)
            return
        self.loaded_seed_profile = self.archive.profile_id
