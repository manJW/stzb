"""Read-only season archive queries for routes, lineups and battle evidence."""
import json
import math
import re
import threading
from collections import OrderedDict, defaultdict

from combat_loadout import side_heroes
from lineup_current import card_identity
from observation_codec import coord, digest, report_system_npc


_binding_cache = OrderedDict()
_binding_lock = threading.RLock()


def report_bindings(archive, name, union, identity='', end=0):
    """Reconcile archive reports against actual army anchors, never hero names.

    Cache by the same committed player revision used by live lineup resolution.
    A card-set with multiple army anchors remains unbound. This is a read model;
    it never changes the original report or makes a guessed army permanent.
    """
    revision = archive.db.execute('''SELECT revision FROM lineup_player_revisions
        WHERE profile_id=? AND player_name=? AND union_id=?''',
        (archive.profile_id, name, union)).fetchone()
    database = archive.db.execute('PRAGMA database_list').fetchone()[2]
    key = (database, archive.profile_id, name, union, identity, end, revision[0] if revision else 0)
    with _binding_lock:
        if key in _binding_cache:
            _binding_cache.move_to_end(key)
            return _binding_cache[key]
    params = (archive.profile_id, name, union, identity, identity, end, end)
    scope = '''c.profile_id=? AND c.player_name=? AND COALESCE(c.union_id,'')=?
        AND (?='' OR c.player_key=?) AND (?=0 OR c.event_at<=?)'''
    entries, identities = {}, {}
    for raw in archive.db.execute('SELECT c.* FROM combat_sides c WHERE '+scope, params):
        row = dict(raw)
        features = json.loads(row['features'])
        skipped = report_system_npc({**features, 'npc': row['npc']}, row['side'])
        heroes = side_heroes(json.loads(row['heroes']), row['side'], features)
        report_key = (row['battle_id'], row['side'])
        entries[report_key] = dict(battle_id=row['battle_id'], side=row['side'], event_at=row['event_at'],
            player_key=row['player_key'], army_id=(row['army_id'] or '') if row['binding']=='exact_army_id' and not skipped else '',
            binding='direct' if row['binding']=='exact_army_id' and row['army_id'] and not skipped else '', skip=skipped,
            display_heroes=[{k: h.get(k) for k in ('id','name','position','level','red_stars')} for h in heroes])
        identities[report_key] = card_identity(dict(heroes=heroes, idu_raw=features.get(row['side']+'_idu'),
                                                  role_raw=features.get(row['side']+'_role_id')))
    routes = defaultdict(set)
    for row in archive.db.execute('''SELECT c.battle_id,c.side,l.method,m.army_id FROM combat_sides c
        JOIN battle_march_links l ON l.profile_id=c.profile_id AND l.battle_id=c.battle_id AND l.side=c.side
        JOIN march_samples m ON m.profile_id=l.profile_id AND m.id=l.march_id WHERE '''+scope+
        " AND l.method IN ('unique_attack_arrival','coordinate_time_match')", params):
        routes[row['battle_id'],row['side']].add((row['army_id'],row['method']))
    for report_key, links in routes.items():
        entry = entries[report_key]
        armies = {army for army, _ in links}
        if not entry['skip'] and not entry['army_id'] and len(armies)==1 and any(m=='unique_attack_arrival' for _,m in links):
            entry.update(army_id=next(iter(armies)), binding='unique_attack_arrival')
    anchors = defaultdict(list)
    for report_key, entry in entries.items():
        cards = identities[report_key]
        if entry['army_id'] and cards and cards['pairs']:
            anchors[cards['role_id'],cards['card_set']].append((entry['army_id'], cards['pairs'], report_key[0]))
    for report_key, entry in entries.items():
        cards = identities[report_key]
        if entry['army_id'] or entry['skip'] or not cards:
            continue
        observed = {cards['cards'][h['position']-1]:h['id'] for h in entry['display_heroes']
                    if h.get('position') in (1,2,3)}
        matches = {(army, bid) for army,pairs,bid in anchors[cards['role_id'],cards['card_set']]
                   if all(dict(pairs).get(card)==hero for card,hero in observed.items())}
        armies = {army for army, _ in matches}
        if len(armies)==1:
            entry.update(army_id=next(iter(armies)), binding='same_card_instances')
    # Actual reports also identify a team of physical cards even when no map
    # army was observed. Keep that report team distinct from every map army.
    formations = defaultdict(set)
    for report_key,cards in identities.items():
        if cards and cards['pairs'] and not entries[report_key]['skip']:
            formations[cards['role_id'],cards['card_set']].add(cards['pairs'])
    for report_key, entry in entries.items():
        entry['formation_key'] = ''
        cards = identities[report_key]
        if entry['army_id'] or entry['skip'] or not cards:
            continue
        observed = {cards['cards'][h['position']-1]:h['id'] for h in entry['display_heroes']
                    if h.get('position') in (1,2,3)}
        matches = [pairs for pairs in formations[cards['role_id'],cards['card_set']]
                   if all(dict(pairs).get(card)==hero for card,hero in observed.items())]
        if len(matches)==1:
            entry['formation_key'] = 'cards:'+digest([archive.profile_id,entry['player_key'],union,
                                                     cards['role_id'],cards['card_set'],matches[0]])[:24]
    with _binding_lock:
        _binding_cache[key] = entries
        while len(_binding_cache)>64 or sum(len(v) for v in _binding_cache.values())>30_000:
            _binding_cache.popitem(last=False)
    return entries


def army_report_keys(archive, army, end):
    scopes = archive.db.execute('''SELECT player_name,union_id FROM combat_sides WHERE profile_id=? AND army_id=?
        UNION SELECT c.player_name,c.union_id FROM march_samples m JOIN battle_march_links l
        ON l.profile_id=m.profile_id AND l.march_id=m.id JOIN combat_sides c
        ON c.profile_id=l.profile_id AND c.battle_id=l.battle_id AND c.side=l.side
        WHERE m.profile_id=? AND m.army_id=? AND l.method='unique_attack_arrival' ''',
        (archive.profile_id, army, archive.profile_id, army))
    return [key for name,union in scopes for key,value in report_bindings(archive,name,union,end=end).items()
            if value['army_id']==army and not value['skip']]


CONFIRMED_ROUTE = "('coordinate_time_match','unique_attack_arrival')"
MARCH_OWNER = """
LEFT JOIN army_states s ON s.id=(SELECT observed.id FROM army_states observed
    WHERE observed.profile_id=m.profile_id AND observed.army_id=m.army_id
    AND observed.begin_at=m.begin_at AND observed.end_at=m.end_at AND observed.state_code=m.state_code
    AND observed.from_x=m.from_x AND observed.from_y=m.from_y AND observed.to_x=m.to_x AND observed.to_y=m.to_y
    ORDER BY observed.observed_at,observed.id LIMIT 1)
LEFT JOIN players p ON p.profile_id=m.profile_id AND p.player_key='world:'||s.owner_uid
"""
OWNER_NAME = "COALESCE(p.name,'')"
MARCH_FILTERS = """m.profile_id=:profile AND (:start=0 OR m.end_at>=:start) AND (:end=0 OR m.begin_at<=:end)
    AND (:wid=0 OR (m.from_x=:x AND m.from_y=:y) OR (m.to_x=:x AND m.to_y=:y))"""
VERSION_BASIS = '''FROM team_versions v WHERE v.profile_id=:profile AND v.team_key=:version_key
    AND v.player_name=:version_player AND (:version_union IN ('','0') OR v.union_id=:version_union)
    AND (:version_identity='' OR v.player_key=:version_identity)
    AND v.binding='exact_army_id' AND COALESCE(json_extract(v.data,'$.binding_conflict'),0)=0'''
VERSION_SEQUENCE = '''versions AS (SELECT v.*,ROW_NUMBER() OVER (
    PARTITION BY COALESCE(NULLIF(v.battle_id,''),v.id) ORDER BY v.observed_at DESC,v.id DESC) AS newest '''+VERSION_BASIS+'''),
    sequence AS (SELECT event_at,battle_id,data,id,configuration_hash,player_key,union_id,player_name,
        LAG(configuration_hash) OVER (ORDER BY event_at,observed_at,id) AS prior,
        LAG(data) OVER (ORDER BY event_at,observed_at,id) AS prior_data,
        LAG(battle_id) OVER (ORDER BY event_at,observed_at,id) AS prior_battle_id FROM versions WHERE newest=1),
    changes AS (SELECT * FROM sequence WHERE prior IS NULL OR prior<>configuration_hash)'''


def search_parameters(query):
    player = str(query.get('player', '')).strip()
    army = str(query.get('army_id', '')).strip()
    point = str(query.get('coord', '')).strip()
    match = re.fullmatch(r'([0-9]{1,4})\s*[,，、\s]\s*([0-9]{1,4})', point) if point else None
    x, y = map(int, match.groups()) if match else (0, 0)
    if point and not (x and y):
        raise ValueError('坐标请填写两个有效数字，例如 1015,926')
    if army and not re.fullmatch(r'[0-9]{1,20}', army):
        raise ValueError('队号请填写完整数字')
    if len(player) > 60:
        raise ValueError('玩家名称过长')
    try:
        start, end = (float(query.get(key) or 0) for key in ('from_at', 'to_at'))
        limit = min(100, max(1, int(query.get('limit', 25))))
        offset = min(1_000_000, max(0, int(query.get('offset', 0))))
    except (ValueError, TypeError, OverflowError):
        raise ValueError('时间或分页参数无效') from None
    if any(not math.isfinite(v) or v < 0 for v in (start, end)) or start and end and end < start:
        raise ValueError('时间范围无效，请检查起止时间')
    return dict(player=player, army=army, x=x, y=y, wid=x*10000+y, start=start, end=end,
                limit=limit+1, offset=offset)


def search(archive, query):
    args = {**search_parameters(query), 'profile': archive.profile_id}
    owner_join = MARCH_OWNER if args['player'] else ''
    owner_name = OWNER_NAME if args['player'] else "''"
    march_filters = MARCH_FILTERS
    if args['army']:
        march_filters += ' AND m.army_id=:army'
    if args['player']:
        march_filters += f' AND instr(lower({OWNER_NAME}),lower(:player))>0'
    rows = archive.db.execute(f'''WITH marches AS (
        SELECT m.id,m.army_id,m.begin_at,m.end_at,m.from_x,m.from_y,m.to_x,m.to_y,m.state_code,
               {owner_name} AS player_name
        FROM march_samples m {owner_join}
        WHERE {march_filters}
    ), results AS (
        SELECT 'march' AS kind,m.id,'' AS side,m.army_id,m.begin_at AS at,m.end_at,m.from_x,m.from_y,
               m.to_x,m.to_y,m.player_name,m.state_code,'' AS outcome,'' AS place_name,
               0 AS has_full_report,'' AS binding FROM marches m
        UNION ALL
        SELECT 'battle',c.battle_id,c.side,COALESCE(NULLIF(c.army_id,''),mi.army_id,''),c.event_at,c.event_at,
               NULL,NULL,b.wid/10000,b.wid%10000,c.player_name,NULL,c.outcome,b.place_name,
               f.battle_id IS NOT NULL,CASE WHEN c.army_id<>'' THEN 'direct' WHEN mi.army_id IS NOT NULL THEN 'inferred' ELSE '' END
        FROM combat_sides c JOIN battle_reports b ON b.profile_id=c.profile_id AND b.battle_id=c.battle_id
        LEFT JOIN battle_march_links li INDEXED BY battle_march_by_report ON li.profile_id=c.profile_id AND li.battle_id=c.battle_id
            AND li.side=c.side AND li.method='unique_attack_arrival'
        LEFT JOIN march_samples mi ON mi.profile_id=li.profile_id AND mi.id=li.march_id
        LEFT JOIN report_bodies f ON f.profile_id=c.profile_id AND f.battle_id=c.battle_id
        WHERE c.profile_id=:profile AND c.event_at>0 AND c.player_name<>''
        AND (:army='' OR c.army_id=:army OR ((c.army_id IS NULL OR c.army_id='') AND mi.army_id=:army))
        AND (:player='' OR instr(lower(c.player_name),lower(:player))>0)
        AND (:start=0 OR c.event_at>=:start) AND (:end=0 OR c.event_at<=:end)
        AND (:wid=0 OR b.wid=:wid)
        AND NOT EXISTS(SELECT 1 FROM battle_march_links l INDEXED BY battle_march_by_report JOIN march_samples m ON m.id=l.march_id {owner_join}
            WHERE l.profile_id=c.profile_id AND l.battle_id=c.battle_id AND l.side=c.side
            AND l.method IN {CONFIRMED_ROUTE} AND {march_filters})
    ) SELECT * FROM results ORDER BY at DESC,kind,id DESC,side LIMIT :limit OFFSET :offset''', args).fetchall()
    items = [dict(row) for row in rows[:args['limit']-1]]
    for item in items:
        if item['kind'] == 'march':
            if not args['player']:
                owner = archive.db.execute(f'''SELECT {OWNER_NAME} FROM march_samples m {MARCH_OWNER}
                    WHERE m.profile_id=? AND m.id=?''', (archive.profile_id, item['id'])).fetchone()
                item['player_name'] = owner[0] if owner else ''
            item['report_count'] = archive.db.execute(f'''SELECT COUNT(DISTINCT battle_id) FROM battle_march_links
                WHERE profile_id=? AND march_id=? AND method IN {CONFIRMED_ROUTE}''',
                (archive.profile_id, item['id'])).fetchone()[0]
    return {'items': items, 'has_more': len(rows)==args['limit']}


def grouped(archive, query):
    stage = query.get('view', 'players')
    if stage not in ('players', 'teams', 'events'):
        raise ValueError('未知档案视图')
    args = {**search_parameters(query), 'profile': archive.profile_id,
            **{key: str(query.get(key, '')) for key in ('group_name', 'group_union', 'group_identity', 'team')}}
    team = args['team'] if stage == 'events' else ''
    args['team'] = team
    if stage == 'events' and team != 'pending' and not re.fullmatch(r'[0-9]{1,20}|cards:[a-f0-9]{24}', team):
        raise ValueError('请选择一个队号或待核对记录')

    march_filters = MARCH_FILTERS
    battle_filters = '''c.profile_id=:profile AND c.event_at>0 AND c.player_name<>''
        AND (:start=0 OR c.event_at>=:start) AND (:end=0 OR c.event_at<=:end) AND (:wid=0 OR b.wid=:wid)'''
    if args['army']:
        march_filters += ' AND m.army_id=:army'
        args['army_keys'] = json.dumps(army_report_keys(archive,args['army'],args['end']))
        battle_filters += ''' AND ((c.army_id=:army AND c.binding='exact_army_id') OR
            (c.battle_id,c.side) IN (SELECT json_extract(value,'$[0]'),json_extract(value,'$[1]')
                FROM json_each(:army_keys)))'''
    if args['player']:
        march_filters += f' AND instr(lower({OWNER_NAME}),lower(:player))>0'
        battle_filters += ' AND instr(lower(c.player_name),lower(:player))>0'
    if stage != 'players':
        march_filters += f" AND {OWNER_NAME}=:group_name AND COALESCE(p.union_id,'')=:group_union"
        battle_filters += " AND c.player_name=:group_name AND COALESCE(c.union_id,'')=:group_union"
    if stage == 'events' and team.isdecimal():
        march_filters += ' AND m.army_id=:team'
    elif stage=='events':
        march_filters += ' AND 0'

    # A unique name/alliance pair can link the map and report views of a player.
    # Keep same-name collisions separate when either source has multiple IDs.
    cte = f'''WITH names AS (
        SELECT name,union_id FROM players WHERE profile_id=:profile GROUP BY name,union_id
        HAVING COUNT(DISTINCT world_uid)>1 OR COUNT(DISTINCT CASE WHEN player_key LIKE 'battle:%' THEN player_key END)>1
    ), evidence AS (
        SELECT 'march' AS kind,m.id,'' AS side,m.army_id,m.begin_at AS at,m.end_at,m.from_x,m.from_y,m.to_x,m.to_y,
            {OWNER_NAME} AS player_name,COALESCE(p.union_id,'') AS union_id,
            COALESCE(p.player_key,'world:'||s.owner_uid,'') AS source_key,'' AS outcome,'' AS place_name,
            0 AS has_full_report,'' AS binding
        FROM march_samples m {MARCH_OWNER} WHERE {march_filters} AND :team<>'pending'
        UNION ALL
        SELECT 'battle',c.battle_id,c.side,CASE WHEN c.binding='exact_army_id' THEN COALESCE(c.army_id,'') ELSE '' END,
            c.event_at,c.event_at,NULL,NULL,b.wid/10000,b.wid%10000,c.player_name,COALESCE(c.union_id,''),c.player_key,
            c.outcome,b.place_name,f.battle_id IS NOT NULL,CASE WHEN c.binding='exact_army_id' AND c.army_id<>'' THEN 'direct' ELSE '' END
        FROM combat_sides c JOIN battle_reports b ON b.profile_id=c.profile_id AND b.battle_id=c.battle_id
        LEFT JOIN report_bodies f ON f.profile_id=c.profile_id AND f.battle_id=c.battle_id
        WHERE {battle_filters}
    ), identities AS (
        SELECT e.*,CASE WHEN e.player_name='' OR n.name IS NOT NULL THEN e.source_key ELSE '' END AS identity
        FROM evidence e LEFT JOIN names n ON n.name=e.player_name AND n.union_id=e.union_id
    ), matched AS (
        SELECT * FROM identities WHERE (:stage='players' OR identity=:group_identity)
    )'''
    args['stage'] = stage
    if stage == 'players':
        sql = '''SELECT m.player_name,m.union_id,m.identity,COALESCE(u.name,'') AS union_name,
            COUNT(DISTINCT NULLIF(m.army_id,'')) AS team_count,SUM(m.army_id='') AS pending_count,MAX(m.at) AS at,
            json_group_array(DISTINCT CASE WHEN m.kind='march' THEN m.army_id END) AS march_armies,
            json_group_array(CASE WHEN m.kind='battle' THEN json_array(m.id,m.side) END) AS report_keys
            FROM matched m LEFT JOIN unions u ON u.profile_id=:profile AND u.union_id=m.union_id
            GROUP BY m.player_name,m.union_id,m.identity ORDER BY at DESC,m.player_name,m.union_id,m.identity
            LIMIT :limit OFFSET :offset'''
    elif stage == 'teams':
        sql = 'SELECT * FROM matched'
    else:
        args.update(version_parameters(team, args['group_name'], args['group_union'], args['group_identity']))
        formation = ''
        if team.isdecimal():
            cte += ', '+VERSION_SEQUENCE
            formation = '''UNION ALL SELECT 'formation',v.id,'' AS side,:team,v.event_at,v.event_at,NULL,NULL,
                b.wid/10000,b.wid%10000,v.player_name,COALESCE(v.union_id,''),v.player_key,
                CASE WHEN v.prior IS NULL THEN 'first'
                    WHEN (SELECT group_concat(json_extract(value,'$.id')) FROM json_each(v.data,'$.heroes'))<>
                         (SELECT group_concat(json_extract(value,'$.id')) FROM json_each(v.prior_data,'$.heroes')) THEN 'lineup'
                    ELSE 'changed' END,COALESCE(b.place_name,''),0,'direct',:group_identity
                FROM changes v LEFT JOIN battle_reports b ON b.profile_id=:profile AND b.battle_id=v.battle_id
                WHERE (:start=0 OR v.event_at>=:start) AND (:end=0 OR v.event_at<=:end) AND (:wid=0 OR b.wid=:wid)'''
        sql = f'SELECT * FROM matched {formation}'
    rows = archive.db.execute(cte+' '+sql, args).fetchall()
    if stage == 'players':
        items = [dict(row) for row in rows[:args['limit']-1]]
        for item in items:
            bindings = report_bindings(archive,item['player_name'],item['union_id'],item['identity'],args['end'])
            armies = set(filter(None,json.loads(item.pop('march_armies'))))
            report_teams = set()
            pending = 0
            for report_key in json.loads(item.pop('report_keys')):
                evidence = bindings.get(tuple(report_key)) if report_key else None
                if evidence and not evidence['skip']:
                    if evidence['army_id']:
                        armies.add(evidence['army_id'])
                    elif evidence['formation_key']:
                        report_teams.add(evidence['formation_key'])
                    else:
                        pending += 1
            item.update(team_count=len(armies)+len(report_teams), pending_count=pending,
                        mapped_team_count=len(armies),report_only_team_count=len(report_teams))
        return {'items':items,'has_more':len(rows)==args['limit']}
    bindings = report_bindings(archive,args['group_name'],args['group_union'],args['group_identity'],args['end'])
    records = []
    for raw in rows:
        row = dict(raw)
        if row['kind']=='battle':
            evidence = bindings.get((row['id'],row['side']))
            if not evidence or evidence['skip']:
                continue
            row.update(army_id=evidence['army_id'],binding=evidence['binding'],formation_key=evidence['formation_key'])
        row['team_ref'] = row['army_id'] or row.get('formation_key') or 'pending'
        if args['army'] and row['army_id']!=args['army']:
            continue
        if stage=='events' and row['team_ref']!=team:
            continue
        records.append(row)
    if stage=='teams':
        groups = {}
        for row in records:
            item = groups.setdefault(row['team_ref'],dict(army_id=row['army_id'],team_ref=row['team_ref'],
                                                        report_only=row['team_ref'].startswith('cards:'),record_count=0,at=0))
            item.update(record_count=item['record_count']+1,at=max(item['at'],row['at']))
        from team_display import attach
        items = [dict(row,player_name=args['group_name'],union_id=args['group_union'],
                      identity=args['group_identity'],display_before=args['end']) for row in groups.values()]
        attach(archive,items)
        latest = {}
        for evidence in bindings.values():
            ref = evidence['army_id'] or evidence['formation_key']
            if ref and not evidence['skip'] and (ref not in latest or
                    (evidence['event_at'],int(evidence['battle_id']))>(latest[ref]['event_at'],int(latest[ref]['battle_id']))):
                latest[ref] = evidence
        for item in items:
            evidence = latest.get(item['team_ref'])
            if evidence:
                item.update(display_heroes=evidence['display_heroes'],display_report_id=evidence['battle_id'],
                            display_report_at=evidence['event_at'],display_binding=evidence['binding'])
        items.sort(key=lambda row:(row['team_ref']=='pending',-row['at'],row['team_ref']))
        return {'items':items}
    records.sort(key=lambda row:(row['at'],{'battle':2,'formation':1,'march':0}[row['kind']],row['id'],row['side']),reverse=True)
    page = records[args['offset']:args['offset']+args['limit']]
    return {'items':page[:args['limit']-1],'has_more':len(page)==args['limit']}


def battle_details(archive, bid, side, method):
    row = archive.db.execute('''SELECT b.event_at,b.wid,b.place_name,
        EXISTS(SELECT 1 FROM report_bodies f WHERE f.profile_id=b.profile_id AND f.battle_id=b.battle_id) AS has_full_report
        FROM battle_reports b WHERE b.profile_id=? AND b.battle_id=?''', (archive.profile_id, bid)).fetchone()
    if not row:
        return None
    sides = []
    for saved in archive.db.execute('''SELECT side,player_name,army_id,union_id,outcome,heroes,features
        FROM combat_sides WHERE profile_id=? AND battle_id=? ORDER BY side''', (archive.profile_id, bid)):
        current = dict(saved)
        current['heroes'] = side_heroes(json.loads(current['heroes']), current['side'], json.loads(current.pop('features')))
        sides.append(current)
    return {'battle_id': bid, 'side': side, 'association': method, 'sides': sides, **dict(row), 'coord': coord(row['wid'])}


def version_parameters(army, player, union, identity=''):
    return {'version_key': 'army:'+army, 'version_player': player, 'version_union': union or '', 'version_identity': identity}


def version_heroes(archive, row, data, bid):
    heroes = json.loads(data)['heroes']
    matches = archive.db.execute('''SELECT side,features FROM combat_sides
        WHERE profile_id=? AND battle_id=? AND player_key=? AND union_id=?''',
        (archive.profile_id, bid, row['player_key'], row['union_id'])).fetchall()
    return side_heroes(heroes, matches[0]['side'], json.loads(matches[0]['features'])) if len(matches)==1 else heroes


def version_record(archive, row, include_previous=False):
    result = {'event_at': row['event_at'], 'battle_id': row['battle_id'],
              'heroes': version_heroes(archive, row, row['data'], row['battle_id'])}
    if include_previous:
        result['previous_heroes'] = version_heroes(archive, row, row['prior_data'], row['prior_battle_id']) if row['prior_data'] else []
    return result


def lineup_changes(archive, army, player, union, at, identity=''):
    if not army or not player:
        return {'known_at_start': None, 'changes': []}
    args = {'profile': archive.profile_id, 'at': at, **version_parameters(army, player, union, identity)}
    start = archive.db.execute('''SELECT v.event_at,v.battle_id,v.data,v.player_key,v.union_id '''+VERSION_BASIS+
        ' AND v.event_at<=:at ORDER BY v.event_at DESC,v.observed_at DESC,v.id DESC LIMIT 1', args).fetchone()
    rows = archive.db.execute('WITH '+VERSION_SEQUENCE+''' SELECT * FROM (
        SELECT * FROM changes WHERE event_at<=:at ORDER BY event_at DESC,id DESC LIMIT 12)
        UNION ALL SELECT * FROM (SELECT * FROM changes WHERE event_at>:at ORDER BY event_at,id LIMIT 12)
        ORDER BY event_at DESC,id DESC''', args)
    return {'known_at_start': version_record(archive, start) if start else None,
            'changes': [version_record(archive, row, include_previous=True) for row in rows]}


def detail(archive, query):
    kind, ident, side = (str(query.get(key, '')) for key in ('entry', 'id', 'side'))
    strict = query.get('grouped') == '1'
    identity = str(query.get('group_identity', ''))
    if kind == 'march' and re.fullmatch(r'[a-zA-Z0-9_-]{1,100}', ident):
        row = archive.db.execute(f'''SELECT m.*,{OWNER_NAME} AS player_name,p.union_id FROM march_samples m {MARCH_OWNER}
            WHERE m.profile_id=? AND m.id=?''', (archive.profile_id, ident)).fetchone()
        if row:
            links = archive.db.execute('''SELECT l.battle_id,l.side,l.method,c.event_at
                FROM battle_march_links l JOIN combat_sides c ON c.profile_id=l.profile_id
                AND c.battle_id=l.battle_id AND c.side=l.side
                WHERE l.profile_id=? AND l.march_id=? ORDER BY c.event_at,l.battle_id''',
                (archive.profile_id, ident)).fetchall()
            bindings = report_bindings(archive,row['player_name'],row['union_id'],identity) if strict else {}
            reports, possible = [], []
            for link in links:
                evidence = bindings.get((link['battle_id'],link['side']))
                if strict and (link['method'] not in ('coordinate_time_match','unique_attack_arrival')
                               or not evidence or evidence['skip'] or evidence['army_id']!=row['army_id']):
                    continue
                report = battle_details(archive, link['battle_id'], link['side'], link['method'])
                if report:
                    (possible if link['method']=='ambiguous_marches' else reports).append(report)
            return {'kind': kind, 'army_id': row['army_id'], 'player_name': row['player_name'], 'march': dict(row),
                    'reports': reports, 'possible_reports': possible,
                    **lineup_changes(archive, row['army_id'], row['player_name'], row['union_id'], row['begin_at'], identity)}
    elif kind == 'battle' and re.fullmatch(r'[0-9]{1,20}', ident) and side in ('attack','defend'):
        row = archive.db.execute('''SELECT c.*,m.army_id AS inferred FROM combat_sides c
            LEFT JOIN battle_march_links l ON l.profile_id=c.profile_id AND l.battle_id=c.battle_id
                AND l.side=c.side AND l.method='unique_attack_arrival'
            LEFT JOIN march_samples m ON m.profile_id=l.profile_id AND m.id=l.march_id
            WHERE c.profile_id=? AND c.battle_id=? AND c.side=?''', (archive.profile_id, ident, side)).fetchone()
        if row:
            direct = row['army_id'] if row['binding']=='exact_army_id' else None
            evidence = report_bindings(archive,row['player_name'],row['union_id'],identity).get((ident,side))
            army = (evidence['army_id'] or None) if evidence and not evidence['skip'] else None
            method = evidence['binding'] if evidence and not evidence['skip'] else ''
            march = None
            if army:
                matches = archive.db.execute(f'''SELECT m.* FROM battle_march_links l
                    JOIN march_samples m ON m.profile_id=l.profile_id AND m.id=l.march_id {MARCH_OWNER}
                    WHERE l.profile_id=? AND l.battle_id=? AND l.side=? AND l.method IN {CONFIRMED_ROUTE}
                    AND m.army_id=? AND (p.name IS NULL OR p.name=?) AND (p.union_id IS NULL OR p.union_id=?) LIMIT 2''',
                    (archive.profile_id, ident, side, army, row['player_name'], row['union_id'])).fetchall()
                if len(matches)==1:
                    march = dict(matches[0])
                    method = 'coordinate_time_match' if direct else method
            report = battle_details(archive, ident, side, method)
            if report:
                return {'kind': kind, 'army_id': army, 'player_name': row['player_name'], 'march': march,
                        'reports': [report], 'possible_reports': [], 'inferred_army_id': row['inferred'] if not direct else None,
                        **lineup_changes(archive, army, row['player_name'], row['union_id'],
                                         march['begin_at'] if march else row['event_at'], identity)}
    elif kind == 'formation' and re.fullmatch(r'[a-f0-9]{64}', ident):
        saved = archive.db.execute('''SELECT team_key,player_name,union_id FROM team_versions
            WHERE profile_id=? AND id=? AND binding='exact_army_id' ''', (archive.profile_id, ident)).fetchone()
        if saved and saved['team_key'].startswith('army:'):
            army = saved['team_key'][5:]
            params = {'profile': archive.profile_id, 'id': ident,
                      **version_parameters(army, saved['player_name'], saved['union_id'], identity)}
            row = archive.db.execute('WITH '+VERSION_SEQUENCE+' SELECT * FROM changes WHERE id=:id', params).fetchone()
            if row:
                return {'kind': kind, 'army_id': army, 'player_name': row['player_name'],
                        'formation': version_record(archive, row, include_previous=True)}
    raise ValueError('此记录在当前赛季档案中不可用')
