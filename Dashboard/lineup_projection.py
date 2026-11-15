"""Versioned current-army projections. Original evidence and replay stay uncached."""
import copy
import json
import logging
import threading
import time
from collections import OrderedDict


VERSION = '1'
VERIFIED = {'exact_army_id', 'matched_arrival', 'same_card_instances'}


def install(db, profile):
    db.executescript('''
    CREATE TABLE IF NOT EXISTS lineup_projection_meta(profile_id TEXT PRIMARY KEY,version TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS lineup_rule_migrations(
      profile_id TEXT NOT NULL,rule TEXT NOT NULL,PRIMARY KEY(profile_id,rule));
    CREATE TABLE IF NOT EXISTS lineup_player_revisions(
      profile_id TEXT NOT NULL,union_id TEXT NOT NULL,player_name TEXT NOT NULL,
      revision INTEGER NOT NULL,dirty INTEGER NOT NULL DEFAULT 1,changed_at REAL NOT NULL,
      PRIMARY KEY(profile_id,union_id,player_name));
    CREATE INDEX IF NOT EXISTS lineup_dirty_players ON lineup_player_revisions(profile_id,changed_at) WHERE dirty=1;
    CREATE TABLE IF NOT EXISTS lineup_army_dirty(
      profile_id TEXT NOT NULL,army_id TEXT NOT NULL,PRIMARY KEY(profile_id,army_id));
    CREATE TABLE IF NOT EXISTS army_lineup_projection(
      profile_id TEXT NOT NULL,army_id TEXT NOT NULL,state_ref TEXT NOT NULL,owner_uid TEXT,
      union_id TEXT NOT NULL,player_name TEXT NOT NULL,revision INTEGER NOT NULL,
      version TEXT NOT NULL,valid_until REAL NOT NULL,computed_at REAL NOT NULL,
      resolved INTEGER NOT NULL,report_id TEXT,payload TEXT NOT NULL,
      PRIMARY KEY(profile_id,army_id));
    CREATE TABLE IF NOT EXISTS report_collection_state(
      profile_id TEXT NOT NULL,battle_id TEXT NOT NULL,received INTEGER NOT NULL DEFAULT 0,
      has_full INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(profile_id,battle_id));
    CREATE INDEX IF NOT EXISTS lineup_projection_expiry ON army_lineup_projection(profile_id,valid_until);
    CREATE INDEX IF NOT EXISTS players_name_scope ON players(profile_id,union_id,name,world_uid);
    ''')

    def bump(prefix):
        return f'''INSERT INTO lineup_player_revisions VALUES(
          {prefix}.profile_id,COALESCE({prefix}.union_id,'0'),COALESCE({prefix}.player_name,''),1,1,
          CAST(strftime('%s','now') AS REAL)) ON CONFLICT(profile_id,union_id,player_name)
          DO UPDATE SET revision=revision+1,dirty=1,changed_at=excluded.changed_at;'''

    # Revision changes commit atomically with evidence. Readers of an older WAL
    # snapshot can cache that revision without poisoning a newer snapshot.
    for table, fields in (
        ('combat_sides', ('player_key','player_name','union_id','army_id','event_at','npc','hero_key','heroes','features')),
        ('team_versions', ('player_key','player_name','union_id','team_key','event_at','data')),
    ):
        changed = ' OR '.join(f'OLD.{field} IS NOT NEW.{field}' for field in fields)
        db.executescript(f'''
        CREATE TRIGGER IF NOT EXISTS projection_{table}_insert AFTER INSERT ON {table}
        BEGIN {bump('NEW')} END;
        CREATE TRIGGER IF NOT EXISTS projection_{table}_update AFTER UPDATE ON {table} WHEN {changed}
        BEGIN {bump('OLD')} {bump('NEW')} END;
        CREATE TRIGGER IF NOT EXISTS projection_{table}_delete AFTER DELETE ON {table}
        BEGIN {bump('OLD')} END;''')
    for action, prefix in (('INSERT','NEW'), ('DELETE','OLD')):
        db.executescript(f'''
        CREATE TRIGGER IF NOT EXISTS projection_links_{action.lower()} AFTER {action} ON battle_march_links
        BEGIN
          INSERT INTO lineup_player_revisions
          SELECT profile_id,union_id,player_name,1,1,CAST(strftime('%s','now') AS REAL)
          FROM combat_sides WHERE profile_id={prefix}.profile_id AND battle_id={prefix}.battle_id AND side={prefix}.side
          ON CONFLICT(profile_id,union_id,player_name) DO UPDATE SET revision=revision+1,dirty=1,changed_at=excluded.changed_at;
        END;''')
        db.executescript(f'''
        CREATE TRIGGER IF NOT EXISTS projection_team_current_{action.lower()} AFTER {action} ON team_current
        BEGIN
          INSERT INTO lineup_player_revisions
          SELECT profile_id,union_id,player_name,1,1,CAST(strftime('%s','now') AS REAL)
          FROM team_versions WHERE id={prefix}.version_ref
          ON CONFLICT(profile_id,union_id,player_name) DO UPDATE SET revision=revision+1,dirty=1,changed_at=excluded.changed_at;
        END;''')
    for action, condition in (('INSERT',''),('UPDATE','WHEN OLD.observed_at IS NOT NEW.observed_at')):
        db.executescript(f'''
        CREATE TRIGGER IF NOT EXISTS projection_march_{action.lower()} AFTER {action} ON march_samples {condition}
        BEGIN
          INSERT INTO lineup_player_revisions
          SELECT DISTINCT s.profile_id,p.union_id,p.name,1,1,CAST(strftime('%s','now') AS REAL)
          FROM army_states s JOIN players p ON p.profile_id=s.profile_id AND p.player_key='world:'||s.owner_uid
          WHERE s.profile_id=NEW.profile_id AND s.army_id=NEW.army_id AND s.message_ref=NEW.message_ref
          AND s.begin_at=NEW.begin_at AND s.end_at=NEW.end_at AND s.from_x=NEW.from_x AND s.from_y=NEW.from_y
          AND s.to_x=NEW.to_x AND s.to_y=NEW.to_y
          ON CONFLICT(profile_id,union_id,player_name) DO UPDATE SET revision=revision+1,dirty=1,changed_at=excluded.changed_at;
        END;''')
    db.executescript('''
    CREATE TRIGGER IF NOT EXISTS projection_team_current_update AFTER UPDATE ON team_current WHEN OLD.version_ref IS NOT NEW.version_ref
    BEGIN
      INSERT INTO lineup_player_revisions
      SELECT profile_id,union_id,player_name,1,1,CAST(strftime('%s','now') AS REAL)
      FROM team_versions WHERE id=OLD.version_ref OR id=NEW.version_ref
      ON CONFLICT(profile_id,union_id,player_name) DO UPDATE SET revision=revision+1,dirty=1,changed_at=excluded.changed_at;
    END;
    CREATE TRIGGER IF NOT EXISTS projection_links_update AFTER UPDATE ON battle_march_links
      WHEN OLD.march_id IS NOT NEW.march_id OR OLD.method IS NOT NEW.method
    BEGIN
      INSERT INTO lineup_player_revisions
      SELECT profile_id,union_id,player_name,1,1,CAST(strftime('%s','now') AS REAL)
      FROM combat_sides WHERE profile_id=NEW.profile_id AND battle_id=NEW.battle_id AND side=NEW.side
      ON CONFLICT(profile_id,union_id,player_name) DO UPDATE SET revision=revision+1,dirty=1,changed_at=excluded.changed_at;
    END;
    CREATE TRIGGER IF NOT EXISTS projection_world_player_insert AFTER INSERT ON players WHEN NEW.world_uid IS NOT NULL
    BEGIN
      INSERT INTO lineup_player_revisions VALUES(NEW.profile_id,NEW.union_id,NEW.name,1,1,0)
      ON CONFLICT(profile_id,union_id,player_name) DO UPDATE SET revision=revision+1,dirty=1;
    END;
    CREATE TRIGGER IF NOT EXISTS projection_world_player_update AFTER UPDATE ON players
      WHEN OLD.world_uid IS NOT NEW.world_uid OR OLD.name IS NOT NEW.name OR OLD.union_id IS NOT NEW.union_id
    BEGIN
      INSERT INTO lineup_player_revisions VALUES(OLD.profile_id,OLD.union_id,OLD.name,1,1,0)
      ON CONFLICT(profile_id,union_id,player_name) DO UPDATE SET revision=revision+1,dirty=1;
      INSERT INTO lineup_player_revisions VALUES(NEW.profile_id,NEW.union_id,NEW.name,1,1,0)
      ON CONFLICT(profile_id,union_id,player_name) DO UPDATE SET revision=revision+1,dirty=1;
    END;
    CREATE TRIGGER IF NOT EXISTS projection_army_insert AFTER INSERT ON army_current
    BEGIN INSERT INTO lineup_army_dirty VALUES(NEW.profile_id,NEW.army_id) ON CONFLICT DO NOTHING; END;
    CREATE TRIGGER IF NOT EXISTS projection_army_update AFTER UPDATE ON army_current WHEN OLD.state_ref IS NOT NEW.state_ref
    BEGIN INSERT INTO lineup_army_dirty VALUES(NEW.profile_id,NEW.army_id) ON CONFLICT DO NOTHING; END;
    CREATE TRIGGER IF NOT EXISTS collection_report_insert AFTER INSERT ON battle_reports
    BEGIN
      INSERT INTO report_collection_state VALUES(NEW.profile_id,NEW.battle_id,1,0)
      ON CONFLICT(profile_id,battle_id) DO UPDATE SET received=1;
    END;
    CREATE TRIGGER IF NOT EXISTS collection_report_delete AFTER DELETE ON battle_reports
    BEGIN UPDATE report_collection_state SET received=0 WHERE profile_id=OLD.profile_id AND battle_id=OLD.battle_id; END;
    CREATE TRIGGER IF NOT EXISTS collection_body_insert AFTER INSERT ON report_bodies
    BEGIN
      INSERT INTO report_collection_state VALUES(NEW.profile_id,NEW.battle_id,0,TRIM(NEW.body)<>'')
      ON CONFLICT(profile_id,battle_id) DO UPDATE SET has_full=excluded.has_full;
    END;
    CREATE TRIGGER IF NOT EXISTS collection_body_update AFTER UPDATE ON report_bodies
    BEGIN UPDATE report_collection_state SET has_full=TRIM(NEW.body)<>'' WHERE profile_id=NEW.profile_id AND battle_id=NEW.battle_id; END;
    CREATE TRIGGER IF NOT EXISTS collection_body_delete AFTER DELETE ON report_bodies
    BEGIN UPDATE report_collection_state SET has_full=0 WHERE profile_id=OLD.profile_id AND battle_id=OLD.battle_id; END;
    CREATE TRIGGER IF NOT EXISTS collection_reference_insert AFTER INSERT ON army_report_refs
    BEGIN INSERT INTO report_collection_state VALUES(NEW.profile_id,NEW.battle_id,0,0) ON CONFLICT DO NOTHING; END;
    CREATE TRIGGER IF NOT EXISTS collection_own_reference_insert AFTER INSERT ON own_report_indices
    BEGIN INSERT INTO report_collection_state VALUES(NEW.profile_id,NEW.battle_id,0,0) ON CONFLICT DO NOTHING; END;
    ''')
    if not db.execute('SELECT 1 FROM lineup_projection_meta WHERE profile_id=? AND version=?', (profile, VERSION)).fetchone():
        with db:
            for table in ('battle_reports','army_report_refs','own_report_indices','report_bodies'):
                db.execute(f'''INSERT OR IGNORE INTO report_collection_state
                    SELECT profile_id,battle_id,0,0 FROM {table} WHERE profile_id=?''', (profile,))
            db.execute('''UPDATE report_collection_state SET
                received=EXISTS(SELECT 1 FROM battle_reports b WHERE b.profile_id=report_collection_state.profile_id AND b.battle_id=report_collection_state.battle_id),
                has_full=EXISTS(SELECT 1 FROM report_bodies b WHERE b.profile_id=report_collection_state.profile_id AND b.battle_id=report_collection_state.battle_id AND TRIM(b.body)<>'')
                WHERE profile_id=?''', (profile,))
            db.execute('''INSERT OR IGNORE INTO lineup_player_revisions
                SELECT a.profile_id,p.union_id,p.name,1,1,0 FROM army_current a
                JOIN army_states s ON s.id=a.state_ref JOIN players p ON p.profile_id=a.profile_id AND p.player_key='world:'||s.owner_uid
                WHERE a.profile_id=? GROUP BY p.union_id,p.name''', (profile,))
            db.execute('INSERT OR IGNORE INTO lineup_army_dirty SELECT profile_id,army_id FROM army_current WHERE profile_id=?', (profile,))
            db.execute('INSERT OR REPLACE INTO lineup_projection_meta VALUES(?,?)', (profile, VERSION))
    if not db.execute("SELECT 1 FROM lineup_rule_migrations WHERE profile_id=? AND rule='group-arrival-12'",(profile,)).fetchone():
        # Only owners of archived legion expeditions need their cached history
        # reconsidered. Raw reports and immutable march evidence are unchanged.
        with db:
            owners = db.execute('''SELECT DISTINCT p.union_id,p.name FROM march_samples m
                JOIN army_states s ON s.profile_id=m.profile_id AND s.army_id=m.army_id
                AND s.begin_at=m.begin_at AND s.end_at=m.end_at AND s.message_ref=m.message_ref
                JOIN players p ON p.profile_id=s.profile_id AND p.player_key='world:'||s.owner_uid
                WHERE m.profile_id=? AND m.state_code=12''',(profile,)).fetchall()
            for union,name in owners:
                db.execute('''INSERT INTO lineup_player_revisions VALUES(?,?,?,1,1,?)
                    ON CONFLICT(profile_id,union_id,player_name) DO UPDATE
                    SET revision=revision+1,dirty=1,changed_at=excluded.changed_at''',(profile,union,name,time.time()))
            db.execute("INSERT INTO lineup_rule_migrations VALUES(?,'group-arrival-12')",(profile,))
    if not db.execute("SELECT 1 FROM lineup_rule_migrations WHERE profile_id=? AND rule='wanderer-human-reference-1'",(profile,)).fetchone():
        # The old human-reference filter excluded unaffiliated wanderers' PvE
        # headers. Invalidate only their player histories, not the whole season.
        with db:
            affected = db.execute('''SELECT DISTINCT union_id,player_name FROM combat_sides
                WHERE profile_id=? AND union_id IN ('','0') AND side='attack' AND npc<>0
                AND CAST(json_extract(features,'$.attacker_force') AS INTEGER)=1''',(profile,)).fetchall()
            for union,name in affected:
                db.execute('''INSERT INTO lineup_player_revisions VALUES(?,?,?,1,1,?)
                    ON CONFLICT(profile_id,union_id,player_name) DO UPDATE
                    SET revision=revision+1,dirty=1,changed_at=excluded.changed_at''',(profile,union,name,time.time()))
            db.execute("INSERT INTO lineup_rule_migrations VALUES(?,'wanderer-human-reference-1')",(profile,))


class LineupProjection:
    MAX_MEMORY = 512

    def __init__(self, archive):
        self.archive = archive
        install(archive.db, archive.profile_id)
        self.lock = threading.RLock()
        self.memory, self.ready, self.requested, self.histories = OrderedDict(), OrderedDict(), OrderedDict(), OrderedDict()
        self.metrics = dict(memory_hits=0,disk_hits=0,misses=0,computations=0,persisted=0,rejected_stale=0,errors=0,
                            history_hits=0,history_misses=0)
        self.status = None
        self.closed, self.wake = threading.Event(), threading.Event()
        self.thread = threading.Thread(target=self.run,daemon=True,name='current-army-projections')
        self.rounds = 0

    def start(self):
        self.thread.start()

    def close(self):
        self.closed.set(); self.wake.set()
        if self.thread.is_alive():
            self.thread.join(timeout=12)

    @staticmethod
    def token(reader, army):
        row = reader.db.execute('''SELECT a.profile_id,a.army_id,a.state_ref,a.observed_at,s.owner_uid,p.name AS player_name,p.union_id,
            COALESCE(r.revision,0) AS revision FROM live_army_current a
            JOIN live_army_states s ON s.id=a.state_ref JOIN players p ON p.profile_id=a.profile_id AND p.player_key='world:'||s.owner_uid
            LEFT JOIN lineup_player_revisions r ON r.profile_id=a.profile_id AND r.union_id=p.union_id AND r.player_name=p.name
            WHERE a.profile_id=? AND a.army_id=?''', (reader.profile_id,str(army))).fetchone()
        return dict(row) if row else None

    @staticmethod
    def key(token):
        return tuple(token[k] for k in ('profile_id','army_id','state_ref','owner_uid','union_id','player_name','revision'))+(VERSION,)

    def lookup(self, reader, token, at, *, request=True):
        key = self.key(token)
        with self.lock:
            value = self.memory.get(key)
            if value and value['valid_until'] > at:
                self.memory.move_to_end(key); self.metrics['memory_hits'] += 1
                return copy.deepcopy(value['result'])
        row = reader.db.execute('SELECT * FROM army_lineup_projection WHERE profile_id=? AND army_id=?',
                                (token['profile_id'],token['army_id'])).fetchone()
        if row and row['version']==VERSION and self.key(row)==key and row['valid_until']>at:
            value = {'result':json.loads(row['payload']), 'valid_until':row['valid_until']}
            with self.lock:
                self.metrics['disk_hits'] += 1
                self._memory(key,value)
            return copy.deepcopy(value['result'])
        with self.lock:
            self.metrics['misses'] += 1
            if request:
                self.requested[token['profile_id'],token['army_id']] = None
        if request: self.wake.set()
        return None

    def _memory(self, key, value):
        self.memory[key] = value; self.memory.move_to_end(key)
        while len(self.memory)>self.MAX_MEMORY:
            self.memory.popitem(last=False)

    def history(self, reader, name, union):
        from lineup_prediction import player_history
        if reader.db.in_transaction and not getattr(reader,'read_only',False):
            return player_history(reader.db,reader.profile_id,name,union)
        revision = reader.db.execute('''SELECT revision FROM lineup_player_revisions
            WHERE profile_id=? AND union_id=? AND player_name=?''',(reader.profile_id,union,name)).fetchone()
        key = (reader.profile_id,union,name,revision[0] if revision else 0)
        with self.lock:
            if key in self.histories:
                self.histories.move_to_end(key); self.metrics['history_hits'] += 1
                return copy.deepcopy(self.histories[key])
        result = player_history(reader.db,reader.profile_id,name,union)
        with self.lock:
            self.histories[key] = copy.deepcopy(result)
            self.metrics['history_misses'] += 1
            while len(self.histories)>128: self.histories.popitem(last=False)
        return result

    def remember(self, token, result, history, at):
        # A future-dated source becomes eligible without another write. Expire
        # exactly at that boundary, and always bypass this cache for replay.
        future = [at+365*24*3600]
        if history:
            for row in history[0]:
                future.extend(row[field] for field in ('event_at','observed_at','binding_at') if row.get(field) and row[field]>=at)
            for rows in history[1].values():
                for row in rows:
                    future.extend(row[field] for field in ('observed_at','end_at') if row.get(field) and row[field]>=at)
        result = ([dict(row) for row in result[0]],result[1],copy.deepcopy(result[2]))
        value = {'result':result,'valid_until':min(future),'computed_at':at,'token':token}
        with self.lock:
            self.metrics['computations'] += 1
            self._memory(self.key(token),value)
            self.ready[self.key(token)] = value
            while len(self.ready)>self.MAX_MEMORY:
                self.ready.popitem(last=False)
        self.wake.set()

    def flush(self):
        with self.lock:
            batch = [self.ready.popitem(last=False)[1] for _ in range(min(32,len(self.ready)))]
        if not batch:
            return
        with self.archive.lock, self.archive.db:
            for value in batch:
                token, result = value['token'], value['result']
                current = self.token(self.archive,token['army_id']) if token['profile_id']==self.archive.profile_id else None
                if not current or self.key(current)!=self.key(token) or value['valid_until']<=time.time():
                    with self.lock: self.metrics['rejected_stale'] += 1
                    continue
                rows,match,evidence = result
                resolved = bool(rows and match in VERIFIED and not evidence.get('current_resolution',{}).get('unresolved')
                                and all(r['player_name']==token['player_name'] and str(r['union_id'])==str(token['union_id']) for r in rows))
                import reported_archive
                if not reported_archive.admitted(self.archive.db,token['profile_id'],token['army_id']):
                    if resolved and evidence.get('current_report_id'):
                        reported_archive.promote(self.archive.db,token['profile_id'],token['army_id'],evidence['current_report_id'],value['computed_at'])
                    # Promotion changes the evidence revision. Recompute on the
                    # next pass rather than persist an obsolete or unknown cache.
                    continue
                self.archive.db.execute('''INSERT OR REPLACE INTO army_lineup_projection VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                    (*[token[k] for k in ('profile_id','army_id','state_ref','owner_uid','union_id','player_name','revision')],
                     VERSION,value['valid_until'],value['computed_at'],int(resolved),evidence.get('current_report_id'),
                     json.dumps(result,ensure_ascii=False,separators=(',',':'))))
                self.archive.db.execute('DELETE FROM lineup_army_dirty WHERE profile_id=? AND army_id=?',
                                        (token['profile_id'],token['army_id']))
                with self.lock: self.metrics['persisted'] += 1

    def refresh_group(self, union, name):
        with self.archive.read_snapshot(timeout=8) as reader:
            history = self.history(reader,name,union)
            ids = [r[0] for r in reader.db.execute('''SELECT a.army_id FROM live_army_current a
                JOIN live_army_states s ON s.id=a.state_ref JOIN players p ON p.profile_id=a.profile_id AND p.player_key='world:'||s.owner_uid
                WHERE a.profile_id=? AND p.union_id=? AND p.name=?''', (reader.profile_id,union,name))]
            revision = reader.db.execute('SELECT revision FROM lineup_player_revisions WHERE profile_id=? AND union_id=? AND player_name=?',
                                         (reader.profile_id,union,name)).fetchone()
            at = time.time()
            for army in ids:
                if self.closed.is_set(): return
                token = self.token(reader,army)
                cached = self.lookup(reader,token,at,request=False)
                if cached is None:
                    result = reader._resolve_army_lineups(army,history=history,known_at=at)
                    self.remember(token,result,history,at)
        self.flush()
        if revision:
            with self.archive.lock, self.archive.db:
                if self.archive.profile_id==reader.profile_id:
                    self.archive.db.execute('''UPDATE lineup_player_revisions SET dirty=0
                        WHERE profile_id=? AND union_id=? AND player_name=? AND revision=?''',
                        (reader.profile_id,union,name,revision[0]))

    def collect_status(self):
        with self.archive.read_snapshot(timeout=5) as reader:
            count = dict(reader.db.execute('''SELECT COUNT(*) AS known,COALESCE(SUM(received),0) AS received,
                COALESCE(SUM(received AND has_full),0) AS full,COALESCE(SUM(has_full AND NOT received),0) AS body_only
                FROM report_collection_state WHERE profile_id=?''',(reader.profile_id,)).fetchone())
            valid = '''c.version=? AND c.state_ref=a.state_ref AND c.owner_uid=s.owner_uid AND c.union_id=p.union_id
                AND c.player_name=p.name AND c.revision=COALESCE(r.revision,0) AND c.valid_until>?'''
            armies = dict(reader.db.execute(f'''SELECT COUNT(*) AS total,
                COALESCE(SUM(CASE WHEN {valid} THEN 1 ELSE 0 END),0) AS checked,
                COALESCE(SUM(CASE WHEN {valid} AND c.resolved=1 THEN 1 ELSE 0 END),0) AS linked
                FROM army_current a JOIN army_states s ON s.id=a.state_ref
                LEFT JOIN players p ON p.profile_id=a.profile_id AND p.player_key='world:'||s.owner_uid
                LEFT JOIN lineup_player_revisions r ON r.profile_id=a.profile_id AND r.union_id=p.union_id AND r.player_name=p.name
                LEFT JOIN army_lineup_projection c ON c.profile_id=a.profile_id AND c.army_id=a.army_id
                WHERE a.profile_id=?''',(VERSION,time.time(),VERSION,time.time(),reader.profile_id)).fetchone())
            def ratio(n,d): return n/d if d else None
            status = {'profile_id':reader.profile_id,'checked_at':time.time(),
                'scope':'本赛季当前角色档案；收到率分母为已发现编号，非全区战报总量',
                'received':{'count':count['received'],'total':count['known'],'ratio':ratio(count['received'],count['known'])},
                'full':{'count':count['full'],'total':count['received'],'ratio':ratio(count['full'],count['received']),'body_only':count['body_only']},
                'linked':{'count':armies['linked'],'total':armies['total'],'ratio':ratio(armies['linked'],armies['total']),
                          'checked':armies['checked'],'pending':armies['total']-armies['checked']}}
        with self.lock:
            self.status = status
        return self.snapshot()

    def snapshot(self):
        with self.lock:
            status = copy.deepcopy(self.status) if self.status and self.status['profile_id']==self.archive.profile_id else None
            return {'ready':status is not None,**(status or {}),'cache':dict(self.metrics)}

    def step(self):
        self.flush()
        with self.lock:
            request = self.requested.popitem(last=False)[0] if self.requested else None
        with self.archive.read_snapshot() as reader:
            group = None
            if request and request[0]==reader.profile_id:
                token = self.token(reader,request[1])
                if token: group = (token['union_id'],token['player_name'])
            if not group:
                direction = 'ASC' if self.rounds%6==0 else 'DESC'
                row = reader.db.execute(f'''SELECT union_id,player_name FROM lineup_player_revisions
                    WHERE profile_id=? AND dirty=1 ORDER BY changed_at {direction} LIMIT 1''',(reader.profile_id,)).fetchone()
                if row: group = tuple(row)
            if not group:
                row = reader.db.execute('''SELECT d.army_id FROM lineup_army_dirty d
                    JOIN live_army_current a ON a.profile_id=d.profile_id AND a.army_id=d.army_id
                    JOIN live_army_states s ON s.id=a.state_ref
                    JOIN players p ON p.profile_id=a.profile_id AND p.player_key='world:'||s.owner_uid
                    WHERE d.profile_id=? LIMIT 1''',(reader.profile_id,)).fetchone()
                token = self.token(reader,row[0]) if row else None
                if token: group = (token['union_id'],token['player_name'])
            if not group:
                row = reader.db.execute('''SELECT army_id FROM army_lineup_projection
                    WHERE profile_id=? AND valid_until<=? LIMIT 1''',(reader.profile_id,time.time())).fetchone()
                token = self.token(reader,row[0]) if row else None
                if token: group = (token['union_id'],token['player_name'])
        if group:
            self.refresh_group(*group)
        self.rounds += 1
        with self.lock:
            due = not self.status or time.time()-self.status['checked_at']>=5
        if due: self.collect_status()
        return bool(group)

    def run(self):
        while not self.closed.is_set():
            delay = .1
            try:
                if not self.step(): delay = 1
            except Exception:
                logging.exception('Current army projection will retry')
                with self.lock: self.metrics['errors'] += 1
                delay = 2
            if self.closed.wait(.05): break
            self.wake.wait(delay); self.wake.clear()
