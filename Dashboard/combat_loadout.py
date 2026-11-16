"""Per-position troop types, equipment and advancement from PC battle summaries."""
import json
import copy
import re
from pathlib import Path

from observation_codec import TROOP_NAMES, TROOP_TYPES, report_troop_codes, report_system_npc


CATALOG = Path(__file__).with_name('catalog')
GEAR = json.loads((CATALOG / 'gear.json').read_text(encoding='utf-8'))
HEROES = json.loads((CATALOG / 'heroes.json').read_text(encoding='utf-8'))
SKILLS = json.loads((CATALOG / 'skills.json').read_text(encoding='utf-8'))
SKILL_SLOT_ROW = re.compile(r'[1-6](?:,[0-9]{1,10},[0-9]{1,10}){1,3}')
UNKNOWN_SKILL_SLOTS = (None, None, None)


def structured_report_complete(battle):
    """Field readiness is independent of numeric turn text and army association."""
    if not isinstance(battle,dict) or not str(battle.get('result','')).isdecimal():return False
    for side in ('attack','defend'):
        # Land/NPC records remain useful without a human loadout on the NPC side.
        if report_system_npc(battle,side):continue
        if not battle.get(side+'_name'):return False
        raw=str(battle.get(side+'_all_hero_info')or'').removesuffix(';').split(';')
        cards=str(battle.get(side+'_idu')or'').split(',')[1:4]
        expected=max((i for i,v in enumerate(cards,1)if v.isdecimal()and int(v)>0),default=3)
        if not 1<=len(raw)<=3 or len(raw)<expected:return False
        stars=report_rows(battle.get(side+'_advance'),side,6)
        skills=skill_slots_by_position(battle.get('all_skill_info'),side)
        present=False
        for pos,segment in enumerate(raw):
            fields=segment.split(',')
            if len(fields)!=5 or not all(v.isascii()and v.isdecimal()and len(v)<=10 for v in fields):return False
            hero,level,initial,remaining,wounded=map(int,fields)
            if hero==0:
                if any((level,initial,remaining,wounded)):return False
                continue
            present=True
            if level<=0 or not stars[pos] or not 0<=stars[pos][0]<=5 or any(s is None for s in skills[pos]):return False
        if not present:return False
    return True


def hero_name(hero_id, saved_name):
    return HEROES.get(str(hero_id), {}).get('name', saved_name) if saved_name == f'武将#{hero_id}' else saved_name


def named_hero(hero):
    hero_id = hero['id']
    result = {**hero, 'name': hero_name(hero_id, hero.get('name') or f'武将#{hero_id}')}
    if hero.get('skills') is not None:
        result['skills'] = [named_skill(skill) for skill in hero['skills']]
    return result


def named_skill(skill):
    skill_id = skill['id']
    return {**skill, 'name': SKILLS.get(str(skill_id), {}).get('name') or skill.get('name') or f'战法#{skill_id}'}


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


def skill_slots_by_position(raw, side):
    positions = ('1', '2', '3') if side == 'attack' else ('6', '5', '4')
    rows = {}
    for row in str(raw or '').split(';'):
        position, _, _ = row.partition(',')
        if position not in positions:
            continue
        if position in rows or not SKILL_SLOT_ROW.fullmatch(row):
            rows[position] = UNKNOWN_SKILL_SLOTS
            continue
        fields = tuple(map(int, row.split(',')[1:]))
        slots = tuple(zip(fields[::2], fields[1::2]))
        ids = [skill for skill, _ in slots if skill]
        if len(ids) != len(set(ids)) or any(not skill and level for skill, level in slots):
            rows[position] = UNKNOWN_SKILL_SLOTS
            continue
        rows[position] = slots + (None,) * (3 - len(slots))
    return tuple(rows.get(position, UNKNOWN_SKILL_SLOTS) for position in positions)


def with_loadout(heroes, side, advance, gear, troop_raw=None, skill_raw=None):
    stars = report_rows(advance, side, 6)
    equipment = report_rows(gear, side, 3)
    troop_codes = report_troop_codes(troop_raw, side)
    skill_slots = skill_slots_by_position(skill_raw, side) if skill_raw is not None else None
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
            skills = [named_skill(skill) for skill in skills]
        slot_fields = {}
        if skill_slots is not None:
            slots = skill_slots[slot] if 0 <= slot < 3 else UNKNOWN_SKILL_SLOTS
            slot_fields['skill_slot_ids'] = [pair[0] if pair is not None else None for pair in slots]
        result.append({**named_hero(hero), 'red_stars': red[0] if red and 0 <= red[0] <= 5 else None,
            'skills': skills, 'gear': gear, 'troop_code': troop_code, **slot_fields,
            'troop_name': TROOP_TYPES.get(str(troop_code), TROOP_NAMES[troop_code % 10]) if troop_code else None,
            'gear_reported': item is not None and (item[0] > 0 or not any(item))})
    return result


def side_heroes(heroes, side, features):
    result = with_loadout(heroes, side, features.get(side + '_advance'), features.get(side + 'er_gear_info'),
                          features.get(side + '_hero_type'), features.get('all_skill_info') or '')
    # The PC report's wounded bar uses column 4, separately from remaining troops in column 3.
    raw = str(features.get(side + '_all_hero_info') or '').split(';')
    for hero in result:
        position = hero['position'] - 1
        fields = raw[position].split(',') if 0 <= position < len(raw) else []
        hero['wounded_hp'] = int(fields[4]) if len(fields) >= 5 and fields[0] == str(hero['id']) and fields[4].isascii() and fields[4].isdecimal() and len(fields[4]) <= 9 else None
    return result


def forecast_heroes(report):
    if report.get('_resolved_heroes') is not None:return copy.deepcopy(report['_resolved_heroes'])
    skill_raw = (report['skill_raw'] or '') if 'skill_raw' in report else None
    return with_loadout(json.loads(report['heroes']), report['side'], report.get('advance_raw'), report.get('gear_raw'),
                        report.get('troop_raw'), skill_raw)
