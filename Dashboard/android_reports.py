"""Idle-time report paging through the logged-in Android client, not packet injection."""
import ctypes
import json
import logging
import re
import threading
import time
from datetime import datetime
from pathlib import Path

from PIL import Image

from android_map_recovery import AndroidMapSession
from game_locator import WindowsOcr
from garrison_trial import SHANGHAI, Screen
from lineup_prediction import meets_main_threshold
from model import write_json
from report_backfill import ReportList, matches_name


class DesktopInput:
    class Info(ctypes.Structure):
        _fields_ = [('size', ctypes.c_uint), ('tick', ctypes.c_uint)]

    def tick(self):
        value = self.Info(ctypes.sizeof(self.Info), 0)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(value)):
            raise RuntimeError('无法检查桌面空闲状态')
        return value.tick

    def idle(self):
        return (ctypes.windll.kernel32.GetTickCount() - self.tick()) & 0xffffffff

    def active_in(self, processes):
        user=ctypes.windll.user32
        user.GetForegroundWindow.restype=ctypes.c_void_p
        user.GetWindowThreadProcessId.argtypes=[ctypes.c_void_p,ctypes.POINTER(ctypes.c_uint)]
        process=ctypes.c_uint()
        user.GetWindowThreadProcessId(user.GetForegroundWindow(),ctypes.byref(process))
        return process.value in processes


class AndroidReportGame(AndroidMapSession):
    def __init__(self, connection, name, current_connection, *, device=None, ocr=None, desktop=None):
        super().__init__(connection, name, current_connection, device=device, ocr=ocr)
        self.desktop = desktop or DesktopInput()
        self.input_marker = None

    def __enter__(self):
        if self.user_in_game() and self.desktop.idle() < 6000:
            raise RuntimeError('等待鼠标键盘空闲6秒后自动读取')
        self.input_marker = self.desktop.tick()
        return self

    def user_in_game(self):
        if not self.connection.get('host_pid') or not hasattr(self.desktop,'active_in'):
            return True
        return self.desktop.active_in({int(self.connection['host_pid']),int(self.connection['pid'])})

    def __exit__(self, *_):
        cleanup=getattr(self,'report_cleanup',None)
        if cleanup:
            try:
                self.undisturbed()
                cleanup()
            except (RuntimeError,OSError):
                logging.info('Report reading ended without changing the current user-controlled page')
        return False

    def undisturbed(self):
        if self.desktop.tick() != self.input_marker and self.user_in_game():
            raise RuntimeError('检测到用户操作，暂停翻报；已收到的数据继续保存')
        self.input_marker=self.desktop.tick()
        if not self.current_connection(self.connection):
            raise RuntimeError('角色、采集来源或游戏连接已变化，已暂停翻报')

    def capture(self):
        self.undisturbed()
        return super().capture()

    def click(self, frame, x, y):
        self.undisturbed()
        super().click(frame, x, y)

    def swipe_list(self, frame):
        self.undisturbed()
        if frame[0].size != self.size or self.device.foreground_package() != 'com.netease.stzb.netease':
            raise RuntimeError('战报画面或游戏进程已变化')
        width, height = self.size
        self.device.execute(f'shell:input swipe {width//2} {int(height*.91)} {width//2} {int(height*.34)} 600')


class AndroidReportList(ReportList):
    def is_map(self, screen):
        return bool(self.game.ui.find(screen.frame[0],'search'))

    def report_times(self, screen):
        center=screen.width/2
        image=screen.crop((center-110,150,center+110,785)).convert('L')
        image=image.resize((660,1905),Image.Resampling.LANCZOS)
        lines=[]
        for word in sorted(self.ocr.read(image),key=lambda w:w['y']):
            y=word['y']+word['h']/2
            if lines and abs(y-lines[-1][0])<18:
                lines[-1][1].append(word)
            else:
                lines.append((y,[word]))
        results=[]
        for y,words in lines:
            value=''.join(w['t'] for w in sorted(words,key=lambda w:w['x'])).replace(' ','')
            value=value.translate(str.maketrans({'：':':','．':'.','。':'.','I':'1','l':'1'}))
            normalized=value.replace('.','/').replace('-','/')
            match=re.search(r'20\d{2}/\d{2}/\d{2}\d{2}:\d{2}(?::\d{2})?',normalized)
            if match:
                results.append((y/3+150,match[0]))
            elif len(re.findall(r'\d',value))>=8 and re.search(r'[/：:]',value):
                # A damaged year/date can still locate a row. It is never used to
                # invent a date or ID: both player names and the real reply must agree.
                results.append((y/3+150,value))
        return results

    def open_alliance(self):
        screen = self.screen()
        title = self.title(screen)
        if title == '同盟战报':
            return screen
        if title in ('个人战报', '收藏战报'):
            self.click(screen, 368, 107)
            return self.wait(lambda s:self.title(s)=='同盟战报', '等待同盟战报列表')
        if self.is_map(screen):
            self.game.verify_role(screen.frame[0])
            self.click(screen,124,600)
            self.game.report_cleanup=self.return_to_map
            screen=self.wait(lambda s:self.title(s) in ('个人战报','同盟战报','收藏战报'), '等待游戏打开战报列表')
            if self.title(screen)!='同盟战报':
                self.click(screen,368,107)
                screen=self.wait(lambda s:self.title(s)=='同盟战报', '等待同盟战报列表')
            return screen
        # A report collector never clicks through unrecognized map/menu layouts.
        raise RuntimeError('自动翻报已就绪；当前页面不是战报列表，未操作其他游戏功能')

    def return_to_map(self):
        screen=self.screen()
        if self.title(screen) in ('个人战报','同盟战报','收藏战报'):
            self.click(screen,screen.width-65,40)
            self.wait(self.is_map,'已结束补采，等待地图返回')

    @staticmethod
    def players(screen, timestamp_y):
        center = screen.width/2
        top, bottom = timestamp_y-240, timestamp_y-170
        if top < 145:
            return None
        return (screen.text((center-300,top,center-105,bottom),high_contrast=True),
                screen.text((center+76,top,center+245,bottom),high_contrast=True))

    def advance(self, screen):
        if self.title(screen) != '同盟战报':
            raise RuntimeError('已离开同盟战报列表，未执行翻页')
        self.check()
        self.game.swipe_list(screen.frame)
        time.sleep(.7)
        return self.wait(lambda s:self.title(s)=='同盟战报', '翻页后未识别到同盟战报')


def timestamp_interval(value):
    for pattern, span in (('%Y/%m/%d%H:%M:%S',1),('%Y/%m/%d%H:%M',60)):
        try:
            return datetime.strptime(value,pattern).replace(tzinfo=SHANGHAI).timestamp(), span
        except ValueError:
            continue
    return None


def match_visible(rows, names, missed):
    """Both player names must match; the actual reply, never OCR, supplies the battle ID."""
    if not names:
        return []
    matches=[]
    for row in rows:
        if not ((matches_name(names[0],row['player_name']) and matches_name(names[1],row['opponent'])) or
                (matches_name(names[1],row['player_name']) and matches_name(names[0],row['opponent']))):
            continue
        item=dict(row)
        teams=[json.loads(row[field]) for field in ('heroes','opponent_heroes')]
        item['priority']=(max(missed.get((row['union_id'],row['player_name']),0),
                              missed.get((row['opponent_union'],row['opponent']),0)),
                          all(meets_main_threshold(team) for team in teams),row['event_at'])
        matches.append(item)
    return sorted(matches,key=lambda item:item['priority'],reverse=True)


class AndroidReportMonitor:
    MAX_SWIPES = 40
    RESPONSE_SECONDS = 12

    def __init__(self, archive, locator, connection, is_busy, *, game_factory=AndroidReportGame,
                 reports_factory=AndroidReportList, ocr_factory=WindowsOcr):
        self.archive,self.locator,self.connection,self.is_busy=archive,locator,connection,is_busy
        self.game_factory,self.reports_factory,self.ocr_factory=game_factory,reports_factory,ocr_factory
        self.lock=threading.RLock()
        self.cancel=threading.Event()
        self.thread=None
        self.last_counts=None
        self.profile_id=archive.profile_id
        self.path=archive.root/'runtime'/f'android-report-monitor-{self.profile_id}.json'
        self.progress={'active':False,'mode':'client_ui','source':'dashen',
                       'reason':'自动翻报待开启；会读取游戏战报页面', 'saved':0,'new_reports':0,
                       'swipes':0,'report_batches':0,'skipped':0,'current':None,
                       'last_checked_at':None,'last_saved_id':None,'earliest_visible_at':None}
        if self.path.is_file():
            try:
                saved=json.loads(self.path.read_text(encoding='utf-8'))
                if saved.get('profile_id')==self.profile_id:
                    for key in ('saved','new_reports','swipes','report_batches','skipped',
                                'last_checked_at','last_saved_id','earliest_visible_at','enabled','maximum'):
                        if key in saved:
                            self.progress[key]=saved[key]
            except (OSError,ValueError):
                logging.warning('Unable to restore Android report progress')
        self.progress['profile_id']=self.profile_id

    def snapshot(self):
        with self.lock:
            result=dict(self.progress)
        with self.archive.lock:
            result['alliance_reports']=self.archive.intelligence.alliance_report_coverage()
        return result

    def update(self, **values):
        with self.lock:
            self.progress.update(values)
            write_json(self.path,self.progress)

    def start(self,maximum=5,*,wait_for_ready=False):
        if type(maximum) is not int or not 1<=maximum<=20:
            raise ValueError('每批补报数量须为1～20')
        if not wait_for_ready and not self.connection():
            raise ValueError('等待已核对角色的大神游戏连接')
        with self.lock:
            if self.progress['active']:
                raise ValueError('自动补采正在进行')
            self.cancel.clear()
            self.last_counts=self.counts()
            self.progress.update(active=True,enabled=True,maximum=maximum,reason='等待空闲后自动翻报并确认入库')
            write_json(self.path,self.progress)
            self.thread=threading.Thread(target=self.run,args=(maximum,),daemon=True,name='android-report-monitor')
            self.thread.start()
        return self.snapshot()

    def restore(self):
        if self.progress.get('enabled'):
            self.start(self.progress.get('maximum',5),wait_for_ready=True)

    def pause(self):
        self.cancel.set()
        self.update(enabled=False,reason='正在暂停；已入库的战报继续保留')
        return self.snapshot()

    def close(self):
        self.cancel.set()
        if self.thread:
            self.thread.join(timeout=8)

    def counts(self):
        with self.archive.lock:
            db=self.archive.db
            reports=db.execute('SELECT COUNT(*) FROM battle_reports WHERE profile_id=?',(self.profile_id,)).fetchone()[0]
            batches=db.execute('SELECT COALESCE(SUM(receipts),0) FROM messages WHERE profile_id=? AND message_id=92',
                               (self.profile_id,)).fetchone()[0]
        return reports,batches

    def pending_at(self, at, span):
        with self.archive.lock:
            return self.archive.db.execute('''SELECT a.battle_id,a.event_at,a.player_name,a.union_id,a.heroes,
                b.player_name AS opponent,b.union_id AS opponent_union,b.heroes AS opponent_heroes
                FROM battle_reports r JOIN combat_sides a ON a.profile_id=r.profile_id AND a.battle_id=r.battle_id AND a.side='attack'
                JOIN combat_sides b ON b.profile_id=a.profile_id AND b.battle_id=a.battle_id AND b.side='defend'
                WHERE r.profile_id=? AND r.event_at>=? AND r.event_at<? AND a.npc=0 AND b.npc=0
                AND (a.union_id=? OR b.union_id=?)
                AND NOT EXISTS (SELECT 1 FROM report_bodies f WHERE f.profile_id=a.profile_id AND f.battle_id=a.battle_id AND f.body<>'')''',
                (self.profile_id,at,at+span,str(self.archive.profile['union_id']),str(self.archive.profile['union_id']))).fetchall()

    def received(self, ids, after):
        with self.archive.lock:
            return next((bid for bid in ids if self.archive.db.execute('''SELECT 1 FROM report_bodies
                WHERE profile_id=? AND battle_id=? AND body<>'' AND observed_at>=?''',
                (self.profile_id,bid,after)).fetchone()),None)

    def received_reply(self, after, expected):
        with self.archive.lock:
            rows=self.archive.db.execute('''SELECT f.battle_id,a.npc AS attack_npc,b.npc AS defend_npc,
                a.union_id AS attack_union,b.union_id AS defend_union FROM report_bodies f
                LEFT JOIN combat_sides a ON a.profile_id=f.profile_id AND a.battle_id=f.battle_id AND a.side='attack'
                LEFT JOIN combat_sides b ON b.profile_id=f.profile_id AND b.battle_id=f.battle_id AND b.side='defend'
                WHERE f.profile_id=? AND f.observed_at>=? AND f.body<>'' ORDER BY f.observed_at DESC LIMIT 3''',
                (self.profile_id,after)).fetchall()
        known=[row for row in rows if row['battle_id'] in expected]
        row=known[0] if len(known)==1 else rows[0] if len(rows)==1 else None
        if row is None:
            return None
        own=str(self.archive.profile['union_id'])
        return row['battle_id'], row['attack_npc']==row['defend_npc']==0 and own in (row['attack_union'],row['defend_union'])

    def pending_for_names(self, names):
        if not names:
            return []
        with self.archive.lock:
            db=self.archive.db
            players=[row[0] for row in db.execute('SELECT DISTINCT player_name FROM combat_sides WHERE profile_id=?',
                                                 (self.profile_id,)) if row[0]]
            matched=[name for name in players if any(matches_name(text,name) for text in names)]
            if not 2<=len(matched)<=12:
                return []
            slots=','.join('?' for _ in matched)
            return db.execute(f'''SELECT a.battle_id,a.event_at,a.player_name,a.union_id,a.heroes,
                b.player_name AS opponent,b.union_id AS opponent_union,b.heroes AS opponent_heroes
                FROM combat_sides a JOIN combat_sides b ON b.profile_id=a.profile_id AND b.battle_id=a.battle_id AND b.side='defend'
                WHERE a.profile_id=? AND a.side='attack' AND a.player_name IN ({slots}) AND b.player_name IN ({slots})
                AND a.npc=0 AND b.npc=0 AND (a.union_id=? OR b.union_id=?)
                AND NOT EXISTS (SELECT 1 FROM report_bodies f WHERE f.profile_id=a.profile_id AND f.battle_id=a.battle_id AND f.body<>'')
                ORDER BY a.event_at DESC LIMIT 200''',
                (self.profile_id,*matched,*matched,str(self.archive.profile['union_id']),str(self.archive.profile['union_id']))).fetchall()

    def poll(self, maximum, connection):
        def check():
            if self.cancel.is_set():
                raise RuntimeError('已暂停自动补采')
            if self.archive.profile_id!=self.profile_id or self.connection()!=connection:
                raise RuntimeError('账号、赛季或游戏连接已变化')
            if self.is_busy():
                raise RuntimeError('等待其他游戏操作完成')
        check()
        before=self.counts()
        with self.archive.lock:
            gaps=self.archive.intelligence.main_report_gaps()['items']
            bodies_before={row[0] for row in self.archive.db.execute(
                'SELECT battle_id FROM report_bodies WHERE profile_id=?',(self.profile_id,))}
        missed={(r['union_id'],r['player_name']):r['missed_marches'] for r in gaps}
        visited=set()
        saved=0
        try:
            with self.ocr_factory() as ocr, self.game_factory(connection,self.archive.profile['account_name'],
                    lambda expected:self.connection()==expected,ocr=ocr) as game:
                reports=self.reports_factory(game,ocr,check)
                screen=reports.open_alliance()
                # Do not silently collect a coordinate-filtered subset as full-alliance coverage.
                if reports.selected_point(screen) is not None or reports.search_panel(screen):
                    raise RuntimeError('战报存在坐标筛选，请清除筛选后继续全盟补采')
                previous=None
                stagnant=0
                for swipe in range(self.MAX_SWIPES+1):
                    check()
                    entries=reports.report_times(screen)
                    position=tuple((round(y),stamp) for y,stamp in entries)
                    stagnant=stagnant+1 if position and position==previous else 0
                    if stagnant>=3:
                        return '当前列表未继续加载；已保存记录，不代表本赛季已采完'
                    previous=position
                    candidates=[]
                    for y,stamp in entries:
                        interval=timestamp_interval(stamp)
                        key=(stamp,round(y))
                        if key in visited:
                            continue
                        names=reports.players(screen,y)
                        if not names or not all(names) or any(text.strip()=='守军' for text in names):
                            continue
                        if interval:
                            at,span=interval
                            self.update(earliest_visible_at=min(at,self.progress.get('earliest_visible_at') or at))
                            pending=self.pending_at(at,span)
                        else:
                            pending=self.pending_for_names(names)
                        matches=match_visible(pending,names,missed)
                        if matches:
                            candidates.append((matches[0]['priority'],y,key,matches))
                        elif not interval:
                            # Read-only fallback for unreadable dates/names. The game's
                            # structured reply supplies identity; no lineup association is guessed.
                            candidates.append(((0,False,0),y,key,[]))
                    for _,y,key,matches in sorted(candidates,reverse=True):
                        check()
                        visited.add(key)
                        ids=[r['battle_id'] for r in matches]
                        self.update(current=ids[0] if ids else None,
                                    reason='自动读取战报'+(' · '+matches[0]['player_name'] if matches else ' · 等待客户端确认编号'))
                        requested=time.time()
                        reports.click(screen,screen.width/2,y-55)
                        reports.wait(lambda s:'阵容详情' in s.text((s.width-273,731,s.width-66,788),high_contrast=True),
                                     '战报详情未打开，停止本次读取')
                        deadline=time.monotonic()+self.RESPONSE_SECONDS
                        actual=None
                        while time.monotonic()<deadline:
                            check()
                            game.undisturbed()
                            actual=self.received(ids,requested)
                            if actual in bodies_before:
                                actual=None
                            reply=self.received_reply(requested,ids)
                            if reply:
                                bid,eligible=reply
                                actual=bid if eligible and bid not in bodies_before else None
                                break
                            if actual and actual not in bodies_before:
                                break
                            self.cancel.wait(.2)
                        reports.close_detail()
                        screen=reports.screen()
                        if actual:
                            saved+=1
                            bodies_before.add(actual)
                            self.update(saved=self.progress['saved']+1,last_saved_id=actual,current=None)
                        else:
                            self.update(skipped=self.progress['skipped']+1,current=None)
                        if saved>=maximum:
                            return '本批全文已确认入库；空闲时继续补采'
                    if swipe==self.MAX_SWIPES:
                        return '达到本批翻页上限，保留当前列表位置继续补采'
                    screen=reports.advance(screen)
                    self.update(swipes=self.progress['swipes']+1)
                return '等待下一批自动补采'
        finally:
            after=self.counts()
            baseline=self.last_counts or before
            self.update(new_reports=self.progress['new_reports']+max(0,after[0]-baseline[0]),
                        report_batches=self.progress['report_batches']+max(0,after[1]-baseline[1]),
                        last_checked_at=time.time(),current=None)
            self.last_counts=after

    def run(self,maximum):
        delay=6
        try:
            while not self.cancel.wait(delay):
                connection=self.connection()
                if not connection:
                    self.update(reason='等待已核对的大神游戏连接')
                    delay=5
                elif self.is_busy() or not self.locator.lock.acquire(blocking=False):
                    self.update(reason='等待其他游戏操作完成')
                    delay=5
                else:
                    try:
                        self.update(reason=self.poll(maximum,connection))
                        delay=60
                    except (RuntimeError,OSError,ValueError) as error:
                        self.update(reason=str(error))
                        delay=5
                    finally:
                        self.locator.lock.release()
        except Exception:
            logging.exception('Android report monitor failed')
            self.update(reason='自动补采已停止，请检查本机日志')
        finally:
            self.update(active=False,current=None)
