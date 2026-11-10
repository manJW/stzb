"""Evidence-based links and season-wide enemy/ally combat queries."""
import collections
import json
import math
import re
import time

from archive import digest, integers, number, packed
from combat_loadout import SKILLS, named_hero, side_heroes
from lineup_prediction import archived_march_key, meets_main_threshold, review_report, troop_total
from model import write_json
from observation_codec import troop_families
from official_campaigns import campaigns_from_messages, in_campaign, map_block, report_campaign

# Decompiled from the installed PC client's Tenth.Data.ReportResult (2026-11-05 snapshot).
RESULTS = {0: ("战败", "loss"), 1: ("胜利", "win"), 2: ("个人占领", "win"),
    3: ("同盟占领", "win"), 6: ("平局", "draw")}
OPPOSITE = {"win": "loss", "loss": "win", "draw": "draw", "unknown": "unknown"}
ARRIVAL_INFERENCE = "unique_attack_arrival"
MAIN_DETAIL_PRIORITY = 80
ALLIANCE_REPORT = """(CAST(json_extract(b.data,'$.attack_unionid') AS TEXT)=:union_id
    OR CAST(json_extract(b.data,'$.defend_unionid') AS TEXT)=:union_id)"""


def report_references(raw):
    if not isinstance(raw, list) or len(raw) != 33 or not isinstance(raw[31], str):
        return []
    result = []
    for segment in raw[31].split(";"):
        if re.fullmatch(r"[0-9]+(?:,[0-9]+){5}", segment):
            values = [int(x) for x in segment.split(",")]
            if values[0] > 0:
                result.append(values)
    return result


def wilson(wins, total):
    if not total:
        return None
    z = 1.959963984540054
    p = wins / total
    denominator = 1 + z*z/total
    center = (p + z*z/(2*total)) / denominator
    half = z * math.sqrt(p*(1-p)/total + z*z/(4*total*total)) / denominator
    return [max(0, center-half), min(1, center+half)]


class Intelligence:
    def __init__(self, archive):
        self.archive, self.db = archive, archive.db
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS army_report_refs(
          id TEXT PRIMARY KEY,profile_id TEXT NOT NULL,army_id TEXT NOT NULL,battle_id TEXT NOT NULL,
          owner_uid TEXT NOT NULL,owner_name TEXT,union_id TEXT,raw_reference TEXT NOT NULL,
          observed_at REAL NOT NULL,state_ref TEXT,message_ref TEXT);
        CREATE INDEX IF NOT EXISTS army_report_lookup ON army_report_refs(profile_id,battle_id,owner_name,union_id);
        CREATE TABLE IF NOT EXISTS combat_sides(
          profile_id TEXT NOT NULL,battle_id TEXT NOT NULL,side TEXT NOT NULL,player_key TEXT NOT NULL,
          player_name TEXT,union_id TEXT,army_id TEXT,binding TEXT NOT NULL,event_at REAL NOT NULL,
          wid INTEGER,result_code INTEGER,outcome TEXT NOT NULL,npc INTEGER NOT NULL,
          hero_key TEXT NOT NULL,initial_hp INTEGER,heroes TEXT NOT NULL,features TEXT NOT NULL,
          observed_at REAL NOT NULL,message_ref TEXT,PRIMARY KEY(profile_id,battle_id,side));
        CREATE INDEX IF NOT EXISTS combat_player ON combat_sides(profile_id,union_id,player_name,event_at);
        CREATE INDEX IF NOT EXISTS combat_army ON combat_sides(profile_id,army_id,event_at);
        CREATE INDEX IF NOT EXISTS combat_arrival ON combat_sides(profile_id,side,wid,event_at);
        CREATE INDEX IF NOT EXISTS message_kind ON messages(profile_id,message_id,first_seen);
        CREATE INDEX IF NOT EXISTS march_arrival ON march_samples(profile_id,state_code,to_x,to_y,end_at);
        CREATE INDEX IF NOT EXISTS march_army_end ON march_samples(profile_id,army_id,end_at);
        CREATE TABLE IF NOT EXISTS battle_march_links(
          profile_id TEXT NOT NULL,battle_id TEXT NOT NULL,side TEXT NOT NULL,march_id TEXT NOT NULL,
          method TEXT NOT NULL,arrival_difference REAL NOT NULL,
          PRIMARY KEY(profile_id,battle_id,side,march_id));
        CREATE INDEX IF NOT EXISTS battle_march_by_march ON battle_march_links(profile_id,march_id,method,battle_id,side);
        CREATE TABLE IF NOT EXISTS same_army_audits(
          profile_id TEXT NOT NULL,battle_id TEXT NOT NULL,data TEXT NOT NULL,
          PRIMARY KEY(profile_id,battle_id));
        CREATE TABLE IF NOT EXISTS campaign_views(
          id TEXT PRIMARY KEY,profile_id TEXT NOT NULL,name TEXT NOT NULL,filters TEXT NOT NULL,created_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS report_read_jobs(
          profile_id TEXT NOT NULL,kind TEXT NOT NULL,battle_id TEXT NOT NULL,state TEXT NOT NULL,
          attempts INTEGER NOT NULL,queued_at REAL NOT NULL,updated_at REAL NOT NULL,last_error TEXT,
          PRIMARY KEY(profile_id,kind,battle_id));
        ''')
        if "priority" not in {r[1] for r in self.db.execute("PRAGMA table_info(report_read_jobs)")}:
            self.db.execute("ALTER TABLE report_read_jobs ADD COLUMN priority INTEGER NOT NULL DEFAULT 0")
        self.db.commit()

    @property
    def profile_id(self):
        return self.archive.profile_id

    def observe_army(self, aid, raw, at, message_ref, state_ref=None):
        references = report_references(raw)
        if not references:
            return set()
        owner = self.db.execute("SELECT name,union_id FROM players WHERE profile_id=? AND player_key=?",
            (self.profile_id, "world:" + str(raw[1]))).fetchone()
        affected = set()
        for reference in references:
            bid = str(reference[0])
            ident = digest([self.profile_id, str(aid), bid, str(raw[1]), owner["name"] if owner else None,
                owner["union_id"] if owner else None, reference])
            inserted = self.db.execute("INSERT OR IGNORE INTO army_report_refs VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (ident, self.profile_id, str(aid), bid, str(raw[1]), owner["name"] if owner else None,
                 owner["union_id"] if owner else None, packed(reference), at, state_ref, message_ref)).rowcount
            if inserted:
                affected.add(bid)
                if not self.db.execute("SELECT 1 FROM battle_reports WHERE profile_id=? AND battle_id=?", (self.profile_id, bid)).fetchone():
                    self.queue_evidence("profile", bid, at)
        return affected

    def army_link(self, battle, side, heroes):
        name, union = str(battle.get(side + "_name", "")), str(battle.get(side + "_unionid", "0"))
        other = "defend" if side == "attack" else "attack"
        if not name or (name == battle.get(other + "_name") and union == str(battle.get(other + "_unionid"))):
            return None
        rows = self.db.execute('''SELECT * FROM army_report_refs WHERE profile_id=? AND battle_id=?
            AND owner_name=? AND union_id=? ORDER BY observed_at''',
            (self.profile_id, str(battle["battle_id"]), name, union)).fetchall()
        if not rows or len({r["army_id"] for r in rows}) != 1:
            return None
        row = rows[0]
        return {"source": "world_army_battle_reference", "army_id": row["army_id"], "world_uid": row["owner_uid"],
            "battle_id": row["battle_id"], "message_ref": row["message_ref"], "state_ref": row["state_ref"],
            "observed_at": row["observed_at"], "validated": ["explicit_battle_id", "world_owner_name", "union_id", "unique_army"],
            "raw_reference": json.loads(row["raw_reference"])}

    def retire_conflicts(self, battle, side):
        aids = self.db.execute('''SELECT DISTINCT army_id FROM army_report_refs WHERE profile_id=?
            AND battle_id=? AND owner_name=? AND union_id=?''',
            (self.profile_id, str(battle["battle_id"]), str(battle.get(side+"_name", "")), str(battle.get(side+"_unionid", "0")))).fetchall()
        if len(aids) < 2:
            return
        rows = self.db.execute('''SELECT v.id,v.data FROM team_current c JOIN team_versions v ON v.id=c.version_ref
            WHERE c.profile_id=? AND v.battle_id=? AND v.player_name=? AND v.union_id=?
            AND json_extract(v.data,'$.binding_evidence.source')='world_army_battle_reference' ''',
            (self.profile_id, str(battle["battle_id"]), str(battle.get(side+"_name", "")), str(battle.get(side+"_unionid", "0")))).fetchall()
        for row in rows:
            data = json.loads(row["data"])
            data["binding_conflict"] = True
            self.db.execute("UPDATE team_versions SET data=? WHERE id=?", (packed(data), row["id"]))
            self.db.execute("DELETE FROM team_current WHERE profile_id=? AND version_ref=?", (self.profile_id, row["id"]))

    def world(self, value, at, message_ref):
        affected, armies, arrivals = set(), set(), set()
        for aid, raw in value[6].items() if isinstance(value[6], dict) else ():
            if not isinstance(raw, list) or len(raw) != 33 or not number(raw[0]):
                continue
            affected.update(self.observe_army(aid, raw, at, message_ref, digest([self.profile_id, str(aid), raw])))
            armies.add(str(aid))
            if number(raw[0]) == 1 and number(raw[3]) > 0 and number(raw[5]) > number(raw[4]):
                arrivals.add((number(raw[3]), number(raw[5])))
        for bid in affected:
            report = self.db.execute("SELECT data,observed_at,message_ref FROM battle_reports WHERE profile_id=? AND battle_id=?",
                (self.profile_id, bid)).fetchone()
            if report:
                self.archive._battle(json.loads(report["data"]), report["observed_at"], report["message_ref"])
        for aid in armies:
            for row in self.db.execute("SELECT battle_id,side,wid,event_at FROM combat_sides WHERE profile_id=? AND army_id=?",
                    (self.profile_id, aid)).fetchall():
                self.link_marches(row["battle_id"], row["side"], aid, row["wid"], row["event_at"])
        for wid, event_at in arrivals:
            for row in self.db.execute("""SELECT battle_id,player_name,union_id FROM combat_sides
                WHERE profile_id=? AND side='attack' AND wid=? AND event_at=? AND army_id IS NULL""",
                (self.profile_id, wid, event_at)).fetchall():
                self.link_marches(row["battle_id"], "attack", None, wid, event_at, row["player_name"], row["union_id"])

    def features(self, battle, side):
        skills = {}
        for segment in str(battle.get("all_skill_info", "")).split(";"):
            values = integers(segment)
            if values:
                skills[values[0]] = [{"id": values[i], "level": values[i+1],
                    "name": self.archive.skills.get(str(values[i]), {}).get("name", "战法#" + str(values[i]))}
                    for i in range(1, len(values)-1, 2) if values[i] > 0] or None
        heroes = []
        for pos, segment in enumerate(str(battle.get(side + "_all_hero_info", "")).split(";")[:3], 1):
            row = integers(segment)
            if not row or row[0] <= 0:
                continue
            hero = {"position": pos, "id": row[0],
                "name": self.archive.heroes.get(str(row[0]), {}).get("name", "武将#" + str(row[0])),
                "level": row[1] if len(row) > 1 else None, "initial_hp": row[2] if len(row) > 2 else None,
                "remaining_hp": row[3] if len(row) > 3 else None,
                "skills": skills.get(pos if side == "attack" else 7-pos)}
            heroes.append(hero)
        return side_heroes(heroes, side, battle)

    def battle(self, battle, at, message_ref):
        bid, event_at, wid = str(battle["battle_id"]), number(battle.get("time")), number(battle.get("wid"))
        result_code = number(battle.get("result"), -1)
        attack_outcome = RESULTS.get(result_code, ("特殊结果待解释", "unknown"))[1]
        own = str(self.archive.profile["union_id"])
        alliance = own not in ("", "0") and own in (str(battle.get("attack_unionid")), str(battle.get("defend_unionid")))
        needs_detail = alliance
        main_sides = 0
        for side in ("attack", "defend"):
            name = str(battle.get(side + "_name", ""))
            if not name:
                continue
            heroes = self.features(battle, side)
            troops = sum(h["initial_hp"] or 0 for h in heroes)
            main_sides += meets_main_threshold(heroes)
            ids = integers(battle.get(side + "_idu"))
            aid = str(ids[0]) if ids and ids[0] > 0 else None
            proof = self.archive._report_army_link(battle, side, heroes) if not aid else None
            if proof:
                aid = proof["army_id"]
            union = str(battle.get(side + "_unionid", "0"))
            outcome = attack_outcome if side == "attack" else OPPOSITE[attack_outcome]
            features = {k: v for k, v in battle.items() if k.startswith(side + "_") or k.startswith(side + "er_") or
                k in ("all_skill_info", "weather", "in_night_mode", "fight_type", "battle_scenes", "military", "extra_result")}
            features["binding_evidence"] = proof
            features["result_label"] = RESULTS.get(result_code, ("结果#" + str(result_code),))[0]
            self.db.execute('''INSERT INTO combat_sides VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(profile_id,battle_id,side) DO UPDATE SET player_key=excluded.player_key,
                player_name=excluded.player_name,union_id=excluded.union_id,army_id=excluded.army_id,binding=excluded.binding,
                event_at=excluded.event_at,wid=excluded.wid,result_code=excluded.result_code,outcome=excluded.outcome,
                npc=excluded.npc,hero_key=excluded.hero_key,initial_hp=excluded.initial_hp,heroes=excluded.heroes,
                features=excluded.features,observed_at=MAX(combat_sides.observed_at,excluded.observed_at),message_ref=excluded.message_ref''',
                (self.profile_id, bid, side, "battle:" + str(battle.get(side + "_role_id") or name), name,
                 union, aid, "exact_army_id" if aid else "unbound_formation",
                 event_at, wid, result_code, outcome, int(bool(number(battle["npc"]))) if "npc" in battle else -1,
                 ",".join(str(h["id"]) for h in heroes), troops,
                 packed(heroes), packed(features), at, message_ref))
            self.link_marches(bid, side, aid, wid, event_at, name, union)
            needs_detail = needs_detail or bool(aid)
        if needs_detail:
            priority = MAIN_DETAIL_PRIORITY if alliance and main_sides == 2 and "npc" in battle and not number(battle["npc"]) else 0
            self.queue_evidence("detail", bid, at, priority=priority)

    def alliance_report_params(self):
        return {"profile_id": self.profile_id, "union_id": str(self.archive.profile["union_id"])}

    def seed_alliance_details(self):
        inserted = self.db.execute(f"""INSERT OR IGNORE INTO report_read_jobs
            (profile_id,kind,battle_id,state,attempts,queued_at,updated_at)
            SELECT b.profile_id,'detail',b.battle_id,'queued',0,b.observed_at,b.observed_at
            FROM battle_reports b WHERE b.profile_id=:profile_id AND :union_id NOT IN ('','0') AND {ALLIANCE_REPORT}
            AND NOT EXISTS(SELECT 1 FROM report_bodies f WHERE f.profile_id=b.profile_id AND f.battle_id=b.battle_id)""",
            self.alliance_report_params()).rowcount
        candidates = self.db.execute("""SELECT j.battle_id,j.priority,a.heroes,b.heroes AS opposing_heroes
            FROM report_read_jobs j
            JOIN combat_sides a ON a.profile_id=j.profile_id AND a.battle_id=j.battle_id AND a.side='attack'
            JOIN combat_sides b ON b.profile_id=j.profile_id AND b.battle_id=j.battle_id AND b.side='defend'
            WHERE j.profile_id=:profile_id AND j.kind='detail' AND j.state='queued' AND j.priority<=:main_priority
            AND :union_id NOT IN ('','0') AND (a.union_id=:union_id OR b.union_id=:union_id)
            AND a.npc=0 AND b.npc=0""", {**self.alliance_report_params(), "main_priority": MAIN_DETAIL_PRIORITY})
        updates = []
        for row in candidates:
            teams = [json.loads(row[field]) for field in ("heroes", "opposing_heroes")]
            priority = MAIN_DETAIL_PRIORITY if all(meets_main_threshold(heroes) for heroes in teams) else 60 if row["priority"] == 60 else 0
            if priority != row["priority"]:
                updates.append((priority, self.profile_id, row["battle_id"]))
        self.db.executemany("""UPDATE report_read_jobs SET priority=?
            WHERE profile_id=? AND battle_id=? AND kind='detail' AND state='queued' AND priority<=?""",
            ((*update, MAIN_DETAIL_PRIORITY) for update in updates))
        return inserted

    def alliance_report_coverage(self):
        return dict(self.db.execute(f"""SELECT COUNT(*) AS reports, COUNT(f.battle_id) AS full_reports
            FROM battle_reports b LEFT JOIN report_bodies f ON f.profile_id=b.profile_id AND f.battle_id=b.battle_id
            WHERE b.profile_id=:profile_id AND :union_id NOT IN ('','0') AND {ALLIANCE_REPORT}""",
            self.alliance_report_params()).fetchone())

    def queue_evidence(self, kind, bid, at=None, retry=False, priority=0):
        if kind not in ("profile", "detail") or not str(bid).isdigit() or not 0 < int(bid) <= 9223372036854775807:
            raise ValueError("战报读取参数无效")
        bid = str(int(bid))
        table = "battle_reports" if kind == "profile" else "report_bodies"
        if self.db.execute(f"SELECT 1 FROM {table} WHERE profile_id=? AND battle_id=?", (self.profile_id, bid)).fetchone():
            return False
        known = self.db.execute("SELECT 1 FROM battle_reports WHERE profile_id=? AND battle_id=? UNION SELECT 1 FROM army_report_refs WHERE profile_id=? AND battle_id=? LIMIT 1",
            (self.profile_id, bid, self.profile_id, bid)).fetchone()
        if not known:
            raise ValueError("只能读取当前档案已经观测到的战报编号")
        at = time.time() if at is None else at
        changed = self.db.execute("INSERT OR IGNORE INTO report_read_jobs(profile_id,kind,battle_id,state,attempts,queued_at,updated_at,last_error,priority) VALUES(?,?,?,?,?,?,?,?,?)",
            (self.profile_id, kind, bid, "queued", 0, at, at, None, priority)).rowcount
        if retry:
            changed += self.db.execute("UPDATE report_read_jobs SET state='queued',last_error=NULL,updated_at=? WHERE profile_id=? AND kind=? AND battle_id=? AND state IN ('failed','paused') AND attempts<3",
                (time.time(), self.profile_id, kind, bid)).rowcount
            priority = 100
        if priority:
            self.db.execute("UPDATE report_read_jobs SET priority=? WHERE profile_id=? AND kind=? AND battle_id=? AND state='queued' AND priority<?",
                (priority, self.profile_id, kind, bid, priority))
        return bool(changed)

    def prioritize_review_detail(self, bid, march, event_at):
        key = archived_march_key(self.profile_id, march)
        if self.db.execute('''SELECT 1 FROM lineup_forecasts WHERE profile_id=? AND march_key=? AND issued_at<? LIMIT 1''',
            (self.profile_id, key, min(event_at,march['end_at']))).fetchone():
            self.queue_evidence('detail', bid, priority=60)
            related = self.db.execute('''SELECT battle_id FROM battle_march_links WHERE profile_id=?
                AND march_id=? AND side='attack' ''', (self.profile_id, march['id'])).fetchall()
            for report in related:
                self.save_same_army_audit(report['battle_id'])

    def save_same_army_audit(self, bid):
        review = review_report(self.db, self.profile_id, bid)
        audit = (review or {}).get('same_army_audit')
        if audit:
            self.db.execute('''INSERT INTO same_army_audits VALUES(?,?,?) ON CONFLICT(profile_id,battle_id)
                DO UPDATE SET data=excluded.data WHERE data<>excluded.data''', (self.profile_id, bid, packed(audit)))
        else:
            self.db.execute('DELETE FROM same_army_audits WHERE profile_id=? AND battle_id=?', (self.profile_id, bid))

    def backfill_same_army_audits(self):
        rows = self.db.execute('''SELECT DISTINCT battle_id FROM battle_march_links WHERE profile_id=? AND side='attack'
            AND method IN ('coordinate_time_match','unique_attack_arrival')''', (self.profile_id,)).fetchall()
        for row in rows:
            self.save_same_army_audit(row['battle_id'])

    def link_marches(self, bid, side, aid, wid, event_at, name=None, union=None):
        self.db.execute("DELETE FROM battle_march_links WHERE profile_id=? AND battle_id=? AND side=?",
            (self.profile_id, str(bid), side))
        if side == 'attack':
            self.db.execute('DELETE FROM same_army_audits WHERE profile_id=? AND battle_id=?', (self.profile_id, str(bid)))
        if not wid or not event_at:
            return
        if not aid:
            if side == "attack" and name and union not in (None, "", "0"):
                self.infer_attack_arrival(str(bid), wid, event_at, name, union)
            return
        # Same army is directly linked by a report reference; the individual march
        # still requires a coordinate/time match. Never attach its later retreat.
        rows = self.db.execute('''SELECT * FROM march_samples WHERE profile_id=? AND army_id=?
            AND state_code IN (1,6) AND to_x=? AND to_y=? AND ABS(end_at-?)<=15''',
            (self.profile_id, str(aid), wid//10000, wid%10000, event_at)).fetchall()
        for row in rows:
            self.db.execute("INSERT OR REPLACE INTO battle_march_links VALUES(?,?,?,?,?,?)",
                (self.profile_id, str(bid), side, row["id"], "coordinate_time_match" if len(rows)==1 else "ambiguous_marches",
                 row["end_at"]-event_at))
        if side == 'attack' and len(rows) == 1:
            self.prioritize_review_detail(bid, rows[0], event_at)

    def infer_attack_arrival(self, bid, wid, event_at, name, union):
        # An exact scheduled arrival can support an inference. An explicit conflict
        # or multiple possible players/routes leaves the report unbound.
        if self.db.execute("""SELECT 1 FROM army_report_refs WHERE profile_id=? AND battle_id=?
            AND owner_name=? AND union_id=?""", (self.profile_id, bid, name, union)).fetchone():
            return
        owners = self.db.execute("""SELECT DISTINCT world_uid FROM players WHERE profile_id=? AND name=?
            AND union_id=? AND world_uid IS NOT NULL LIMIT 2""", (self.profile_id, name, union)).fetchall()
        if len(owners) != 1:
            return
        rows = self.db.execute("""SELECT m.* FROM march_samples m WHERE m.profile_id=? AND m.state_code=1
            AND m.to_x=? AND m.to_y=? AND m.end_at=? AND m.observed_at<=m.end_at
            AND EXISTS(SELECT 1 FROM army_states s WHERE s.profile_id=m.profile_id AND s.army_id=m.army_id
                AND s.message_ref=m.message_ref AND s.owner_uid=? AND s.state_code=m.state_code
                AND s.begin_at=m.begin_at AND s.end_at=m.end_at AND s.from_x=m.from_x AND s.from_y=m.from_y
                AND s.to_x=m.to_x AND s.to_y=m.to_y) LIMIT 2""",
            (self.profile_id, wid//10000, wid%10000, event_at, owners[0]["world_uid"])).fetchall()
        if len(rows) == 1:
            self.db.execute("INSERT INTO battle_march_links VALUES(?,?,?,?,?,?)",
                (self.profile_id, bid, "attack", rows[0]["id"], ARRIVAL_INFERENCE, 0))
            self.prioritize_review_detail(bid, rows[0], event_at)

    def official_campaigns(self):
        return campaigns_from_messages(self.db.execute("""SELECT message_id,payload,first_seen FROM messages
            WHERE profile_id=? AND message_id IN (3807,3811) ORDER BY first_seen""", (self.profile_id,)), self.archive.profile)

    def filters(self, query):
        filters = dict(query)
        if filters.get("campaign_id"):
            row = self.db.execute("SELECT filters FROM campaign_views WHERE profile_id=? AND id=?",
                (self.profile_id, filters["campaign_id"])).fetchone()
            if not row:
                raise ValueError("未找到此赛季的战役视角")
            filters = {**json.loads(row[0]), **{k:v for k,v in filters.items() if v not in (None, "")}}
        region = filters.get("region")
        if region:
            values = str(region).split(",") if not isinstance(region, list) else region
            if len(values) != 4 or any(not re.fullmatch(r"[0-9]+", str(x)) for x in values):
                raise ValueError("范围格式：最小X,最小Y,最大X,最大Y")
            region = [int(x) for x in values]
            if not all(0 < x < 10000 for x in region) or region[0] > region[2] or region[1] > region[3]:
                raise ValueError("坐标范围无效")
        result = {"region": region, "from_at": 0, "to_at": 0, "search": str(filters.get("search", "")).strip()[:100],
            "union_id": str(filters.get("union_id", "")), "army_id": str(filters.get("army_id", "")),
            "relation": str(filters.get("relation", "enemy"))}
        if filters.get("official_campaign_id"):
            event_id = str(filters["official_campaign_id"])
            if event_id not in self.official_campaigns():
                raise ValueError("未找到本赛季的官方战役")
            result["official_campaign_id"] = event_id
        for key in ("from_at", "to_at"):
            if filters.get(key):
                try:
                    value = float(filters[key])
                    if not math.isfinite(value) or value < 0:
                        raise ValueError()
                    result[key] = value
                except (TypeError, ValueError):
                    raise ValueError("战役时间格式无效")
        if result["from_at"] and result["to_at"] and result["from_at"] > result["to_at"]:
            raise ValueError("开始时间不能晚于结束时间")
        return result

    def unions(self, filters):
        if filters["union_id"]:
            return {filters["union_id"]}
        config = self.archive.root / "config.json"
        data = json.loads(config.read_text(encoding="utf-8-sig")) if config.exists() else {}
        if filters["relation"] == "enemy":
            return {str(uid) for uid, relation in data.get("relations", {}).items() if relation.get("relation") == "enemy"}
        if filters["relation"] == "ally":
            return {str(self.archive.profile["union_id"])}
        return None

    @staticmethod
    def inside(x, y, region):
        return bool(x and y and region[0] <= x <= region[2] and region[1] <= y <= region[3])

    def add_march_evidence(self, item):
        item["march_links"] = [dict(r) for r in self.db.execute('''SELECT l.*,m.army_id,m.from_x,m.from_y,m.to_x,m.to_y,
            m.begin_at,m.end_at,m.duration_seconds,m.observed_at,m.message_ref
            FROM battle_march_links l JOIN march_samples m ON m.id=l.march_id
            WHERE l.profile_id=? AND l.battle_id=? AND l.side=?''', (self.profile_id, item["battle_id"], item["side"]))]
        item["inferred_army_id"] = next((r["army_id"] for r in item["march_links"] if r["method"] == ARRIVAL_INFERENCE), None)

    def encounters(self, filters):
        unions = self.unions(filters)
        campaigns = self.official_campaigns()
        selected = filters.get("official_campaign_id")
        result = []
        query = '''SELECT c.*,EXISTS(SELECT 1 FROM report_bodies b WHERE b.profile_id=c.profile_id
                AND b.battle_id=c.battle_id) AS has_full_report,
                json_extract(p.data,'$.block_id') AS battle_block,
                json_extract(p.data,'$.attack_unionid') AS attack_union,
                json_extract(p.data,'$.defend_unionid') AS defend_union
                FROM combat_sides c JOIN battle_reports p ON p.profile_id=c.profile_id AND p.battle_id=c.battle_id
                WHERE c.profile_id=:profile'''
        params = {"profile": self.profile_id, "aid": filters["army_id"]}
        if filters["army_id"]:
            query += ''' AND (c.battle_id,c.side) IN (
                SELECT b.battle_id,b.side FROM combat_sides b WHERE b.profile_id=:profile AND b.army_id=:aid
                UNION SELECT l.battle_id,l.side FROM march_samples m CROSS JOIN battle_march_links l
                ON l.profile_id=m.profile_id AND l.march_id=m.id AND l.method='unique_attack_arrival'
                WHERE m.profile_id=:profile AND m.army_id=:aid)'''
        for row in self.db.execute(query + ' ORDER BY c.event_at DESC', params):
            item = dict(row)
            if unions is not None and item["union_id"] not in unions:
                continue
            if filters["from_at"] and item["event_at"] < filters["from_at"] or filters["to_at"] and item["event_at"] > filters["to_at"]:
                continue
            if filters["region"] and not self.inside(item["wid"]//10000, item["wid"]%10000, filters["region"]):
                continue
            ids = {str(item.pop(k)) for k in ("attack_union", "defend_union")} - {"0", "None", ""}
            item["campaign"] = report_campaign(campaigns, item.pop("battle_block"), item["event_at"], ids)
            if selected and (not item["campaign"] or selected != item["campaign"]["id"]):
                continue
            self.add_march_evidence(item)
            if filters["army_id"] and filters["army_id"] not in (item["army_id"], item["inferred_army_id"]):
                continue
            item["features"] = json.loads(item["features"])
            item["heroes"] = side_heroes(json.loads(item["heroes"]), item["side"], item["features"])
            instances = integers(item["features"].get(item["side"] + "_idu"))
            item["team_key"] = "army:" + item["army_id"] if item["army_id"] else "unbound:" + digest([
                item["player_key"][7:], instances[1:] if any(instances[1:]) else [h["id"] for h in item["heroes"]]])[:24]
            if item["inferred_army_id"]:
                item["team_key"] = "inferred-army:" + item["inferred_army_id"]
            if filters["search"] and filters["search"] not in packed([item["player_name"], item["army_id"], item["inferred_army_id"], item["heroes"], item["battle_id"]]):
                continue
            result.append(item)
        return result

    def operations(self, filters):
        unions = self.unions(filters)
        selected = filters.get("official_campaign_id")
        campaign = self.official_campaigns().get(selected) if selected else None
        result = []
        query = '''SELECT m.*,s.owner_uid,p.name AS player_name,p.union_id FROM march_samples m
            LEFT JOIN army_current a ON a.profile_id=m.profile_id AND a.army_id=m.army_id
            LEFT JOIN army_states s ON s.id=a.state_ref LEFT JOIN players p ON p.profile_id=m.profile_id
            AND p.player_key='world:'||s.owner_uid WHERE m.profile_id=:profile'''
        params = {"profile": self.profile_id, "aid": filters["army_id"]}
        if filters["army_id"]:
            query += ' AND m.army_id=:aid'
        for row in self.db.execute(query + ' ORDER BY m.begin_at DESC', params):
            item = dict(row)
            if unions is not None and item["union_id"] not in unions:
                continue
            if filters["from_at"] and item["end_at"] < filters["from_at"] or filters["to_at"] and item["begin_at"] > filters["to_at"]:
                continue
            region = filters["region"]
            if region and not (self.inside(item["from_x"], item["from_y"], region) or self.inside(item["to_x"], item["to_y"], region)):
                continue
            if campaign and not (item["to_x"] and item["to_y"] and
                    in_campaign(campaign, map_block(item["to_x"], item["to_y"]), item["end_at"], (item["union_id"],))):
                continue
            if filters["search"] and filters["search"] not in packed([item["player_name"], item["army_id"]]):
                continue
            item["battles"] = [dict(r) for r in self.db.execute('''SELECT c.battle_id,c.side,c.event_at,c.outcome,c.player_name,
                c.heroes,l.method FROM battle_march_links l JOIN combat_sides c ON c.profile_id=l.profile_id
                AND c.battle_id=l.battle_id AND c.side=l.side WHERE l.profile_id=? AND l.march_id=?''', (self.profile_id, item["id"]))]
            for battle in item["battles"]:
                battle["heroes"] = [named_hero(h) for h in json.loads(battle["heroes"])]
            result.append(item)
        return result

    def matchup_stats(self, filters):
        # Perspective is always our alliance. Count each report once, not once per side.
        all_sides = self.encounters({**filters, "relation": "all", "union_id": "", "army_id": "", "search": ""})
        grouped = collections.defaultdict(dict)
        for row in all_sides:
            grouped[row["battle_id"]][row["side"]] = row
        enemy_unions = self.unions(filters)
        groups, excluded = {}, collections.Counter()
        own = str(self.archive.profile["union_id"])
        for bid, sides in grouped.items():
            if set(sides) != {"attack", "defend"}:
                excluded["missing_side"] += 1
                continue
            a, b = sides["attack"], sides["defend"]
            if b["union_id"] == own:
                a, b = b, a
            if a["union_id"] != own or (enemy_unions is not None and b["union_id"] not in enemy_unions):
                continue
            if filters["army_id"] and filters["army_id"] not in (a["army_id"], b["army_id"]):
                continue
            if filters["search"] and filters["search"] not in packed([a["player_name"], b["player_name"], a["heroes"], b["heroes"]]):
                continue
            if a["npc"] or b["npc"] or len(a["heroes"]) != 3 or len(b["heroes"]) != 3:
                excluded["npc_or_incomplete_lineup"] += 1
                continue
            if min(a["initial_hp"], b["initial_hp"]) < 1000:
                excluded["scouting_or_low_troops"] += 1
                continue
            if a["outcome"] == "unknown":
                excluded["unmapped_result"] += 1
                continue
            key = (a["hero_key"], b["hero_key"], a["side"])
            entry = groups.setdefault(key, {"own_heroes": a["heroes"], "enemy_heroes": b["heroes"], "own_side": a["side"],
                "wins": 0, "losses": 0, "draws": 0, "battle_ids": [], "latest_at": 0})
            entry[{"win":"wins", "loss":"losses", "draw":"draws"}[a["outcome"]]] += 1
            entry["battle_ids"].append(bid)
            entry["latest_at"] = max(entry["latest_at"], a["event_at"])
        for entry in groups.values():
            entry["samples"] = len(entry["battle_ids"])
            entry["historical_win_rate"] = entry["wins"] / entry["samples"]
            entry["win_rate_interval_95"] = wilson(entry["wins"], entry["samples"])
            entry["sample_status"] = "insufficient" if entry["samples"] < 30 else "descriptive_only"
            entry["predicted_win_probability"] = None
        return {"items": sorted(groups.values(), key=lambda x: (-x["samples"], -x["latest_at"])),
            "excluded": dict(excluded), "method": "historical_hero_order_and_attack_defend_side_v1",
            "policy": "仅为本库观测样本的历史胜率，平局计入分母。未控制战法、等级、兵力、红度、装备、士气与重复交战；不是未来胜率或已验证的克制关系。"}

    def main_samples(self, filters):
        from defense import REENGAGEMENT_SECONDS, matchup_keys

        own = str(self.archive.profile["union_id"])
        opponents = self.unions(filters) if filters["relation"] != "ally" or filters["union_id"] else None
        campaigns = self.official_campaigns()
        selected = filters.get("official_campaign_id")
        any_main = filters["main_scope"] == "either"
        query = """SELECT a.battle_id,a.side,a.event_at,a.wid,a.outcome,a.player_key,a.player_name,a.heroes,a.features,
            b.side AS opponent_side,b.player_key AS opponent_key,b.player_name AS opponent_name,
            b.union_id AS opponent_union,b.heroes AS opponent_heroes,b.features AS opponent_features,
            json_extract(p.data,'$.block_id') AS battle_block,
            EXISTS (SELECT 1 FROM report_bodies r WHERE r.profile_id=a.profile_id AND r.battle_id=a.battle_id) AS has_full_report
            FROM combat_sides a JOIN combat_sides b ON a.profile_id=b.profile_id AND a.battle_id=b.battle_id AND a.side<>b.side
            JOIN battle_reports p ON p.profile_id=a.profile_id AND p.battle_id=a.battle_id
            WHERE a.profile_id=:profile AND a.union_id=:own AND b.union_id NOT IN (:own,'','0')
              AND a.npc=0 AND b.npc=0
              AND a.outcome IN ('win','loss','draw') AND a.event_at>0
              AND a.event_at>=:from_at AND (:to_at=0 OR a.event_at<=:to_at)"""
        if filters["army_id"]:
            query += """ AND (a.army_id=:army_id OR b.army_id=:army_id OR EXISTS (
                SELECT 1 FROM battle_march_links l JOIN march_samples m ON m.id=l.march_id AND m.profile_id=l.profile_id
                WHERE l.profile_id=a.profile_id AND l.battle_id=a.battle_id AND l.method='unique_attack_arrival'
                AND m.army_id=:army_id))"""
        params = {**filters, "profile": self.profile_id, "own": own}
        samples, recent = [], {}
        coverage = dict(reports=0, full_reports=0, samples=0, repeated=0, complete_builds=0, known_troops=0)
        for row in self.db.execute(query + " ORDER BY a.event_at,a.battle_id", params):
            if opponents is not None and row["opponent_union"] not in opponents:
                continue
            if filters["region"] and not self.inside(*divmod(row["wid"] or 0, 10000), filters["region"]):
                continue
            campaign = report_campaign(campaigns, row["battle_block"], row["event_at"], (own, row["opponent_union"]))
            if selected and (not campaign or selected != campaign["id"]):
                continue
            a, b = json.loads(row["heroes"]), json.loads(row["opponent_heroes"])
            main_sides = meets_main_threshold(a), meets_main_threshold(b)
            if not (any(main_sides) if any_main else all(main_sides)):
                continue
            a = side_heroes(a, row["side"], json.loads(row["features"]))
            b = side_heroes(b, row["opponent_side"], json.loads(row["opponent_features"]))
            if filters["search"] and filters["search"] not in packed(
                    [row["battle_id"], row["player_name"], row["opponent_name"], a, b]):
                continue
            coverage["reports"] += 1
            coverage["full_reports"] += row["has_full_report"]
            keys = matchup_keys(a, b)
            troop_pair = tuple(tuple(h["troop_code"] for h in team) for team in (a, b))
            families = tuple(troop_families(code or 0 for code in codes) for codes in troop_pair)
            key = (row["side"], keys["formation"], families, main_sides)
            event = (key, row["player_key"], row["opponent_key"], row["wid"], troop_pair, keys["tactics"])
            at = row["event_at"]
            if event in recent and at - recent[event] <= REENGAGEMENT_SECONDS:
                coverage["repeated"] += 1
                continue
            recent[event] = at
            coverage["samples"] += 1
            coverage["complete_builds"] += keys["build"] is not None
            coverage["known_troops"] += all(all(codes) for codes in troop_pair)
            samples.append({**dict(row), "own_heroes": a, "enemy_heroes": b, "campaign":campaign,
                            "own_main": main_sides[0], "enemy_main": main_sides[1], "group":key, "build":keys["build"]})
        return samples, coverage

    def main_matchup_stats(self, filters):
        from defense import COUNTER_SKILLS, rule_evidence, summarize

        samples, coverage = self.main_samples(filters)
        groups, counter_cases = {}, collections.defaultdict(list)
        for sample in samples:
            a, b = sample["own_heroes"], sample["enemy_heroes"]
            sample["own_rules"], sample["enemy_rules"] = rule_evidence(a, b, SKILLS), rule_evidence(b, a, SKILLS)
            for side in ("own", "enemy"):
                for rule in sample[side + "_rules"]:
                    counter_cases[side, rule["key"]].append(sample)
            group = groups.setdefault(sample["group"], ([], collections.defaultdict(list)))
            group[0].append(sample)
            if sample["build"]:
                group[1][sample["build"]].append(sample)

        def observed_matchup(cases):
            latest = cases[-1]
            a, b = latest["own_heroes"], latest["enemy_heroes"]
            return {**summarize(cases), "own_side": latest["side"], "own_heroes": a, "enemy_heroes": b,
                    "own_player": latest["player_name"], "enemy_player": latest["opponent_name"],
                    "own_main": latest["own_main"], "enemy_main": latest["enemy_main"],
                    "latest_at": latest["event_at"], "latest_battle_id": latest["battle_id"], "campaign": latest["campaign"],
                    "latest_has_full_report": bool(latest["has_full_report"]),
                    "own_rules": latest["own_rules"], "enemy_rules": latest["enemy_rules"]}

        def order(entry):
            return -entry["samples"], -entry["latest_at"]

        items = []
        for cases, builds in groups.values():
            configurations = sorted((observed_matchup(samples) for samples in builds.values()), key=order)
            items.append({**observed_matchup(cases), "configurations": configurations})
        def comparison(side, rule, cases):
            hero_field = "own_heroes" if side == "own" else "enemy_heroes"

            def formation(sample):
                return sample["side"], tuple(h["id"] for h in sample[hero_field])

            matched = {formation(sample) for sample in cases}
            included = {sample["battle_id"] for sample in cases}
            controls = [sample for sample in samples if sample["battle_id"] not in included
                        and formation(sample) in matched and any(
                            skill["id"] == COUNTER_SKILLS[rule]
                            for hero in sample[hero_field] for skill in hero.get("skills") or ())]
            return summarize(controls)

        counters = [{"holder": side, "rule": rule, **summarize(cases), "comparison": comparison(side, rule, cases)}
                    for (side, rule), cases in sorted(counter_cases.items())]
        return {"items": sorted(items, key=order), "coverage": coverage, "counter_samples": counters,
                "method": "main_troop_and_build_v3",
                "policy": "主力需三将均至少40级，且每将三个战法均有记录。"
                          "按本盟攻守方向、双方武将站位、弓步骑和是否达到主力门槛分组。"
                          "同一玩家对在同一地块、同一阵容兵种战法10分钟内交战只计首场。平局计入分母。"
                          "完整配置分组另区分原始兵种编码、等级、战法、红度、宝物和锻造。"
                          "克制统计为战法规则匹配的实战样本，同场可命中多条规则。"}

    def combat_analysis(self, filters):
        from defense import summarize

        samples, coverage = self.main_samples(filters)
        dimension, subject, perspective = (filters[k] for k in ("analysis_dimension", "analysis_subject", "analysis_perspective"))
        groups, baseline, labels = collections.defaultdict(list), collections.defaultdict(list), {}

        def entities(team):
            items = team if dimension == "hero" else [s for h in team for s in h.get("skills") or ()]
            labels.update((item["id"], item["name"]) for item in items)
            return {item["id"] for item in items}

        for sample in samples:
            own, enemy = sample["own_heroes"], sample["enemy_heroes"]
            holder, opponent = (own, enemy) if perspective == "own" else (enemy, own)
            if dimension == "skill" and not all(h.get("skills") for h in holder):
                continue
            holder_ids = entities(holder)
            direction = sample["side"] if perspective == "own" else sample["opponent_side"]
            if perspective == "enemy":
                sample["outcome"] = OPPOSITE[sample["outcome"]]
            if subject:
                if subject not in holder_ids:
                    continue
                if dimension == "skill" and not all(h.get("skills") for h in opponent):
                    continue
                baseline[direction].append(sample)
                for ident in entities(opponent):
                    groups[ident, direction].append(sample)
            else:
                baseline[direction].append(sample)
                for ident in holder_ids:
                    groups[ident, direction].append(sample)

        items = []
        for (ident, direction), cases in groups.items():
            included = {sample["battle_id"] for sample in cases}
            other = [sample for sample in baseline[direction] if sample["battle_id"] not in included]
            items.append({**summarize(cases), "id":ident, "name":labels[ident], "direction":direction,
                          "skill_type":SKILLS.get(str(ident), {}).get("type") if dimension == "skill" else None,
                          "baseline":summarize(other), "latest_battle_id":cases[-1]["battle_id"]})
        return {"items":sorted(items, key=lambda entry:(-entry["samples"], entry["name"], entry["direction"])),
                "coverage":coverage, "subject_name":labels.get(subject),
                "policy":"按所选一方视角统计胜率，攻守分开，平局计入分母。对手统计只比较同武将或战法、同攻守方向的其他已观测对阵。"}

    def summary(self, filters):
        encounters, operations = self.encounters(filters), self.operations(filters)
        observed = {r["army_id"] for r in operations if r["army_id"]}
        linked = {r["army_id"] for r in encounters if r["army_id"]}
        inferred = {r["inferred_army_id"] for r in encounters if r["inferred_army_id"]}
        names = {(r["union_id"], r["player_name"]) for r in encounters + operations if r["player_name"]}
        if not filters.get("official_campaign_id") and not any(filters[k] for k in ("region", "from_at", "to_at", "army_id")):
            unions = self.unions(filters)
            names.update((r["union_id"], r["name"]) for r in self.db.execute("SELECT name,union_id FROM players WHERE profile_id=?", (self.profile_id,))
                if (unions is None or r["union_id"] in unions) and (not filters["search"] or filters["search"] in r["name"]))
        return {"observed_names": len(names),
            "observed_armies": len(observed), "linked_armies": len(linked),
            "observed_linked_armies": len(observed & linked),
            "observed_inferred_only_armies": len(observed & (inferred - linked)),
            "battle_reports": len({r["battle_id"] for r in encounters}), "marches": len(operations),
            "marches_with_report_match": sum(bool(r["battles"]) for r in operations),
            "full_reports": len({r["battle_id"] for r in encounters if r["has_full_report"]}),
            "all_members_verified": False, "all_armies_verified": False,
            "scope": "当前角色实际收到的敌我双方资料；查询范围不限制采集归档"}

    def personnel(self, filters):
        groups = {}
        def person(item):
            key = (item["union_id"], item["player_name"])
            return groups.setdefault(key, {"union_id": key[0], "player_name": key[1], "identity_keys": set(),
                "army_ids": set(), "battle_ids": set(), "march_count": 0, "lineups": {}, "latest_at": 0})
        for item in self.encounters(filters):
            p = person(item)
            p["identity_keys"].add(item["player_key"])
            p["battle_ids"].add(item["battle_id"])
            p["latest_at"] = max(p["latest_at"], item["event_at"])
            if item["heroes"]:
                p["lineups"].setdefault(item["team_key"], {k: item[k] for k in
                    ("team_key", "army_id", "inferred_army_id", "heroes", "binding", "event_at", "battle_id", "npc", "side")})
            if item["army_id"]:
                p["army_ids"].add(item["army_id"])
        for item in self.operations(filters):
            if not item["player_name"]:
                continue
            p = person(item)
            p["identity_keys"].add("world:" + str(item["owner_uid"]))
            p["army_ids"].add(item["army_id"])
            p["march_count"] += 1
            p["latest_at"] = max(p["latest_at"], item["begin_at"])
        if not any(filters[k] for k in ("region", "from_at", "to_at", "army_id")):
            unions = self.unions(filters)
            for row in self.db.execute("SELECT * FROM players WHERE profile_id=?", (self.profile_id,)):
                if unions is not None and row["union_id"] not in unions or filters["search"] and filters["search"] not in row["name"]:
                    continue
                p = person({"union_id": row["union_id"], "player_name": row["name"]})
                p["identity_keys"].add(row["player_key"])
                p["latest_at"] = max(p["latest_at"], row["event_at"])
        for p in groups.values():
            for key in ("identity_keys", "army_ids", "battle_ids"):
                p[key] = sorted(p[key])
            p["lineups"] = list(p["lineups"].values())
            p["identity_policy"] = "按同盟与名字汇总查询；各来源身份编号保留，未强制合并为同一身份"
        return sorted(groups.values(), key=lambda x: (-x["latest_at"], x["player_name"]))

    def report(self, bid):
        with self.archive.lock:
            row = self.db.execute("SELECT * FROM battle_reports WHERE profile_id=? AND battle_id=?", (self.profile_id, str(bid))).fetchone()
            if not row:
                raise ValueError("此战报尚未在当前档案中观测到")
            body = self.db.execute("SELECT body,observed_at,message_ref FROM report_bodies WHERE profile_id=? AND battle_id=?",
                (self.profile_id, str(bid))).fetchone()
            brief = self.db.execute("SELECT body,observed_at,message_ref FROM report_briefs WHERE profile_id=? AND battle_id=?",
                (self.profile_id, str(bid))).fetchone()
            sides = [dict(r) for r in self.db.execute("SELECT * FROM combat_sides WHERE profile_id=? AND battle_id=?",
                (self.profile_id, str(bid)))]
            for item in sides:
                item["features"] = json.loads(item["features"])
                item["heroes"] = side_heroes(json.loads(item["heroes"]), item["side"], item["features"])
                self.add_march_evidence(item)
                for route in item["march_links"]:
                    route["sources"] = self.archive.record_sources("march", route["march_id"], route["message_ref"], route["observed_at"])
            body_data = dict(body) if body else None
            if body_data:
                body_data["sources"] = self.archive.record_sources("body", str(bid), body["message_ref"], body["observed_at"])
            brief_data = dict(brief) if brief else None
            if brief_data:
                brief_data["sources"] = self.archive.record_sources("brief", str(bid), brief["message_ref"], brief["observed_at"])
            data = json.loads(row["data"])
            campaign = report_campaign(self.official_campaigns(), data.get("block_id"), row["event_at"],
                {str(data.get(k)) for k in ("attack_unionid", "defend_unionid")} - {"0", "None", ""})
            return {"profile_id": self.profile_id, "profile": data, "campaign": campaign,
                "battle_id": str(bid), "sides": sides, "full_report": body_data, "brief_report": brief_data,
                "lineup_review": review_report(self.db, self.profile_id, str(bid)),
                "sources": self.archive.record_sources("report", str(bid), row["message_ref"], row["observed_at"])}

    def query(self, kind, **query):
        with self.archive.lock:
            if kind == "recent-battles":
                return self.recent_battles(query.get("army_id", ""), query.get("coord", ""))
            if kind == "official-campaigns":
                events = self.official_campaigns().values()
                return {"items": sorted(events, key=lambda e: (not e["name_verified"], bool(e["filters"]["to_at"]), -e["observed_at"]))}
            filters = self.filters(query)
            if kind == "summary":
                return {"profile_id": self.profile_id, "filters": filters, **self.summary(filters)}
            if kind == "matchups":
                result = self.matchup_stats(filters)
            elif kind in ("main-matchups", "combat-analysis"):
                filters["main_scope"] = "either" if query.get("main_scope") == "either" else "both"
                if kind == "combat-analysis":
                    filters["analysis_dimension"] = "hero" if query.get("analysis_dimension") == "hero" else "skill"
                    filters["analysis_perspective"] = "enemy" if query.get("analysis_perspective") == "enemy" else "own"
                    filters["analysis_subject"] = max(0, number(query.get("analysis_subject")))
                    result = self.combat_analysis(filters)
                else:
                    result = self.main_matchup_stats(filters)
            elif kind == "operations":
                result = {"items": self.operations(filters)}
            elif kind == "personnel":
                result = {"items": self.personnel(filters)}
            elif kind == "encounters":
                result = {"items": self.encounters(filters)}
            elif kind == "campaigns":
                return {"items": [{**dict(r), "filters": json.loads(r["filters"])} for r in self.db.execute(
                    "SELECT * FROM campaign_views WHERE profile_id=? ORDER BY created_at DESC", (self.profile_id,))]}
            else:
                raise ValueError("未知情报查询")
            limit = max(1, min(number(query.get("limit"), 100), 500))
            offset = max(0, number(query.get("offset")))
            result["total"] = len(result["items"])
            result["items"] = result["items"][offset:offset+limit]
            if kind in ("operations", "encounters"):
                for item in result["items"]:
                    record_kind, record_id = ("march", item["id"]) if kind == "operations" else ("report", item["battle_id"])
                    item["sources"] = self.archive.record_sources(record_kind, record_id, item["message_ref"], item["observed_at"])
                    item["last_observed_at"] = max((s["observed_at"] for s in item["sources"]), default=item["observed_at"])
            return {"profile_id": self.profile_id, "filters": filters, "limit": limit, "offset": offset, **result}

    def recent_battles(self, army_id, coord, now=None):
        now = time.time() if now is None else now
        if bool(army_id) == bool(coord):
            raise ValueError("请选择一支部队或一个地块")
        if army_id:
            if not re.fullmatch(r"[1-9][0-9]{0,19}", army_id):
                raise ValueError("队号无效")
            owner = self.db.execute('''SELECT p.name,p.union_id FROM army_current a
                JOIN army_states s ON s.id=a.state_ref JOIN players p ON p.profile_id=a.profile_id
                AND p.player_key='world:'||s.owner_uid WHERE a.profile_id=? AND a.army_id=?''',
                (self.profile_id, army_id)).fetchone()
            params = {"profile": self.profile_id, "aid": army_id, "start": now - 3600, "end": now,
                      "name": owner["name"] if owner else None, "union": owner["union_id"] if owner else None}
            matches = self.db.execute('''SELECT battle_id,side,event_at,association FROM (
                    SELECT c.battle_id,c.side,c.event_at,c.player_name,c.union_id,'direct' AS association
                    FROM combat_sides c WHERE c.profile_id=:profile AND c.army_id=:aid
                    AND c.event_at BETWEEN :start AND :end
                    UNION ALL
                    SELECT c.battle_id,c.side,c.event_at,c.player_name,c.union_id,'arrival' AS association
                    FROM march_samples m CROSS JOIN battle_march_links l
                    ON l.profile_id=m.profile_id AND l.march_id=m.id AND l.method='unique_attack_arrival'
                    CROSS JOIN combat_sides c ON c.profile_id=l.profile_id AND c.battle_id=l.battle_id AND c.side=l.side
                    WHERE m.profile_id=:profile AND m.army_id=:aid AND m.end_at BETWEEN :start AND :end
                    AND c.army_id IS NULL AND c.event_at BETWEEN :start AND :end)
                WHERE :name IS NULL OR (player_name=:name AND union_id=:union)
                ORDER BY event_at DESC,CAST(battle_id AS INTEGER) DESC,association DESC LIMIT 1''', params).fetchall()
        else:
            if not re.fullmatch(r"[1-9][0-9]{0,3},[1-9][0-9]{0,3}", coord):
                raise ValueError("地块坐标无效")
            x, y = map(int, coord.split(","))
            matches = self.db.execute('''SELECT battle_id,event_at,NULL AS side,NULL AS association
                FROM battle_reports WHERE profile_id=? AND wid=? AND event_at BETWEEN ? AND ?
                ORDER BY event_at DESC,CAST(battle_id AS INTEGER) DESC LIMIT 6''',
                (self.profile_id, x * 10000 + y, now - 3600, now)).fetchall()
        items = []
        campaigns = self.official_campaigns()
        for match in matches:
            bid = match["battle_id"]
            report = self.db.execute('''SELECT wid,place_name,data FROM battle_reports
                WHERE profile_id=? AND battle_id=?''', (self.profile_id, bid)).fetchone()
            point = divmod(report["wid"] or 0, 10000)
            sides = []
            for side in self.db.execute('''SELECT c.side,c.player_name,c.outcome,c.heroes,c.features,
                    COALESCE(c.army_id,(SELECT m.army_id FROM battle_march_links l JOIN march_samples m ON m.id=l.march_id
                    WHERE l.profile_id=c.profile_id AND l.battle_id=c.battle_id AND l.side=c.side
                    AND l.method='unique_attack_arrival' LIMIT 1)) AS army_id FROM combat_sides c
                WHERE c.profile_id=? AND c.battle_id=? ORDER BY c.side''', (self.profile_id, bid)):
                heroes = side_heroes(json.loads(side["heroes"]), side["side"], json.loads(side["features"]))
                sides.append({k: side[k] for k in ("side", "player_name", "army_id", "outcome")} |
                    {"initial_hp": troop_total(heroes, "initial_hp"), "remaining_hp": troop_total(heroes, "remaining_hp"),
                     "wounded_hp": troop_total(heroes, "wounded_hp"), "heroes": heroes})
            data = json.loads(report["data"])
            campaign = report_campaign(campaigns, data.get("block_id"), match["event_at"],
                {str(data.get(k)) for k in ("attack_unionid", "defend_unionid")} - {"0", "None", ""})
            items.append({**dict(match), "coord": list(point), "campaign": campaign,
                          "place_name": report["place_name"], "sides": sides})
        return {"items": items}

    def save_campaign(self, payload):
        name = str(payload.get("name", "")).strip()
        if not name or len(name) > 80:
            raise ValueError("请填写80字以内的战役视角名称")
        filters = self.filters(payload)
        ident = digest([self.profile_id, name, filters])[:24]
        with self.archive.lock, self.db:
            self.db.execute("INSERT OR IGNORE INTO campaign_views VALUES(?,?,?,?,?)",
                (ident, self.profile_id, name, packed(filters), time.time()))
        return {"id": ident, "name": name, "filters": filters}

    def export(self, payload):
        kind = payload.get("kind")
        if kind not in ("personnel", "operations", "encounters", "matchups", "main-matchups", "combat-analysis", "report"):
            raise ValueError("未知导出类别")
        with self.archive.lock:
            if kind == "report":
                result = self.report(payload.get("battle_id", ""))
            else:
                query = {k:v for k,v in payload.items() if k not in ("kind", "limit", "offset")}
                result = self.query(kind, **query, limit=500)
                while len(result["items"]) < result["total"]:
                    page = self.query(kind, **query, limit=500, offset=len(result["items"]))
                    if not page["items"]:
                        break
                    result["items"].extend(page["items"])
            result.update(exported_at=time.time(), profile=self.archive.profile, kind=kind)
            path = self.archive.root.parent / "Exports" / ("stzb-" + kind + "-" + str(time.time_ns()) + ".json")
            write_json(path, result)
            return {"path": str(path), "records": len(result.get("items", [result]))}
