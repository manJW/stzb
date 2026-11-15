"""Bounded, resumable union-report history reading through the verified PC bridge."""
import copy
import json
import threading
import time

from archive import dictionaries
from bridge import union_report_request
from model import write_json


class BattleHarvester:
    INTERVAL = 2.5
    RESPONSE_TIMEOUT = 30

    def __init__(self, root, archive, bridge, ready, autostart=True):
        self.archive, self.bridge, self.ready = archive, bridge, ready
        self.profile_id = archive.profile_id
        from role_context import perspective_id
        perspective=perspective_id(archive.profile)
        self.path = root / 'runtime' / ('report-harvest-'+self.profile_id+'-'+perspective+'.json')
        legacy_path=root/'runtime'/('report-harvest-'+self.profile_id+'.json')
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self.closed = threading.Event()
        self.pending = None
        # Let any request made before this service started finish before our first query.
        self.not_before = time.monotonic() + self.RESPONSE_TIMEOUT
        self.run_started = 0
        self.wait_for_ready = False
        self.continuous = False
        self.latest_due = 0
        self.fresh_due = 0
        self.fresh_reads = 0
        self.urgent_latest = False
        self.history_not_before = 0
        self.priority_until = 0
        self.data = {"profile_id": self.profile_id, "status": "idle", "reason": "尚未开始自动读取",
            "cursor": 0, "pages": 0, "new_reports": 0, "received_reports": 0,
            "run_pages": 0, "max_pages": 100, "max_minutes": 20, "updated_at": 0,
            "available_end": False, "fresh_ranges": [], "fresh_top": 0, "fresh_pages": 0,
            "scope": "当前账号游戏界面已核验的同盟战报筛选"}
        self.data['perspective_id']=perspective
        saved_path=self.path if self.path.exists()else legacy_path
        if saved_path.exists():
            saved = json.loads(saved_path.read_text(encoding="utf-8"))
            if saved.get("profile_id") == self.profile_id and saved.get('perspective_id')==self.data['perspective_id']:
                self.data.update(saved)
                if self.data["status"] == "running" and (self.run_started or not self.wait_for_ready):
                    self.data.update(status="paused", reason="服务曾中断，可从已保存游标续采")
                    self.not_before = time.monotonic() + self.RESPONSE_TIMEOUT
        self.thread = threading.Thread(target=self._run, daemon=True, name="report-harvester")
        if autostart:
            self.thread.start()

    def snapshot(self):
        with self.lock:
            result = copy.deepcopy(self.data)
            result["waiting_response"] = self.pending is not None
            result["can_resume"] = self.data["status"] in ("paused", "error")
            return result

    def request_latest(self):
        """Coalesce live evidence requests; never cancel an in-flight page."""
        with self.lock:
            self.urgent_latest = True
        self.wake.set()

    def yield_history(self, seconds=5):
        with self.lock:
            self.priority_until = max(self.priority_until, time.monotonic() + seconds)

    def _save(self):
        self.data["updated_at"] = time.time()
        write_json(self.path, self.data)

    def _check_ready(self):
        if self.archive.profile_id != self.profile_id:
            raise ValueError("当前档案已变更，请重新核对采集绑定")
        reason = self.ready()
        if reason:
            raise ValueError(reason)

    def start(self, payload=None, resume=False):
        payload = payload or {}
        if set(payload) - {"max_pages", "max_minutes", "wait_for_ready"}:
            raise ValueError("只能设置本轮读取页数、时间和等待连接")
        wait_for_ready = payload.get("wait_for_ready", False)
        if type(wait_for_ready) is not bool:
            raise ValueError("等待连接设置须为布尔值")
        limits = {}
        for key, maximum, default in (("max_pages", 1000, 100), ("max_minutes", 120, 20)):
            value = payload.get(key, self.data.get(key, default))
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError("页数须为1～1000，时间须为1～120分钟")
            limits[key] = value
        if not wait_for_ready or self.archive.profile_id != self.profile_id:
            self._check_ready()
        reason = self.ready() if wait_for_ready else None
        with self.lock:
            if self.data["status"] == "running":
                raise ValueError("历史读取正在进行")
            if resume and self.data["status"] not in (("paused", "error", "available_end") if self.continuous else ("paused", "error")):
                raise ValueError("当前没有可续采的任务")
            if not resume:
                self.data.update(cursor=0, pages=0, new_reports=0, received_reports=0, available_end=False)
            self.data.update(limits, status="running", reason=reason or "正在读取历史战报", run_pages=0)
            self.wait_for_ready = wait_for_ready
            self.run_started = 0 if reason else time.monotonic()
            self._save()
            self.wake.set()
            return self.snapshot()

    def _halt(self, status, reason):
        if self.pending:
            # Drain a possible late response before any new request; it cannot advance a cursor.
            self.not_before = max(self.not_before, self.pending["deadline"])
        self.pending = None
        self.data.update(status=status, reason=reason)
        self._save()
        self.wake.set()

    def pause(self):
        with self.lock:
            if self.data["status"] == "running":
                self._halt("paused", "已暂停，已入库记录和翻页位置已保存")
            return self.snapshot()

    def fail(self, reason):
        with self.lock:
            if self.data["status"] == "running":
                self._halt("error", reason)

    def on_request(self, body, observed_at):
        with self.lock:
            pending = self.pending
            if not pending:
                self.not_before = max(self.not_before, time.monotonic() + self.RESPONSE_TIMEOUT)
                if self.data["status"] == "running":
                    self._halt("paused", "检测到游戏内另一项战报翻页，已暂停避免混用游标")
                return
            if observed_at < pending["sent_at"]:
                return
            if body != pending["body"]:
                self._halt("paused", "检测到游戏内另一项战报翻页，已暂停避免混用游标")
                return
            pending["observed"] = True

    def on_page(self, value, observed_at, stats):
        with self.lock:
            pending = self.pending
            if not pending or not pending["observed"] or observed_at < pending["sent_at"]:
                return
            try:
                if not isinstance(value, list):
                    raise ValueError("战报返回结构未通过核对")
                records = [r for r in dictionaries(value) if r.get("battle_id") and
                    ("attack_name" in r or "defend_name" in r)]
                if value and not records:
                    raise ValueError("返回内容不是已核验的战报页")
                ids = sorted({int(r["battle_id"]) for r in records})
                union = str(self.archive.profile["union_id"])
                if any(union not in (str(r.get("attack_unionid")), str(r.get("defend_unionid"))) for r in records):
                    raise ValueError("战报同盟与当前档案不一致，已停止推进游标")
                if ids and (ids[0] <= 0 or (pending["cursor"] and ids[-1] >= pending["cursor"])):
                    raise ValueError("战报页未严格早于当前游标，已暂停核对")
                if set(map(str, ids)) != set(stats["battle_ids"]) | set(stats.get("excluded_battle_ids", [])):
                    raise ValueError("战报入库回执与返回内容不一致")
                pending["result"] = {"ids": ids, "new": len(stats["new_battle_ids"]), "at": observed_at,
                                     "floor_reached": bool(stats.get('excluded_battle_ids')),
                                     "admitted_ids": sorted(int(bid) for bid in stats["battle_ids"])}
                self.wake.set()
            except (ValueError, TypeError, KeyError) as error:
                self._halt("error", str(error))

    def _step(self):
        with self.lock:
            if self.data["status"] != "running":
                return
            moment = time.monotonic()
            if self.pending:
                pending = self.pending
                if pending.get("ack") and pending.get("result") is not None:
                    result = pending["result"]
                    self.pending = None
                    self.not_before = moment + self.INTERVAL
                    if result.get('floor_reached'):
                        self.data.update(available_end=True, retention_end=True,
                                         reason='已到采集起始日期，继续检查最新战报')
                        if pending.get('fresh') == 'gap' and self.data['fresh_ranges']:
                            self.data['fresh_ranges'].pop()
                        elif pending.get('latest'):
                            self.data['latest_cursor'] = 0
                        result['ids'] = result['admitted_ids']
                    if pending.get('urgent'):
                        self.data['priority_head'] = {'sent_at':pending['sent_at'], 'received_at':result['at'],
                                                     'reports':len(result['ids'])}
                    if pending.get('fresh'):
                        self._fresh_result(pending, result, moment)
                        return
                    if self.continuous and pending.get("latest"):
                        ids = result["ids"]
                        boundary = self.data.get("latest_highwater", 0)
                        top = self.data.get("latest_top", 0) or (max(ids) if ids else 0)
                        complete = result.get('floor_reached') or not ids or not boundary or min(ids) <= boundary
                        self.latest_due = moment + 60 if complete else 0
                        self.data.update(latest_cursor=0 if complete else min(ids),
                            latest_top=0 if complete else top,
                            latest_highwater=max(top, boundary) if complete else boundary)
                        self.data.update(latest_pages=self.data.get("latest_pages", 0) + 1,
                            last_latest_at=result["at"], new_reports=self.data["new_reports"] + result["new"],
                            received_reports=self.data["received_reports"] + len(result["ids"]))
                        if pending['cursor'] == 0:
                            self.fresh_due = moment + 60
                            self.data['fresh_top'] = max(self.data['fresh_top'], top)
                            self.data['last_head_at'] = result['at']
                        if complete and not self.data['fresh_ranges']:
                            self.data['latest_highwater'] = max(self.data['latest_highwater'], self.data['fresh_top'])
                        self._save()
                        return
                    if self.continuous and not result["ids"]:
                        self.data["available_end"] = True
                        self.data["reason"] = "当前可访问历史页已补完，继续检查最新战报"
                        self._save()
                        return
                    if not result["ids"]:
                        self.data["available_end"] = True
                        self._halt("available_end", "当前战报筛选未再返回记录；不代表本赛季全部战报均可获取")
                        return
                    self.data.update(cursor=result["ids"][0], pages=self.data["pages"] + 1,
                        run_pages=self.data["run_pages"] + 1,
                        new_reports=self.data["new_reports"] + result["new"],
                        received_reports=self.data["received_reports"] + len(result["ids"]),
                        last_response_at=result["at"])
                    if self.continuous and pending["cursor"] == 0:
                        self.latest_due = moment + 60
                        self.data["latest_highwater"] = max(result["ids"])
                        self.data['fresh_top'] = max(self.data['fresh_top'], max(result['ids']))
                        self.data['last_head_at'] = result['at']
                        self.fresh_due = moment + 60
                    self._save()
                elif moment >= pending["deadline"]:
                    self._halt("paused", "等待游戏战报超时，游标未推进；可稍后续采")
                    return
                else:
                    return
            if self.continuous and (self.data["run_pages"] >= self.data["max_pages"]
                    or (self.run_started and moment - self.run_started >= self.data["max_minutes"] * 60)):
                self.history_not_before = moment + 60
                self.data["run_pages"] = 0
                self.run_started = 0
                self.data["reason"] = "历史翻页冷却60秒，随后自动续采"
                self._save()
                if not self.urgent_latest:
                    return
            if self.data["run_pages"] >= self.data["max_pages"]:
                self._halt("paused", "达到本轮页数上限，可从已保存位置续采")
                return
            if self.run_started and moment - self.run_started >= self.data["max_minutes"] * 60:
                self._halt("paused", "达到本轮时间上限，可从已保存位置续采")
                return
            if moment < self.not_before:
                return
            try:
                self._check_ready()
            except ValueError as error:
                if self.wait_for_ready and self.archive.profile_id == self.profile_id:
                    if self.data["reason"] != str(error):
                        self.data["reason"] = str(error)
                        self._save()
                else:
                    self._halt("paused", str(error))
                return
            if not self.run_started:
                self.run_started = moment
            if self.data["reason"] != "正在读取历史战报":
                self.data["reason"] = "正在读取历史战报"
                self._save()
            latest = self.continuous and self.data["cursor"] > 0 and (
                self.data.get("latest_cursor", 0) > 0 or moment >= self.latest_due)
            # A long catch-up must not hold the newest page hostage. Keep every
            # interrupted gap as a separate persisted range; never move the
            # original cursor/highwater over reports we have not received.
            fresh = None
            urgent = self.continuous and self.urgent_latest
            if urgent:
                fresh = 'head'
            elif self.continuous and (self.data.get('latest_cursor') or self.data['fresh_ranges']):
                if moment >= self.fresh_due:
                    fresh = 'head'
                elif self.data['fresh_ranges'] and self.fresh_reads < 5:
                    fresh = 'gap'
            if not fresh and not latest and moment < max(self.history_not_before, self.priority_until):
                return
            if not urgent and moment < self.priority_until:
                return
            if self.continuous and self.data["available_end"] and not latest and not fresh:
                return
            cursor = (0 if fresh == 'head' else self.data['fresh_ranges'][-1]['cursor'] if fresh == 'gap'
                      else self.data.get("latest_cursor", 0) if latest else self.data["cursor"])
            self.fresh_reads = self.fresh_reads + 1 if fresh else 0
            pending = {"cursor": cursor, "body": union_report_request(cursor), "sent_at": time.time(),
                "deadline": moment + self.RESPONSE_TIMEOUT, "observed": False, "ack": False,
                "latest": latest, 'fresh': fresh, 'urgent':urgent}
            self.pending = pending
            if urgent:
                self.urgent_latest = False
        try:
            # Do not hold the lock during transport: capture callbacks can arrive before its acknowledgement.
            self.bridge.fetch_union_page(cursor)
            with self.lock:
                if self.pending is pending:
                    pending["ack"] = True
        except Exception as error:
            with self.lock:
                if self.pending is pending:
                    self._halt("error", "读取请求未完成：" + str(error)[:140])

    def _fresh_result(self, pending, result, moment):
        ids, ranges = result['ids'], self.data['fresh_ranges']
        if result.get('floor_reached'):
            ranges.clear()
        if pending['fresh'] == 'head':
            previous = max(self.data['fresh_top'], self.data.get('latest_top', 0), self.data.get('latest_highwater', 0))
            if ids and max(ids) > previous:
                if previous and min(ids) > previous and not result.get('floor_reached'):
                    ranges.append({'cursor':min(ids), 'boundary':previous, 'top':max(ids)})
                self.data['fresh_top'] = max(ids)
            self.data['last_head_at'] = result['at']
            self.fresh_due = moment + 60
            if not self.data.get('latest_cursor'):
                self.latest_due = moment + 60
        elif ranges:
            if not ids or min(ids) <= ranges[-1]['boundary']:
                ranges.pop()
            else:
                ranges[-1]['cursor'] = min(ids)
        if not ranges and not self.data.get('latest_cursor'):
            self.data['latest_highwater'] = max(self.data.get('latest_highwater', 0), self.data['fresh_top'])
        self.data.update(fresh_pages=self.data['fresh_pages'] + 1,
                         new_reports=self.data['new_reports'] + result['new'],
                         received_reports=self.data['received_reports'] + len(ids))
        self._save()

    def _run(self):
        while not self.closed.is_set():
            try:
                self._step()
            except Exception as error:
                self.fail("历史读取已暂停：" + type(error).__name__)
            self.wake.wait(.2)
            self.wake.clear()

    def close(self):
        self.pause()
        self.closed.set()
        self.wake.set()
        if self.thread.is_alive():
            self.thread.join(timeout=25)
