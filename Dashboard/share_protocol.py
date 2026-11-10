"""Small upload format for visible map troops and same-alliance battle evidence."""
import math
import re
from urllib.parse import urlsplit

from observation_codec import clean, coord, number, packed

VERSION = 1
UPLOAD_PATH = "/v1/observations"
MAX_BYTES = 1_000_000
MAX_EVENTS = 30
MAX_AGE = 24 * 3600
MAP_IDS = {5026, 5028}
REPORT_IDS = {10, 92, 6231}
MAP_SLOTS = (0, 1, 2, 3, 4, 5, 9, 15, 16, 27, 31, 32)
REPORT_FIELDS = set("""battle_id time wid wid_name result npc all_skill_info weather in_night_mode fight_type
    battle_scenes military military_effect extra_result garrison blind is_ai is_support city_type""".split())
SIDE_FIELDS = """name role_id unionid union_name idu all_hero_info all_sub_hero_info all_surface hp hero_type
    hero_type_advance advance base_level base_heroid policy_info attr_add hero_policy ship_type quality_up_stat_change
    fight_event_count fight_event_facade army_group hero_cost""".split()
REPORT_FIELDS.update(f"{side}_{field}" for side in ("attack", "defend") for field in SIDE_FIELDS)
REPORT_FIELDS.update(f"{side}_{field}" for side in ("attacker", "defender") for field in
                     ("gear_info", "surface", "force", "army_effect"))


def numeric_id(value):
    return isinstance(value, (int, str)) and type(value) is not bool and bool(re.fullmatch(r"[1-9][0-9]{0,18}", str(value)))


def scope_of(profile):
    return {field: str(profile[field]) for field in ("server", "season", "union_id")}


def endpoint(base):
    url = urlsplit(base)
    if (url.path not in ("", "/") or url.query or url.fragment or url.username or url.password or
            not (url.scheme == "https" and (url.hostname or "").endswith(".ts.net") or
                 url.scheme == "http" and url.hostname == "127.0.0.1")):
        raise ValueError("接收地址须使用试跑专用的 Tailscale HTTPS 地址")
    return base.rstrip("/") + UPLOAD_PATH


def project_player(raw):
    if not isinstance(raw, list) or len(raw) < 13 or not isinstance(raw[0], str) or not raw[0] or len(raw[0]) > 80:
        return None
    extra = raw[12] if isinstance(raw[12], list) else []
    union_name = extra[2] if len(extra) > 2 and isinstance(extra[2], str) else ""
    return [raw[0], number(raw[1]), number(raw[2])] + [0] * 9 + [[0, 0, union_name[:80]]]


def project_scene(value, owners=None):
    if not isinstance(value, list) or len(value) != 34 or not isinstance(value[6], dict):
        return None
    players = value[1] if isinstance(value[1], dict) else {}
    result = [None] * 34
    result[1], result[6] = {}, {}
    for aid, raw in value[6].items():
        if not numeric_id(aid) or not isinstance(raw, list) or len(raw) != 33 or not numeric_id(raw[1]):
            continue
        if number(raw[0]) <= 0 or not (coord(raw[2]) or coord(raw[3])):
            continue
        uid = str(raw[1])
        owner = project_player(players.get(uid)) or (owners or {}).get(uid)
        if owner is None:
            continue
        fields = [0] * 33
        for pos in MAP_SLOTS:
            field = raw[pos]
            fields[pos] = str(field)[:4096] if pos in (15, 16, 31) and isinstance(field, str) else number(field)
        result[1][uid] = owner
        result[6][str(aid)] = fields
        if len(result[6]) >= 500:
            break
    return result if result[6] else None


def in_alliance(row, union_id):
    return union_id in (str(row.get("attack_unionid")), str(row.get("defend_unionid")))


def project_report(row, union_id):
    if not isinstance(row, dict) or not numeric_id(row.get("battle_id")) or not in_alliance(row, union_id):
        return None
    if not coord(row.get("wid")) or number(row.get("time")) <= 0 or not any(
            isinstance(row.get(side + "_name"), str) and row[side + "_name"] for side in ("attack", "defend")):
        return None
    result = clean({key: row[key] for key in REPORT_FIELDS if key in row and
                    (type(row[key]) in (int, bool, float) or isinstance(row[key], str) and len(row[key]) <= 24_000)})
    result["battle_id"] = int(row["battle_id"])
    return result


def project_body(row):
    if not isinstance(row, dict) or not numeric_id(row.get("battle_id")):
        return None
    body = row.get("report")
    if not isinstance(body, str) or not body.strip() or len(body.encode("utf-8")) > 600_000:
        return None
    return {"battle_id": int(row["battle_id"]), "report": body}


def validate_events(events, union_id, now):
    if not isinstance(events, list) or not 1 <= len(events) <= MAX_EVENTS:
        raise ValueError("上传批次大小无效")
    for event in events:
        if not isinstance(event, list) or len(event) != 3:
            raise ValueError("上传记录格式无效")
        mid, at, value = event
        if type(mid) is not int or type(at) not in (int, float) or not math.isfinite(at) or not now-MAX_AGE <= at <= now+120:
            raise ValueError("观测时间或类型无效，请核对 Windows 时间")
        normalized = project_scene(value) if mid in MAP_IDS else project_body(value) if mid == 6231 else (
            project_report(value, union_id) if mid in REPORT_IDS else None)
        if normalized is None or packed(value) != packed(normalized):
            raise ValueError("记录超出试跑允许的地图或本盟战报范围")
        if mid in (10, 92) and number(value.get("time")) > now+120:
            raise ValueError("战报发生时间无效")
