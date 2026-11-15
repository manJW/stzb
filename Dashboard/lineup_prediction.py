"""Rank incoming formations using only evidence available before impact."""
import json
import math
import time
from collections import defaultdict
from contextlib import nullcontext
from statistics import median

from combat_loadout import forecast_heroes, hero_name, skill_slots_by_position
from lineup_current import card_identity, resolve_current_report
from observation_codec import digest, map_troop_codes, number, packed, report_troop_codes, short_lineup_hero_count, troop_families


MODEL_VERSION = '32'
MAIN_MIN_LEVEL = 40
SIEGE_SKILL_IDS = frozenset((200187, 200663))  # 冲车、云梯
LOW_FORCE_MAX_AGE = 3600
POSSIBLE_MAIN_MAX_AGE = 48 * 3600
MAIN_PATTERN_MAX_AGE = 5 * 24 * 3600
SCOUT_HINT_MAX_AGE = 48 * 3600
TWO_SKILL_MAX_AGE = 96 * 3600
CANDIDATE_TWO_SKILL_MAX_AGE = 48 * 3600
# A PvE report can still name a human attacker. Use the role and hero-instance
# fields on the report itself so a future roster refresh cannot affect replay.
HUMAN_ATTACK = """(c.side='attack' AND (c.union_id NOT IN ('','0')
    OR CAST(json_extract(c.features,'$.attacker_force') AS INTEGER)=1)
    AND COALESCE(CAST(json_extract(c.features,'$.attack_role_id') AS TEXT),'') NOT IN ('','0')
    AND COALESCE(json_extract(c.features,'$.attack_idu'),'') NOT IN ('','0','0,0,0,0,0'))"""
LOADOUT_FIELDS = """json_extract(c.features,'$.'||c.side||'_advance') AS advance_raw,
    json_extract(c.features,'$.'||c.side||'_idu') AS idu_raw,
    json_extract(c.features,'$.'||c.side||'_role_id') AS role_raw,
    json_extract(c.features,'$.'||c.side||'er_gear_info') AS gear_raw,
    json_extract(c.features,'$.'||c.side||'_hero_type') AS troop_raw"""


def player_history(db, profile, name, union, *, include_pve=True):
    import reported_archive
    marches=reported_archive.live_table(db,'march_samples')
    states=reported_archive.live_table(db,'army_states')
    params = (profile, name, union, include_pve)
    reports = db.execute(f'''SELECT c.battle_id,c.side,c.hero_key,c.heroes,c.army_id,c.event_at,c.observed_at,c.npc,c.wid,
        {LOADOUT_FIELDS},
        json_extract(c.features,'$.all_skill_info') AS skill_raw,
        json_extract(c.features,'$.'||c.side||'_hp') AS reported_hp,
        json_extract(c.features,'$.binding_evidence.observed_at') AS binding_at,
        json_extract(c.features,'$.binding_evidence.world_uid') AS binding_owner
        FROM combat_sides c WHERE c.profile_id=? AND c.player_name=? AND c.union_id=?
        AND (c.npc<>1 OR (? AND {HUMAN_ATTACK})) AND c.event_at>0
        AND (c.hero_key<>'' OR c.army_id IS NOT NULL OR EXISTS (
            SELECT 1 FROM battle_march_links l WHERE l.profile_id=c.profile_id AND l.battle_id=c.battle_id
            AND l.side=c.side AND l.method='unique_attack_arrival'))
        ORDER BY c.event_at DESC,CAST(c.battle_id AS INTEGER) DESC,c.observed_at DESC,c.side''', params).fetchall()
    links = defaultdict(list)
    for row in db.execute(f'''SELECT m.*,l.method,c.battle_id,c.side FROM combat_sides c INDEXED BY combat_player
        CROSS JOIN battle_march_links l ON l.profile_id=c.profile_id AND l.battle_id=c.battle_id AND l.side=c.side
        CROSS JOIN {marches} m ON m.profile_id=l.profile_id AND m.id=l.march_id
        WHERE c.profile_id=? AND c.player_name=? AND c.union_id=? AND c.side='attack'
        AND (c.npc<>1 OR (? AND {HUMAN_ATTACK}))
        AND (l.method='unique_attack_arrival' OR (l.method='coordinate_time_match' AND c.army_id=m.army_id))''', params):
        links[row['battle_id'], row['side']].append(dict(row))
    owners = db.execute('''SELECT DISTINCT world_uid FROM players WHERE profile_id=? AND name=? AND union_id=?
        AND world_uid IS NOT NULL LIMIT 2''', params[:3]).fetchall()
    ambiguous_arrivals = set()
    if len(owners)==1:
        # Filter this player first, then seek each arrival through march_arrival.
        ambiguous_arrivals = {r[0] for r in db.execute(f'''SELECT c.battle_id FROM combat_sides c INDEXED BY combat_player
            CROSS JOIN {marches} m ON m.profile_id=c.profile_id AND m.state_code IN (1,12)
            AND m.to_x=CAST(c.wid/10000 AS INTEGER) AND m.to_y=c.wid%10000 AND m.end_at=c.event_at
            AND m.observed_at<=m.end_at WHERE c.profile_id=? AND c.player_name=? AND c.union_id=?
            AND c.side='attack' AND c.army_id IS NULL AND EXISTS(
                SELECT 1 FROM {states} s WHERE s.profile_id=m.profile_id AND s.army_id=m.army_id
                AND s.message_ref=m.message_ref AND s.owner_uid=? AND s.begin_at=m.begin_at AND s.end_at=m.end_at
                AND s.from_x=m.from_x AND s.from_y=m.from_y AND s.to_x=m.to_x AND s.to_y=m.to_y)
            GROUP BY c.battle_id HAVING COUNT(DISTINCT m.id)>1''', (*params[:3],owners[0]['world_uid']))}
    return [{**dict(r),'arrival_ambiguous':r['battle_id'] in ambiguous_arrivals} for r in reports], links, len(owners) > 1


def troop_match(live, reported):
    if not live or not reported:
        return 'unknown'
    if live == reported:
        return 'exact'
    return 'family' if troop_families(live) == troop_families(reported) else 'conflict'


def march_speed(row):
    start, target = row.get('from'), row.get('target')
    begin, end = row.get('begin'), row.get('end')
    if not start or not target or start == target or not begin or not end or end <= begin:
        return None
    distance = math.dist(start, target)
    return {'distance': distance, 'seconds': end-begin, 'grids_per_hour': distance*3600/(end-begin)}


def speed_reference(samples, row, speed):
    if not speed or not samples:
        return None
    current = speed['distance']
    comparable = [s for s in samples if current >= 4 and s['straight_distance'] >= 4
                  and 0.5 <= s['straight_distance']/current <= 2]
    if not comparable:
        return None
    values = [s['straight_grids_per_hour'] for s in comparable]
    typical = median(values)
    return {'samples': len(values), 'median': typical, 'min': min(values), 'max': max(values),
            'delta_pct': (speed['grids_per_hour']/typical-1)*100,
            'same_army': sum(s['army_id'] == row['id'] for s in comparable),
            'direct_samples': sum(s['method'] == 'coordinate_time_match' for s in comparable)}


def troop_total(heroes, field):
    values = [hero.get(field) for hero in heroes]
    return sum(values) if values and all(type(value) is int and value >= 0 for value in values) else None


def spartan_build_basis(heroes):
    if any(type(hero.get('level')) is int and 0 < hero['level'] < MAIN_MIN_LEVEL for hero in heroes):
        return 'level_below_40'
    for hero in heroes:
        skill_ids = hero.get('skill_slot_ids')
        if skill_ids is None:
            skill_ids = [skill.get('id') for skill in hero.get('skills') or ()]
        if SIEGE_SKILL_IDS.intersection(skill_ids):
            return 'siege_skill'
    return None


def meets_main_threshold(heroes):
    if (len(heroes) != 3 or any(type(hero.get('level')) is not int or hero['level'] < MAIN_MIN_LEVEL for hero in heroes)
            or spartan_build_basis(heroes)):
        return False
    for hero in heroes:
        slots = hero.get('skill_slot_ids')
        if slots is not None and (len(slots) != 3 or not all(slots)):
            return False
        skills = hero.get('skills') or ()
        skill_ids = {skill['id'] for skill in skills if type(skill.get('id')) is int and skill['id'] > 0}
        if len(skills) != 3 or len(skill_ids) != 3:
            return False
    return True


def report_initial_hp(heroes, report):
    initial = troop_total(heroes, 'initial_hp')
    reported = number(report.get('reported_hp'))
    return initial if initial and (reported <= 0 or initial == reported) else None


def reported_skill_slots(report):
    rows = skill_slots_by_position(report.get('skill_raw'), report['side'])
    return rows if all(pair is not None for slots in rows for pair in slots) else None


def empty_skill_basis(heroes):
    if len(heroes) != 3:
        return None
    rows = []
    for hero in heroes:
        slots = hero.get('skill_slot_ids') or ()
        if len(slots) != 3 or not slots[0] or any(ident is None for ident in slots):
            return None
        rows.append(slots)
    if not any(0 in slots for slots in rows):
        return None
    return 'two_skills_each' if all(slots[1] and slots[2] == 0 for slots in rows) else 'empty_skill_slots'


def incomplete_main_reference(row, linked, reports, known_at):
    if not linked:
        return None
    latest, heroes, association, match, codes = linked
    if (association != 'direct' or match != 'exact' or len(heroes) != 3
            or any((h.get('level') or 0) < MAIN_MIN_LEVEL for h in heroes)):
        return None
    current = [slot for hero in heroes for slot in hero['skill_slot_ids']]
    if None not in current or 0 in current:
        return None
    for report in reports:
        if (report['event_at'] >= latest['event_at'] or report['observed_at'] > known_at
                or report['army_id'] != row['id'] or (report['binding_at'] or 0) > known_at
                or report['binding_owner'] not in (None, row.get('owner'))):
            continue
        if (report['hero_key'] != latest['hero_key'] or known_at - report['event_at'] > POSSIBLE_MAIN_MAX_AGE
                or report_troop_codes(report['troop_raw'], report['side']) != codes):
            return None
        old_heroes = forecast_heroes(report)
        old_slots = [slot for hero in old_heroes for slot in hero['skill_slot_ids']]
        if (meets_main_threshold(old_heroes) and all(old_slots)
                and all(current_slot is None or current_slot == old_slot for current_slot, old_slot in zip(current, old_slots))):
            return {'battle_id': report['battle_id'], 'event_at': report['event_at']}
        return None
    return None


def downgraded_main(heroes, slots, previous_main):
    if not previous_main or not slots:
        return None
    previous_report, previous_heroes = previous_main
    missing = sum(not skill for row in slots for skill, _ in row)
    if not missing:
        return None
    former = {hero['id']:hero['skills'][0] for hero in previous_heroes}
    for hero in heroes:
        first_skill, level = slots[hero['position'] - 1][0]
        old = former.get(hero['id'])
        if old and old['id'] == first_skill and level == 1 and number(old.get('level')) > 1:
            return {'previous_main_battle_id':previous_report['battle_id'], 'removed_skill_slots':missing,
                    'skill_level_before':old['level'], 'downgraded_hero':hero['name']}
    return None


def last_force_observation(linked, occupied_slots, known_at, previous_main=None):
    if not linked:
        return None
    report, heroes, association, match, reported_codes = linked
    report_slots = {i for i,code in enumerate(reported_codes,1) if code}
    slots = {hero['position'] for hero in heroes}
    if match == 'conflict' or slots != (occupied_slots or report_slots or {1,2,3}):
        return None
    initial = report_initial_hp(heroes, report)
    priority = 'unknown' if initial is None else 'standard'
    transition = None
    build_basis = spartan_build_basis(heroes) if match == 'exact' else None
    if build_basis:
        priority = build_basis
    elif meets_main_threshold(heroes):
        priority = 'main'
    elif initial and initial <= 300 and match == 'exact' and known_at-report['event_at'] <= LOW_FORCE_MAX_AGE:
        priority = 'low'
    elif match == 'exact' and occupied_slots == {1, 2, 3}:
        skills = reported_skill_slots(report)
        transition = (downgraded_main(heroes, skills, previous_main)
                      if association == 'direct' and known_at-report['event_at'] <= TWO_SKILL_MAX_AGE else None)
        if transition:
            priority = 'downgraded_main'
        else:
            priority = empty_skill_basis(heroes) or priority
    return {'priority':priority, 'battle_id':report['battle_id'], 'event_at':report['event_at'],
            'association':association, 'initial_hp':initial, 'remaining_hp':troop_total(heroes, 'remaining_hp'), 'npc':report['npc'],
            'heroes':heroes, **(transition or {})}


def force_assessment(prediction, cutoff, recent_layout_count):
    def evidence(kind, report, strength, source, confidence='inferred'):
        basis = {'main_basis':'skills'} if kind == 'main' else {}
        return {'kind':kind, 'battle_id':report['battle_id'], 'event_at':report['event_at'],
                'initial_hp':strength, 'levels':[hero.get('level') for hero in report['heroes']],
                'source':source, 'confidence':confidence, 'npc':report.get('npc'), **basis}

    personal = [(item, item.get('initial_hp')) for item in prediction['items'] if item['event_at'] < cutoff]
    exact = [(item, hp) for item, hp in personal if item['troop_match'] == 'exact']
    eligible = [(item, hp) for item, hp in exact if can_classify_from(item, prediction['army_id'])]
    compatible_low = (bool(eligible) and not prediction['last_army_conflict']
                      and all(hp is not None and 0 < hp <= 300 and not meets_main_threshold(item['heroes'])
                              for item, hp in eligible))
    force = prediction.get('force')
    if force and force['priority'] in ('level_below_40', 'siege_skill', 'two_skills_each', 'empty_skill_slots', 'downgraded_main'):
        transition = {key:force[key] for key in ('previous_main_battle_id', 'removed_skill_slots', 'skill_level_before', 'downgraded_hero') if key in force}
        return {**evidence('spartan', force, force['initial_hp'], force['association']), 'spartan_basis':force['priority'], **transition}
    if force:
        hp = force['initial_hp']
        if force['priority'] in ('main', 'low'):
            kind = 'main' if force['priority'] == 'main' else 'spartan'
            confidence = 'linked' if force['association'] == 'direct' else 'inferred'
            return evidence(kind, force, hp, force['association'], confidence)
        if compatible_low and any(item['battle_id'] == force['battle_id'] for item, _ in eligible):
            return evidence('spartan', force, hp, force['association'])
        return evidence('unknown', force, hp, force['association'], 'reference')

    if prediction.get('latest_link') and short_lineup_hero_count(prediction['troop_codes']) is None:
        return {'kind':'unknown', **prediction['latest_link'], 'confidence':'reference', 'initial_hp':None}

    if len(eligible) == 1 and not prediction['last_army_conflict']:
        item, hp = eligible[0]
        basis = spartan_build_basis(item['heroes'])
        if basis:
            return {**evidence('spartan', item, hp, 'same_player_exact'), 'spartan_basis':basis}

    if compatible_low:
        item, hp = max(eligible, key=lambda candidate:candidate[0]['event_at'])
        return evidence('spartan', item, hp, 'same_player_exact')

    # An unbound report can support a classification without proving its army ID.
    # Competing recent exact layouts and an explicit conflict leave it uncertain.
    recent_exact = [(item, hp) for item, hp in eligible if 0 < cutoff-item['event_at'] <= CANDIDATE_TWO_SKILL_MAX_AGE]
    if (recent_layout_count == len(recent_exact) == 1 and not prediction.get('latest_link') and not prediction['last_army_conflict']
            and len(prediction['troop_codes']) == 3 and all(prediction['troop_codes'])):
        item, hp = recent_exact[0]
        basis = empty_skill_basis(item['heroes'])
        if (item['npc'] == 0 and item['report_army_id'] is None and item['inferred_report_army_id'] is None
                and basis):
            return {**evidence('spartan', item, hp, 'same_player_exact'), 'spartan_basis':'candidate_' + basis}

    for item, hp in exact + [entry for entry in personal if entry[0]['troop_match'] != 'exact']:
        main = (len(eligible) == 1 and eligible[0][0] is item and not prediction['last_army_conflict']
                and cutoff - item['event_at'] <= POSSIBLE_MAIN_MAX_AGE
                and meets_main_threshold(item['heroes']))
        if not hp and not main:
            continue
        source = 'same_player_exact' if item['troop_match'] == 'exact' else 'same_player'
        return evidence('main' if main else 'unknown', item, hp, source, 'inferred' if main else 'reference')
    return {'kind':'unknown'}


def can_classify_from(item, army_id):
    return all(item.get(key) in (None, army_id) for key in ('report_army_id', 'inferred_report_army_id'))


def main_pattern_assessment(prediction, cutoff):
    codes = prediction['troop_codes']
    latest_link = prediction.get('latest_link')
    if (len(codes) != 3 or not all(codes) or prediction['force']
            or (latest_link and not prediction['last_army_conflict'])):
        return None
    personal = [item for item in prediction['items'] if item['troop_match'] == 'exact'
                and can_classify_from(item, prediction['army_id'])
                and (not latest_link or item['event_at'] > latest_link['event_at'])
                and item['event_at'] < cutoff]
    recent = [item for item in personal if cutoff - item['event_at'] <= MAIN_PATTERN_MAX_AGE]
    # An older PvP lineup can still be useful when it is the only compatible
    # formation from this player and there is no conflicting army history.
    if not recent and (latest_link or len(personal) != 1 or personal[0]['npc'] != 0):
        return None
    samples = recent or personal
    if not all(meets_main_threshold(item['heroes']) for item in samples):
        return None
    sample = max(samples, key=lambda item: item['event_at'])
    return {'kind':'main', 'confidence':'inferred', 'source':'same_player_exact', 'battle_id':sample['battle_id'],
            'event_at':sample['event_at'], 'initial_hp':sample['initial_hp'], 'npc':sample['npc'],
            'levels':[h['level'] for h in sample['heroes']], 'main_basis':'skills'}


def predict(row, history, known_at, *, use_identity=True, use_speed=True):
    reports, links, ambiguous_player = history
    speed = march_speed(row)
    live_codes = tuple(t['code'] for t in row.get('troops', ()))
    output = {'army_id': row['id'], 'begin': row.get('begin'), 'end': row.get('end'), 'speed': speed,
              'from': row.get('from'), 'target': row.get('target'), 'troop_codes': live_codes,
              'items': [], 'same_army_report_id': None, 'ambiguous': False, 'last_army_conflict': False,
              'lineup_confidence':'uncertain'}
    # A live card uses everything actually known now. Historical replay supplies
    # its original issued_at, so this cannot borrow a later battle or receipt.
    cutoff = known_at
    occupied_slots = {i for i,code in enumerate(live_codes,1) if code}
    candidates, recent_links, pvp_samples = {}, {}, {}
    recent_layouts = set()
    linked_reports = {}
    previous_main = None
    check_downgrade = False
    same_army = arrival_item = None
    observations = defaultdict(dict)
    for report in reports:
        if report['event_at'] >= cutoff or report['observed_at'] > known_at:
            continue
        hero_key = report['hero_key']
        direct = (report['event_at'] < cutoff and report['army_id'] == row['id'] and (report['binding_at'] or 0) <= known_at
                  and report['binding_owner'] in (None, row.get('owner')))
        reported_codes = report_troop_codes(report['troop_raw'], report['side'])
        match = troop_match(live_codes, reported_codes)
        routes = [m for m in links[report['battle_id'], report['side']]
                  if report['event_at'] < cutoff and m['observed_at'] <= known_at and m['end_at'] <= cutoff
                  and (m['method'] == 'unique_attack_arrival' or (report['binding_at'] or 0) <= known_at)]
        arrivals = [m for m in routes if m['method'] == 'unique_attack_arrival']
        arrival_armies = {m['army_id'] for m in arrivals}
        inferred_id = next(iter(arrival_armies)) if len(arrival_armies) == 1 else None
        inferred = report['army_id'] in (None, row['id']) and inferred_id == row['id']
        # A subset in an arrival report does not replace a full lineup that
        # was observed marching to that battle and matches the current march.
        partial_arrival = (inferred and len(occupied_slots) == 3
                           and short_lineup_hero_count(reported_codes) is not None
                           and all(not code or code == live for code, live in zip(reported_codes, live_codes))
                           and all(map_troop_codes(m['hero_type']) == live_codes for m in arrivals))
        heroes = None
        if direct or (inferred and not partial_arrival):
            association = 'direct' if direct else 'arrival'
            if association not in linked_reports and (direct or not ambiguous_player):
                recent_links[association] = (report['event_at'], hero_key, match, report['battle_id'])
                heroes = forecast_heroes(report)
                linked_reports[association] = (report, heroes, association, match, reported_codes)
                if direct and match == 'exact' and occupied_slots == {1, 2, 3} and known_at-report['event_at'] <= TWO_SKILL_MAX_AGE:
                    skills = reported_skill_slots(report)
                    check_downgrade = bool(skills and any(row[0][1] == 1 for row in skills)
                                           and any(not ident for row in skills for ident, _ in row))
            elif direct and check_downgrade and previous_main is None:
                archived_heroes = json.loads(report['heroes'])
                if meets_main_threshold(archived_heroes):
                    previous_main = (report, archived_heroes)
        if not hero_key or match == 'conflict' or (ambiguous_player and not direct):
            continue
        # A full lineup can share its broad troop classes with another army
        # of the same player. A mismatch in the detailed codes needs army evidence.
        if match == 'family' and len(occupied_slots) == 3 and not (direct or inferred):
            continue
        heroes = heroes if heroes is not None else forecast_heroes(report)
        if live_codes and {h['position'] for h in heroes} != occupied_slots:
            continue
        initial_hp = report_initial_hp(heroes, report)
        if (len(occupied_slots) == 3 and match == 'exact' and report['side'] == 'attack' and report['npc'] == 0
                and initial_hp is not None and 0 < cutoff-report['event_at'] <= SCOUT_HINT_MAX_AGE):
            # Several rounds at the same location and second supply one battle sample.
            key = (report['event_at'], report['wid'])
            if key not in pvp_samples or initial_hp > pvp_samples[key]['initial_hp']:
                pvp_samples[key] = {'battle_id':report['battle_id'], 'event_at':report['event_at'], 'initial_hp':initial_hp,
                                    'main':meets_main_threshold(heroes)}
        item = {'hero_key': hero_key, 'battle_id': report['battle_id'], 'side': report['side'], 'event_at': report['event_at'],
                'troop_match': match, 'heroes': heroes, 'initial_hp': initial_hp, 'npc': report['npc'],
                'report_army_id': report['army_id'], 'inferred_report_army_id': inferred_id,
                'identity': None, 'speed': None}
        identity = card_identity(report)
        if identity:
            item['card_instances'] = list(identity['cards'])
            item['report_role_id'] = identity['role_id']
        if (len(occupied_slots) == 3 and match == 'exact' and can_classify_from(item, row['id'])
                and 0 < cutoff-report['event_at'] <= CANDIDATE_TWO_SKILL_MAX_AGE):
            recent_layouts.add((hero_key, tuple(tuple(h.get('skill_slot_ids') or ()) for h in heroes)))
        # Keep the same-army report separate from a newer report about a
        # different army that happens to contain the same heroes.
        if direct and match == 'exact' and report['battle_id'] == recent_links['direct'][3]:
            same_army = item
        if inferred and match == 'exact' and 'arrival' in recent_links and report['battle_id'] == recent_links['arrival'][3]:
            arrival_item = item
        candidates.setdefault((hero_key, direct), item)
        for m in routes:
            if (m['observed_at'] >= m['end_at'] or not m['straight_grids_per_hour'] or m['straight_grids_per_hour'] <= 0
                    or m['state_code'] != row.get('state_code') or troop_match(live_codes, map_troop_codes(m['hero_type'])) == 'conflict'):
                continue
            march = (m['army_id'], m['begin_at'], m['from_x'], m['from_y'], m['to_x'], m['to_y'])
            observations[march].setdefault(hero_key, m)

    direct = recent_links.get('direct')
    arrival = recent_links.get('arrival')
    latest = direct or arrival
    # Prefer the newer matched battle when all current troop positions agree.
    # A newer explicit report retains its precedence.
    if arrival_item and direct and arrival[0] > direct[0]:
        latest = arrival
        if use_identity:
            candidates[(arrival_item['hero_key'], False)] = arrival_item
    linked = linked_reports.get('direct') if latest is direct else linked_reports.get('arrival')
    output['force'] = last_force_observation(linked, occupied_slots, known_at, previous_main)
    output['last_army_conflict'] = bool(latest and latest[2] == 'conflict')
    if latest:
        source = 'direct' if latest is direct else 'arrival'
        output['latest_link'] = {'battle_id':latest[3], 'event_at':latest[0], 'source':source,
                                 'npc':linked_reports[source][0]['npc']}
    if same_army and not use_identity:
        same_army = None
    if same_army:
        same_army['identity'] = 'direct'
        same_army['identity_at'] = same_army['event_at']
        output['same_army_report_id'] = same_army['battle_id']
    by_formation = defaultdict(list)
    for matches in observations.values():
        if len(matches) == 1:
            hero_key, sample = next(iter(matches.items()))
            by_formation[hero_key].append(sample)

    alternatives = {}
    for (key, is_direct), item in candidates.items():
        if not same_army or not is_direct:
            alternatives.setdefault(key, item)
    ranked = list(alternatives.values())
    if same_army:
        ranked.append(same_army)
    scores = []
    for item in ranked:
        key = item['hero_key']
        age = known_at-item['event_at']
        score = {'exact': 2, 'family': 1.1, 'unknown': 0}[item['troop_match']]
        score += 1.5/(1+age/21600)
        if use_identity and arrival and not output['last_army_conflict'] and item['battle_id'] == arrival[3]:
            item['identity'] = 'arrival'
            item['identity_at'] = arrival[0]
            score += 2.4/(1+(cutoff-arrival[0])/172800)
        reference = speed_reference(by_formation[key], row, speed)
        item['speed'] = reference
        if use_speed and reference:
            spread = (reference['max']-reference['min'])/reference['median']
            difference = abs(reference['delta_pct'])/100
            weight = min(0.45, 0.15+0.08*reference['samples'])
            score += weight*(1-min(2, difference/(0.08+spread)))
        scores.append((score, item))
    match_rank = {'exact': 2, 'family': 1, 'unknown': 0}
    def rank(pair):
        score, item = pair
        return (match_rank[item['troop_match']], can_classify_from(item, row['id']), item['event_at'],
                item is same_army, score, item['hero_key'])

    scores.sort(key=rank, reverse=True)
    output['ambiguous'] = (len(scores) > 1 and scores[0][1] is not same_army
                           and rank(scores[0])[:3] == rank(scores[1])[:3])
    output['items'] = [item for _, item in scores]
    # An old map/arrival reference identifies cards, not their configuration forever.
    # Refresh every displayed field from one newer report of the same player/cards.
    current_report, resolution = resolve_current_report(reports, links, row['id'], cutoff, known_at,
        owner=row.get('owner'), live_codes=live_codes, ambiguous_player=ambiguous_player)
    if current_report:
        anchor = next(report for report in reports if report['battle_id']==resolution['anchor_battle_id']
                      and report['side']==resolution['anchor_side'])
        association = 'direct' if resolution['anchor_source']=='army_reference' else 'arrival'
        anchor_codes = report_troop_codes(anchor['troop_raw'],anchor['side'])
        linked = (anchor,forecast_heroes(anchor),association,troop_match(live_codes,anchor_codes),anchor_codes)
        output['latest_link'] = {'battle_id':anchor['battle_id'],'event_at':anchor['event_at'],
                                 'source':association,'npc':anchor['npc']}
        output['same_army_report_id'] = (anchor['battle_id'] if use_identity and association=='direct'
                                         and linked[3]=='exact' else None)
        if resolution:
            output['current_resolution'] = resolution
            heroes = forecast_heroes(current_report)
            codes = report_troop_codes(current_report['troop_raw'], current_report['side'])
            match = troop_match(live_codes, codes)
            output['force'] = last_force_observation((current_report, heroes,
                association if current_report is anchor else 'same_cards', match, codes),
                occupied_slots, known_at, previous_main)
            output['last_army_conflict'] = match=='conflict'
            current = next((item for item in output['items'] if item['battle_id'] == current_report['battle_id']
                            and item['side'] == current_report['side']), None)
            if current is None and match != 'conflict' and {h['position'] for h in heroes} == occupied_slots:
                current = {'hero_key':current_report['hero_key'], 'battle_id':current_report['battle_id'],
                           'side':current_report['side'], 'event_at':current_report['event_at'], 'troop_match':match,
                           'heroes':heroes, 'initial_hp':report_initial_hp(heroes,current_report), 'npc':current_report['npc'],
                           'report_army_id':current_report['army_id'], 'inferred_report_army_id':None,
                           'identity':linked[2] if current_report is linked[0] else 'same_cards', 'speed':None,
                           'card_instances':resolution.get('card_instances'), 'report_role_id':resolution.get('role_id')}
                output['items'].append(current)
            if current is not None and match != 'conflict':
                output['items'] = [current] + [item for item in output['items'] if item is not current]
                if current_report is not linked[0]:
                    output['last_army_conflict'] = False
                    current['identity'] = 'same_cards'
            else:
                resolution['unresolved'] = 'troop_conflict' if match == 'conflict' else 'incomplete_roster'
            output['current_report_id'] = current_report['battle_id']
        else:
            resolution = None
    else:
        resolution = None
    if output['items'] and not (resolution and resolution.get('unresolved')):
        first = output['items'][0]
        output['lead_loadout'] = {'battle_id':first['battle_id'],
                                  'heroes':detailed_loadout(first['heroes'])}
        output['lineup_confidence'] = 'uncertain'
        if first['troop_match'] == 'exact' and not output['ambiguous'] and not output['last_army_conflict']:
            output['lineup_confidence'] = {'direct':'same_army', 'arrival':'matched_arrival'}.get(first['identity'], 'uncertain')
    assessment = force_assessment(output, cutoff, len(recent_layouts))
    if resolution and resolution.get('unresolved'):
        assessment = {'kind':'unknown', 'battle_id':resolution['battle_id'], 'source':'current_report_incomplete'}
    hero_count = short_lineup_hero_count(live_codes)
    if hero_count is not None:
        if resolution and resolution.get('unresolved'):
            assessment = {'kind':'spartan','source':'map_roster'}
        assessment.update(kind='spartan', map_hero_count=hero_count)
    elif assessment['kind'] == 'unknown' and latest is None and len(pvp_samples) >= 2:
        strengths = [sample['initial_hp'] for sample in pvp_samples.values()]
        if max(strengths) <= 300 and not any(sample['main'] for sample in pvp_samples.values()):
            assessment['spartan_hint'] = {'sample_count':len(strengths), 'min_hp':min(strengths), 'max_hp':max(strengths),
                                          'reports':list(pvp_samples.values())[:3]}
    if assessment['kind'] == 'unknown' and 'spartan_hint' not in assessment and not ambiguous_player and not (resolution and resolution.get('unresolved')):
        assessment = main_pattern_assessment(output, cutoff) or assessment
    if assessment['kind'] == 'unknown' and latest:
        assessment['review_state'] = 'linked_report'
        hint = incomplete_main_reference(row, linked_reports['direct'] if latest is direct else None, reports, known_at)
        if hint:
            assessment['prior_main_hint'] = hint
    if row.get('army_group_id') and current_report is None:
        # A legion's members remain distinct personal armies. Personal troop
        # patterns cannot identify which member owns a matching formation.
        output.pop('lead_loadout',None)
        output['current_report_id'] = None
        output['force_assessment'] = {'kind':'spartan' if hero_count is not None else 'unknown',
                                      'source':'army_group_identity_pending'}
        return output
    # One report drives the label, map, sidebar and stored live loadout.
    # Alternative armies remain in items for audit, never as the main card.
    selected_id = output.get('current_report_id') or assessment.get('battle_id')
    selected = next((item for item in output['items'] if item['battle_id'] == selected_id), None)
    if selected is None and selected_id and (output.get('force') or {}).get('battle_id') == selected_id:
        force = output['force']
        source = next((report for report in reports if report['battle_id'] == selected_id), {})
        selected = {**force, 'hero_key':','.join(str(hero['id']) for hero in force['heroes']),
            'side':source.get('side'), 'report_army_id':source.get('army_id'),
            'inferred_report_army_id':row['id'] if force['association']=='arrival' else None,
            'identity':force['association'], 'speed':None,
            'troop_match':troop_match(live_codes, report_troop_codes(source.get('troop_raw'), source.get('side','attack')))}
    unresolved = bool(resolution and resolution.get('unresolved'))
    if not selected_id and output['items']:
        selected = output['items'][0]
        selected_id = selected['battle_id']
    if selected_id and selected is None:
        unresolved = True
        output['current_resolution'] = {**(output.get('current_resolution') or {}),
            'battle_id':selected_id, 'unresolved':'incomplete_roster'}
    output['current_report_id'] = selected_id
    if selected is not None and not unresolved:
        if not selected.get('hero_key'):
            selected['hero_key'] = ','.join(str(hero['id']) for hero in selected['heroes'])
        output['items'] = [selected] + [item for item in output['items'] if item is not selected]
        output['lead_loadout'] = {'battle_id':selected_id, 'heroes':detailed_loadout(selected['heroes'])}
    else:
        output.pop('lead_loadout', None)
    output['force_assessment'] = assessment
    return output


def current_prediction(row, history, snapshot, *, revision=0, profile_id=None):
    """One display decision for incoming cards, map popups and selected armies.

    A display reference never creates an army/report identity in the archive.
    """
    output = predict(row, history, snapshot['server_now'])
    resolution = output.get('current_resolution') or {}
    verified = bool(resolution.get('anchor_battle_id') and not resolution.get('unresolved'))
    selected_id = output.get('current_report_id')
    selected = next((item for item in output['items'] if item['battle_id'] == selected_id), None)
    if selected is None and (output.get('force') or {}).get('battle_id') == selected_id:
        selected = output['force']
    reason = resolution.get('unresolved')
    if not verified:
        codes = tuple(t['code'] for t in row.get('troops', ()))
        peers = [other['id'] for other in snapshot['armies']
                 if other['id'] != row['id'] and not other.get('stale')
                 and other.get('owner') == row.get('owner')
                 and other.get('player_name') == row.get('player_name')
                 and str(other.get('union_id')) == str(row.get('union_id'))
                 and tuple(t['code'] for t in other.get('troops', ())) == codes]
        if peers:
            reason = 'multiple_personal_armies'
            output['indistinguishable_armies'] = peers
        elif selected and not can_classify_from(selected, row['id']):
            reason = 'another_army_reference'
        elif selected and selected.get('troop_match') != 'exact':
            reason = 'no_exact_troop_reference'
        elif row.get('army_group_id'):
            reason = 'army_group_identity_pending'
    if reason:
        selected = None
        output.pop('lead_loadout', None)
        output['force_assessment'] = {
            'kind':'spartan' if short_lineup_hero_count(output['troop_codes']) is not None else 'unknown',
            'source':reason}
    output.update(display_report=selected, display_verified=verified and selected is not None,
                  display_reason=reason, source_revision=revision, evaluated_at=snapshot['server_now'],
                  owner=row.get('owner'), player_name=row.get('player_name'), union_id=row.get('union_id'),
                  profile_id=profile_id)
    return output


def predict_alerts(archive, snapshot, *, record=False, army_id=None):
    ids = {alert['army_id'] for alert in snapshot['alerts']}
    if army_id is not None:
        ids = {army_id}
    histories = {}
    result = {}
    reader_context = archive.read_snapshot() if hasattr(archive, 'read_snapshot') and not getattr(archive, 'read_only', False) else nullcontext(archive)
    with reader_context as reader, reader.lock:
        for row in snapshot['armies']:
            if row['id'] not in ids:
                continue
            if time.monotonic() >= getattr(reader, 'read_deadline', float('inf')):
                raise TimeoutError('阵容查询超过时限，实时采集继续运行')
            identity = (row['player_name'], row['union_id'])
            if identity not in histories:
                projection = getattr(reader,'lineup_projection',None)
                histories[identity] = projection.history(reader,*identity) if projection else player_history(reader.db, reader.profile_id, *identity)
            revision = reader.db.execute('''SELECT revision FROM lineup_player_revisions
                WHERE profile_id=? AND player_name=? AND union_id=?''',
                (reader.profile_id, *identity)).fetchone()
            result[row['id']] = current_prediction(row, histories[identity], snapshot,
                revision=revision[0] if revision else 0, profile_id=reader.profile_id)
    if record and result:
        with archive.lock, archive.db:
            if archive.profile_id != reader.profile_id:
                return {}
            for row in snapshot['armies']:
                if row['id'] in result:
                    record_forecast(archive.db, archive.profile_id, row, result[row['id']], snapshot['server_now'])
    return result


def army_lineup(archive, snapshot, army_id):
    """Archive-compatible live view, driven by the same decision as batch alerts."""
    prediction = predict_alerts(archive, snapshot, army_id=army_id).get(army_id)
    answer = {'live_lineup':True, 'profile_id':archive.profile_id, 'items':[],
              'match':'no_current_observation', 'prediction':prediction}
    if prediction is None:
        return answer
    row = next(row for row in snapshot['armies'] if row['id'] == army_id)
    # Verify the snapshot's owner before exposing archive evidence for its ID.
    projection = getattr(archive, 'lineup_projection', None)
    token = projection.token(archive, army_id) if projection else None
    if token and (str(token['owner_uid']) != str(row.get('owner'))
                  or token['player_name'] != row.get('player_name')
                  or str(token['union_id']) != str(row.get('union_id'))):
        raise TimeoutError('队伍所属信息正在更新，请稍后重试')
    report = prediction['display_report']
    answer.update(troops=row.get('troops', []), current_report_id=report['battle_id'] if report else None,
                  current_resolution=prediction.get('current_resolution'), match='live_reference')
    if report:
        # Retain the real configuration hash/group evidence when it is verified.
        source = archive.query('army_lineups', army_id=army_id) if prediction['display_verified'] else {}
        item = next((item for item in source.get('items', []) if item['battle_id'] == report['battle_id']), None)
        if item is None:
            item = {'battle_id':report['battle_id'], 'event_at':report['event_at'],
                    'player_name':row['player_name'], 'union_id':row['union_id'],
                    'data':{'heroes':report['heroes']}}
        answer['items'] = [{**item, 'data':{**item['data'], 'heroes':report['heroes']}}]
        if prediction['display_verified']:
            answer['match'] = source.get('match', 'live_reference')
    return answer


def forecast_key(profile, march):
    return digest([profile, march[0], *(int(value) for value in march[1:])])


def archived_march_key(profile, march):
    fields = ('army_id','from_x','from_y','to_x','to_y','begin_at','end_at','state_code')
    return forecast_key(profile, tuple(march[k] for k in fields))


def loadout_fields(heroes):
    return [{'position': h['position'], 'hero_id': h['id'], 'red_stars': h.get('red_stars'),
             'skills': [{'id': s['id'], 'level': s.get('level')} for s in h.get('skills') or ()]}
            for h in heroes]


def detailed_loadout(heroes):
    result = loadout_fields(heroes)
    for row, hero in zip(result, heroes):
        row.update({field:hero.get(field) for field in ('level','gear','gear_reported','initial_hp','remaining_hp','wounded_hp')})
        row['troop_code'] = hero.get('troop_code')
        slots = hero.get('skill_slot_ids')
        if slots and all(slot is not None for slot in slots):
            row['skill_slots_verified'] = True
    return result


def saved_same_army_loadout(data, army_id):
    snapshot = data.get('same_army_loadout')
    if snapshot:
        return snapshot if snapshot['army_id'] == army_id else None
    # Older forecasts can be audited only when they already froze the same
    # direct report shown first, with verified troop codes for every position.
    first = next(iter(data['items']), {})
    force = data.get('force') or {}
    codes = data.get('troop_codes') or ()
    heroes = force.get('heroes') or ()
    positions = {h.get('position') for h in heroes}
    occupied = {i for i, code in enumerate(codes, 1) if code}
    if (first.get('identity') == 'direct' and force.get('association') == 'direct'
            and force['battle_id'] == first['battle_id'] == data.get('same_army_report_id', first['battle_id'])
            and first.get('report_army_id') in (None, army_id) and not data.get('last_army_conflict')
            and occupied and positions == occupied and len(heroes) == len(occupied)
            and all(h.get('id') and h.get('troop_code') == codes[h['position']-1] for h in heroes)):
        return {'army_id': army_id, 'battle_id': force['battle_id'], 'heroes': loadout_fields(heroes)}
    return None


def compare_loadouts(predicted, actual):
    def compare(before, after):
        status = 'unavailable' if before is None or after is None else 'match' if before == after else 'mismatch'
        return {'status': status, 'predicted': before, 'actual': after}

    def summarize(statuses):
        if 'mismatch' in statuses:
            return 'mismatch'
        return 'match' if statuses and all(status == 'match' for status in statuses) else 'unavailable'

    def complete_skills(hero):
        skills = hero.get('skills') or ()
        return skills if hero.get('skill_slots_verified') or (
            len(skills) == 3 and len({s['id'] for s in skills if s['id'] > 0}) == 3) else None

    include_troops = any('troop_code' in hero for hero in predicted)
    predicted = {hero['position']: hero for hero in predicted}
    actual = {hero['position']: hero for hero in actual}
    positions = []
    fields = ('hero', 'red_stars', 'skills', 'skill_levels') + (('troop',) if include_troops else ())
    for pos in sorted(predicted.keys() | actual.keys()):
        before, after = predicted.get(pos, {}), actual.get(pos, {})
        hero = compare(before.get('hero_id', 0), after.get('hero_id', 0))
        same_hero = hero['status'] == 'match'
        a, b = complete_skills(before), complete_skills(after)
        skills = compare([s['id'] for s in a] if a is not None else None, [s['id'] for s in b] if b is not None else None)
        levels = compare([s['level'] for s in a] if a is not None and all(s['level'] for s in a) else None,
                         [s['level'] for s in b] if b is not None and all(s['level'] for s in b) else None)
        red = compare(before.get('red_stars'), after.get('red_stars'))
        if not same_hero:
            for field in (red, skills, levels):
                field['status'] = 'unavailable'
        elif skills['status'] != 'match':
            levels['status'] = 'unavailable'
        positions.append({'position': pos, 'hero': hero, 'red_stars': red, 'skills': skills, 'skill_levels': levels})
        if include_troops:
            positions[-1]['troop'] = compare(before.get('troop_code'), after.get('troop_code'))
    statuses = {field: summarize([item[field]['status'] for item in positions]) for field in fields}
    return {'status': summarize(list(statuses.values())), 'fields': statuses, 'positions': positions}


def record_forecast(db, profile, row, prediction, issued_at):
    if not row.get('from') or not row.get('target') or not row.get('begin') or not row.get('end') or issued_at >= row['end']:
        return
    march = (row['id'], *row['from'], *row['target'], row['begin'], row['end'], row['state_code'])
    key = forecast_key(profile, march)
    import reported_archive
    target=reported_archive.storage(db,profile,row['id'],'lineup_forecasts')
    forecasts=reported_archive.live_table(db,'lineup_forecasts')
    items = [{'hero_key':item['hero_key'], 'heroes':[h['name'] for h in item['heroes']], 'battle_id':item['battle_id'],
              'identity':item['identity'], 'event_at':item.get('event_at'), 'initial_hp':item.get('initial_hp'),
              'npc':item.get('npc'), 'report_army_id':item.get('report_army_id'),
              'card_instances':item.get('card_instances'), 'report_role_id':item.get('report_role_id'),
              'inferred_report_army_id':item.get('inferred_report_army_id')} for item in prediction['items']]
    data = {'items':items, 'troop_codes':prediction['troop_codes'], 'ambiguous':prediction['ambiguous'],
            'last_army_conflict':prediction.get('last_army_conflict', False),
            'same_army_report_id':prediction.get('same_army_report_id'),
            'lineup_confidence':prediction.get('lineup_confidence'), 'force':prediction.get('force'),
            'force_assessment':prediction.get('force_assessment')}
    data['current_report_id'] = prediction.get('current_report_id')
    data['current_resolution'] = prediction.get('current_resolution')
    if 'display_report' in prediction:
        data['display_report_id'] = (prediction['display_report'] or {}).get('battle_id')
        data['display_verified'] = prediction['display_verified']
        data['display_reason'] = prediction['display_reason']
        data['indistinguishable_armies'] = prediction.get('indistinguishable_armies', [])
    if prediction.get('lead_loadout'):
        data['lead_loadout'] = prediction['lead_loadout']
    same_army = next((item for item in prediction['items'] if item['battle_id'] == data['same_army_report_id']
                      and item.get('report_army_id') == row['id'] and item['identity'] == 'direct'), None)
    if same_army:
        data['same_army_loadout'] = {'army_id': row['id'], 'battle_id': same_army['battle_id'],
                                    'heroes': loadout_fields(same_army['heroes'])}
    fingerprint = digest(data)
    previous = db.execute(f'''SELECT fingerprint,model_version FROM {forecasts} WHERE profile_id=? AND march_key=?
        ORDER BY issued_at DESC LIMIT 1''', (profile, key)).fetchone()
    if previous and tuple(previous) == (fingerprint, MODEL_VERSION):
        return
    db.execute(f'''INSERT OR IGNORE INTO {target}(profile_id,march_key,issued_at,army_id,from_x,from_y,to_x,to_y,
        begin_at,end_at,state_code,model_version,fingerprint,data) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        (profile, key, issued_at, *march, MODEL_VERSION, fingerprint, packed(data)))


def review_report(db, profile, battle_id):
    side = db.execute(f'''SELECT c.side,c.hero_key,c.heroes,c.event_at,c.army_id,{LOADOUT_FIELDS},
        json_extract(c.features,'$.all_skill_info') AS skill_raw
        FROM combat_sides c WHERE c.profile_id=? AND c.battle_id=? AND c.side='attack' ''',
                      (profile, battle_id)).fetchone()
    if not side or not side['hero_key'] or not side['event_at']:
        return None
    marches = db.execute('''SELECT m.*,l.method FROM battle_march_links l JOIN march_samples m
        ON m.profile_id=l.profile_id AND m.id=l.march_id WHERE l.profile_id=? AND l.battle_id=? AND l.side='attack' ''',
        (profile, battle_id)).fetchall()
    if len(marches) != 1 or marches[0]['method'] not in ('coordinate_time_match','unique_attack_arrival'):
        return {'status':'unmatched_march'}
    march = marches[0]
    if side['army_id'] and side['army_id'] != march['army_id']:
        return {'status':'unmatched_march'}
    key = archived_march_key(profile, march)
    saved = db.execute('''SELECT issued_at,model_version,data FROM lineup_forecasts WHERE profile_id=? AND march_key=?
        AND issued_at<? ORDER BY issued_at''', (profile, key, min(side['event_at'], march['end_at']))).fetchall()
    info = {'army_id':march['army_id'], 'method':march['method']}
    if not saved:
        return {'status':'forecast_missing', **info}

    last_data = json.loads(saved[-1]['data'])
    troop_codes = last_data['troop_codes'] or map_troop_codes(march['hero_type'])
    expected = {position for position, code in enumerate(troop_codes, 1) if code}
    if expected:
        peers = db.execute('''SELECT c.battle_id,c.hero_key,c.heroes FROM battle_march_links l JOIN combat_sides c
            ON c.profile_id=l.profile_id AND c.battle_id=l.battle_id AND c.side=l.side WHERE l.profile_id=?
            AND l.march_id=? AND l.side='attack' AND l.method IN ('coordinate_time_match','unique_attack_arrival')
            ORDER BY c.event_at,c.battle_id''', (profile, march['id']))
        complete = [r for r in peers if {h['position'] for h in json.loads(r['heroes'])} == expected]
        if len({r['hero_key'] for r in complete}) > 1:
            return {'status':'conflicting_reports', **info}
        if {h['position'] for h in json.loads(side['heroes'])} != expected:
            return {'status':'partial_report', 'comparison_battle_id':complete[0]['battle_id'] if complete else None, **info}

    def assessment(record):
        data = json.loads(record['data'])
        items = data['items']
        rank = next((i for i,item in enumerate(items,1) if item['hero_key'] == side['hero_key']), None)
        outcome = 'candidate_missing' if rank is None else 'top1' if rank == 1 else 'top3' if rank <= 3 else 'ranking_miss'
        first = items[0] if items else None
        if first:
            ids = first['hero_key'].split(',')
            if len(ids) == len(first['heroes']):
                first['heroes'] = [hero_name(hero_id, name) for hero_id, name in zip(ids, first['heroes'])]
        return {'issued_at':record['issued_at'], 'model_version':record['model_version'], 'rank':rank,
                'outcome':outcome, 'candidate_count':len(items), 'first':first,
                'lineup_confidence':data.get('lineup_confidence'), 'force_assessment':data.get('force_assessment')}

    result = {'status':'matched', **info, 'first':assessment(saved[0]), 'last':assessment(saved[-1])}
    actual_codes = report_troop_codes(side['troop_raw'], side['side'])
    if troop_codes and actual_codes:
        result['troop_audit'] = [{'position':pos,'before':before,'actual':after,'match':before == after}
                                 for pos, (before, after) in enumerate(zip(troop_codes, actual_codes), 1)]
    lead = last_data.get('lead_loadout')
    actual_heroes = forecast_heroes(dict(side))
    if lead:
        actual = detailed_loadout(actual_heroes)
        result['lead_audit'] = {'source_battle_id':lead['battle_id'], **compare_loadouts(lead['heroes'], actual)}
    exact = [(record, saved_same_army_loadout(json.loads(record['data']), march['army_id'])) for record in saved]
    exact = [(record, source) for record, source in exact if source]
    if exact:
        actual = loadout_fields(actual_heroes)
        def audit(entry):
            record, source = entry
            return {'issued_at': record['issued_at'], 'model_version': record['model_version'],
                    'source_battle_id': source['battle_id'], **compare_loadouts(source['heroes'], actual)}
        first = audit(exact[0])
        result['same_army_audit'] = {'actual_battle_id': str(battle_id), **info, 'first': first,
                                     'last': audit(exact[-1]) if len(exact) > 1 else first}
    return result
