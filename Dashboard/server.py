"""Local-only intelligence archive and bounded read-only report collection."""
import argparse
import json
import logging
import os
import secrets
import signal
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

from collector import Collector
from model import WorldState, write_json
from archive import Archive
from game_locator import GameLocator
from defense import analyze_alert, garrison_plans
from lineup_prediction import predict_alerts
from member_receiver import sources as member_sources, start_receiver

ROOT = Path(__file__).resolve().parent
STATIC = {"/": ("warroom.html", "text/html; charset=utf-8"),
          "/archive": ("intelligence.html", "text/html; charset=utf-8"),
          "/warroom.js": ("warroom.js", "application/javascript; charset=utf-8"),
          "/warroom.css": ("warroom.css", "text/css; charset=utf-8"),
          "/battlefield": ("index.html", "text/html; charset=utf-8"),
          "/intelligence.js": ("intelligence.js", "application/javascript; charset=utf-8"),
          "/intelligence.css": ("intelligence.css", "text/css; charset=utf-8"),
          "/harvest.css": ("harvest.css", "text/css; charset=utf-8"),
          "/app.js": ("app.js", "application/javascript; charset=utf-8"),
          "/app.css": ("app.css", "text/css; charset=utf-8")}


def make_handler(state, port, shutdown=None, stop_token=None, archive=None, harvester=None, evidence_reader=None, game_locator=None, garrison_trial=None):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def _allowed(self):
            if self.headers.get("Host") not in (f"127.0.0.1:{port}", f"localhost:{port}"):
                self.send_error(403)
                return False
            return True

        def _reply(self, data, kind="application/json; charset=utf-8", status=200):
            body = json.dumps(data, ensure_ascii=False).encode("utf-8") if isinstance(data, (dict, list)) else data
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'self'")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if not self._allowed():
                return
            path = urlsplit(self.path).path
            if path == "/api/state":
                snapshot = state.snapshot()
                if garrison_trial is not None:
                    from garrison_trial import available_garrison_teams
                    snapshot["garrison_teams"] = available_garrison_teams(snapshot, archive)
                self._reply(snapshot)
            elif path == "/api/game/garrison-trial" and garrison_trial:
                self._reply(garrison_trial.snapshot())
            elif path == "/api/alert-lineups" and archive:
                snapshot = state.snapshot()
                predictions = predict_alerts(archive, snapshot, record=not snapshot["health"].get("scope_pending"))
                plans = garrison_plans(archive, snapshot, predictions)
                for army_id, plan in plans.items():
                    predictions[army_id]['garrison_plan'] = plan
                self._reply({"predictions": predictions})
            elif path == "/api/alert-defense" and archive:
                army_id = parse_qs(urlsplit(self.path).query).get("army_id", [""])[0]
                self._reply(analyze_alert(archive, state.snapshot(), army_id))
            elif path == "/api/harvest/status" and harvester:
                self._reply(harvester.snapshot())
            elif path == "/api/harvest/status" and state.health.get("local_install"):
                self._reply({"available": False, "reason": "在游戏里打开或翻页，已收到的战报会归档。"})
            elif path == "/api/intel/reader" and evidence_reader:
                self._reply(evidence_reader.snapshot())
            elif path == "/api/intel/reader" and state.health.get("local_install"):
                self._reply({"available": False, "reason": "在游戏里打开战报，已收到的资料会归档。"})
            elif path == "/api/intel/member-sources" and archive:
                self._reply({"items": member_sources(archive)})
            elif path == "/api/archive/summary" and archive:
                self._reply(archive.summary())
            elif path == "/api/archive/definitions" and archive:
                self._reply(json.loads((ROOT / "field-definitions.json").read_text(encoding="utf-8")))
            elif path.startswith("/api/intel/") and archive:
                q = {k:v[0] for k,v in parse_qs(urlsplit(self.path).query).items()}
                kind = path.rsplit("/", 1)[-1]
                try:
                    self._reply(archive.intelligence.report(q.get("battle_id", "")) if kind == "report" else archive.intelligence.query(kind, **q))
                except (ValueError, TypeError) as error:
                    self._reply({"error": str(error)}, status=400)
            elif path.startswith("/api/archive/") and archive:
                q = parse_qs(urlsplit(self.path).query)
                self._reply(archive.query(path.rsplit("/", 1)[-1],
                    **{key: q[key][0] for key in ("search", "union_id", "limit", "offset", "team_key", "army_id") if key in q}))
            elif path in STATIC:
                name, kind = STATIC[path]
                self._reply((ROOT / "static" / name).read_bytes(), kind)
            else:
                self.send_error(404)

        def do_POST(self):
            if not self._allowed():
                return
            if self.path == "/api/stop" and shutdown and stop_token:
                if not secrets.compare_digest(self.headers.get("X-Local-Stop", ""), stop_token):
                    self.send_error(403)
                    return
                self._reply({"ok": True})
                threading.Thread(target=shutdown, daemon=True).start()
                return
            origin = self.headers.get("Origin")
            if origin not in (f"http://127.0.0.1:{port}", f"http://localhost:{port}"):
                self.send_error(403)
                return
            harvest_paths = ("/api/harvest/start", "/api/harvest/pause", "/api/harvest/resume")
            reader_paths = ("/api/intel/report/read", "/api/intel/reader/resume", "/api/intel/reader/pause")
            if self.path not in ("/api/calibrate", "/api/archive/profile", "/api/intel/campaigns", "/api/intel/export", "/api/game/locate", "/api/game/garrison-trial", *harvest_paths, *reader_paths) or self.headers.get_content_type() != "application/json":
                self.send_error(404)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self.send_error(400)
                return
            if length < 1 or length > 4096:
                self.send_error(413)
                return
            try:
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    raise ValueError("请输入有效的校准信息")
                if self.path == "/api/game/garrison-trial":
                    action = payload.get("action")
                    if garrison_trial is None or action not in ("arm", "arm_alert", "preview_alert", "cancel"):
                        raise ValueError("请选择启动或停止一次驻守任务")
                    if action in ("arm_alert", "preview_alert"):
                        if not isinstance(payload.get("alert"), dict):
                            raise ValueError("请在主力来袭卡片上选择驻守目标")
                        self._reply(garrison_trial.arm(payload["alert"], payload.get("team_id"),
                                    allow_recruit=payload.get("allow_recruit", False),
                                    preview_only=action == "preview_alert"))
                    else:
                        self._reply(garrison_trial.cancel() if action == "cancel" else garrison_trial.arm())
                    return
                if self.path == "/api/game/locate":
                    if game_locator is None:
                        raise ValueError("当前服务未启用游戏内定位")
                    with state.lock:
                        health = state.health
                        connection = health.get("connection") if health["status"] == "live" and time.time()-health["last_packet_at"] <= 30 else None
                    point = payload.get("coord")
                    try:
                        result = game_locator.locate(connection, point)
                    except (RuntimeError, OSError) as error:
                        logging.warning("Game coordinate jump to %s failed: %s", point, error)
                        self._reply({"ok": False, "error": str(error)}, status=409)
                    else:
                        self._reply(result)
                    return
                if self.path in reader_paths:
                    if not evidence_reader:
                        raise ValueError("当前服务未启用战报补全")
                    if self.path.endswith("/pause"):
                        self._reply(evidence_reader.pause())
                    elif self.path.endswith("/resume"):
                        self._reply(evidence_reader.resume(payload.get("max_requests", 30),
                            continuous=payload.get("continuous", False), wait_for_ready=payload.get("wait_for_ready", False)))
                    else:
                        self._reply(evidence_reader.enqueue(payload.get("battle_ids")))
                    return
                if self.path == "/api/intel/campaigns" and archive:
                    self._reply(archive.intelligence.save_campaign(payload))
                    return
                if self.path == "/api/intel/export" and archive:
                    self._reply(archive.intelligence.export(payload))
                    return
                if self.path in harvest_paths:
                    if not harvester:
                        raise ValueError("当前服务未启用自动读取")
                    action = self.path.rsplit("/", 1)[-1]
                    if action == "pause":
                        self._reply(harvester.pause())
                    else:
                        self._reply(harvester.start(payload, resume=action == "resume"))
                    return
                if self.path == "/api/archive/profile" and archive:
                    identity = ("server", "season", "account_role_id", "union_id")
                    if any(str(payload.get(k, "")).strip() != str(archive.profile.get(k, "")) for k in identity):
                        raise ValueError("当前采集已绑定此角色与赛季。更换角色、赛季或同盟前，需要停止采集并核对新游戏环境，避免资料混入错误档案。")
                    self._reply(archive.activate_profile(payload))
                    return
                state.calibrate(payload)
                self._reply({"ok": True})
            except (ValueError, TypeError, KeyError) as error:
                self._reply({"ok": False, "error": str(error)}, status=400)

    return Handler


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--member-port", type=int, default=8788)
    parser.add_argument("--local-install", type=Path, metavar="WORKSPACE")
    args = parser.parse_args()
    workspace = args.local_install or ROOT
    (workspace / "runtime").mkdir(parents=True, exist_ok=True)
    if args.local_install:
        from installed_mode import InstalledCollector, load_installation
        profile, settings = load_installation(workspace)
    else:
        profile = None
    state = WorldState(workspace)
    archive = Archive(workspace, profile=profile)
    archive.seed_cache(workspace / "data" / "legacy-projection.json")
    archive.backfill_report_briefs()
    collector = InstalledCollector(state, archive, settings) if args.local_install else Collector(state, archive)
    def report_ready():
        settings = json.loads((ROOT / "capture-source.json").read_text(encoding="utf-8"))
        if collector.source != "pc" or settings.get("mode") != "pc" or settings.get("profile_id") != archive.profile_id:
            return "自动历史读取需要已核对的PC版角色与赛季绑定"
        with state.lock:
            if state.health["status"] != "live" or time.time() - state.health["last_packet_at"] > 30:
                return "请保持PC版游戏在线，等待采集连接恢复后续采"
            if state.health.get("resync_required"):
                return "请进入PC版地图，等待首帧场景采集完成后开始补全"
            if state.health.get("archive_errors"):
                return "归档出现错误，请先检查本地保存状态"
        return None
    forecast_stop = threading.Event()
    locator = GameLocator()
    trial = harvester = evidence_reader = None
    if not args.local_install:
        from bridge import ReadOnlyBridge
        from evidence_reader import ReportEvidenceReader
        from garrison_trial import GarrisonTrial
        from harvest import BattleHarvester

        read_bridge = ReadOnlyBridge()
        harvester = BattleHarvester(ROOT, archive, read_bridge, report_ready)
        evidence_reader = ReportEvidenceReader(archive, read_bridge, report_ready)
        collector.report_observer = harvester
        trial = GarrisonTrial(state, archive, locator, report_ready, ROOT / "runtime")

    def archive_forecasts():
        try:
            with archive.lock, archive.db:
                archive.intelligence.backfill_same_army_audits()
        except Exception:
            logging.exception('Unable to backfill same-army audits')
        while not forecast_stop.wait(4):
            with state.lock:
                active = (state.health['status'] == 'live' and not state.health.get('scope_pending')
                          and time.time() - state.health['last_scene_at'] <= 15)
            if active:
                try:
                    predict_alerts(archive, state.snapshot(), record=True)
                except Exception:
                    logging.exception('Unable to archive the current incoming forecasts')

    forecast_thread = threading.Thread(target=archive_forecasts, daemon=True, name='incoming-forecast-archive')
    def stop(*_):
        forecast_stop.set()
        if trial:
            trial.stop.set()
            evidence_reader.pause()
            harvester.pause()
        collector.stop()
        server.shutdown()
    stop_token = secrets.token_hex(32)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(state, args.port, stop, stop_token, archive, harvester, evidence_reader, locator, trial))
    member_service = None
    if not args.local_install:
        try:
            member_service, member_thread = start_receiver(archive, args.member_port)
        except OSError as error:
            print(f"成员接收端口启动失败 {error}", flush=True)
    write_json(workspace / "runtime" / "service.json", {"port": args.port, "pid": os.getpid(), "stop_token": stop_token})
    collector.start()
    forecast_thread.start()
    if trial:
        trial.thread.start()
    def signal_stop(*_):
        threading.Thread(target=stop, daemon=True).start()
    signal.signal(signal.SIGINT, signal_stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, signal_stop)
    print(f"Local: http://127.0.0.1:{args.port}", flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        forecast_stop.set()
        if member_service:
            member_service.shutdown()
            member_service.server_close()
            member_thread.join(timeout=2)
        collector.stop()
        collector.thread.join(timeout=8)
        forecast_thread.join()
        if trial:
            trial.stop.set()
            trial.thread.join(timeout=12)
            evidence_reader.close()
            harvester.close()
        server.server_close()
        state.persist()
        archive.close()


if __name__ == "__main__":
    main()
