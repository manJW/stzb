"""Small, scoped lineup labels for paged UI records; IDs remain internal keys."""
import json

from combat_loadout import side_heroes
from observation_codec import report_system_npc


def attach(archive, rows):
    """Attach latest linked heroes without crossing player/team scope.

    One indexed lookup per distinct scope happens within a batched SQL statement.
    Missing or incomplete newest evidence never falls back to another army/build.
    An optional display_before timestamp makes historical labels time-correct.
    """
    requests = {}
    for row in rows:
        scope = (str(row.get('army_id') or ''), row.get('player_name') or '',
                 str(row.get('union_id') or ''), row.get('identity') or '',
                 row.get('display_before') or 0)
        row['display_heroes'] = []
        row['display_report_at'] = None
        row['display_report_id'] = None
        row['display_binding'] = None
        if scope[0] and scope[1]:
            requests.setdefault(scope, []).append(row)
    scopes = list(requests)
    for start in range(0, len(scopes), 100):
        batch = scopes[start:start+100]
        params = [value for index, scope in enumerate(batch) for value in (index, *scope)]
        params.extend([archive.profile_id, archive.profile_id])
        sql = '''WITH wanted(i,army,name,uid,identity,before_at) AS (VALUES ''' + ','.join(
            '(?,?,?,?,?,?)' for _ in batch) + '''), choices AS (
            SELECT w.i,'exact_army_id' AS method,(
                SELECT x.rowid FROM combat_sides x INDEXED BY combat_army
                WHERE x.profile_id=? AND x.army_id=w.army AND x.player_name=w.name
                AND (w.uid='' OR x.union_id=w.uid) AND (w.identity='' OR x.player_key=w.identity)
                AND x.binding='exact_army_id' AND (w.before_at=0 OR x.event_at<=w.before_at)
                ORDER BY x.event_at DESC,CAST(x.battle_id AS INTEGER) DESC,x.side LIMIT 1) AS rid FROM wanted w
            UNION ALL
            SELECT w.i,'unique_attack_arrival',(
                SELECT x.rowid FROM march_samples m INDEXED BY march_history
                JOIN battle_march_links l INDEXED BY battle_march_by_march
                    ON l.profile_id=m.profile_id AND l.march_id=m.id AND l.method='unique_attack_arrival'
                JOIN combat_sides x ON x.profile_id=l.profile_id AND x.battle_id=l.battle_id AND x.side=l.side
                WHERE m.profile_id=? AND m.army_id=w.army AND x.player_name=w.name
                AND (w.uid='' OR x.union_id=w.uid) AND (w.identity='' OR x.player_key=w.identity)
                AND (w.before_at=0 OR x.event_at<=w.before_at)
                AND (x.army_id IS NULL OR x.army_id='' OR x.army_id=w.army)
                AND NOT EXISTS(SELECT 1 FROM battle_march_links other
                    JOIN march_samples om ON om.profile_id=other.profile_id AND om.id=other.march_id
                    WHERE other.profile_id=l.profile_id AND other.battle_id=l.battle_id AND other.side=l.side
                    AND other.method IN ('unique_attack_arrival','coordinate_time_match') AND om.army_id<>w.army)
                ORDER BY x.event_at DESC,CAST(x.battle_id AS INTEGER) DESC,x.side LIMIT 1) FROM wanted w
            ), ranked AS (
                SELECT q.i,q.method,c.side,c.npc,c.heroes,c.features,c.event_at,c.battle_id,
                    ROW_NUMBER() OVER(PARTITION BY q.i ORDER BY c.event_at DESC,
                        CAST(c.battle_id AS INTEGER) DESC,c.side,q.method) AS newest
                FROM choices q JOIN combat_sides c ON c.rowid=q.rid
            ) SELECT * FROM ranked WHERE newest=1'''
        for saved in archive.db.execute(sql, params):
            features = json.loads(saved['features'])
            if report_system_npc({**features, 'npc': saved['npc']}, saved['side']):
                continue
            heroes = side_heroes(json.loads(saved['heroes']), saved['side'], features)
            label = [{key: hero.get(key) for key in ('id', 'name', 'position', 'level', 'red_stars')}
                     for hero in heroes]
            for row in requests[batch[saved['i']]]:
                row['display_heroes'] = label
                row['display_report_at'] = saved['event_at']
                row['display_report_id'] = saved['battle_id']
                row['display_binding'] = saved['method']
    return rows


def attach_events(archive, rows):
    teams=[]
    for row in rows:
        if row.get('kind') in ('army_state','out_of_view'):
            owner=archive.db.execute('''SELECT p.name,p.union_id FROM army_states s
                JOIN players p ON p.profile_id=s.profile_id AND p.player_key='world:'||s.owner_uid
                WHERE s.profile_id=? AND s.army_id=? AND s.observed_at<=?
                ORDER BY s.observed_at DESC,s.id LIMIT 1''',
                (archive.profile_id,row['entity_key'],row['observed_at'])).fetchone()
            row.update(army_id=row['entity_key'],player_name=owner['name'] if owner else '',
                       union_id=owner['union_id'] if owner else '',display_before=row.get('event_at') or row['observed_at'])
            teams.append(row)
        elif row.get('kind')=='team_configuration_changed':
            version=archive.db.execute('SELECT data FROM team_versions WHERE profile_id=? AND id=?',
                (archive.profile_id,row['data'].get('new_version',''))).fetchone()
            row['display_heroes']=json.loads(version['data']).get('heroes',[]) if version else []
    attach(archive,teams)
    return rows
