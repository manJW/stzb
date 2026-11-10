"""One-shot, local PC garrison for a designated own squad."""
import ctypes
import json
import logging
import re
import threading
import time
import unicodedata
import zlib
from ctypes import wintypes
from datetime import datetime
from math import dist
from pathlib import Path
from zoneinfo import ZoneInfo

from PIL import Image, ImageChops, ImageOps, ImageStat

from archive import table_updates
from combat_loadout import named_hero
from game_locator import ASSETS, CoordinateControls, OcrCoordinateControls, WindowsGame, WindowsOcr, coordinates
from lineup_prediction import predict_alerts
from model import write_json

TEAM = '79000215'
ROLE = '7900021'
OWNER = '71021'
TROOPS = (3, 3, 0)
TRIAL_SECONDS = 20 * 60
SHANGHAI = ZoneInfo('Asia/Shanghai')
ARMED = {'waiting', 'reading'}
TRACKING = {'submitting', 'marching', 'arrived', 'holding', 'returning', 'arrival_unknown', 'submission_unknown'}
ACTIVE = ARMED | (TRACKING - {'arrival_unknown', 'submission_unknown'})
TARGET_REQUEST_SECONDS = 60
TARGET_MIN_REMAINING = 20
RECRUIT_OPTION = '返回立即征兵'
RECRUIT_TOAST_SECONDS = 2.2


def compact(value):
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', value)).translate(
        str.maketrans({'，': ',', '、': ',', '華': '华', '駐': '驻', '時': '时', '間': '间'}))


def read_point(value):
    pairs = re.findall(r'(?<!\d)(\d{1,4})\s*[,，、]\s*(\d{1,4})(?!\d)', value)
    return tuple(map(int, pairs[0])) if len(pairs) == 1 else None


def read_duration(value):
    match = re.search(r'(?<!\d)(\d{1,2}):([0-5]\d):([0-5]\d)(?!\d)', value)
    if not match:
        return None
    h, m, s = map(int, match.groups())
    return 3600*h + 60*m + s


def read_arrival(value):
    match = re.search(r'(\d{4})[/.-](\d{1,2})[/.-](\d{1,2})(\d{1,2}):([0-5]\d):([0-5]\d)', compact(value))
    if not match:
        return None
    try:
        return datetime(*map(int, match.groups()), tzinfo=SHANGHAI).timestamp()
    except ValueError:
        return None


class TrialGame(WindowsGame):
    class LastInput(ctypes.Structure):
        _fields_ = [('size', wintypes.UINT), ('tick', wintypes.DWORD)]

    def input_tick(self):
        info = self.LastInput(ctypes.sizeof(self.LastInput), 0)
        if not self.user.GetLastInputInfo(ctypes.byref(info)):
            raise RuntimeError('无法检查桌面输入状态')
        return info.tick

    def idle(self):
        return (self.kernel.GetTickCount() - self.input_tick()) & 0xffffffff

    def activate(self):
        if self.idle() < 4000:
            raise RuntimeError('鼠标或键盘正在使用')
        self.input_marker = self.input_tick()
        super().activate()

    def undisturbed(self):
        self.require_foreground()
        if self.input_tick() != self.input_marker:
            raise RuntimeError('检测到用户操作，暂停本次试跑')

    def click(self, frame, x, y):
        self.undisturbed()
        super().click(frame, x, y)
        self.input_marker = self.input_tick()

    def enter_number(self, number):
        self.undisturbed()
        super().enter_number(number)
        self.input_marker = self.input_tick()

    def right_click(self, frame, x, y):
        self.undisturbed()
        self.move_to_control(frame, x, y)
        self.user.mouse_event(0x0008, 0, 0, 0, 0)
        self.user.mouse_event(0x0010, 0, 0, 0, 0)
        time.sleep(.12)
        self.input_marker = self.input_tick()

    def scroll_sources(self, frame, x, y):
        self.undisturbed()
        self.move_to_control(frame, x, y)
        self.user.mouse_event(0x0800, 0, 0, ctypes.c_uint(-120).value, 0)
        time.sleep(.2)
        self.input_marker = self.input_tick()


class Screen:
    """Coordinates are based on the 810-pixel-high PC client content, excluding its title bar."""
    def __init__(self, game, ocr):
        self.frame = game.capture()
        self.captured_at = time.time()
        self.image = self.frame[0]
        self.ocr = ocr
        self.scale = self.image.height / 810
        self.width = self.image.width / self.scale
        if not 1200 <= self.width <= 1600 or not .75 <= self.scale <= 1.5:
            raise RuntimeError('窗口比例超出已核对的 PC 布局')

    def crop(self, box):
        return self.image.crop(tuple(round(n*self.scale) for n in box))

    def text(self, box, *, high_contrast=False):
        image = self.crop(box)
        if high_contrast:
            image = ImageOps.autocontrast(image.convert('L'))
        image = image.resize((round(image.width*1.5), round(image.height*1.5)), Image.Resampling.LANCZOS)
        return compact(''.join(w['t'] for w in self.ocr.read(image)))

    def hit(self, game, x, y):
        game.click(self.frame, round(x*self.scale), round(y*self.scale))

    def garrison_action(self, point):
        center = self.width/2
        info = self.text((center-82, 302, center+105, 331), high_contrast=True)
        action = self.text((center+155, 353, center+277, 403))
        selected = read_point(info)
        return '驻守' in action and selected in (None, point)

    def target(self, expected=None):
        center = self.width/2
        first = None
        for box in ((center-30,262,center+137,296), (center,261,center+145,300),
                    (center-132,260,center+135,309), (center-92,265,center+144,320)):
            point = read_point(self.text(box, high_contrast=True))
            if point is not None:
                if expected is None or point == expected:
                    return point
                first = first or point
        return first

    def team_card(self):
        center = self.width/2
        return self.text((center+15, 720, center+88, 747), high_contrast=True) == '200'

    def roster_cards(self):
        with Image.open(ASSETS / 'garrison-team-slots.png') as strip:
            references = [strip.crop((14*i, 0, 14*(i+1), 24)) for i in range(5)]
        matches = {}
        ambiguous = set()
        for index in range(-4, 5):
            card = self.width/2+index*108.5
            if not 105 < card < self.width-75:
                continue
            x = round(card-96)
            scores = [float('inf')]*5
            for dx in range(-4, 5):
                for dy in range(-5, 6):
                    tag = self.crop((x-7+dx, 565+dy, x+7+dx, 589+dy)).convert('RGB')
                    tag = tag.resize((14,24), Image.Resampling.LANCZOS)
                    for i, reference in enumerate(references):
                        distance = sum(ImageStat.Stat(ImageChops.difference(tag, reference)).mean)/3
                        scores[i] = min(scores[i], distance)
            ((best, slot), (second, _)) = sorted((score, i) for i, score in enumerate(scores, 1))[:2]
            if best <= 11 and second-best >= 7:
                if slot in matches:
                    ambiguous.add(slot)
                matches[slot] = card
        return {slot:card for slot,card in matches.items() if slot not in ambiguous}

    def source_rows(self, name, distance):
        rows = []
        for y in (153,266,378):
            left = self.width-272
            label = self.text((left,y-18,left+170,y+25))
            travel = self.text((left-4,y+28,left+62,y+71), high_contrast=True)
            if (name and label == compact(name)) or travel == f'{distance:.1f}':
                rows.append(y+18)
        return rows


    def matches_heroes(self):
        # Both hero names are printed in fixed positions in the PC preview.
        with Image.open(ASSETS / 'garrison-test-heroes.png') as reference:
            for i, x in enumerate((43, 218)):
                glyphs = reference.crop((i*25, 0, (i+1)*25, 58))
                for dx in (-2, 0, 2):
                    area = (x+dx, 497, x+dx+25, 555)
                    actual = self.crop(area).convert('L').resize(glyphs.size, Image.Resampling.LANCZOS)
                    if ImageStat.Stat(ImageChops.difference(actual, glyphs)).mean[0] <= 14:
                        break
                else:
                    return False
        return True

    def preview(self, point, *, test_only=True):
        center = self.width/2
        if self.target(point) != point:
            raise RuntimeError('驻守预览的目标坐标核对失败')
        strength = self.text((center-170, 471, center+50, 519))
        if test_only:
            if not re.search(r'(?<!\d)200(?!\d)', strength) or not self.matches_heroes():
                raise RuntimeError('驻守预览中的兵力或武将与测试队不一致')
        elif not re.search(r'[1-9]\d*', strength):
            raise RuntimeError('驻守预览中无法核对有效兵力')
        timing = self.text((center-155, 603, center+235, 683))
        travel = read_duration(timing)
        arrival = read_arrival(timing)
        if not travel or not arrival or abs(arrival-self.captured_at-travel) > 5:
            raise RuntimeError('驻守预览的行军时长和到达时刻未能核对')
        return travel, arrival

    def path_preview(self, source, target, travel):
        center = self.width/2
        head = self.text((center-100, 94, center+106, 142))
        left = center-502
        clock = self.text((left-8, 234, left+107, 270), high_contrast=True)
        origin = self.text((left, 302, left+143, 339), high_contrast=True)
        destination = self.text((left, 375, left+143, 410), high_contrast=True)
        if ('预览' not in head or read_duration(clock) != travel
                or read_point(origin) != source or read_point(destination) != target):
            raise RuntimeError('路径预览的起点、终点或用时未能核对')

    def has_confirm_button(self):
        left = self.width-265
        with Image.open(ASSETS / 'garrison-confirm.png') as reference:
            for dx in (-3,0,3):
                for dy in (-3,0,3):
                    area = self.crop((left+dx,728+dy,left+133+dx,766+dy)).convert('L')
                    area = area.resize(reference.size, Image.Resampling.LANCZOS)
                    if ImageStat.Stat(ImageChops.difference(area, reference)).mean[0] <= 15:
                        return True
        return '20/驻守' in self.text((self.width-365,718,self.width-87,772), high_contrast=True)

    def checked_options(self):
        if not self.has_confirm_button():
            raise RuntimeError('没有核对到消耗 20 体力的驻守按钮')
        checked = set()
        for y, option_name in ((226,'撤退立即返回'), (303,RECRUIT_OPTION), (381,'行军加速')):
            x = self.width-92
            option = self.crop((x-10, y-10, x+10, y+10)).convert('L')
            if ImageStat.Stat(option.point(lambda p: 255 if p > 155 else 0)).mean[0] > 9:
                checked.add(option_name)
        return checked

    def toggle_recruit(self, game):
        self.hit(game, self.width-92, 303)

    def back_from_preview(self, game):
        game.right_click(self.frame, round((self.width/2-232)*self.scale), round(103*self.scale))


def read_garrison_preview(game, ocr, target, *, test_only):
    # After changing screens, the coordinate label can move as the camera settles.
    for _ in range(3):
        game.undisturbed()
        screen = Screen(game, ocr)
        if screen.target(target) == target:
            travel, arrival = screen.preview(target, test_only=test_only)
            return screen, travel, arrival
        time.sleep(.35)
    raise RuntimeError('驻守预览的目标坐标核对失败')


def open_garrison(game, ocr, locator, source, target, *, slot=None, source_name=None):
    ui = OcrCoordinateControls(ocr)
    for dx, dy in ((-39, 27), (-60, 25), (39, 27), (0, 47), (-24, 19), (0, 0), (-30, -20)):
        locator.navigate(game, target, ui)
        time.sleep(.62)
        screen = Screen(game, ocr)
        if not ui.find(screen.image, 'search'):
            raise RuntimeError('跳转后未核对到游戏地图')
        screen.hit(game, screen.width/2+dx, 405+dy)
        time.sleep(.65)
        screen = Screen(game, ocr)
        if not screen.garrison_action(target):
            continue
        screen.hit(game, screen.width/2+204, 378)
        time.sleep(.5)
        screen = Screen(game, ocr)
        selected = screen.target(target)
        if selected == target:
            break
        if selected is not None and '出发点' in screen.text((screen.width-280, 86, screen.width-55, 124)):
            screen.hit(game, screen.width-67, 42)
            time.sleep(.45)
    else:
        raise RuntimeError('地图上没有核对到该坐标的驻守入口')

    if slot is None:
        if not screen.team_card():
            raise RuntimeError('出发点列表未识别到 200 兵测试队')
        card = screen.width/2
    else:
        screen, card = find_roster_card(game, ocr, screen, slot, source_name, dist(source, target), target)
    screen.hit(game, card, 667)
    time.sleep(.5)
    screen, travel, _ = read_garrison_preview(game, ocr, target, test_only=slot is None)
    screen.hit(game, screen.width/2, 91)
    time.sleep(.35)
    screen = Screen(game, ocr)
    screen.path_preview(source, target, travel)
    screen.hit(game, screen.width-112, 118)
    time.sleep(.35)
    screen, latest, preview_at = read_garrison_preview(game, ocr, target, test_only=slot is None)
    if latest != travel:
        raise RuntimeError('关闭路径预览后行军时长有变化')
    return screen, travel, preview_at


def find_roster_card(game, ocr, screen, slot, name, distance, target):
    for page in range(5):
        if screen.target(target) != target or '出发点' not in screen.text((screen.width-280,86,screen.width-55,124)):
            raise RuntimeError('无法核对游戏出发点选择界面')
        cards = screen.roster_cards()
        if page == 0 and len(cards) == 1 and slot in cards:
            return screen, cards[slot]
        for y in screen.source_rows(name, distance):
            screen.hit(game, screen.width-195, y)
            time.sleep(.45)
            screen = Screen(game, ocr)
            if screen.target(target) != target:
                raise RuntimeError('切换出发点后目标坐标变化')
            card = screen.roster_cards().get(slot)
            if card is not None:
                return screen, card
        if page < 4:
            game.scroll_sources(screen.frame, round((screen.width-160)*screen.scale), round(362*screen.scale))
            screen = Screen(game, ocr)
    raise RuntimeError(f'游戏出发点列表未核对到 {slot} 队')


def restore_map(game, ocr, target):
    """Dismiss a recognized trial screen after a skipped order."""
    ui = CoordinateControls()
    for _ in range(3):
        game.undisturbed()
        screen = Screen(game, ocr)
        if ui.find(screen.image, 'search'):
            return
        center = screen.width/2
        if '预览' in screen.text((center-100, 94, center+106, 142)):
            left = center-502
            if read_point(screen.text((left, 375, left+143, 410), high_contrast=True)) != target:
                return
            screen.hit(game, screen.width-112, 118)
        elif screen.has_confirm_button() and screen.target(target) in (target, None):
            screen.back_from_preview(game)
        elif (screen.target(target) == target and
              '出发点' in screen.text((screen.width-280, 86, screen.width-55, 124))):
            screen.hit(game, screen.width-67, 42)
        else:
            return
        time.sleep(.5)


def same_alert(alert, request):
    return all(alert[key] == request[key] for key in ('army_id', 'coord', 'end'))


def team_slot(team_id):
    match = re.fullmatch(re.escape(ROLE)+r'([1-5])', team_id) if isinstance(team_id, str) else None
    return int(match[1]) if match else None


def select_team(snapshot, team_id=TEAM, *, fresh=True, test_only=True):
    if team_slot(team_id) is None:
        return None
    for army in snapshot['armies']:
        if army['id'] != team_id:
            continue
        codes = tuple(t['code'] for t in army['troops'])
        if (army['owner'] == OWNER and army['relation'] == 'self' and army['current_connection']
                and (not fresh or not army['stale'])
                and (army['kind'] == 'stay' or (army['state_code'], army['target_type']) == (5, 0))
                and army['position'] and army['position'] == army['from'] == army['target']
                and any(codes) and (not test_only or codes == TROOPS)):
            return army
        break
    return None


def own_hero_roster(archive, army, reports):
    codes = [t['code'] for t in army['troops']]
    slots = {}
    cohort = None
    rows = archive.db.execute('''SELECT e.operation,e.body,e.observed_at,e.message_ref
        FROM client_table_events e JOIN messages m ON m.id=e.message_ref
        WHERE e.profile_id=? AND e.table_name='Tb_army' AND e.record_key=? AND m.source='pc-live'
        ORDER BY e.observed_at DESC''', (archive.profile_id, army['id']))
    for row in rows:
        body = json.loads(row['body'])
        if row['operation'] == 1 and len(body) > 7 and body[1:3] == [int(OWNER), int(ROLE)]:
            fields = dict(enumerate(body))
        elif row['operation'] == 2 and body[:2] == [0, int(army['id'])]:
            fields = dict(zip(body[2::2], body[3::2]))
        else:
            break
        # The PC table orders these slots as front, middle and base.
        for pos, field in enumerate((7, 6, 5)):
            if field in fields:
                slots.setdefault(pos, fields[field])
        if (len(slots) == 0 and row['operation'] == 2 and army['state_code'] == 5 and fields.get(11) == 5
                and fields.get(2) == army['position'][0]*10000+army['position'][1]
                and fields.get(79) == army.get('end') and abs(row['observed_at']-fields[79]) <= 3):
            cohort = (row['message_ref'], fields[79])
        if len(slots) == 3 or row['operation'] == 1:
            break
    assigned = tuple(slots.get(pos) for pos in range(3))
    linked = None
    if cohort and not slots:
        ref, at = cohort
        payload = archive.db.execute("SELECT payload FROM messages WHERE id=?", (ref,)).fetchone()['payload']
        updates = list(table_updates(json.loads(zlib.decompress(payload))))
        if sum(table == 'Tb_army' for _, table, _ in updates) == 1:
            synced = set()
            for operation, table, body in updates:
                if operation == 2 and table == 'Tb_hero' and len(body) >= 6 and body[0] == 0:
                    fields = dict(zip(body[2::2], body[3::2]))
                    if 7 in fields and fields.get(9) == at:
                        synced.add(body[1])
            if len(synced) == sum(code > 0 for code in codes):
                linked = synced
    exact = all(type(hero) is int and (hero > 0) == (code > 0) for hero, code in zip(assigned, codes))
    historical = None
    for report in reports:
        ids = str(report['hero_instances']).split(',')[1:4]
        heroes = [named_hero(hero) for hero in json.loads(report['heroes'])]
        # Legacy combat summaries can lack the troop code for an occupied slot.
        # Treat that as unknown and keep checking later reports for this squad.
        observed = {hero['position']: hero.get('troop_code') for hero in heroes}
        if [observed.get(pos, 0) for pos in (1, 2, 3)] != codes:
            continue
        names = [hero['name'] for hero in sorted(heroes, key=lambda h: h['position'])]
        if not names or any(name.startswith('武将#') for name in names):
            continue
        if len(ids) == 3 and all(item.isascii() and item.isdecimal() for item in ids):
            instances = tuple(map(int, ids))
            if exact and assigned == instances:
                return names, '队伍编成'
            if linked and {item for item in instances if item} == linked:
                return names, '近期行军核对'
        if historical is None and not exact and not linked and report['army_id'] == army['id']:
            historical = names
    return (historical, '同队历史') if historical else ([], None)


def available_garrison_teams(snapshot, archive=None):
    if not snapshot['reliable'] or snapshot['health'].get('resync_required') or snapshot['health'].get('capture_source') != 'pc':
        return []
    forts = {tuple(fort['coord']):fort['name'] for fort in snapshot.get('forts', []) if fort['name']}
    teams = []
    for row in snapshot['armies']:
        if row['relation'] != 'self':
            continue
        army = select_team(snapshot, row['id'], test_only=False)
        if army is not None:
            teams.append((army, {'id':army['id'], 'slot':team_slot(army['id']), 'coord':army['position'],
                                 'source_name':forts.get(tuple(army['position'])), 'troops':army['troops']}))
    if archive is not None and teams and str(archive.profile['account_role_id']) == ROLE:
        with archive.lock:
            reports = archive.db.execute('''SELECT c.army_id,c.heroes,
                json_extract(b.data,'$.' || c.side || '_idu') AS hero_instances
                FROM combat_sides c JOIN battle_reports b ON b.profile_id=c.profile_id AND b.battle_id=c.battle_id
                WHERE c.profile_id=? AND c.union_id=? AND c.player_name=?
                ORDER BY c.event_at DESC LIMIT 100''',
                (archive.profile_id, str(archive.profile['union_id']), archive.profile['account_name'])).fetchall()
            for army, team in teams:
                team['hero_names'], team['hero_source'] = own_hero_roster(archive, army, reports)
    return [team for _, team in teams]


class GarrisonTrial:
    def __init__(self, state, archive, locator, ready, directory):
        self.state, self.archive, self.locator, self.ready = state, archive, locator, ready
        self.path = directory / 'garrison-trial.json'
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.status = {'phase':'off', 'team_id':TEAM, 'message':'驻守试跑未启动'}
        if self.path.is_file():
            try:
                self.status = json.loads(self.path.read_text(encoding='utf-8'))
                if self.status['phase'] in ARMED:
                    self.update('interrupted', '服务已重启，请查阅试跑记录')
            except (ValueError, KeyError, OSError):
                logging.exception('Unable to read the garrison trial record')
        self.thread = threading.Thread(target=self.run, daemon=True, name='garrison-trial')

    def update(self, phase, message, **fields):
        self.status.update(phase=phase, message=message, updated_at=time.time(), **fields)
        write_json(self.path, self.status)

    def snapshot(self):
        with self.lock:
            return dict(self.status)

    def arm(self, requested=None, team_id=None, *, allow_recruit=False, preview_only=False):
        fields = {}
        if type(allow_recruit) is not bool or (allow_recruit and team_id is None):
            raise ValueError('返回征兵选项无效')
        until = time.time()+TRIAL_SECONDS
        if team_id is not None and (requested is None or team_slot(team_id) is None):
            raise ValueError('请选择当前地图中的自有队伍')
        if preview_only and team_id is None:
            raise ValueError('请选择要预览的自有队伍')
        if requested is not None:
            alert = self.requested_alert(requested, team_id, preview_only=preview_only)
            until = time.time()+TARGET_REQUEST_SECONDS
            if not preview_only:
                until = min(alert['end'], until)
            fields = {'requested_alert':{key:alert[key] for key in ('army_id', 'coord', 'end')},
                      'enemy_id':alert['army_id'], 'enemy_name':alert['player_name'],
                      'target':alert['coord'], 'enemy_arrival':alert['end']}
        with self.lock:
            if self.status['phase'] in ACTIVE:
                raise ValueError('已有一次驻守任务在进行')
            if str(self.archive.profile['account_role_id']) != ROLE:
                raise ValueError('当前赛季角色与测试队绑定不符')
            self.status = {'team_id':team_id or TEAM, 'armed_at':time.time(), 'until':until, **fields}
            if team_id is not None:
                self.status['team_slot'] = team_slot(team_id)
                self.status['allow_recruit'] = allow_recruit
            if preview_only:
                self.status['preview_only'] = True
            message = '等待鼠标键盘空闲，随后核对本条来袭' if fields else '等待主力来袭及测试队就绪'
            self.update('waiting', message)
            return dict(self.status)

    def requested_alert(self, request, team_id=None, *, preview_only=False):
        if not isinstance(request, dict):
            raise ValueError('请在主力来袭卡片上选择驻守目标')
        army_id, end = request.get('army_id'), request.get('end')
        if not isinstance(army_id, str) or not army_id.isascii() or not army_id.isdigit() or type(end) not in (int, float):
            raise ValueError('来袭编号或抵达时间无效')
        key = {'army_id':army_id, 'coord':list(coordinates(request.get('coord'))), 'end':end}
        reason = self.ready()
        if reason:
            raise ValueError(reason)
        snapshot = self.state.snapshot()
        alert = next((item for item in snapshot['alerts'] if same_alert(item, key)), None)
        if not snapshot['reliable'] or alert is None:
            raise ValueError('这条来袭已更新，请从最新卡片重新操作')
        if not preview_only and alert['end']-snapshot['server_now'] <= TARGET_MIN_REMAINING:
            raise ValueError('剩余时间不足 20 秒，来不及核对驻守预览')
        team = select_team(snapshot, team_id or TEAM, test_only=team_id is None)
        if team is None:
            raise ValueError('地图中暂无法核对所选队伍的待命状态')
        if team['position'] == alert['coord']:
            raise ValueError('所选队伍已在目标地块，请核对当前驻守状态')
        prediction = predict_alerts(self.archive, snapshot, army_id=army_id).get(army_id, {})
        if alert.get('map_hero_count') is not None or prediction.get('force_assessment', {}).get('kind') != 'main':
            raise ValueError('当前来袭尚未判为主力')
        return alert

    def skip_requested(self, status, message):
        if status.get('requested_alert'):
            with self.lock:
                if self.status['armed_at'] == status['armed_at'] and self.status['phase'] in ARMED:
                    self.update('skipped', message)

    def cancel(self):
        with self.lock:
            if self.status['phase'] in {'waiting', 'reading'}:
                self.update('cancelled', '已停止驻守试跑')
            return dict(self.status)

    def run(self):
        attempted = set()
        trial_at = None
        while not self.stop.wait(2):
            status = self.snapshot()
            if status['phase'] not in ARMED | TRACKING:
                continue
            try:
                if status['phase'] in TRACKING:
                    self.observe_dispatch(status)
                    continue
                if status.get('armed_at') != trial_at:
                    attempted.clear()
                    trial_at = status['armed_at']
                if time.time() >= status['until']:
                    with self.lock:
                        if self.status['phase'] == 'waiting' and self.status['armed_at'] == status['armed_at']:
                            self.update('expired', '本条来袭驻守核对超时，未派队' if status.get('requested_alert')
                                        else '试跑到期，未派队')
                    continue
                self.check_alerts(status, attempted)
            except Exception:
                logging.exception('Garrison trial failure')
                with self.lock:
                    if self.status['phase'] in ACTIVE:
                        self.update('error', '试跑遇到异常，请查看本机服务日志')

    def check_alerts(self, status, attempted):
        requested = status.get('requested_alert')
        reason = self.ready()
        if reason:
            self.skip_requested(status, reason)
            return
        snapshot = self.state.snapshot()
        army = select_team(snapshot, status.get('team_id', TEAM), test_only='team_slot' not in status)
        if not snapshot['reliable'] or army is None:
            self.skip_requested(status, '游戏地图或所选队伍状态已变化，未派队')
            return
        source = tuple(army['position'])
        source_name = next((f['name'] for f in snapshot.get('forts', []) if f['coord'] == army['position']), None)
        preview_only = status.get('preview_only', False)
        candidates = [a for a in snapshot['alerts'] if a.get('map_hero_count') is None
                      and tuple(a['coord']) != source
                      and (a['army_id'], a['end']) not in attempted
                      and (not requested or same_alert(a, requested))
                      and (preview_only or a['end']-snapshot['server_now'] > (TARGET_MIN_REMAINING if requested
                           else max(TARGET_MIN_REMAINING, dist(source, a['coord'])*3600/198)))]
        if not candidates:
            self.skip_requested(status, '本条来袭已更新，无法读取预览' if preview_only
                                else '本条来袭已变化，或剩余时间不足 20 秒，未派队')
            return
        predictions = predict_alerts(self.archive, {**snapshot, 'alerts':candidates})
        alert = min((a for a in candidates if predictions.get(a['army_id'], {}).get(
            'force_assessment', {}).get('kind') == 'main'), key=lambda a:a['end'], default=None)
        if not alert:
            self.skip_requested(status, '本条来袭的主力判定已变化，未派队')
            return
        connection = snapshot['health']['connection']
        with self.locator.lock:
            with TrialGame(connection) as game:
                if game.idle() < 4000:
                    return
                with self.lock:
                    if self.status['phase'] != 'waiting' or self.status['armed_at'] != status['armed_at']:
                        return
                    self.update('reading', '正在游戏中核对驻守预览', enemy_id=alert['army_id'],
                                enemy_name=alert['player_name'], target=alert['coord'], source=list(source),
                                enemy_arrival=alert['end'], preview_seconds=None, preview_at=None,
                                source_name=source_name, troop_codes=[t['code'] for t in army['troops']])
                    selection = dict(self.status)
                attempted.add((alert['army_id'], alert['end']))
                try:
                    self.try_dispatch(game, connection, source, alert, selection=selection)
                except (RuntimeError, OSError) as error:
                    with self.lock:
                        if self.status['phase'] == 'reading' and self.status['armed_at'] == selection['armed_at']:
                            self.update('skipped' if requested else 'waiting', str(error))

    def try_dispatch(self, game, connection, source, alert, *, selection=None):
        target = tuple(alert['coord'])
        if selection is None:
            selection = self.snapshot()
        slot = selection.get('team_slot')
        game.activate()
        toggled_recruit = False
        preview = None
        with WindowsOcr() as ocr:
            try:
                screen, travel, preview_at = open_garrison(game, ocr, self.locator, source, target,
                                                           slot=slot, source_name=selection.get('source_name'))
                with self.lock:
                    if (self.status['phase'] != 'reading' or self.stop.is_set()
                            or self.status.get('armed_at') != selection.get('armed_at')):
                        return
                    if selection.get('preview_only'):
                        preview = travel, preview_at
                        return
                    if not self.record_preview(travel, preview_at, alert):
                        return
                options = screen.checked_options()
                blocked = options - {RECRUIT_OPTION}
                if blocked:
                    raise RuntimeError(f'游戏预览已勾选「{"、".join(sorted(blocked))}」，未派队')
                if RECRUIT_OPTION in options and not selection.get('allow_recruit'):
                    if slot is None:
                        raise RuntimeError(f'请在游戏预览中取消「{RECRUIT_OPTION}」后再试')
                    screen.toggle_recruit(game)
                    toggled_recruit = True
                    # The PC client briefly covers the coordinate with a settings toast.
                    time.sleep(RECRUIT_TOAST_SECONDS)
                    screen = Screen(game, ocr)
                    latest, preview_at = screen.preview(target, test_only=False)
                    if latest != travel or screen.checked_options():
                        raise RuntimeError('取消返程征兵后未能重新核对驻守预览，未派队')
                current = self.state.snapshot()
                team = select_team(current, selection.get('team_id', TEAM), fresh=False, test_only=slot is None)
                same = any(same_alert(a, alert) for a in current['alerts'])
                with self.lock:
                    if (self.status['phase'] != 'reading' or self.stop.is_set()
                            or self.status.get('armed_at') != selection.get('armed_at')):
                        return
                    if (not current['reliable'] or current['health'].get('resync_required')
                            or current['health']['connection'] != connection or not same
                            or not team or tuple(team['position']) != source or time.time() > self.status['until']
                            or (slot is not None and [t['code'] for t in team['troops']] != selection['troop_codes'])):
                        raise RuntimeError('来袭或所选队伍状态变化，本次未派队')
                    if not self.record_preview(travel, preview_at, alert):
                        return
                    if time.time()-screen.captured_at > 2:
                        raise RuntimeError('确认按钮画面已过期')
                    game.undisturbed()
                    self.update('submitting', '已使用本次派队额度，正在核对指令回执',
                                submitted_at=time.time())
                    # Never retry after the confirmation boundary, including exceptions from the input method.
                    screen.hit(game, screen.width-221, 747)
            finally:
                if self.snapshot()['phase'] not in {'submitting', 'marching'} and not self.stop.is_set():
                    if toggled_recruit:
                        try:
                            screen = Screen(game, ocr)
                            if screen.target(target) == target and not screen.checked_options():
                                screen.toggle_recruit(game)
                                time.sleep(RECRUIT_TOAST_SECONDS)
                        except (RuntimeError, OSError):
                            logging.warning('Could not restore the recruit checkbox after skipping garrison')
                    try:
                        restore_map(game, ocr, target)
                    except (RuntimeError, OSError):
                        pass
                if preview is not None:
                    travel, arrival = preview
                    with self.lock:
                        if (self.status['phase'] == 'reading' and not self.stop.is_set()
                                and self.status.get('armed_at') == selection.get('armed_at')):
                            self.update('previewed', '驻守预览完成，未派队', preview_seconds=travel,
                                        preview_at=arrival, lead_seconds=round(alert['end']-arrival, 1))

    def record_preview(self, travel, preview_at, alert):
        arrival = max(preview_at, time.time()+travel)
        ready = arrival+2 < alert['end']
        phase = 'reading' if ready else 'skipped' if self.status.get('requested_alert') else 'waiting'
        message = '已读取游戏驻守预览，正在核对派队条件' if ready else '游戏预览显示无法提前到达，未派队'
        self.update(phase, message, preview_seconds=travel, preview_at=preview_at,
                    latest_estimated_arrival=arrival, lead_seconds=round(alert['end']-arrival, 1))
        return ready

    def observe_dispatch(self, status):
        team_id = status.get('team_id', TEAM)
        with self.archive.lock:
            rows = self.archive.db.execute("""SELECT state_code,from_x,from_y,to_x,to_y,begin_at,end_at,observed_at,
                json_extract(data,'$[9]') AS target_type FROM army_states
                WHERE profile_id=? AND army_id=? AND owner_uid=? AND observed_at>=?
                ORDER BY observed_at ASC""",
                (self.archive.profile_id, team_id, OWNER, status['submitted_at']-1)).fetchall()
        source, target = tuple(status['source']), tuple(status['target'])
        march = next((row for row in rows if row['state_code'] == 3 and row['target_type'] == 14
                      and (row['from_x'],row['from_y']) == source and (row['to_x'],row['to_y']) == target
                      and abs(row['begin_at']-status['submitted_at']) <= 5), None)
        now = time.time()
        phase, message, fields = status['phase'], status['message'], {}
        if march is None:
            if now > status['submitted_at']+40:
                phase, message = 'submission_unknown', '已操作确认按钮，未收到对应行军记录'
        else:
            fields.update(depart_at=march['begin_at'], game_arrival=march['end_at'])
            arrived = next((row for row in rows if row['state_code'] == 6 and row['target_type'] == 14
                            and (row['from_x'],row['from_y']) == (row['to_x'],row['to_y']) == target
                            and row['observed_at'] >= march['end_at']-3), None)
            returning = next((row for row in rows if row['state_code'] == 4 and row['target_type'] == 0
                              and (row['from_x'],row['from_y']) == target and (row['to_x'],row['to_y']) == source
                              and row['begin_at'] >= march['end_at']-3
                              and (arrived is None or row['observed_at'] >= arrived['observed_at'])), None)
            if arrived is not None:
                fields.update(arrived_at=march['end_at'], actual_seconds=march['end_at']-march['begin_at'],
                              actual_lead=status['enemy_arrival']-march['end_at'])
            if returning is not None:
                fields.update(return_started_at=returning['begin_at'], return_expected_at=returning['end_at'])
                home = next((row for row in rows if row['state_code'] == 5 and row['target_type'] == 0
                             and (row['from_x'],row['from_y']) == (row['to_x'],row['to_y']) == source
                             and (row['begin_at'],row['end_at']) == (returning['begin_at'],returning['end_at'])
                             and row['observed_at'] >= returning['end_at']-3), None)
                phase, message = 'returning', '返程中'
                if home is not None:
                    phase, message = 'returned', '已返程'
                    fields['returned_at'] = home['end_at']
                elif now > returning['end_at']+45:
                    message = '已开始返程，回到起点待核对'
                battle = self.return_battle(march, returning, team_id)
                if battle is not None:
                    fields['return_battle'] = battle
            elif arrived is not None:
                phase, message = 'arrived', '已到达，当前驻守状态待核对'
                if now >= arrived['observed_at']+2:
                    current = self.state.snapshot()
                    if current['reliable'] and not current['health'].get('resync_required') and any(
                            army['id'] == team_id and army['owner'] == OWNER and not army['stale']
                            and army['kind'] == 'garrison' and army['position'] == list(target)
                            for army in current['armies']):
                        phase, message = 'holding', '驻守中'
            elif now > march['end_at']+45:
                phase, message = 'arrival_unknown', '已派出，到达待核对'
            else:
                phase, message = 'marching', '前往驻守'
        with self.lock:
            if self.status.get('submitted_at') == status['submitted_at'] and (
                    self.status['phase'] != phase or self.status['message'] != message
                    or any(self.status.get(key) != value for key, value in fields.items())):
                self.update(phase, message, **fields)

    def return_battle(self, march, returning, team_id):
        # The personal defender row carries the exact world army ID. Match its
        # order start and battle tile against the decoded report at return time.
        with self.archive.lock:
            reports = self.archive.db.execute("""SELECT DISTINCT d.battle_id,d.event_at,d.wid,d.initial_hp,d.heroes,
                a.player_name AS attacker FROM client_table_events e
                JOIN combat_sides d ON d.profile_id=e.profile_id AND d.battle_id=e.record_key AND d.side='defend'
                JOIN combat_sides a ON a.profile_id=d.profile_id AND a.battle_id=d.battle_id AND a.side='attack'
                WHERE e.profile_id=? AND e.table_name='Tb_battle_report_defend' AND e.operation=1
                  AND e.observed_at BETWEEN ? AND ?
                  AND json_array_length(e.body)=14 AND json_extract(e.body,'$[1]')=?
                  AND json_extract(e.body,'$[3]')=? AND json_extract(e.body,'$[4]')=?
                  AND json_extract(e.body,'$[7]')=d.wid
                  AND d.player_name=? AND d.outcome='loss' AND d.event_at=?""",
                (self.archive.profile_id, returning['begin_at']-2, returning['begin_at']+90,
                 int(OWNER), int(team_id), march['begin_at'], self.archive.profile['account_name'],
                 returning['begin_at'])).fetchall()
        if len(reports) != 1:
            return None
        row = reports[0]
        remaining = [hero.get('remaining_hp') for hero in json.loads(row['heroes'])]
        return {'battle_id':row['battle_id'], 'event_at':row['event_at'], 'attacker':row['attacker'],
                'coord':list(divmod(row['wid'], 10000)), 'initial_hp':row['initial_hp'],
                'remaining_hp':sum(remaining) if remaining and all(hp is not None for hp in remaining) else None}
