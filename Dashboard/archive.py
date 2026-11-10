"""Season-scoped observations, immutable evidence and current team configurations."""
import json
import math
import sqlite3
import threading
import time
import zlib
from pathlib import Path

from combat_loadout import named_hero, side_heroes
from observation_codec import (clean, coord, describe_troops, dictionaries, digest, integers,
    map_troop_codes, number, packed, report_troop_codes, troop_families)


def table_updates(value):
    if isinstance(value, list):
        if len(value) == 3 and type(value[0]) is int and isinstance(value[1], str) and value[1].startswith("Tb_") and isinstance(value[2], list):
            yield value
        else:
            for child in value:
                yield from table_updates(child)
    elif isinstance(value, dict):
        for child in value.values():
            yield from table_updates(child)


class Archive:
    def __init__(self, root, profile=None, database_path=None):
        self.root = Path(root)
        self.lock = threading.RLock()
        (self.root / "data").mkdir(parents=True, exist_ok=True)
        self.profile_path = self.root / "archive-profile.json"
        if profile is None and self.profile_path.exists():
            profile = json.loads(self.profile_path.read_text(encoding="utf-8-sig"))
        self.profile = profile or {"server": "X0000", "season": "example-season",
            "season_label": "当前赛季（赛季编号待核对）", "account_role_id": "7900021",
            "account_name": "示例角色", "union_id": "1909", "union_name": "示例同盟"}
        if not self.profile_path.exists():
            self.profile_path.write_text(json.dumps(self.profile, ensure_ascii=False, indent=2), encoding="utf-8")
        self.profile_id = digest([self.profile.get(k) for k in ("server", "season", "account_role_id")])[:24]
        self.database_path = Path(database_path) if database_path else self.root / "data" / "intelligence.sqlite3"
        self.db = sqlite3.connect(self.database_path, check_same_thread=False, timeout=15)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS profiles(id TEXT PRIMARY KEY, data TEXT NOT NULL, created_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS messages(
          id TEXT PRIMARY KEY, profile_id TEXT NOT NULL, message_id INTEGER NOT NULL,
          first_seen REAL NOT NULL, last_seen REAL NOT NULL, receipts INTEGER NOT NULL,
          source TEXT NOT NULL, payload BLOB NOT NULL);
        CREATE TABLE IF NOT EXISTS message_fields(
          profile_id TEXT NOT NULL,message_id INTEGER NOT NULL,path TEXT NOT NULL,type TEXT NOT NULL,
          first_seen REAL NOT NULL,last_seen REAL NOT NULL,PRIMARY KEY(profile_id,message_id,path,type));
        CREATE TABLE IF NOT EXISTS players(
          profile_id TEXT NOT NULL,player_key TEXT NOT NULL,world_uid TEXT,role_id TEXT,name TEXT NOT NULL,
          union_id TEXT NOT NULL,union_name TEXT,source TEXT NOT NULL,event_at REAL NOT NULL,observed_at REAL NOT NULL,
          PRIMARY KEY(profile_id,player_key));
        CREATE TABLE IF NOT EXISTS unions(
          profile_id TEXT NOT NULL,union_id TEXT NOT NULL,name TEXT,expected_members INTEGER,
          observed_at REAL,PRIMARY KEY(profile_id,union_id));
        CREATE TABLE IF NOT EXISTS roster_observations(
          profile_id TEXT NOT NULL,union_id TEXT NOT NULL,player_key TEXT NOT NULL,
          source TEXT NOT NULL,observed_at REAL NOT NULL,PRIMARY KEY(profile_id,union_id,player_key));
        CREATE TABLE IF NOT EXISTS army_states(
          id TEXT PRIMARY KEY,profile_id TEXT NOT NULL,army_id TEXT NOT NULL,owner_uid TEXT,
          state_code INTEGER,state_id INTEGER,from_x INTEGER,from_y INTEGER,to_x INTEGER,to_y INTEGER,
          begin_at REAL,end_at REAL,hero_type TEXT,observed_at REAL NOT NULL,message_ref TEXT,
          data TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS army_history ON army_states(profile_id,army_id,observed_at);
        CREATE TABLE IF NOT EXISTS army_current(
          profile_id TEXT NOT NULL,army_id TEXT NOT NULL,state_ref TEXT NOT NULL,observed_at REAL NOT NULL,
          PRIMARY KEY(profile_id,army_id));
        CREATE TABLE IF NOT EXISTS march_samples(
          id TEXT PRIMARY KEY,profile_id TEXT NOT NULL,army_id TEXT NOT NULL,state_code INTEGER,
          from_x INTEGER,from_y INTEGER,to_x INTEGER,to_y INTEGER,begin_at REAL,end_at REAL,
          straight_distance REAL,duration_seconds REAL,straight_grids_per_hour REAL,
          hero_type TEXT,observed_at REAL,message_ref TEXT);
        CREATE INDEX IF NOT EXISTS march_history ON march_samples(profile_id,army_id,begin_at);
        CREATE TABLE IF NOT EXISTS tiles(
          id TEXT PRIMARY KEY,profile_id TEXT NOT NULL,wid INTEGER NOT NULL,observed_at REAL,
          owner_uid TEXT,union_id TEXT,name TEXT,data TEXT,message_ref TEXT);
        CREATE TABLE IF NOT EXISTS battle_reports(
          profile_id TEXT NOT NULL,battle_id TEXT NOT NULL,event_at REAL NOT NULL,observed_at REAL NOT NULL,
          wid INTEGER,place_name TEXT,data TEXT NOT NULL,message_ref TEXT,
          PRIMARY KEY(profile_id,battle_id));
        CREATE INDEX IF NOT EXISTS battle_place_time ON battle_reports(
          profile_id,wid,event_at DESC,CAST(battle_id AS INTEGER) DESC);
        CREATE TABLE IF NOT EXISTS report_bodies(
          profile_id TEXT NOT NULL,battle_id TEXT NOT NULL,body TEXT NOT NULL,observed_at REAL,message_ref TEXT,
          PRIMARY KEY(profile_id,battle_id));
        CREATE TABLE IF NOT EXISTS report_briefs(
          profile_id TEXT NOT NULL,battle_id TEXT NOT NULL,body TEXT NOT NULL,observed_at REAL NOT NULL,message_ref TEXT,
          PRIMARY KEY(profile_id,battle_id));
        CREATE TABLE IF NOT EXISTS lineup_forecasts(
          profile_id TEXT NOT NULL,march_key TEXT NOT NULL,issued_at REAL NOT NULL,army_id TEXT NOT NULL,
          from_x INTEGER NOT NULL,from_y INTEGER NOT NULL,to_x INTEGER NOT NULL,to_y INTEGER NOT NULL,
          begin_at REAL NOT NULL,end_at REAL NOT NULL,state_code INTEGER NOT NULL,model_version TEXT NOT NULL,
          fingerprint TEXT NOT NULL,data TEXT NOT NULL,PRIMARY KEY(profile_id,march_key,issued_at));
        CREATE TABLE IF NOT EXISTS team_versions(
          id TEXT PRIMARY KEY,profile_id TEXT NOT NULL,team_key TEXT NOT NULL,player_key TEXT NOT NULL,
          player_name TEXT,union_id TEXT,union_name TEXT,binding TEXT NOT NULL,
          event_at REAL NOT NULL,observed_at REAL NOT NULL,battle_id TEXT,
          configuration_hash TEXT NOT NULL,data TEXT NOT NULL,message_ref TEXT);
        CREATE INDEX IF NOT EXISTS team_version_history ON team_versions(profile_id,team_key,event_at);
        CREATE TABLE IF NOT EXISTS team_current(
          profile_id TEXT NOT NULL,team_key TEXT NOT NULL,version_ref TEXT NOT NULL,
          event_at REAL NOT NULL,observed_at REAL NOT NULL,PRIMARY KEY(profile_id,team_key));
        CREATE TABLE IF NOT EXISTS observations(
          id TEXT PRIMARY KEY,profile_id TEXT NOT NULL,kind TEXT NOT NULL,entity_key TEXT,
          observed_at REAL NOT NULL,event_at REAL,data TEXT NOT NULL,message_ref TEXT);
        CREATE INDEX IF NOT EXISTS observed_time ON observations(profile_id,observed_at);
        CREATE TABLE IF NOT EXISTS client_table_events(
          id TEXT PRIMARY KEY,profile_id TEXT NOT NULL,table_name TEXT NOT NULL,operation INTEGER NOT NULL,
          record_key TEXT,body TEXT NOT NULL,observed_at REAL NOT NULL,message_ref TEXT);
        CREATE INDEX IF NOT EXISTS client_table_history ON client_table_events(profile_id,table_name,record_key,observed_at);
        CREATE TABLE IF NOT EXISTS own_report_indices(
          profile_id TEXT NOT NULL,battle_id TEXT NOT NULL,side TEXT NOT NULL,army_id TEXT NOT NULL,
          owner_uid TEXT NOT NULL,hero_ids TEXT NOT NULL,wid INTEGER NOT NULL,join_id_raw INTEGER,
          observed_at REAL NOT NULL,message_ref TEXT,PRIMARY KEY(profile_id,battle_id,side));
        CREATE TABLE IF NOT EXISTS pilot_members(
          profile_id TEXT NOT NULL,id TEXT NOT NULL,name TEXT NOT NULL,world_uid TEXT NOT NULL,
          role_id TEXT NOT NULL,key_hash TEXT NOT NULL,enabled INTEGER NOT NULL,
          last_received_at REAL,last_observed_at REAL,PRIMARY KEY(profile_id,id));
        CREATE TABLE IF NOT EXISTS source_sightings(
          profile_id TEXT NOT NULL,kind TEXT NOT NULL,record_id TEXT NOT NULL,source_id TEXT NOT NULL,
          observed_at REAL NOT NULL,PRIMARY KEY(profile_id,kind,record_id,source_id));
        PRAGMA user_version=1;
        ''')
        self.db.execute("INSERT OR IGNORE INTO profiles VALUES(?,?,?)", (self.profile_id, packed(self.profile), time.time()))
        self.db.commit()
        self.heroes, self.skills = {}, {}
        for name, attr in (("heroes", "heroes"), ("skills", "skills")):
            path = self.root / "catalog" / (name + ".json")
            if path.exists():
                setattr(self, attr, json.loads(path.read_text(encoding="utf-8")))
        from intelligence import Intelligence
        self.intelligence = Intelligence(self)

    def backfill_report_briefs(self):
        with self.lock, self.db:
            for message in self.db.execute('''SELECT id,last_seen,source,payload FROM messages WHERE profile_id=?
                AND message_id IN (11,6231)''', (self.profile_id,)):
                source = message['source']
                source_id = source.removeprefix('member:') if source.startswith('member:') else 'local'
                for record in dictionaries(json.loads(zlib.decompress(message['payload']))):
                    self._report_brief(record, message['last_seen'], message['id'], source_id)

    def _report_brief(self, record, at, message_ref, source_id):
        brief = record.get('brief_report')
        if not record.get('battle_id') or not isinstance(brief, str) or not brief.strip():
            return
        bid = str(record['battle_id'])
        self._keep_origin('report_briefs', 'brief', bid)
        self._sight('brief', bid, source_id, at)
        self.db.execute('''INSERT INTO report_briefs VALUES(?,?,?,?,?)
            ON CONFLICT(profile_id,battle_id) DO UPDATE SET body=excluded.body,
            observed_at=excluded.observed_at,message_ref=excluded.message_ref
            WHERE excluded.observed_at>report_briefs.observed_at''', (self.profile_id, bid, brief, at, message_ref))

    def close(self):
        with self.lock:
            self.db.close()

    def seed_cache(self, path):
        path = Path(path)
        if not path.exists():
            return
        cached = json.loads(path.read_text(encoding="utf-8"))
        if cached.get("profile_id") != self.profile_id:
            return
        cached_at = path.stat().st_mtime
        with self.lock, self.db:
            for uid, player in cached.get("players", {}).items():
                self._player("world:" + str(uid), player.get("name", "未显示昵称"), player.get("union_id", "0"),
                    player.get("union_name", ""), cached_at, "legacy_cache", uid=str(uid))
            for aid, row in cached.get("armies", {}).items():
                at = row.get("seen_at", cached_at)
                origin, target = row.get("from"), row.get("target")
                facts = {k: row.get(k) for k in ("id", "owner", "from", "target", "begin", "end", "state_code", "state_id", "morale", "reside", "stay")}
                ident = digest([self.profile_id, "legacy_projection", aid, facts])
                self.db.execute("INSERT OR IGNORE INTO army_states VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (ident, self.profile_id, str(aid), row.get("owner"), row.get("state_code"), row.get("state_id"),
                     *(origin or [None, None]), *(target or [None, None]), row.get("begin"), row.get("end"),
                     "", at, None, packed({"source": "legacy_projection", "facts": facts})))
                self.db.execute('''INSERT INTO army_current VALUES(?,?,?,?)
                    ON CONFLICT(profile_id,army_id) DO UPDATE SET state_ref=excluded.state_ref,observed_at=excluded.observed_at
                    WHERE excluded.observed_at>army_current.observed_at''', (self.profile_id, str(aid), ident, at))
                duration = number(row.get("end")) - number(row.get("begin"))
                if origin and target and origin != target and duration > 0:
                    distance = math.dist(origin, target)
                    sample_id = digest([self.profile_id, str(aid), row.get("state_code"),
                        [origin[0]*10000+origin[1], target[0]*10000+target[1], row.get("begin"), row.get("end")]])
                    self.db.execute("INSERT OR IGNORE INTO march_samples VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (sample_id, self.profile_id, str(aid), row.get("state_code"), *origin, *target,
                         row.get("begin"), row.get("end"), distance, duration, distance*3600/duration, "", at, None))

    def _field_shapes(self, mid, value, at):
        seen = set()
        def visit(v, path, depth=0):
            seen.add((path, type(v).__name__))
            if depth >= 5:
                return
            if isinstance(v, dict):
                for key, child in list(v.items())[:150]:
                    key = str(key) if not str(key).lstrip("-").isdigit() else "*"
                    visit(child, path + "." + key, depth + 1)
            elif isinstance(v, list):
                for i, child in enumerate(v[:40]):
                    visit(child, path + (f"[{i}]" if depth == 0 and mid in (5026, 5028) else "[]"), depth + 1)
        visit(value, "$")
        self.db.executemany('''INSERT INTO message_fields VALUES(?,?,?,?,?,?)
          ON CONFLICT(profile_id,message_id,path,type) DO UPDATE SET
          first_seen=MIN(message_fields.first_seen,excluded.first_seen),last_seen=MAX(message_fields.last_seen,excluded.last_seen)''',
          [(self.profile_id, mid, p, t, at, at) for p, t in seen])

    def _sight(self, kind, record_id, source_id, at):
        self.db.execute('''INSERT INTO source_sightings VALUES(?,?,?,?,?)
            ON CONFLICT(profile_id,kind,record_id,source_id) DO UPDATE SET observed_at=excluded.observed_at
            WHERE excluded.observed_at>source_sightings.observed_at''',
            (self.profile_id, kind, record_id, source_id, at))

    def _keep_origin(self, table, kind, bid):
        row = self.db.execute(f"""SELECT r.observed_at FROM {table} r JOIN messages m ON m.id=r.message_ref
            WHERE r.profile_id=? AND r.battle_id=? AND m.source NOT LIKE 'member:%'""",
            (self.profile_id, bid)).fetchone()
        if row:
            self._sight(kind, bid, "local", row[0])

    def record_sources(self, kind, record_id, message_ref, observed_at):
        rows = [dict(row) for row in self.db.execute('''SELECT s.source_id,s.observed_at,
            COALESCE(p.name,?) AS name FROM source_sightings s LEFT JOIN pilot_members p
            ON p.profile_id=s.profile_id AND p.id=s.source_id WHERE s.profile_id=? AND s.kind=? AND s.record_id=?''',
            (self.profile["account_name"], self.profile_id, kind, record_id))]
        if not any(row["source_id"] == "local" for row in rows):
            origin = self.db.execute("SELECT source FROM messages WHERE id=?", (message_ref,)).fetchone()
            if origin and not origin[0].startswith("member:"):
                rows.append({"source_id": "local", "name": self.profile["account_name"], "observed_at": observed_at})
        return sorted(rows, key=lambda row: (-row["observed_at"], row["source_id"]))

    def ingest(self, mid, value, observed_at, source="live"):
        stats = {"battle_ids": [], "new_battle_ids": []} if mid == 92 else None
        source_id = source.removeprefix("member:") if source.startswith("member:") else "local"
        with self.lock, self.db:
            value = clean(value)
            self._field_shapes(mid, value, observed_at)
            business = None
            if mid in (5026, 5028) and isinstance(value, list) and len(value) == 34:
                business = value
            else:
                records = [v for v in dictionaries(value) if v.get("battle_id") and
                           ("attack_name" in v or "defend_name" in v or "report" in v or "brief_report" in v)]
                if records:
                    business = records
                elif mid in (103, 700, 6243, 301):
                    business = value
            if business is None:
                if isinstance(value, (list, dict)):
                    # Preserve unmapped business bodies for later parsers. Scalar
                    # acknowledgements only need their field/type receipt metadata.
                    business = value
                else:
                    return
            message_ref = digest([self.profile_id, mid, business])
            self.db.execute('''INSERT INTO messages VALUES(?,?,?,?,?,?,?,?)
              ON CONFLICT(id) DO UPDATE SET last_seen=MAX(last_seen,excluded.last_seen),receipts=receipts+1''',
              (message_ref, self.profile_id, mid, observed_at, observed_at, 1, source,
               zlib.compress(packed(business).encode("utf-8"))))
            if mid in (5026, 5028):
                self._world(value, observed_at, message_ref, source_id)
            elif mid == 103:
                self._roster(value, observed_at, message_ref)
            elif mid == 700:
                self._union_list(value, observed_at)
            elif mid == 90005:
                self._client_tables(value, observed_at, message_ref)
            for record in dictionaries(business):
                if record.get("battle_id") and ("attack_name" in record or "defend_name" in record):
                    bid = str(record["battle_id"])
                    if stats is not None:
                        if bid not in stats["battle_ids"]:
                            stats["battle_ids"].append(bid)
                            if not self.db.execute("SELECT 1 FROM battle_reports WHERE profile_id=? AND battle_id=?",
                                    (self.profile_id, bid)).fetchone():
                                stats["new_battle_ids"].append(bid)
                    self._keep_origin("battle_reports", "report", bid)
                    self._sight("report", bid, source_id, observed_at)
                    self._battle(record, observed_at, message_ref, shared=source_id != "local")
                if record.get("battle_id") and isinstance(record.get("report"), str) and record["report"].strip():
                    bid = str(record["battle_id"])
                    self._keep_origin("report_bodies", "body", bid)
                    self._sight("body", bid, source_id, observed_at)
                    self.db.execute('''INSERT INTO report_bodies VALUES(?,?,?,?,?)
                      ON CONFLICT(profile_id,battle_id) DO UPDATE SET body=excluded.body,
                      observed_at=excluded.observed_at,message_ref=excluded.message_ref
                      WHERE excluded.observed_at>=report_bodies.observed_at''',
                      (self.profile_id, bid, record["report"], observed_at, message_ref))
                if "brief_report" in record:
                    self._report_brief(record, observed_at, message_ref, source_id)
        # The callback receives counts only after the transaction successfully commits.
        return stats

    def _client_tables(self, value, at, ref):
        allowed = {"Tb_army", "Tb_hero", "Tb_army_alert", "Tb_battle_report_attack", "Tb_battle_report_defend"}
        for operation, table, body in table_updates(value):
            if table not in allowed or not body:
                continue
            key = body[1] if operation == 2 and len(body) >= 2 and body[0] == 0 else body[0]
            ident = digest([self.profile_id, table, operation, body])
            self.db.execute("INSERT OR IGNORE INTO client_table_events VALUES(?,?,?,?,?,?,?,?)",
                (ident, self.profile_id, table, operation, str(key), packed(body), at, ref))
            # Current attack-index layout is cross-checked against 13 real report profiles.
            # Defender and hero layouts differ: preserve those rows without applying old indices.
            if operation != 1 or table != "Tb_battle_report_attack" or len(body) != 15:
                continue
            bid, uid, aid, wid = (number(body[i]) for i in (0, 1, 3, 7))
            heroes = integers(body[10])
            if min(bid, uid, aid, wid) <= 0 or not heroes or any(h <= 0 for h in heroes):
                continue
            self.db.execute('''INSERT INTO own_report_indices VALUES(?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(profile_id,battle_id,side) DO UPDATE SET army_id=excluded.army_id,
                owner_uid=excluded.owner_uid,hero_ids=excluded.hero_ids,wid=excluded.wid,
                join_id_raw=excluded.join_id_raw,observed_at=excluded.observed_at,message_ref=excluded.message_ref
                WHERE excluded.observed_at>=own_report_indices.observed_at''',
                (self.profile_id, str(bid), "attack", str(aid), str(uid), packed(heroes), wid, number(body[4]), at, ref))
            report = self.db.execute("SELECT data,observed_at,message_ref FROM battle_reports WHERE profile_id=? AND battle_id=?",
                (self.profile_id, str(bid))).fetchone()
            if report:
                self._battle(json.loads(report["data"]), report["observed_at"], report["message_ref"])

    def _report_army_link(self, battle, side, heroes):
        own = self._own_report_army_link(battle, side, heroes)
        return own or self.intelligence.army_link(battle, side, heroes)

    def _own_report_army_link(self, battle, side, heroes):
        link = self.db.execute("SELECT * FROM own_report_indices WHERE profile_id=? AND battle_id=? AND side=?",
            (self.profile_id, str(battle["battle_id"]), side)).fetchone()
        if not link:
            return None
        config_path = self.root / "config.json"
        if not config_path.exists():
            return None
        own = json.loads(config_path.read_text(encoding="utf-8-sig")).get("self", {})
        if not own.get("confirmed") or str(own.get("user_id")) != link["owner_uid"]:
            return None
        if (own.get("name") != self.profile["account_name"] or
                str(own.get("union_id")) != str(self.profile["union_id"]) or
                battle.get(side + "_name") != self.profile["account_name"] or
                str(battle.get(side + "_unionid")) != str(self.profile["union_id"])):
            return None
        if [h["id"] for h in heroes] != json.loads(link["hero_ids"]) or number(battle.get("wid")) != link["wid"]:
            return None
        observed_army = self.db.execute("SELECT 1 FROM army_states WHERE profile_id=? AND army_id=? AND owner_uid=? LIMIT 1",
            (self.profile_id, link["army_id"], link["owner_uid"])).fetchone()
        if not observed_army:
            return None
        return {"source": "Tb_battle_report_attack", "army_id": link["army_id"], "world_uid": link["owner_uid"],
            "battle_id": link["battle_id"], "message_ref": link["message_ref"], "observed_at": link["observed_at"],
            "validated": ["confirmed_self", "world_army_owner", "battle_id", "hero_ids", "battle_coordinate"]}

    def _player(self, key, name, union_id, union_name, at, source, role=None, uid=None, event_at=None):
        event_at = at if event_at is None else event_at
        self.db.execute('''INSERT INTO players VALUES(?,?,?,?,?,?,?,?,?,?)
          ON CONFLICT(profile_id,player_key) DO UPDATE SET name=excluded.name,
          union_id=excluded.union_id,union_name=excluded.union_name,source=excluded.source,
          event_at=excluded.event_at,observed_at=excluded.observed_at,
          role_id=COALESCE(excluded.role_id,players.role_id),world_uid=COALESCE(excluded.world_uid,players.world_uid)
          WHERE excluded.event_at>=players.event_at''',
          (self.profile_id, key, uid, role, name, str(union_id), union_name, source, event_at, at))
        if str(union_id) not in ("", "0"):
            self.db.execute('''INSERT INTO unions VALUES(?,?,?,?,?)
              ON CONFLICT(profile_id,union_id) DO UPDATE SET name=COALESCE(excluded.name,unions.name),observed_at=excluded.observed_at''',
              (self.profile_id, str(union_id), union_name or None, None, at))
            self.db.execute('''INSERT INTO roster_observations VALUES(?,?,?,?,?)
              ON CONFLICT(profile_id,union_id,player_key) DO UPDATE SET
              source=CASE WHEN roster_observations.source='alliance_roster' THEN roster_observations.source ELSE excluded.source END,
              observed_at=MAX(roster_observations.observed_at,excluded.observed_at)''',
              (self.profile_id, str(union_id), key, source, at))

    def _event(self, kind, key, data, at, ref, event_at=None):
        ident = digest([self.profile_id, kind, key, data, at])
        self.db.execute("INSERT OR IGNORE INTO observations VALUES(?,?,?,?,?,?,?,?)",
            (ident, self.profile_id, kind, key, at, event_at, packed(data), ref))

    def _world(self, value, at, ref, source_id="local"):
        for uid, raw in value[1].items() if isinstance(value[1], dict) else ():
            if not isinstance(raw, list) or len(raw) < 13:
                continue
            extra = raw[12] if isinstance(raw[12], list) else []
            self._player("world:" + str(uid), str(raw[0]), raw[2], str(extra[2]) if len(extra) > 2 else "",
                         at, "world_scene", str(raw[1]), str(uid))
        for aid, raw in value[6].items() if isinstance(value[6], dict) else ():
            if not isinstance(raw, list) or not raw:
                continue
            aid = str(aid)
            if number(raw[0]) == 0:
                self._event("out_of_view", aid, {"reason": "server_removal"}, at, ref)
                continue
            if len(raw) < 33:
                continue
            origin, target = coord(raw[2]), coord(raw[3])
            ident = digest([self.profile_id, aid, raw])
            old = self.db.execute("SELECT state_ref FROM army_current WHERE profile_id=? AND army_id=?",
                                  (self.profile_id, aid)).fetchone()
            changed = old is None or old[0] != ident
            self.db.execute('''INSERT INTO army_states VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET observed_at=excluded.observed_at,message_ref=excluded.message_ref
                WHERE excluded.observed_at<army_states.observed_at''',
                (ident, self.profile_id, aid, str(raw[1]), number(raw[0]), number(raw[32]),
                 *(origin or [None, None]), *(target or [None, None]), number(raw[4]), number(raw[5]),
                 str(raw[16]), at, ref, packed(raw)))
            if changed:
                self._event("army_state", aid, {"state": number(raw[0]), "from": origin, "target": target,
                    "begin": number(raw[4]), "end": number(raw[5]), "hero_type": str(raw[16])}, at, ref)
            self.db.execute('''INSERT INTO army_current VALUES(?,?,?,?)
                ON CONFLICT(profile_id,army_id) DO UPDATE SET state_ref=excluded.state_ref,observed_at=excluded.observed_at
                WHERE excluded.observed_at>=army_current.observed_at''', (self.profile_id, aid, ident, at))
            duration = number(raw[5]) - number(raw[4])
            if origin and target and origin != target and duration > 0:
                distance = math.dist(origin, target)
                sample_id = digest([self.profile_id, aid, raw[0], raw[2:6]])
                self.db.execute('''INSERT INTO march_samples VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET observed_at=excluded.observed_at,message_ref=excluded.message_ref
                    WHERE excluded.observed_at<march_samples.observed_at''',
                    (sample_id, self.profile_id, aid, number(raw[0]), *origin, *target, number(raw[4]), number(raw[5]),
                     distance, duration, distance * 3600 / duration, str(raw[16]), at, ref))
                self._sight("march", sample_id, source_id, at)
        for aid in value[7] if isinstance(value[7], list) else ():
            self._event("out_of_view", str(aid), {"reason": "server_view_removal"}, at, ref)
        for wid, chunks in value[14].items() if isinstance(value[14], dict) else ():
            raw = chunks.get("0") if isinstance(chunks, dict) else None
            if isinstance(raw, list) and len(raw) >= 21:
                ident = digest([self.profile_id, wid, raw])
                self.db.execute("INSERT OR IGNORE INTO tiles VALUES(?,?,?,?,?,?,?,?,?)",
                    (ident, self.profile_id, number(wid), at, str(raw[2]), str(raw[3]), str(raw[6]), packed(raw), ref))
        if isinstance(value[17], list) and len(value[17]) == 4:
            self._event("view_coverage", "map", {"bounds": value[17]}, at, ref)
        self.intelligence.world(value, at, ref)

    def _roster(self, value, at, ref):
        rows = value if isinstance(value, list) else []
        if rows and isinstance(rows[0], list) and rows[0] and isinstance(rows[0][0], list):
            rows = rows[0]
        known_unions = {r[0] for row in rows if isinstance(row, list) and row
            for r in self.db.execute("SELECT union_id FROM players WHERE profile_id=? AND player_key=?",
                (self.profile_id, "world:" + str(row[0]))) if r[0] not in ("", "0")}
        context_union = next(iter(known_unions)) if len(known_unions) == 1 else "0"
        context_name = self.db.execute("SELECT name FROM unions WHERE profile_id=? AND union_id=?",
            (self.profile_id, context_union)).fetchone()
        for row in rows:
            if isinstance(row, list) and len(row) >= 31 and number(row[0]) > 0:
                self._player("world:" + str(row[0]), str(row[1]) if isinstance(row[1], str) and row[1] else "未显示昵称", context_union, context_name[0] if context_name else "",
                             at, "alliance_roster", uid=str(row[0]))
                self._event("roster_member", str(row[0]), {"raw": row}, at, ref)

    def _union_list(self, value, at):
        for item in dictionaries(value):
            if "union_id" in item and "name" in item and any(k in item for k in ("user_count", "total_member")):
                total = number(item.get("total_member", item.get("user_count")))
                self.db.execute('''INSERT INTO unions VALUES(?,?,?,?,?)
                  ON CONFLICT(profile_id,union_id) DO UPDATE SET name=excluded.name,
                  expected_members=excluded.expected_members,observed_at=excluded.observed_at''',
                  (self.profile_id, str(item["union_id"]), str(item["name"]), total, at))

    def _battle(self, battle, at, ref, emit_change=True, shared=False):
        bid = str(battle["battle_id"])
        previous = self.db.execute("SELECT data,observed_at,message_ref FROM battle_reports WHERE profile_id=? AND battle_id=?",
                                   (self.profile_id, bid)).fetchone()
        merged = json.loads(previous[0]) if previous else {}
        incoming = {k: v for k, v in battle.items() if v not in (None, "")}
        if previous and (shared or at < previous["observed_at"]):
            # A partial view of a known immutable battle can fill absent fields.
            # It cannot replace an established field or promote an older replay.
            merged = {**incoming, **merged}
            if at < previous["observed_at"]:
                at, ref = previous["observed_at"], previous["message_ref"]
            battle = merged
        else:
            merged.update(incoming)
        event_at = number(merged.get("time"))
        self.db.execute('''INSERT INTO battle_reports VALUES(?,?,?,?,?,?,?,?)
          ON CONFLICT(profile_id,battle_id) DO UPDATE SET data=excluded.data,
          observed_at=MAX(battle_reports.observed_at,excluded.observed_at),message_ref=excluded.message_ref''',
          (self.profile_id, bid, event_at, at, number(merged.get("wid")), str(merged.get("wid_name", "")), packed(merged), ref))
        skill_rows = {}
        for segment in str(battle.get("all_skill_info", "")).split(";"):
            parts = integers(segment)
            if parts:
                skills = [{"id": parts[i], "level": parts[i + 1],
                    "name": self.skills.get(str(parts[i]), {}).get("name", f"战法#{parts[i]}")}
                    for i in range(1, len(parts) - 1, 2) if parts[i] > 0]
                skill_rows[parts[0]] = skills or None
        for side, offset in (("attack", 0), ("defend", 3)):
            name = str(battle.get(side + "_name", ""))
            if not name:
                continue
            owner = str(battle.get(side + "_role_id") or name)
            player_key = "battle:" + owner
            union_id = str(battle.get(side + "_unionid", 0))
            union_name = str(battle.get(side + "_union_name", ""))
            self._player(player_key, name, union_id, union_name, at, "battle_report", owner, event_at=event_at)
            instances = integers(battle.get(side + "_idu"))
            team_id = instances[0] if instances else 0
            heroes = []
            for position, segment in enumerate(str(battle.get(side + "_all_hero_info", "")).split(";")[:3], 1):
                parts = integers(segment)
                if not parts or parts[0] <= 0:
                    continue
                hid = str(parts[0])
                meta = self.heroes.get(hid, {})
                hero = {"position": position, "id": parts[0], "name": meta.get("name", f"武将#{hid}"),
                    "level": parts[1] if len(parts) > 1 else None,
                    "base_troop": meta.get("type"), "country": meta.get("country"),
                    # Full report headers (0e1..0e6) confirm that defender
                    # combat slots run front-to-camp, opposite the hero list.
                    "skills": skill_rows.get(position if side == "attack" else 7 - position),
                    "max_hp": parts[2] if len(parts) > 2 else None,
                    "remaining_hp": parts[3] if len(parts) > 3 else None}
                heroes.append(hero)
            if not heroes:
                continue
            formation_key = "unbound:" + digest([owner,
                instances[1:] if any(instances[1:]) else [h["id"] for h in heroes]])[:24]
            link = self._report_army_link(battle, side, heroes) if team_id <= 0 else None
            if team_id <= 0 and not link:
                self.intelligence.retire_conflicts(battle, side)
            if link:
                team_id = int(link["army_id"])
            binding = "exact_army_id" if team_id > 0 else "unbound_formation"
            team_key = "army:" + str(team_id) if team_id > 0 else formation_key
            configuration = {"heroes": heroes, "hero_type_raw": battle.get(side + "_hero_type"),
                "type_advance_raw": battle.get(side + "_hero_type_advance"),
                "advance_raw": battle.get(side + "_advance"), "hero_instances": instances[1:],
                "binding": binding, "npc": bool(battle.get("npc")),
                "configuration_time": event_at, "source": "battle_report", "parser_version": 2}
            if link:
                configuration.update(binding_evidence=link, parser_version=3)
            current = self.db.execute('''SELECT v.data,v.event_at,v.configuration_hash,v.id FROM team_current c
                JOIN team_versions v ON v.id=c.version_ref WHERE c.profile_id=? AND c.team_key=?''',
                (self.profile_id, team_key)).fetchone()
            prior = self.db.execute('''SELECT data,event_at FROM team_versions WHERE profile_id=? AND team_key=?
                AND event_at<=? AND COALESCE(json_extract(data,'$.binding_conflict'),0)=0
                ORDER BY event_at DESC,observed_at DESC,id LIMIT 1''',
                (self.profile_id, team_key, event_at)).fetchone()
            for hero in heroes:
                hero["field_times"] = {"identity": event_at,
                    "level": event_at if hero.get("level") else None,
                    "skills": event_at if hero.get("skills") is not None else None}
            if prior:
                old_config = json.loads(prior["data"])
                old_heroes = {h["position"]: h for h in old_config["heroes"]}
                same_lineup = all(h["position"] in old_heroes and old_heroes[h["position"]]["id"] == h["id"] for h in heroes)
                for hero in heroes:
                    old = old_heroes.get(hero["position"], {})
                    if old.get("id") != hero["id"]:
                        continue
                    for field in ("skills", "level"):
                        if hero.get(field) is None or (field == "level" and not hero.get(field)):
                            hero[field] = old.get(field)
                            hero["field_times"][field] = old.get("field_times", {}).get(field, prior["event_at"])
                if same_lineup:
                    present = {h["position"] for h in heroes}
                    for pos, old in old_heroes.items():
                        if pos not in present:
                            retained = dict(old)
                            retained["last_known_only"] = True
                            heroes.append(retained)
                    heroes.sort(key=lambda h: h["position"])
                for field in ("hero_type_raw", "type_advance_raw", "advance_raw"):
                    if configuration[field] in (None, "") and same_lineup:
                        configuration[field] = old_config.get(field)
            # HP is battle evidence, not configuration identity.
            signature = digest({"heroes": [{k: v for k, v in h.items() if k not in ("max_hp", "remaining_hp", "field_times", "last_known_only")} for h in heroes],
                "troop": configuration["hero_type_raw"], "troop_advance": configuration["type_advance_raw"],
                "advance": configuration["advance_raw"]})
            vid = digest([self.profile_id, team_key, bid, signature])
            legacy_signature = digest([{k: v for k, v in h.items() if k not in ("max_hp", "remaining_hp", "field_times", "last_known_only")} for h in heroes])
            legacy_id = digest([self.profile_id, team_key, bid, legacy_signature])
            if self.db.execute("SELECT 1 FROM team_versions WHERE id=?", (legacy_id,)).fetchone():
                vid = legacy_id
            self.db.execute('''INSERT INTO team_versions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET data=excluded.data,configuration_hash=excluded.configuration_hash,
                observed_at=MAX(team_versions.observed_at,excluded.observed_at)''',
                (vid, self.profile_id, team_key, player_key, name, union_id, union_name, binding,
                 event_at, at, bid, signature, packed(configuration), ref))
            self.db.execute('''INSERT INTO team_current VALUES(?,?,?,?,?)
              ON CONFLICT(profile_id,team_key) DO UPDATE SET version_ref=excluded.version_ref,
              event_at=excluded.event_at,observed_at=excluded.observed_at
              WHERE excluded.event_at>team_current.event_at OR
               (excluded.event_at=team_current.event_at AND excluded.observed_at>=team_current.observed_at)''',
              (self.profile_id, team_key, vid, event_at, at))
            if emit_change and current and event_at >= current["event_at"] and signature != current["configuration_hash"]:
                self._event("team_configuration_changed", team_key,
                    {"player": name, "previous_version": current["id"], "new_version": vid,
                     "heroes": [h["name"] for h in heroes], "binding": binding}, at, ref, event_at)
            if link:
                # Retire only the duplicate current projection of this very report/side.
                # Original unbound versions and their immutable source evidence remain saved.
                retired = self.db.execute('''DELETE FROM team_current WHERE profile_id=? AND team_key=? AND version_ref IN
                    (SELECT id FROM team_versions WHERE profile_id=? AND battle_id=? AND player_key=? AND binding='unbound_formation')''',
                    (self.profile_id, formation_key, self.profile_id, bid, player_key)).rowcount
                if retired:
                    pending = self.db.execute('''SELECT v.id,v.event_at,v.observed_at FROM team_versions v
                        WHERE v.profile_id=? AND v.team_key=? AND v.binding='unbound_formation'
                        AND NOT EXISTS(SELECT 1 FROM team_versions e WHERE e.profile_id=v.profile_id
                            AND e.battle_id=v.battle_id AND e.player_key=v.player_key AND e.binding='exact_army_id'
                            AND COALESCE(json_extract(e.data,'$.binding_conflict'),0)=0)
                        ORDER BY v.event_at DESC,v.observed_at DESC,v.id LIMIT 1''',
                        (self.profile_id, formation_key)).fetchone()
                    if pending:
                        self.db.execute("INSERT OR REPLACE INTO team_current VALUES(?,?,?,?,?)",
                            (self.profile_id, formation_key, pending["id"], pending["event_at"], pending["observed_at"]))
        self.intelligence.battle(merged, at, ref)

    def summary(self):
        with self.lock:
            counts = {table: self.db.execute(f"SELECT COUNT(*) FROM {table} WHERE profile_id=?", (self.profile_id,)).fetchone()[0]
                for table in ("messages", "players", "army_states", "march_samples", "battle_reports", "team_versions", "team_current", "observations", "report_bodies", "report_briefs", "lineup_forecasts", "same_army_audits", "client_table_events", "own_report_indices", "army_report_refs", "combat_sides", "battle_march_links", "report_read_jobs")}
            counts["bound_teams"] = self.db.execute('''SELECT COUNT(*) FROM team_current c JOIN team_versions v
                ON v.id=c.version_ref WHERE c.profile_id=? AND v.binding='exact_army_id' ''', (self.profile_id,)).fetchone()[0]
            counts["unbound_formations"] = counts["team_current"] - counts["bound_teams"]
            counts["roster_members"] = self.db.execute('''SELECT COUNT(*) FROM roster_observations
                WHERE profile_id=? AND union_id=? AND source='alliance_roster' ''',
                (self.profile_id, self.profile["union_id"])).fetchone()[0]
            counts["world_armies"] = self.db.execute("SELECT COUNT(*) FROM army_current WHERE profile_id=?",
                (self.profile_id,)).fetchone()[0]
            report_range = self.db.execute("SELECT MIN(event_at),MAX(event_at) FROM battle_reports WHERE profile_id=? AND event_at>0",
                (self.profile_id,)).fetchone()
            pages = self.db.execute("SELECT COUNT(*),MAX(last_seen) FROM messages WHERE profile_id=? AND message_id=92",
                (self.profile_id,)).fetchone()
            unions = []
            for row in self.db.execute("SELECT * FROM unions WHERE profile_id=?", (self.profile_id,)):
                item = dict(row)
                item["observed_members"] = self.db.execute('''SELECT COUNT(DISTINCT name) FROM players
                    WHERE profile_id=? AND union_id=?''', (self.profile_id, row["union_id"])).fetchone()[0]
                item["roster_complete"] = False
                unions.append(item)
            return {"profile": self.profile, "profile_id": self.profile_id, "counts": counts, "unions": unions,
                "scope": "全部接收到的本赛季数据；战役与坐标仅用于查询分类，不过滤采集",
                "coverage": {"full_roster_verified": False, "all_teams_verified": False,
                    "missing_fields_remain_unknown": True},
                "report_loading": {"distinct_alliance_batches": pages[0], "last_batch_at": pages[1],
                    "oldest_battle_at": report_range[0], "latest_battle_at": report_range[1],
                    "history_complete_verified": False},
                "database": str(self.database_path)}

    def activate_profile(self, profile):
        required = ("server", "season", "account_role_id", "account_name", "union_id", "union_name")
        if not isinstance(profile, dict) or any(not str(profile.get(k, "")).strip() for k in required):
            raise ValueError("请填写区服、赛季、角色和同盟档案信息")
        selected = {k: str(profile[k]).strip()[:100] for k in required}
        selected["season_label"] = str(profile.get("season_label") or selected["season"])[:100]
        if isinstance(profile.get("server_label"), str) and profile["server_label"].strip():
            selected["server_label"] = profile["server_label"].strip()[:100]
        selected_id = digest([selected[k] for k in ("server", "season", "account_role_id")])[:24]
        with self.lock, self.db:
            self.db.execute("INSERT OR IGNORE INTO profiles VALUES(?,?,?)", (selected_id, packed(selected), time.time()))
            temporary = self.profile_path.with_suffix(".json.tmp")
            temporary.write_text(json.dumps(selected, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(self.profile_path)
            self.profile, self.profile_id = selected, selected_id
        return {"ok": True, "profile_id": selected_id}

    def _army_lineups(self, army_id):
        owner = self.db.execute('''SELECT p.name,p.union_id,s.hero_type,a.observed_at FROM army_current a
            JOIN army_states s ON s.id=a.state_ref LEFT JOIN players p ON p.profile_id=a.profile_id
            AND p.player_key='world:'||s.owner_uid WHERE a.profile_id=? AND a.army_id=?''',
            (self.profile_id, str(army_id))).fetchone()
        codes = map_troop_codes(owner["hero_type"]) if owner else ()
        evidence = {"troops": describe_troops(codes), "troops_observed_at": owner["observed_at"] if codes else None}
        exact = self.db.execute('''SELECT v.* FROM team_current c JOIN team_versions v ON v.id=c.version_ref
            WHERE c.profile_id=? AND c.team_key=?''', (self.profile_id, "army:" + str(army_id))).fetchall()
        if exact:
            return exact, "exact_army_id", evidence
        if not owner or not owner["name"]:
            return [], None, evidence
        if not codes:
            rows = self.db.execute('''SELECT v.* FROM team_current c JOIN team_versions v ON v.id=c.version_ref
                WHERE v.profile_id=? AND v.player_name=? AND v.union_id=?
                ORDER BY v.event_at DESC LIMIT 10''', (self.profile_id, owner["name"], owner["union_id"])).fetchall()
            return rows, "name_union_candidates", evidence

        rows = self.db.execute('''SELECT v.*, c.side AS report_side, c.hero_key AS report_hero_key,
            json_extract(c.features,'$.'||c.side||'_hero_type') AS report_troop_raw
            FROM team_versions v JOIN combat_sides c ON c.profile_id=v.profile_id AND c.battle_id=v.battle_id
            AND c.player_key=v.player_key AND c.union_id=v.union_id
            WHERE v.profile_id=? AND v.player_name=? AND v.union_id=? AND v.event_at<=?
            AND COALESCE(json_extract(v.data,'$.binding_conflict'),0)=0
            ORDER BY v.event_at DESC,v.observed_at DESC,v.id''',
            (self.profile_id, owner["name"], owner["union_id"], owner["observed_at"]))
        exact_troops, same_families = {}, {}
        families = troop_families(codes)
        for row in rows:
            reported = report_troop_codes(row["report_troop_raw"], row["report_side"])
            if not row["report_hero_key"] or troop_families(reported) != families:
                continue
            formation = (row["player_key"], row["report_hero_key"])
            matches = exact_troops if reported == codes else same_families
            if formation not in matches:
                item = dict(row)
                item["report_troops"] = describe_troops(reported)
                matches[formation] = item
        if exact_troops:
            return list(exact_troops.values()), "name_union_troop_candidate", evidence
        if same_families:
            return list(same_families.values()), "name_union_troop_family_candidate", evidence
        return [], "no_troop_match", evidence

    def query(self, kind, search="", union_id="", limit=100, offset=0, team_key="", army_id=""):
        limit, offset = max(1, min(number(limit, 100), 500)), max(0, number(offset))
        with self.lock:
            match = None
            evidence = {}
            if kind == "army_lineups":
                rows, match, evidence = self._army_lineups(army_id)
            elif kind == "teams":
                sql = '''SELECT v.* FROM team_current c JOIN team_versions v ON v.id=c.version_ref
                    WHERE v.profile_id=?'''
                args = [self.profile_id]
                if search:
                    sql += " AND (v.player_name LIKE ? OR v.data LIKE ?)"
                    args += ["%" + search + "%"] * 2
                if union_id:
                    sql += " AND v.union_id=?"
                    args.append(union_id)
                sql += " ORDER BY v.event_at DESC LIMIT ? OFFSET ?"
                rows = self.db.execute(sql, args + [limit, offset]).fetchall()
            elif kind == "team_history":
                rows = self.db.execute('''SELECT * FROM team_versions WHERE profile_id=? AND team_key=?
                    ORDER BY event_at DESC LIMIT ? OFFSET ?''', (self.profile_id, team_key, limit, offset)).fetchall()
            elif kind == "marches":
                rows = self.db.execute('''SELECT m.*,s.owner_uid,p.name AS player_name,p.union_id,
                    (SELECT COUNT(*) FROM march_samples x WHERE x.profile_id=m.profile_id AND x.army_id=m.army_id) AS samples
                    FROM march_samples m LEFT JOIN army_states s ON s.id=(SELECT state_ref FROM army_current a
                    WHERE a.profile_id=m.profile_id AND a.army_id=m.army_id)
                    LEFT JOIN players p ON p.profile_id=m.profile_id AND p.player_key='world:'||s.owner_uid
                    WHERE m.profile_id=? AND (?='' OR p.union_id=?) AND (?='' OR p.name LIKE ? OR m.army_id LIKE ?)
                    ORDER BY m.begin_at DESC LIMIT ? OFFSET ?''',
                    (self.profile_id, union_id, union_id, search, "%" + search + "%", "%" + search + "%", limit, offset)).fetchall()
            elif kind == "players":
                rows = self.db.execute('''SELECT * FROM players WHERE profile_id=? AND (?='' OR union_id=?)
                    AND (?='' OR name LIKE ?) ORDER BY event_at DESC LIMIT ? OFFSET ?''',
                    (self.profile_id, union_id, union_id, search, "%" + search + "%", limit, offset)).fetchall()
            elif kind == "battles":
                rows = self.db.execute('''SELECT * FROM battle_reports WHERE profile_id=? AND (?='' OR data LIKE ?)
                    AND (?='' OR json_extract(data,'$.attack_unionid')=CAST(? AS INTEGER)
                    OR json_extract(data,'$.defend_unionid')=CAST(? AS INTEGER))
                    ORDER BY event_at DESC LIMIT ? OFFSET ?''', (self.profile_id, search, "%" + search + "%", union_id, union_id, union_id, limit, offset)).fetchall()
            elif kind == "fields":
                rows = self.db.execute('''SELECT message_id,path,type,first_seen,last_seen FROM message_fields
                    WHERE profile_id=? ORDER BY message_id,path LIMIT ? OFFSET ?''', (self.profile_id, limit, offset)).fetchall()
            elif kind == "client_records":
                rows = self.db.execute('''SELECT table_name,operation,record_key,body,observed_at,message_ref
                    FROM client_table_events WHERE profile_id=? AND (?='' OR table_name LIKE ? OR record_key LIKE ?)
                    ORDER BY observed_at DESC LIMIT ? OFFSET ?''',
                    (self.profile_id, search, "%" + search + "%", "%" + search + "%", limit, offset)).fetchall()
            else:
                rows = self.db.execute('''SELECT * FROM observations WHERE profile_id=? AND (?='' OR data LIKE ?)
                    ORDER BY observed_at DESC LIMIT ? OFFSET ?''', (self.profile_id, search, "%" + search + "%", limit, offset)).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                if "data" in item:
                    item["data"] = json.loads(item["data"])
                if kind in ("army_lineups", "teams", "team_history"):
                    sides = self.db.execute('''SELECT side,features FROM combat_sides WHERE profile_id=?
                        AND battle_id=? AND player_key=? AND union_id=?''',
                        (self.profile_id, item["battle_id"], item["player_key"], item["union_id"])).fetchall()
                    if len(sides) == 1:
                        side, features = sides[0]
                        item["data"]["heroes"] = side_heroes(item["data"]["heroes"], side, json.loads(features))
                    else:
                        item["data"]["heroes"] = [named_hero(h) for h in item["data"]["heroes"]]
                if "body" in item:
                    item["body"] = json.loads(item["body"])
                result.append(item)
            return {"items": result, "limit": limit, "offset": offset, "profile_id": self.profile_id, "match": match, **evidence}
