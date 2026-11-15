import json
import shutil
import struct
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from android_map_recovery import AndroidMapSession, LocalAdb, MapRecovery, dashen_adb_port


def packet(command, a=0, b=0, body=b''):
    code = struct.unpack('<I', command)[0]
    return struct.pack('<6I', code, a, b, len(body), sum(body), code ^ 0xffffffff) + body


class SocketFixture:
    def __init__(self, response):
        self.response, self.sent = bytearray(response), []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def settimeout(self, value):
        pass

    def recv(self, length):
        # Deliberately fragment protocol reads.
        length = min(length, 7)
        part = self.response[:length]
        del self.response[:length]
        return bytes(part)

    def sendall(self, value):
        self.sent.append(value)


class RecoveryTests(unittest.TestCase):
    def connection(self):
        return {'source': 'dashen', 'pid': 7, 'host_pid': 8, 'peer': 'game', 'local_port': 123,
                'connection_started': 100, 'executable': r'D:\DSemulator\GLClient\GLEmulator.exe'}

    def session(self, *, name='时运丨天远', package='com.netease.stzb.netease', fields=None):
        self.commands = []
        device = SimpleNamespace(foreground_package=lambda: package,
                                 screenshot=lambda: Image.new('RGB', (1920, 1080)),
                                 execute=lambda command: self.commands.append(command))
        ocr = SimpleNamespace(read=lambda image: [{'t': name, 'y': 2}])
        session = AndroidMapSession(self.connection(), '时运丨天远', lambda c: True, device=device, ocr=ocr)
        session.ui = SimpleNamespace(find=lambda image, name: (10, 20, 1) if name == 'search' else (70, 80, 1),
                                     read_field=lambda image, jump, offset: '987' if offset == 271 else '924')
        session.ui.wait_for = lambda game, name, error: (game.capture(), session.ui.find(None, name))
        if fields is not None:
            values = iter(fields)
            session.ui.read_field = lambda *args: next(values)
        return session

    def test_adb_reads_fragmented_frames_and_acknowledges_only_its_stream(self):
        sock = SocketFixture(packet(b'CNXN') + packet(b'OKAY', 9, 1) +
                             packet(b'WRTE', 9, 1, b'hello') + packet(b'CLSE', 9, 1))
        with patch('android_map_recovery.socket.create_connection', return_value=sock) as connect:
            self.assertEqual(LocalAdb(12345).execute('shell:wm size'), b'hello')
        self.assertEqual(connect.call_args.args[0], ('127.0.0.1', 12345))
        self.assertEqual([value[:4] for value in sock.sent], [b'CNXN', b'OPEN', b'OKAY', b'CLSE'])

    def test_adb_does_not_authenticate_or_execute_without_existing_access(self):
        sock = SocketFixture(packet(b'AUTH', 1, 0, b'challenge'))
        with patch('android_map_recovery.socket.create_connection', return_value=sock):
            with self.assertRaisesRegex(RuntimeError, '尚未授权'):
                LocalAdb(12345).execute('shell:wm size')
        self.assertEqual([value[:4] for value in sock.sent], [b'CNXN'])

    def test_refresh_uses_game_prefilled_coordinates_without_typing_or_distant_jump(self):
        self.assertEqual(self.session().refresh(), [987, 924])
        self.assertEqual(self.commands, ['shell:input tap 10 20', 'shell:input tap 70 80'])

    def test_dashen_embedding_container_requires_game_process_and_same_role(self):
        session=self.session(package='com.netease.gl')
        def execute(command):
            if command=='shell:pidof com.netease.stzb.netease':return b'321'
            self.commands.append(command)
        session.device.execute=execute
        self.assertEqual(session.refresh(),[987,924])
        self.assertEqual(self.commands,['shell:input tap 10 20','shell:input tap 70 80'])
        session.expected_name='另一个账号';self.commands=[]
        with self.assertRaisesRegex(RuntimeError,'角色名称'):session.refresh()
        self.assertEqual(self.commands,[])
        session.device.execute=lambda _:b''
        with self.assertRaisesRegex(RuntimeError,'等待率土'):session.capture()

    def test_other_role_or_application_is_not_touched(self):
        for kwargs in ({'name': '其他角色'}, {'package': 'com.example.other'}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(RuntimeError):
                    self.session(**kwargs).refresh()
                self.assertEqual(self.commands, [])

    def test_missing_or_modified_coordinates_close_only_the_opened_panel(self):
        for fields in ([None, '924'], ['987', '924', '988', '924']):
            with self.subTest(fields=fields):
                with self.assertRaises(RuntimeError):
                    self.session(fields=fields).refresh()
                self.assertEqual(self.commands, ['shell:input tap 10 20'])

    def test_cleanup_uses_visible_panel_close_and_never_android_back(self):
        session=self.session();session.opened=True
        image=Image.new('RGB',(1920,1080));image.paste((170,40,50),(1850,36,1863,49))
        session.device.screenshot=lambda:image
        session.close_panel()
        self.assertEqual(self.commands,['shell:input tap 1856 42'])
        session.opened=False;self.commands=[];session.close_panel();self.assertEqual(self.commands,[])

    def state(self):
        return SimpleNamespace(lock=threading.RLock(), config={'self': {'confirmed': True, 'name': '时运丨天远'}},
                               health={'status': 'live', 'last_packet_at': time.time(), 'resync_required': True,
                                       'connection': self.connection(), 'last_full_scene_at': 0})

    def test_old_full_scene_cannot_be_reported_as_recovered(self):
        state = self.state()
        factory = lambda *a, **k: SimpleNamespace(refresh=lambda: [987, 924])
        recovery = MapRecovery(state, factory)
        state.health['resync_required'] = False
        with patch('android_map_recovery.WindowsOcr'), patch('android_map_recovery.time.monotonic', side_effect=[0, 0, 9]):
            with patch.object(recovery.stop_event, 'wait', return_value=False):
                with self.assertRaisesRegex(RuntimeError, '等待客户端完整地图'):
                    recovery.recover(self.connection(), '时运丨天远')
        self.assertNotEqual(state.health['map_recovery']['status'], 'ready')

    def test_new_full_scene_completes_recovery_and_records_current_position(self):
        state = self.state()

        def refresh():
            state.health.update(resync_required=False, last_full_scene_at=time.time() + 1)
            return [987, 924]

        recovery = MapRecovery(state, lambda *a, **k: SimpleNamespace(refresh=refresh))
        with patch('android_map_recovery.WindowsOcr'):
            self.assertTrue(recovery.recover(self.connection(), '时运丨天远'))
        self.assertEqual(state.health['map_recovery']['status'], 'ready')
        self.assertEqual(state.health['map_recovery']['coord'], [987, 924])

    def test_pending_requires_same_live_source_and_confirmed_role(self):
        state = self.state()
        recovery = MapRecovery(state)
        self.assertIsNotNone(recovery.pending())
        self.assertFalse(recovery.current_connection({**self.connection(), 'pid': 99}))
        state.health['scope_pending'] = True
        self.assertIsNone(recovery.pending())
        state.health['scope_pending'] = False
        state.config['self']['confirmed'] = False
        self.assertIsNone(recovery.pending())

    def test_connection_loss_cannot_keep_a_previous_ready_status(self):
        state = self.state()
        recovery = MapRecovery(state)
        recovery._update(status='ready', last_success_at=100)
        state.health.update(status='reconnecting', resync_required=True)
        recovery.observe_idle()
        self.assertEqual(state.health['map_recovery']['status'], 'waiting_for_connection')
        self.assertEqual(state.health['map_recovery']['last_success_at'], 100)

    def test_adb_port_must_belong_to_the_current_vm_process(self):
        parent = ROOT / 'runtime' / 'map-recovery-tests'
        parent.mkdir(exist_ok=True)
        folder = Path(tempfile.mkdtemp(prefix='case-', dir=parent)).resolve()
        try:
            (folder / 'vms').mkdir()
            (folder / 'vms/GLEmulator.vbox').write_text('<VirtualBox><Forwarding hostport="12345" guestport="5555"/></VirtualBox>', encoding='utf-8')
            connection = {**self.connection(), 'executable': str(folder / 'GLClient/GLEmulator.exe')}
            with patch('android_map_recovery.subprocess.run', return_value=SimpleNamespace(stdout='7')):
                self.assertEqual(dashen_adb_port(connection), 12345)
            with patch('android_map_recovery.subprocess.run', return_value=SimpleNamespace(stdout='99')):
                with self.assertRaisesRegex(RuntimeError, '进程不一致'):
                    dashen_adb_port(connection)
        finally:
            if folder.parent != parent.resolve() or not folder.name.startswith('case-'):
                raise RuntimeError('Unexpected cleanup path')
            shutil.rmtree(folder)


if __name__ == '__main__':
    unittest.main()
