"""Occupation evidence independent of army archives and PvP eligibility."""
import json
import re
import time
import uuid
from observation_codec import digest, packed, report_system_npc

SCHEMA='''
CREATE TABLE IF NOT EXISTS territory_current(
 profile_id TEXT NOT NULL,wid INTEGER NOT NULL,owner_id TEXT,union_id TEXT,city_type INTEGER,
 observed_at REAL NOT NULL,session TEXT NOT NULL,message_ref TEXT,PRIMARY KEY(profile_id,wid));
CREATE TABLE IF NOT EXISTS territory_events(
 id TEXT PRIMARY KEY,profile_id TEXT NOT NULL,wid INTEGER NOT NULL,kind TEXT NOT NULL,
 event_at REAL NOT NULL,observed_from REAL,observed_to REAL,actor_key TEXT,actor_name TEXT,actor_union TEXT,
 old_player_key TEXT,old_owner TEXT,old_union TEXT,new_owner TEXT,new_union TEXT,level INTEGER,
 countable INTEGER NOT NULL,battle_id TEXT,evidence TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS territory_time ON territory_events(profile_id,event_at,wid);
CREATE INDEX IF NOT EXISTS territory_actor ON territory_events(profile_id,actor_union,actor_key,event_at);
CREATE INDEX IF NOT EXISTS territory_report ON territory_events(profile_id,battle_id);
CREATE TABLE IF NOT EXISTS member_metric_snapshots(
 profile_id TEXT NOT NULL,player_key TEXT NOT NULL,observed_at REAL NOT NULL,
 joined_at REAL,honor INTEGER,source TEXT NOT NULL,PRIMARY KEY(profile_id,player_key,observed_at,source));
CREATE TABLE IF NOT EXISTS durability_snapshots(
 profile_id TEXT NOT NULL,wid INTEGER NOT NULL,kind TEXT NOT NULL,event_at REAL NOT NULL,
 current INTEGER NOT NULL,maximum INTEGER,message_ref TEXT,
 PRIMARY KEY(profile_id,wid,kind,event_at));
'''


def install(archive):
    archive.db.executescript(SCHEMA)
    archive.db.execute('CREATE INDEX IF NOT EXISTS tile_history_scope ON tiles(profile_id,wid,observed_at)')
    archive.territory_session=uuid.uuid4().hex


def seed_metrics(archive):
    import zlib
    rule='member-metric-snapshots-1'
    if archive.db.execute('SELECT 1 FROM lineup_rule_migrations WHERE profile_id=? AND rule=?',(archive.profile_id,rule)).fetchone():return
    rows=archive.db.execute('''SELECT payload,first_seen,id FROM messages WHERE profile_id=? AND message_id IN (90005,99991)
        AND first_seen>=? ORDER BY first_seen DESC LIMIT 100''',(archive.profile_id,archive.retention_from)).fetchall()
    for row in reversed(rows):
        try:observe_tables(archive,json.loads(zlib.decompress(row['payload'])),row['first_seen'],row['id'])
        except (ValueError,TypeError,zlib.error):continue
    archive.db.execute('INSERT INTO lineup_rule_migrations VALUES(?,?)',(archive.profile_id,rule))


def player(archive, uid):
    own=archive.profile
    if str(uid)==str(own.get('account_role_id'))and own.get('battle_role_id'):
        return dict(key='battle:'+own['battle_role_id'],name=own['account_name'],union=str(own['union_id']))
    row=archive.db.execute("SELECT name,union_id,role_id FROM players WHERE profile_id=? AND player_key=?",
                           (archive.profile_id,'world:'+str(uid))).fetchone()
    return dict(key=('battle:'+row['role_id']) if row and row['role_id'] else 'world:'+str(uid),
                name=row['name']if row else '',union=row['union_id']if row else '')


def normalized_key(archive,key):
    return player(archive,key[6:])['key']if key and key.startswith('world:')else key


def observe_durability(archive,wid,chunks,at,ref):
    # Verified ChunkMsgType: FORTIFY=17, NPC_CITY_DURABILITY=21.
    # A map-wide durability drop has no proven attacking player; never credit it.
    if at<archive.retention_from:return
    for code,kind,current_idx,max_idx in (('17','fortify',1,None),('21','npc_city',2,1)):
        values=chunks.get(code)
        if not isinstance(values,list)or len(values)<=current_idx:continue
        current=values[current_idx];maximum=values[max_idx]if max_idx is not None else None
        if type(current)is not int or current<0 or maximum is not None and (type(maximum)is not int or maximum<current):continue
        archive.db.execute('INSERT OR REPLACE INTO durability_snapshots VALUES(?,?,?,?,?,?,?)',
                           (archive.profile_id,int(wid),kind,at,current,maximum,ref))


def observe_tables(archive,value,at,ref):
    """Client 9.3.4 Tb.SetValueAt: union_attr[1]=join_time, user_res[101]=wu_xun_total."""
    from archive import table_updates
    def save(operation,table,body):
        index=1 if table=='Tb_user_union_attr' else 101 if table=='Tb_user_res' else None
        if index is None or not body:return
        if operation==1:
            if len(body)<=index:return
            uid,amount=body[0],body[index]
        elif operation==2 and len(body)>=4 and body[0]==0:
            uid=body[1];fields=dict(zip(body[2::2],body[3::2]))
            if index not in fields:return
            amount=fields[index]
        else:return
        if type(uid)is not int or uid<=0 or type(amount)is not int or amount<0:return
        key=player(archive,uid)['key']
        # A join-date update must not make a previously observed honor fresh.
        joined=(amount or None)if index==1 else None
        honor=amount if index==101 else None
        archive.db.execute('INSERT OR REPLACE INTO member_metric_snapshots VALUES(?,?,?,?,?,?)',
                           (archive.profile_id,key,at,joined,honor,table+':'+str(index)))
        archive.contribution_revision+=1
    for operation,table,body in table_updates(value):save(operation,table,body)
    def initial(node):
        if isinstance(node,list):
            if len(node)==2 and node[0]in ('Tb_user_res','Tb_user_union_attr') and isinstance(node[1],list):
                for row in node[1]:
                    if isinstance(row,list):save(1,node[0],row)
                return
            for child in node:
                if isinstance(child,(list,dict)):initial(child)
        elif isinstance(node,dict):
            for child in node.values():
                if isinstance(child,(list,dict)):initial(child)
    initial(value)


def observe_roster(archive,rows,union,at):
    """Client 9.3.4 UnionData.SetUnionMember: join[30], ranger total[15]."""
    if str(union)in ('','0')or str(union)!=str(archive.profile['union_id']):return
    from role_context import context
    wanderer=context(archive.profile)['is_wanderer']
    for row in rows:
        if not isinstance(row,list)or len(row)<31 or type(row[0])is not int or row[0]<=0:continue
        joined=row[30]if type(row[30])is int and 0<row[30]<=at else None
        honor=row[15]if wanderer and type(row[15])is int and row[15]>=0 else None
        if joined is None and honor is None:continue
        key='world:'+str(row[0])
        archive.db.execute('INSERT OR REPLACE INTO member_metric_snapshots VALUES(?,?,?,?,?,?)',
            (archive.profile_id,key,at,joined,honor,'member_list_103:join30'+(',ranger15'if wanderer else '')))
    archive.contribution_revision+=1


def seed_roster_metrics(archive):
    import zlib
    rule='member-roster-metrics-1'
    db=archive.db;pid=archive.profile_id
    if db.execute('SELECT 1 FROM lineup_rule_migrations WHERE profile_id=? AND rule=?',(pid,rule)).fetchone():return
    for receipt in db.execute('''SELECT payload,first_seen FROM messages WHERE profile_id=? AND message_id=103
        AND first_seen>=? ORDER BY first_seen DESC LIMIT 10''',(pid,archive.retention_from)).fetchall():
        try:rows=json.loads(zlib.decompress(receipt['payload']))
        except (ValueError,TypeError,zlib.error):continue
        if not isinstance(rows,list):continue
        if rows and isinstance(rows[0],list)and rows[0]and isinstance(rows[0][0],list):rows=rows[0]
        # A retained member response is only replayed for the verified own group.
        if not any(isinstance(r,list)and r and str(r[0])==str(archive.profile['account_role_id'])for r in rows):continue
        observe_roster(archive,rows,str(archive.profile['union_id']),receipt['first_seen'])
    db.execute('INSERT INTO lineup_rule_migrations VALUES(?,?)',(pid,rule))


def _merge(archive,event):
    db=archive.db;pid=archive.profile_id
    # Merge a successful report with the immediately corresponding map change,
    # never combine two report receipts or two successive ownership changes.
    near=db.execute('''SELECT * FROM territory_events WHERE profile_id=? AND wid=? AND
        (actor_key=? OR (? IS NOT NULL AND new_owner=?))
        AND kind IN ('capture','occupy','neutral_occupation') AND ABS(event_at-?)<=30
        AND ((battle_id IS NULL AND ? IS NOT NULL) OR (battle_id IS NOT NULL AND ? IS NULL))
        ORDER BY ABS(event_at-?) LIMIT 1''',(pid,event['wid'],event.get('actor_key'),event.get('new_owner'),event.get('new_owner'),event['event_at'],
                                        event.get('battle_id'),event.get('battle_id'),event['event_at'])).fetchone()
    if near and near['old_owner'] and event.get('old_owner') and near['old_owner']!=event['old_owner']:near=None
    if near and db.execute('''SELECT 1 FROM territory_events WHERE profile_id=? AND wid=? AND id<>?
        AND event_at BETWEEN MIN(?,?) AND MAX(?,?) AND COALESCE(actor_key,'')<>? LIMIT 1''',
        (pid,event['wid'],near['id'],near['event_at'],event['event_at'],near['event_at'],event['event_at'],event.get('actor_key')or'')).fetchone():near=None
    if near:
        old=dict(near);proof=json.loads(old.pop('evidence'));proof['map_and_report']=True
        for key,value in event.items():
            if key in ('old_owner','old_union','old_player_key')and event.get('old_owner')in (None,''):continue
            if value is not None and key not in ('id','evidence','event_at','countable'):old[key]=value
        old['countable']=max(old['countable'],event['countable'])
        if event.get('battle_id'):old['event_at']=event['event_at']
        if old.get('old_owner')not in (None,'','0'):
            old['kind']='transfer' if old.get('old_union')==old.get('new_union') and old.get('old_union')not in (None,'','0') else 'capture'
        old['evidence']=packed({**proof,**json.loads(event['evidence'])});event=old
    # Reprocessing the report must preserve ownership evidence added by the map.
    saved=db.execute('SELECT * FROM territory_events WHERE id=?',(event['id'],)).fetchone()
    if saved and json.loads(saved['evidence']).get('map_and_report'):
        existing=dict(saved)
        if event.get('level')is not None:existing['level']=event['level']
        event=existing
    columns=[r[1]for r in db.execute('PRAGMA table_info(territory_events)')]
    db.execute('INSERT INTO territory_events VALUES('+','.join('?'for _ in columns)+') ON CONFLICT(id) DO UPDATE SET '+
               ','.join(c+'=excluded.'+c for c in columns if c!='id'),[event.get(c)for c in columns])
    archive.contribution_revision+=1


def observe_tile(archive,wid,raw,at,ref):
    db,pid=archive.db,archive.profile_id;wid=int(wid)
    if at<archive.retention_from:return
    old=db.execute('SELECT * FROM territory_current WHERE profile_id=? AND wid=?',(pid,wid)).fetchone()
    owner,union=str(raw[2]),str(raw[3]);city=int(raw[0])
    if old and at<=old['observed_at']:return
    if old and old['owner_id']!=owner:
        fresh=old['session']==archive.territory_session and 0<=at-old['observed_at']<=120
        actor=player(archive,owner) if owner not in ('','0') else dict(key=None,name='',union=union)
        previous=player(archive,old['owner_id'])if old['owner_id']not in (None,'','0')else dict(key=None)
        kind=('ownership_gap'if not fresh else 'abandoned'if owner in ('','0') else
              'neutral_occupation'if old['owner_id']in (None,'','0') else
              'transfer'if union==old['union_id']and union not in ('','0') else 'capture')
        _merge(archive,dict(id=digest([pid,wid,old['message_ref'],ref,owner]),profile_id=pid,wid=wid,kind=kind,event_at=at,
            observed_from=old['observed_at'],observed_to=at,actor_key=actor['key'],actor_name=actor['name'],actor_union=union,
            old_player_key=previous['key'],old_owner=old['owner_id'],old_union=old['union_id'],new_owner=owner,new_union=union,
            level=None,countable=int(fresh),battle_id=None,evidence=packed(dict(source='map_ownership',message_ref=ref,time_precision='interval'))))
    if old and old['city_type']in (4,12) and city not in (4,12):
        _merge(archive,dict(id=digest([pid,wid,'building_change',old['message_ref'],ref]),profile_id=pid,wid=wid,
            kind='building_change',event_at=at,observed_from=old['observed_at'],observed_to=at,
            old_owner=old['owner_id'],old_union=old['union_id'],new_owner=owner,new_union=union,countable=0,
            evidence=packed(dict(source='map_building',message_ref=ref,reason='拆毁、自拆或类型变化尚不能区分'))))
    db.execute('INSERT OR REPLACE INTO territory_current VALUES(?,?,?,?,?,?,?,?)',
               (pid,wid,owner,union,city,at,archive.territory_session,ref))


def index_report(archive,battle):
    """Verified client ReportResult 2/3: personal/alliance occupied, not victory 1."""
    pid=archive.profile_id;bid=str(battle['battle_id']);at=battle.get('time',0);wid=battle.get('wid')
    # A corrected result retracts this report-derived event, not unrelated map facts.
    if battle.get('result')not in (2,3):
        archive.db.execute("DELETE FROM territory_events WHERE profile_id=? AND battle_id=? AND json_extract(evidence,'$.source')='occupation_report' AND COALESCE(json_extract(evidence,'$.map_and_report'),0)=0",(pid,bid));return
    if not wid or not at or at<archive.retention_from or report_system_npc(battle,'attack'):return
    role=str(battle.get('attack_role_id')or'');name=str(battle.get('attack_name')or'')
    if role in ('','0')or not name:return
    actor='battle:'+role;union=str(battle.get('attack_unionid')or'0')
    old=archive.db.execute('''SELECT * FROM territory_current WHERE profile_id=? AND wid=?
        AND observed_at<=? AND observed_at>=?''',(pid,wid,at,at-120)).fetchone()
    if not old:
        old=archive.db.execute('''SELECT owner_uid owner_id,union_id,observed_at FROM tiles
            WHERE profile_id=? AND wid=? AND observed_at<=? AND observed_at>=?
            ORDER BY observed_at DESC LIMIT 1''',(pid,wid,at,at-120)).fetchone()
    owner=archive.db.execute('SELECT world_uid FROM players WHERE profile_id=? AND role_id=? AND world_uid IS NOT NULL LIMIT 2',(pid,role)).fetchall()
    new_owner=owner[0][0]if len(owner)==1 else None
    old_owner=old['owner_id']if old and old['owner_id']!=new_owner else None
    old_player=player(archive,old_owner)['key']if old_owner not in (None,'','0')else None
    kind=('neutral_occupation'if old_owner=='0'else 'transfer'if old_owner and old and old['union_id']==union else 'capture'if old_owner else 'occupy')
    # Resource-land city type 2 and its explicit official label were verified.
    level_match=re.fullmatch(r'土地\s*Lv\.?\s*([1-9])',str(battle.get('wid_name')or''),re.I)
    level=int(level_match[1])if level_match and battle.get('city_type')==2 else None
    _merge(archive,dict(id=digest([pid,'occupation',wid,at,actor]),profile_id=pid,wid=wid,kind=kind,event_at=at,
        observed_from=at,observed_to=at,actor_key=actor,actor_name=name,actor_union=union,
        old_player_key=old_player,old_owner=old_owner,old_union=old['union_id']if old else None,
        new_owner=new_owner,new_union=union,level=level,countable=1,battle_id=bid,
        evidence=packed(dict(source='occupation_report',result=battle['result'],level_source='official_land_label'if level else None))))


def query(archive,q):
    start=max(float(q.get('from_at')or 0),archive.retention_from);end=float(q.get('to_at')or time.time())
    where=['profile_id=?','event_at>=?','event_at<=?'];args=[archive.profile_id,start,end]
    if q.get('player_key'):
        aliases=[q['player_key']]
        if q['player_key'].startswith('battle:'):
            aliases.extend('world:'+r[0]for r in archive.db.execute('SELECT world_uid FROM players WHERE profile_id=? AND role_id=? AND world_uid IS NOT NULL',(archive.profile_id,q['player_key'][7:])))
        slots=','.join('?'for _ in aliases);where.append(f'(actor_key IN ({slots}) OR old_player_key IN ({slots}))');args.extend(aliases*2)
    if q.get('union_id'):where.append('(actor_union=? OR old_union=?)');args.extend([q['union_id']]*2)
    if q.get('player'):
        where.append("""(instr(lower(actor_name),lower(?))>0 OR (old_player_key IS NOT NULL AND EXISTS(
            SELECT 1 FROM players p WHERE p.profile_id=territory_events.profile_id
            AND (p.player_key=territory_events.old_player_key OR 'battle:'||p.role_id=territory_events.old_player_key)
            AND instr(lower(p.name),lower(?))>0)))""")
        args.extend([q['player']]*2)
    if q.get('official_campaign_id'):
        campaign=archive.intelligence.official_campaigns().get(str(q['official_campaign_id']))
        if not campaign:raise ValueError('官方战役尚未取得')
        blocks=campaign['blocks']
        where.append('((CAST((CAST(wid/10000 AS INTEGER)-1)/10 AS INTEGER)+1)*1000+CAST((wid%10000-1)/10 AS INTEGER)+1) IN ('+','.join('?'for _ in blocks)+')')
        args.extend(blocks);where.append('event_at>=?');args.append(campaign['filters']['from_at'])
        if campaign['filters']['to_at']:where.append('event_at<=?');args.append(campaign['filters']['to_at'])
    if q.get('coord'):
        xy=re.split(r'[,，\s]+',q['coord'].strip())
        if len(xy)!=2 or not all(s.isdecimal()for s in xy):raise ValueError('坐标格式无效')
        where.append('wid=?');args.append(int(xy[0])*10000+int(xy[1]))
    limit=max(1,min(int(q.get('limit')or 100),500));offset=max(0,int(q.get('offset')or 0))
    rows=[dict(r)for r in archive.db.execute('SELECT * FROM territory_events WHERE '+' AND '.join(where)+' ORDER BY event_at DESC,id LIMIT ? OFFSET ?',(*args,limit+1,offset))]
    for row in rows:row['evidence']=json.loads(row['evidence'])
    return dict(items=rows[:limit],has_more=len(rows)>limit,capabilities=dict(occupation=True,demolition=False,durability_snapshot=True,player_siege_damage=False),
                coverage='仅已接收的占领结果和已观测归属变化；跨采集间断不推算次数')


def player_stats(archive,union,start,end):
    result={}
    for row in archive.db.execute('''SELECT * FROM territory_events WHERE profile_id=? AND countable=1
        AND event_at>=? AND event_at<? AND (actor_union=? OR old_union=?)''',
        (archive.profile_id,start,end,union,union)):
        row=dict(row);row['actor_key']=normalized_key(archive,row['actor_key']);row['old_player_key']=normalized_key(archive,row['old_player_key'])
        if row['actor_union']==union and row['actor_key']:
            stats=result.setdefault(row['actor_key'],dict(capture_low=0,capture_high=0,capture_unknown=0,lost_land=0))
            if row['kind']=='capture':
                key='capture_low'if row['level']and row['level']<=4 else 'capture_high'if row['level']else'capture_unknown'
                stats[key]+=1
            elif row['kind']=='occupy':stats['capture_unknown']+=1
        if row['old_union']==union and row['old_player_key']and row['kind']=='capture':
            result.setdefault(row['old_player_key'],dict(capture_low=0,capture_high=0,capture_unknown=0,lost_land=0))['lost_land']+=1
    return result
