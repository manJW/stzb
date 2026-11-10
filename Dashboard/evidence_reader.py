"""Bounded enrichment of received battle IDs; no ID enumeration or game orders."""
import threading
import time


class ReportEvidenceReader:
    RESPONSE_TIMEOUT = 25
    INTERVAL = 2.5

    def __init__(self, archive, bridge, ready, autostart=True):
        self.archive, self.bridge, self.ready = archive, bridge, ready
        self.profile_id = archive.profile_id
        self.lock = threading.RLock()
        self.closed = threading.Event()
        self.wake = threading.Event()
        self.active = False
        self.waiting = False
        self.requests = 0
        self.maximum = 30
        self.failures = 0
        self.profile_blocked = False
        self.profile_blocked_until = 0
        self.continuous = False
        self.total_requests = 0
        self.not_before = 0
        self.reason = "等待启用战报补全"
        with archive.lock, archive.db:
            archive.db.execute("UPDATE report_read_jobs SET state='queued' WHERE profile_id=? AND state IN ('reading','paused')", (self.profile_id,))
            archive.intelligence.seed_alliance_details()
        self.thread = threading.Thread(target=self._run, daemon=True, name="report-evidence-reader")
        if autostart:
            self.thread.start()

    def snapshot(self):
        with self.lock:
            result = {"active": self.active, "waiting_response": self.waiting, "requests": self.requests,
                "max_requests": self.maximum, "reason": self.reason, "profile_temporarily_blocked": self.profile_blocked,
                "continuous": self.continuous, "total_requests": self.total_requests}
        with self.archive.lock:
            result["jobs"] = [dict(r) for r in self.archive.db.execute('''SELECT kind,state,COUNT(*) AS count
                FROM report_read_jobs WHERE profile_id=? GROUP BY kind,state''', (self.profile_id,))]
            result["alliance_reports"] = self.archive.intelligence.alliance_report_coverage()
        return result

    def resume(self, maximum=30, continuous=False, wait_for_ready=False):
        if type(maximum) is not int or not 1 <= maximum <= 200:
            raise ValueError("本轮补全请求数须为1～200")
        if type(continuous) is not bool or type(wait_for_ready) is not bool:
            raise ValueError("持续补全设置须为布尔值")
        reason = self.ready()
        if self.archive.profile_id != self.profile_id or (reason and not (continuous and wait_for_ready)):
            raise ValueError(reason or "当前档案与战报读取绑定不一致")
        with self.lock:
            if self.active:
                raise ValueError("战报补全正在进行")
            self.active, self.requests, self.maximum, self.failures = True, 0, maximum, 0
            self.profile_blocked = False
            self.continuous = continuous
            self.reason = reason or "补全地图已观测战报摘要与本盟战报全文"
        self.wake.set()
        return self.snapshot()

    def pause(self, reason="已暂停；当前请求返回的资料仍会正常归档"):
        with self.lock:
            self.active, self.reason, self.continuous = False, reason, False
        return self.snapshot()

    def enqueue(self, ids):
        if not isinstance(ids, list) or not 1 <= len(ids) <= 100 or any(type(x) is not int for x in ids):
            raise ValueError("请选择1～100个已观测战报")
        # Validate the complete selection before adding any job.
        with self.archive.lock, self.archive.db:
            for bid in ids:
                self.archive.intelligence.queue_evidence("detail", bid, retry=True)
        with self.lock:
            active = self.active
        if not active:
            return self.resume()
        self.wake.set()
        return self.snapshot()

    def _received(self, kind, bid):
        table = "battle_reports" if kind == "profile" else "report_bodies"
        return self.archive.db.execute(f"SELECT 1 FROM {table} WHERE profile_id=? AND battle_id=?",
            (self.profile_id, str(bid))).fetchone() is not None

    def _step(self):
        with self.lock:
            if not self.active:
                return
            if time.monotonic() < self.not_before:
                return
            if self.requests >= self.maximum:
                if self.continuous:
                    self.requests = 0
                    self.not_before = time.monotonic() + 60
                    self.reason = "持续补全冷却60秒，随后自动继续"
                    return
                self.active, self.reason = False, "达到本轮补全上限，队列已保留，可继续补全"
                return
        reason = self.ready()
        if self.archive.profile_id != self.profile_id:
            self.pause("档案绑定已改变，已暂停战报补全")
            return
        if reason:
            with self.lock:
                if self.continuous:
                    self.reason = reason
                else:
                    self.active, self.reason = False, reason
            return
        with self.lock:
            if self.profile_blocked and time.monotonic() >= self.profile_blocked_until:
                self.profile_blocked = False
            if self.continuous and not self.profile_blocked:
                self.reason = "持续跟随新战报编号，自动补全摘要与全文"
        with self.archive.lock, self.archive.db:
            rows = self.archive.db.execute('''SELECT * FROM report_read_jobs WHERE profile_id=? AND state='queued'
                AND (?=0 OR kind='detail') ORDER BY priority DESC,CASE kind WHEN 'profile' THEN 0 ELSE 1 END,
                CASE kind WHEN 'profile' THEN -queued_at ELSE queued_at END,battle_id LIMIT 15''',
                (self.profile_id, int(self.profile_blocked))).fetchall()
            if not rows:
                if self.continuous:
                    with self.lock:
                        self.reason = "持续监听新战报编号；暂缓的摘要稍后自动尝试" if self.profile_blocked else "持续监听新战报编号，当前可处理队列已完成"
                else:
                    self.pause("当前补全队列已处理完；新观测到的编号会继续入队")
                return
            kind = rows[0]["kind"]
            jobs = [r for r in rows if r["kind"] == kind][:15 if kind == "profile" else 1]
            jobs = [r for r in jobs if not self._received(kind, r["battle_id"])]
            # Existing replies can satisfy a queued request before this worker reaches it.
            for r in rows:
                if self._received(r["kind"], r["battle_id"]):
                    self.archive.db.execute("UPDATE report_read_jobs SET state='done',updated_at=? WHERE profile_id=? AND kind=? AND battle_id=?",
                        (time.time(), self.profile_id, r["kind"], r["battle_id"]))
            for r in jobs:
                self.archive.db.execute("UPDATE report_read_jobs SET state='reading',attempts=attempts+1,updated_at=? WHERE profile_id=? AND kind=? AND battle_id=?",
                    (time.time(), self.profile_id, kind, r["battle_id"]))
        if not jobs:
            return
        with self.lock:
            self.requests += 1
            self.total_requests += 1
            self.waiting = True
        error = None
        try:
            if kind == "profile":
                self.bridge.fetch_profiles([int(r["battle_id"]) for r in jobs])
            else:
                self.bridge.fetch_battle_detail(int(jobs[0]["battle_id"]))
            deadline = time.monotonic() + self.RESPONSE_TIMEOUT
            while not self.closed.is_set():
                with self.archive.lock:
                    if all(self._received(kind, r["battle_id"]) for r in jobs):
                        break
                if time.monotonic() >= deadline:
                    error = "客户端未返回相应资料；可能无访问权限或记录已过期，未推断任何缺失数据"
                    break
                self.closed.wait(.2)
        except Exception as exc:
            error = "读取未完成：" + str(exc)[:140]
        saved = 0
        with self.archive.lock, self.archive.db:
            for r in jobs:
                received = self._received(kind, r["battle_id"])
                saved += bool(received)
                state = "done" if received else "paused" if self.closed.is_set() else "failed"
                self.archive.db.execute("UPDATE report_read_jobs SET state=?,last_error=?,updated_at=? WHERE profile_id=? AND kind=? AND battle_id=?",
                    (state, None if received else error, time.time(), self.profile_id, kind, r["battle_id"]))
        with self.lock:
            self.waiting = False
            self.not_before = time.monotonic() + self.INTERVAL
            self.failures = 0 if saved else self.failures + 1
            if self.failures >= 2:
                if kind == "profile":
                    self.profile_blocked, self.failures = True, 0
                    self.profile_blocked_until = time.monotonic() + 300
                    self.reason = "部分摘要未返回，已暂缓摘要补取，继续补已知战报全文"
                else:
                    if self.continuous:
                        self.failures = 0
                        self.not_before = time.monotonic() + 60
                        self.reason = "部分全文未返回，冷却60秒后继续其他已观测战报"
                    else:
                        self.active, self.reason = False, "连续两次未获得全文，已暂停补全；原始观测继续保留"

    def _run(self):
        while not self.closed.is_set():
            try:
                self._step()
            except Exception as exc:
                self.pause("战报补全暂停：" + type(exc).__name__)
            self.wake.wait(self.INTERVAL)
            self.wake.clear()

    def close(self):
        self.pause()
        self.closed.set()
        self.wake.set()
        if self.thread.is_alive():
            self.thread.join(timeout=25)
