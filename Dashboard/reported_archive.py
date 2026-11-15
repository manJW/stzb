"""Keep unreported armies in connection-local MEMORY tables, not season history."""
import json
import sqlite3
import time
import zlib

from observation_codec import packed, report_system_npc

TABLES=('army_states','army_current','march_samples','lineup_forecasts','army_report_refs','own_report_indices')
SCHEMA='''
CREATE TABLE IF NOT EXISTS team_archive_policy(profile_id TEXT PRIMARY KEY,reported_only INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS reported_armies(
 profile_id TEXT NOT NULL,army_id TEXT NOT NULL,battle_id TEXT NOT NULL,established_at REAL NOT NULL,
 PRIMARY KEY(profile_id,army_id));
CREATE TABLE IF NOT EXISTS message_redactions(
 profile_id TEXT NOT NULL,message_id TEXT NOT NULL,reason TEXT NOT NULL,redacted_at REAL NOT NULL,
 PRIMARY KEY(profile_id,message_id));
'''


def install_live(db, values=None):
    db.execute('PRAGMA temp_store=MEMORY')
    for table in TABLES:
        definition=db.execute("SELECT sql FROM main.sqlite_master WHERE type='table' AND name=?",(table,)).fetchone()[0]
        db.execute('CREATE TEMP TABLE IF NOT EXISTS pending_'+table+definition[definition.index('('):])
        keys=[r[1]for r in db.execute('PRAGMA main.table_info('+table+')')if r[5]]
        match=' AND '.join('saved.'+key+'=pending.'+key for key in keys)
        db.execute('CREATE TEMP VIEW IF NOT EXISTS live_'+table+' AS SELECT * FROM main.'+table+
                   ' UNION ALL SELECT pending.* FROM temp.pending_'+table+' pending WHERE NOT EXISTS('
                   'SELECT 1 FROM main.'+table+' saved WHERE '+match+')')
        rows=(values or {}).get(table,[])
        if rows:
            db.executemany('INSERT INTO pending_'+table+' VALUES('+','.join('?'for _ in rows[0])+')',rows)
    db.execute('CREATE INDEX IF NOT EXISTS temp.pending_state_army ON pending_army_states(profile_id,army_id,observed_at)')
    db.execute('CREATE INDEX IF NOT EXISTS temp.pending_arrival ON pending_march_samples(profile_id,state_code,to_x,to_y,end_at)')
    db.execute('CREATE INDEX IF NOT EXISTS temp.pending_army_march ON pending_march_samples(profile_id,army_id,end_at)')
    db.execute('CREATE INDEX IF NOT EXISTS temp.pending_report_owner ON pending_army_report_refs(profile_id,battle_id,owner_name,union_id)')


def live_snapshot(db):
    return {table:[tuple(r)for r in db.execute('SELECT * FROM pending_'+table)]for table in TABLES}


def enabled(db,profile):
    try:
        row=db.execute('SELECT reported_only FROM main.team_archive_policy WHERE profile_id=?',(profile,)).fetchone()
        return bool(row and row[0])
    except sqlite3.OperationalError:
        return False


def live_table(db,table):
    return 'live_'+table if db.execute("SELECT 1 FROM sqlite_temp_master WHERE type='view' AND name=?",('live_'+table,)).fetchone() else table


def admitted(db,profile,army):
    return not enabled(db,profile) or bool(db.execute(
        'SELECT 1 FROM main.reported_armies WHERE profile_id=? AND army_id=?',(profile,str(army))).fetchone())


def storage(db,profile,army,table):
    assert table in TABLES
    return table if admitted(db,profile,army) else 'pending_'+table


def promote(db,profile,army,battle,at):
    army,battle=str(army),str(battle)
    if not enabled(db,profile) or not army or not db.execute(
            'SELECT 1 FROM battle_reports WHERE profile_id=? AND battle_id=?',(profile,battle)).fetchone():
        return False
    inserted=db.execute('INSERT OR IGNORE INTO reported_armies VALUES(?,?,?,?)',(profile,army,battle,at)).rowcount
    for table in TABLES:
        if table=='army_current':
            db.execute('''INSERT INTO main.army_current SELECT * FROM pending_army_current
                WHERE profile_id=? AND army_id=? ON CONFLICT(profile_id,army_id) DO UPDATE
                SET state_ref=excluded.state_ref,observed_at=excluded.observed_at
                WHERE excluded.observed_at>=army_current.observed_at''',(profile,army))
        else:
            db.execute('INSERT OR IGNORE INTO main.'+table+' SELECT * FROM pending_'+table+
                       ' WHERE profile_id=? AND army_id=?',(profile,army))
        db.execute('DELETE FROM pending_'+table+' WHERE profile_id=? AND army_id=?',(profile,army))
    return bool(inserted)


def trim(db,profile,at):
    # Volatile evidence supports delayed headers, but cannot grow into a history.
    cutoff=at-3600
    for table,stamp in (('army_current','observed_at'),('army_states','observed_at'),('march_samples','observed_at'),
                        ('lineup_forecasts','issued_at'),('army_report_refs','observed_at'),('own_report_indices','observed_at')):
        keep_current=' AND NOT EXISTS(SELECT 1 FROM pending_army_current c WHERE c.state_ref=pending_army_states.id)' if table=='army_states' else ''
        db.execute('DELETE FROM pending_'+table+' WHERE profile_id=? AND '+stamp+'<?'+keep_current,(profile,cutoff))
    for table,stamp,maximum in (('army_states','observed_at',6),('march_samples','observed_at',4),('lineup_forecasts','issued_at',6)):
        keep_current=' AND NOT EXISTS(SELECT 1 FROM pending_army_current c WHERE c.state_ref=pending_army_states.id)' if table=='army_states' else ''
        db.execute('DELETE FROM pending_'+table+''' WHERE rowid IN (SELECT rowid FROM (
            SELECT rowid,ROW_NUMBER() OVER(PARTITION BY army_id ORDER BY '''+stamp+''' DESC,rowid DESC) n
            FROM pending_'''+table+' WHERE profile_id=?) WHERE n>?)'+keep_current,(profile,maximum))


def known_ids(db,profile,ids):
    if not enabled(db,profile):return set(map(str,ids))
    result=set();ids=list(map(str,ids))
    for start in range(0,len(ids),400):
        batch=ids[start:start+400]
        result.update(r[0]for r in db.execute('SELECT army_id FROM reported_armies WHERE profile_id=? AND army_id IN ('+
                                             ','.join('?'for _ in batch)+')',(profile,*batch)))
    return result


def filter_message(db,profile,mid,value):
    if not enabled(db,profile):return value
    if mid in (5026,5028) and isinstance(value,list) and len(value)==34:
        result=list(value)
        if isinstance(value[6],dict):
            keep=known_ids(db,profile,value[6]);result[6]={k:v for k,v in value[6].items()if str(k)in keep}
        if isinstance(value[7],list):
            keep=known_ids(db,profile,value[7]);result[7]=[v for v in value[7]if str(v)in keep]
        return result
    if mid==90005:
        def visit(v):
            if isinstance(v,list):
                if len(v)==3 and isinstance(v[1],str) and v[1]in ('Tb_army','Tb_army_alert') and isinstance(v[2],list) and v[2]:
                    body=v[2];key=body[1]if v[0]==2 and len(body)>1 and body[0]==0 else body[0]
                    return v if admitted(db,profile,key) else None
                return [child for item in v if (child:=visit(item))is not None]
            if isinstance(v,dict):return {k:child for k,item in v.items()if(child:=visit(item))is not None}
            return v
        return visit(value)
    return value


def seed_admissions(db,profile):
    """Only actual report associations qualify; same-player candidates do not."""
    valid=set();proofs={}
    for row in db.execute('''SELECT battle_id,side,player_name,union_id,army_id,event_at,npc,
        CASE WHEN npc<>0 OR CAST(json_extract(features,'$.'||side||'er_force') AS INTEGER)=1000001 THEN features END features
        FROM combat_sides c WHERE profile_id=? AND EXISTS(SELECT 1 FROM battle_reports b
        WHERE b.profile_id=c.profile_id AND b.battle_id=c.battle_id)''',(profile,)):
        if row['features'] and report_system_npc({**json.loads(row['features']),'npc':row['npc']},row['side']):continue
        valid.add((row['battle_id'],row['side'],row['player_name'],row['union_id']))
        if row['army_id']:proofs.setdefault(row['army_id'],(row['battle_id'],row['event_at']))
    for row in db.execute('''SELECT m.army_id,l.battle_id,l.side,c.player_name,c.union_id,c.event_at
        FROM battle_march_links l JOIN march_samples m ON m.profile_id=l.profile_id AND m.id=l.march_id
        JOIN combat_sides c ON c.profile_id=l.profile_id AND c.battle_id=l.battle_id AND c.side=l.side
        WHERE l.profile_id=? AND l.method IN ('coordinate_time_match','unique_attack_arrival')''',(profile,)):
        if (row['battle_id'],row['side'],row['player_name'],row['union_id'])in valid:
            proofs.setdefault(row['army_id'],(row['battle_id'],row['event_at']))
    for row in db.execute('''SELECT substr(team_key,6) army_id,battle_id,player_name,union_id,event_at
        FROM team_versions v WHERE profile_id=? AND binding='exact_army_id' AND team_key LIKE 'army:%'
        AND COALESCE(json_extract(data,'$.binding_conflict'),0)=0''',(profile,)):
        if any((row['battle_id'],side,row['player_name'],row['union_id'])in valid for side in ('attack','defend')):
            proofs.setdefault(row['army_id'],(row['battle_id'],row['event_at']))
    for row in db.execute('SELECT army_id,report_id,player_name,union_id,computed_at FROM army_lineup_projection WHERE profile_id=? AND resolved=1',(profile,)):
        if any((row['report_id'],side,row['player_name'],row['union_id'])in valid for side in ('attack','defend')):
            proofs.setdefault(row['army_id'],(row['report_id'],row['computed_at']))
    db.executemany('INSERT OR IGNORE INTO reported_armies VALUES(?,?,?,?)',[(profile,army,*proof)for army,proof in proofs.items()])
    return len(proofs)


def prune_history(db,profile,*,redact=True):
    """Offline maintenance: caller must back up first. Never delete real reports."""
    fields={'army_current':'army_id','army_states':'army_id','march_samples':'army_id',
            'lineup_forecasts':'army_id','army_lineup_projection':'army_id','lineup_army_dirty':'army_id',
            'army_report_refs':'army_id','own_report_indices':'army_id'}
    result={}
    for table,column in fields.items():
        result[table]=db.execute('DELETE FROM '+table+''' WHERE profile_id=? AND NOT EXISTS(
            SELECT 1 FROM reported_armies r WHERE r.profile_id='''+table+'.profile_id AND r.army_id='+table+'.'+column+')',(profile,)).rowcount
    result['army_observations']=db.execute('''DELETE FROM observations WHERE profile_id=? AND kind IN ('army_state','out_of_view')
        AND NOT EXISTS(SELECT 1 FROM reported_armies r WHERE r.profile_id=observations.profile_id AND r.army_id=observations.entity_key)''',(profile,)).rowcount
    result['client_army_events']=db.execute('''DELETE FROM client_table_events WHERE profile_id=? AND table_name IN ('Tb_army','Tb_army_alert')
        AND NOT EXISTS(SELECT 1 FROM reported_armies r WHERE r.profile_id=client_table_events.profile_id AND r.army_id=client_table_events.record_key)''',(profile,)).rowcount
    result['orphan_versions']=db.execute('''DELETE FROM team_versions WHERE profile_id=? AND NOT EXISTS(
        SELECT 1 FROM battle_reports b WHERE b.profile_id=team_versions.profile_id AND b.battle_id=team_versions.battle_id)''',(profile,)).rowcount
    db.execute('DELETE FROM team_current WHERE profile_id=? AND NOT EXISTS(SELECT 1 FROM team_versions v WHERE v.id=team_current.version_ref)',(profile,))
    db.execute('DELETE FROM battle_march_links WHERE profile_id=? AND NOT EXISTS(SELECT 1 FROM march_samples m WHERE m.id=battle_march_links.march_id)',(profile,))
    db.execute('DELETE FROM source_sightings WHERE profile_id=? AND kind=\'march\' AND NOT EXISTS(SELECT 1 FROM march_samples m WHERE m.profile_id=source_sightings.profile_id AND m.id=source_sightings.record_id)',(profile,))
    db.execute('UPDATE army_report_refs SET state_ref=NULL WHERE profile_id=? AND state_ref IS NOT NULL AND NOT EXISTS(SELECT 1 FROM army_states s WHERE s.id=army_report_refs.state_ref)',(profile,))
    redactions=0
    if redact:
        cursor=db.execute('SELECT id,message_id,payload FROM messages WHERE profile_id=? AND message_id IN (5026,5028,90005)',(profile,))
        while True:
            batch=cursor.fetchmany(100)
            if not batch:break
            for row in batch:
                original=json.loads(zlib.decompress(row['payload']));filtered=filter_message(db,profile,row['message_id'],original)
                if filtered==original:continue
                # Keep receipt keys stable for retained rows; explicitly record that
                # this is a filtered archival envelope, not the original packet.
                db.execute('UPDATE messages SET payload=? WHERE id=?',(zlib.compress(packed(filtered).encode('utf8')),row['id']))
                db.execute('INSERT OR REPLACE INTO message_redactions VALUES(?,?,?,?)',(profile,row['id'],'unreported_armies',time.time()))
                redactions+=1
    result['filtered_messages']=redactions
    return result
