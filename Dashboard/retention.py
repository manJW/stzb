"""Profile-scoped collection floors and offline, backed-up history maintenance."""
import json
import sqlite3
import time
import zlib

SCHEMA="""
CREATE TABLE IF NOT EXISTS retention_policy(profile_id TEXT PRIMARY KEY,retain_from REAL NOT NULL,updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS retention_ignored_ids(profile_id TEXT NOT NULL,battle_id TEXT NOT NULL,PRIMARY KEY(profile_id,battle_id));
CREATE INDEX IF NOT EXISTS battle_profile_time ON battle_reports(profile_id,event_at,battle_id);
"""


def floor(archive):
    row=archive.db.execute('SELECT retain_from FROM retention_policy WHERE profile_id=?',(archive.profile_id,)).fetchone()
    return row[0] if row else 0


def ignored(archive,bid):
    return bool(archive.db.execute('SELECT 1 FROM retention_ignored_ids WHERE profile_id=? AND battle_id=?',(archive.profile_id,str(bid))).fetchone())


def accepts(archive,record):
    cutoff=floor(archive)
    if not cutoff:return True
    bid=str(record.get('battle_id',''))
    if not bid or ignored(archive,bid):return False
    stamp=record.get('time')
    if isinstance(stamp,(int,float)) and stamp>0:return stamp>=cutoff
    known=archive.db.execute('SELECT event_at FROM battle_reports WHERE profile_id=? AND battle_id=?',(archive.profile_id,bid)).fetchone()
    return bool(known and known[0]>=cutoff)


def sanitize(archive,value,_cutoff=None):
    """Filter before hashing/storing raw messages, not only before projecting them."""
    cutoff=floor(archive) if _cutoff is None else _cutoff
    if not cutoff:return value
    if isinstance(value,dict):
        if value.get('battle_id') and not accepts(archive,value):return None
        return {k:sanitize(archive,v,cutoff) for k,v in value.items()}
    if isinstance(value,list):
        # Client-table report snapshots may contain old indices with no timestamp.
        if len(value)==3 and isinstance(value[1],str) and value[1] in ('Tb_battle_report_attack','Tb_battle_report_defend'):
            body=value[2]
            if isinstance(body,list) and body:
                bid=body[1] if value[0]==2 and len(body)>1 and body[0]==0 else body[0]
                if ignored(archive,bid):return None
        result=[]
        for child in value:
            item=sanitize(archive,child,cutoff)
            if item is None and isinstance(child,(dict,list)):continue
            result.append(item)
        return result
    return value


def prune(db,profile,cutoff,*,rewrite_messages=True):
    """Caller must stop writers and create a consistent backup before committing."""
    db.row_factory=sqlite3.Row;db.executescript(SCHEMA)
    tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    before={t:db.execute(f'SELECT COUNT(*) FROM {t} WHERE profile_id=?',(profile,)).fetchone()[0]
            for t in tables if any(c[1]=='profile_id' for c in db.execute(f'PRAGMA table_info({t})'))}
    db.execute('INSERT OR REPLACE INTO retention_policy VALUES(?,?,?)',(profile,cutoff,time.time()))
    db.execute('INSERT OR IGNORE INTO retention_ignored_ids SELECT profile_id,battle_id FROM battle_reports WHERE profile_id=? AND event_at<?',(profile,cutoff))
    db.execute('DELETE FROM battle_reports WHERE profile_id=? AND event_at<?',(profile,cutoff))
    related=('report_bodies','report_briefs','team_versions','combat_sides','army_report_refs','battle_march_links',
             'same_army_audits','report_read_jobs','own_report_indices','report_collection_state','attendance_facts','attendance_dirty','formation_facts')
    for table in related:
        if table in tables:
            db.execute(f"DELETE FROM {table} WHERE profile_id=? AND NOT EXISTS(SELECT 1 FROM battle_reports b WHERE b.profile_id={table}.profile_id AND b.battle_id={table}.battle_id)",(profile,))
    for table,column in (('territory_events','event_at'),('territory_current','observed_at'),('member_metric_snapshots','observed_at'),('durability_snapshots','event_at')):
        if table in tables:db.execute(f'DELETE FROM {table} WHERE profile_id=? AND {column}<?',(profile,cutoff))
    if 'formation_configs'in tables:
        db.execute('DELETE FROM formation_configs WHERE profile_id=? AND NOT EXISTS(SELECT 1 FROM formation_facts f WHERE f.profile_id=formation_configs.profile_id AND f.config_id=formation_configs.config_id)',(profile,))
        db.execute('DELETE FROM formation_teams WHERE profile_id=? AND NOT EXISTS(SELECT 1 FROM formation_facts f WHERE f.profile_id=formation_teams.profile_id AND f.formation_id=formation_teams.formation_id)',(profile,))
    for table,condition in (('army_states','observed_at<?'),('march_samples','begin_at<?'),('tiles','observed_at<?'),
                             ('observations','COALESCE(event_at,observed_at)<?'),('client_table_events','observed_at<?'),
                             ('lineup_forecasts','issued_at<?')):
        db.execute(f'DELETE FROM {table} WHERE profile_id=? AND {condition}',(profile,cutoff))
    db.execute('DELETE FROM team_versions WHERE profile_id=? AND event_at<?',(profile,cutoff))
    db.execute('DELETE FROM team_current WHERE profile_id=?',(profile,))
    db.execute("""INSERT INTO team_current SELECT profile_id,team_key,id,event_at,observed_at FROM
      (SELECT *,ROW_NUMBER() OVER(PARTITION BY team_key ORDER BY event_at DESC,observed_at DESC,id DESC) n
       FROM team_versions WHERE profile_id=? AND COALESCE(json_extract(data,'$.binding_conflict'),0)=0) WHERE n=1""",(profile,))
    db.execute('DELETE FROM army_current WHERE profile_id=? AND NOT EXISTS(SELECT 1 FROM army_states s WHERE s.id=army_current.state_ref)',(profile,))
    db.execute('DELETE FROM battle_march_links WHERE profile_id=? AND NOT EXISTS(SELECT 1 FROM march_samples m WHERE m.id=battle_march_links.march_id)',(profile,))
    db.execute('UPDATE army_report_refs SET state_ref=NULL WHERE profile_id=? AND state_ref IS NOT NULL AND NOT EXISTS(SELECT 1 FROM army_states s WHERE s.id=army_report_refs.state_ref)',(profile,))
    for table in ('army_lineup_projection','lineup_army_dirty','same_army_audits'):
        if table in tables:db.execute(f'DELETE FROM {table} WHERE profile_id=?',(profile,))
    if 'lineup_player_revisions' in tables:db.execute('UPDATE lineup_player_revisions SET dirty=1,revision=revision+1 WHERE profile_id=?',(profile,))
    db.execute('DELETE FROM messages WHERE profile_id=? AND first_seen<?',(profile,cutoff))
    # Mixed post-cutoff responses can contain pre-cutoff historical pages.
    # Remove those raw envelopes rather than silently altering immutable evidence.
    old_ids={r[0] for r in db.execute('SELECT battle_id FROM retention_ignored_ids WHERE profile_id=?',(profile,))}
    def contains_old(value):
        if isinstance(value,dict):
            if value.get('battle_id') and (str(value['battle_id']) in old_ids or isinstance(value.get('time'),(int,float)) and value['time']<cutoff):return True
            return any(contains_old(v) for v in value.values())
        if isinstance(value,list):
            if len(value)==3 and isinstance(value[1],str) and value[1].startswith('Tb_battle_report') and isinstance(value[2],list) and value[2]:
                body=value[2];bid=body[1] if value[0]==2 and len(body)>1 and body[0]==0 else body[0]
                if str(bid) in old_ids:return True
            return any(contains_old(v) for v in value)
        return False
    raw_deleted=0
    if rewrite_messages:
        cursor=db.execute('SELECT id,payload FROM messages WHERE profile_id=?',(profile,))
        while True:
            batch=cursor.fetchmany(500)
            if not batch:break
            bad=[]
            for r in batch:
                try:bad.append((r['id'],)) if contains_old(json.loads(zlib.decompress(r['payload']))) else None
                except (ValueError,zlib.error):bad.append((r['id'],))
            db.executemany('DELETE FROM messages WHERE id=?',bad);raw_deleted+=len(bad)
    for table in tables:
        cols={c[1] for c in db.execute(f'PRAGMA table_info({table})')}
        if {'profile_id','message_ref'}<=cols:
            db.execute(f'UPDATE {table} SET message_ref=NULL WHERE profile_id=? AND message_ref IS NOT NULL AND NOT EXISTS(SELECT 1 FROM messages m WHERE m.id={table}.message_ref)',(profile,))
    for kind,table,column in (('report','battle_reports','battle_id'),('body','report_bodies','battle_id'),('brief','report_briefs','battle_id'),('march','march_samples','id')):
        db.execute(f'DELETE FROM source_sightings WHERE profile_id=? AND kind=? AND NOT EXISTS(SELECT 1 FROM {table} r WHERE r.profile_id=source_sightings.profile_id AND r.{column}=source_sightings.record_id)',(profile,kind))
    after={t:db.execute(f'SELECT COUNT(*) FROM {t} WHERE profile_id=?',(profile,)).fetchone()[0] for t in before}
    return {'profile_id':profile,'retain_from':cutoff,'before':before,'after':after,'mixed_raw_removed':raw_deleted}
