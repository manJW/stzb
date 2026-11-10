"""Rank incoming formations using only evidence available before impact."""
import json
import math
from collections import defaultdict
from statistics import median

from combat_loadout import forecast_heroes, hero_name
from observation_codec import digest, map_troop_codes, number, packed, report_troop_codes, short_lineup_hero_count, troop_families


MODEL_VERSION = '17'
MAIN_MIN_LEVEL = 40
LOW_FORCE_MAX_AGE = 3600
POSSIBLE_MAIN_MAX_AGE = 48 * 3600
MAIN_PATTERN_MAX_AGE = 96 * 3600
SCOUT_HINT_MAX_AGE = 48 * 3600
# A PvE report can still name a human attacker. Use the role and hero-instance
# fields on the report itself so a future roster refresh cannot affect replay.
HUMAN_ATTACK = """(c.side='attack' AND c.union_id NOT IN ('','0')
    AND COALESCE(CAST(json_extract(c.features,'$.attack_role_id') AS TEXT),'') NOT IN ('','0')
    AND COALESCE(json_extract(c.features,'$.attack_idu'),'') NOT IN ('','0','0,0,0,0,0'))"""
LOADOUT_FIELDS = """json_extract(c.features,'$.'||c.side||'_advance') AS advance_raw,
    json_extract(c.features,'$.'||c.side||'er_gear_info') AS gear_raw,
    json_extract(c.features,'$.'||c.side||'_hero_type') AS troop_raw"""


def player_history(db, profile, name, union, *, include_pve=True):
    params = (profile, name, union, include_pve)
    reports = db.execute(f'''SELECT c.battle_id,c.side,c.hero_key,c.heroes,c.army_id,c.event_at,c.observed_at,c.npc,c.wid,
        {LOADOUT_FIELDS},
        json_extract(c.features,'$.'||c.side||'_hp') AS reported_hp,
        json_extract(c.features,'$.binding_evidence.observed_at') AS binding_at,
        json_extract(c.features,'$.binding_evidence.world_uid') AS binding_owner
        FROM combat_sides c WHERE c.profile_id=? AND c.player_name=? AND c.union_id=?
        AND (c.npc<>1 OR (? AND {HUMAN_ATTACK})) AND c.hero_key<>'' AND c.event_at>0
        ORDER BY c.event_at DESC,c.observed_at DESC,c.battle_id DESC,c.side''', params).fetchall()
    links = defaultdict(list)
    for row in db.execute(f'''SELECT m.*,l.method,c.battle_id,c.side FROM combat_sides c
        JOIN battle_march_links l ON l.profile_id=c.profile_id AND l.battle_id=c.battle_id AND l.side=c.side
        JOIN march_samples m ON m.profile_id=l.profile_id AND m.id=l.march_id
        WHERE c.profile_id=? AND c.player_name=? AND c.union_id=? AND c.side='attack'
        AND (c.npc<>1 OR (? AND {HUMAN_ATTACK}))
        AND (l.method='unique_attack_arrival' OR (l.method='coordinate_time_match' AND c.army_id=m.army_id))''', params):
        links[row['battle_id'], row['side']].append(dict(row))
    owners = db.execute('''SELECT DISTINCT world_uid FROM players WHERE profile_id=? AND name=? AND union_id=?
        AND world_uid IS NOT NULL LIMIT 2''', params[:3]).fetchall()
    return [dict(r) for r in reports], links, len(owners) > 1


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


def meets_main_threshold(heroes):
    if len(heroes) != 3 or any(type(hero.get('level')) is not int or hero['level'] < MAIN_MIN_LEVEL for hero in heroes):
        return False
    for hero in heroes:
        skills = hero.get('skills') or ()
        skill_ids = {skill['id'] for skill in skills if type(skill.get('id')) is int and skill['id'] > 0}
        if len(skills) != 3 or len(skill_ids) != 3:
            return False
    return True


def report_initial_hp(heroes, report):
    initial = troop_total(heroes, 'initial_hp')
    reported = number(report.get('reported_hp'))
    return initial if initial and (reported <= 0 or initial == reported) else None


def last_force_observation(linked, occupied_slots, known_at):
    complete = []
    for report, heroes, association, match, reported_codes in linked:
        report_slots = {i for i,code in enumerate(reported_codes,1) if code}
        slots = {hero['position'] for hero in heroes}
        if match == 'conflict' or slots != (occupied_slots or report_slots or {1,2,3}):
            continue
        initial = report_initial_hp(heroes, report)
        complete.append((report, heroes, association, match, initial, meets_main_threshold(heroes)))
    if not complete or len({row[0]['hero_key'] for row in complete}) != 1:
        return None

    # Prefer a fully recorded main configuration. Among equal reports, later
    # rounds at the same arrival time can carry a reduced troop count.
    report, heroes, association, match, initial, is_main = max(complete, key=lambda row:(row[5], row[4] or 0, row[2]=='direct'))
    priority = 'unknown' if initial is None else 'standard'
    if is_main:
        priority = 'main'
    elif initial and initial <= 300 and match == 'exact' and known_at-report['event_at'] <= LOW_FORCE_MAX_AGE:
        priority = 'low'
    return {'priority':priority, 'battle_id':report['battle_id'], 'event_at':report['event_at'],
            'association':association, 'initial_hp':initial, 'remaining_hp':troop_total(heroes, 'remaining_hp'), 'npc':report['npc'],
            'heroes':heroes}


def force_assessment(prediction, cutoff):
    def evidence(kind, report, strength, source, confidence='inferred'):
        basis = {'main_basis':'skills'} if kind == 'main' else {}
        return {'kind':kind, 'battle_id':report['battle_id'], 'event_at':report['event_at'],
                'initial_hp':strength, 'levels':[hero.get('level') for hero in report['heroes']],
                'source':source, 'confidence':confidence, 'npc':report.get('npc'), **basis}

    personal = [(item, item.get('initial_hp')) for item in prediction['items'] if item['event_at'] < cutoff]
    exact = [(item, hp) for item, hp in personal if item['troop_match'] == 'exact']
    compatible_low = (bool(exact) and not prediction['last_army_conflict']
                      and all(hp is not None and 0 < hp <= 300 and not meets_main_threshold(item['heroes'])
                              for item, hp in exact))
    force = prediction.get('force')
    if force and (force['initial_hp'] is not None or force['priority'] == 'main'):
        hp = force['initial_hp']
        if force['priority'] in ('main', 'low'):
            kind = 'main' if force['priority'] == 'main' else 'spartan'
            confidence = 'linked' if force['association'] == 'direct' else 'inferred'
            return evidence(kind, force, hp, force['association'], confidence)
        if compatible_low and any(item['battle_id'] == force['battle_id'] for item, _ in exact):
            return evidence('spartan', force, hp, force['association'])
        return evidence('unknown', force, hp, force['association'], 'reference')

    if compatible_low:
        item, hp = max(exact, key=lambda candidate:candidate[0]['event_at'])
        return evidence('spartan', item, hp, 'same_player_exact')

    for item, hp in exact + [entry for entry in personal if entry[0]['troop_match'] != 'exact']:
        main = (len(exact) == 1 and exact[0][0] is item and prediction['items'][0] is item
                and not prediction['ambiguous'] and not prediction['last_army_conflict']
                and cutoff - item['event_at'] <= POSSIBLE_MAIN_MAX_AGE
                and meets_main_threshold(item['heroes']))
        if not hp and not main:
            continue
        source = 'same_player_exact' if item['troop_match'] == 'exact' else 'same_player'
        return evidence('main' if main else 'unknown', item, hp, source, 'inferred' if main else 'reference')
    return {'kind':'unknown'}


def main_pattern_assessment(prediction, cutoff):
    codes = prediction['troop_codes']
    if len(codes) != 3 or not all(codes) or prediction['force'] or prediction['last_army_conflict']:
        return None
    personal = [item for item in prediction['items'] if item['troop_match'] == 'exact'
                and 0 < cutoff - item['event_at'] <= MAIN_PATTERN_MAX_AGE]
    if not personal or not all(meets_main_threshold(item['heroes']) for item in personal):
        return None
    sample = max(personal, key=lambda item: item['event_at'])
    return {'kind':'main', 'confidence':'inferred', 'source':'same_player_exact', 'battle_id':sample['battle_id'],
            'event_at':sample['event_at'], 'initial_hp':sample['initial_hp'], 'npc':sample['npc'],
            'levels':[h['level'] for h in sample['heroes']], 'main_basis':'skills'}


def predict(row, history, known_at, *, use_identity=True, use_speed=True):
    reports, links, ambiguous_player = history
    speed = march_speed(row)
    live_codes = tuple(t['code'] for t in row.get('troops', ()))
    output = {'army_id': row['id'], 'begin': row.get('begin'), 'end': row.get('end'), 'speed': speed,
              'from': row.get('from'), 'target': row.get('target'), 'troop_codes': live_codes,
              'items': [], 'same_army_report_id': None, 'ambiguous': False, 'last_army_conflict': False}
    cutoff = min(row.get('begin') or known_at, known_at)
    occupied_slots = {i for i,code in enumerate(live_codes,1) if code}
    candidates, recent_links, pvp_samples = {}, {}, {}
    linked_reports = {'direct': [], 'arrival': []}
    same_army = arrival_item = None
    observations = defaultdict(dict)
    for report in reports:
        if report['event_at'] >= known_at or report['observed_at'] > known_at:
            continue
        hero_key = report['hero_key']
        direct = (report['event_at'] < cutoff and report['army_id'] == row['id'] and (report['binding_at'] or 0) <= known_at
                  and report['binding_owner'] in (None, row.get('owner')))
        reported_codes = report_troop_codes(report['troop_raw'], report['side'])
        match = troop_match(live_codes, reported_codes)
        routes = [m for m in links[report['battle_id'], report['side']]
                  if report['event_at'] < cutoff and m['observed_at'] <= known_at and m['end_at'] <= cutoff
                  and (m['method'] == 'unique_attack_arrival' or (report['binding_at'] or 0) <= known_at)]
        arrival_armies = {m['army_id'] for m in routes if m['method'] == 'unique_attack_arrival'}
        inferred_id = next(iter(arrival_armies)) if len(arrival_armies) == 1 else None
        inferred = report['army_id'] in (None, row['id']) and inferred_id == row['id']
        if direct or inferred:
            association = 'direct' if direct else 'arrival'
            recent_links.setdefault(association, (report['event_at'], hero_key, match, report['battle_id']))
            group = linked_reports[association]
            if (direct or not ambiguous_player) and (not group or group[0][0]['event_at'] == report['event_at']):
                group.append((report, forecast_heroes(report), association, match, reported_codes))
        if match == 'conflict' or (ambiguous_player and not direct):
            continue
        # A full lineup can share its broad troop classes with another army
        # of the same player. A mismatch in the detailed codes needs army evidence.
        if match == 'family' and len(occupied_slots) == 3 and not (direct or inferred):
            continue
        heroes = forecast_heroes(report)
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
        item = {'hero_key': hero_key, 'battle_id': report['battle_id'], 'event_at': report['event_at'],
                'troop_match': match, 'heroes': heroes, 'initial_hp': initial_hp, 'npc': report['npc'],
                'report_army_id': report['army_id'], 'inferred_report_army_id': inferred_id,
                'identity': None, 'speed': None}
        # Keep the same-army report separate from a newer report about a
        # different army that happens to contain the same heroes.
        if direct and match == 'exact' and report['battle_id'] == recent_links['direct'][3]:
            same_army = item
        if inferred and match == 'exact' and report['battle_id'] == recent_links['arrival'][3]:
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
    direct_reports = linked_reports['direct']
    output['force'] = last_force_observation(direct_reports or linked_reports['arrival'], occupied_slots, known_at)
    # A newer uniquely matched arrival with the current exact troop layout
    # can supersede an older direct report for a different lineup.
    if output['force'] is None and direct and direct[2] == 'conflict' and arrival_item and arrival[0] > direct[0]:
        recovered = last_force_observation(linked_reports['arrival'], occupied_slots, known_at)
        if recovered and recovered['priority'] == 'main' and recovered['battle_id'] == arrival_item['battle_id']:
            latest, output['force'] = arrival, recovered
            if use_identity:
                candidates[(arrival_item['hero_key'], False)] = arrival_item
    output['last_army_conflict'] = bool(latest and latest[2] == 'conflict')
    if same_army and (not use_identity or len({r['hero_key'] for r, *_ in direct_reports}) != 1):
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
            weight = min(1.8, 0.7+0.35*reference['samples'])
            score += weight*(1-min(2, difference/(0.08+spread)))
        scores.append((score, item))
    scores.sort(key=lambda pair:(pair[1] is same_army, pair[0], pair[1]['event_at'], pair[1]['hero_key']), reverse=True)
    output['ambiguous'] = same_army is None and len(scores) > 1 and scores[0][0]-scores[1][0] < 0.45
    output['items'] = [item for _, item in scores]
    if output['items']:
        first = output['items'][0]
        output['lineup_confidence'] = 'uncertain'
        if first['troop_match'] == 'exact' and not output['ambiguous'] and not output['last_army_conflict']:
            output['lineup_confidence'] = {'direct':'same_army', 'arrival':'matched_arrival'}.get(first['identity'], 'uncertain')
    assessment = force_assessment(output, cutoff)
    hero_count = short_lineup_hero_count(live_codes)
    if hero_count is not None:
        assessment.update(kind='spartan', map_hero_count=hero_count)
    elif assessment['kind'] == 'unknown' and latest is None and len(pvp_samples) >= 2:
        strengths = [sample['initial_hp'] for sample in pvp_samples.values()]
        if max(strengths) <= 300 and not any(sample['main'] for sample in pvp_samples.values()):
            assessment['spartan_hint'] = {'sample_count':len(strengths), 'min_hp':min(strengths), 'max_hp':max(strengths),
                                          'reports':list(pvp_samples.values())[:3]}
    if assessment['kind'] == 'unknown' and 'spartan_hint' not in assessment and not ambiguous_player:
        assessment = main_pattern_assessment(output, cutoff) or assessment
    output['force_assessment'] = assessment
    return output


def predict_alerts(archive, snapshot, *, record=False, army_id=None):
    ids = {alert['army_id'] for alert in snapshot['alerts']}
    if army_id is not None:
        ids.intersection_update((army_id,))
    histories = {}
    result = {}
    with archive.lock:
        for row in snapshot['armies']:
            if row['id'] not in ids:
                continue
            identity = (row['player_name'], row['union_id'])
            if identity not in histories:
                histories[identity] = player_history(archive.db, archive.profile_id, *identity)
            result[row['id']] = predict(row, histories[identity], snapshot['server_now'])
        if record and result:
            with archive.db:
                for row in snapshot['armies']:
                    if row['id'] in result:
                        record_forecast(archive.db, archive.profile_id, row, result[row['id']], snapshot['server_now'])
    return result


def forecast_key(profile, march):
    return digest([profile, march[0], *(int(value) for value in march[1:])])


def archived_march_key(profile, march):
    fields = ('army_id','from_x','from_y','to_x','to_y','begin_at','end_at','state_code')
    return forecast_key(profile, tuple(march[k] for k in fields))


def loadout_fields(heroes):
    return [{'position': h['position'], 'hero_id': h['id'], 'red_stars': h.get('red_stars'),
             'skills': [{'id': s['id'], 'level': s.get('level')} for s in h.get('skills') or ()]}
            for h in heroes]


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
        return skills if len(skills) == 3 and len({s['id'] for s in skills if s['id'] > 0}) == 3 else None

    predicted = {hero['position']: hero for hero in predicted}
    actual = {hero['position']: hero for hero in actual}
    positions = []
    fields = ('hero', 'red_stars', 'skills', 'skill_levels')
    for pos in sorted(predicted.keys() | actual.keys()):
        before, after = predicted.get(pos, {}), actual.get(pos, {})
        hero = compare(before.get('hero_id', 0), after.get('hero_id', 0))
        same_hero = hero['status'] == 'match'
        a, b = complete_skills(before), complete_skills(after)
        skills = compare([s['id'] for s in a] if a else None, [s['id'] for s in b] if b else None)
        levels = compare([s['level'] for s in a] if a and all(s['level'] for s in a) else None,
                         [s['level'] for s in b] if b and all(s['level'] for s in b) else None)
        red = compare(before.get('red_stars'), after.get('red_stars'))
        if not same_hero:
            for field in (red, skills, levels):
                field['status'] = 'unavailable'
        elif skills['status'] != 'match':
            levels['status'] = 'unavailable'
        positions.append({'position': pos, 'hero': hero, 'red_stars': red, 'skills': skills, 'skill_levels': levels})
    statuses = {field: summarize([item[field]['status'] for item in positions]) for field in fields}
    return {'status': summarize(list(statuses.values())), 'fields': statuses, 'positions': positions}


def record_forecast(db, profile, row, prediction, issued_at):
    if not row.get('from') or not row.get('target') or not row.get('begin') or not row.get('end') or issued_at >= row['end']:
        return
    march = (row['id'], *row['from'], *row['target'], row['begin'], row['end'], row['state_code'])
    key = forecast_key(profile, march)
    items = [{'hero_key':item['hero_key'], 'heroes':[h['name'] for h in item['heroes']], 'battle_id':item['battle_id'],
              'identity':item['identity'], 'event_at':item.get('event_at'), 'initial_hp':item.get('initial_hp'),
              'npc':item.get('npc'), 'report_army_id':item.get('report_army_id'),
              'inferred_report_army_id':item.get('inferred_report_army_id')} for item in prediction['items']]
    data = {'items':items, 'troop_codes':prediction['troop_codes'], 'ambiguous':prediction['ambiguous'],
            'last_army_conflict':prediction.get('last_army_conflict', False),
            'same_army_report_id':prediction.get('same_army_report_id'),
            'lineup_confidence':prediction.get('lineup_confidence'), 'force':prediction.get('force'),
            'force_assessment':prediction.get('force_assessment')}
    same_army = next((item for item in prediction['items'] if item['battle_id'] == data['same_army_report_id']
                      and item.get('report_army_id') == row['id'] and item['identity'] == 'direct'), None)
    if same_army:
        data['same_army_loadout'] = {'army_id': row['id'], 'battle_id': same_army['battle_id'],
                                    'heroes': loadout_fields(same_army['heroes'])}
    fingerprint = digest(data)
    previous = db.execute('''SELECT fingerprint,model_version FROM lineup_forecasts WHERE profile_id=? AND march_key=?
        ORDER BY issued_at DESC LIMIT 1''', (profile, key)).fetchone()
    if previous and tuple(previous) == (fingerprint, MODEL_VERSION):
        return
    db.execute('''INSERT OR IGNORE INTO lineup_forecasts(profile_id,march_key,issued_at,army_id,from_x,from_y,to_x,to_y,
        begin_at,end_at,state_code,model_version,fingerprint,data) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        (profile, key, issued_at, *march, MODEL_VERSION, fingerprint, packed(data)))


def review_report(db, profile, battle_id):
    side = db.execute(f'''SELECT c.side,c.hero_key,c.heroes,c.event_at,c.army_id,{LOADOUT_FIELDS}
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

    troop_codes = map_troop_codes(march['hero_type']) or json.loads(saved[-1]['data'])['troop_codes']
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
    exact = [(record, saved_same_army_loadout(json.loads(record['data']), march['army_id'])) for record in saved]
    exact = [(record, source) for record, source in exact if source]
    if exact:
        actual = loadout_fields(forecast_heroes(dict(side)))
        def audit(entry):
            record, source = entry
            return {'issued_at': record['issued_at'], 'model_version': record['model_version'],
                    'source_battle_id': source['battle_id'], **compare_loadouts(source['heroes'], actual)}
        first = audit(exact[0])
        result['same_army_audit'] = {'actual_battle_id': str(battle_id), **info, 'first': first,
                                     'last': audit(exact[-1]) if len(exact) > 1 else first}
    return result
