"""Read-only cross-checks of archived rosters and live lineup predictions."""
import argparse
import json
import logging
import sqlite3
import threading
import time
from collections import Counter, defaultdict
from pathlib import Path
from types import SimpleNamespace

from archive import Archive
from combat_loadout import side_heroes
from lineup_prediction import (empty_skill_basis, meets_main_threshold, player_history,
                               predict, spartan_build_basis)
from observation_codec import digest,map_troop_codes


def army_rows(db, profile, army_ids=None):
    restriction = ''
    args = [profile]
    if army_ids is not None:
        if not army_ids:
            return []
        restriction = ' AND a.army_id IN (' + ','.join('?' for _ in army_ids) + ')'
        args.extend(army_ids)
    return [dict(r) for r in db.execute('''SELECT a.army_id,s.owner_uid,s.hero_type,s.state_code,
        s.begin_at,s.end_at,s.from_x,s.from_y,s.to_x,s.to_y,s.data,p.name,p.union_id
        FROM army_current a JOIN army_states s ON s.id=a.state_ref
        JOIN players p ON p.profile_id=a.profile_id AND p.player_key='world:'||s.owner_uid
        WHERE a.profile_id=?''' + restriction + ' ORDER BY p.union_id,p.name,a.army_id', args)]


def loadout_signature(heroes):
    return [{key:h.get(key) for key in ('position','id','level','red_stars','troop_code','skill_slot_ids','skills')}
            for h in sorted(heroes, key=lambda h:h['position'])]


def check_army(db, profile, row, history, at, *, archive_type=Archive, predictor=predict):
    codes = map_troop_codes(row['hero_type'])
    march = {'id':row['army_id'], 'owner':row['owner_uid'], 'troops':[{'code':c} for c in codes],
             'state_code':row['state_code'], 'begin':row['begin_at'], 'end':row['end_at'],
             'from':[row['from_x'],row['from_y']], 'target':[row['to_x'],row['to_y']]}
    records, match, evidence = archive_type._army_lineups(SimpleNamespace(db=db,profile_id=profile),
        row['army_id'], history=history, known_at=at)
    prediction = predictor(march, history, at)
    reference = bool(evidence.get('current_resolution',{}).get('anchor_battle_id'))
    predicted_reference = bool(prediction.get('current_resolution',{}).get('anchor_battle_id'))
    archive_id = evidence.get('current_report_id')
    prediction_id = prediction.get('current_report_id')
    issues = []
    missing_fields = []
    if codes and reference != predicted_reference:
        issues.append('reference_path_mismatch')
    elif codes and reference and archive_id != prediction_id:
        issues.append('current_report_mismatch')
    unresolved = bool(evidence.get('current_resolution',{}).get('unresolved'))
    predicted_unresolved = bool(prediction.get('current_resolution',{}).get('unresolved'))
    if codes and reference and predicted_reference and archive_id == prediction_id:
        if unresolved != predicted_unresolved:
            issues.append('roster_completeness_mismatch')
        if not unresolved and not predicted_unresolved:
            sides = db.execute('''SELECT side,heroes,features FROM combat_sides INDEXED BY sqlite_autoindex_combat_sides_1 WHERE profile_id=?
                AND battle_id=? AND player_key=? AND union_id=?''',
                (profile,archive_id,records[0]['player_key'],records[0]['union_id'])).fetchall() if records else []
            displayed = side_heroes(json.loads(sides[0]['heroes']),sides[0]['side'],json.loads(sides[0]['features'])) if len(sides)==1 else None
            lead = next((r for r in prediction['items'] if r['battle_id']==prediction_id), None)
            if not records or displayed is None or not lead:
                issues.append('known_roster_not_displayed')
            elif loadout_signature(displayed) != loadout_signature(lead['heroes']):
                issues.append('displayed_loadout_mismatch')
            if lead:
                heroes = lead['heroes']
                if any(h.get('red_stars') is None for h in heroes):
                    missing_fields.append('red_stars')
                if any(h.get('level') is None for h in heroes):
                    missing_fields.append('hero_levels')
                if any(not h.get('skill_slot_ids') or None in h['skill_slot_ids'] for h in heroes):
                    missing_fields.append('skill_slots')
                expected = ('spartan' if spartan_build_basis(heroes) or empty_skill_basis(heroes) else
                            'main' if meets_main_threshold(heroes) else None)
                if expected and tuple(h.get('troop_code') for h in heroes)==codes and prediction['force_assessment']['kind'] != expected:
                    issues.append('force_label_mismatch')
    if not codes:
        category = 'no_live_troops'
    elif unresolved:
        category = 'incomplete_current_report'
    elif reference and records:
        category = 'source_fields_missing' if missing_fields else 'resolved'
    elif not history[0]:
        category = 'no_player_reports'
    elif not records:
        category = 'no_compatible_roster'
    else:
        category = 'unverified_player_rosters'
    return {'army_id':row['army_id'], 'player':row['name'], 'union_id':row['union_id'], 'checked_at':at,
            'category':category, 'issues':issues, 'archive_report_id':archive_id,
            'missing_fields':missing_fields,
            'prediction_report_id':prediction_id, 'match':match,
            'archive_resolution':evidence.get('current_resolution'),
            'prediction_resolution':prediction.get('current_resolution')}


def audit_group(db, profile, rows, at, *, archive_type=Archive, predictor=predict):
    history = player_history(db, profile, rows[0]['name'], rows[0]['union_id'])
    return [check_army(db, profile, row, history, at,archive_type=archive_type,predictor=predictor) for row in rows]


def summarize(profile, items, started, **extra):
    categories = Counter(item['category'] for item in items)
    problems = [item for item in items if item['issues']]
    return {'profile_id':profile, 'started_at':started, 'checked_at':time.time(),
            'checked_armies':len(items), 'problem_armies':len(problems),
            'categories':dict(categories), 'issues':problems,
            'source_gaps':[i for i in items if i.get('missing_fields')], **extra}


def save_report(path, data):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
    for attempt in range(4):
        try:
            temporary.replace(path)
            return
        except PermissionError:
            if attempt == 3:
                raise
            # A Windows reader can briefly hold the destination without delete sharing.
            time.sleep(.025)


class LineupAudit:
    """One player per read transaction; no game requests or production DB writes."""
    def __init__(self, archive, *, interval=300, group_pause=.05):
        self.archive = archive
        self.path = archive.root/'runtime'/'lineup-consistency.json'
        self.path.parent.mkdir(exist_ok=True)
        self.interval,self.group_pause = interval,group_pause
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self.run,daemon=True,name='lineup-consistency-audit')

    def start(self):
        self.thread.start()

    def close(self):
        self.stop_event.set()
        self.thread.join(timeout=10)

    def run(self):
        cache = {}
        cached_profile = None
        while not self.stop_event.is_set():
            db = None
            delay = self.interval
            try:
                db = sqlite3.connect(self.archive.database_path.resolve().as_uri()+'?mode=ro',uri=True,timeout=2)
                db.row_factory = sqlite3.Row
                profile = self.archive.profile_id
                if cached_profile != profile:
                    cache.clear()
                    cached_profile = profile
                cache = self.check_round(db,profile,cache)
            except Exception as error:
                logging.exception('Lineup consistency audit will retry')
                delay = 30
                try:
                    save_report(self.path,{'profile_id':self.archive.profile_id,'running':False,
                        'checked_at':time.time(),'last_error':type(error).__name__,'retry_after_seconds':delay})
                except OSError:
                    logging.exception('Unable to save lineup audit failure')
            finally:
                if db is not None:
                    db.close()
            if self.stop_event.wait(delay):
                break

    def check_round(self, db, profile, cache):
        groups = defaultdict(list)
        for row in army_rows(db,profile):
            groups[row['name'],row['union_id']].append(row)
        stamps = {(r[0],r[1]):(r[2],r[3]) for r in db.execute('''SELECT player_name,union_id,
            MAX(observed_at),COUNT(*) FROM combat_sides WHERE profile_id=? GROUP BY union_id,player_name''',(profile,))}
        cache = {key:value for key,value in cache.items() if key in groups}
        started,refreshed = time.time(),0
        for key,rows in groups.items():
            if self.stop_event.is_set() or profile != self.archive.profile_id:
                break
            signature = digest([stamps.get(key),int(started//3600),[(r['army_id'],r['data']) for r in rows]])
            previous = cache.get(key)
            if previous and previous[0]==signature and not any(i['category']=='audit_error' for i in previous[1]):
                continue
            try:
                db.execute('BEGIN')
                # Collection continues during a long sweep. Refresh this group's rows
                # inside the same read transaction used by both lineup paths.
                rows = [r for r in army_rows(db,profile,[r['army_id'] for r in rows])
                        if (r['name'],r['union_id']) == key]
                if not rows:
                    cache.pop(key,None)
                    continue
                history = player_history(db,profile,rows[0]['name'],rows[0]['union_id'])
                checked = []
                for row in rows:
                    if self.stop_event.is_set():
                        break
                    checked.append(check_army(db,profile,row,history,time.time()))
                    if self.stop_event.wait(.01):
                        break
            except Exception as error:
                logging.exception('Unable to audit lineup group')
                checked = [{'army_id':r['army_id'],'player':r['name'],'union_id':r['union_id'],
                            'category':'audit_error','issues':['audit_error'],'error':type(error).__name__}
                           for r in rows]
            finally:
                db.rollback()
            cache[key] = (signature,checked)
            refreshed += 1
            items = [item for _,values in cache.values() for item in values]
            save_report(self.path,summarize(profile,items,started,running=True,
                refreshed_players=refreshed,total_armies=sum(map(len,groups.values()))))
            if self.stop_event.wait(self.group_pause):
                break
        items = [item for _,values in cache.values() for item in values]
        result = summarize(profile,items,started,running=False,total_armies=sum(map(len,groups.values())))
        save_report(self.path,result)
        issue_key = digest([{k:i.get(k) for k in ('army_id','issues','archive_report_id','prediction_report_id')}
                            for i in result['issues']])
        if result['problem_armies'] and issue_key != getattr(self,'last_issue_key',None):
            logging.warning('Lineup audit found %s inconsistent armies; see %s',result['problem_armies'],self.path)
        self.last_issue_key = issue_key
        return cache


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--database',type=Path,required=True)
    parser.add_argument('--profile',required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--check',action='store_true',help='Fail validation when inconsistent armies are found')
    args = parser.parse_args()
    db = sqlite3.connect(args.database.resolve().as_uri()+'?mode=ro',uri=True,timeout=10)
    db.row_factory = sqlite3.Row
    groups = defaultdict(list)
    for row in army_rows(db,args.profile):
        groups[row['name'],row['union_id']].append(row)
    items, started = [], time.time()
    try:
        db.execute('BEGIN')
        for index,rows in enumerate(groups.values(),1):
            items.extend(audit_group(db,args.profile,rows,started))
            if index%20 == 0:
                print(json.dumps({'players':index,'armies':len(items),'problems':sum(bool(i['issues']) for i in items)}),flush=True)
        report = summarize(args.profile,items,started,running=False,total_players=len(groups))
        save_report(args.output,report)
        print(json.dumps({k:v for k,v in report.items() if k not in ('issues','source_gaps')},ensure_ascii=False),flush=True)
    finally:
        db.rollback()
        db.close()
    if args.check and report['problem_armies']:
        raise SystemExit(1)


if __name__=='__main__':
    main()
