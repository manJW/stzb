"""Navigate the visible Windows game through its coordinate-search controls."""
import ctypes
import json
import os
import re
import subprocess
import tempfile
import threading
import time
from ctypes import wintypes
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageGrab, ImageStat


ROOT = Path(__file__).resolve().parent
ASSETS = ROOT / 'catalog' / 'game-locator'
REFERENCE_HEIGHT = 810
ANCHORS = {'search': (132, 89, False), 'jump': (118, 37, True)}


def coordinates(value):
    if not isinstance(value, list) or len(value) != 2 or any(type(v) is not int or not 1 <= v <= 9999 for v in value):
        raise ValueError('定位需要两个有效的游戏坐标')
    return tuple(value)


def centered(x, y, width, height):
    left, top = round(x - width / 2), round(y - height / 2)
    return left, top, left + width, top + height


def digit_glyphs(image):
    mask = image.convert('L').point(lambda value: 255 if value >= 150 else 0)
    columns, _ = mask.getprojection()
    start = None
    for x, present in enumerate(columns + [0]):
        if present and start is None:
            start = x
        elif not present and start is not None:
            glyph = mask.crop((start, 0, x, mask.height))
            box = glyph.getbbox()
            if x - start >= 3 and box[3] - box[1] >= mask.height * .43:
                yield glyph.crop(box).resize((16, 24), Image.Resampling.LANCZOS)
            start = None


class GameLocator:
    def __init__(self):
        self.lock = threading.Lock()

    def locate(self, connection, point):
        x, y = coordinates(point)
        if os.name != 'nt' or not connection or connection.get('source') != 'pc':
            raise RuntimeError('请先保持 PC 游戏在线并进入地图')
        if not self.lock.acquire(blocking=False):
            raise RuntimeError('正在进行上一次定位')
        try:
            with WindowsGame(connection) as game, WindowsOcr() as ocr:
                game.activate()
                self.navigate(game, (x, y), OcrCoordinateControls(ocr))
            return {'ok': True, 'coord': [x, y]}
        finally:
            self.lock.release()

    @staticmethod
    def navigate(game, point, ui=None):
        x, y = point
        ui = ui or CoordinateControls()
        frame = game.capture()
        jump = ui.find(frame[0], 'jump')
        if jump is None:
            search = ui.find(frame[0], 'search')
            if search is None:
                raise RuntimeError('请切回游戏地图，再试一次定位')
            game.click(frame, *search[:2])
            frame, jump = ui.wait_for(game, 'jump', '游戏未打开坐标跳转界面')
        for value, offset in ((x, 271), (y, 158)):
            field_x, field_y = ui.field(jump, offset)
            game.click(frame, field_x + round(36 * jump[2]), field_y)
            game.enter_number(value)
            frame = game.capture()
            jump = ui.find(frame[0], 'jump')
            if jump is None:
                raise RuntimeError('坐标界面已变化，请返回游戏地图重试')
        frame = ui.verify_fields(game, frame, jump, point)
        game.click(frame, *jump[:2])
        ui.wait_for(game, 'search', '已提交坐标，游戏地图返回状态待核对')


class CoordinateControls:
    def __init__(self):
        self.templates = {name: Image.open(ASSETS / (name + '.png')).convert('L') for name in ANCHORS}
        strip = Image.open(ASSETS / 'digits.png').convert('L')
        self.digits = []
        for n in range(10):
            glyph = strip.crop((16*n, 0, 16*(n+1), 24))
            variants = []
            # Antialiasing changes slightly with each digit's subpixel position.
            for dx in range(-2, 3):
                for dy in range(-1, 2):
                    shifted = Image.new('L', glyph.size)
                    shifted.paste(glyph, (dx, dy))
                    variants.append(shifted)
            self.digits.append(variants)

    def find(self, image, name):
        right, vertical, from_bottom = ANCHORS[name]
        width, height = image.size
        grey = image.convert('L')
        best = (float('inf'), None)
        for scale in dict.fromkeys((round(height / REFERENCE_HEIGHT, 3), 1.0)):
            if not .6 <= scale <= 1.6:
                continue
            template = self.templates[name]
            template = template.resize(tuple(round(n * scale) for n in template.size), Image.Resampling.LANCZOS)
            cx = width - round(right * scale)
            cy = height - round(vertical * scale) if from_bottom else round(vertical * scale)
            for dy in range(-7, 8):
                for dx in range(-7, 8):
                    box = centered(cx+dx, cy+dy, *template.size)
                    difference = ImageStat.Stat(ImageChops.difference(grey.crop(box), template)).mean[0]
                    if difference < best[0]:
                        best = (difference, (cx+dx, cy+dy, scale))
            if best[0] < 8:
                break
        return best[1] if best[0] <= 18 else None

    def wait_for(self, game, name, error):
        deadline = time.monotonic() + 2.2
        while time.monotonic() < deadline:
            time.sleep(.15)
            frame = game.capture()
            result = self.find(frame[0], name)
            if result is not None:
                return frame, result
        raise RuntimeError(error)

    @staticmethod
    def field(jump, offset):
        x, y, scale = jump
        return x - round(offset * scale), y - round(3 * scale)

    def verify_fields(self, game, frame, jump, point):
        if any(self.read_field(frame[0], jump, offset) != str(value)
               for value, offset in zip(point, (271, 158))):
            raise RuntimeError('游戏输入框中的坐标核对失败，请在游戏内检查坐标')
        return frame

    def read_field(self, image, jump, offset):
        x, y = self.field(jump, offset)
        scale = jump[2]
        crop = image.crop(centered(x, y, round(70 * scale), round(24 * scale)))
        return self.read_digits(crop)

    def read_digits(self, crop):
        result = []
        for glyph in digit_glyphs(crop):
            distances = sorted((min(ImageStat.Stat(ImageChops.difference(glyph, ref)).mean[0] for ref in variants), digit)
                               for digit, variants in enumerate(self.digits))
            if distances[0][0] > 75 or distances[1][0] - distances[0][0] < 7:
                return None
            result.append(str(distances[0][1]))
        return ''.join(result)


class WindowsOcr:
    def __enter__(self):
        self.directory = tempfile.TemporaryDirectory(prefix='game-ocr-', dir=ROOT / 'runtime')
        self.sequence = 0
        try:
            self.proc = subprocess.Popen(
                ['powershell.exe', '-NoProfile', '-NonInteractive', '-File',
                 str(Path(__file__).with_name('Windows-Ocr.ps1'))],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, encoding='utf-8', creationflags=subprocess.CREATE_NO_WINDOW)
        except OSError:
            self.directory.cleanup()
            raise
        return self

    def __exit__(self, *_):
        self.proc.kill()
        self.proc.communicate()
        self.directory.cleanup()

    def read(self, image):
        self.sequence += 1
        path = Path(self.directory.name) / f'frame-{self.sequence}.png'
        image.save(path)
        self.proc.stdin.write(str(path) + '\n')
        self.proc.stdin.flush()
        reply = self.proc.stdout.readline()
        if not reply:
            raise RuntimeError('Windows 游戏读数服务不可用')
        result = json.loads(reply)
        if result.get('error'):
            raise RuntimeError('Windows 游戏读数失败')
        return result['words']


class OcrCoordinateControls(CoordinateControls):
    def __init__(self, ocr):
        super().__init__()
        self.ocr = ocr

    def find(self, image, name):
        found = super().find(image, name)
        if found or name != 'jump':
            return found
        width, height = image.size
        scale = height / REFERENCE_HEIGHT
        jump = round(width-118*scale), round(height-37*scale), scale
        # The client cursor can cover the middle of the jump label. Verify its
        # bright button edges together with both adjacent numeric fields.
        edges = [image.crop(tuple(round(v) for v in
                 (width-right*scale, height-54*scale, width-(right-18)*scale, height-21*scale))).convert('L')
                 for right in (207, 42)]
        if all(ImageStat.Stat(edge).mean[0] >= 195 for edge in edges) and any(
                self.read_field(image, jump, offset) for offset in (271, 158)):
            return jump
        return None

    def read_field(self, image, jump, offset):
        x, y = self.field(jump, offset)
        scale = jump[2]
        bounds = x-39*scale, y-17*scale, x+39*scale, y+22*scale
        field = image.crop(tuple(map(round, bounds))).convert('L')
        mask = field.point(lambda p: 255 if p >= 150 else 0)
        extent = mask.getbbox()
        if extent:
            # A blinking caret can touch the final digit. It is taller than
            # every numeric glyph; remove only those outermost columns.
            draw = ImageDraw.Draw(field)
            for i in range(extent[2]-1, max(extent[0]-1, extent[2]-5), -1):
                column = mask.crop((i, 0, i+1, mask.height)).getbbox()
                if not column or column[3]-column[1] < 21*scale:
                    break
                draw.line((i, 0, i, field.height-1), fill=0)
            value = self.read_digits(field.crop((0, round(5*scale), field.width, round(31*scale))))
            if value:
                return value
        try:
            value = ''.join(word['t'].strip() for word in self.ocr.read(field))
        except (OSError, RuntimeError):
            return super().read_field(image, jump, offset)
        return value if re.fullmatch(r'[0-9]{1,4}', value) else super().read_field(image, jump, offset)

    def verify_fields(self, game, frame, jump, point):
        try:
            return super().verify_fields(game, frame, jump, point)
        except RuntimeError:
            # The focused input caret can touch the last digit. Clear focus
            # using the separator between the two fields, then read both again.
            x, y, scale = jump
            game.click(frame, x-round(214*scale), y)
            for _ in range(3):
                time.sleep(.25)
                frame = game.capture()
                if not self.find(frame[0], 'jump'):
                    continue
                try:
                    return super().verify_fields(game, frame, jump, point)
                except RuntimeError:
                    continue
            raise RuntimeError('游戏输入框中的坐标核对失败，请在游戏内检查坐标')


class WindowsGame:
    def __init__(self, connection):
        self.user = ctypes.WinDLL('user32', use_last_error=True)
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        self.pid = int(connection['pid'])
        self.exe = os.path.normcase(connection['executable'])
        if os.path.basename(self.exe) != 'stzb.exe':
            raise RuntimeError('当前采集进程与 PC 游戏不一致')
        self.dpi = self.bind(self.user, 'SetThreadDpiAwarenessContext', ctypes.c_void_p, ctypes.c_void_p)
        self.foreground = self.bind(self.user, 'GetForegroundWindow', wintypes.HWND)
        self.window_at = self.bind(self.user, 'WindowFromPoint', wintypes.HWND, wintypes.POINT)
        self.root_window = self.bind(self.user, 'GetAncestor', wintypes.HWND, wintypes.HWND, wintypes.UINT)
        self.thread_process = self.bind(self.user, 'GetWindowThreadProcessId', wintypes.DWORD, wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
        self.client_rect = self.bind(self.user, 'GetClientRect', wintypes.BOOL, wintypes.HWND, ctypes.POINTER(wintypes.RECT))
        self.to_screen = self.bind(self.user, 'ClientToScreen', wintypes.BOOL, wintypes.HWND, ctypes.POINTER(wintypes.POINT))

    @staticmethod
    def bind(library, name, result, *args):
        function = getattr(library, name)
        function.argtypes, function.restype = args, result
        return function

    def __enter__(self):
        self.previous_dpi = self.dpi(ctypes.c_void_p(-4))
        try:
            self.hwnd = self.find_window()
        except Exception:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *_):
        if self.previous_dpi:
            self.dpi(self.previous_dpi)

    def find_window(self):
        handle = self.bind(self.kernel, 'OpenProcess', wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)(0x1000, False, self.pid)
        if not handle:
            raise RuntimeError('PC 游戏进程未就绪')
        try:
            path = ctypes.create_unicode_buffer(32768)
            length = wintypes.DWORD(len(path))
            get_path = self.bind(self.kernel, 'QueryFullProcessImageNameW', wintypes.BOOL, wintypes.HANDLE, wintypes.DWORD,
                                 wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
            if not get_path(handle, 0, path, ctypes.byref(length)) or os.path.normcase(path.value) != self.exe:
                raise RuntimeError('无法核对当前 PC 游戏进程')
        finally:
            self.bind(self.kernel, 'CloseHandle', wintypes.BOOL, wintypes.HANDLE)(handle)
        candidates = []
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        window_text = self.bind(self.user, 'GetWindowTextW', ctypes.c_int, wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)

        @callback_type
        def collect(hwnd, _):
            pid = wintypes.DWORD()
            self.thread_process(hwnd, ctypes.byref(pid))
            if pid.value == self.pid:
                title = ctypes.create_unicode_buffer(80)
                window_text(hwnd, title, len(title))
                if title.value == '率土之滨':
                    candidates.append(hwnd)
            return True

        self.bind(self.user, 'EnumWindows', wintypes.BOOL, callback_type, wintypes.LPARAM)(collect, 0)
        if len(candidates) != 1:
            raise RuntimeError('无法唯一识别率土之滨主窗口')
        return candidates[0]

    def activate(self):
        u, hwnd = self.user, self.hwnd
        if self.bind(u, 'IsIconic', wintypes.BOOL, wintypes.HWND)(hwnd):
            self.bind(u, 'ShowWindow', wintypes.BOOL, wintypes.HWND, ctypes.c_int)(hwnd, 9)
        activate = self.bind(u, 'SetForegroundWindow', wintypes.BOOL, wintypes.HWND)
        activate(hwnd)
        if self.foreground() != hwnd:
            thread = self.kernel.GetCurrentThreadId()
            foreground = self.thread_process(self.foreground(), None)
            attach = self.bind(u, 'AttachThreadInput', wintypes.BOOL, wintypes.DWORD, wintypes.DWORD, wintypes.BOOL)
            # Ensure this request thread has a message queue before attaching it.
            message = wintypes.MSG()
            self.bind(u, 'PeekMessageW', wintypes.BOOL, ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                      wintypes.UINT, wintypes.UINT, wintypes.UINT)(ctypes.byref(message), None, 0, 0, 0)
            if foreground and thread != foreground and attach(thread, foreground, True):
                try:
                    self.bind(u, 'BringWindowToTop', wintypes.BOOL, wintypes.HWND)(hwnd)
                    activate(hwnd)
                finally:
                    attach(thread, foreground, False)
        time.sleep(.18)
        self.require_foreground()

    def require_foreground(self):
        if self.foreground() != self.hwnd:
            raise RuntimeError('游戏未在前台，请切回游戏地图后重试')

    def bounds(self):
        rect, origin = wintypes.RECT(), wintypes.POINT()
        if not self.client_rect(self.hwnd, ctypes.byref(rect)) or not self.to_screen(self.hwnd, ctypes.byref(origin)):
            raise RuntimeError('游戏窗口位置已变化')
        return origin.x, origin.y, origin.x + rect.right, origin.y + rect.bottom

    def capture(self):
        self.require_foreground()
        bounds = self.bounds()
        return ImageGrab.grab(bbox=bounds, all_screens=True), bounds

    def move_to_control(self, frame, x, y):
        self.require_foreground()
        _, bounds = frame
        if bounds != self.bounds() or not (0 <= x < bounds[2]-bounds[0] and 0 <= y < bounds[3]-bounds[1]):
            raise RuntimeError('游戏窗口已移动或控件不在窗口内')
        px, py = bounds[0]+x, bounds[1]+y
        if self.root_window(self.window_at(wintypes.POINT(px, py)), 2) != self.hwnd:
            raise RuntimeError('游戏控件被其他窗口遮挡')
        if not self.user.SetCursorPos(px, py):
            raise RuntimeError('无法定位游戏控件')
        self.require_foreground()

    def click(self, frame, x, y):
        self.move_to_control(frame, x, y)
        self.user.mouse_event(0x0002, 0, 0, 0, 0)
        self.user.mouse_event(0x0004, 0, 0, 0, 0)
        time.sleep(.12)

    def enter_number(self, number):
        self.require_foreground()
        key = self.user.keybd_event
        key(0x11, 0, 0, 0)
        try:
            key(0x41, 0, 0, 0)
            key(0x41, 0, 2, 0)
        finally:
            key(0x11, 0, 2, 0)
        time.sleep(.05)
        self.require_foreground()
        for digit in str(number):
            key(ord(digit), 0, 0, 0)
            key(ord(digit), 0, 2, 0)
        time.sleep(.12)
