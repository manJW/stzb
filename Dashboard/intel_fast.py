"""Page intelligence records before decoding loadouts or following march evidence."""
import json
from collections import defaultdict

from combat_loadout import side_heroes, named_hero
from observation_codec import digest, integers, number, packed, report_system_npc
from official_campaigns import in_campaign, map_block, report_campaign


def scope_sql(intel, filters, alias, *, report=False):
    unions=intel.unions(filters); params={'profile':intel.profile_id}
    if filters['relation']=='ally'and not filters['union_id']and str(intel.archive.profile['union_id'])in ('','0'):
        role=intel.archive.profile.get('battle_role_id')
        if report:
            params['personal']='battle:'+str(role)if role else ''
            return f'{alias}.player_key=:personal',params
        params['personal']='world:'+str(intel.archive.profile['account_role_id'])
        return f'{alias}.player_key=:personal',params
    if unions is None:return '1',params
    def members(values,prefix):
        names=[]
        for index,value in enumerate(sorted(values)):
            key=prefix+str(index);params[key]=value;names.append(':'+key)
        return ','.join(names) or 'NULL'
    included=f"{alias}.union_id IN ({members(unions,'union')})"
    if filters['relation']!='enemy' or filters['union_id']:return included,params
    excluded={str(intel.archive.profile['union_id'])}
    excluded.update(uid for uid,entry in intel.query_relations.items() if entry.get('relation')=='friendly'
                    or entry.get('manual_override') and entry.get('relation')!='enemy')
    force=f"CAST(json_extract({alias}.features,'$.'||{alias}.side||'er_force') AS INTEGER)" if report else 't.force_type'
    return f"({included} OR ({alias}.union_id NOT IN ({members(excluded,'excluded')}) AND {force}=1))",params


def team_key(row):
    if row.get('inferred_army_id'):return 'inferred-army:'+row['inferred_army_id']
    if row['army_id']:return 'army:'+row['army_id']
    instances=integers(row['idu_raw'])
    return 'unbound:'+digest([row['player_key'][7:],instances[1:] if any(instances[1:]) else integers(row['hero_key'])])[:24]


def facts(intel,filters,*,formations=True):
    where,params=scope_sql(intel,filters,'c',report=True)
    campaign_id=filters.get('official_campaign_id'); campaigns=intel.official_campaigns() if campaign_id else None
    selected=campaigns.get(campaign_id) if campaign_id else None
    inferred="""(SELECT m.army_id FROM battle_march_links l JOIN march_samples m ON m.id=l.march_id
       WHERE l.profile_id=c.profile_id AND l.battle_id=c.battle_id AND l.side=c.side
       AND l.method='unique_attack_arrival' ORDER BY l.march_id LIMIT 1)"""
    columns=f"""c.battle_id,c.side,c.player_key,c.player_name,c.union_id,c.army_id,c.binding,c.event_at,c.wid,c.npc,c.hero_key,
       {inferred} inferred_army_id,
       EXISTS(SELECT 1 FROM report_bodies b WHERE b.profile_id=c.profile_id AND b.battle_id=c.battle_id) has_full_report"""
    if formations:columns+=",json_extract(c.features,'$.'||c.side||'_idu') idu_raw"
    join=''
    if selected:
        columns+=",json_extract(b.data,'$.block_id') battle_block,json_extract(b.data,'$.attack_unionid') attack_union,json_extract(b.data,'$.defend_unionid') defend_union"
        join='JOIN battle_reports b ON b.profile_id=c.profile_id AND b.battle_id=c.battle_id'
    search=filters['search']
    if search:columns+=',c.heroes search_heroes,c.features search_features'
    conditions=['c.profile_id=:profile',where]
    for key,operator in (('from_at','>='),('to_at','<=')):
        if filters[key]:conditions.append('c.event_at'+operator+':'+key);params[key]=filters[key]
    if filters['army_id']:
        # Restrict to this army's report keys before evaluating arrival evidence.
        # The correlated inference check alone would scan the whole season.
        join+=''' JOIN (SELECT profile_id,battle_id,side FROM combat_sides
            WHERE profile_id=:profile AND army_id=:army
            UNION SELECT l.profile_id,l.battle_id,l.side FROM march_samples m
            JOIN battle_march_links l ON l.profile_id=m.profile_id AND l.march_id=m.id
            WHERE m.profile_id=:profile AND m.army_id=:army AND l.method='unique_attack_arrival') chosen
            ON chosen.profile_id=c.profile_id AND chosen.battle_id=c.battle_id AND chosen.side=c.side'''
        conditions.append(f'(c.army_id=:army OR {inferred}=:army)');params['army']=filters['army_id']
    if filters['region']:
        x0,y0,x1,y1=filters['region'];params.update(x0=x0,y0=y0,x1=x1,y1=y1)
        conditions.extend(['CAST(c.wid/10000 AS INTEGER) BETWEEN :x0 AND :x1','c.wid%10000 BETWEEN :y0 AND :y1'])
    result=[]
    sql=f"SELECT {columns} FROM combat_sides c {join} WHERE "+' AND '.join(conditions)+' ORDER BY c.event_at DESC,CAST(c.battle_id AS INTEGER) DESC,c.side'
    for raw in intel.db.execute(sql,params):
        row=dict(raw)
        if selected:
            row['campaign']=report_campaign(campaigns,row.pop('battle_block'),row['event_at'],
                 {str(row.pop('attack_union')),str(row.pop('defend_union'))}-{'0','None',''})
            if not row['campaign'] or row['campaign']['id']!=campaign_id:continue
        if search:
            heroes=row.pop('search_heroes');features=row.pop('search_features')
            if search not in packed([row['player_name'],row['army_id'],row['inferred_army_id'],row['battle_id']]):
                heroes=side_heroes(json.loads(heroes),row['side'],json.loads(features))
                if search not in packed([row['player_name'],row['army_id'],row['inferred_army_id'],heroes,row['battle_id']]):continue
        if formations:row['team_key']=team_key(row)
        result.append(row)
    return result


def marches(intel,filters):
    where,params=scope_sql(intel,filters,'p')
    conditions=['m.profile_id=:profile',where]
    if filters['army_id']:conditions.append('m.army_id=:army');params['army']=filters['army_id']
    if filters['from_at']:conditions.append('m.end_at>=:from_at');params['from_at']=filters['from_at']
    if filters['to_at']:conditions.append('m.begin_at<=:to_at');params['to_at']=filters['to_at']
    if filters['region']:
        x0,y0,x1,y1=filters['region'];params.update(x0=x0,y0=y0,x1=x1,y1=y1)
        conditions.append('((m.from_x BETWEEN :x0 AND :x1 AND m.from_y BETWEEN :y0 AND :y1) OR (m.to_x BETWEEN :x0 AND :x1 AND m.to_y BETWEEN :y0 AND :y1))')
    campaign=intel.official_campaigns().get(filters.get('official_campaign_id')) if filters.get('official_campaign_id') else None
    rows=intel.db.execute("""SELECT m.*,s.owner_uid,p.name player_name,p.union_id,t.force_type FROM march_samples m
      LEFT JOIN army_current a ON a.profile_id=m.profile_id AND a.army_id=m.army_id
      LEFT JOIN army_states s ON s.id=a.state_ref LEFT JOIN players p ON p.profile_id=m.profile_id AND p.player_key='world:'||s.owner_uid
      LEFT JOIN player_traits t ON t.profile_id=p.profile_id AND t.player_key=p.player_key WHERE """+' AND '.join(conditions)+' ORDER BY m.begin_at DESC,m.id',params)
    result=[]
    for raw in rows:
        row=dict(raw)
        if filters['search'] and filters['search'] not in packed([row['player_name'],row['army_id']]):continue
        if campaign and not(row['to_x'] and row['to_y'] and in_campaign(campaign,map_block(row['to_x'],row['to_y']),row['end_at'],(row['union_id'],))):continue
        result.append(row)
    return result


def enrich_facts(intel,rows):
    if not rows:return []
    full={}
    for start in range(0,len(rows),150):
        batch=rows[start:start+150];params=[]
        for row in batch:params.extend((row['battle_id'],row['side']))
        params.append(intel.profile_id)
        sql='WITH wanted(bid,side) AS (VALUES '+','.join('(?,?)' for _ in batch)+''')
           SELECT c.*,json_extract(b.data,'$.block_id') report_block,
            json_extract(b.data,'$.attack_unionid') report_attack_union,
            json_extract(b.data,'$.defend_unionid') report_defend_union
           FROM wanted w CROSS JOIN combat_sides c ON c.profile_id=? AND c.battle_id=w.bid AND c.side=w.side
           JOIN battle_reports b ON b.profile_id=c.profile_id AND b.battle_id=c.battle_id'''
        full.update({(r['battle_id'],r['side']):dict(r) for r in intel.db.execute(sql,params)})
    campaigns=intel.official_campaigns();result=[]
    for row in rows:
        item={**full[(row['battle_id'],row['side'])],**row}
        item['features']=json.loads(item['features'])
        force=item['features'].get(item['side']+'er_force')
        item['force_type']=number(force) if force is not None else None
        item['heroes']=side_heroes(json.loads(item['heroes']),item['side'],item['features'])
        intel.add_march_evidence(item)
        if 'campaign' not in item:
            item['campaign']=report_campaign(campaigns,item['report_block'],item['event_at'],
                 {str(item['report_attack_union']),str(item['report_defend_union'])}-{'0','None',''})
        for key in ('report_block','report_attack_union','report_defend_union'):item.pop(key,None)
        item.pop('idu_raw',None);result.append(item)
    return result


def enrich_marches(intel,rows):
    if not rows:return rows
    matched=defaultdict(list)
    for start in range(0,len(rows),300):
        batch=rows[start:start+300]
        for r in intel.db.execute("""SELECT l.march_id,c.battle_id,c.side,c.event_at,c.outcome,c.player_name,c.heroes,l.method
           FROM battle_march_links l JOIN combat_sides c ON c.profile_id=l.profile_id AND c.battle_id=l.battle_id AND c.side=l.side
           WHERE l.profile_id=? AND l.march_id IN ("""+','.join('?' for _ in batch)+')',(intel.profile_id,*[r['id']for r in batch])):
            item=dict(r);mid=item.pop('march_id');item['heroes']=[named_hero(h)for h in json.loads(item['heroes'])];matched[mid].append(item)
    return [{**r,'battles':matched[r['id']]}for r in rows]


def player_metadata(intel,filters):
    unions=intel.unions(filters)
    return [dict(r)for r in intel.db.execute('''SELECT p.*,t.force_type FROM players p LEFT JOIN player_traits t
        ON t.profile_id=p.profile_id AND t.player_key=p.player_key WHERE p.profile_id=?''',(intel.profile_id,))
        if intel.scope_matches(filters,unions,r['union_id'],r['force_type']) and (not filters['search'] or filters['search']in r['name'])]


def personnel(intel,filters,reports,movements,limit,offset):
    groups={}
    def person(row):
        key=(row['union_id'],row['player_name'])
        return groups.setdefault(key,dict(union_id=key[0],player_name=key[1],identity_keys=set(),army_ids=set(),battle_ids=set(),march_count=0,lineups={},latest_at=0))
    for row in reports:
        if row['side']=='defend' and row['npc']==1 and not any(integers(row['idu_raw'])):continue
        p=person(row);p['identity_keys'].add(row['player_key']);p['battle_ids'].add(row['battle_id'])
        p['latest_at']=max(p['latest_at'],row['event_at'])
        if row['hero_key']:p['lineups'].setdefault(row['team_key'],row)
        if row['army_id']:p['army_ids'].add(row['army_id'])
    for row in movements:
        if not row['player_name']:continue
        p=person(row);p['identity_keys'].add('world:'+str(row['owner_uid']));p['army_ids'].add(row['army_id'])
        p['march_count']+=1;p['latest_at']=max(p['latest_at'],row['begin_at'])
    if not any(filters[k] for k in ('region','from_at','to_at','army_id')):
        for row in player_metadata(intel,filters):
            p=person(dict(union_id=row['union_id'],player_name=row['name']))
            p['identity_keys'].add(row['player_key']);p['latest_at']=max(p['latest_at'],row['event_at'])
    ordered=sorted(groups.values(),key=lambda p:(-p['latest_at'],p['player_name']));page=ordered[offset:offset+limit]
    latest=[row for p in page for row in p['lineups'].values()]
    details={(r['battle_id'],r['side']):r for r in enrich_facts(intel,latest)}
    for p in page:
        for key in ('identity_keys','army_ids','battle_ids'):p[key]=sorted(p[key])
        if getattr(intel.archive,'report_only',False):
            from reported_archive import known_ids
            keep=known_ids(intel.db,intel.profile_id,p['army_ids'])
            p['army_ids']=[army for army in p['army_ids'] if army in keep]
        p['lineups']=[{k:details[(r['battle_id'],r['side'])][k]for k in ('team_key','army_id','inferred_army_id','heroes','binding','event_at','battle_id','npc','side')}
                      for r in p['lineups'].values() if not report_system_npc({**details[(r['battle_id'],r['side'])]['features'],'npc':r['npc']},r['side'])]
        p['identity_policy']='按同盟与名字汇总查询；各来源身份编号保留，未强制合并为同一身份'
    from team_display import attach
    labels = [dict(army_id=army,player_name=p['player_name'],union_id=p['union_id'])
              for p in page for army in p['army_ids']]
    attach(intel.archive,labels)
    lookup = {(r['union_id'],r['player_name'],r['army_id']):r for r in labels}
    for p in page:
        p['army_labels'] = [lookup[p['union_id'],p['player_name'],army] for army in p['army_ids']]
    return dict(total=len(ordered),items=page)


def summary(intel,filters,reports,movements):
    observed={r['army_id']for r in movements if r['army_id']}
    linked={r['army_id']for r in reports if r['army_id']}
    inferred={r['inferred_army_id']for r in reports if r['inferred_army_id']}
    names={(r['union_id'],r['player_name'])for r in reports+movements if r['player_name']}
    if not filters.get('official_campaign_id') and not any(filters[k]for k in ('region','from_at','to_at','army_id')):
        names.update((r['union_id'],r['name'])for r in player_metadata(intel,filters))
    matched=set()
    for start in range(0,len(movements),400):
        ids=[r['id']for r in movements[start:start+400]]
        matched.update(r[0]for r in intel.db.execute('SELECT DISTINCT l.march_id FROM battle_march_links l JOIN combat_sides c ON c.profile_id=l.profile_id AND c.battle_id=l.battle_id AND c.side=l.side WHERE l.profile_id=? AND l.march_id IN ('+','.join('?'for _ in ids)+')',(intel.profile_id,*ids)))
    return dict(observed_names=len(names),observed_armies=len(observed),linked_armies=len(linked),
        observed_linked_armies=len(observed&linked),observed_inferred_only_armies=len(observed&(inferred-linked)),
        battle_reports=len({r['battle_id']for r in reports}),marches=len(movements),marches_with_report_match=len(matched),
        full_reports=len({r['battle_id']for r in reports if r['has_full_report']}),
        all_members_verified=False,all_armies_verified=False,scope='当前赛季档案中已收录的敌我双方资料，含本机采集及已导入记录')


def read(intel,kind,filters,limit=100,offset=0):
    reports=facts(intel,filters,formations=kind!='summary') if kind in ('summary','personnel','encounters') else []
    movements=marches(intel,filters) if kind in ('summary','personnel','operations') else []
    if kind=='summary':return dict(profile_id=intel.profile_id,filters=filters,**summary(intel,filters,reports,movements))
    if kind=='personnel':result=personnel(intel,filters,reports,movements,limit,offset)
    elif kind=='encounters':result=dict(total=len(reports),items=enrich_facts(intel,reports[offset:offset+limit]))
    else:result=dict(total=len(movements),items=enrich_marches(intel,movements[offset:offset+limit]))
    if kind=='operations':
        from team_display import attach
        attach(intel.archive,result['items'])
    if kind in ('operations','encounters'):
        for item in result['items']:
            record_kind,record_id=('march',item['id']) if kind=='operations' else ('report',item['battle_id'])
            item['sources']=intel.archive.record_sources(record_kind,record_id,item['message_ref'],item['observed_at'])
            item['last_observed_at']=max((s['observed_at']for s in item['sources']),default=item['observed_at'])
    return dict(profile_id=intel.profile_id,filters=filters,limit=limit,offset=offset,**result)
