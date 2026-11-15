"""Incremental battle-side attendance, scoped to the logged-in season and alliance."""
import csv
import io
import json
import math
import threading
import time
from datetime import datetime, timedelta, timezone
from observation_codec import report_has_npc

TZ = timezone(timedelta(hours=8))
METRICS = {"participations":"玩家参战", "main":"主力场次", "spartan":"斯巴达场次", "wins":"胜场", "honor":"战报武勋", "damage":"造成玩家兵损", "losses":"自身玩家对战兵损",
           "capture_low":"翻地1—4级", "capture_high":"翻地5—9级", "lost_land":"被翻地", "demolitions":"拆除", "demolished":"被拆", "siege_damage":"拆除耐久"}
VERSION = "4"
SCHEMA = """
CREATE TABLE IF NOT EXISTS attendance_facts(
 profile_id TEXT NOT NULL,battle_id TEXT NOT NULL,side TEXT NOT NULL,player_key TEXT NOT NULL,
 player_name TEXT NOT NULL,union_id TEXT NOT NULL,event_at REAL NOT NULL,wid INTEGER,
 category TEXT NOT NULL,outcome TEXT NOT NULL,pvp INTEGER NOT NULL,honor INTEGER,
 damage INTEGER,losses INTEGER,updated_at REAL NOT NULL,
 PRIMARY KEY(profile_id,battle_id,side));
CREATE INDEX IF NOT EXISTS attendance_period ON attendance_facts(profile_id,union_id,event_at,player_key);
CREATE INDEX IF NOT EXISTS attendance_player ON attendance_facts(profile_id,player_key,event_at DESC);
CREATE TABLE IF NOT EXISTS attendance_dirty(profile_id TEXT NOT NULL,battle_id TEXT NOT NULL,
 PRIMARY KEY(profile_id,battle_id));
CREATE TABLE IF NOT EXISTS attendance_meta(profile_id TEXT PRIMARY KEY,version TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS attendance_rules(profile_id TEXT NOT NULL,union_id TEXT NOT NULL,
 data TEXT NOT NULL,updated_at REAL NOT NULL,PRIMARY KEY(profile_id,union_id));
CREATE TABLE IF NOT EXISTS attendance_rule_history(
 profile_id TEXT NOT NULL,union_id TEXT NOT NULL,version TEXT NOT NULL,data TEXT NOT NULL,created_at REAL NOT NULL,
 PRIMARY KEY(profile_id,union_id,version));
CREATE TABLE IF NOT EXISTS attendance_roster(profile_id TEXT NOT NULL,union_id TEXT NOT NULL,
 world_uid TEXT NOT NULL,name TEXT NOT NULL,group_name TEXT NOT NULL,observed_at REAL NOT NULL,
 PRIMARY KEY(profile_id,union_id,world_uid));
CREATE TRIGGER IF NOT EXISTS attendance_insert AFTER INSERT ON combat_sides BEGIN
 INSERT INTO attendance_dirty VALUES(NEW.profile_id,NEW.battle_id) ON CONFLICT DO NOTHING; END;
CREATE TRIGGER IF NOT EXISTS attendance_update AFTER UPDATE ON combat_sides
 WHEN OLD.heroes IS NOT NEW.heroes OR OLD.features IS NOT NEW.features OR OLD.player_key IS NOT NEW.player_key
 OR OLD.union_id IS NOT NEW.union_id OR OLD.npc IS NOT NEW.npc OR OLD.outcome IS NOT NEW.outcome
 OR OLD.event_at IS NOT NEW.event_at BEGIN
 INSERT INTO attendance_dirty VALUES(NEW.profile_id,NEW.battle_id) ON CONFLICT DO NOTHING; END;
CREATE TRIGGER IF NOT EXISTS attendance_delete AFTER DELETE ON combat_sides BEGIN
 DELETE FROM attendance_facts WHERE profile_id=OLD.profile_id AND battle_id=OLD.battle_id AND side=OLD.side;
 INSERT INTO attendance_dirty VALUES(OLD.profile_id,OLD.battle_id) ON CONFLICT DO NOTHING; END;
"""


def integer(value):
    return value if type(value) is int and value >= 0 else None


def wuxun(value):
    # Official client ReportProfileHelper.GetBattlesWuxun (9.3.4): xwc is encoded.
    # gongxun is a different field and must never be used as 武勋.
    if integer(value) is None:return None
    return max(value//100-(value%100-3)-678,0) if value else 0


def classify(heroes, raw):
    from team_catalog import classify as shared_classify
    return shared_classify(heroes,raw)


def loss(heroes):
    if not heroes or any(integer(h.get("initial_hp")) is None or integer(h.get("remaining_hp")) is None
                         or h["remaining_hp"]>h["initial_hp"] for h in heroes): return None
    return sum(h["initial_hp"]-h["remaining_hp"] for h in heroes)


class Attendance:
    def __init__(self, archive):
        self.archive = archive
        self.closed = threading.Event()
        self.error = ""
        with archive.lock, archive.db:
            archive.db.executescript(SCHEMA)
            self.seed(archive.profile_id)
            from territory_events import seed_roster_metrics
            seed_roster_metrics(archive)
        self.thread = threading.Thread(target=self._run,name="attendance-index",daemon=True)
        self.thread.start()

    def seed(self, profile):
        db=self.archive.db
        if not db.execute("SELECT 1 FROM attendance_meta WHERE profile_id=? AND version=?",(profile,VERSION)).fetchone():
            db.execute('DELETE FROM attendance_facts WHERE profile_id=? AND pvp=0',(profile,))
            db.execute("INSERT OR IGNORE INTO attendance_dirty SELECT profile_id,battle_id FROM combat_sides WHERE profile_id=?",(profile,))
            db.execute("INSERT OR REPLACE INTO attendance_meta VALUES(?,?)",(profile,VERSION))
        from team_catalog import migrate_player_identity
        migrate_player_identity(self.archive)
        rule='attendance-known-opponent-1'
        if not db.execute('SELECT 1 FROM lineup_rule_migrations WHERE profile_id=? AND rule=?',(profile,rule)).fetchone():
            db.execute('''DELETE FROM attendance_facts WHERE profile_id=? AND EXISTS(SELECT 1 FROM formation_facts f
                WHERE f.profile_id=attendance_facts.profile_id AND f.battle_id=attendance_facts.battle_id
                AND f.side=attendance_facts.side AND f.pvp=0)''',(profile,))
            db.execute('INSERT INTO lineup_rule_migrations VALUES(?,?)',(profile,rule))

    def drain(self, limit=200):
        a=self.archive; db=a.db
        with a.lock,db:
            self.seed(a.profile_id)
            # New combat takes priority over the one-time historical projection.
            jobs=db.execute('''SELECT d.profile_id,d.battle_id FROM battle_reports b INDEXED BY battle_profile_time
                CROSS JOIN attendance_dirty d ON d.profile_id=b.profile_id AND d.battle_id=b.battle_id
                WHERE b.profile_id=? ORDER BY b.event_at DESC,b.battle_id DESC LIMIT ?''',(a.profile_id,limit)).fetchall()
            for profile,bid in jobs:
                rows=[dict(r) for r in db.execute("SELECT * FROM combat_sides WHERE profile_id=? AND battle_id=?",(profile,bid))]
                report=db.execute('SELECT data FROM battle_reports WHERE profile_id=? AND battle_id=?',(profile,bid)).fetchone()
                import team_catalog,territory_events
                if report:
                    battle=json.loads(report[0])
                    team_catalog.index_battle(a,bid,rows,battle)
                    territory_events.index_report(a,battle)
                if any(r['npc'] != 0 for r in rows) or (report and not team_catalog.human_opponents(battle)):
                    db.execute('DELETE FROM attendance_facts WHERE profile_id=? AND battle_id=?',(profile,bid))
                    db.execute('DELETE FROM attendance_dirty WHERE profile_id=? AND battle_id=?',(profile,bid))
                    continue
                sides={r["side"]:json.loads(r["heroes"]) for r in rows}
                for r in rows:
                    features=json.loads(r["features"]); heroes=sides[r["side"]]
                    opponent=sides.get("defend" if r["side"]=="attack" else "attack",[])
                    db.execute("INSERT OR REPLACE INTO attendance_facts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (profile,bid,r["side"],r["player_key"],r["player_name"],r["union_id"],r["event_at"],r["wid"],
                      classify(heroes,features.get(r["side"]+"_all_hero_info")),r["outcome"],int(r["npc"]==0),
                      wuxun(features.get(r["side"]+"er_xwc")),loss(opponent),loss(heroes),time.time()))
                db.execute("DELETE FROM attendance_dirty WHERE profile_id=? AND battle_id=?",(profile,bid))
            if jobs:a.contribution_revision+=1
        return len(jobs)

    def _run(self):
        while not self.closed.is_set():
            try: count=self.drain(40); self.error=""
            except Exception as exc: count=0; self.error=type(exc).__name__
            self.closed.wait(.05 if count==40 else 1)

    def close(self):
        self.closed.set(); self.thread.join(timeout=15)


def roster(archive, value, union, at):
    rows=value if isinstance(value,list) else []
    if rows and isinstance(rows[0],list) and rows[0] and isinstance(rows[0][0],list): rows=rows[0]
    if union in ("","0"): return
    valid=[row for row in rows if isinstance(row,list)and len(row)>=31 and type(row[0])is int and row[0]>0 and isinstance(row[1],str)]
    if valid and str(union)==str(archive.profile['union_id'])and any(str(row[0])==str(archive.profile['account_role_id'])for row in valid):
        # The client's own member response is complete, not a page of armies.
        archive.db.execute('DELETE FROM attendance_roster WHERE profile_id=? AND union_id=? AND world_uid NOT IN ('+
            ','.join('?'for _ in valid)+') AND observed_at<=?',(archive.profile_id,union,*(str(row[0])for row in valid),at))
    for row in rows:
        if isinstance(row,list) and len(row)>=31 and type(row[0]) is int and row[0]>0 and isinstance(row[1],str):
            archive.db.execute("""INSERT INTO attendance_roster VALUES(?,?,?,?,?,?) ON CONFLICT(profile_id,union_id,world_uid)
             DO UPDATE SET name=excluded.name,group_name=excluded.group_name,observed_at=excluded.observed_at
             WHERE excluded.observed_at>=attendance_roster.observed_at""",
             (archive.profile_id,union,str(row[0]),row[1],row[13] if isinstance(row[13],str) else "",at))
    from territory_events import observe_roster
    observe_roster(archive,rows,union,at)


def member_metrics(archive,aliases,start,end):
    aliases=list(set(aliases))
    if 'world:'+str(archive.profile['account_role_id'])in aliases and archive.profile.get('battle_role_id'):
        aliases.append('battle:'+archive.profile['battle_role_id'])
    cutoff=min(end,time.time())
    snapshots=[dict(s)for s in archive.db.execute('''SELECT * FROM member_metric_snapshots
        WHERE profile_id=? AND player_key IN ('''+','.join('?'for _ in aliases)+''')
        AND observed_at<=? ORDER BY observed_at DESC''',(archive.profile_id,*aliases,cutoff))]
    result=dict(joined_at=None,cumulative_honor=None,new_honor=None)
    if not snapshots:return result
    result['joined_at']=next((s['joined_at']for s in snapshots if s['joined_at']),None)
    from role_context import context
    # RangerTotalWuxun is a separate client counter from user_res.wu_xun_total.
    honors=[s for s in snapshots if s['honor']is not None and
        (not context(archive.profile)['is_wanderer']or s['source'].endswith(',ranger15'))]
    last=next(iter(honors),None)
    first=next((s for s in honors if s['observed_at']<=start),None)
    result['cumulative_honor']=last['honor']if last else None
    if first and last and start-first['observed_at']<=300 and cutoff-last['observed_at']<=300 and last['honor']>=first['honor']:
        result['new_honor']=last['honor']-first['honor']
    return result


def rules(archive):
    row=archive.db.execute("SELECT data FROM attendance_rules WHERE profile_id=? AND union_id=?",(archive.profile_id,archive.profile["union_id"])).fetchone()
    return json.loads(row[0]) if row else {"enabled":False,"weights":{}}


def save_rules(archive, value):
    weights=value.get("weights"); enabled=value.get("enabled")
    if type(enabled) is not bool or not isinstance(weights,dict) or set(weights)-METRICS.keys(): raise ValueError("积分规则格式不正确")
    if any(type(v) not in (int,float) or not math.isfinite(v) or abs(v)>1000000 for v in weights.values()): raise ValueError("积分权重须为有限数字")
    if enabled and not any(weights.values()): raise ValueError("请至少设置一项非零权重")
    result={"enabled":enabled,"weights":weights}
    from observation_codec import digest
    result['version']=digest([enabled,weights])[:16]
    with archive.lock,archive.db:
        archive.db.execute("INSERT OR REPLACE INTO attendance_rules VALUES(?,?,?,?)",(archive.profile_id,archive.profile["union_id"],json.dumps(result),time.time()))
        archive.db.execute('INSERT OR IGNORE INTO attendance_rule_history VALUES(?,?,?,?,?)',
                           (archive.profile_id,archive.profile['union_id'],result['version'],json.dumps(result),time.time()))
        archive.contribution_revision+=1
    return result


def period(archive, q):
    now=datetime.now(TZ)
    try:
        start=datetime.strptime(q.get("from") or now.strftime("%Y-%m-%d"),"%Y-%m-%d").replace(tzinfo=TZ)
        end=datetime.strptime(q.get("to") or now.strftime("%Y-%m-%d"),"%Y-%m-%d").replace(tzinfo=TZ)+timedelta(days=1)
    except (ValueError,TypeError): raise ValueError("请选择有效日期")
    if end<=start or (end-start).days>370: raise ValueError("查询日期范围须为1～370天")
    floor=archive.retention_from
    return max(start.timestamp(),floor),end.timestamp(),floor


def query(archive,q):
    start,end,floor=period(archive,q); db=archive.db; pid=archive.profile_id; union=str(archive.profile["union_id"])
    from role_context import context
    role=context(archive.profile);personal=(role['own_player_key']or'')if not role['has_organization']else''
    if not role['has_organization']and not personal:
        return {"items":[],"profile":archive.profile,"role_context":role,"message":"正在核对本人的战报角色标识，不合并其他无组织玩家","rules":rules(archive),"metrics":METRICS}
    selected=q.get("player_key")
    if personal and selected and selected!=personal:return {'items':[],'events':[],'limit':200}
    if selected:
        from combat_loadout import named_hero
        rows=[dict(r) for r in db.execute("""SELECT f.*,b.place_name,c.heroes own_heroes,
         o.player_name opponent_name,o.heroes opponent_heroes FROM attendance_facts f JOIN battle_reports b
         ON b.profile_id=f.profile_id AND b.battle_id=f.battle_id
         LEFT JOIN combat_sides c ON c.profile_id=f.profile_id AND c.battle_id=f.battle_id AND c.side=f.side
         LEFT JOIN combat_sides o ON o.profile_id=f.profile_id AND o.battle_id=f.battle_id
           AND o.side=CASE f.side WHEN 'attack' THEN 'defend' ELSE 'attack' END
         WHERE f.profile_id=? AND f.union_id=? AND f.pvp=1
         AND f.player_key=? AND f.event_at>=? AND f.event_at<? ORDER BY f.event_at DESC LIMIT 200""",(pid,union,selected,start,end))]
        for row in rows:
            for key in ('own_heroes','opponent_heroes'):
                row[key]=[{'id':h['id'],'position':h['position'],'name':named_hero(h)['name']}
                          for h in json.loads(row[key] or '[]')]
        if q.get('include_pve')=='1':
            rows=[dict(r)for r in db.execute('''SELECT f.*,b.place_name,c.heroes own_heroes,o.player_name opponent_name,o.heroes opponent_heroes
                FROM formation_facts f JOIN battle_reports b ON b.profile_id=f.profile_id AND b.battle_id=f.battle_id
                JOIN combat_sides c ON c.profile_id=f.profile_id AND c.battle_id=f.battle_id AND c.side=f.side
                LEFT JOIN combat_sides o ON o.profile_id=f.profile_id AND o.battle_id=f.battle_id AND o.side<>f.side
                WHERE f.profile_id=? AND f.union_id=? AND f.player_key=? AND f.event_at>=? AND f.event_at<?
                ORDER BY f.event_at DESC,CAST(f.battle_id AS INTEGER) DESC LIMIT 200''',(pid,union,selected,start,end))]
            for row in rows:
                for key in ('own_heroes','opponent_heroes'):row[key]=json.loads(row[key]or'[]')
        from territory_events import query as event_query
        events=event_query(archive,dict(player_key=selected,from_at=start,to_at=end,limit=200))['items']
        return {"items":rows,"events":events,"limit":200}
    aggregate="""COUNT(*) records,SUM(pvp) participations,SUM(pvp AND category='main') main,
     SUM(pvp AND category='spartan') spartan,SUM(pvp AND category='unknown') unknown,
     SUM(pvp AND outcome='win') wins,SUM(pvp AND outcome='loss') defeats,SUM(pvp AND outcome='draw') draws,
     SUM(pvp AND category='main' AND opponent_main) main_duels,
     SUM(pvp AND category='main' AND opponent_main AND outcome='win') main_wins,
     SUM(pvp AND category='main' AND opponent_main AND outcome IN ('win','loss','draw')) main_decided,
     SUM(CASE WHEN pvp=1 THEN honor END) honor, SUM(pvp=1 AND honor IS NOT NULL) honor_known,
     SUM(CASE WHEN pvp=1 THEN damage END) damage,SUM(CASE WHEN pvp=1 THEN losses END) losses,
     SUM(pvp=1 AND damage IS NOT NULL AND losses IS NOT NULL) damage_known,
     SUM(CASE WHEN pvp=1 AND category='main' AND opponent_main THEN damage END) main_damage,
     SUM(CASE WHEN pvp=1 AND category='main' AND opponent_main THEN losses END) main_losses,
     SUM(pvp=1 AND category='main' AND opponent_main AND damage IS NOT NULL AND losses IS NOT NULL) main_damage_known,
     MAX(event_at) last_at"""
    rows=[dict(r) for r in db.execute(f"""WITH paired AS (
      SELECT f.*,COALESCE(o.category='main' AND o.pvp=1,0) opponent_main FROM attendance_facts f
      LEFT JOIN attendance_facts o ON o.profile_id=f.profile_id AND o.battle_id=f.battle_id
        AND o.side=CASE f.side WHEN 'attack' THEN 'defend' ELSE 'attack' END)
      SELECT player_key,MAX(player_name) player_name,{aggregate} FROM paired
      WHERE profile_id=? AND union_id=? AND pvp=1 AND event_at>=? AND event_at<?
      AND (?='' OR player_key=?) GROUP BY player_key""",(pid,union,start,end,personal,personal))]
    from territory_events import player_stats
    contributions=player_stats(archive,union,start,end)
    if personal:contributions={k:v for k,v in contributions.items()if k==personal}
    pve=[dict(r)for r in db.execute('''SELECT player_key,MAX(player_name) player_name,COUNT(*) pve_samples,
        MAX(event_at) last_at FROM formation_facts WHERE profile_id=? AND union_id=? AND pvp=0
        AND event_at>=? AND event_at<? AND (?='' OR player_key=?) GROUP BY player_key''',(pid,union,start,end,personal,personal))]
    by_key={r['player_key']:r for r in rows}
    for entry in pve:
        # NPC fighting is retained in its own ledger, never inserted into PvP facts.
        if entry['player_key']not in by_key:
            # Only surface non-PvP-only people when requested by the contribution UI.
            if q.get('include_pve')!='1':continue
            row=dict(player_key=entry['player_key'],player_name=entry['player_name'],participations=0,records=0,
                     main=0,spartan=0,unknown=0,main_wins=0,main_decided=0,main_duels=0,
                     main_damage=0,main_losses=0,main_damage_known=0,honor=None,honor_known=0,
                     damage=None,losses=None,damage_known=0,last_at=entry['last_at'])
            rows.append(row);by_key[row['player_key']]=row
        by_key[entry['player_key']]['pve_samples']=entry['pve_samples']
    for key,metrics in contributions.items():
        if key not in by_key:
            who=db.execute('SELECT name FROM players WHERE profile_id=? AND player_key=?',(pid,key)).fetchone()
            row=dict(player_key=key,player_name=who[0]if who else '玩家资料待补',participations=0,records=0,
                     main=0,spartan=0,unknown=0,main_wins=0,main_decided=0,main_duels=0,main_damage=0,
                     main_losses=0,main_damage_known=0,honor=None,honor_known=0,damage=None,losses=None,damage_known=0)
            rows.append(row);by_key[key]=row
        by_key[key].update(metrics)
    # Roster/group labels join only through a verified role ID; names alone never merge players.
    members=[dict(r) for r in db.execute("""SELECT r.*,p.role_id FROM attendance_roster r LEFT JOIN players p
      ON p.profile_id=r.profile_id AND p.player_key='world:'||r.world_uid WHERE r.profile_id=? AND r.union_id=?""",(pid,union))]
    if personal:members=[]
    by_role={}
    for member in members:
        if member['role_id']:
            by_role.setdefault('battle:'+member['role_id'],[]).append(member)
    # Name matching supplies group labels only, never joins battle identities or teams.
    # Require one retained role and one roster member in this alliance; collisions stay separate.
    by_name={}; roster_names={}
    for row in rows:by_name.setdefault(row['player_name'],[]).append(row)
    for member in members:roster_names.setdefault(member['name'],[]).append(member)
    seen=set(); score_rules=rules(archive)
    for r in rows:
        r.update(joined_at=None,cumulative_honor=None,new_honor=None,demolitions=None,demolished=None,siege_damage=None)
        for metric in ('capture_low','capture_high','capture_unknown','lost_land'):r.setdefault(metric,0)
        r.setdefault('pve_samples',0)
        aliases=[r['player_key']]
        if r['player_key'].startswith('battle:'):
            aliases.extend('world:'+x[0]for x in db.execute('SELECT world_uid FROM players WHERE profile_id=? AND role_id=? AND world_uid IS NOT NULL',(pid,r['player_key'][7:])))
        candidates=by_role.get(r['player_key'],[])
        member=candidates[0] if len(candidates)==1 else None
        r['roster_binding']='role_id' if member else 'unbound'
        if not member and len(by_name[r['player_name']])==1 and len(roster_names.get(r['player_name'],[]))==1:
            member=roster_names[r['player_name']][0]
            r['roster_binding']='unique_name_label_only'
        r['group_name']=member['group_name'] if member else ''
        if member:
            seen.add(member['world_uid'])
            # This attaches member-list display attributes only; battle/team
            # identities and contribution totals are never merged by nickname.
            aliases.append('world:'+member['world_uid'])
        r.update(member_metrics(archive,aliases,start,end))
        r['status']='已见参战' if r['participations'] else '仅见非玩家对战'
        r['main_win_rate']=round(100*r['main_wins']/r['main_decided'],2) if r['main_decided'] else None
        r['exchange']=round(r['main_damage']/r['main_losses'],3) if r['main_losses'] and r['main_damage_known']==r['main_duels'] else None
        for field,known in (('honor','honor_known'),('damage','damage_known'),('losses','damage_known')):
            if r[known]<r['participations']: r[field]=None
        r['score']=score(r,score_rules)
    for member in members:
        if member['world_uid'] not in seen:
            key='world:'+member['world_uid']
            rows.append({'player_key':key,'player_name':member['name'],'group_name':member['group_name'],
                         'status':'未见可关联参战记录','score':None,'records':0,
                         **member_metrics(archive,[key],start,end)})
    text=q.get('search','').strip().casefold(); group=q.get('group','')
    groups=sorted({r['group_name'] for r in rows if r.get('group_name')})
    # Rank in the full alliance period, before search/group display filters.
    rows.sort(key=lambda r:(r.get('score') is None,-(r.get('score') or 0),-(r.get('participations') or 0),r['player_key']))
    previous=None; rank=0
    for i,r in enumerate(rows,1):
        if r['score'] is not None:
            if r['score']!=previous: rank=i; previous=r['score']
            r['rank']=rank
        else:r['rank']=None
    counts=db.execute("""SELECT COUNT(*),SUM(pvp),COUNT(DISTINCT player_key) FROM attendance_facts
      WHERE profile_id=? AND union_id=? AND pvp=1 AND event_at>=? AND event_at<?
      AND (?='' OR player_key=?)""",(pid,union,start,end,personal,personal)).fetchone()
    pending=db.execute('SELECT COUNT(*) FROM attendance_dirty WHERE profile_id=?',(pid,)).fetchone()[0]
    if not q.get('_totals'):
        earliest=db.execute('SELECT MIN(event_at) FROM battle_reports WHERE profile_id=?',(pid,)).fetchone()[0]
        total_start=max(floor,earliest or start)
        total=query(archive,dict(q,**{'from':datetime.fromtimestamp(total_start,TZ).strftime('%Y-%m-%d'),'_totals':True}))
        totals={r['player_key']:r for r in total['items']}
        for r in rows:
            value=totals.get(r['player_key'],{})
            r.update(total_score=value.get('score'),total_rank=value.get('rank'),total_main_win_rate=value.get('main_win_rate'),
                     total_exchange=value.get('exchange'))
    return {'items':[r for r in rows if (not text or text in r['player_name'].casefold()) and (not group or r['group_name']==group)],
      'groups':groups,'profile':archive.profile,'role_context':role,'rules':score_rules,'metrics':METRICS,'retain_from':floor,'start':start,'end':end,
      'pending':pending,'summary':{'sides':counts[0],'pvp':counts[1] or 0,'players':counts[2]},
      'updated_at':time.time(),'definitions':{'main_win_rate':'仅双方均为主力：胜场 / 已知结果场次（含平局）',
      'exchange':'仅双方均为主力：造成兵损 / 自身兵损；兵损=战前兵力−战后现兵',
      'coverage':'玩家胜率与交换比只用双方主力；NPC作战和地块事件另记。归属变化仅覆盖已收到或已观测范围；缺失项目不记为0',
      'capture':'翻地须有对外归属变化；原归属不明的成功占领单列，胜利战报不自动算翻地',
      'unsupported':'拆除、被拆、耐久、累计武勋和进盟时间只在取得可靠来源后计入；尚未取得时显示—'}}


def score(row, config):
    if not config.get('enabled') or not any(row.get(k)for k in ('participations','pve_samples','capture_low','capture_high','lost_land')):return None
    weights={k:v for k,v in config['weights'].items() if v}
    if any(row.get(k) is None for k in weights):return None
    return round(sum(row[k]*v for k,v in weights.items()),4)


def export_csv(result):
    out=io.StringIO(); writer=csv.writer(out)
    columns=[('player_name','玩家名'),('joined_at','进盟时间'),('group_name','分组'),('total_score','总积分'),('total_rank','总排名'),
      ('total_main_win_rate','累计主力胜率'),('total_exchange','累计主力交换比'),('score','积分'),('rank','排名'),
      ('cumulative_honor','武勋'),('new_honor','新增武勋'),('capture_low','翻地(1-4)'),('capture_high','翻地(5-9)'),
      ('demolitions','拆除'),('spartan','斯巴达场次'),('main','战场主力场次'),('main_win_rate','主力胜率'),('exchange','主力交换比'),('demolished','被拆'),
      ('lost_land','被翻地'),('capture_unknown','原归属或等级待核占领'),('siege_damage','拆除耐久'),('honor','战报武勋'),('pve_samples','NPC等非玩家作战'),('status','记录状态')]
    writer.writerow([label for _,label in columns])
    for row in result['items']:
        values=[]
        for key,_ in columns:
            value=row.get(key)
            if key=='joined_at' and value:value=datetime.fromtimestamp(value,TZ).strftime('%Y-%m-%d %H:%M:%S')
            if isinstance(value,str) and value.lstrip().startswith(('=','+','-','@')):value="'"+value
            values.append('' if value is None else value)
        writer.writerow(values)
    return ('\ufeff'+out.getvalue()).encode('utf8')
