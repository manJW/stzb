"""Read-only walk-forward check of pre-arrival lineup predictions."""
import collections
import json
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from lineup_prediction import meets_main_threshold, player_history, predict
from observation_codec import describe_troops, digest, map_troop_codes


def replay(db, profile, enemies, *, include_pve=True, force=False):
    rows = db.execute('''SELECT c.player_name,c.union_id,c.hero_key,c.army_id AS direct_army,
        c.observed_at AS report_seen,c.heroes,c.initial_hp,c.npc,c.event_at,l.method,m.* FROM combat_sides c
        JOIN battle_march_links l ON l.profile_id=c.profile_id AND l.battle_id=c.battle_id AND l.side=c.side
        JOIN march_samples m ON m.profile_id=l.profile_id AND m.id=l.march_id
        WHERE c.profile_id=? AND c.side='attack' AND c.npc<>1 AND c.hero_key<>''
        AND m.observed_at<m.end_at AND c.event_at>=m.observed_at AND c.observed_at>m.observed_at
        AND (l.method='unique_attack_arrival' OR (l.method='coordinate_time_match' AND c.army_id=m.army_id))
        ORDER BY m.observed_at,c.event_at,c.initial_hp DESC''', (profile,))
    unique = collections.defaultdict(list)
    for row in rows:
        if row['union_id'] not in enemies or (force and (row['npc'] != 0 or row['state_code'] not in (1,2))):
            continue
        key = tuple(row[k] for k in ('army_id','begin_at','from_x','from_y','to_x','to_y'))
        unique[key].append(row)

    histories, results = {}, []
    for group in unique.values():
        row = group[0]
        name_union = row['player_name'], row['union_id']
        if name_union not in histories:
            histories[name_union] = player_history(db, profile, *name_union, include_pve=include_pve)
        # Use only complete outcomes whose occupied slots agree with the map.
        troops = map_troop_codes(row['hero_type'])
        occupied = {i for i, code in enumerate(troops, 1) if code}
        if not occupied:
            continue
        complete = [r for r in group if {h['position'] for h in json.loads(r['heroes'])} == occupied]
        if not complete or len({r['hero_key'] for r in complete}) != 1:
            continue
        march = {'id':row['army_id'], 'owner':None, 'troops':describe_troops(troops), 'player_name':row['player_name'],
                 'union_id':row['union_id'],
                 'state_code':row['state_code'], 'begin':row['begin_at'], 'end':row['end_at'],
                 'from':[row['from_x'],row['from_y']], 'target':[row['to_x'],row['to_y']]}
        owner = db.execute('''SELECT owner_uid FROM army_states WHERE profile_id=? AND army_id=?
            AND state_code=? AND begin_at=? AND end_at=? AND observed_at<=? ORDER BY observed_at DESC LIMIT 1''',
            (profile, row['army_id'], row['state_code'], row['begin_at'], row['end_at'], row['observed_at'])).fetchone()
        if owner:
            march['owner'] = owner[0]
        if force:
            truth = min(complete, key=lambda r:(r['event_at'], -(r['initial_hp'] or 0)))
            actual = meets_main_threshold(json.loads(truth['heroes']))
            def assess():
                result = predict(march, histories[name_union], row['observed_at'])
                return result['force_assessment']['kind'] == 'main'
            new = assess()
            with patch('lineup_prediction.main_pattern_assessment', return_value=None):
                old = assess()
            results.append((row['observed_at'], actual, old, new))
            continue
        row = complete[0]
        ranks = {}
        for model, use_identity, use_speed in (('troops',False,False),('identity',True,False),('full',True,True)):
            output = predict(march, histories[name_union], row['observed_at'], use_identity=use_identity, use_speed=use_speed)
            keys = [item['hero_key'] for item in output['items']]
            ranks[model] = keys.index(row['hero_key'])+1 if row['hero_key'] in keys else 0
            ranks[model+'_covered'] = bool(keys)
            if model == 'full':
                ranks['speed_available'] = any(item['speed'] for item in output['items'])
        results.append((row['observed_at'], any(r['direct_army'] for r in group), ranks))
    return sorted(results, key=lambda entry:entry[0])


def summarize(rows):
    result = {'total':len(rows), 'direct_outcomes':sum(direct for _,direct,_ in rows),
              'speed_available':sum(r['speed_available'] for _,_,r in rows)}
    for model in ('troops','identity','full'):
        result[model] = {'covered':sum(r[model+'_covered'] for _,_,r in rows),
                         'top1':sum(r[model] == 1 for _,_,r in rows),
                         'top3':sum(0 < r[model] <= 3 for _,_,r in rows),
                         'any':sum(r[model] > 0 for _,_,r in rows)}
    return result


def summarize_force(rows):
    return {'marches':len(rows), 'actual_main':sum(r[1] for r in rows),
            **{label:{'detected':sum(r[1] and r[column] for r in rows),
                      'false_main':sum(not r[1] and r[column] for r in rows)}
               for column,label in ((2,'previous'),(3,'current'))}}


if __name__ == '__main__':
    archive = json.loads((ROOT/'archive-profile.json').read_text(encoding='utf-8-sig'))
    config = json.loads((ROOT/'config.json').read_text(encoding='utf-8-sig'))
    profile = digest([archive[k] for k in ('server','season','account_role_id')])[:24]
    enemies = {u for u,r in config['relations'].items() if r['relation']=='enemy'}
    with sqlite3.connect((ROOT/'data'/'intelligence.sqlite3').as_uri()+'?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        db.execute('BEGIN')
        if '--force' in sys.argv:
            data = replay(db, profile, enemies, force=True)
            split = int(len(data) * .7)
            print(json.dumps({'earlier':summarize_force(data[:split]),'later':summarize_force(data[split:]),
                              'all':summarize_force(data)}, indent=2))
            sys.exit(0)
        result = {}
        for label, include_pve in (('previous', False), ('current', True)):
            data = replay(db, profile, enemies, include_pve=include_pve)
            split = len(data)//2
            result[label] = {'earlier':summarize(data[:split]),'later':summarize(data[split:]),
                             'direct_only':summarize([r for r in data if r[1]])}
        print(json.dumps(result, indent=2))
