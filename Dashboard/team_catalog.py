"""Ordered hero formations, immutable loadouts and incremental battle facts."""
import json
import re
import time

from combat_loadout import side_heroes
from observation_codec import digest, packed, report_system_npc, report_has_npc

VERSION = '1'
SCHEMA = '''
CREATE TABLE IF NOT EXISTS formation_teams(
 profile_id TEXT NOT NULL,formation_id TEXT NOT NULL,player_key TEXT NOT NULL,player_name TEXT NOT NULL,
 union_id TEXT NOT NULL,roster TEXT NOT NULL,latest_at REAL NOT NULL,battle_id TEXT NOT NULL,
 config_id TEXT NOT NULL,category TEXT NOT NULL,PRIMARY KEY(profile_id,formation_id));
CREATE INDEX IF NOT EXISTS formation_player ON formation_teams(profile_id,player_name,union_id,latest_at);
CREATE TABLE IF NOT EXISTS formation_configs(
 profile_id TEXT NOT NULL,config_id TEXT NOT NULL,formation_id TEXT NOT NULL,tactic_key TEXT NOT NULL,
 heroes TEXT NOT NULL,first_at REAL NOT NULL,last_at REAL NOT NULL,
 PRIMARY KEY(profile_id,config_id));
CREATE INDEX IF NOT EXISTS config_formation ON formation_configs(profile_id,formation_id,last_at);
CREATE TABLE IF NOT EXISTS formation_facts(
 profile_id TEXT NOT NULL,battle_id TEXT NOT NULL,side TEXT NOT NULL,player_key TEXT NOT NULL,
 player_name TEXT NOT NULL,union_id TEXT NOT NULL,event_at REAL NOT NULL,wid INTEGER,
 formation_id TEXT,config_id TEXT,tactic_key TEXT,category TEXT NOT NULL,outcome TEXT NOT NULL,
 pvp INTEGER NOT NULL,opponent_main INTEGER NOT NULL,damage INTEGER,losses INTEGER,
 own_initial INTEGER,own_remaining INTEGER,own_wounded INTEGER,own_dead INTEGER,
 enemy_dead INTEGER,force_type INTEGER,PRIMARY KEY(profile_id,battle_id,side));
CREATE INDEX IF NOT EXISTS formation_fact_team ON formation_facts(profile_id,formation_id,event_at,battle_id);
CREATE INDEX IF NOT EXISTS formation_fact_player ON formation_facts(profile_id,union_id,player_key,event_at);
CREATE INDEX IF NOT EXISTS formation_fact_config ON formation_facts(profile_id,config_id,event_at);
CREATE INDEX IF NOT EXISTS formation_fact_time ON formation_facts(profile_id,event_at,battle_id);
CREATE INDEX IF NOT EXISTS formation_fact_identity ON formation_facts(profile_id,player_key,event_at,formation_id);
'''


def classify(heroes, raw):
    from lineup_prediction import meets_main_threshold, spartan_build_basis, empty_skill_basis
    if meets_main_threshold(heroes): return 'main'
    if spartan_build_basis(heroes) or empty_skill_basis(heroes): return 'spartan'
    segments = str(raw or '').strip(';').split(';')
    if heroes and len(heroes)<3 and len(segments)==3 and all(len(s.split(','))>=5 for s in segments):
        return 'spartan'
    return 'unknown'


def roster(heroes, raw):
    slots = {h.get('position'):h.get('id') for h in heroes if h.get('id')}
    if len(slots)==3 and set(slots)=={1,2,3}: return [slots[p] for p in (1,2,3)]
    segments = str(raw or '').strip(';').split(';')
    if len(segments)!=3 or any(len(s.split(','))<5 for s in segments): return None
    try: ids=[int(s.split(',')[0]) for s in segments]
    except ValueError:return None
    if not any(ids) or any(slots.get(p)!=v for p,v in enumerate(ids,1) if v):return None
    return ids


def hp(heroes, field):
    values=[h.get(field) for h in heroes]
    return sum(values) if values and all(type(v)is int and v>=0 for v in values) else None


def loadout(heroes):
    fields=('id','name','position','level','red_stars','troop_code','troop_name','skill_slot_ids','skills','gear','gear_reported')
    return [{k:h.get(k) for k in fields} for h in sorted(heroes,key=lambda h:h['position'])]


def config_signature(heroes):
    # Dictionary corrections are display changes, not a new combat configuration.
    return [dict(id=h['id'],position=h['position'],level=h.get('level'),red=h.get('red_stars'),
                 troop=h.get('troop_code'),slots=h.get('skill_slot_ids'),
                 skills=[(s.get('id'),s.get('level')) for s in h.get('skills')or[]],
                 gear={k:h['gear'].get(k)for k in ('id','level','forge_id')}if h.get('gear')else None,
                 gear_reported=h.get('gear_reported'))for h in heroes]


def install(db):db.executescript(SCHEMA)


def human_opponents(battle):
    return not report_has_npc(battle) and all(str(battle.get(s+'_role_id')or'')not in ('','0')
        and bool(battle.get(s+'_name'))for s in ('attack','defend'))


def migrate_player_identity(archive):
    rule='formation-player-identity-2';db=archive.db;pid=archive.profile_id
    if db.execute('SELECT 1 FROM lineup_rule_migrations WHERE profile_id=? AND rule=?',(pid,rule)).fetchone():return
    pending=set()
    for row in db.execute('''SELECT f.battle_id,b.data FROM formation_facts f JOIN battle_reports b
        ON b.profile_id=f.profile_id AND b.battle_id=f.battle_id WHERE f.profile_id=? AND f.pvp=0''',(pid,)):
        if human_opponents(json.loads(row['data'])):pending.add(row['battle_id'])
    db.executemany('INSERT OR IGNORE INTO attendance_dirty VALUES(?,?)',[(pid,bid)for bid in pending])
    db.execute('INSERT INTO lineup_rule_migrations VALUES(?,?)',(pid,rule))


def index_battle(archive, bid, rows, battle):
    """Upsert one report atomically; the worker's existing dirty queue drives this."""
    db,pid=archive.db,archive.profile_id
    previous={r[0] for r in db.execute('SELECT formation_id FROM formation_facts WHERE profile_id=? AND battle_id=?',(pid,bid)) if r[0]}
    db.execute('DELETE FROM formation_facts WHERE profile_id=? AND battle_id=?',(pid,bid))
    decoded={};all_heroes={}
    for saved in rows:
        row=dict(saved);features=json.loads(row['features'])
        heroes=side_heroes(json.loads(row['heroes']),row['side'],features)
        all_heroes[row['side']]=heroes
        if report_system_npc({**features,'npc':row['npc']},row['side']) or not heroes:continue
        ids=roster(heroes,features.get(row['side']+'_all_hero_info'))
        category=classify(heroes,features.get(row['side']+'_all_hero_info'))
        fid=digest([pid,row['player_key'],ids])[:32] if ids else None
        data=loadout(heroes)
        cid=digest([fid,config_signature(data)])[:32] if fid else None
        tactics=digest([fid,[(h['position'],h.get('skill_slot_ids')) for h in data]])[:32] if fid else None
        decoded[row['side']]=dict(row=row,features=features,heroes=heroes,ids=ids,category=category,fid=fid,cid=cid,tactics=tactics,data=data)
        if fid:
            previous.add(fid)
            db.execute('''INSERT INTO formation_configs VALUES(?,?,?,?,?,?,?) ON CONFLICT(profile_id,config_id)
                DO UPDATE SET first_at=MIN(first_at,excluded.first_at),last_at=MAX(last_at,excluded.last_at),heroes=excluded.heroes''',
                (pid,cid,fid,tactics,packed(data),row['event_at'],row['event_at']))
    for side,item in decoded.items():
        row,heroes=item['row'],item['heroes'];other=decoded.get('defend' if side=='attack' else 'attack')
        # Missing enemy hero details do not turn a verified human opponent into NPC.
        pvp=human_opponents(battle)
        initial,remaining,wounded=(hp(heroes,k) for k in ('initial_hp','remaining_hp','wounded_hp'))
        loss=initial-remaining if initial is not None and remaining is not None and initial>=remaining else None
        dead=initial-remaining-wounded if loss is not None and wounded is not None and wounded<=loss else None
        enemy=all_heroes.get('defend' if side=='attack' else 'attack',[])
        ei,er,ew=(hp(enemy,k)for k in ('initial_hp','remaining_hp','wounded_hp'))
        damage=ei-er if ei is not None and er is not None and ei>=er else None
        enemy_dead=damage-ew if damage is not None and ew is not None and ew<=damage else None
        db.execute('INSERT INTO formation_facts VALUES('+','.join('?'for _ in range(23))+')',
            (pid,bid,side,row['player_key'],row['player_name'],row['union_id'],row['event_at'],row['wid'],
             item['fid'],item['cid'],item['tactics'],item['category'],row['outcome'],int(pvp),
             int(bool(other and other['category']=='main')),damage,loss,initial,remaining,wounded,dead,enemy_dead,
             item['features'].get(side+'er_force')))
    # A corrected report may change formations; reselect latest facts for both.
    for fid in previous:
        latest=db.execute('''SELECT f.*,c.heroes FROM formation_facts f JOIN formation_configs c
            ON c.profile_id=f.profile_id AND c.config_id=f.config_id WHERE f.profile_id=? AND f.formation_id=?
            ORDER BY f.event_at DESC,CAST(f.battle_id AS INTEGER) DESC,f.side LIMIT 1''',(pid,fid)).fetchone()
        if not latest:
            db.execute('DELETE FROM formation_teams WHERE profile_id=? AND formation_id=?',(pid,fid));continue
        hs=json.loads(latest['heroes']);ids=[next((h['id']for h in hs if h['position']==pos),0) for pos in (1,2,3)]
        db.execute('INSERT OR REPLACE INTO formation_teams VALUES(?,?,?,?,?,?,?,?,?,?)',
            (pid,fid,latest['player_key'],latest['player_name'],latest['union_id'],packed(ids),latest['event_at'],
             latest['battle_id'],latest['config_id'],latest['category']))


AGG = '''COUNT(*) samples,SUM(f.pvp) pvp_samples,SUM(NOT f.pvp) pve_samples,
 SUM(f.pvp AND f.outcome='win') wins,SUM(f.pvp AND f.outcome='loss') defeats,SUM(f.pvp AND f.outcome='draw') draws,
 SUM(f.pvp AND f.category='main' AND f.opponent_main) main_samples,
 SUM(f.pvp AND f.category='main' AND f.opponent_main AND f.outcome='win') main_wins,
 SUM(f.pvp AND f.category='main' AND f.opponent_main AND f.outcome IN ('win','loss','draw')) main_decided,
 SUM(CASE WHEN f.pvp AND f.damage IS NOT NULL AND f.losses IS NOT NULL THEN f.damage END) damage,
 SUM(CASE WHEN f.pvp AND f.damage IS NOT NULL AND f.losses IS NOT NULL THEN f.losses END) losses,
 SUM(f.pvp AND f.damage IS NOT NULL AND f.losses IS NOT NULL) hp_known,
 SUM(CASE WHEN f.pvp AND f.category='main' AND f.opponent_main AND f.damage IS NOT NULL AND f.losses IS NOT NULL THEN f.damage END) main_damage,
 SUM(CASE WHEN f.pvp AND f.category='main' AND f.opponent_main AND f.damage IS NOT NULL AND f.losses IS NOT NULL THEN f.losses END) main_losses,
 SUM(f.pvp AND f.category='main' AND f.opponent_main AND f.damage IS NOT NULL AND f.losses IS NOT NULL) main_hp_known,
 MAX(f.event_at) at'''


def rates(row):
    n=(row.get('wins')or 0)+(row.get('defeats')or 0)+(row.get('draws')or 0)
    row['win_rate']=row['wins']/n if n else None
    row['exchange']=row['damage']/row['losses'] if row.get('losses') and row['hp_known']==row['pvp_samples'] else None
    row['main_win_rate']=row['main_wins']/row['main_decided'] if row.get('main_decided') else None
    row['main_exchange']=row['main_damage']/row['main_losses'] if row.get('main_losses') and row['main_hp_known']==row['main_samples'] else None
    row['zero_loss']=bool(row.get('pvp_samples') and row['hp_known']==row['pvp_samples'] and row['losses']==0)
    row['main_zero_loss']=bool(row.get('main_samples') and row['main_hp_known']==row['main_samples'] and row['main_losses']==0)
    return row


def query(archive, q):
    """Shared user-facing formation catalog for intelligence and season history."""
    pid=archive.profile_id;view=q.get('view','players');fid=q.get('formation_id','');cid=q.get('config_id','')
    if view not in ('summary','players','teams','events','configs','matchups'):raise ValueError('未知队伍视图')
    if fid and not re.fullmatch('[a-f0-9]{32}',fid):raise ValueError('队伍选择无效')
    if cid and not re.fullmatch('[a-f0-9]{32}',cid):raise ValueError('配置选择无效')
    limit=max(1,min(int(q.get('limit')or 25),100));offset=max(0,int(q.get('offset')or 0))
    start=max(float(q.get('from_at')or 0),archive.retention_from);end=float(q.get('to_at')or 0)
    where=['f.profile_id=?','f.event_at>=?'];args=[pid,start]
    for column,value in (('formation_id',fid),('config_id',cid),('player_key',q.get('player_key'))):
        if value:where.append('f.'+column+'=?');args.append(value)
    if end:where.append('f.event_at<=?');args.append(end)
    if q.get('region'):
        region=[int(v)for v in str(q['region']).split(',')]
        if len(region)!=4:raise ValueError('范围需填写四个坐标值')
        where.extend(['CAST(f.wid/10000 AS INTEGER) BETWEEN ? AND ?','f.wid%10000 BETWEEN ? AND ?'])
        args.extend([region[0],region[2],region[1],region[3]])
    if q.get('official_campaign_id'):
        campaign=archive.intelligence.official_campaigns().get(str(q['official_campaign_id']))
        if not campaign:raise ValueError('官方战役尚未取得')
        blocks=campaign['blocks']
        where.append('((CAST((CAST(f.wid/10000 AS INTEGER)-1)/10 AS INTEGER)+1)*1000+CAST((f.wid%10000-1)/10 AS INTEGER)+1) IN ('+','.join('?'for _ in blocks)+')')
        args.extend(blocks)
        where.append('f.event_at>=?');args.append(campaign['filters']['from_at'])
        if campaign['filters']['to_at']:where.append('f.event_at<=?');args.append(campaign['filters']['to_at'])
    if q.get('coord'):
        parts=re.split('[,，\\s]+',q['coord'].strip())
        if len(parts)!=2 or not all(p.isdecimal()for p in parts):raise ValueError('坐标格式无效')
        where.append('f.wid=?');args.append(int(parts[0])*10000+int(parts[1]))
    if q.get('player'):
        where.append('instr(lower(f.player_name),lower(?))>0');args.append(q['player'])
    union=q.get('union_id') or (archive.profile['union_id'] if q.get('relation')=='ally' else '')
    if q.get('relation')in ('self','ally')and (q.get('relation')=='self'or str(union)=='0')and not q.get('union_id'):
        from role_context import context
        own_key=context(archive.profile)['own_player_key']
        where.append('f.player_key=?'if own_key else '0')
        if own_key:args.append(own_key)
    elif union:where.append('f.union_id=?');args.append(str(union))
    elif q.get('relation')=='enemy':
        filters=archive.intelligence.filters(q);unions=archive.intelligence.unions(filters) or set()
        excluded={uid for uid,v in archive.intelligence.query_relations.items()if v.get('relation')=='friendly' or v.get('manual_override')and v.get('relation')!='enemy'}
        if str(archive.profile['union_id'])not in ('','0'):excluded.add(str(archive.profile['union_id']))
        other='(f.force_type=1 AND f.player_key<>?'+(' AND f.union_id NOT IN ('+','.join('?'for _ in excluded)+')'if excluded else'')+')'
        if unions:where.append('(f.union_id IN ('+','.join('?'for _ in unions)+') OR '+other+')');args.extend(sorted(unions))
        else:where.append(other)
        args.extend(['battle:'+str(archive.profile.get('battle_role_id')or archive.profile['account_role_id']),*sorted(excluded)])
    search=q.get('search','').strip()
    if search:where.append('(instr(lower(f.player_name),lower(?))>0 OR instr(lower(c.heroes),lower(?))>0)');args.extend([search,search])
    if q.get('category')in ('main','spartan','unknown'):where.append('t.category=?');args.append(q['category'])
    joins='''FROM formation_facts f JOIN formation_teams t ON t.profile_id=f.profile_id AND t.formation_id=f.formation_id
        JOIN formation_configs c ON c.profile_id=t.profile_id AND c.config_id=t.config_id'''
    base=joins+' WHERE '+' AND '.join(where)
    if view=='players' and not any(q.get(k)for k in ('from_at','to_at','coord','region','official_campaign_id')):
        # Default player lists read one maintained latest fact per formation,
        # rather than regrouping the entire battle ledger on every refresh.
        current='''FROM formation_teams t CROSS JOIN formation_facts f
            ON f.profile_id=t.profile_id AND f.battle_id=t.battle_id AND f.formation_id=t.formation_id
            AND f.event_at=t.latest_at JOIN formation_configs c ON c.profile_id=t.profile_id AND c.config_id=t.config_id'''
        base=current+' WHERE '+' AND '.join(where)
    if view=='summary':
        row=archive.db.execute('''SELECT COUNT(DISTINCT f.player_key),COUNT(DISTINCT f.formation_id),COUNT(DISTINCT f.battle_id),
            COUNT(DISTINCT CASE WHEN EXISTS(SELECT 1 FROM report_bodies r WHERE r.profile_id=f.profile_id AND r.battle_id=f.battle_id)
            THEN f.battle_id END) '''+base,args).fetchone()
        return dict(observed_names=row[0],observed_armies=row[1],observed_linked_armies=row[1],observed_inferred_only_armies=0,
                    battle_reports=row[2],full_reports=row[3],marches=0,marches_with_report_match=0)
    if view=='players':
        sql='''SELECT f.player_key,MAX(f.player_name) player_name,MAX(f.union_id) union_id,
            COUNT(DISTINCT f.formation_id) team_count,COUNT(DISTINCT CASE WHEN t.category='main' THEN f.formation_id END) main_count,
            COUNT(DISTINCT CASE WHEN t.category='spartan' THEN f.formation_id END) spartan_count,
            COUNT(DISTINCT CASE WHEN t.category='unknown' THEN f.formation_id END) unknown_count,MAX(f.event_at) at '''+base+' GROUP BY f.player_key ORDER BY at DESC,f.player_key LIMIT ? OFFSET ?'
        rows=[dict(r)for r in archive.db.execute(sql,(*args,limit+1,offset))]
    elif view in ('teams','configs'):
        key='f.config_id' if view=='configs' else 'f.formation_id'
        sql=f'SELECT f.formation_id,f.config_id,f.player_key,f.player_name,MAX(f.union_id) union_id,{AGG} '+base+f" GROUP BY {key} ORDER BY CASE t.category WHEN 'main' THEN 0 WHEN 'spartan' THEN 1 ELSE 2 END,at DESC,{key} LIMIT ? OFFSET ?"
        rows=[]
        for saved in archive.db.execute(sql,(*args,limit+1,offset)):
            row=rates(dict(saved));config=archive.db.execute('''SELECT c.*,t.category,t.battle_id latest_battle_id,t.latest_at
                FROM formation_teams t JOIN formation_configs c ON c.profile_id=t.profile_id AND c.config_id=
                CASE WHEN ?='configs' THEN ? ELSE t.config_id END WHERE t.profile_id=? AND t.formation_id=?''',
                (view,row['config_id'],pid,row['formation_id'])).fetchone()
            if config:row.update(heroes=json.loads(config['heroes']),category=config['category'],latest_battle_id=config['latest_battle_id'],latest_at=config['latest_at'],config_id=config['config_id'],tactic_key=config['tactic_key'])
            if end or view=='configs':
                known=archive.db.execute('''SELECT f.*,c.heroes FROM formation_facts f JOIN formation_configs c
                    ON c.profile_id=f.profile_id AND c.config_id=f.config_id WHERE f.profile_id=? AND f.formation_id=?
                    AND (?=0 OR f.event_at<=?) AND (?<>'configs' OR f.config_id=?)
                    ORDER BY f.event_at DESC,CAST(f.battle_id AS INTEGER) DESC LIMIT 1''',
                    (pid,row['formation_id'],end,end,view,row['config_id'])).fetchone()
                if known:row.update(heroes=json.loads(known['heroes']),category=known['category'],latest_battle_id=known['battle_id'],latest_at=known['event_at'],config_id=known['config_id'])
            rows.append(row)
        rows.sort(key=lambda r:({'main':0,'spartan':1,'unknown':2}.get(r.get('category'),2),-r['at']))
    elif view=='events':
        sql='''SELECT f.*,b.place_name,c2.heroes,EXISTS(SELECT 1 FROM report_bodies r WHERE r.profile_id=f.profile_id AND r.battle_id=f.battle_id) has_full_report
            '''+base.replace(joins,joins+' JOIN battle_reports b ON b.profile_id=f.profile_id AND b.battle_id=f.battle_id LEFT JOIN formation_configs c2 ON c2.profile_id=f.profile_id AND c2.config_id=f.config_id')+' ORDER BY f.event_at DESC,CAST(f.battle_id AS INTEGER) DESC LIMIT ? OFFSET ?'
        rows=[dict(r)for r in archive.db.execute(sql,(*args,limit+1,offset))]
        for row in rows:
            row['heroes']=json.loads(row['heroes']or'[]')
            # Only evidence-linked marches belong to this report/formation.
            row['marches']=[dict(m)for m in archive.db.execute('''SELECT DISTINCT m.from_x,m.from_y,m.to_x,m.to_y,
                m.begin_at,m.end_at,m.duration_seconds,l.method FROM battle_march_links l JOIN march_samples m
                ON m.profile_id=l.profile_id AND m.id=l.march_id WHERE l.profile_id=? AND l.battle_id=? AND l.side=?
                ORDER BY m.begin_at''',(pid,row['battle_id'],row['side']))]
    else:
        sql=f'''SELECT o.formation_id opponent_formation_id,o.config_id opponent_config_id,{AGG}
            '''+base.replace(joins,joins+" JOIN formation_facts o ON o.profile_id=f.profile_id AND o.battle_id=f.battle_id AND o.side<>f.side")+" AND f.pvp=1 AND f.category='main' AND f.opponent_main=1 GROUP BY o.formation_id,o.config_id ORDER BY samples DESC LIMIT ? OFFSET ?"
        rows=[rates(dict(r))for r in archive.db.execute(sql,(*args,limit+1,offset))]
        for row in rows:
            config=archive.db.execute('SELECT heroes FROM formation_configs WHERE profile_id=? AND config_id=?',(pid,row['opponent_config_id'])).fetchone()
            row['opponent_heroes']=json.loads(config[0])if config else []
    pending=archive.db.execute('SELECT COUNT(*) FROM attendance_dirty WHERE profile_id=?',(pid,)).fetchone()[0]
    return dict(items=rows[:limit],has_more=len(rows)>limit,pending=pending,retain_from=archive.retention_from,updated_at=time.time())
