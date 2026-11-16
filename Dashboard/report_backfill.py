"""Fill missing structured fields through the visible PC battle report list."""
import json
import logging
import re
import threading
import time
from datetime import datetime
from difflib import SequenceMatcher

from PIL import Image, ImageOps

from garrison_trial import SHANGHAI, Screen, TrialGame, compact, read_point
from game_locator import CoordinateControls, WindowsOcr
from lineup_prediction import meets_main_threshold
from model import write_json


REPORT_TIME = re.compile(r'20\d\d[/.-]\d{2}[/.-]\d{2}\d{2}:(?:\d{2}(?::\d{2})?)?')


class ReportSearchMiss(Exception):
    pass


def normalize_name(name):
    return ''.join(c for c in compact(name) if c.isalnum()).lower()


def matches_name(text, expected):
    text, expected = normalize_name(text), normalize_name(expected)
    if not expected:
        return False
    if expected in text:
        return True
    return len(expected) >= 4 and any(
        SequenceMatcher(None, expected, text[i:i + len(expected)]).ratio() >= .75
        for i in range(max(0, len(text) - len(expected) + 2)))


class ReportList:
    def __init__(self, game, ocr, check):
        self.game, self.ocr, self.check = game, ocr, check
        self.map_controls = CoordinateControls()

    def screen(self):
        self.check()
        self.game.undisturbed()
        return Screen(self.game, self.ocr)

    @staticmethod
    def title(screen):
        tabs = screen.text((92, 83, 640, 128), high_contrast=True)
        if sum(label in tabs for label in ('个人', '同盟', '收藏')) < 2:
            return ''
        centers = (150, 368, 570)
        def highlight(x):
            strip = screen.crop((x-38, 125, x+38, 137)).convert('RGB')
            return sum(max(0, r-max(g, b)-20) for r, g, b in strip.getdata())
        scores = [highlight(x) for x in centers]
        selected = max(range(len(scores)), key=scores.__getitem__)
        if scores[selected] == 0:
            return ''
        return ('个人战报', '同盟战报', '收藏战报')[selected]

    def wait(self, predicate, error, seconds=4, failure=RuntimeError):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            screen = self.screen()
            if predicate(screen):
                return screen
            time.sleep(.2)
        raise failure(error)

    def click(self, screen, x, y):
        self.check()
        screen.hit(self.game, x, y)

    def open_alliance(self):
        screen = self.screen()
        title = self.title(screen)
        if '同盟战报' in title:
            return screen
        if '个人战报' not in title and '收藏战报' not in title:
            if not self.is_map(screen):
                raise RuntimeError('请让 PC 游戏停在地图或战报列表')
            self.click(screen, 101, 644)
            screen = self.wait(lambda s: '战报' in self.title(s), '未识别到游戏战报列表')
        if '同盟战报' not in self.title(screen):
            self.click(screen, 371, 107)
            screen = self.wait(lambda s: '同盟战报' in self.title(s), '未识别到同盟战报列表')
        return screen

    def is_map(self, screen):
        menu = screen.text((35, 675, 388, 795), high_contrast=True)
        return sum(name in menu for name in ('武将', '库藏', '内政', '势力', '国家')) >= 2 or bool(
            self.map_controls.find(screen.image, 'search'))

    def return_to_map(self):
        screen = self.screen()
        if '同盟战报' in self.title(screen):
            self.click(screen, screen.width-65, 40)

    @staticmethod
    def selected_point(screen):
        return read_point(screen.text((screen.width-580, 91, screen.width-399, 128), high_contrast=True))

    @staticmethod
    def search_panel(screen):
        return '搜索' in screen.text((screen.width-220, 689, screen.width-102, 729), high_contrast=True)

    def clear_filter(self, screen):
        if self.selected_point(screen) is not None:
            self.click(screen, screen.width-395, 107)
            screen = self.wait(lambda s: self.selected_point(s) is None, '无法清除上一次战报筛选', failure=ReportSearchMiss)
        return screen

    def filter(self, point):
        screen = self.clear_filter(self.open_alliance())
        if not self.search_panel(screen):
            self.click(screen, screen.width-17, 407)
        self.wait(self.search_panel, '未识别到同盟战报坐标筛选', failure=ReportSearchMiss)
        time.sleep(.3)
        screen = self.screen()
        for offset, value in zip((429, 309), point):
            self.click(screen, screen.width-offset, 709)
            time.sleep(.22)
            self.check()
            self.game.enter_number(value)
            screen = self.screen()
        self.click(screen, screen.width-166, 709)
        return self.wait(lambda s: self.selected_point(s) == point, '游戏中的战报筛选坐标核对失败', failure=ReportSearchMiss)

    def report_times(self, screen):
        center = screen.width / 2
        image = screen.crop((center-90, 150, center+90, 785)).convert('L')
        image = ImageOps.autocontrast(image).resize((360, 1270), Image.Resampling.LANCZOS)
        lines = []
        for word in sorted(self.ocr.read(image), key=lambda w: w['y']):
            mid = word['y'] + word['h']/2
            if lines and abs(mid - lines[-1][0]) < 15:
                lines[-1][1].append(word)
            else:
                lines.append((mid, [word]))
        results = []
        for y, words in lines:
            value = compact(''.join(w['t'] for w in sorted(words, key=lambda w: w['x'])))
            value = value.translate(str.maketrans({'l': '1', 'I': '1', 'g': '9', 'O': '0', '：': ':'}))
            match = REPORT_TIME.search(value)
            if match:
                results.append((y/2+150, match[0].replace('.', '/').replace('-', '/')))
        return results

    @staticmethod
    def players(screen, timestamp_y):
        center = screen.width / 2
        top = timestamp_y - 195
        bottom = timestamp_y - 158
        if top < 150:
            return None
        return (screen.text((center-300, top, center-112, bottom), high_contrast=True),
                screen.text((center+76, top, center+235, bottom), high_contrast=True))

    def open_report(self, target):
        when = datetime.fromtimestamp(target['event_at'], SHANGHAI)
        expected = when.strftime('%Y/%m/%d%H:%M:%S')
        def open_match(screen, entries, names_required):
            for y, timestamp in entries:
                exact = timestamp == expected
                if not exact and (not target['unique_pair_hour'] or not expected.startswith(timestamp)):
                    continue
                names = self.players(screen, y)
                if names and sum(any(matches_name(text, player) for text in names)
                                 for player in (target['player_name'], target['opponent'])) >= (names_required if exact else 2):
                    self.click(screen, screen.width/2, y-55)
                    return True
            return False

        screen = self.clear_filter(self.open_alliance())
        if self.search_panel(screen):
            self.click(screen, screen.width-17, 407)
            screen = self.wait(lambda s: not self.search_panel(s), '无法收起同盟战报筛选', failure=ReportSearchMiss)
        entries = self.report_times(screen)
        if open_match(screen, entries, 2):
            return True
        screen = self.filter(tuple(target['coord']))
        same_view = 0
        previous = None
        for _ in range(35):
            self.check()
            entries = self.report_times(screen)
            if open_match(screen, entries, 1):
                return True
            position = tuple((round(y), timestamp) for y, timestamp in entries)
            same_view = same_view + 1 if position and position == previous else 0
            if same_view >= 4 or entries and entries[0][1][:13] < expected[:13]:
                break
            previous = position
            self.check()
            self.game.scroll_sources(screen.frame, round((screen.width-305)*screen.scale), round(604*screen.scale))
            screen = self.screen()
        raise ReportSearchMiss('列表中未定位到这份战报')

    def close_detail(self):
        screen = self.wait(lambda s: '阵容详情' in s.text((s.width-273, 731, s.width-66, 788), high_contrast=True),
                           '全文已入库，但当前战报界面已变化')
        self.click(screen, screen.width-65, 40)
        self.wait(lambda s: '同盟战报' in self.title(s), '未返回同盟战报列表')


class ReportBackfill:
    POLL_SECONDS = 60
    RETRY_SECONDS = 300

    def __init__(self, archive, locator, connection, is_busy):
        self.archive, self.locator, self.connection, self.is_busy = archive, locator, connection, is_busy
        self.lock = threading.Lock()
        self.cancel = threading.Event()
        self.thread = None
        self.path = archive.root / 'runtime' / f'report-monitor-{archive.profile_id}.json'
        since = None
        if self.path.is_file():
            try:
                since = float(json.loads(self.path.read_text(encoding='utf-8'))['since'])
                if not 0 < since <= time.time():
                    since = None
            except (OSError, ValueError, TypeError, KeyError):
                logging.warning('Unable to restore the new report collection start time')
        self.progress = {'active': False, 'reason': '开启后只采集新产生的战报', 'saved': 0,
                         'skipped': 0, 'current': None, 'total': 0, 'since': since,
                         'last_checked_at': None, 'last_saved_id': None}

    def snapshot(self):
        with self.lock:
            result = dict(self.progress)
        with self.archive.lock:
            result['alliance_reports'] = self.archive.intelligence.alliance_report_coverage()
        return result

    def start(self, maximum=5):
        if type(maximum) is not int or not 1 <= maximum <= 20:
            raise ValueError('每批补报数量须为 1～20')
        if not self.connection():
            raise ValueError('请保持已核对角色的 PC 游戏在线')
        with self.lock:
            if self.progress['active']:
                raise ValueError('新战报自动采集已开启')
            since = self.progress['since'] or time.time()
            if self.progress['since'] is None:
                write_json(self.path, {'since': since})
            self.cancel.clear()
            self.progress.update(active=True, reason='监听新战报，游戏空闲时补齐队伍资料', since=since)
            self.thread = threading.Thread(target=self.run, args=(maximum, self.archive.profile_id, since),
                                           name='game-report-backfill', daemon=True)
            self.thread.start()
        return self.snapshot()

    def pause(self):
        self.cancel.set()
        return self.snapshot()

    def close(self):
        self.cancel.set()
        if self.thread:
            self.thread.join(timeout=10)

    def update(self, **values):
        with self.lock:
            self.progress.update(values)

    def queue(self, profile, since, deferred, limit):
        with self.archive.lock:
            rows = self.archive.db.execute('''SELECT a.battle_id,a.event_at,a.wid,a.player_name,
                b.player_name AS opponent,a.heroes,b.heroes AS opponent_heroes,a.union_id,b.union_id AS opponent_union,
                NOT EXISTS (SELECT 1 FROM combat_sides x JOIN combat_sides y ON y.profile_id=x.profile_id
                    AND y.battle_id=x.battle_id AND y.side='defend'
                    WHERE x.profile_id=a.profile_id AND x.side='attack' AND x.battle_id<>a.battle_id
                    AND x.event_at>=a.event_at-(CAST(a.event_at AS INTEGER)%3600)
                    AND x.event_at<a.event_at-(CAST(a.event_at AS INTEGER)%3600)+3600
                    AND ((x.player_name=a.player_name AND y.player_name=b.player_name)
                        OR (x.player_name=b.player_name AND y.player_name=a.player_name))) AS unique_pair_hour
                FROM combat_sides a JOIN combat_sides b ON a.profile_id=b.profile_id
                    AND a.battle_id=b.battle_id AND b.side='defend'
                WHERE a.profile_id=? AND a.side='attack' AND a.event_at>=? AND a.wid>0
                AND a.npc=0 AND b.npc=0 AND a.player_name<>'' AND b.player_name<>''
                AND (a.union_id=? OR b.union_id=?)
                ORDER BY a.event_at DESC,CAST(a.battle_id AS INTEGER) DESC''',
                (profile, since, str(self.archive.profile['union_id']), str(self.archive.profile['union_id']))).fetchall()
            gaps = self.archive.intelligence.main_report_gaps()['items'] if rows else []
        missed = {(g['union_id'], g['player_name']): g['missed_marches'] for g in gaps}
        now = time.monotonic()
        targets = []
        for row in rows:
            with self.archive.lock:
                if self.archive.intelligence.detail_received(row['battle_id']):continue
            if deferred.get(row['battle_id'], 0) > now:
                continue
            target = dict(row)
            target['coord'] = divmod(target.pop('wid'), 10000)
            if not all(target['coord']):
                continue
            misses = max(missed.get((row['union_id'], row['player_name']), 0),
                         missed.get((row['opponent_union'], row['opponent']), 0))
            teams = [json.loads(row[key]) for key in ('heroes', 'opponent_heroes')]
            both_main = all(meets_main_threshold(team) for team in teams)
            targets.append(((misses, both_main, row['event_at']), target))
        targets.sort(key=lambda item: item[0], reverse=True)
        return [target for _, target in targets[:limit]]

    def received(self, profile, battle_id):
        with self.archive.lock:
            return profile==self.archive.profile_id and self.archive.intelligence.detail_received(battle_id)

    def poll(self, maximum, profile, since, connection, deferred):
        def check():
            if self.cancel.is_set():
                raise RuntimeError('已暂停新战报采集')
            if self.archive.profile_id != profile or self.connection() != connection:
                raise RuntimeError('游戏连接或档案已变化')
            if self.is_busy():
                raise RuntimeError('等待其他游戏操作完成')

        with TrialGame(connection) as game:
            if game.foreground() != game.hwnd or game.idle() < 4000:
                return '正在监听，游戏空闲后补齐队伍资料'
            game.input_marker = game.input_tick()
            with WindowsOcr() as ocr:
                reports = ReportList(game, ocr, check)
                initial = reports.screen()
                from_map = reports.is_map(initial)
                reports.clear_filter(reports.open_alliance())
                self.cancel.wait(.8)
                targets = self.queue(profile, since, deferred, maximum)
                self.update(total=len(targets), last_checked_at=time.time())
                for target in targets:
                    check()
                    battle_id = target['battle_id']
                    if self.received(profile, battle_id):
                        continue
                    self.update(current=battle_id, reason=f'查找新战报 {battle_id} · {target["player_name"]}')
                    try:
                        reports.open_report(target)
                    except ReportSearchMiss as error:
                        deferred[battle_id] = time.monotonic() + self.RETRY_SECONDS
                        with self.lock:
                            self.progress['skipped'] += 1
                        logging.info('Report %s will be retried: %s', battle_id, error)
                        continue
                    self.update(reason=f'等待队伍资料 {battle_id} 入库')
                    deadline = time.monotonic() + 12
                    while not self.received(profile, battle_id):
                        check()
                        if time.monotonic() >= deadline:
                            deferred[battle_id] = time.monotonic() + self.RETRY_SECONDS
                            reports.close_detail()
                            with self.lock:
                                self.progress['skipped'] += 1
                            break
                        self.cancel.wait(.2)
                    else:
                        with self.lock:
                            self.progress['saved'] += 1
                            self.progress['last_saved_id'] = battle_id
                        reports.close_detail()
                if from_map:
                    reports.clear_filter(reports.open_alliance())
                    reports.return_to_map()
        return '持续监听新战报' if targets else '已检查同盟战报列表，等待新战报'

    def run(self, maximum, profile, since):
        reason = '已暂停新战报采集'
        deferred = {}
        try:
            while not self.cancel.is_set() and self.archive.profile_id == profile:
                connection = self.connection()
                if not connection:
                    self.update(reason='等待 PC 游戏连接', current=None)
                elif self.is_busy() or not self.locator.lock.acquire(blocking=False):
                    self.update(reason='等待其他游戏操作完成', current=None)
                else:
                    try:
                        status = self.poll(maximum, profile, since, connection, deferred)
                    except (ReportSearchMiss, RuntimeError, OSError, ValueError) as error:
                        status = str(error)
                        logging.info('Report collection interrupted: %s', status)
                    finally:
                        self.locator.lock.release()
                    self.update(reason=status, current=None)
                now = time.monotonic()
                deferred = {bid: until for bid, until in deferred.items() if until > now}
                self.cancel.wait(self.POLL_SECONDS)
        except Exception:
            reason = '新战报采集遇到错误，详见本机日志'
            logging.exception('Report collection failed')
        finally:
            self.update(active=False, reason=reason, current=None)
