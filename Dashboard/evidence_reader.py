"""Bounded enrichment of received battle IDs; no ID enumeration or game orders."""
import threading
import time
import json

from intelligence import MAIN_DETAIL_PRIORITY, report_references
from observation_codec import map_troop_codes, report_troop_codes, dictionaries
from report_access import rejection, request_ids

URGENT_DETAIL_PRIORITY = 200


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
        self.structured_requests = self.profile_requests = 0
        self.not_before = 0
        self.budget_cooldown = False
        self.last_completed = 0
        self.retry_at = 0
        self.priority_detail_reads = 0
        self.reason = "等待启用战报补全"
        self.focus_checked = (None, 0)
        self.focus_status = None
        self.pending_read = None
        self.server_errors = []
        self.scope_check_at = 0
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
                "continuous": self.continuous, "total_requests": self.total_requests,
                "selected_army": self.focus_status,"automatic_full_text":False,"detail_mode":"structured_fields",
                "automatic_requests":{"profiles":self.profile_requests,"structured_details":self.structured_requests,"numeric_body":0},
                "queue_reconciliation":dict(getattr(self.archive.intelligence,'detail_queue_reconciliation',{}))}
            result['server_errors'] = list(self.server_errors[-12:])
        with self.archive.lock:
            result['access'] = self.archive.intelligence.report_access.snapshot()
            result["jobs"] = [dict(r) for r in self.archive.db.execute('''SELECT kind,state,COUNT(*) AS count
                FROM report_read_jobs WHERE profile_id=? GROUP BY kind,state''', (self.profile_id,))]
            result["alliance_reports"] = self.archive.intelligence.alliance_report_coverage()
        return result

    def on_request(self, mid, value, observed_at):
        if mid != 10:
            return
        ids = request_ids(value)
        with self.lock:
            pending = self.pending_read
            if not pending or observed_at < pending['created_at']:
                return
            if ids == pending['ids']:
                pending['wire_at'] = observed_at
            elif pending['wire_at'] is not None:
                pending['interleaved'] = True

    def on_gap(self):
        with self.lock:
            if self.pending_read:
                self.pending_read['interleaved'] = True

    def on_message(self, mid, value, observed_at):
        negative = rejection(value) if mid == 90002 else None
        with self.lock:
            pending = self.pending_read
            correlated = bool(pending and pending['wire_at'] is not None and
                observed_at >= pending['wire_at'] and not pending['interleaved'] and
                pending['scope'] == self.archive.intelligence.report_access.scope)
            if negative:
                self.server_errors.append({'at':observed_at,'status':negative[0],'text':negative[1],
                    'battle_ids':list(pending['ids']) if correlated else [],'correlated':correlated})
                self.server_errors = self.server_errors[-40:]
                if correlated and not pending['reply']:
                    pending['negative'] = (*negative,observed_at)
                    pending['wake'].set()
            elif mid == 10 and pending:
                if any(str(r.get('battle_id')) in pending['ids'] for r in dictionaries(value)):
                    pending['reply'] = True
                    pending['negative'] = None
                    pending['wake'].set()

    def scope_refresh_due(self):
        if time.monotonic() < self.scope_check_at:
            return False
        self.scope_check_at = time.monotonic() + 15
        with self.archive.lock:
            return bool(self.archive.db.execute('''SELECT 1 FROM report_read_jobs WHERE profile_id=?
                AND state='awaiting_scope' AND priority>=120 LIMIT 1''',(self.profile_id,)).fetchone())

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
            self.reason = reason or "补全已观测战报的队伍资料与交战结果"
        self.wake.set()
        return self.snapshot()

    def pause(self, reason="已暂停；当前请求返回的资料仍会正常归档"):
        with self.lock:
            self.active, self.reason, self.continuous = False, reason, False
        return self.snapshot()

    def enqueue(self, ids, verify_access=False):
        if not isinstance(ids, list) or not 1 <= len(ids) <= 100 or any(type(x) is not int for x in ids):
            raise ValueError("请选择1～100个已观测战报")
        if type(verify_access) is not bool or (verify_access and len(ids)!=1):
            raise ValueError('查询范围核对每次只允许一个已观测编号')
        if verify_access and (self.ready() or self.archive.profile_id!=self.profile_id):
            raise ValueError('当前角色和查询范围尚未核对，不执行单条诊断')
        # Validate the complete selection before adding any job.
        with self.archive.lock, self.archive.db:
            for bid in ids:
                self.archive.intelligence.queue_evidence("detail", bid, retry=True)
                if verify_access and not self._received('detail',bid):
                    self.archive.intelligence.report_access.prepare_probe(bid)
        with self.lock:
            active = self.active
        if not active:
            return self.resume()
        self.wake.set()
        return self.snapshot()

    def prioritize_army(self, army_id, *, urgent=False, current=None):
        """Fetch known evidence for the selected army; fetching never establishes identity."""
        if not isinstance(army_id, str) or not army_id.isdecimal():
            return
        moment = time.monotonic()
        with self.lock:
            if not urgent and self.focus_checked[0] == army_id and moment - self.focus_checked[1] < 15:
                return
            self.focus_checked = (army_id, moment)
        if urgent and current is None:
            with self.archive.read_snapshot(timeout=3) as db:
                current = db.query('army_lineups',army_id=army_id)
        with self.archive.lock, self.archive.db:
            owner = self.archive.db.execute('''SELECT s.data,s.hero_type,p.name,p.union_id
                FROM live_army_current a JOIN live_army_states s ON s.id=a.state_ref
                JOIN players p ON p.profile_id=a.profile_id AND p.player_key='world:'||s.owner_uid
                WHERE a.profile_id=? AND a.army_id=?''', (self.profile_id, army_id)).fetchone()
            if not owner:
                return
            ids = set()
            profile_priority = URGENT_DETAIL_PRIORITY + 10 if urgent else 130
            detail_priority = URGENT_DETAIL_PRIORITY if urgent else 120
            for ref in report_references(json.loads(owner['data'])):
                bid = str(ref[0])
                self.archive.intelligence.queue_evidence('profile', bid, retry=True, priority=profile_priority)
                self.archive.intelligence.queue_evidence('detail', bid, retry=True, priority=detail_priority)
                ids.add(bid)
            if urgent:
                # An incomplete latest report may omit troop positions; still
                # fetch that exact report instead of resurrecting an older build.
                bid = current.get('current_report_id')
                if bid:
                    self.archive.intelligence.queue_evidence('detail', bid, retry=True, priority=detail_priority)
                    ids.add(bid)
            codes = map_troop_codes(owner['hero_type'])
            rows = self.archive.db.execute('''SELECT battle_id,side,
                json_extract(features,'$.'||side||'_hero_type') AS troop_raw
                FROM combat_sides INDEXED BY combat_player
                WHERE profile_id=? AND union_id=? AND player_name=?
                ORDER BY event_at DESC,CAST(battle_id AS INTEGER) DESC LIMIT 30''',
                (self.profile_id, owner['union_id'], owner['name'])).fetchall()
            matching = []
            for row in rows:
                if codes and report_troop_codes(row['troop_raw'], row['side']) == codes:
                    bid = row['battle_id']
                    if bid not in matching:
                        matching.append(bid)
                    if len(matching) == 3:
                        break
            for bid in matching:
                self.archive.intelligence.queue_evidence('detail', bid, retry=True, priority=detail_priority)
                ids.add(bid)
        with self.lock:
            self.focus_status = {'army_id':army_id, 'player_name':owner['name'],
                                 'battle_ids':sorted(ids, key=int), 'checked_at':time.time()}
            if urgent and self.budget_cooldown:
                self.not_before = min(self.not_before, self.last_completed + self.INTERVAL)
                self.budget_cooldown = False
        self.wake.set()

    def _received(self, kind, bid):
        if kind=='detail':return self.archive.intelligence.detail_received(bid)
        return self.archive.db.execute("SELECT 1 FROM battle_reports WHERE profile_id=? AND battle_id=?",
            (self.profile_id, str(bid))).fetchone() is not None

    def _step(self):
        with self.lock:
            if not self.active:
                return
            if time.monotonic() < self.not_before:
                return
            self.budget_cooldown = False
            if self.requests >= self.maximum:
                if self.continuous:
                    self.requests = 0
                    self.not_before = time.monotonic() + 60
                    self.budget_cooldown = True
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
                self.reason = "持续跟随新战报，补齐队伍资料与结果；不单独补采数字正文"
        with self.archive.lock, self.archive.db:
            if self.continuous and time.monotonic() >= self.retry_at:
                # Bounded retries persist across restarts; unavailable reports do not block later jobs.
                self.archive.db.execute("""UPDATE report_read_jobs SET state='queued',last_error=NULL
                    WHERE profile_id=? AND state='failed' AND attempts<3 AND updated_at<?""",
                    (self.profile_id, time.time() - 300))
                self.retry_at = time.monotonic() + 60
            access = self.archive.intelligence.report_access
            access.defer_unlisted()
            rows = self.archive.db.execute('''SELECT * FROM report_read_jobs WHERE profile_id=? AND state='queued'
                AND (?=0 OR kind='detail') ORDER BY priority DESC,CASE kind WHEN 'profile' THEN 0 ELSE 1 END,
                CAST(battle_id AS INTEGER) DESC,queued_at LIMIT 15''',
                (self.profile_id, int(self.profile_blocked))).fetchall()
            if (self.continuous and self.priority_detail_reads >= 5
                    and not (rows and rows[0]['priority'] >= URGENT_DETAIL_PRIORITY)):
                # Preserve priority for main armies without starving other
                # battles needed for later analysis. One ordinary structured
                # detail opportunity follows every five priority attempts.
                ordinary = self.archive.db.execute('''SELECT * FROM report_read_jobs
                    WHERE profile_id=? AND state='queued' AND kind='detail' AND priority<?
                    ORDER BY CAST(battle_id AS INTEGER) DESC LIMIT 1''',
                    (self.profile_id, MAIN_DETAIL_PRIORITY)).fetchall()
                if ordinary:
                    rows = ordinary
            if not rows:
                if self.continuous:
                    with self.lock:
                        self.reason = "持续监听新战报编号；暂缓的摘要稍后自动尝试" if self.profile_blocked else "持续监听新战报编号，当前可处理队列已完成"
                else:
                    self.pause("当前补全队列已处理完；新观测到的编号会继续入队")
                return
            kind = rows[0]["kind"]
            # The server's 90002 rejection names a command, not a report ID.
            # One uncertain report per request makes a refusal attributable.
            jobs = [r for r in rows if r["kind"] == kind][:1]
            if rows[0]['priority']>=URGENT_DETAIL_PRIORITY:
                jobs=[r for r in jobs if r['priority']>=URGENT_DETAIL_PRIORITY]
            jobs = [r for r in jobs if not self._received(kind, r["battle_id"])]
            # Existing replies can satisfy a queued request before this worker reaches it.
            for r in rows:
                if self._received(r["kind"], r["battle_id"]):
                    self.archive.db.execute("UPDATE report_read_jobs SET state='done',updated_at=? WHERE profile_id=? AND kind=? AND battle_id=?",
                        (time.time(), self.profile_id, r["kind"], r["battle_id"]))
            for r in jobs:
                self.archive.db.execute("UPDATE report_read_jobs SET state='reading',attempts=attempts+1,updated_at=? WHERE profile_id=? AND kind=? AND battle_id=?",
                    (time.time(), self.profile_id, kind, r["battle_id"]))
            token = access.begin(jobs[0]['battle_id'],kind) if jobs else None
        if not jobs:
            return
        with self.lock:
            self.requests += 1
            self.total_requests += 1
            if kind=='detail':self.structured_requests+=1
            else:self.profile_requests+=1
            self.waiting = True
            pending = {'ids':[r['battle_id'] for r in jobs],'created_at':time.time(),
                'wire_at':None,'interleaved':False,'scope':access.scope,
                'negative':None,'reply':False,'wake':threading.Event()}
            self.pending_read = pending
            if kind == 'detail':
                self.priority_detail_reads = self.priority_detail_reads + 1 if jobs[0]['priority'] >= MAIN_DETAIL_PRIORITY else 0
        error = None
        try:
            # Command 10 returns structured heroes/skills/HP/results. Command
            # 11 returns only numeric turn text and is never an automatic goal.
            self.bridge.fetch_profiles([int(r["battle_id"]) for r in jobs])
            deadline = time.monotonic() + self.RESPONSE_TIMEOUT
            while not self.closed.is_set():
                with self.archive.lock:
                    if all(self._received(kind, r["battle_id"]) for r in jobs):
                        break
                if pending['negative'] or pending['reply']:
                    error = pending['negative'][1] if pending['negative'] else '已收到战报，部分队伍字段暂未提供'
                    break
                if time.monotonic() >= deadline:
                    error = "客户端未返回相应资料；可能无访问权限或记录已过期，未推断任何缺失数据"
                    break
                pending['wake'].wait(.2)
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
            if (not saved and pending['negative'] and access.scope == pending['scope']
                    and self.archive.profile_id == self.profile_id):
                status,text,at = pending['negative']
                access.reject(jobs[0]['battle_id'],status,text,at)
            outcome = 'received' if saved else pending['negative'][0] if pending['negative'] else 'partial' if pending['reply'] else 'timeout'
            access.finish(token,outcome,pending['wire_at'],error)
        with self.lock:
            self.pending_read = None
            self.waiting = False
            self.last_completed = time.monotonic()
            self.not_before = time.monotonic() + self.INTERVAL
            self.failures = 0 if saved or pending['negative'] or pending['reply'] else self.failures + 1
            if pending['negative']:
                self.reason = '已停止当前账号不可读战报的自动重试；最新战报和其他队伍继续补采'
            if self.failures >= 2:
                if kind == "profile":
                    self.profile_blocked, self.failures = True, 0
                    self.profile_blocked_until = time.monotonic() + 300
                    self.reason = "部分摘要未返回，已暂缓摘要补取，继续核对已知战报资料"
                else:
                    if self.continuous:
                        self.failures = 0
                        self.reason = "未返回的单条任务已暂缓，继续最新战报和其他队伍资料"
                    else:
                        self.active, self.reason = False, "连续两次未获得队伍资料，已暂停补全；原始观测继续保留"

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
