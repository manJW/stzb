"""Role-bound report-only adapter for the verified Dashen Android runtime."""
import hashlib
import json
import re
import sys
import threading
import time
from pathlib import Path

from android_map_recovery import LocalAdb, dashen_adb_port
from android_report_transport import AdbStream, LocalTunnel, push_tool, GUEST_DIR, GUEST_BINARY, GUEST_PORT
from bridge import union_report_request


def positive_id(value):
    if type(value) is not int or not 0 < value <= 2147483647:
        raise ValueError('战报编号无效')
    return value


class AndroidReadOnlyBridge:
    INTERVAL = 2.5

    def __init__(self, root, archive, connection_provider, tools=None, identity_observer=None):
        self.root, self.archive, self.connection_provider = Path(root), archive, connection_provider
        self.assets = Path(__file__).resolve().parent / 'android_reader_assets'
        self.tools = Path(tools) if tools else Path(__file__).resolve().parent.parent / 'Tools' / 'android-report-probe-20261112'
        self.profile_id = archive.profile_id
        self.identity_observer=identity_observer
        self.lock = threading.RLock()
        self.closed = False
        self.next_request = 0
        self.device = self.session = self.script = self.inspector = self.tunnel = self.remote = None
        self.selection_script = None
        self.server_stream = None
        self.server_pid = self.game_pid = self.adb = None
        self.key = None
        self.stats = {'submitted': 0, 'connections': 0, 'last_submitted_at': 0, 'ready': False}

    @staticmethod
    def _fingerprint(connection):
        return tuple(connection.get(k) for k in ('pid', 'host_pid', 'local_port', 'peer', 'connection_started'))

    def _binding(self):
        current = json.loads((self.root / 'archive-profile.json').read_text(encoding='utf-8-sig'))
        own = json.loads((self.root / 'config.json').read_text(encoding='utf-8-sig'))['self']
        if (self.archive.profile_id != self.profile_id or not own.get('confirmed')
                or own.get('name') != current.get('account_name')
                or str(current.get('account_role_id')) != str(self.archive.profile.get('account_role_id'))
                or str(current.get('union_id')) != str(self.archive.profile.get('union_id'))
                or current.get('server') != self.archive.profile.get('server')
                or current.get('season') != self.archive.profile.get('season')):
            raise RuntimeError('当前登录角色与赛季档案不一致，后台补采已停止')
        network_user = current['account_role_id']
        try:
            identity = json.loads((self.root/'runtime'/'identity.json').read_text(encoding='utf8'))
            scope = identity.get('scope') or {}
            if (identity.get('user_id') == str(own['user_id']) and identity.get('account_role_id') == str(current['account_role_id'])
                    and all(scope.get(k) == current[k] for k in ('server','season')) and identity.get('network_user_id')):
                network_user = identity['network_user_id']
        except (OSError,ValueError):
            pass
        return [str(positive_id(int(network_user))), str(positive_id(int(own['user_id'])))]

    def _component(self, manifest_name):
        manifest = json.loads((self.assets / manifest_name).read_text(encoding='utf-8-sig'))
        name = manifest['file']
        if not re.fullmatch(r'STLab\.(InteropSelfTest|BattleReportReader|IdentityReader|MapSelectionReader)\.[a-f0-9]{12}\.dll', name):
            raise RuntimeError('后台组件名称未通过核对')
        payload = (self.assets / name).read_bytes()
        if hashlib.sha256(payload).hexdigest() != manifest['sha256']:
            raise RuntimeError('后台组件文件校验失败')
        return name, list(payload), manifest['sha256']

    def _connect(self, connection, users):
        self._disconnect()
        port = dashen_adb_port(connection)
        self.adb = LocalAdb(port, timeout=15)
        pid = self.adb.execute('shell:pidof com.netease.stzb.netease').decode('ascii').strip()
        if not re.fullmatch(r'[1-9][0-9]*', pid):
            raise RuntimeError('等待率土游戏进程和已登录角色')
        manifest = json.loads((self.tools / 'tools-manifest.json').read_text(encoding='utf8'))
        binary = self.tools / manifest['server']['file']
        digest = manifest['server']['sha256']
        if hashlib.sha256(binary.read_bytes()).hexdigest() != digest:
            raise RuntimeError('本机后台连接工具校验失败')
        self.adb.execute('shell:mkdir -p -m 700 ' + GUEST_DIR)
        existing = self.adb.execute('shell:test -f ' + GUEST_BINARY + ' && sha256sum ' + GUEST_BINARY).decode('ascii', 'replace').split()
        if existing and existing[0] != digest:
            raise RuntimeError('模拟器后台连接工具校验失败')
        if not existing:
            push_tool(port, binary)
            if self.adb.execute('shell:sha256sum ' + GUEST_BINARY).decode('ascii').split()[0] != digest:
                raise RuntimeError('模拟器后台连接工具传输校验失败')
        existing_pid = self.adb.execute('shell:pidof ' + GUEST_BINARY.rsplit('/', 1)[-1]).decode('ascii', 'replace').strip()
        reuse = existing_pid.isdigit() and self.adb.execute('shell:readlink /proc/' + existing_pid + '/exe').decode('ascii', 'replace').strip() == GUEST_BINARY
        if not reuse:
            self.server_stream = AdbStream(port, 'shell:echo $$; exec ' + GUEST_BINARY + f' -l 127.0.0.1:{GUEST_PORT}')
            self.server_pid = self.server_stream.read_some().decode('ascii', 'replace').splitlines()[0].strip()
            if not re.fullmatch(r'[1-9][0-9]*', self.server_pid):
                raise RuntimeError('本机后台连接未启动')
            time.sleep(.5)
            if self.adb.execute('shell:readlink /proc/' + self.server_pid + '/exe').decode('ascii', 'replace').strip() != GUEST_BINARY:
                raise RuntimeError('本机后台连接进程不匹配')
        sys.path.insert(0, str(self.tools / 'bindings'))
        import frida
        self.frida = frida
        self.tunnel = LocalTunnel(port)
        self.remote = frida.get_device_manager().add_remote_device('127.0.0.1:' + str(self.tunnel.port), keepalive_interval=5)
        self.session = self.remote.attach(int(pid))
        self.inspector = self.session.create_script((self.assets / 'mono_session_inspect.js').read_text(encoding='utf8'))
        self.inspector.load()
        runtime = self.inspector.exports_sync.inspect(users)
        if not runtime.get('ready') or runtime.get('networkState') != 1 or runtime.get('engineImages') != 1:
            raise RuntimeError('游戏尚未登录到已绑定角色，等待恢复')
        loader = (self.assets / 'mono_managed_loader.js').read_text(encoding='utf8')
        if not users:
            self.script = self.session.create_script(loader + '\n' + (self.assets / 'mono_identity.js').read_text(encoding='utf8'))
            self.script.load()
            self.script.exports_sync.initialize(*self._component('identity-manifest.json'))
            return
        test = self.session.create_script(loader + '\n' + (self.assets / 'mono_shared_selftest.js').read_text(encoding='utf8'))
        try:
            test.load()
            if not test.exports_sync.run(*self._component('self-test-manifest.json')).get('passed'):
                raise RuntimeError('后台连接兼容性自检失败')
        finally:
            test.unload()
        self.script = self.session.create_script(loader + '\n' + (self.assets / 'mono_background_reports.js').read_text(encoding='utf8'))
        self.script.load()
        self.script.exports_sync.initialize(*self._component('reader-manifest.json'))
        self.script.exports_sync.initialize_identity(*self._component('identity-manifest.json'))
        self.device, self.game_pid, self.key = port, pid, self._fingerprint(connection)
        self.stats.update(ready=True, connections=self.stats['connections'] + 1, game_pid=int(pid), server_id=runtime['serverId'])

    def _verify_identity(self):
        from portable_mode import validated_identity
        actual=validated_identity(self.script.exports_sync.identify())
        profile=self.archive.profile
        expected={k:profile.get(k)for k in ('account_role_id','union_id','force_type','battle_role_id')}
        mismatch=any(actual.get(k)!=v for k,v in expected.items())or any(actual['scope'].get(k)!=profile.get(k)for k in ('server','season'))
        if mismatch:
            if self.identity_observer:self.identity_observer(actual,'后台读取前自动核验身份',time.time())
            raise RuntimeError('当前角色、赛季或所属组织已变化，暂停旧范围读取并自动重新接入')
        self.stats['identity_verified_at']=time.time()

    def _submit(self, kind, value, ids=None):
        with self.lock:
            if self.closed:
                raise RuntimeError('后台读取任务已关闭')
            try:
                users = self._binding()
                connection = self.connection_provider()
                if not connection or connection.get('source') != 'dashen':
                    raise RuntimeError('等待当前角色的大神游戏连接')
                changed = self.key != self._fingerprint(connection)
                if not changed and self.adb:
                    changed = self.adb.execute('shell:pidof com.netease.stzb.netease').decode('ascii').strip() != self.game_pid
                if changed or self.script is None or self.session.is_detached:
                    self._connect(connection, users)
                runtime = self.inspector.exports_sync.inspect(users)
                if not runtime.get('ready') or runtime.get('networkState') != 1:
                    raise RuntimeError('等待已绑定角色重新登录')
                self._verify_identity()
                if self._fingerprint(self.connection_provider() or {}) != self.key:
                    raise RuntimeError('游戏连接已切换，稍后自动恢复')
                delay = self.next_request - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
                status = self.script.exports_sync.queue(users, runtime['serverId'], kind, value, ids or [])
                deadline = time.monotonic() + 14
                while status['phase'] == 'queued' and time.monotonic() < deadline:
                    time.sleep(.1)
                    status = self.script.exports_sync.status()
                if status['phase'] != 'submitted':
                    self.script.exports_sync.cancel()
                    raise RuntimeError('客户端后台读取暂未完成：' + status['phase'] + ' ' + status.get('error', '')[:160])
                self.next_request = time.monotonic() + self.INTERVAL
                self.stats.update(submitted=self.stats['submitted'] + 1, last_submitted_at=time.time(), last_command=status['command'])
                return {'command': status['command'], 'acknowledged': True, 'submitted': True}
            except Exception:
                self._disconnect()
                raise

    def fetch_union_page(self, cursor):
        if str(self.archive.profile.get('union_id','0'))in ('','0'):
            raise ValueError('当前角色无所属势力，仅补齐已发现的个人战报')
        body = union_report_request(cursor)
        if cursor > 2147483647:
            raise ValueError('当前客户端战报游标超出范围')
        return {**self._submit(2, cursor), 'body': body}

    def fetch_profiles(self, ids):
        if not isinstance(ids, list) or not 1 <= len(ids) <= 15 or len(set(ids)) != len(ids):
            raise ValueError('一次只能读取1～15个不同的已观测战报')
        for bid in ids:
            positive_id(bid)
        return self._submit(0, 0, ids)

    def fetch_members(self):
        if str(self.archive.profile.get('union_id','0'))in ('','0'):
            raise ValueError('当前角色无所属组织，不读取成员名单')
        return {**self._submit(3,0),'body':[]}

    def fetch_battle_detail(self, bid):
        return self._submit(1, positive_id(bid))

    def snapshot(self):
        # Called by the UI without blocking behind transport setup.
        return dict(self.stats)

    def read_map_selection(self):
        # Report reads keep priority; selecting an army never queues a game request.
        if not self.lock.acquire(blocking=False):
            return None
        try:
            if self.closed:
                return None
            connection = self.connection_provider()
            if not connection:
                return None
            users = self._binding()
            if not self.script or self.key != self._fingerprint(connection):
                self._connect(connection, users)
            if not self.selection_script:
                source = (self.assets/'mono_managed_loader.js').read_text(encoding='utf8')
                source += '\n' + (self.assets/'mono_map_selection.js').read_text(encoding='utf8')
                self.selection_script = self.session.create_script(source)
                try:
                    self.selection_script.load()
                    runtime = self.inspector.exports_sync.inspect(users)
                    self.selection_script.exports_sync.initialize(*self._component('map-selection-manifest.json'),
                                                                 users, runtime['serverId'])
                    self.stats['map_selection_ready'] = True
                except Exception:
                    self.selection_script.unload()
                    self.selection_script = None
                    raise
            result = self.selection_script.exports_sync.read()
            if connection != self.connection_provider():
                return None
            return result
        except Exception:
            if self.selection_script:
                try:
                    self.selection_script.exports_sync.cancel()
                    self.selection_script.unload()
                except Exception:
                    pass
                self.selection_script = None
            raise
        finally:
            self.lock.release()

    def _disconnect(self):
        if self.selection_script:
            try:
                self.selection_script.exports_sync.cancel()
            except Exception:
                pass
        if self.script:
            try:
                self.script.exports_sync.cancel()
            except Exception:
                pass
        for item in (self.selection_script, self.script, self.inspector):
            if item:
                try:
                    item.unload()
                except Exception:
                    pass
        if self.session:
            try:
                self.session.detach()
            except Exception:
                pass
        if self.remote and self.tunnel:
            try:
                self.frida.get_device_manager().remove_remote_device('127.0.0.1:' + str(self.tunnel.port))
            except Exception:
                pass
        if self.tunnel:
            self.tunnel.close()
        if self.adb and self.server_pid and re.fullmatch(r'[1-9][0-9]*', self.server_pid):
            try:
                if self.adb.execute('shell:readlink /proc/' + self.server_pid + '/exe').decode('ascii', 'replace').strip() == GUEST_BINARY:
                    self.adb.execute('shell:kill -TERM ' + self.server_pid)
            except Exception:
                pass
        if self.server_stream:
            self.server_stream.close()
        self.script = self.inspector = self.session = self.remote = self.tunnel = self.server_stream = None
        self.selection_script = None
        self.key = self.server_pid = None
        self.stats['ready'] = False
        self.stats['map_selection_ready'] = False

    def close(self):
        with self.lock:
            self.closed = True
            self._disconnect()


def inspect_logged_in_identity(root, connection, tools=None):
    from types import SimpleNamespace
    bridge = AndroidReadOnlyBridge(root, SimpleNamespace(profile_id='identity-inspection'), lambda: connection, tools=tools)
    try:
        bridge._connect(connection, [])
        return bridge.script.exports_sync.identify()
    finally:
        bridge._disconnect()
