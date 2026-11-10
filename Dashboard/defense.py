"""Recommend observed alliance builds against each incoming formation hypothesis."""
from collections import defaultdict
from math import ceil, dist

from combat_loadout import forecast_heroes
from defense_model import context, estimate, observations, profile as combat_profile, validate
from intelligence import wilson
from lineup_prediction import LOADOUT_FIELDS, meets_main_threshold, predict_alerts
from observation_codec import map_troop_codes, report_troop_codes


MIN_PROBABILITY_CASES = 12
MIN_REPLAY_CASES = 20
REENGAGEMENT_SECONDS = 600
COUNTER_SKILLS = {'cowardice': 200228, 'hesitation': 200220}
DISPATCH_BUFFER = 20
SPEED_HISTORY_AGE = 7 * 86400


def tactics_key(heroes):
    if len(heroes) != 3 or any(len(h.get('skills') or ()) != 3 for h in heroes):
        return None
    return tuple((h['id'], tuple((s['id'], s['level']) for s in h['skills'])) for h in heroes)


def build_key(heroes, tactics):
    if tactics is None or any(h.get('red_stars') is None or not h.get('gear_reported') or not h.get('troop_code')
                             for h in heroes):
        return None
    return (tactics, tuple((h['level'], h['red_stars'], h['troop_code'], tuple(h['gear'][k] for k in ('id', 'level', 'forge_id'))
                           if h['gear'] else ()) for h in heroes))


def matchup_keys(own, enemy):
    own_tactics, enemy_tactics = tactics_key(own), tactics_key(enemy)
    own_build, enemy_build = build_key(own, own_tactics), build_key(enemy, enemy_tactics)
    return {'formation': (tuple(h['id'] for h in own), tuple(h['id'] for h in enemy)),
            'tactics': (own_tactics, enemy_tactics) if own_tactics and enemy_tactics else None,
            'build': (own_build, enemy_build) if own_build and enemy_build else None}


def summarize(cases):
    total = len(cases)
    wins = sum(row['outcome'] == 'win' for row in cases)
    draws = sum(row['outcome'] == 'draw' for row in cases)
    return {'samples': total, 'wins': wins, 'draws': draws, 'losses': total - wins - draws,
            'historical_win_rate': wins / total if total else None, 'interval_95': wilson(wins, total),
            'battle_ids': [row['battle_id'] for row in cases[-6:]][::-1]}


def pooled_probability(wins, count, prior):
    return (wins + 4 * prior) / (count + 4)


def replay(cases):
    """Chronological replay of identical builds; same-second results are held out together."""
    training_end = cases[int(len(cases) * .7)]['event_at'] if cases else 0
    groups, wins, count = defaultdict(lambda: [0, 0]), 0, 0
    model_error, baseline_error, evaluated = 0., 0., 0
    known = iter(sorted(cases, key=lambda row: row['available_at']))
    past = next(known, None)
    for row in cases:
        at = row['event_at']
        while past and past['available_at'] < at:
            won = int(past['outcome'] == 'win')
            group = groups[past['keys']['build']]
            group[0] += won
            group[1] += 1
            wins += won
            count += 1
            past = next(known, None)
        prior = (wins + 1) / (count + 2)
        key, won = row['keys']['build'], int(row['outcome'] == 'win')
        group_wins, group_count = groups[key]
        if key and at >= training_end and group_count >= MIN_PROBABILITY_CASES:
            model_error += (pooled_probability(group_wins, group_count, prior) - won) ** 2
            baseline_error += (prior - won) ** 2
            evaluated += 1
    return {'samples': evaluated, 'model_brier': model_error / evaluated if evaluated else None,
            'baseline_brier': baseline_error / evaluated if evaluated else None,
            'validated': evaluated >= MIN_REPLAY_CASES and model_error < baseline_error}


def rule_evidence(own, enemy, catalog):
    own_ids = {s['id'] for h in own for s in h.get('skills') or ()}
    enemy_skills = [s for h in enemy for s in h.get('skills') or ()]
    enemy_ids = {s['id'] for s in enemy_skills}
    chase = {s['name'] for s in enemy_skills if catalog.get(str(s['id']), {}).get('type') == '追击'}
    active = {s['name'] for s in enemy_skills if catalog.get(str(s['id']), {}).get('type') == '主动'}
    result = []
    normal_attack = {s['name'] for s in enemy_skills if s['id'] in (200027, 200223, 200224, 200233, 200819)}
    if COUNTER_SKILLS['cowardice'] in own_ids and (chase or normal_attack):
        result.append({'key': 'cowardice', 'text': '战必断金可限制普攻，压低' +
                       ('、'.join(sorted(chase)) + '等追击战法的触发机会' if chase else
                        '敌方连击的收益（' + '、'.join(sorted(normal_attack)) + '）') +
                       ('。敌方疾战可免疫怯战，需看发动时序' if 200819 in enemy_ids else '') +
                       ('。敌方始计在满足触发条件后可提供洞察' if 200687 in enemy_ids else '')})
    if COUNTER_SKILLS['hesitation'] in own_ids and len(active) >= 3:
        result.append({'key': 'hesitation', 'text': '反计之策可压制开局主动战法。敌方有' + '、'.join(sorted(active))})
    return result


def historical_cases(db, profile, union, now):
    sides = defaultdict(dict)
    for row in db.execute(f'''SELECT c.side,c.battle_id,c.player_key,c.event_at,c.observed_at,c.wid,c.outcome,c.heroes,{LOADOUT_FIELDS}
        FROM combat_sides c JOIN combat_sides o ON o.profile_id=c.profile_id AND o.battle_id=c.battle_id
          AND o.side<>c.side WHERE c.profile_id=? AND c.event_at>0 AND c.event_at<? AND c.observed_at<=?
        AND c.npc=0 AND o.npc=0
        AND ((c.side='defend' AND c.union_id=? AND o.union_id NOT IN (?,'0',''))
          OR (c.side='attack' AND o.union_id=? AND c.union_id NOT IN (?,'0','')))
        ORDER BY c.event_at,c.battle_id''', (profile, now, now, union, union, union, union)):
        sides[row['battle_id']][row['side']] = dict(row)
    result, recent = [], {}
    for bid, pair in sides.items():
        if len(pair) != 2:
            continue
        own, enemy = pair['defend'], pair['attack']
        a, b = forecast_heroes(own), forecast_heroes(enemy)
        if own['outcome'] not in ('win', 'loss', 'draw') or not (meets_main_threshold(a) and meets_main_threshold(b)):
            continue
        keys = matchup_keys(a, b)
        event = (own['player_key'], enemy['player_key'], own['wid'], keys['tactics'] or keys['formation'])
        at = own['event_at']
        if at - recent.get(event, -REENGAGEMENT_SECONDS) <= REENGAGEMENT_SECONDS:
            continue
        recent[event] = at
        result.append({'event_at': at, 'available_at': max(at, own['observed_at'], enemy['observed_at']),
                       'battle_id': bid, 'outcome': own['outcome'], 'keys': keys,
                       'model_context': context(combat_profile(a), combat_profile(b))})
    return result


def alliance_builds(db, profile, union, now, army_ids=None):
    seen_armies, seen_formations = set(), set()
    query = f'''SELECT c.side,c.battle_id,c.player_key,c.player_name,c.army_id,c.hero_key,c.event_at,
        c.heroes,c.initial_hp,{LOADOUT_FIELDS}
        FROM combat_sides c WHERE c.profile_id=? AND c.union_id=?
        AND c.event_at>0 AND c.event_at<? AND c.observed_at<=?'''
    params = (profile, union, now, now)
    if army_ids is not None:
        if not army_ids:
            return
        query += ' AND c.army_id IN (' + ','.join('?' for _ in army_ids) + ')'
        params += tuple(army_ids)
    for raw in db.execute(query + ' ORDER BY c.event_at DESC,c.observed_at DESC,c.battle_id DESC', params):
        row = dict(raw)
        army = (row['player_key'], row['army_id']) if row['army_id'] else None
        formation = (row['player_key'], row['hero_key'])
        if (army and army in seen_armies) or (army_ids is None and formation in seen_formations):
            continue
        if army:
            seen_armies.add(army)
        seen_formations.add(formation)
        row['heroes'] = forecast_heroes(row)
        if meets_main_threshold(row['heroes']):
            row['troop_codes'] = report_troop_codes(row['troop_raw'], row['side'])
            yield row


def availability(army, troop_codes, target, deadline):
    if not army:
        return 'unknown'
    observed = tuple(t['code'] for t in army.get('troops', ()))
    if troop_codes and observed and troop_codes != observed:
        return 'changed'
    if army['kind'] == 'garrison' and army['position'] == target:
        return 'holding'
    if army['kind'] == 'garrison_move' and army['target'] == target and army['end']:
        return 'arriving' if army['end'] < deadline else 'late'
    return 'observed'


def recommendations(cases, builds, enemies, catalog, visible, target, deadline, arrival_plans=None):
    by_key = defaultdict(list)
    for case in cases:
        for scope, key in case['keys'].items():
            if key:
                by_key[scope, key].append(case)
    evaluation = replay(cases)
    records = observations(cases)
    similar_evaluation = validate(records)
    troop_evaluation = validate(records, use_troops=True)
    prior_window = validate(records, start=.5, end=.7)
    troop_prior_window = validate(records, use_troops=True, start=.5, end=.7)
    use_troops = (troop_evaluation['validated'] and troop_prior_window['validated']
                  and troop_evaluation['model_brier'] < similar_evaluation['model_brier']
                  and troop_prior_window['model_brier'] < prior_window['model_brier'])
    if use_troops:
        similar_evaluation = troop_evaluation
    tactical_evaluation = validate(records, True, use_troops=use_troops)
    use_skills = tactical_evaluation['validated'] and tactical_evaluation['model_brier'] < similar_evaluation['model_brier']
    if use_skills:
        similar_evaluation = tactical_evaluation
    model_records = [(matchup, won) for _, _, matchup, won in records]
    prior = (sum(case['outcome'] == 'win' for case in cases) + 1) / (len(cases) + 2)
    hypotheses = []
    for enemy in enemies:
        options = []
        enemy_profile = combat_profile(enemy['heroes'])
        for own in builds:
            keys = matchup_keys(own['heroes'], enemy['heroes'])
            samples = {scope: summarize(by_key.get((scope, key), ())) for scope, key in keys.items()}
            rules = rule_evidence(own['heroes'], enemy['heroes'], catalog)
            risks = rule_evidence(enemy['heroes'], own['heroes'], catalog)
            army = visible.get((own['army_id'], own['player_name']))
            arrival = (arrival_plans or {}).get(own['army_id'])
            status = arrival['availability'] if arrival else availability(army, own['troop_codes'], target, deadline)
            timely = status in ('holding', 'arriving', 'estimated')
            complete = samples['build']
            probability = model = None
            if evaluation['validated'] and complete['samples'] >= MIN_PROBABILITY_CASES:
                probability = pooled_probability(complete['wins'], complete['samples'], prior)
                model = {'method': 'same_build', 'lineup_cases': complete['samples']}
            elif similar_evaluation['validated'] and meets_main_threshold(enemy['heroes']):
                model = estimate(model_records, context(combat_profile(own['heroes']), enemy_profile), prior,
                                 use_skills=use_skills, use_troops=use_troops)
                if model:
                    probability = model.pop('probability')
                    model['method'] = 'similar_main'
            if probability is None and not rules and not samples['formation']['samples'] and not timely:
                continue
            matched = next((samples[scope] for scope in ('build', 'tactics', 'formation') if samples[scope]['samples'] >= 5), None)
            point = army.get('position') if army else None
            distance = round(dist(point, target), 1) if point and target else None
            item = {k: own[k] for k in ('player_name', 'army_id', 'battle_id', 'event_at', 'initial_hp', 'heroes')}
            item.update(history=samples, rules=rules, risks=risks, position=point, distance=distance,
                        availability=status, arrival_at=arrival['arrival_at'] if arrival else
                            army['end'] if status in ('arriving', 'late') else None,
                        predicted_win_probability=probability, probability_evidence=model)
            score = (timely, status not in ('late', 'changed'), probability is not None,
                     round(probability * 20) if probability is not None else 0, bool(matched),
                     matched['interval_95'][0] if matched else 0, len(rules)-len(risks),
                     point is not None, -(distance or 0), probability or 0, own['event_at'])
            options.append((score, item))
        options.sort(key=lambda pair: pair[0], reverse=True)
        hypotheses.append({'battle_id': enemy['battle_id'], 'heroes': enemy['heroes'], 'identity': enemy.get('identity'),
                           'items': [i for _, i in options[:5]]})
    # Keep the lead conditional on an explicit enemy hypothesis instead of averaging guessed odds.
    choice = next(((index, h['items'][0]) for index, h in enumerate(hypotheses) if h['items']
                   and h['items'][0]['availability'] not in ('late', 'changed')), None)
    recommendation = {'hypothesis_index': choice[0], 'item': choice[1]} if choice else None
    return {'historical_cases': len(cases), 'validation': evaluation, 'similar_validation': similar_evaluation,
            'troop_validation': {**troop_evaluation, 'earlier_window': troop_prior_window, 'selected':use_troops},
            'skill_validation': {**tactical_evaluation, 'selected':use_skills},
            'hypotheses': hypotheses, 'recommendation': recommendation}


def analyze_alert(archive, snapshot, army_id):
    for alert in snapshot['alerts']:
        if alert['army_id'] == army_id and alert.get('map_hero_count'):
            return {'active': True, 'arrival_at': alert['end'], 'low_force': True,
                    'map_hero_count': alert['map_hero_count'], 'hypotheses': []}
    with archive.lock:
        prediction = predict_alerts(archive, snapshot, army_id=army_id).get(army_id)
        if not prediction:
            return {'active': False}
        if not prediction['items']:
            return {'active': True, 'hypotheses': []}
        union = str(archive.profile['union_id'])
        now = snapshot['server_now']
        cases = historical_cases(archive.db, archive.profile_id, union, now)
        builds = list(alliance_builds(archive.db, archive.profile_id, union, now))
        catalog = archive.skills
    visible = {(r['id'], r['player_name']): r for r in snapshot['armies']
               if r['union_id'] == union and not r['stale'] and r['kind'] in ('stay', 'garrison', 'garrison_move')}
    result = recommendations(cases, builds, prediction['items'][:3], catalog, visible, prediction['target'], prediction['end'])
    low_force = (prediction.get('force') or {}).get('priority') == 'low'
    if low_force:
        result['recommendation'] = None
    return {'active': True, 'arrival_at': prediction['end'], 'low_force': low_force, **result}


def own_speed_history(db, profile, army, state_rules, now):
    rows = db.execute('''SELECT DISTINCT m.id,m.straight_distance,m.straight_grids_per_hour,m.hero_type,
        m.state_code,s.state_id,json_extract(s.data,'$[9]') AS target_type FROM march_samples m
        JOIN army_states s ON s.profile_id=m.profile_id AND s.army_id=m.army_id AND s.message_ref=m.message_ref
            AND s.begin_at=m.begin_at AND s.end_at=m.end_at AND s.state_code=m.state_code
            AND s.from_x=m.from_x AND s.from_y=m.from_y AND s.to_x=m.to_x AND s.to_y=m.to_y
        WHERE m.profile_id=? AND m.army_id=? AND s.owner_uid=? AND m.begin_at>=? AND m.end_at<=?
            AND m.observed_at<m.end_at AND m.straight_grids_per_hour>0''',
        (profile, army['id'], army['owner'], now-SPEED_HISTORY_AGE, now))
    codes = tuple(t['code'] for t in army['troops'])
    history = defaultdict(list)
    for row in rows:
        prefix = f"{row['state_code']}:{row['target_type']}:"
        rule = state_rules.get(prefix+str(row['state_id']), state_rules.get(prefix+'*', {}))
        if (rule.get('verified') and not rule.get('require_same_coordinate')
                and rule.get('kind') in ('garrison_move', 'attack') and map_troop_codes(row['hero_type']) == codes):
            history[rule['kind']].append((row['straight_distance'], row['straight_grids_per_hour']))
    return history


def garrison_arrival(army, target, deadline, now, history):
    kind = army['kind']
    if kind == 'garrison' and army['position'] == target:
        # A verified stationary guard can have begin == end == the order time.
        # The current state, not that timestamp, establishes that it is holding.
        begin = army.get('begin') or 0
        return {'availability':'holding', 'arrival_at':None, 'source':'live_holding',
                'guard_since':begin if begin == army['end'] and 0 < begin <= now else None}
    if kind == 'garrison_move' and army['target'] == target and now < army['end'] < deadline:
        return {'availability':'arriving', 'arrival_at':army['end'], 'source':'live_march'}
    if kind != 'stay' or army['relation'] != 'self' or not army['position']:
        return None
    distance = dist(army['position'], target)
    if distance == 0:
        return None
    for method in ('garrison_move', 'attack'):
        comparable = [speed for length, speed in history.get(method, ()) if .5 <= length/distance <= 2]
        if len(comparable) < 2:
            continue
        margin = 1. if method == 'garrison_move' else 1.2
        duration = ceil(distance * 3600 / min(comparable) * margin) + DISPATCH_BUFFER
        if now + duration < deadline:
            return {'availability':'estimated', 'arrival_at':now+duration, 'duration_seconds':duration,
                    'source':'history_'+method, 'speed_samples':len(comparable), 'buffer_seconds':DISPATCH_BUFFER}
        return None
    return None


def garrison_plans(archive, snapshot, predictions):
    now = snapshot['server_now']
    alerts = sorted((a for a in snapshot['alerts'] if a['end'] > now and
                    predictions.get(a['army_id'], {}).get('force_assessment', {}).get('kind') == 'main'),
                    key=lambda a: a['end'])
    if not alerts:
        return {}
    visible = {a['id']: a for a in snapshot['armies'] if a['relation'] in ('self', 'ally') and not a['stale']
               and a['kind'] in ('stay', 'garrison', 'garrison_move')
               and len(a['troops']) == 3 and all(t['code'] > 0 for t in a['troops'])}
    plans = {a['army_id']:{'status':'no_timely_team'} for a in alerts}
    if not visible:
        return plans
    with archive.lock:
        db, profile = archive.db, archive.profile_id
        union = str(archive.profile['union_id'])
        rules = snapshot['config']['state_rules']
        history = {aid:own_speed_history(db, profile, army, rules, now) for aid, army in visible.items()
                   if army['relation'] == 'self' and army['kind'] == 'stay'}
        arrivals = {a['army_id']:{aid:arrival for aid, army in visible.items()
                    if (arrival := garrison_arrival(army, a['coord'], a['end'], now, history.get(aid, {})))} for a in alerts}
        ready_ids = {aid for options in arrivals.values() for aid in options}
        if not ready_ids:
            return plans
        builds = {b['army_id']:b for b in alliance_builds(db, profile, union, now, ready_ids)
                  if b['player_name'] == visible[b['army_id']]['player_name']
                  and b['troop_codes'] == tuple(t['code'] for t in visible[b['army_id']]['troops'])}
        cases = historical_cases(db, profile, union, now) if builds else []
        catalog = archive.skills
    reserved = {}
    for alert in alerts:
        enemy_id, target = alert['army_id'], alert['coord']
        eligible = {aid:arrival for aid, arrival in arrivals[enemy_id].items()
                    if (arrival['availability'] != 'estimated' or aid in builds) and reserved.get(aid, target) == target}
        if not eligible:
            continue
        enemy = next(iter(predictions[enemy_id]['items']), None)
        configured = [builds[aid] for aid in eligible if aid in builds]
        item = None
        if configured and enemy:
            result = recommendations(cases, configured, [enemy], catalog, {}, target, alert['end'], eligible)
            if result['recommendation']:
                item = result['recommendation']['item']
        aid = item['army_id'] if item else min(eligible, key=lambda key:eligible[key]['arrival_at'] or now)
        army = visible[aid]
        arrival = eligible[aid]
        reserved[aid] = target
        plans[enemy_id] = {'status':'ready', **arrival, 'issued_at':now, 'player_name':army['player_name'],
                           'relation':army['relation'], 'army_id':aid, 'enemy_battle_id':enemy['battle_id'] if enemy else None,
                           'enemy_identity':enemy.get('identity') if enemy else None, 'matchup':item,
                           'defender':{key:army[key] for key in ('id','kind','from','target','begin','end','troops')}}
    return plans
