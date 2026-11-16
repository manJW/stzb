"""Persistent dashboard-owned report collection; no GUI automation."""
import json
import threading
import time

from model import write_json


class BackgroundReports:
    DEFAULT_MAXIMUM = 40

    def __init__(self, root, archive, bridge, harvester, reader, ready, seed_baseline=None, focus_provider=None, state_provider=None):
        self.archive, self.bridge, self.harvester, self.reader, self.ready = archive, bridge, harvester, reader, ready
        self.profile_id = archive.profile_id
        self.focus_provider = focus_provider
        from report_priority import ReportPriority
        self.priority = ReportPriority(archive,harvester,reader,state_provider) if state_provider else None
        self.path = root / 'runtime' / ('background-reports-' + self.profile_id + '.json')
        self.lock = threading.RLock()
        self.closed = threading.Event()
        self.wake = threading.Event()
        self.next_retry = 0
        self.reader_retry = 0
        self.members_due=0
        self.data = {'profile_id': self.profile_id, 'enabled': True, 'since': time.time(), 'baseline': self._counts(),
                     'maximum': 40, 'reason': '等待游戏登录，随后自动补采本赛季全盟战报'}
        if self.path.exists():
            saved = json.loads(self.path.read_text(encoding='utf8'))
            if saved.get('profile_id') == self.profile_id:
                self.data.update(saved)
        # The collector may still be importing the bundled archive. Imported
        # history is the starting baseline, never new live collection.
        if seed_baseline:
            self.data['baseline'] = {table:max(self.data['baseline'][table], seed_baseline.get(table,0))
                                     for table in ('battle_reports','report_bodies')}
        if self.data.get('format') != 2:
            # Older PC/UI jobs do not establish continuous coverage between
            # their historical cursor and today's newest reports. Re-scan that
            # gap once, retaining the old checkpoint and deduplicating the DB.
            with self.harvester.lock:
                self.data['previous_checkpoint'] = dict(self.harvester.data)
                self.harvester.data.update(status='paused', cursor=0, pages=0, new_reports=0,
                    received_reports=0, run_pages=0, available_end=False, latest_cursor=0,
                    latest_top=0, latest_highwater=0, latest_pages=0,
                    reason='后台通道首次接入，从最新页核查全盟可访问历史')
                self.harvester._save()
            self.data['format'] = 2
        self.harvester.continuous = True
        self.thread = threading.Thread(target=self._run, name='background-alliance-reports', daemon=True)

    def _counts(self):
        with self.archive.lock:
            return {table: self.archive.db.execute(f'SELECT COUNT(*) FROM {table} WHERE profile_id=?',
                    (self.profile_id,)).fetchone()[0] for table in ('battle_reports', 'report_bodies')}

    def _save(self):
        self.data['updated_at'] = time.time()
        write_json(self.path, self.data)

    def restore(self):
        with self.lock:
            self._save()
        if not self.thread.is_alive():
            self.thread.start()
        self.wake.set()

    def snapshot(self):
        with self.lock:
            data = dict(self.data)
        counts = self._counts()
        history = self.harvester.snapshot()
        reader = self.reader.snapshot()
        with self.archive.lock:
            members=self.archive.db.execute('SELECT COUNT(*),MAX(observed_at) FROM attendance_roster WHERE profile_id=? AND union_id=?',
                (self.profile_id,str(self.archive.profile['union_id']))).fetchone()
        member_status=dict(data.get('members')or{})
        if members[1]and members[1]>=member_status.get('requested_at',0):
            member_status.update(status='ready',count=members[0],observed_at=members[1])
        from role_context import context
        role=context(self.archive.profile)
        return {**data, 'mode': 'background', 'available': True, 'active': data['enabled'],
                'new_reports': max(0, counts['battle_reports'] - data['baseline']['battle_reports']),
                'saved': max(0, counts['report_bodies'] - data['baseline']['report_bodies']),
                'counts': counts, 'pages': history['pages'], 'cursor': history['cursor'],
                'history': history, 'reader': reader, 'bridge': self.bridge.snapshot(),
                'alliance_reports': reader['alliance_reports'],
                'priority': self.priority.snapshot() if self.priority else None,
                'members':member_status,
                'scope': '当前已核对角色、本赛季、'+role['report_scope'],
                'automatic_full_text':False,'detail_mode':'structured_fields'}

    def start(self, maximum=40):
        if type(maximum) is not int or not 1 <= maximum <= 200:
            raise ValueError('每轮补全请求数须为1～200')
        with self.lock:
            # Resuming never discards the historical cursor or existing baseline.
            self.data.update(enabled=True, maximum=maximum, reason='后台自动补采已开启；已保存进度继续使用')
            self.next_retry = 0
            self._save()
        self.wake.set()
        return self.snapshot()

    def pause(self):
        with self.lock:
            self.data.update(enabled=False, reason='已暂停后台补采；记录和翻页进度已保存')
            self._save()
        self.harvester.pause()
        self.reader.pause()
        return self.snapshot()

    def _step(self):
        with self.lock:
            self._tick()

    def _tick(self):
        with self.lock:
            if not self.data['enabled'] or self.closed.is_set():
                return
        if self.archive.profile_id != self.profile_id:
            self.pause()
            return
        reason = self.ready()
        if reason:
            with self.lock:
                self.data['reason'] = reason
            return
        if self.focus_provider:
            self.reader.prioritize_army(self.focus_provider())
        if self.priority:
            self.priority.step()
        moment = time.monotonic()
        if str(self.archive.profile['union_id'])in ('','0'):
            # Union zero is not a group. Keep filling observed personal report
            # indices, but never send a fake "alliance zero" history request.
            with self.harvester.lock:running=self.harvester.data['status']=='running'
            if running:self.harvester.pause()
            with self.reader.lock:active=self.reader.active
            if not active and moment>=self.reader_retry:
                self.reader_retry=moment+60
                self.reader.resume(self.data['maximum'],continuous=True,wait_for_ready=True)
            self.data['reason']='个人模式：记录本人已发现战报并补齐队伍资料，不汇总其他无组织玩家'
            self.data['members']={'status':'not_applicable','count':0}
            return
        member_status=self.data.get('members')or{}
        if member_status.get('status')=='requested':
            with self.archive.lock:
                count,observed=self.archive.db.execute('SELECT COUNT(*),MAX(observed_at) FROM attendance_roster WHERE profile_id=? AND union_id=?',
                    (self.profile_id,str(self.archive.profile['union_id']))).fetchone()
            if observed and observed>=member_status['requested_at']:
                self.data['members']={**member_status,'status':'ready','count':count,'observed_at':observed}
            elif time.time()-member_status['requested_at']>=30:
                self.members_due=min(self.members_due,moment)
        if hasattr(self.bridge,'fetch_members')and moment>=self.members_due:
            self.members_due=moment+600
            requested_at=time.time()
            try:
                result=self.bridge.fetch_members()
                self.data['members']={'status':'requested','requested_at':requested_at,'command':result['command']}
            except Exception as error:
                self.members_due=moment+30
                self.data['members']={'status':'waiting','reason':str(error)[:160]}
        with self.harvester.lock:
            status = self.harvester.data['status']
        if status != 'running' and moment >= self.next_retry:
            self.harvester.start({'max_pages': 100, 'max_minutes': 20, 'wait_for_ready': True},
                                 resume=status in ('paused', 'error', 'available_end'))
            self.next_retry = moment + 60
        with self.reader.lock:
            active = self.reader.active
        if not active and moment >= self.reader_retry:
            self.reader_retry = moment + 60
            self.reader.resume(self.data['maximum'], continuous=True, wait_for_ready=True)
        with self.lock:
            self.data['reason'] = '后台自动采集：检查最新战报、历史翻页，优先补齐双方主力资料；不单独补采正文'

    def _run(self):
        while not self.closed.is_set():
            try:
                self._step()
            except Exception as error:
                self.next_retry = time.monotonic() + 60
                with self.lock:
                    self.data['reason'] = '后台连接暂未就绪，60秒后自动重试：' + str(error)[:140]
            self.wake.wait(1)
            self.wake.clear()

    def close(self):
        # A service restart preserves the user's enabled setting.
        self.closed.set()
        self.wake.set()
        if self.thread.is_alive():
            self.thread.join(timeout=5)
        self.harvester.pause()
        self.reader.pause()
        with self.lock:
            self._save()
