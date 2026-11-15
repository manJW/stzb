"""Local, source-bound Android map observations and recovery controls."""
import io
import os
import re
import socket
import struct
import subprocess
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from PIL import Image, ImageOps

from game_locator import CoordinateControls, WindowsOcr


class LocalAdb:
    """Small ADB transport client for the emulator's existing loopback port."""
    def __init__(self, port, timeout=5):
        self.port, self.timeout = int(port), timeout

    @staticmethod
    def _send(sock, command, arg0=0, arg1=0, payload=b''):
        code = struct.unpack('<I', command)[0]
        sock.sendall(struct.pack('<6I', code, arg0, arg1, len(payload), sum(payload) & 0xffffffff,
                                 code ^ 0xffffffff) + payload)

    @staticmethod
    def _read(sock, size):
        result = bytearray()
        while len(result) < size:
            part = sock.recv(size - len(result))
            if not part:
                raise RuntimeError('模拟器本地连接已中断')
            result.extend(part)
        return bytes(result)

    def _receive(self, sock):
        command, a, b, length, checksum, magic = struct.unpack('<6I', self._read(sock, 24))
        if magic != command ^ 0xffffffff or length > 8 * 1024 * 1024:
            raise RuntimeError('模拟器本地消息格式异常')
        body = self._read(sock, length)
        if checksum and checksum != sum(body) & 0xffffffff:
            raise RuntimeError('模拟器本地消息校验失败')
        return struct.pack('<I', command), a, b, body

    def execute(self, service):
        with socket.create_connection(('127.0.0.1', self.port), self.timeout) as sock:
            sock.settimeout(self.timeout)
            self._send(sock, b'CNXN', 0x01000001, 1024 * 1024, b'host::features=cmd;\0')
            command, _, _, _ = self._receive(sock)
            if command == b'AUTH':
                raise RuntimeError('模拟器本地连接尚未授权，未执行刷新')
            if command != b'CNXN':
                raise RuntimeError('模拟器本地连接未就绪')
            self._send(sock, b'OPEN', 1, 0, service.encode('ascii') + b'\0')
            result = bytearray()
            while True:
                command, remote, local, body = self._receive(sock)
                if local != 1:
                    raise RuntimeError('模拟器本地消息目标不一致')
                if command == b'WRTE':
                    result.extend(body)
                    if len(result) > 8 * 1024 * 1024:
                        raise RuntimeError('模拟器本地返回内容过大')
                    self._send(sock, b'OKAY', 1, remote)
                elif command == b'CLSE':
                    self._send(sock, b'CLSE', 1, remote)
                    return bytes(result)
                elif command != b'OKAY':
                    raise RuntimeError('模拟器本地消息类型异常')

    def screenshot(self):
        raw = self.execute('exec:screencap -p')
        if not raw.startswith(b'\x89PNG\r\n\x1a\n'):
            raise RuntimeError('未取得模拟器游戏画面')
        image = Image.open(io.BytesIO(raw))
        if image.width * image.height > 12000000 or image.width < image.height:
            raise RuntimeError('模拟器画面尺寸暂不支持')
        image.load()
        return image.convert('RGB')

    def foreground_package(self):
        text = self.execute('shell:dumpsys activity activities').decode('utf-8', 'replace')
        matches = re.findall(r'(?:mResumedActivity|topResumedActivity)[^\n]*?\s([a-zA-Z0-9_.]+)/', text)
        return matches[-1] if matches else None


def dashen_adb_port(connection):
    if os.name != 'nt' or not connection or connection.get('source') != 'dashen':
        raise RuntimeError('当前连接不是大神模拟器')
    host = Path(connection['executable'])
    if host.name.lower() != 'glemulator.exe':
        raise RuntimeError('模拟器主程序不匹配')
    config = host.parent.parent / 'vms' / 'GLEmulator.vbox'
    root = ET.parse(config).getroot()
    ports = {int(element.attrib['hostport']) for element in root.iter()
             if element.tag.rsplit('}', 1)[-1] == 'Forwarding'
             and element.attrib.get('guestport') == '5555' and element.attrib.get('hostport', '').isdigit()}
    if len(ports) != 1:
        raise RuntimeError('无法唯一识别模拟器本地端口')
    port = ports.pop()
    if not 1024 <= port <= 65535:
        raise RuntimeError('模拟器本地端口无效')
    script = (f"Get-NetTCPConnection -State Listen -LocalPort {port} -ErrorAction SilentlyContinue | "
              "Select-Object -ExpandProperty OwningProcess -Unique")
    result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script],
                            capture_output=True, text=True, timeout=10,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), check=True)
    owners = {int(value) for value in result.stdout.split() if value.isdigit()}
    if owners != {int(connection['pid'])}:
        raise RuntimeError('模拟器本地端口与当前游戏进程不一致')
    return port


def role_key(value):
    return re.sub(r'[\s丨|·丶]', '', value)


class AndroidMapSession:
    def __init__(self, connection, expected_name, current_connection, device=None, ocr=None):
        self.connection, self.expected_name = connection, expected_name
        self.current_connection = current_connection
        self.device = device or LocalAdb(dashen_adb_port(connection))
        self.ocr = ocr
        self.ui = CoordinateControls(anchors={'search': (165, 97, False), 'jump': (118, 37, True)},
                                     asset_names={'search': 'dashen-search'})
        self.size = None
        self.opened = False
        self.point = None
        self.submitted_at = None

    def game_foreground(self):
        self.foreground_package=self.device.foreground_package()
        if self.foreground_package=='com.netease.stzb.netease':return True
        # Dashen can keep its embedding container resumed while rendering STZB.
        # The bound live connection, running game process, map controls and
        # exact role-name check are still required before any refresh input.
        if self.foreground_package=='com.netease.gl':
            pid=self.device.execute('shell:pidof com.netease.stzb.netease')
            return isinstance(pid,bytes)and pid.strip().isdigit()and int(pid.strip())>0
        return False

    def capture(self):
        if not self.current_connection(self.connection):
            raise RuntimeError('游戏连接已变化，已停止本次地图恢复')
        if not self.game_foreground():
            raise RuntimeError('等待率土之滨回到地图（当前应用：'+str(self.foreground_package or '未读到')+'）')
        image = self.device.screenshot()
        if self.size and image.size != self.size:
            raise RuntimeError('模拟器画面尺寸已变化')
        self.size = image.size
        return image, (0, 0, *image.size)

    def click(self, frame, x, y):
        if not self.current_connection(self.connection) or frame[0].size != self.size:
            raise RuntimeError('游戏连接或画面已变化')
        if not self.game_foreground():
            raise RuntimeError('游戏已离开地图，已停止恢复')
        if not 0 <= x < self.size[0] or not 0 <= y < self.size[1]:
            raise RuntimeError('地图控件不在画面内')
        self.device.execute(f'shell:input tap {int(x)} {int(y)}')

    def verify_role(self, image):
        scale = image.height / 810
        # Crop only the name row. Including the power value below it makes OCR
        # occasionally merge a digit into the role name.
        box = tuple(round(v * scale) for v in (215, 0, 370, 27))
        crop = image.crop(box)
        grey = crop.convert('L')
        for variant in (grey, grey.point(lambda pixel: 255 if pixel >= 170 else 0)):
            variant = ImageOps.expand(variant, border=12, fill=0)
            variant = variant.resize((variant.width * 3, variant.height * 3), Image.Resampling.LANCZOS)
            name = ''.join(word['t'] for word in self.ocr.read(variant))
            if self.expected_name and role_key(name) == role_key(self.expected_name):
                return
        raise RuntimeError('等待地图中的角色名称核对，未执行自动恢复')

    def refresh(self):
        frame = self.capture()
        search = self.ui.find(frame[0], 'search')
        if search is None:
            raise RuntimeError('等待游戏回到地图，未执行自动恢复')
        self.verify_role(frame[0])
        try:
            self.click(frame, *search[:2])
            self.opened = True
            frame, jump = self.ui.wait_for(self, 'jump', '等待游戏打开地图面板')
            point = [self.ui.read_field(frame[0], jump, offset) for offset in (271, 158)]
            if any(not value or not value.isascii() or not value.isdigit() or not 1 <= int(value) <= 9999 for value in point):
                raise RuntimeError('当前地图坐标暂未读清，未执行跳转')
            # The panel is prefilled by the game. Return to that exact location;
            # no distant coordinate, game packet, army or combat action is used.
            frame = self.capture()
            jump = self.ui.find(frame[0], 'jump')
            if jump is None or [self.ui.read_field(frame[0], jump, offset) for offset in (271, 158)] != point:
                raise RuntimeError('地图坐标已被修改，已停止本次恢复')
            self.point = list(map(int, point))
            self.submitted_at = time.time()
            self.click(frame, *jump[:2])
            return self.point
        except Exception:
            self.close_panel()
            raise

    def close_panel(self):
        # Close only the panel opened by this attempt, on the same source.
        if self.opened and self.current_connection(self.connection):
            try:
                frame = self.capture()
                jump=self.ui.find(frame[0], 'jump')
                if jump:
                    scale=jump[2];image=frame[0]
                    x=image.width-round(64*scale);y=round(42*scale);radius=round(14*scale)
                    pixels=image.crop((x-radius,y-radius,x+radius+1,y+radius+1)).convert('RGB').getdata()
                    red=sum(r>100 and g<r*.7 and b<r*.85 for r,g,b in pixels)
                    if red>=max(6,round(15*scale*scale)):
                        self.click(frame,x,y)
                    # Android Back is a game-exit action in Dashen's container.
                    # With no recognized close control, leave the panel alone.
            except (OSError, RuntimeError):
                pass
        self.opened = False


class MapRecovery:
    """Refresh the existing map after a capture gap, then require a new baseline."""
    def __init__(self, state, session_factory=AndroidMapSession):
        self.state, self.session_factory = state, session_factory
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True, name='map-recovery')
        self.next_attempt = 0
        self.status = {'status': 'waiting', 'attempts': 0, 'message': '等待游戏连接'}
        self._update()

    def _update(self, **values):
        self.status.update(values)
        with self.state.lock:
            self.state.health['map_recovery'] = dict(self.status)

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread.is_alive() and threading.current_thread() is not self.thread:
            self.thread.join(timeout=12)

    @staticmethod
    def identity(connection):
        return tuple(connection.get(key) for key in ('source', 'pid', 'host_pid', 'peer', 'local_port', 'connection_started'))

    def current_connection(self, expected):
        with self.state.lock:
            h = self.state.health
            return (h.get('status') == 'live' and
                    time.time() - h.get('last_packet_at', 0) <= 15 and
                    self.identity(h.get('connection') or {}) == self.identity(expected))

    def pending(self):
        with self.state.lock:
            h = self.state.health
            if not h.get('resync_required'):
                return None
            connection = h.get('connection') or {}
            own = self.state.config.get('self', {})
            if (connection.get('source') != 'dashen' or not own.get('confirmed') or
                    h.get('identity_pending') or h.get('scope_pending') or not self.current_connection(connection)):
                return None
            return dict(connection), own.get('name')

    def recover(self, connection, name):
        started = time.time()
        self._update(status='recovering', attempts=self.status['attempts'] + 1,
                     last_attempt_at=started, required_scene_after=started, coord=None, error=None,
                     message='正在自动恢复当前地图')
        session = None
        try:
            with WindowsOcr() as ocr:
                session = self.session_factory(connection, name, self.current_connection, ocr=ocr)
                point = session.refresh()
            required_after = getattr(session, 'submitted_at', None) or started
            self._update(coord=point, required_scene_after=required_after)
            deadline = time.monotonic() + 8
            while not self.stop_event.is_set() and time.monotonic() < deadline:
                if not self.current_connection(connection):
                    raise RuntimeError('游戏连接已变化，等待重新恢复')
                with self.state.lock:
                    h = self.state.health
                    ready = not h.get('resync_required') and h.get('last_full_scene_at', 0) >= required_after
                if ready:
                    # A completed new map packet is authoritative. A delayed
                    # button animation must not turn successful recovery into failure.
                    session.opened = False
                    self._update(status='ready', message='当前地图已自动恢复', coord=point,
                                 last_success_at=time.time(), error=None)
                    return True
                self.stop_event.wait(.25)
            raise RuntimeError('等待客户端完整地图，保留局部观测继续重试')
        finally:
            if session is not None and hasattr(session, 'close_panel'):
                session.close_panel()

    def run(self):
        while not self.stop_event.wait(1):
            candidate = self.pending()
            if candidate is None:
                self.observe_idle()
                continue
            if time.monotonic() < self.next_attempt:
                continue
            # Allow the login/map-entry response to arrive naturally first.
            if not self.stop_event.wait(2):
                candidate = self.pending()
                if candidate is None:
                    continue
                try:
                    self.recover(*candidate)
                    self.next_attempt = time.monotonic() + 5
                except (OSError, RuntimeError, ValueError, struct.error, subprocess.SubprocessError, ET.ParseError) as error:
                    self._update(status='waiting_for_map', message=str(error), error=type(error).__name__)
                    self.next_attempt = time.monotonic() + 10

    def observe_idle(self):
        with self.state.lock:
            h = dict(self.state.health)
        if h.get('resync_required'):
            if h.get('status') != 'live' or time.time() - h.get('last_packet_at', 0) > 15:
                if self.status['status'] != 'waiting_for_connection':
                    self._update(status='waiting_for_connection', message='等待采集重新连接，随后自动恢复当前地图', coord=None)
            return
        if (h.get('last_full_scene_at', 0) >= self.status.get('required_scene_after', float('inf'))
                and self.status['status'] != 'ready'):
            self._update(status='ready', message='当前地图已自动恢复', last_success_at=time.time(), error=None)
