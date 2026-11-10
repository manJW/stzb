"""Loopback upload-only listener and local tools for enrolling pilot members."""
import argparse
import hashlib
import hmac
import json
import re
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from archive import Archive, number
from share_protocol import MAX_BYTES, UPLOAD_PATH, VERSION, endpoint, in_alliance, numeric_id, scope_of, validate_events

ROOT = Path(__file__).resolve().parent


def key_hash(key):
    return hashlib.sha256(key.encode("ascii")).hexdigest()


def identity_of(row):
    return {key: row[key] for key in ("name", "world_uid", "role_id")}


def enroll(archive, name, base_url):
    endpoint(base_url)
    union = str(archive.profile["union_id"])
    with archive.lock, archive.db:
        rows = archive.db.execute("""SELECT name,world_uid,role_id FROM players WHERE profile_id=?
            AND name=? AND union_id=? AND world_uid IS NOT NULL AND role_id IS NOT NULL""",
            (archive.profile_id, name, union)).fetchall()
        if len(rows) != 1 or not all(numeric_id(rows[0][field]) for field in ("world_uid", "role_id")):
            raise ValueError("本盟档案里缺少唯一角色编号。请在地图上查看该盟友后重试。")
        identity = identity_of(rows[0])
        if identity["role_id"] == str(archive.profile["account_role_id"]):
            raise ValueError("本机角色已经在归档，请选择另一位本盟成员")
        registered = archive.db.execute("SELECT id,enabled FROM pilot_members WHERE profile_id=? AND role_id=?",
            (archive.profile_id, identity["role_id"])).fetchone()
        count = archive.db.execute("SELECT COUNT(*) FROM pilot_members WHERE profile_id=? AND enabled=1",
            (archive.profile_id,)).fetchone()[0]
        if count >= 2 and not (registered and registered["enabled"]):
            raise ValueError("试跑最多同时启用两位盟友")
        member_id = registered["id"] if registered else secrets.token_hex(8)
        secret = secrets.token_urlsafe(32)
        archive.db.execute('''INSERT INTO pilot_members VALUES(?,?,?,?,?,?,1,NULL,NULL)
            ON CONFLICT(profile_id,id) DO UPDATE SET name=excluded.name,world_uid=excluded.world_uid,
            key_hash=excluded.key_hash,enabled=1,last_received_at=NULL,last_observed_at=NULL''',
            (archive.profile_id, member_id, identity["name"], identity["world_uid"], identity["role_id"], key_hash(secret)))
    return {"version": VERSION, "scope": scope_of(archive.profile), "identity": identity,
        "url": base_url, "key": member_id + "." + secret}


def sources(archive):
    with archive.lock:
        return [dict(row) for row in archive.db.execute("""SELECT id,name,enabled,last_observed_at,last_received_at
            FROM pilot_members WHERE profile_id=? ORDER BY name""", (archive.profile_id,))]


def authenticated(archive, authorization):
    match = re.fullmatch(r"Bearer ([a-f0-9]{16})\.([A-Za-z0-9_-]{43})", authorization or "")
    if not match:
        return None
    with archive.lock:
        row = archive.db.execute("SELECT * FROM pilot_members WHERE profile_id=? AND id=? AND enabled=1",
            (archive.profile_id, match[1])).fetchone()
    return row if row and hmac.compare_digest(row["key_hash"], key_hash(match[2])) else None


def accept(archive, member, payload, now):
    scope = scope_of(archive.profile)
    if (not isinstance(payload, dict) or payload.get("version") != VERSION or payload.get("scope") != scope
            or payload.get("identity") != identity_of(member)):
        raise ValueError("上传角色或赛季与邀请不一致")
    events = payload.get("events")
    validate_events(events, scope["union_id"], now)
    with archive.lock:
        # Recheck after reading the body in case the invitation was rotated or revoked.
        active = archive.db.execute("SELECT key_hash FROM pilot_members WHERE profile_id=? AND id=? AND enabled=1",
            (archive.profile_id, member["id"])).fetchone()
        if not active or active["key_hash"] != member["key_hash"]:
            raise PermissionError("邀请已停用")
        batch_reports = {}
        for mid, _, value in events:
            if mid in (10, 92):
                bid = str(value["battle_id"])
                location = (number(value["wid"]), number(value["time"]))
                known = archive.db.execute("SELECT wid,event_at FROM battle_reports WHERE profile_id=? AND battle_id=?",
                    (archive.profile_id, bid)).fetchone()
                if (known and all(known) and tuple(known) != location) or batch_reports.get(bid, location) != location:
                    raise ValueError("战报编号与已有时间或坐标冲突")
                batch_reports[bid] = location
            if mid == 6231 and str(value["battle_id"]) not in batch_reports:
                summary = archive.db.execute("SELECT data FROM battle_reports WHERE profile_id=? AND battle_id=?",
                    (archive.profile_id, str(value["battle_id"]))).fetchone()
                if not summary or not in_alliance(json.loads(summary[0]), scope["union_id"]):
                    raise ValueError("全文须对应已收到的本盟战报摘要")
        for mid, at, value in events:
            archive.ingest(mid, value, at, source="member:" + member["id"])
        with archive.db:
            archive.db.execute('''UPDATE pilot_members SET last_received_at=?,
                last_observed_at=MAX(COALESCE(last_observed_at,0),?) WHERE profile_id=? AND id=?''',
                (now, max(event[1] for event in events), archive.profile_id, member["id"]))
    return {"accepted": len(events)}


def receiver(archive, port=8788):
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(12)

        def log_message(self, *_):
            pass

        def reply(self, status, body):
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            if self.path != UPLOAD_PATH:
                self.send_error(404)
                return
            member = authenticated(archive, self.headers.get("Authorization"))
            if not member:
                self.reply(401, {"error": "邀请无效或已停用"})
                return
            length = number(self.headers.get("Content-Length"), -1)
            if not 1 <= length <= MAX_BYTES or self.headers.get_content_type() != "application/json":
                self.reply(413, {"error": "上传批次过大或格式错误"})
                return
            try:
                payload = json.loads(self.rfile.read(length))
                result = accept(archive, member, payload, time.time())
            except PermissionError as error:
                self.reply(401, {"error": str(error)})
            except (ValueError, TypeError, OverflowError, RecursionError) as error:
                self.reply(400, {"error": str(error)[:150]})
            else:
                self.reply(200, result)

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def start_receiver(archive, port):
    service = receiver(archive, port)
    thread = threading.Thread(target=service.serve_forever, name="member-receiver", daemon=True)
    thread.start()
    return service, thread


def main():
    parser = argparse.ArgumentParser(description="同盟视野试跑 · 本机邀请管理")
    actions = parser.add_subparsers(dest="action", required=True)
    invite = actions.add_parser("invite")
    invite.add_argument("name")
    invite.add_argument("url")
    revoke = actions.add_parser("revoke")
    revoke.add_argument("name")
    actions.add_parser("status")
    args = parser.parse_args()
    archive = Archive(ROOT)
    try:
        if args.action == "invite":
            value = enroll(archive, args.name, args.url)
            folder = ROOT.parent / "Exports"
            folder.mkdir(exist_ok=True)
            path = folder / ("成员邀请-" + value["key"].split(".")[0] + ".json")
            path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
            print(path)
        elif args.action == "revoke":
            with archive.lock, archive.db:
                changed = archive.db.execute("UPDATE pilot_members SET enabled=0 WHERE profile_id=? AND name=? AND enabled=1",
                    (archive.profile_id, args.name)).rowcount
            print(f"已停用 {changed} 个邀请")
        else:
            print(json.dumps(sources(archive), ensure_ascii=False, indent=2))
    except ValueError as error:
        parser.exit(1, str(error) + "\n")
    finally:
        archive.close()


if __name__ == "__main__":
    main()
