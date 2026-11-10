"""Opt-in Windows pilot. Observe only incoming PC game messages and upload business records."""
import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from observation_codec import dictionaries, digest, number, packed
from collector import Collector
from share_protocol import MAP_IDS, MAX_AGE, MAX_BYTES, MAX_EVENTS, REPORT_IDS, VERSION, endpoint, project_body, project_player, project_report, project_scene

ROOT = Path(__file__).resolve().parent


class Outbox:
    def __init__(self, path):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("CREATE TABLE IF NOT EXISTS pending (id INTEGER PRIMARY KEY AUTOINCREMENT, seen REAL, payload TEXT)")
        self.lock = threading.Lock()

    def add(self, events):
        with self.lock, self.db:
            self.db.executemany("INSERT INTO pending(seen,payload) VALUES(?,?)",
                [(event[1], packed(event)) for event in events])
            removed = self.db.execute("""DELETE FROM pending WHERE seen<? OR id IN
                (SELECT id FROM pending ORDER BY id DESC LIMIT -1 OFFSET 3000)""", (time.time()-MAX_AGE+60,)).rowcount
            if removed:
                print(f"本地待发队列已清理 {removed} 条过期或超限记录。", flush=True)

    def batch(self, limit=MAX_EVENTS):
        rows, size = [], 0
        with self.lock, self.db:
            self.db.execute("DELETE FROM pending WHERE seen<?", (time.time()-MAX_AGE+60,))
            for row in self.db.execute("SELECT id,payload FROM pending ORDER BY id LIMIT ?", (limit,)):
                size += len(row[1].encode("utf-8")) + 1
                if rows and size > MAX_BYTES-2048:
                    break
                rows.append(row)
        return rows

    def acknowledge(self, rows):
        with self.lock, self.db:
            self.db.executemany("DELETE FROM pending WHERE id=?", [(row[0],) for row in rows])


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_):
        return None


class Uploader:
    def __init__(self, invitation, outbox):
        self.invitation, self.outbox = invitation, outbox
        self.url = endpoint(invitation["url"])
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def run(self, stop):
        limit, status, last_status = MAX_EVENTS, "", 0
        while not stop.is_set():
            rows = self.outbox.batch(limit)
            if not rows:
                stop.wait(1)
                continue
            payload = {key: self.invitation[key] for key in ("version", "scope", "identity")}
            payload["events"] = [json.loads(row[1]) for row in rows]
            try:
                request = Request(self.url, packed(payload).encode("utf-8"), {"Content-Type": "application/json",
                    "Authorization": "Bearer " + self.invitation["key"]}, method="POST")
                with self.opener.open(request, timeout=10) as response:
                    if json.loads(response.read(8192)).get("accepted") != len(rows):
                        raise ValueError("接收端未确认本批记录")
                self.outbox.acknowledge(rows)
                limit, message = MAX_EVENTS, "正在上报，接收端已确认入库"
            except HTTPError as error:
                if error.code in (400, 413) and len(rows) > 1:
                    limit = 1
                    continue
                if error.code in (400, 413):
                    self.outbox.acknowledge(rows)
                    message = "接收端拒收一条记录，继续处理其余记录"
                elif error.code == 401:
                    message = "邀请无效或已停用，请联系看板管理者"
                    stop.wait(10)
                else:
                    message = "接收暂不可用，本地保留待发记录"
                    stop.wait(5)
            except (URLError, ValueError, OSError):
                message = "无法连接接收端，检查 Tailscale，待发记录已保留"
                stop.wait(5)
            if status != message or time.monotonic() - last_status > 60:
                print(message, flush=True)
                status, last_status = message, time.monotonic()
            stop.wait(1)


class CaptureStatus:
    def __init__(self, root):
        self.root, self.health = root, {}
        self.status = ""
        self.on_gap = None

    def connection(self, status, message, detail=None):
        if status != self.status:
            print(message, flush=True)
            self.status = status

    def packet(self, _):
        pass

    def persist(self):
        pass

    def gap(self):
        if self.on_gap:
            self.on_gap()

    def decode_error(self):
        self.gap()


class MemberCollector(Collector):
    def __init__(self, root, executable, dumpcap, invitation, outbox):
        super().__init__(CaptureStatus(root), settings={"mode": "pc", "executable": executable, "dumpcap": dumpcap})
        self.incoming_ids = MAP_IDS | REPORT_IDS
        self.identity, self.union_id = invitation["identity"], invitation["scope"]["union_id"]
        self.outbox = outbox
        self.verified = False
        self.state.on_gap = self.invalidate
        self.owners, self.seen_armies, self.report_ids = {}, {}, set()

    def invalidate(self):
        if self.verified:
            print("地图采集有中断，等待下一帧完整地图重新核对。", flush=True)
        self.verified = False

    def capture(self, connection):
        self.verified = False
        self.owners.clear()
        self.seen_armies.clear()
        self.report_ids.clear()
        print("请用受邀角色进入主城附近的地图，等待身份核对。", flush=True)
        return super().capture(connection)

    def _message(self, mid, value, observed_at):
        if mid in MAP_IDS:
            if not isinstance(value, list) or len(value) != 34 or not isinstance(value[1], dict):
                return
            for uid, raw in value[1].items():
                player = project_player(raw)
                if player:
                    self.owners[uid] = player
                    if uid == self.identity["world_uid"]:
                        matched = player[:3] == [self.identity["name"], int(self.identity["role_id"]), int(self.union_id)]
                        if not matched:
                            self.verified = False
                        elif not self.verified and mid == 5026 and number(value[18]) > 0:
                            self.verified = True
                            print("地图角色与本盟身份已核对。可以查看战区并翻阅同盟战报。", flush=True)
            if not self.verified:
                return
            scene = project_scene(value, self.owners)
            if scene:
                for aid, raw in list(scene[6].items()):
                    signature = digest(raw)
                    old = self.seen_armies.get(aid)
                    if old and old[0] == signature and observed_at-old[1] < 15:
                        del scene[6][aid]
                    else:
                        self.seen_armies[aid] = (signature, observed_at)
                armies = list(scene[6].items())
                if armies:
                    events = []
                    for start in range(0, len(armies), 50):
                        chunk = list(scene)
                        chunk[6] = dict(armies[start:start+50])
                        events.append([mid, observed_at, project_scene(chunk)])
                    self.outbox.add(events)
            return
        if not self.verified:
            return
        events = []
        for row in dictionaries(value):
            record = project_body(row) if mid == 6231 else project_report(row, self.union_id)
            if not record:
                continue
            bid = record["battle_id"]
            if mid == 6231:
                if bid not in self.report_ids:
                    continue
            else:
                self.report_ids.add(bid)
            events.append([mid, observed_at, record])
        if events:
            self.outbox.add(events)


def path_input(prompt, default=None):
    value = input(prompt).strip().strip('"')
    return Path(value) if value else default


def main():
    invitation_path = path_input("拖入管理员发给你的邀请 JSON 文件后按回车。\n> ")
    invitation = json.loads(invitation_path.read_text(encoding="utf-8-sig"))
    if invitation.get("version") != VERSION:
        raise ValueError("邀请版本不匹配")
    endpoint(invitation["url"])
    scope, identity = invitation["scope"], invitation["identity"]
    print(f"受邀角色 {identity['name']} · 区服 {scope['server']} · 本盟编号 {scope['union_id']}")
    print("仅上传你看到的地图队伍和可读的本盟战报。关闭此窗口即可停止。")
    if input("确认是同服同赛季的受邀角色，并同意参与试跑？输入 Y 继续。\n> ").strip().lower() != "y":
        return
    exe = path_input("拖入已安装的 PC 官服 stzb.exe 后按回车。\n> ")
    if not exe or exe.name.casefold() != "stzb.exe" or not exe.is_file():
        raise ValueError("未找到 PC 官服的 stzb.exe")
    dumpcap = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Wireshark" / "dumpcap.exe"
    if not dumpcap.is_file():
        dumpcap = path_input("拖入 Wireshark 目录里的 dumpcap.exe 后按回车。\n> ")
    if not dumpcap or dumpcap.name.casefold() != "dumpcap.exe" or not dumpcap.is_file():
        raise ValueError("请先安装 Wireshark 和 Npcap")
    (ROOT / "runtime").mkdir(exist_ok=True)
    outbox = Outbox(ROOT / "runtime" / ("outbox-" + digest(invitation["key"])[:16] + ".sqlite3"))
    collector = MemberCollector(ROOT, str(exe.resolve()), str(dumpcap.resolve()), invitation, outbox)
    uploader = Uploader(invitation, outbox)
    worker = threading.Thread(target=uploader.run, args=(collector.stop_event,), daemon=True)
    collector.start()
    worker.start()
    try:
        while not collector.stop_event.wait(1):
            pass
    except KeyboardInterrupt:
        collector.stop()
    finally:
        collector.thread.join(timeout=8)
        worker.join(timeout=12)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
        print(f"无法启动试跑采集：{error}", flush=True)
