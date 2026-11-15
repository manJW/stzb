"""Export and merge an account-independent snapshot of a verified game season."""
import json
import sqlite3
import time
import zlib
from pathlib import Path
from uuid import uuid4

from lineup_prediction import forecast_key
from observation_codec import digest, packed
from official_campaigns import campaigns_from_messages


FILENAME = "season-seed.sqlite3"
SOURCE_ID = "season-seed"
CAMPAIGN_SCHEMA = """CREATE TABLE IF NOT EXISTS archived_official_campaigns(
    profile_id TEXT NOT NULL, id TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY(profile_id,id))"""

# Export by allowlist. Messages, login/profile records, private client tables,
# capture configuration, report-read jobs and member credentials stay local.
TABLES = (
    "players", "player_traits", "unions", "roster_observations", "army_states", "army_current",
    "march_samples", "tiles", "battle_reports", "report_bodies", "report_briefs",
    "army_report_refs", "combat_sides", "battle_march_links", "team_versions",
    "team_current", "observations", "lineup_forecasts", "same_army_audits",
    "archived_official_campaigns", "source_sightings",
)

# Match the archive's IDs for future live observations of the same records.
ID_FIELDS = {
    "army_states": "army_id,data", "march_samples": "army_id,state_code,from_x,from_y,to_x,to_y,begin_at,end_at",
    "tiles": "wid,data", "army_report_refs": "army_id,battle_id,owner_uid,owner_name,union_id,raw_reference",
    "team_versions": "team_key,battle_id,configuration_hash",
}
REFERENCES = {
    "army_current": {"state_ref": "army_states"}, "army_report_refs": {"state_ref": "army_states"},
    "team_current": {"version_ref": "team_versions"}, "battle_march_links": {"march_id": "march_samples"},
}
LATEST = {
    "players": ("event_at", "observed_at"), "player_traits": ("observed_at",), "unions": ("observed_at",),
    "roster_observations": ("observed_at",), "army_current": ("observed_at",),
    "report_bodies": ("observed_at",), "report_briefs": ("observed_at",),
    "team_current": ("event_at", "observed_at"), "source_sightings": ("observed_at",),
}


def export_seed(root, output):
    from installed_mode import logged_in_role, role_detail_scope

    profile = json.loads((root / "archive-profile.json").read_text(encoding="utf-8-sig"))
    profile_id = digest([profile[k] for k in ("server", "season", "account_role_id")])[:24]
    role = str(profile["account_role_id"])
    config = json.loads((root / "config.json").read_text(encoding="utf-8-sig"))
    own = config["self"]
    source = (root / "data" / "intelligence.sqlite3").resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(output.resolve().as_uri(), uri=True) as db:
        db.row_factory = sqlite3.Row
        db.execute("ATTACH DATABASE ? AS original", (source,))
        db.execute("BEGIN")
        rows = db.execute("SELECT message_id,payload FROM original.messages WHERE profile_id=? AND message_id IN (99991,3686)", (profile_id,))
        logins, details = set(), set()
        for mid, payload in rows:
            data = json.loads(zlib.decompress(payload))
            if mid == 99991:
                identity = logged_in_role(data)
                if identity and identity["account_role_id"] == role and identity.get("scope"):
                    scope = identity["scope"]
                    logins.add((scope["server"], scope["season"]))
            else:
                detail = role_detail_scope(data)
                if detail and detail[:2] == (str(own["user_id"]), role) and detail[2].get("server_label") == profile["server"]:
                    details.add((detail[2]["server"], detail[2]["season"]))
        if len(logins) != 1 or logins != details:
            raise ValueError("历史档案的区服与赛季缺少一致的核对记录")
        server, season = logins.pop()
        union_id = str(profile["union_id"])
        relations = {key: {"relation": value["relation"], "verified_at": value["verified_at"]}
                     for key, value in config["relations"].items()
                     if key.isascii() and key.isdigit() and int(key)>0
                     and value.get("relation") in ("enemy", "friendly") and value.get("verified_at")}
        manifest = {"version": 1, "id": uuid4().hex, "server": server, "season": season,
                    "server_label": profile["server"], "union_id": union_id,
                    "relations": relations if str(own["union_id"]) == union_id else {}, "exported_at": time.time()}
        db.execute("CREATE TABLE manifest(data TEXT NOT NULL)")
        db.execute("INSERT INTO manifest VALUES(?)", (packed(manifest),))
        counts = {}
        for table in TABLES:
            if table == 'player_traits' and not db.execute('PRAGMA original.table_info(player_traits)').fetchone():
                continue
            if table == "archived_official_campaigns":
                db.execute("CREATE TABLE archived_official_campaigns(id TEXT, data TEXT)")
                rows = db.execute("""SELECT message_id,payload,first_seen FROM original.messages
                    WHERE profile_id=? AND message_id IN (3807,3811) ORDER BY first_seen""", (profile_id,))
                events = campaigns_from_messages(rows, profile)
                db.executemany("INSERT INTO archived_official_campaigns VALUES(?,?)",
                               ((key, packed(value)) for key, value in events.items()))
            elif table == "source_sightings":
                db.execute("""CREATE TABLE source_sightings AS SELECT kind,record_id,MAX(observed_at) AS observed_at
                    FROM original.source_sightings WHERE profile_id=? GROUP BY kind,record_id""", (profile_id,))
            else:
                cols = [row[1] for row in db.execute(f"PRAGMA original.table_info({table})") if row[1] != "profile_id"]
                db.execute(f"CREATE TABLE {table} AS SELECT {','.join(cols)} FROM original.{table} WHERE profile_id=?", (profile_id,))
            counts[table] = db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    db.close()
    return {"server": server, "season": season, "counts": counts}


def _record_id(profile_id, table, *row):
    if table == "army_states":
        army_id, data = row
        raw = json.loads(data)
        return digest([profile_id, army_id, raw] if isinstance(raw, list)
                      else [profile_id, "legacy_projection", army_id, raw["facts"]])
    if table == "march_samples":
        army_id, code, x0, y0, x1, y1, start, end = row
        return digest([profile_id, army_id, code, [x0 * 10000 + y0, x1 * 10000 + y1, int(start), int(end)]])
    if table == "tiles":
        return digest([profile_id, str(row[0]), json.loads(row[1])])
    if table == "army_report_refs":
        return digest([profile_id, *row[:-1], json.loads(row[-1])])
    return digest([profile_id, *row])


def _merge_table(db, table, profile_id):
    # Seeds produced before wanderer monitoring have no supplementary traits.
    if table == 'player_traits' and not db.execute("PRAGMA seed.table_info(player_traits)").fetchone():
        return
    schema = db.execute(f"PRAGMA main.table_info({table})").fetchall()
    columns = [row[1] for row in schema]
    keys = [row[1] for row in sorted(schema, key=lambda row: row[5]) if row[5]]
    expressions = {column: f"s.{column}" for column in columns}
    expressions["profile_id"] = ":profile"
    if table in ID_FIELDS:
        expressions["id"] = "(SELECT new_id FROM seed_ids WHERE old_id=s.id)"
    for column in REFERENCES.get(table, {}):
        expressions[column] = f"COALESCE((SELECT new_id FROM seed_ids WHERE old_id=s.{column}),s.{column})"
    if table == "observations":
        expressions["id"] = "seed_digest(:profile,s.id)"
        expressions["data"] = """CASE WHEN s.kind='team_configuration_changed' THEN json_replace(s.data,
            '$.previous_version',COALESCE((SELECT new_id FROM seed_ids WHERE old_id=json_extract(s.data,'$.previous_version')),json_extract(s.data,'$.previous_version')),
            '$.new_version',COALESCE((SELECT new_id FROM seed_ids WHERE old_id=json_extract(s.data,'$.new_version')),json_extract(s.data,'$.new_version'))) ELSE s.data END"""
    if table in ("combat_sides", "team_versions"):
        field = "features" if table == "combat_sides" else "data"
        expressions[field] = f"""CASE WHEN json_type(s.{field},'$.binding_evidence.state_ref')='text'
            THEN json_replace(s.{field},'$.binding_evidence.state_ref',COALESCE((SELECT new_id FROM seed_ids
            WHERE old_id=json_extract(s.{field},'$.binding_evidence.state_ref')),json_extract(s.{field},'$.binding_evidence.state_ref')))
            ELSE s.{field} END"""
    if table == "lineup_forecasts":
        expressions["march_key"] = "seed_forecast_key(:profile,army_id,from_x,from_y,to_x,to_y,begin_at,end_at,state_code)"
    if table == "source_sightings":
        expressions["source_id"] = ":source_id"
        expressions["record_id"] = "COALESCE((SELECT new_id FROM seed_ids WHERE old_id=s.record_id),s.record_id)"
    conflict = "ON CONFLICT DO NOTHING"
    if table in LATEST:
        updated = [f"{c}=excluded.{c}" for c in columns if c not in keys]
        current = ",".join(f"COALESCE({table}.{c},0)" for c in LATEST[table])
        incoming = ",".join(f"COALESCE(excluded.{c},0)" for c in LATEST[table])
        if len(LATEST[table]) > 1:
            current, incoming = f"({current})", f"({incoming})"
        conflict = f"ON CONFLICT({','.join(keys)}) DO UPDATE SET {','.join(updated)} WHERE {incoming}>{current}"
    elif table == "battle_reports":
        conflict = """ON CONFLICT(profile_id,battle_id) DO UPDATE SET
            data=json_patch(excluded.data,battle_reports.data),
            observed_at=MAX(battle_reports.observed_at,excluded.observed_at)"""
    elif table == "combat_sides":
        conflict = """ON CONFLICT(profile_id,battle_id,side) DO UPDATE SET
            features=json_patch(excluded.features,combat_sides.features),
            army_id=COALESCE(combat_sides.army_id,excluded.army_id),
            binding=CASE WHEN combat_sides.army_id IS NULL THEN excluded.binding ELSE combat_sides.binding END"""
    elif table == "archived_official_campaigns":
        conflict = """ON CONFLICT(profile_id,id) DO UPDATE SET data=excluded.data
            WHERE json_extract(excluded.data,'$.observed_at')>json_extract(archived_official_campaigns.data,'$.observed_at')"""
    db.execute(f"""INSERT INTO {table}({','.join(columns)})
        SELECT {','.join(expressions.values())} FROM seed.{table} s WHERE 1 {conflict}""",
        {"profile": profile_id, "source_id": SOURCE_ID})


def merge_seed(archive, path):
    if not path or not path.is_file():
        return None
    with archive.lock:
        db = archive.db
        db.execute("ATTACH DATABASE ? AS seed", (path.resolve().as_uri() + "?mode=ro&immutable=1",))
        try:
            manifest = json.loads(db.execute("SELECT data FROM seed.manifest").fetchone()[0])
            if manifest["version"] != 1 or any(archive.profile[key] != manifest[key] for key in ("server", "season")):
                return None
            profile_id = archive.profile_id
            db.execute("""CREATE TABLE IF NOT EXISTS imported_season_seeds(
                profile_id TEXT NOT NULL, seed_id TEXT NOT NULL, PRIMARY KEY(profile_id,seed_id))""")
            if db.execute("SELECT 1 FROM imported_season_seeds WHERE profile_id=? AND seed_id=?",
                          (profile_id, manifest["id"])).fetchone():
                return manifest
            db.create_function("seed_record_id", -1, _record_id)
            db.create_function("seed_digest", 2, lambda profile, ident: digest([profile, SOURCE_ID, ident]))
            db.create_function("seed_forecast_key", -1, lambda profile, *row: forecast_key(profile, row))
            if archive.retention_from:
                # A seed predating this local reset is deliberately not imported.
                # Fresh live collection will populate retained records for this profile.
                return manifest
            with db:
                db.execute("BEGIN")
                db.execute("CREATE TEMP TABLE seed_ids(old_id TEXT PRIMARY KEY,new_id TEXT NOT NULL)")
                for table, fields in ID_FIELDS.items():
                    db.execute(f"INSERT INTO seed_ids SELECT id,seed_record_id(?,?,{fields}) FROM seed.{table}",
                               (profile_id, table))
                for table in TABLES:
                    _merge_table(db, table, profile_id)
                db.execute("INSERT INTO imported_season_seeds VALUES(?,?)", (profile_id, manifest["id"]))
                db.execute("DROP TABLE seed_ids")
            with db:
                db.execute('DELETE FROM lineup_rule_migrations WHERE profile_id=? AND rule=?',
                           (profile_id,'system-npc-teams-1'))
            archive.remove_system_npc_teams()
            if archive.report_only:
                import reported_archive
                with db:
                    reported_archive.seed_admissions(db,profile_id)
                    reported_archive.prune_history(db,profile_id)
            return manifest
        finally:
            db.execute("DETACH DATABASE seed")
