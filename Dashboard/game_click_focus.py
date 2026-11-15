"""Follow map selections using the verified game's own city-info traffic."""
import ctypes
import os
import threading
import time
from ctypes import wintypes


def world_coordinate(value):
    if type(value) is not int:
        return None
    x,y = divmod(value,10000)
    return [x,y] if 1<=x<=9999 and 1<=y<=9999 else None


def city_request(value):
    # Decode only cmd 21. Reject bodies with competing IDs or unknown fields.
    if (not isinstance(value,list) or not 1<=len(value)<=8 or
            any(type(item) is not int or item<0 for item in value)):
        return None
    points = {tuple(point) for item in value if (point:=world_coordinate(item))}
    return list(points.pop()) if len(points)==1 else None


def city_selection(value):
    # Dashen's observed cmd 21 reply: 66 fields, selected world ID at index 65.
    if (not isinstance(value,list) or len(value)!=66 or not isinstance(value[0],str) or
            any(type(value[i]) is not int or value[i]<0 for i in (4,5)) or value[4]>value[5]):
        return None
    point = world_coordinate(value[65])
    return (point,value[0]) if point else None


class ClickTracker:
    def __init__(self):
        self.was_down = False
        self.press = None

    def sample(self, down, point, window, inside, at):
        click = False
        if down and not self.was_down:
            self.press = (point,window,at) if inside else None
        if self.press:
            origin,expected_window,started = self.press
            if (not inside or expected_window!=window or at-started>1.2 or
                    max(abs(a-b) for a,b in zip(origin,point))>6):
                self.press = None
            elif not down:
                click = True
                self.press = None
        self.was_down = down
        return click


class WindowsGameClicks:
    """Observe clicks inside the foreground game window, without sending input."""
    def __init__(self):
        self.user = ctypes.WinDLL('user32',use_last_error=True)
        for name,restype,args in (
            ('GetAsyncKeyState',wintypes.SHORT,[ctypes.c_int]),
            ('GetForegroundWindow',wintypes.HWND,[]),
            ('GetWindowThreadProcessId',wintypes.DWORD,[wintypes.HWND,ctypes.POINTER(wintypes.DWORD)]),
            ('GetCursorPos',wintypes.BOOL,[ctypes.POINTER(wintypes.POINT)]),
            ('GetClientRect',wintypes.BOOL,[wintypes.HWND,ctypes.POINTER(wintypes.RECT)]),
            ('ScreenToClient',wintypes.BOOL,[wintypes.HWND,ctypes.POINTER(wintypes.POINT)]),
        ):
            method = getattr(self.user,name)
            method.restype,method.argtypes = restype,args
        self.tracker = ClickTracker()

    def poll(self,connection):
        button = self.user.GetAsyncKeyState(1)
        down = bool(button&0x8000)
        hwnd = self.user.GetForegroundWindow()
        pid,point,rect = wintypes.DWORD(),wintypes.POINT(),wintypes.RECT()
        self.user.GetWindowThreadProcessId(hwnd,ctypes.byref(pid))
        inside = False
        if (connection and pid.value==connection.get('host_pid') and
                self.user.GetCursorPos(ctypes.byref(point)) and
                self.user.ScreenToClient(hwnd,ctypes.byref(point)) and
                self.user.GetClientRect(hwnd,ctypes.byref(rect))):
            inside = 0<=point.x<rect.right and 0<=point.y<rect.bottom
        sample = ((point.x,point.y),int(hwnd or 0),inside,time.monotonic())
        if button&1 and not down and not self.tracker.was_down:
            self.tracker.sample(True,*sample)
        return self.tracker.sample(down,*sample)


class GameClickFocus:
    def __init__(self,state,mouse_factory=WindowsGameClicks):
        self.state,self.mouse_factory = state,mouse_factory
        self.stop_event = threading.Event()
        self.lock = threading.RLock()
        self.generation = 0
        self.click = None
        self.requests,self.responses = [],[]
        self.published = None
        self.monitor = threading.Thread(target=self.run,daemon=True,name='game-click-monitor')

    def connection(self):
        with self.state.lock:
            h,own = self.state.health,self.state.config['self']
            connection = h.get('connection') or {}
            if (h['status']!='live' or time.time()>self.state._transport_valid_until() or
                    connection.get('source')!='dashen' or h.get('identity_pending') or
                    h.get('scope_pending') or not own.get('confirmed') or h['schema_errors']):
                return None
            return dict(connection)

    def start(self):
        if os.name=='nt':
            self.monitor.start()

    def stop(self):
        self.stop_event.set()
        if self.monitor.is_alive() and self.monitor is not threading.current_thread():
            self.monitor.join(timeout=2)

    def submit(self,connection):
        with self.lock:
            moment = time.time()
            single = not self.click or moment-self.click['at']>1
            self.generation += 1
            self.click = {'generation':self.generation,'at':moment,'connection':dict(connection),'single':single}
            with self.state.lock:
                self.state.health['game_click_focus'] = {'status':'waiting_for_city_data',
                    'clicks':self.generation,'last_click_at':self.click['at'],'method':'game_packets'}
            return self._synchronize()

    def on_request(self,message_id,value,observed_at):
        point = city_request(value) if message_id==21 else None
        connection = self.connection() if point else None
        if not connection:
            return False
        with self.lock:
            self.requests = [r for r in self.requests if observed_at-r['at']<=4][-15:]
            self.requests.append({'point':point,'at':observed_at,'connection':connection})
            return self._synchronize()

    def on_message(self,message_id,value,observed_at):
        selected = city_selection(value) if message_id==21 else None
        connection = self.connection() if selected else None
        if not connection:
            return False
        with self.lock:
            self.responses = [r for r in self.responses if observed_at-r['at']<=4][-15:]
            self.responses.append({'point':selected[0],'name':selected[1],
                                   'at':observed_at,'connection':connection})
            return self._synchronize()

    def request_gap(self):
        with self.lock:
            self.requests.clear()
            self.responses.clear()
            if self.click:
                self.click['single'] = False

    def _synchronize(self):
        click = self.click
        if not click or self.connection()!=click['connection']:
            return False
        requests = [r for r in self.requests if r['connection']==click['connection'] and
                    -.15<=r['at']-click['at']<=3]
        request = max(requests,key=lambda r:r['at']) if requests else None
        if request:
            # A late reply to the preceding tile must never override the newest one.
            replies = [r for r in self.responses if r['connection']==request['connection'] and
                       r['point']==request['point'] and request['at']<=r['at']<=request['at']+3]
        elif click['single'] and (not self.published or self.published[0]!=click['generation']):
            # The receive-only adapter has this same verified fast path. During
            # rapid selections require a matching outgoing request instead.
            replies = [r for r in self.responses if r['connection']==click['connection'] and
                       -.1<=r['at']-click['at']<=3]
        else:
            return False
        if not replies:
            return False
        reply = max(replies,key=lambda r:r['at'])
        key = (click['generation'],request['at'] if request else reply['at'],tuple(reply['point']))
        if key==self.published or time.time()-click['at']>4:
            return False
        evidence = 'selected_tile_request_response_21' if request else 'selected_tile_response_21'
        if not self.state.set_game_focus(reply['point'],reply['connection'],time.time(),evidence=evidence):
            return False
        self.published = key
        with self.state.lock:
            self.state.game_focus.update(name=reply['name'][:120],clicked_at=click['at'],
                request_at=request['at'] if request else None,message_at=reply['at'])
            self.state.health['game_click_focus'].update(status='ready',coord=reply['point'])
        return True

    def run(self):
        try:
            mouse = self.mouse_factory()
            while not self.stop_event.wait(.02):
                connection = self.connection()
                if mouse.poll(connection) and connection:
                    self.submit(connection)
        except (OSError,RuntimeError) as error:
            with self.state.lock:
                self.state.health['game_click_error'] = type(error).__name__
