"""Season-scoped observations, immutable evidence and current team configurations."""
import copy
import json
from contextlib import contextmanager
import math
import sqlite3
import threading
import time
import zlib
from pathlib import Path

from combat_loadout import named_hero, side_heroes
import retention
import reported_archive
from observation_codec import (clean, coord, describe_troops, dictionaries, digest, integers,
    map_player_traits, map_troop_codes, number, packed, report_has_npc, report_system_npc,
    report_troop_codes, troop_families)


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
        self.contribution_revision = 0
        (self.root / "data").mkdir(parents=True, exist_ok=True)
        self.profile_path = self.root / "archive-profile.json"
        if profile is None and self.profile_path.exists():
            profile = json.loads(self.profile_path.read_text(encoding="utf-8-sig"))
        self.profile = profile or {"server": "pending", "season": "pending",
            "season_label": "等待读取当前赛季", "account_role_id": "0",
            "account_name": "待识别", "union_id": "0", "union_name": "未加入同盟"}
        if not self.profile_path.exists():
            self.profile_path.write_text(json.dumps(self.profile, ensure_ascii=False, indent=2), encoding="utf-8")
        self.profile_id = digest([self.profile.get(k) for k in ("server", "season", "account_role_id")])[:24]
        self.database_path = Path(database_path) if database_path else self.root / "data" / "intelligence.sqlite3"
        self.db = sqlite3.connect(self.database_path.resolve().as_uri(), uri=True, check_same_thread=False, timeout=15)
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
        CREATE TABLE IF NOT EXISTS player_traits(
          profile_id TEXT NOT NULL,player_key TEXT NOT NULL,force_type INTEGER,
          clan_id INTEGER NOT NULL,clan_name TEXT NOT NULL,observed_at REAL NOT NULL,message_ref TEXT,
          PRIMARY KEY(profile_id,player_key));
        CREATE INDEX IF NOT EXISTS player_traits_force ON player_traits(profile_id,force_type,observed_at);
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
        CREATE INDEX IF NOT EXISTS team_version_player ON team_versions(profile_id,union_id,player_name,event_at DESC,battle_id);
        CREATE INDEX IF NOT EXISTS team_version_report ON team_versions(profile_id,battle_id,player_key,union_id);
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
        from profile_aliases import resolve
        self.profile_id=resolve(self.db,self.profile)
        self.db.execute("INSERT INTO profiles VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data", (self.profile_id, packed(self.profile), time.time()))
        self.db.commit()
        self.heroes, self.skills = {}, {}
        for name, attr in (("heroes", "heroes"), ("skills", "skills")):
            path = self.root / "catalog" / (name + ".json")
            if path.exists():
                setattr(self, attr, json.loads(path.read_text(encoding="utf-8")))
        self.db.executescript(retention.SCHEMA)
        from intelligence import Intelligence
        self.intelligence = Intelligence(self)
        from lineup_projection import LineupProjection
        self.lineup_projection = LineupProjection(self)
        self.db.executescript(reported_archive.SCHEMA)
        policy_path=self.root/'archive-policy.json'
        policy=json.loads(policy_path.read_text(encoding='utf-8')) if policy_path.exists() else {}
        self.report_only=policy.get('reported_teams_only',True)
        self.history_floor=policy.get('retain_from',0)
        if type(self.history_floor)in (int,float)and self.history_floor>0:
            self.db.execute('INSERT INTO retention_policy VALUES(?,?,?) ON CONFLICT(profile_id) DO UPDATE SET retain_from=MAX(retain_from,excluded.retain_from)',(self.profile_id,self.history_floor,time.time()))
        self.db.execute('INSERT OR REPLACE INTO team_archive_policy VALUES(?,?)',(self.profile_id,int(self.report_only)))
        self.db.commit()
        reported_archive.install_live(self.db)
        self._pending_trim_at=0
        self.remove_system_npc_teams()
        import team_catalog,territory_events
        team_catalog.install(self.db)
        territory_events.install(self)
        territory_events.seed_metrics(self)
        from attendance import Attendance
        self.attendance = Attendance(self)
        self.seed_player_traits()

    def _discard_system_npc_team(self, battle, side):
        owner = str(battle.get(side + '_role_id') or battle.get(side + '_name') or '')
        rows = self.db.execute('''SELECT id,team_key FROM team_versions WHERE profile_id=?
            AND battle_id=? AND player_key=?''',
            (self.profile_id,str(battle['battle_id']),'battle:'+owner)).fetchall()
        if not rows:
            return 0
        self.db.executemany('DELETE FROM team_current WHERE profile_id=? AND version_ref=?',
                            [(self.profile_id,row['id']) for row in rows])
        self.db.executemany('DELETE FROM team_versions WHERE id=?',[(row['id'],) for row in rows])
        for key in {row['team_key'] for row in rows}:
            latest = self.db.execute('''SELECT id,event_at,observed_at FROM team_versions v
                WHERE profile_id=? AND team_key=? AND COALESCE(json_extract(data,'$.binding_conflict'),0)=0
                AND (binding<>'unbound_formation' OR NOT EXISTS(
                    SELECT 1 FROM team_versions e WHERE e.profile_id=v.profile_id AND e.battle_id=v.battle_id
                    AND e.player_key=v.player_key AND e.binding='exact_army_id'
                    AND COALESCE(json_extract(e.data,'$.binding_conflict'),0)=0))
                ORDER BY event_at DESC,observed_at DESC,id DESC LIMIT 1''',(self.profile_id,key)).fetchone()
            if latest:
                self.db.execute('INSERT OR IGNORE INTO team_current VALUES(?,?,?,?,?)',
                                (self.profile_id,key,latest['id'],latest['event_at'],latest['observed_at']))
        return len(rows)

    def remove_system_npc_teams(self):
        rule='system-npc-teams-1'
        with self.lock,self.db:
            if self.db.execute('SELECT 1 FROM lineup_rule_migrations WHERE profile_id=? AND rule=?',
                               (self.profile_id,rule)).fetchone():
                return
            candidates = self.db.execute('''SELECT c.side,b.data FROM combat_sides c
                JOIN battle_reports b ON b.profile_id=c.profile_id AND b.battle_id=c.battle_id
                WHERE c.profile_id=? AND ((c.side='defend' AND c.npc=1)
                    OR CAST(json_extract(c.features,'$.'||c.side||'er_force') AS INTEGER)=1000001)
                AND EXISTS(SELECT 1 FROM team_versions v WHERE v.profile_id=c.profile_id
                    AND v.battle_id=c.battle_id AND v.player_key=c.player_key)''',(self.profile_id,)).fetchall()
            for row in candidates:
                battle=json.loads(row['data'])
                if report_system_npc(battle,row['side']):
                    self._discard_system_npc_team(battle,row['side'])
            self.db.execute('INSERT INTO lineup_rule_migrations VALUES(?,?)',(self.profile_id,rule))

    def seed_player_traits(self):
        """Bounded one-time recovery of identities, without replaying old armies."""
        rule='player-traits-recent-scenes-1'
        if self.db.execute('SELECT 1 FROM lineup_rule_migrations WHERE profile_id=? AND rule=?',
                           (self.profile_id,rule)).fetchone():
            return
        with self.lock,self.db:
            messages=[]
            for mid,limit in ((5026,64),(5028,128)):
                messages.extend(self.db.execute('''SELECT id,payload,last_seen FROM messages
                    WHERE profile_id=? AND message_id=? AND source NOT LIKE 'member:%'
                    ORDER BY first_seen DESC LIMIT ?''',
                    (self.profile_id,mid,limit)).fetchall())
            for message in sorted(messages,key=lambda m:m['last_seen']):
                try:
                    value=json.loads(zlib.decompress(message['payload']))
                except (ValueError,zlib.error):
                    continue
                if isinstance(value,list) and len(value)==34 and isinstance(value[1],dict):
                    for uid,raw in value[1].items():
                        if isinstance(raw,list) and len(raw)>=13:
                            self._player_traits(uid,raw,message['last_seen'],message['id'])
            self.db.execute('INSERT INTO lineup_rule_migrations VALUES(?,?)',(self.profile_id,rule))

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
        if not retention.accepts(self, record):
            return
        bid = str(record['battle_id'])
        self._keep_origin('report_briefs', 'brief', bid)
        self._sight('brief', bid, source_id, at)
        self.db.execute('''INSERT INTO report_briefs VALUES(?,?,?,?,?)
            ON CONFLICT(profile_id,battle_id) DO UPDATE SET body=excluded.body,
            observed_at=excluded.observed_at,message_ref=excluded.message_ref
            WHERE excluded.observed_at>report_briefs.observed_at''', (self.profile_id, bid, brief, at, message_ref))

    @property
    def retention_from(self):
        return retention.floor(self)

    def close(self):
        self.attendance.close()
        self.lineup_projection.close()
        with self.lock:
            self.db.close()

    @contextmanager
    def read_snapshot(self, timeout=3):
        """Give queries a bounded WAL reader, never the capture writer's lock."""
        with self.lock:
            reader = copy.copy(self)
            reader.profile = dict(self.profile)
            reader.intelligence = copy.copy(self.intelligence)
            pending=reported_archive.live_snapshot(self.db) if self.report_only else None
        db = sqlite3.connect(self.database_path.resolve().as_uri() + '?mode=ro', uri=True, timeout=1)
        db.row_factory = sqlite3.Row
        reported_archive.install_live(db,pending)
        db.commit()
        deadline = time.monotonic() + timeout
        db.set_progress_handler(lambda: time.monotonic() >= deadline, 1000)
        reader.lock, reader.db, reader.read_only = threading.RLock(), db, True
        reader.read_deadline = deadline
        reader.intelligence.archive, reader.intelligence.db = reader, db
        reader.intelligence.analysis_cache = self.intelligence.analysis_cache.copy()
        try:
            db.execute('PRAGMA query_only=ON')
            db.execute('BEGIN')
            yield reader
        finally:
            db.close()

    def seed_cache(self, path):
        path = Path(path)
        if not path.exists():
            return
        cached = json.loads(path.read_text(encoding="utf-8"))
        if self.retention_from and number(cached.get("saved_at", cached.get("updated_at"))) < self.retention_from:
            return
        if cached.get("profile_id") != self.profile_id:
            return
        cached_at = path.stat().st_mtime
        with self.lock, self.db:
            for uid, player in cached.get("players", {}).items():
                self._player("world:" + str(uid), player.get("name", "未显示昵称"), player.get("union_id", "0"),
                    player.get("union_name", ""), cached_at, "legacy_cache", uid=str(uid))
            for aid, row in cached.get("armies", {}).items():
                states=reported_archive.storage(self.db,self.profile_id,aid,'army_states')
                current=reported_archive.storage(self.db,self.profile_id,aid,'army_current')
                marches=reported_archive.storage(self.db,self.profile_id,aid,'march_samples')
                at = row.get("seen_at", cached_at)
                origin, target = row.get("from"), row.get("target")
                facts = {k: row.get(k) for k in ("id", "owner", "from", "target", "begin", "end", "state_code", "state_id", "morale", "reside", "stay")}
                ident = digest([self.profile_id, "legacy_projection", aid, facts])
                self.db.execute(f"INSERT OR IGNORE INTO {states} VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (ident, self.profile_id, str(aid), row.get("owner"), row.get("state_code"), row.get("state_id"),
                     *(origin or [None, None]), *(target or [None, None]), row.get("begin"), row.get("end"),
                     "", at, None, packed({"source": "legacy_projection", "facts": facts})))
                self.db.execute(f'''INSERT INTO {current} VALUES(?,?,?,?)
                    ON CONFLICT(profile_id,army_id) DO UPDATE SET state_ref=excluded.state_ref,observed_at=excluded.observed_at
                    WHERE excluded.observed_at>{current}.observed_at''', (self.profile_id, str(aid), ident, at))
                duration = number(row.get("end")) - number(row.get("begin"))
                if origin and target and origin != target and duration > 0:
                    distance = math.dist(origin, target)
                    sample_id = digest([self.profile_id, str(aid), row.get("state_code"),
                        [origin[0]*10000+origin[1], target[0]*10000+target[1], row.get("begin"), row.get("end")]])
                    self.db.execute(f"INSERT OR IGNORE INTO {marches} VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
            CASE WHEN s.source_id='season-seed' THEN '随安装包导入的赛季档案' ELSE COALESCE(p.name,?) END AS name
            FROM source_sightings s LEFT JOIN pilot_members p
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
            cutoff = self.retention_from
            if cutoff:
                excluded = [str(r['battle_id']) for r in dictionaries(value) if r.get('battle_id')
                            and isinstance(r.get('time'),(int,float)) and r['time'] < cutoff]
                self.db.executemany('INSERT OR IGNORE INTO retention_ignored_ids VALUES(?,?)',
                                    [(self.profile_id,bid) for bid in excluded])
                if stats is not None: stats['excluded_battle_ids'] = excluded
                if observed_at < cutoff: return stats
                value = retention.sanitize(self,value)
                if value is None: return stats
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
            business = reported_archive.filter_message(self.db,self.profile_id,mid,business)
            message_ref = digest([self.profile_id, mid, business])
            self.db.execute('''INSERT INTO messages VALUES(?,?,?,?,?,?,?,?)
              ON CONFLICT(id) DO UPDATE SET last_seen=MAX(last_seen,excluded.last_seen),receipts=receipts+1''',
              (message_ref, self.profile_id, mid, observed_at, observed_at, 1, source,
               zlib.compress(packed(business).encode("utf-8"))))
            if mid in (90005,99991):
                import territory_events
                territory_events.observe_tables(self,value,observed_at,message_ref)
            if mid in (5026, 5028):
                self._world(value, observed_at, message_ref, source_id)
            elif mid == 103:
                self._roster(value, observed_at, message_ref)
            elif mid == 700:
                self._union_list(value, observed_at)
            elif mid == 90005:
                self._client_tables(value, observed_at, message_ref,
                    allow_report_reads=source in ('live','pc-live','dashen-live','mumu-live'))
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
                    if source in ('live','pc-live','dashen-live','mumu-live'):
                        self.intelligence.report_access.observe_report(mid,record,observed_at)
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

    def _client_tables(self, value, at, ref, allow_report_reads=False):
        allowed = {"Tb_army", "Tb_hero", "Tb_army_alert", "Tb_battle_report_attack", "Tb_battle_report_defend"}
        for operation, table, body in table_updates(value):
            if table not in allowed or not body:
                continue
            key = body[1] if operation == 2 and len(body) >= 2 and body[0] == 0 else body[0]
            ident = digest([self.profile_id, table, operation, body])
            if table not in ('Tb_army','Tb_army_alert') or reported_archive.admitted(self.db,self.profile_id,key):
                self.db.execute("INSERT OR IGNORE INTO client_table_events VALUES(?,?,?,?,?,?,?,?)",
                    (ident, self.profile_id, table, operation, str(key), packed(body), at, ref))
            # Current attack-index layout is cross-checked against 13 real report profiles.
            # Defender and hero layouts differ: preserve those rows without applying old indices.
            if operation != 1 or table != "Tb_battle_report_attack" or len(body) != 15:
                continue
            bid, uid, aid, wid = (number(body[i]) for i in (0, 1, 3, 7))
            if retention.ignored(self,bid):
                continue
            heroes = integers(body[10])
            if min(bid, uid, aid, wid) <= 0 or not heroes or any(h <= 0 for h in heroes):
                continue
            target=reported_archive.storage(self.db,self.profile_id,aid,'own_report_indices')
            self.db.execute(f'''INSERT INTO {target} VALUES(?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(profile_id,battle_id,side) DO UPDATE SET army_id=excluded.army_id,
                owner_uid=excluded.owner_uid,hero_ids=excluded.hero_ids,wid=excluded.wid,
                join_id_raw=excluded.join_id_raw,observed_at=excluded.observed_at,message_ref=excluded.message_ref
                WHERE excluded.observed_at>={target}.observed_at''',
                (self.profile_id, str(bid), "attack", str(aid), str(uid), packed(heroes), wid, number(body[4]), at, ref))
            if allow_report_reads:
                self.intelligence.report_access.personal_index(bid,uid,at)
                if self.intelligence.report_access.allowed(bid):
                    self.intelligence.queue_evidence('profile',bid,at)
            report = self.db.execute("SELECT data,observed_at,message_ref FROM battle_reports WHERE profile_id=? AND battle_id=?",
                (self.profile_id, str(bid))).fetchone()
            if report:
                self._battle(json.loads(report["data"]), report["observed_at"], report["message_ref"])

    def _report_army_link(self, battle, side, heroes):
        own = self._own_report_army_link(battle, side, heroes)
        return own or self.intelligence.army_link(battle, side, heroes)

    def _own_report_army_link(self, battle, side, heroes):
        link = self.db.execute("SELECT * FROM live_own_report_indices WHERE profile_id=? AND battle_id=? AND side=?",
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
        observed_army = self.db.execute("SELECT 1 FROM live_army_states WHERE profile_id=? AND army_id=? AND owner_uid=? LIMIT 1",
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
        if kind in ('army_state','out_of_view') and not reported_archive.admitted(self.db,self.profile_id,key):
            return
        ident = digest([self.profile_id, kind, key, data, at])
        self.db.execute("INSERT OR IGNORE INTO observations VALUES(?,?,?,?,?,?,?,?)",
            (ident, self.profile_id, kind, key, at, event_at, packed(data), ref))

    def _player_traits(self,uid,raw,at,ref):
        traits=map_player_traits(raw)
        old=self.db.execute('''SELECT force_type,clan_id,clan_name,observed_at FROM player_traits
            WHERE profile_id=? AND player_key=?''',(self.profile_id,'world:'+str(uid))).fetchone()
        if old is not None and at < old['observed_at']:
            return
        values=tuple(traits[k]for k in ('force_type','clan_id','clan_name'))
        self.db.execute('''INSERT INTO player_traits VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(profile_id,player_key) DO UPDATE SET force_type=excluded.force_type,
            clan_id=excluded.clan_id,clan_name=excluded.clan_name,observed_at=excluded.observed_at,
            message_ref=excluded.message_ref''',(self.profile_id,'world:'+str(uid),*values,at,ref))
        if old is None or tuple(old)[:3]!=values:
            self._event('player_traits','world:'+str(uid),traits,at,ref)

    def _world(self, value, at, ref, source_id="local"):
        for uid, raw in value[1].items() if isinstance(value[1], dict) else ():
            if not isinstance(raw, list) or len(raw) < 13:
                continue
            extra = raw[12] if isinstance(raw[12], list) else []
            self._player("world:" + str(uid), str(raw[0]), raw[2], str(extra[2]) if len(extra) > 2 else "",
                         at, "world_scene", str(raw[1]), str(uid))
            self._player_traits(uid,raw,at,ref)
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
            states=reported_archive.storage(self.db,self.profile_id,aid,'army_states')
            current=reported_archive.storage(self.db,self.profile_id,aid,'army_current')
            marches=reported_archive.storage(self.db,self.profile_id,aid,'march_samples')
            ident = digest([self.profile_id, aid, raw])
            old = self.db.execute("SELECT state_ref FROM live_army_current WHERE profile_id=? AND army_id=?",
                                  (self.profile_id, aid)).fetchone()
            changed = old is None or old[0] != ident
            self.db.execute(f'''INSERT INTO {states} VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET observed_at=excluded.observed_at,message_ref=excluded.message_ref
                WHERE excluded.observed_at<{states}.observed_at''',
                (ident, self.profile_id, aid, str(raw[1]), number(raw[0]), number(raw[32]),
                 *(origin or [None, None]), *(target or [None, None]), number(raw[4]), number(raw[5]),
                 str(raw[16]), at, ref, packed(raw)))
            if changed:
                self._event("army_state", aid, {"state": number(raw[0]), "from": origin, "target": target,
                    "begin": number(raw[4]), "end": number(raw[5]), "hero_type": str(raw[16])}, at, ref)
            self.db.execute(f'''INSERT INTO {current} VALUES(?,?,?,?)
                ON CONFLICT(profile_id,army_id) DO UPDATE SET state_ref=excluded.state_ref,observed_at=excluded.observed_at
                WHERE excluded.observed_at>={current}.observed_at''', (self.profile_id, aid, ident, at))
            duration = number(raw[5]) - number(raw[4])
            if origin and target and origin != target and duration > 0:
                distance = math.dist(origin, target)
                sample_id = digest([self.profile_id, aid, raw[0], raw[2:6]])
                self.db.execute(f'''INSERT INTO {marches} VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET observed_at=excluded.observed_at,message_ref=excluded.message_ref
                    WHERE excluded.observed_at<{marches}.observed_at''',
                    (sample_id, self.profile_id, aid, number(raw[0]), *origin, *target, number(raw[4]), number(raw[5]),
                     distance, duration, distance * 3600 / duration, str(raw[16]), at, ref))
                if marches=='march_samples':self._sight("march", sample_id, source_id, at)
        for aid in value[7] if isinstance(value[7], list) else ():
            self._event("out_of_view", str(aid), {"reason": "server_view_removal"}, at, ref)
        for wid, chunks in value[14].items() if isinstance(value[14], dict) else ():
            if isinstance(chunks,dict):
                import territory_events
                territory_events.observe_durability(self,wid,chunks,at,ref)
            raw = chunks.get("0") if isinstance(chunks, dict) else None
            if isinstance(raw, list) and len(raw) >= 21:
                import territory_events
                territory_events.observe_tile(self,wid,raw,at,ref)
                ident = digest([self.profile_id, wid, raw])
                self.db.execute("INSERT OR IGNORE INTO tiles VALUES(?,?,?,?,?,?,?,?,?)",
                    (ident, self.profile_id, number(wid), at, str(raw[2]), str(raw[3]), str(raw[6]), packed(raw), ref))
        if isinstance(value[17], list) and len(value[17]) == 4:
            self._event("view_coverage", "map", {"bounds": value[17]}, at, ref)
        self.intelligence.world(value, at, ref)
        if self.report_only and at-self._pending_trim_at>=30:
            reported_archive.trim(self.db,self.profile_id,at)
            self._pending_trim_at=at

    def _roster(self, value, at, ref):
        rows = value if isinstance(value, list) else []
        if rows and isinstance(rows[0], list) and rows[0] and isinstance(rows[0][0], list):
            rows = rows[0]
        known_unions = {r[0] for row in rows if isinstance(row, list) and row
            for r in self.db.execute("SELECT union_id FROM players WHERE profile_id=? AND player_key=?",
                (self.profile_id, "world:" + str(row[0]))) if r[0] not in ("", "0")}
        context_union = next(iter(known_unions)) if len(known_unions) == 1 else "0"
        if any(isinstance(row,list)and row and str(row[0])==str(self.profile['account_role_id'])for row in rows):
            context_union=str(self.profile['union_id'])
        context_name = self.db.execute("SELECT name FROM unions WHERE profile_id=? AND union_id=?",
            (self.profile_id, context_union)).fetchone()
        from attendance import roster
        roster(self,value,context_union,at)
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
        if not retention.accepts(self,battle):
            return
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
            if report_system_npc(merged,side):
                self._discard_system_npc_team(merged,side)
                continue
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
            if team_id>0:reported_archive.promote(self.db,self.profile_id,team_id,bid,at)
            configuration = {"heroes": heroes, "hero_type_raw": battle.get(side + "_hero_type"),
                "type_advance_raw": battle.get(side + "_hero_type_advance"),
                "advance_raw": battle.get(side + "_advance"), "hero_instances": instances[1:],
                "binding": binding, "npc": report_has_npc(merged),
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
                "scope": "本赛季已收到的战报及已关联队伍历史；未关联队伍只保留实时临时状态" if self.report_only else "全部接收到的本赛季数据；战役与坐标仅用于查询分类，不过滤采集",
                "archive_policy": {"reported_teams_only":bool(self.report_only),
                    "temporary_live_armies":self.db.execute('SELECT COUNT(*) FROM pending_army_current WHERE profile_id=?',(self.profile_id,)).fetchone()[0]},
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
        for key in ('force_type','is_wanderer','battle_role_id'):
            if key in profile:selected[key]=profile[key]
        if isinstance(profile.get("server_label"), str) and profile["server_label"].strip():
            selected["server_label"] = profile["server_label"].strip()[:100]
        selected_id = digest([selected[k] for k in ("server", "season", "account_role_id")])[:24]
        with self.lock, self.db:
            from profile_aliases import resolve
            selected_id=resolve(self.db,selected)
            self.db.execute("INSERT INTO profiles VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data", (selected_id, packed(selected), time.time()))
            temporary = self.profile_path.with_suffix(".json.tmp")
            temporary.write_text(json.dumps(selected, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(self.profile_path)
            self.profile, self.profile_id = selected, selected_id
            self.db.execute('INSERT OR REPLACE INTO team_archive_policy VALUES(?,?)',(selected_id,int(self.report_only)))
            if type(getattr(self,'history_floor',0))in (int,float)and self.history_floor>0:
                self.db.execute('INSERT INTO retention_policy VALUES(?,?,?) ON CONFLICT(profile_id) DO UPDATE SET retain_from=MAX(retain_from,excluded.retain_from)',(selected_id,self.history_floor,time.time()))
        self.remove_system_npc_teams()
        return {"ok": True, "profile_id": selected_id}

    def _army_lineups(self, army_id, *, history=None, known_at=None):
        projection = getattr(self, 'lineup_projection', None)
        if (projection is None or history is not None or known_at is not None
                or self.db.in_transaction and not getattr(self,'read_only',False)):
            return Archive._resolve_army_lineups(self, army_id, history=history, known_at=known_at)
        token = projection.token(self, army_id)
        if token is None:
            return self._resolve_army_lineups(army_id)
        at = time.time()
        cached = projection.lookup(self, token, at)
        if cached is not None:
            if cached[2].get('troops'):
                cached[2]['troops_observed_at'] = token['observed_at']
            # Duplicate receipts only update provenance timestamps. Refresh
            # these by primary key without invalidating the resolved lineup.
            ids = [row['id'] for row in cached[0]]
            stamps = {}
            for offset in range(0,len(ids),500):
                chunk = ids[offset:offset+500]
                stamps.update(self.db.execute('SELECT id,observed_at FROM team_versions WHERE id IN ('+
                    ','.join('?' for _ in chunk)+')',chunk).fetchall())
            for row in cached[0]:
                if row['id'] in stamps: row['observed_at'] = stamps[row['id']]
            return cached
        history = projection.history(self, token['player_name'], token['union_id'])
        result = self._resolve_army_lineups(army_id, history=history, known_at=at)
        projection.remember(token, result, history, at)
        return result

    def _resolve_army_lineups(self, army_id, *, history=None, known_at=None):
        current=reported_archive.live_table(self.db,'army_current')
        states=reported_archive.live_table(self.db,'army_states')
        owner = self.db.execute(f'''SELECT p.name,p.union_id,s.owner_uid,s.hero_type,a.observed_at FROM {current} a
            JOIN {states} s ON s.id=a.state_ref LEFT JOIN players p ON p.profile_id=a.profile_id
            AND p.player_key='world:'||s.owner_uid WHERE a.profile_id=? AND a.army_id=?''',
            (self.profile_id, str(army_id))).fetchone()
        codes = map_troop_codes(owner["hero_type"]) if owner else ()
        evidence = {"troops": describe_troops(codes), "troops_observed_at": owner["observed_at"] if codes else None}
        exact = self.db.execute('''SELECT v.* FROM team_current c JOIN team_versions v ON v.id=c.version_ref
            WHERE c.profile_id=? AND c.team_key=?''', (self.profile_id, "army:" + str(army_id))).fetchall()
        if owner and owner['name']:
            exact = [row for row in exact if row['player_name']==owner['name'] and str(row['union_id'])==str(owner['union_id'])]
        identity = owner if owner and owner['name'] else (exact[0] if exact else None)
        if identity:
            from lineup_current import resolve_current_report
            from lineup_prediction import player_history
            name = identity['name'] if identity is owner else identity['player_name']
            union = identity['union_id']
            reports, links, ambiguous = history if history is not None else player_history(self.db, self.profile_id, name, union)
            at = time.time() if known_at is None else known_at
            latest, resolution = resolve_current_report(reports, links, str(army_id), at, at,
                owner=owner['owner_uid'] if owner else None, live_codes=codes, ambiguous_player=ambiguous)
            if resolution and resolution.get('fallback'):
                evidence['previous_reference'] = resolution
                exact = []
            if latest:
                evidence.update(current_resolution=resolution, current_report_id=latest['battle_id'])
                if latest.get('_resolved_heroes') is not None:
                    evidence['current_heroes']=latest['_resolved_heroes']
                if resolution.get('unresolved'):
                    return [], 'current_report_incomplete', evidence
                instances = integers(latest.get('idu_raw'))
                formation = 'unbound:' + digest([str(latest.get('role_raw') or name),
                    instances[1:] if any(instances[1:]) else [h['id'] for h in json.loads(latest['heroes'])]])[:24]
                current = self.db.execute('''SELECT v.* FROM team_versions v JOIN combat_sides c
                    ON c.profile_id=v.profile_id AND c.battle_id=v.battle_id AND c.player_key=v.player_key
                    AND c.union_id=v.union_id WHERE v.profile_id=? AND v.battle_id=? AND v.player_name=?
                    AND v.union_id=? AND c.side=? AND v.team_key IN (?,?)
                    AND COALESCE(json_extract(v.data,'$.binding_conflict'),0)=0
                    ORDER BY v.observed_at DESC,v.id DESC LIMIT 1''',
                    (self.profile_id, latest['battle_id'], name, union, latest['side'], 'army:'+str(army_id), formation)).fetchall()
                if current:
                    match = ('same_card_instances' if latest['battle_id'] != resolution['anchor_battle_id'] else
                             'matched_arrival' if resolution['anchor_source'] == 'unique_attack_arrival' else 'exact_army_id')
                    return current, match, evidence
                resolution['unresolved'] = 'configuration_not_indexed'
                return [], 'current_report_incomplete', evidence
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
            (self.profile_id, owner["name"], owner["union_id"], time.time() if known_at is None else known_at))
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
                rows = self.db.execute('''SELECT p.*,t.force_type,t.clan_id,t.clan_name,
                    t.observed_at AS traits_observed_at FROM players p LEFT JOIN player_traits t
                    ON t.profile_id=p.profile_id AND t.player_key=p.player_key
                    WHERE p.profile_id=? AND (?='' OR p.union_id=?)
                    AND (?='' OR p.name LIKE ?) ORDER BY p.event_at DESC LIMIT ? OFFSET ?''',
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
                if kind == 'players':
                    item['is_wanderer'] = item['force_type'] == 1
                if "data" in item:
                    item["data"] = json.loads(item["data"])
                if kind in ("army_lineups", "teams", "team_history"):
                    # Seek this report's primary key; an alliance-only scan is much larger.
                    sides = self.db.execute('''SELECT side,heroes,features FROM combat_sides INDEXED BY sqlite_autoindex_combat_sides_1 WHERE profile_id=?
                        AND battle_id=? AND player_key=? AND union_id=?''',
                        (self.profile_id, item["battle_id"], item["player_key"], item["union_id"])).fetchall()
                    if len(sides) == 1:
                        side, raw_heroes, features = sides[0]
                        heroes = json.loads(raw_heroes) if kind == 'army_lineups' else item["data"]["heroes"]
                        features = json.loads(features)
                        item["data"]["heroes"] = (evidence['current_heroes'] if kind=='army_lineups'
                            and item['battle_id']==evidence.get('current_report_id') and evidence.get('current_heroes')
                            else side_heroes(heroes, side, features))
                        item["data"]["army_group_name"] = features.get(side+'_army_group') or ''
                        if kind=='army_lineups' and item['data']['army_group_name'] and match in ('exact_army_id','matched_arrival','same_card_instances'):
                            membership = self.db.execute('''SELECT json_extract(data,'$[6]') FROM army_states
                                WHERE profile_id=? AND army_id=? AND observed_at<=?
                                ORDER BY observed_at DESC LIMIT 1''',
                                (self.profile_id,str(army_id),item['event_at']+1)).fetchone()
                            item['data']['report_army_group_id'] = str(membership[0]) if membership and membership[0] else None
                    else:
                        item["data"]["heroes"] = [named_hero(h) for h in item["data"]["heroes"]]
                if "body" in item:
                    item["body"] = json.loads(item["body"])
                result.append(item)
            if kind == 'marches':
                from team_display import attach
                attach(self,result)
            elif kind == 'events':
                from team_display import attach_events
                attach_events(self,result)
            return {"items": result, "limit": limit, "offset": offset, "profile_id": self.profile_id, "match": match, **evidence}
