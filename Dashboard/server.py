"""Local-only intelligence archive and bounded read-only report collection."""
import argparse
import hashlib
import json
import logging
import os
import secrets
import signal
import sqlite3
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
from lineup_prediction import predict_alerts, army_lineup
from member_receiver import sources as member_sources, start_receiver
from terrain import TerrainCatalog
from defense_model import analysis_budget
from read_cache import ReadCache, QueryBusy

ROOT = Path(__file__).resolve().parent
STATIC = {"/role-context.css": ("role-context.css", "text/css; charset=utf-8"),"/role-context.js": ("role-context.js", "application/javascript; charset=utf-8"),"/attendance": ("attendance.html", "text/html; charset=utf-8"),
          "/attendance.js": ("attendance.js", "application/javascript; charset=utf-8"),
          "/attendance.css": ("attendance.css", "text/css; charset=utf-8"),
          "/formations.js": ("formations.js", "application/javascript; charset=utf-8"),
          "/formations.css": ("formations.css", "text/css; charset=utf-8"),
          "/": ("warroom.html", "text/html; charset=utf-8"),
          "/archive": ("intelligence.html", "text/html; charset=utf-8"),
          "/warroom.js": ("warroom.js", "application/javascript; charset=utf-8"),
          "/warroom.css": ("warroom.css", "text/css; charset=utf-8"),
          "/battlefield": ("index.html", "text/html; charset=utf-8"),
          "/intelligence.js": ("intelligence.js", "application/javascript; charset=utf-8"),
          "/intelligence.css": ("intelligence.css", "text/css; charset=utf-8"),
          "/harvest.css": ("harvest.css", "text/css; charset=utf-8"),
          "/app.js": ("app.js", "application/javascript; charset=utf-8"),
          "/app.css": ("app.css", "text/css; charset=utf-8")}


class UiRevision:
    """Publish settled frontend changes without interrupting game collection."""
    def __init__(self, root):
        self.paths = [Path(root) / 'static' / name for name in ('index.html','app.js','app.css')]
        self.lock = threading.Lock()
        self.key, self.pending, self.revision = None, None, ''

    def current(self, now=None):
        now = time.monotonic() if now is None else now
        try:
            key = tuple((p.stat().st_mtime_ns,p.stat().st_size) for p in self.paths)
        except OSError:
            return self.revision
        with self.lock:
            if key == self.key:
                self.pending = None
            elif self.key is None or self.pending and self.pending[0] == key and now-self.pending[1] >= 2:
                self.key, self.pending = key, None
                self.revision = hashlib.sha256(repr(key).encode()).hexdigest()[:20]
            elif not self.pending or self.pending[0] != key:
                self.pending = (key,now)
            return self.revision


def make_handler(state, port, shutdown=None, stop_token=None, archive=None, harvester=None, evidence_reader=None, game_locator=None, garrison_trial=None, report_backfill=None):
    terrain = TerrainCatalog(ROOT)
    ui_revision = UiRevision(ROOT)
    query_slots = threading.BoundedSemaphore(4)
    defense_slot = threading.BoundedSemaphore(1)
    read_cache = ReadCache()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def _allowed(self):
            if self.headers.get("Host") not in (f"127.0.0.1:{port}", f"localhost:{port}"):
                self.send_error(403)
                return False
            return True

        def _reply(self, data, kind="application/json; charset=utf-8", status=200, headers=None):
            body = json.dumps(data, ensure_ascii=False).encode("utf-8") if isinstance(data, (dict, list)) else data
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            for key, value in (headers or {}).items():
                self.send_header(key, str(value))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'self'")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError,ConnectionResetError,ConnectionAbortedError):
                pass

        def _archive_reply(self, query, *, defense=False, writer=False, cache=False):
            slot = defense_slot if defense else query_slots
            scope=(archive.profile_id,str(archive.profile['union_id'])) if archive else None
            def compute():
                if not slot.acquire(blocking=False):raise QueryBusy('查询名额已满')
                try:
                    with analysis_budget():
                        if writer:
                            return query(archive)
                        with archive.read_snapshot(timeout=3 if defense else 10) as reader:
                            if (reader.profile_id,str(reader.profile['union_id']))!=scope:
                                raise QueryBusy('当前角色档案已变化')
                            return query(reader)
                finally:
                    slot.release()
            try:
                if cache:
                    try:
                        config=(archive.root/'config.json').stat()
                        preferences=(config.st_mtime_ns,config.st_size)
                    except OSError:preferences=None
                    url=urlsplit(self.path)
                    params=tuple(sorted((k,v[0])for k,v in parse_qs(url.query).items()))
                    revision=getattr(archive,'contribution_revision',0)if url.path in ('/api/attendance','/api/intel/formations','/api/intel/territory-events')else None
                    result=read_cache.run((scope,preferences,url.path,params,revision),compute)
                else:
                    result=compute()
                self._reply(result)
            except QueryBusy:
                self._reply({'error':'查询正在处理中，请稍后重试；实时采集继续运行'},status=503,
                            headers={'Retry-After':'2'})
            except (TimeoutError, sqlite3.OperationalError) as error:
                logging.warning('Bounded archive query %s: %s', urlsplit(self.path).path, error)
                self._reply({'error': '本次查询暂未完成，请稍后重试；实时采集继续运行'}, status=503)

        def do_GET(self):
            if not self._allowed():
                return
            path = urlsplit(self.path).path
            if state is not None and ((path.startswith('/api/archive/')and path!='/api/archive/definitions')or
                    path.startswith('/api/intel/')and path not in ('/api/intel/backfill','/api/intel/reader','/api/intel/harvest')):
                with state.lock:pending=state.health.get('identity_pending')or state.health.get('scope_pending')
                if pending:
                    self._reply({'error':'正在自动核对当前账号及统计范围，请稍后再试','identity_pending':True},status=409)
                    return
            if path == "/api/instance":
                self._reply({"app":"stzb-local-observer", "instance_id":state.health.get("instance_id"),
                             "local_install":bool(state.health.get("local_install")),
                             "program_id":ROOT.parent.name if state.health.get("local_install") else "source"})
            elif path == "/api/map-terrain":
                with state.lock:
                    pending = state.health.get("scope_pending") or state.health.get("identity_pending")
                    map_config_id = state.map_config_id
                result = terrain.response(archive.profile, map_config_id) if archive and not pending else None
                if result is None:
                    self.send_error(404)
                    return
                body, size, config_id = result
                self._reply(body, "application/octet-stream", headers={"Content-Encoding": "gzip",
                            "X-Map-Size": size, "X-Map-Config": config_id})
            elif path == "/api/state":
                snapshot = state.snapshot()
                if isinstance(snapshot.get('health'),dict):
                    snapshot['health']['ui_revision'] = ui_revision.current()
                priority = getattr(report_backfill,'priority',None)
                if priority is not None:
                    snapshot['health']['report_priority'] = {'updates':priority.snapshot()['updates']}
                if garrison_trial is not None:
                    from garrison_trial import available_garrison_teams
                    snapshot["garrison_teams"] = available_garrison_teams(snapshot, archive)
                self._reply(snapshot)
            elif path == "/api/game/garrison-trial" and garrison_trial:
                self._reply(garrison_trial.snapshot())
            elif path == "/api/alert-lineups" and archive:
                snapshot = state.snapshot()
                if snapshot["health"].get("scope_pending"):
                    self._reply({"predictions": {}})
                    return
                def lineups(writer):
                    predictions = predict_alerts(writer, snapshot, record=True)
                    if parse_qs(urlsplit(self.path).query).get("include_garrison") != ["0"]:
                        with writer.read_snapshot() as reader:
                            for army_id, plan in garrison_plans(reader, snapshot, predictions).items():
                                predictions[army_id]['garrison_plan'] = plan
                    return {"predictions": predictions}
                self._archive_reply(lineups, writer=True)
            elif path == "/api/army-lineups" and archive:
                snapshot = state.snapshot()
                if snapshot['health'].get('scope_pending') or snapshot['health'].get('identity_pending'):
                    self._reply({'live_lineup':True, 'items':[], 'prediction':None, 'match':'scope_pending'})
                    return
                army_id = parse_qs(urlsplit(self.path).query).get('army_id', [''])[0]
                self._archive_reply(lambda reader: army_lineup(reader, snapshot, army_id))
            elif path == "/api/alert-defense" and archive:
                army_id = parse_qs(urlsplit(self.path).query).get("army_id", [""])[0]
                self._archive_reply(lambda reader: analyze_alert(reader, state.snapshot(), army_id), defense=True)
            elif path == "/api/harvest/status" and harvester:
                self._reply(harvester.snapshot())
            elif path == "/api/harvest/status" and state.health.get("local_install"):
                self._reply({"available": False, "reason": "在游戏里打开或翻页，已收到的战报会归档。"})
            elif path == "/api/intel/reader" and evidence_reader:
                self._reply(evidence_reader.snapshot())
            elif path == "/api/intel/reader" and state.health.get("local_install"):
                self._reply({"available": False, "reason": "在游戏里打开战报，已收到的资料会归档。"})
            elif path == "/api/intel/backfill":
                self._reply(report_backfill.snapshot() if report_backfill else {"available": False, "reason": "当前运行版尚未启用一键补报"})
            elif path == "/api/intel/collection-status" and archive:
                self._reply(archive.lineup_projection.snapshot())
            elif path == "/api/intel/member-sources" and archive:
                self._archive_reply(lambda reader: {"items": member_sources(reader)})
            elif path == "/api/attendance" and archive:
                if state.health.get('identity_pending') or state.health.get('scope_pending'):
                    self._reply({'error':'正在核对当前登录角色，请稍后再试'},status=409)
                    return
                from attendance import query
                q = {k:v[0] for k,v in parse_qs(urlsplit(self.path).query).items()}
                try:
                    self._archive_reply(lambda reader: query(reader,q),cache=True)
                except (ValueError,TypeError) as error:
                    self._reply({'error':str(error)},status=400)
            elif path=='/api/role-context' and archive:
                from role_context import context
                with state.lock:
                    ready=not(state.health.get('identity_pending')or state.health.get('scope_pending'))
                self._reply({**context(archive.profile),'profile':archive.profile,'profile_id':archive.profile_id,'ready':ready})
            elif path == "/api/archive/summary" and archive:
                self._archive_reply(lambda reader: reader.summary())
            elif path == "/api/intel/lineup-audit" and archive:
                try:
                    result = json.loads((archive.root/'runtime'/'lineup-consistency.json').read_text(encoding='utf-8'))
                    if result.get('profile_id') != archive.profile_id:
                        raise ValueError('Audit profile changed')
                except (OSError,ValueError):
                    result = {'profile_id':archive.profile_id,'running':True,'checked_armies':0,
                              'problem_armies':0,'issues':[]}
                self._reply(result)
            elif path == "/api/archive/definitions" and archive:
                self._reply(json.loads((ROOT / "field-definitions.json").read_text(encoding="utf-8")))
            elif path.startswith("/api/intel/") and archive:
                q = {k:v[0] for k,v in parse_qs(urlsplit(self.path).query).items()}
                kind = path.rsplit("/", 1)[-1]
                try:
                    self._archive_reply(lambda reader: reader.intelligence.report(q.get("battle_id", "")) if kind == "report" else reader.intelligence.query(kind, **q),
                                        cache=kind in ('personnel','summary','operations','encounters','formations','territory-events'))
                except (ValueError, TypeError) as error:
                    self._reply({"error": str(error)}, status=400)
            elif path.startswith("/api/archive/") and archive:
                q = parse_qs(urlsplit(self.path).query)
                self._archive_reply(lambda reader: reader.query(path.rsplit("/", 1)[-1],
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
            backfill_paths = ("/api/intel/backfill/start", "/api/intel/backfill/pause")
            if self.path not in ("/api/attendance/rules", "/api/calibrate", "/api/archive/profile", "/api/intel/campaigns", "/api/intel/export", "/api/game/locate", "/api/game/garrison-trial", "/api/intel/priority/displayed", *harvest_paths, *reader_paths, *backfill_paths) or self.headers.get_content_type() != "application/json":
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
                if self.path == '/api/attendance/rules' and archive:
                    if state.health.get('identity_pending') or state.health.get('scope_pending'):
                        raise ValueError('请等待当前角色核对完成')
                    from attendance import save_rules
                    self._reply(save_rules(archive,payload))
                    return
                if self.path == '/api/intel/priority/displayed':
                    priority = getattr(report_backfill,'priority',None)
                    aid,version = payload.get('army_id'),payload.get('version')
                    if priority is None or not isinstance(aid,str) or not isinstance(version,str):
                        raise ValueError('当前没有可核对的队伍显示记录')
                    self._reply({'ok':priority.displayed(aid,version)})
                    return
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
                if self.path in backfill_paths:
                    if not report_backfill:
                        raise ValueError("当前运行版尚未启用一键补报")
                    self._reply(report_backfill.pause() if self.path.endswith("/pause") else
                                report_backfill.start(payload.get("maximum", getattr(report_backfill, 'DEFAULT_MAXIMUM', 5))))
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
    os.environ["STZB_RUNTIME_ROOT"] = str((workspace / "runtime").resolve())
    if args.local_install:
        from installed_mode import load_installation
        from portable_mode import PortableCollector as InstalledCollector
        profile, settings = load_installation(workspace)
    else:
        profile = None
        settings=json.loads((workspace/'capture-source.json').read_text(encoding='utf8'))
        if settings.get('mode')in ('pc','dashen')and settings.get('auto_identity'):
            from portable_mode import prepare_installation,PortableCollector as InstalledCollector
            try:profile,settings=prepare_installation(workspace,settings,timeout=0)
            except (RuntimeError,ValueError,OSError)as error:
                logging.warning('Current role not ready: %s',str(error)[:160])
    state = WorldState(workspace)
    state.health["instance_id"] = secrets.token_hex(16)
    archive = Archive(workspace, profile=profile)
    state.restore_player_traits(archive.db.execute('SELECT * FROM player_traits WHERE profile_id=?',
                                                 (archive.profile_id,)).fetchall())
    archive.seed_cache(workspace / "data" / "legacy-projection.json")
    archive.backfill_report_briefs()
    auto_identity=bool(args.local_install or settings.get('auto_identity'))
    if auto_identity and settings.get('profile_id')!=archive.profile_id:
        settings['profile_id']=archive.profile_id
        write_json(workspace/'capture-source.json',settings)
    collector = InstalledCollector(state, archive, settings, seed_path=ROOT / "season-seed.sqlite3") if auto_identity else Collector(state, archive)
    def report_ready():
        settings = json.loads((workspace / "capture-source.json").read_text(encoding="utf-8"))
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
    trial = harvester = evidence_reader = report_backfill = read_bridge = army_focus = None
    from bridge import ReadOnlyBridge
    from evidence_reader import ReportEvidenceReader
    from garrison_trial import ARMED, GarrisonTrial
    from harvest import BattleHarvester
    from report_backfill import ReportBackfill

    source_settings = json.loads((workspace / "capture-source.json").read_text(encoding="utf-8"))
    # The account-bound historical garrison experiment is not a public feature.
    trial = None if settings.get('distribution_v2')else GarrisonTrial(state, archive, locator, report_ready, workspace / "runtime")

    def backfill_connection():
        source_settings = json.loads((workspace / "capture-source.json").read_text(encoding="utf-8"))
        if (collector.source not in ("pc", "dashen") or source_settings.get("mode") != collector.source
                or source_settings.get("profile_id") != archive.profile_id):
            return None
        with state.lock:
            health = state.health
            if (health["status"] == "live" and time.time() - health["last_packet_at"] <= 45
                    and not health.get("scope_pending") and not health.get("archive_errors")):
                return health.get("connection")
        return None

    def read_ready():
        if not backfill_connection():
            return "等候当前角色、区服和赛季核对；核对后自动续采"
        if collector.source != "dashen":
            return report_ready()
        if not backfill_connection():
            return "等待已核对角色登录；游戏连接恢复后自动续采"
        if trial:
            with trial.lock:
                if trial.status.get("phase") in ARMED:
                    return "驻守操作进行中，后台补采稍后继续"
        return None

    if collector.source == "dashen":
        from android_report_bridge import AndroidReadOnlyBridge
        read_bridge = AndroidReadOnlyBridge(workspace, archive, backfill_connection,identity_observer=collector._confirm_identity if auto_identity else None)
    else:
        read_bridge = ReadOnlyBridge()
    harvester = BattleHarvester(workspace, archive, read_bridge, read_ready)
    evidence_reader = ReportEvidenceReader(archive, read_bridge, read_ready)
    collector.report_observer = harvester
    from union_relations import ClientUnionRelations
    union_sync = ClientUnionRelations(state,archive,backfill_connection)
    collector.relation_observer = lambda mid,value,at: union_sync.packet(mid,value,backfill_connection(),at)

    def other_game_task():
        if trial:
            with trial.lock:
                if trial.status.get('phase')in ARMED:return True
        with evidence_reader.lock,harvester.lock:
            return evidence_reader.active or harvester.data.get("status") == "running"

    if collector.source == "dashen":
        from background_reports import BackgroundReports
        from game_army_focus import GameArmyFocus
        army_focus = GameArmyFocus(state, read_bridge, backfill_connection,relation_observer=union_sync.cache,
                                  palette_observer=union_sync.palette)
        seed_baseline = {}
        seed_path = ROOT / "season-seed.sqlite3"
        if args.local_install and seed_path.is_file():
            import sqlite3
            with sqlite3.connect(seed_path.resolve().as_uri() + '?mode=ro', uri=True) as seed_db:
                seed_scope = json.loads(seed_db.execute('SELECT data FROM manifest').fetchone()[0])
                if all(seed_scope.get(key) == archive.profile[key] for key in ('server','season')):
                    seed_baseline = {table:seed_db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
                                     for table in ('battle_reports','report_bodies')}
        def focused_army():
            with state.lock:
                focus = state.game_army_focus
                return focus['army_id'] if focus and time.time()-focus.get('validated_at', 0) < 10 else None
        def priority_state():
            with state.lock:
                if (state.health.get('status')!='live' or state.health.get('scope_pending')
                        or time.time()-state.health.get('last_message_at',0)>30):
                    return None
                group_id = (state.game_army_focus or {}).get('group_id')
                return {'armies':[{k:r.get(k) for k in ('id','owner','state_code','state_id','target_type','begin','end','seen_at')}
                                  for r in state.armies.values() if r.get('epoch')==state.epoch],
                        'focus':focused_army(),'focus_event':(state.game_army_focus or {}).get('event_id'),
                        'focus_members':[r['id'] for r in state.armies.values() if group_id
                            and str(r.get('army_group_id'))==group_id and r.get('visible') and r.get('epoch')==state.epoch]}
        report_backfill = BackgroundReports(workspace, archive, read_bridge, harvester, evidence_reader, read_ready,
                                            seed_baseline=seed_baseline, focus_provider=focused_army,state_provider=priority_state)
    else:
        report_backfill = ReportBackfill(archive, locator, backfill_connection, other_game_task)

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
    restart_requested=threading.Event()
    stopping=threading.Event()
    def stop(*_,restart=False):
        if not restart:restart_requested.clear()
        if stopping.is_set():return
        stopping.set()
        forecast_stop.set()
        if army_focus:
            army_focus.close()
        if report_backfill:
            report_backfill.close()
        if trial:
            trial.stop.set()
        if evidence_reader:evidence_reader.pause()
        if harvester:harvester.pause()
        collector.stop()
        server.shutdown()
    stop_token = secrets.token_hex(32)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(state, args.port, stop, stop_token, archive, harvester, evidence_reader, locator, trial, report_backfill))
    def switch_role(identity):
        if stopping.is_set():return
        restart_requested.set()
        threading.Thread(target=lambda:stop(restart=True),daemon=True,name='switch-role').start()
    if hasattr(collector,'identity_change_callback'):collector.identity_change_callback=switch_role
    member_service = None
    if not args.local_install:
        try:
            member_service, member_thread = start_receiver(archive, args.member_port)
        except OSError as error:
            print(f"成员接收端口启动失败 {error}", flush=True)
    write_json(workspace / "runtime" / "service.json", {"port": args.port, "pid": os.getpid(), "stop_token": stop_token, "instance_id":state.health["instance_id"]})
    from lineup_audit import LineupAudit
    lineup_audit = LineupAudit(archive)
    collector.start()
    archive.lineup_projection.start()
    if army_focus:
        army_focus.start()
    lineup_audit.start()
    if report_backfill and hasattr(report_backfill, 'restore'):
        report_backfill.restore()
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
        archive.lineup_projection.close()
        if army_focus:
            army_focus.close()
        lineup_audit.close()
        if report_backfill:
            report_backfill.close()
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
        if evidence_reader:evidence_reader.close()
        if harvester:harvester.close()
        if read_bridge and hasattr(read_bridge, "close"):
            read_bridge.close()
        server.server_close()
        state.persist()
        archive.close()
    return restart_requested.is_set()


if __name__ == "__main__":
    while main():pass
