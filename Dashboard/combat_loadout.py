"""Per-position troop types, equipment and advancement from PC battle summaries."""
import json
from pathlib import Path

from observation_codec import TROOP_NAMES, report_troop_codes


CATALOG = Path(__file__).with_name('catalog')
GEAR = json.loads((CATALOG / 'gear.json').read_text(encoding='utf-8'))
HEROES = json.loads((CATALOG / 'heroes.json').read_text(encoding='utf-8'))
SKILLS = json.loads((CATALOG / 'skills.json').read_text(encoding='utf-8'))


def hero_name(hero_id, saved_name):
    return HEROES.get(str(hero_id), {}).get('name', saved_name) if saved_name == f'武将#{hero_id}' else saved_name


def named_hero(hero):
    hero_id = hero['id']
    return {**hero, 'name': hero_name(hero_id, hero.get('name') or f'武将#{hero_id}')}


def report_rows(raw, side, width):
    if not isinstance(raw, str):
        return (None,) * 3
    rows = raw.removesuffix(';').split(';')
    if side == 'defend':
        rows.reverse()
    if len(rows) != 4 or rows[0] != ','.join(['0'] * width):
        return (None,) * 3
    parsed = []
    for row in rows[1:]:
        fields = row.split(',')
        parsed.append(tuple(map(int, fields)) if len(fields) == width and all(
            field.isascii() and field.isdecimal() and len(field) <= 10 for field in fields) else None)
    return parsed


def with_loadout(heroes, side, advance, gear, troop_raw=None):
    stars = report_rows(advance, side, 6)
    equipment = report_rows(gear, side, 3)
    troop_codes = report_troop_codes(troop_raw, side)
    result = []
    for hero in heroes:
        slot = hero['position'] - 1
        red, item = (stars[slot], equipment[slot]) if 0 <= slot < 3 else (None, None)
        troop_code = (troop_codes[slot] or None) if 0 <= slot < len(troop_codes) else None
        gear = None
        if item and item[0]:
            gear = {'id': item[0], 'level': item[1], 'forge_id': item[2], 'name': GEAR['gear'].get(str(item[0])),
                    'forge_name': GEAR['forge'].get(str(item[2]))}
        skills = hero.get('skills')
        if skills:
            skills = [{**skill, 'name': SKILLS.get(str(skill['id']), {}).get('name', skill.get('name'))} for skill in skills]
        result.append({**named_hero(hero), 'red_stars': red[0] if red and 0 <= red[0] <= 5 else None,
            'skills': skills, 'gear': gear, 'troop_code': troop_code,
            'troop_name': TROOP_NAMES[troop_code % 10] if troop_code else None,
            'gear_reported': item is not None and (item[0] > 0 or not any(item))})
    return result


def side_heroes(heroes, side, features):
    result = with_loadout(heroes, side, features.get(side + '_advance'), features.get(side + 'er_gear_info'),
                          features.get(side + '_hero_type'))
    # The PC report's wounded bar uses column 4, separately from remaining troops in column 3.
    raw = str(features.get(side + '_all_hero_info') or '').split(';')
    for hero in result:
        position = hero['position'] - 1
        fields = raw[position].split(',') if 0 <= position < len(raw) else []
        hero['wounded_hp'] = int(fields[4]) if len(fields) >= 5 and fields[0] == str(hero['id']) and fields[4].isascii() and fields[4].isdecimal() and len(fields[4]) <= 9 else None
    return result


def forecast_heroes(report):
    return with_loadout(json.loads(report['heroes']), report['side'], report.get('advance_raw'), report.get('gear_raw'),
                        report.get('troop_raw'))
