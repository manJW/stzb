'use strict';
const $=id=>document.getElementById(id);
const escapeHtml=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const date=t=>t?new Date(t*1000).toLocaleString('zh-CN',{hour12:false}):'未知';
const number=v=>Number(v||0).toLocaleString('zh-CN');
const percent=v=>v==null?'—':(v*100).toFixed(1)+'%';
const xy=(x,y)=>x&&y?`${x},${y}`:'未知';
const outcome=v=>({win:'胜',loss:'负',draw:'平',unknown:'待解释'})[v]||'待解释';
const table=(heads,rows)=>`<table><thead><tr>${heads.map(x=>`<th>${escapeHtml(x)}</th>`).join('')}</tr></thead><tbody>${rows.join('')}</tbody></table>`;
const heroes=(list,levels=true)=>(list||[]).map(h=>escapeHtml(h.name)+(levels?` Lv.${escapeHtml(h.level??'?')}`:'')).join(' / ')||'未取得阵容';
const armyButton=id=>`<button class="row-button" data-army="${escapeHtml(id)}">${escapeHtml(id)}</button>`;
const armyEvidence=r=>r.army_id?armyButton(r.army_id)+' <span class="record-badge badge-direct">直接关联</span>':r.inferred_army_id?armyButton(r.inferred_army_id)+' <span class="record-badge">行军推定</span>':'<span class="record-badge">队号待关联</span>';
const marchEvidence=m=>({unique_attack_arrival:'行军推定，目标和到达秒数一致',ambiguous_marches:'多个行军候选，待核对',coordinate_time_match:'同队、同目标、近到达时间匹配'})[m.method]||'';
const sourcesText=list=>list?.length?list.map(s=>'<small>'+escapeHtml(s.name)+' · '+escapeHtml(date(s.observed_at))+'</small>').join(''):'<small>历史来源待核对</small>';
const reportButton=id=>`<button class="row-button" data-report="${escapeHtml(id)}">战报 ${escapeHtml(id)}</button>`;
let view='personnel',offset=0,limit=100,rows=[],requestId=0,dialogRequest=0,analysisSubject=0,readerAvailable=true;
const help={
  personnel:'按同盟和名字汇总已观测的队伍、阵容与行军。',
  operations:'查看起点、目标、计划耗时与匹配战报。',
  encounters:'查看交战时的武将、战法和兵力。队号按直接关联和行军推定标注。',
  matchups:'按阵容和攻守方汇总历史战绩，剔除 NPC、阵容不全和低兵力交战。',
  'main-matchups':'三将均至少40级，每将的3个战法均有记录。按攻守方向、武将站位和战报兵种分组。',
  'combat-analysis':'按本盟或对方视角汇总战绩。点击武将或战法，查看遇到不同对手的表现。'
};
async function api(path,body){const r=await fetch(path,body===undefined?{cache:'no-store'}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});const data=await r.json();if(!r.ok)throw new Error(data.error||`本机服务返回 ${r.status}`);return data;}
function filters(){return {relation:$('relation').value,union_id:$('intelUnion').value,search:$('intelSearch').value.trim(),army_id:$('intelArmy').value.trim(),region:$('region').value.trim(),official_campaign_id:$('campaign').value,from_at:$('fromTime').value?new Date($('fromTime').value).getTime()/1000:'',to_at:$('toTime').value?new Date($('toTime').value).getTime()/1000:'',...(['main-matchups','combat-analysis'].includes(view)?{main_scope:$('mainScope').value}:{}),...(view==='combat-analysis'?{analysis_dimension:$('analysisDimension').value,analysis_perspective:$('analysisPerspective').value,analysis_subject:analysisSubject}:{})};}
const query=(kind,f=filters(),extra={})=>'/api/intel/'+kind+'?'+new URLSearchParams({...f,...extra});
function updateAdvancedFilterCount(){
  const count=['intelUnion','intelArmy','region','fromTime','toTime'].filter(id=>$(id).value.trim()).length;
  $('advancedFilterCount').textContent=count?'（'+count+' 项）':'';
}
async function load(){const id=++requestId;updateAdvancedFilterCount();$('queryStatus').textContent='查询中…';$('viewHelp').textContent=help[view];try{const [data,s]=await Promise.all([api(query(view,filters(),{limit,offset})),api(query('summary'))]);if(id!==requestId)return;rows=data.items;render(data);$('mainCounterStats').hidden=view!=='main-matchups';if(view==='main-matchups'){$('viewHelp').textContent=mainCoverage(data.coverage);renderMainCounters(data.counter_samples);}$('namesCount').textContent=number(s.observed_names);$('armyCount').textContent=`${number(s.observed_linked_armies+s.observed_inferred_only_armies)} / ${number(s.observed_armies)}`;$('armyCoverageNote').textContent=`${number(s.observed_linked_armies)} 直接 · ${number(s.observed_inferred_only_armies)} 仅推定`;$('marchCount').textContent=number(s.marches);$('battleCount').textContent=number(s.battle_reports);$('chainCount').textContent=number(s.marches_with_report_match);$('bodyCount').textContent=number(s.full_reports);$('resultPage').textContent=`${number(data.total)} 条 · 第 ${offset/limit+1} 页`;$('prevResult').disabled=offset===0;$('nextResult').disabled=offset+rows.length>=data.total;$('queryStatus').textContent='';$('refreshedAt').textContent=`更新 ${new Date().toLocaleTimeString('zh-CN',{hour12:false})}`;}catch(err){if(id!==requestId)return;$('queryStatus').textContent=err.message;$('intelTable').innerHTML=`<div class="empty error">${escapeHtml(err.message)}</div>`;}}
function render(data){if(view==='combat-analysis'){renderCombatAnalysis(data);return;}if(!rows.length){$('intelTable').innerHTML='<div class="empty">当前条件下尚无记录。可以清除战役范围或切换阵营查询；未接收的信息会保留为未知。</div>';return;}
 if(view==='main-matchups'){$('intelTable').innerHTML=table(['本盟阵容','对方阵容','历史战绩','配置与克制'],rows.map((r,i)=>`<tr>
    <td>${mainTeam(r.own_heroes)}<small>本盟为${r.own_side==='attack'?'攻方':'守方'} · ${r.own_main?'主力':'未达主力门槛'}</small></td>
    <td>${mainTeam(r.enemy_heroes)}<small>${r.enemy_main?'主力':'未达主力门槛'}</small></td><td>${mainRecord(r)}</td>
    <td><button class="row-button" data-main-matchup="${i}">武将配置与克制</button><small>${number(r.configurations.length)} 种完整对阵配置</small>
    ${reportButton(r.latest_battle_id)}</td></tr>`));return;}
 if(view==='personnel')$('intelTable').innerHTML=table(['玩家 / 同盟','已见队号','最近已知阵容','行军 / 战报','资料时间'],rows.map((r,i)=>{
  const latest=r.lineups[0];
  const more=r.army_ids.length-3;
  return `<tr>
    <td><button class="row-button player-link" data-person="${i}">${escapeHtml(r.player_name)}</button><small>同盟 ${escapeHtml(r.union_id)} · ${number(r.lineups.length)} 份阵容</small></td>
    <td><div class="army-buttons">${r.army_ids.slice(0,3).map(armyButton).join('')||'尚未取得'}${more>0?`<button class="row-button more-armies" data-person="${i}" aria-label="查看其余 ${number(more)} 个队号">+${number(more)}</button>`:''}</div></td>
    <td>${latest?`<span class="compact-heroes">${heroes(latest.heroes)}</span><small>${armyEvidence(latest)} · ${escapeHtml(date(latest.event_at))}</small>`:'尚未取得阵容'}</td>
    <td>${number(r.march_count)} 次行军<small>${number(r.battle_ids.length)} 份战报</small></td>
    <td>${escapeHtml(date(r.latest_at))}</td>
  </tr>`;
 }));
 else if(view==='operations')$('intelTable').innerHTML=table(['玩家 / 精确队号','起点 → 目标','开始 → 计划结束','计划耗时','同队战报匹配','来源 / 观测时间'],rows.map(r=>`<tr><td>${escapeHtml(r.player_name||'身份待补')}<small>${armyButton(r.army_id)}</small></td><td>${xy(r.from_x,r.from_y)} → ${xy(r.to_x,r.to_y)}</td><td>${escapeHtml(date(r.begin_at))}<small>${escapeHtml(date(r.end_at))}</small></td><td>${number(r.duration_seconds)} 秒<small>${Number(r.straight_grids_per_hour).toFixed(2)} 格/小时（直线等效）</small></td><td>${r.battles.map(b=>`<div>${reportButton(b.battle_id)} · ${outcome(b.outcome)}<small>${marchEvidence(b)}</small></div>`).join('')||'<span class="muted">尚未建立战报关联</span>'}</td><td>${sourcesText(r.sources)}</td></tr>`));
 else if(view==='encounters')$('intelTable').innerHTML=table(['交战 / 地点','玩家 / 攻守方','队伍关联','当时武将与兵力','本侧结果 / 全文','摘要来源 / 观测时间'],rows.map(r=>`<tr><td>${reportButton(r.battle_id)}<small>${escapeHtml(date(r.event_at))} · ${xy(Math.floor(r.wid/10000),r.wid%10000)}</small>${r.campaign?'<small>'+escapeHtml(r.campaign.name)+'</small>':''}</td><td>${escapeHtml(r.player_name)}<small>${r.side==='attack'?'攻方':'守方'} · 同盟 ${escapeHtml(r.union_id)}</small></td><td>${armyEvidence(r)}<small>${r.march_links.length?'有行军匹配':'行军资料待关联'}</small></td><td><span class="compact-heroes">${heroes(r.heroes)}</span><small>战前 ${number(r.initial_hp)} 兵</small></td><td>${outcome(r.outcome)}<small>攻方结果：${escapeHtml(r.features.result_label)}<br>${r.has_full_report?'全文已保存':'已有阵容摘要，全文待补'}</small></td><td>${sourcesText(r.sources)}</td></tr>`));
 else $('intelTable').innerHTML=table(['本盟阵容 / 攻守方','对方阵容','历史样本','样本胜率 / 区间','解释'],rows.map(r=>`<tr><td><span class="compact-heroes">${heroes(r.own_heroes,false)}</span><small>本盟为${r.own_side==='attack'?'攻方':'守方'}</small></td><td><span class="compact-heroes">${heroes(r.enemy_heroes,false)}</span></td><td>${number(r.samples)} 场<small>${r.wins} 胜 / ${r.losses} 负 / ${r.draws} 平</small></td><td><span class="match-rate">${percent(r.historical_win_rate)}</span><small>95%二项区间 ${r.win_rate_interval_95.map(percent).join('～')}</small></td><td>${r.sample_status==='insufficient'?'<span class="record-badge">样本不足30场</span>':'仅描述本库样本'}<small>尚不输出未来获胜概率<br>重复交战未校正，区间可能偏乐观</small></td></tr>`));}
function openDialog(title,html){dialogRequest++;$('dialogTitle').textContent=title;$('dialogBody').innerHTML=html;if(!$('intelDialog').open)$('intelDialog').showModal();return dialogRequest;}
function openPerson(r){openDialog(r.player_name+' · 已观测队伍',`<p class="help">${escapeHtml(r.identity_policy)}。以下阵容按各自发生时间保存，未知队号不冒充精确队伍。</p><p>已观测身份：${r.identity_keys.map(escapeHtml).join(' / ')}</p><p class="army-buttons">${r.army_ids.map(armyButton).join('')||'尚未取得精确队号'}</p>${r.lineups.map(x=>`<div class="lineup-record"><strong>${armyEvidence(x)}</strong><span class="compact-heroes">${heroes(x.heroes)}</span>${x.heroes.map(h=>`<p class="help">${escapeHtml(h.name)}：${h.skills?.length?h.skills.map(s=>escapeHtml(s.name)+' '+s.level+'级').join(' / '):'战法未取得'}</p>`).join('')}<small>${escapeHtml(date(x.event_at))} · ${reportButton(x.battle_id)}</small></div>`).join('')||'<p>目前只有身份或行军资料，尚无阵容战报。</p>'}`);}
async function openArmy(id){const token=openDialog('队号 '+id+' · 行军与战报','<p>读取记录中…</p>');try{const f={...filters(),relation:'all',union_id:'',search:'',army_id:id};const [m,b,t]=await Promise.all([api(query('operations',f,{limit:500})),api(query('encounters',f,{limit:500})),api('/api/archive/army_lineups?'+new URLSearchParams({army_id:id}))]);if(token!==dialogRequest)return;$('dialogBody').innerHTML=`<p class="help">按当前时间和范围查询；清除范围可查看全赛季。时间均为游戏计划或战报发生时间。</p><h3>最近已知阵容</h3>${t.items.filter(x=>x.binding==='exact_army_id').map(x=>`<p>${heroes(x.data.heroes)}<small>${escapeHtml(date(x.event_at))}</small></p>`).join('')||'<p>尚未精确关联阵容。</p>'}<h3>行军记录 ${number(m.total)} 条</h3><div class="table-container">${table(['起点 → 目标','开始 → 计划结束','耗时','匹配战报','来源 / 观测时间'],m.items.map(x=>`<tr><td>${xy(x.from_x,x.from_y)} → ${xy(x.to_x,x.to_y)}</td><td>${escapeHtml(date(x.begin_at))}<small>${escapeHtml(date(x.end_at))}</small></td><td>${number(x.duration_seconds)} 秒</td><td>${x.battles.map(y=>reportButton(y.battle_id)+'<small>'+marchEvidence(y)+'</small>').join('')||'待关联'}</td><td>${sourcesText(x.sources)}</td></tr>`))}</div><h3>已关联的交战 ${number(b.total)} 条</h3><div class="table-container">${table(['战报','时间 / 坐标','当时阵容','本侧结果'],b.items.map(x=>`<tr><td>${reportButton(x.battle_id)}<small>${x.army_id?'直接关联':'行军推定'}</small></td><td>${escapeHtml(date(x.event_at))}<small>${xy(Math.floor(x.wid/10000),x.wid%10000)}</small></td><td><span class="compact-heroes">${heroes(x.heroes)}</span></td><td>${outcome(x.outcome)}</td></tr>`))}</div><p class="help">每类最多显示500条；全部记录可用首页筛选后导出到本机。</p>`;}catch(err){if(token===dialogRequest)$('dialogBody').textContent=err.message;}}
function reportReview(review){
  if(!review)return '';
  if(review.status==='unmatched_march')return '<p class="help">本战报尚无唯一对应的行军，待补复核证据。</p>';
  if(review.status==='forecast_missing')return '<p class="help">已找到对应行军，交战前的预判快照未留存。</p>';
  if(review.status==='partial_report'){
    const related=review.comparison_battle_id?` · <a href="/?battle_id=${encodeURIComponent(review.comparison_battle_id)}">查看本次行军的完整阵容</a>`:'';
    return `<p class="help">本战报武将站位与行军观测不完整对应，暂不计入命中统计${related}</p>`;
  }
  if(review.status==='conflicting_reports')return '<p class="help">同次行军的完整阵容战报互相冲突，待核对后统计。</p>';
  const describe=sample=>{
    const first=sample.first?sample.first.heroes.map(escapeHtml).join(' / '):'暂无候选';
    const outcome=sample.rank===1?'首选命中':sample.rank?`实际阵容排第 ${sample.rank} 名`:
      sample.candidate_count?'实际阵容未进候选':'当时没有可匹配阵容';
    const force=sample.force_assessment;
    const kind={main:'主力',unknown:'未知',spartan:'斯巴达',suspectedLow:'疑似斯巴达'}[force?.kind];
    const label=kind?`${kind}${force.map_hero_count?`（地图 ${force.map_hero_count} 将）`:''}`:'';
    const hint=force?.spartan_hint;
    const evidence=hint?` · 偏斯巴达 · 玩家对战 ${number(hint.sample_count)} 份 · ${number(hint.min_hp)}${hint.min_hp===hint.max_hp?'':` 至 ${number(hint.max_hp)}`} 兵 · ${hint.reports.map(r=>reportButton(r.battle_id)).join(' ')}`:
      force?.battle_id?` · 历史 ${number(force.initial_hp)} 兵 · ${escapeHtml(date(force.event_at))} · ${reportButton(force.battle_id)}`:'';
    const lineup={same_army:'阵容线索较强',matched_arrival:'阵容推定',uncertain:'阵容待确认'}[sample.lineup_confidence];
    const firstEvidence=sample.first?.event_at?` · ${escapeHtml(date(sample.first.event_at))}${sample.first.initial_hp==null?'':` · 历史 ${number(sample.first.initial_hp)} 兵`} · ${reportButton(sample.first.battle_id)}`:'';
    return `${escapeHtml(date(sample.issued_at))} · ${lineup?`${lineup} · `:''}${first} · ${outcome}${firstEvidence}${label?`<br>兵力判断 · ${label}${evidence}`:''}`;
  };
  const latest=review.last.issued_at!==review.first.issued_at?`<p class="help">最后一次预判 · ${describe(review.last)}</p>`:'';
  const basis=review.method==='coordinate_time_match'?'队号与抵达坐标、时间吻合':'玩家、同盟、目标与抵达时间唯一吻合';
  return `<h3>来袭预判复核</h3><p class="help">部队 ${escapeHtml(review.army_id)} · ${basis}</p>
    <p class="help">首次预判 · ${describe(review.first)}</p>${latest}`;
}
function reportBrief(brief){
  if(!brief)return '';
  return `<details><summary>战斗简报已保存（${number(brief.body.length)} 字符）</summary>
    <pre>${escapeHtml(brief.body.slice(0,20000))}</pre></details>`;
}
function reportGear(hero){
  const g=hero.gear;
  if(!g)return hero.gear_reported?'战报未列宝物':'宝物字段未取得';
  return `${escapeHtml(g.name||`宝物 #${g.id}`)} +${escapeHtml(g.level)}${g.forge_id?` · ${escapeHtml(g.forge_name||`锻造 #${g.forge_id}`)}`:''}`;
}
function reportTroop(hero){
  return hero.troop_name?`${escapeHtml(hero.troop_name)}（${escapeHtml(hero.troop_code)}）`:'兵种未取得';
}
function mainTeam(heroes){
  return heroes.map(h=>`<div>${escapeHtml(h.name)} · ${escapeHtml(h.troop_name||'兵种未取得')}</div>`).join('');
}
function mainCoverage(c){
  return `${help['main-matchups']} 共 ${number(c.reports)} 份战报，全文 ${number(c.full_reports)} 份；去重后 ${number(c.samples)} 场，其中双方兵种已知 ${number(c.known_troops)} 场、配置齐全 ${number(c.complete_builds)} 场。`;
}
function mainRecord(r){
  return `<strong class="match-rate">${percent(r.historical_win_rate)}</strong> · ${number(r.samples)} 场
    <small>${number(r.wins)} 胜 / ${number(r.losses)} 负 / ${number(r.draws)} 平</small>
    <small>95% 区间 ${r.interval_95.map(percent).join('～')}</small>`;
}
function renderCombatAnalysis(data){
  const opponent=Boolean(analysisSubject);
  const subject=data.subject_name||'当前所选';
  const side=$('analysisPerspective').value==='enemy'?'对方':'本盟';
  const label=$('analysisDimension').value==='hero'?'武将':'战法';
  $('clearAnalysisSubject').hidden=!opponent;
  $('viewHelp').textContent=opponent?`${side}使用「${subject}」时的对手表现。对照为同攻守方向、遇到其他对手的战绩。`:help['combat-analysis'];
  $('intelTable').innerHTML=rows.length?table([opponent?'遇到的'+label:label,'统计一方攻守','历史战绩','对照样本','原战报'],rows.map(r=>{
    const name=escapeHtml(r.name), base=r.baseline;
    const title=opponent?name:`<button class="row-button" data-analysis-subject="${r.id}">${name}</button>`;
    const gap=base.samples>=10&&r.samples>=10?'<small>历史差值 '+((r.historical_win_rate-base.historical_win_rate)*100).toFixed(1)+' 个百分点</small>':'';
    return `<tr><td>${title}${r.skill_type?'<small>'+escapeHtml(r.skill_type)+'</small>':''}</td>
      <td>${side} · ${r.direction==='attack'?'攻方':'守方'}</td>
      <td>${mainRecord(r)}${r.samples<10?'<small>样本少于 10 场</small>':''}</td>
      <td>${base.samples?mainRecord(base):'暂无对照'}${gap}</td><td>${reportButton(r.latest_battle_id)}</td></tr>`;
  })):'<div class="empty">当前条件下暂无符合条件的主力战报。</div>';
}
function renderMainCounters(rows){
  const labels={cowardice:'战必断金遇普攻／追击体系',hesitation:'反计之策遇多主动战法'};
  $('mainCounterTable').innerHTML='<p class="help">胜率统一按本盟视角。对照取同一方携带该战法、武将站位和攻守方向相同的其他交锋。</p>'+
    (rows.length?table(['战法与对手特征','携带方','本盟历史战绩','对照战绩','最近战报'],rows.map(r=>`<tr>
    <td>${escapeHtml(labels[r.rule]||r.rule)}</td><td>${r.holder==='own'?'本盟':'对方'}</td>
    <td>${mainRecord(r)}</td><td>${r.comparison?.samples?mainRecord(r.comparison):'暂无对照'}</td><td>${reportButton(r.battle_ids[0])}</td></tr>`)):'<p class="help">当前筛选暂无匹配克制规则的战报。</p>');
}
function mainBuild(label, name, heroes){
  return `<section><h3>${label} · ${escapeHtml(name)}</h3>${heroes.map(h=>`<p>
    <strong>${escapeHtml(h.name)} Lv.${escapeHtml(h.level)} · ${reportTroop(h)} · ${h.red_stars==null?'红度未取得':`${escapeHtml(h.red_stars)}红`}</strong>
    <small>${h.skills?.length?h.skills.map(s=>`${escapeHtml(s.name)} ${escapeHtml(s.level)}级`).join(' / '):'战法未取得'}</small>
    <small>${reportGear(h)}</small></p>`).join('')}</section>`;
}
function mainExample(r){
  const rules=[...r.own_rules.map(rule=>['本盟',rule]),...r.enemy_rules.map(rule=>['对方',rule])];
  return `<p>${reportButton(r.latest_battle_id)} · ${escapeHtml(date(r.latest_at))} · ${r.latest_has_full_report?'全文已归档':'已有战报摘要'}${r.campaign?' · '+escapeHtml(r.campaign.name):''}</p>
    <div class="report-columns">${mainBuild('本盟',r.own_player,r.own_heroes)}${mainBuild('对方',r.enemy_player,r.enemy_heroes)}</div>
    <p class="help">该场战报的克制线索</p>
    ${rules.length?rules.map(([side,rule])=>`<p class="help">${side} · ${escapeHtml(rule.text)}</p>`).join(''):'<p class="help">当前规则暂无可解释的克制线索。</p>'}`;
}
function openMainMatchup(r){
  openDialog('主力对阵 · 配置与克制',`<p>${mainRecord(r)}</p>
    <p class="help">胜率按去重后的历史战报计算，平局计入总场次。样本少时请一起看区间。</p>
    <h3>最近一场的配置与克制线索</h3>${mainExample(r)}
    <h3>按完整配置统计</h3>
    ${r.configurations.map(b=>`<details class="lineup-record"><summary>${number(b.samples)} 场 · ${percent(b.historical_win_rate)} 胜率 · ${escapeHtml(date(b.latest_at))}</summary>
    <p>${mainRecord(b)}</p>${mainExample(b)}<p>${b.battle_ids.map(reportButton).join(' ')}</p></details>`).join('')||
    '<p class="help">目前尚无双方等级、兵种、战法、红度和宝物字段均齐全的战报。</p>'}`);
}
async function openReport(id){const token=openDialog('战报 '+id,'<p>读取战报中…</p>');try{const r=await api(query('report',{}, {battle_id:id}));if(token!==dialogRequest)return;const p=r.profile;$('dialogBody').innerHTML=`<p class="help">${r.campaign?escapeHtml(r.campaign.name)+' · ':''}${escapeHtml(date(p.time))} · ${xy(Math.floor(p.wid/10000),p.wid%10000)} · 原始结果 ${escapeHtml(p.result)}${Number(p.npc)===1?' · 土地／NPC 记录':''}</p><p class="help">摘要来源${sourcesText(r.sources)}</p><div class="report-columns">${r.sides.map(s=>`<section><h3>${s.side==='attack'?'攻方':'守方'}：${escapeHtml(s.player_name)} · ${outcome(s.outcome)}</h3><p>${armyEvidence(s)}</p>${s.heroes.map(h=>`<p><strong>${escapeHtml(h.name)} Lv.${escapeHtml(h.level)} · ${h.red_stars==null?'红度待核对':`${h.red_stars}红`}</strong><small>${reportTroop(h)} · 战前 ${number(h.initial_hp)} → 战后 ${number(h.remaining_hp)} 兵</small><small>${h.skills?.length?h.skills.map(x=>escapeHtml(x.name)+' '+x.level+'级').join(' / '):'战法未取得'}</small><small>${reportGear(h)}</small></p>`).join('')}<p class="help">${s.features.binding_evidence?.source==='world_army_battle_reference'?'关联依据：地图队伍直接携带此战报编号，玩家、同盟与唯一队号匹配。':s.features.binding_evidence?'关联依据为已核验的本方战报索引。':s.inferred_army_id?'同一玩家、同盟、进攻目标和到达秒数唯一吻合。':s.army_id?'战报直接下发了队号。':'队号证据待补。'}</p>${s.march_links.map(m=>`<p class="help">${xy(m.from_x,m.from_y)} → ${xy(m.to_x,m.to_y)}<small>${escapeHtml(date(m.begin_at))} → ${escapeHtml(date(m.end_at))} · ${number(m.duration_seconds)} 秒</small><small>${marchEvidence(m)}</small>${sourcesText(m.sources)}</p>`).join('')}</section>`).join('')}</div><p><button data-export-report="${escapeHtml(id)}">导出本战报</button> ${r.full_report?'全文已持久保存'+sourcesText(r.full_report.sources):!readerAvailable?'请在游戏内打开这份战报':`<button data-read-report="${escapeHtml(id)}">补取全文</button>`}</p>${r.full_report?`<details><summary>查看已保存的战报原文（${number(r.full_report.body.length)}字符）</summary><p class="help">原文完整留存；逐回合解释尚待扩展。</p><pre>${escapeHtml(r.full_report.body.slice(0,20000))}${r.full_report.body.length>20000?'\n…界面省略余文，导出保留全文':''}</pre></details>`:'<p class="help">当前已有战报阵容摘要；全文读取结果需要客户端实际返回后才算已取得。</p>'}${reportBrief(r.brief_report)}${reportReview(r.lineup_review)}`;}catch(err){if(token===dialogRequest)$('dialogBody').textContent=err.message;}}
async function refreshConnection(){try{const [s,p]=await Promise.all([api('/api/state'),api('/api/archive/summary')]);$('connectionStatus').textContent=s.live?'采集中':'等待游戏连接';$('connectionStatus').dataset.live=String(s.live);$('identity').textContent=`${p.profile.server} · ${p.profile.account_name}`;if($('intelUnion').options.length===1)for(const u of p.unions.filter(x=>x.union_id!=='0')){const o=document.createElement('option');o.value=u.union_id;o.textContent=u.name||u.union_id;$('intelUnion').append(o);}}catch(err){$('connectionStatus').textContent='服务未连接';$('connectionStatus').dataset.live='false';}}
async function refreshCampaigns(){
  const {items}=await api(query('official-campaigns',{}));
  const select=$('campaign'), selected=select.value;
  select.innerHTML='<option value="">全赛季</option>'+items.map(c=>`<option value="${escapeHtml(c.id)}">${escapeHtml(c.name)}${c.name_verified?'':' · 名称待核对'}${c.filters.to_at?' · 已结束':''}</option>`).join('');
  select.value=selected;
}
async function refreshReader(){try{const s=await api('/api/intel/reader');readerAvailable=s.available!==false;if(!readerAvailable){$('readerStatus').textContent=s.reason;$('resumeReader').disabled=true;$('pauseReader').disabled=true;return;}const pending=s.jobs.filter(j=>['queued','reading','paused'].includes(j.state)).reduce((n,j)=>n+j.count,0);$('readerStatus').textContent=`${s.reason} · 本轮请求 ${s.requests}/${s.max_requests} · 待补 ${number(pending)} 项 · 本盟全文 ${number(s.alliance_reports.full_reports)} / ${number(s.alliance_reports.reports)}`;$('resumeReader').disabled=s.active;$('pauseReader').disabled=!s.active;}catch(err){$('readerStatus').textContent=err.message;}}
$('resumeReader').onclick=async()=>{try{await api('/api/intel/reader/resume',{max_requests:30,continuous:true});refreshReader();}catch(err){$('readerStatus').textContent=err.message;}};
$('pauseReader').onclick=async()=>{try{await api('/api/intel/reader/pause',{});refreshReader();}catch(err){$('readerStatus').textContent=err.message;}};
$('queryForm').onsubmit=event=>{event.preventDefault();offset=0;load();};
$('intelViews').onclick=event=>{
  const button=event.target.closest('[data-view]');
  if(!button)return;
  view=button.dataset.view;
  offset=0;
  $('mainScopeControls').hidden=!['main-matchups','combat-analysis'].includes(view);
  $('analysisControls').hidden=view!=='combat-analysis';
  $('mainCounterStats').hidden=true;
  $('intelViews').querySelectorAll('[data-view]').forEach(item=>{
    const active=item===button;
    item.classList.toggle('active',active);
    item.setAttribute('aria-pressed',String(active));
  });
  load();
};
$('mainScope').onchange=()=>{offset=0;load();};
$('analysisDimension').onchange=$('analysisPerspective').onchange=$('clearAnalysisSubject').onclick=()=>{
  analysisSubject=0;offset=0;load();
};
document.body.addEventListener('click',async event=>{const army=event.target.closest('[data-army]'),report=event.target.closest('[data-report]'),person=event.target.closest('[data-person]'),exp=event.target.closest('[data-export-report]'),read=event.target.closest('[data-read-report]');if(army)openArmy(army.dataset.army);else if(report)openReport(report.dataset.report);else if(person)openPerson(rows[Number(person.dataset.person)]);else if(exp){try{const r=await api('/api/intel/export',{kind:'report',battle_id:exp.dataset.exportReport});$('queryStatus').textContent='已保存：'+r.path;}catch(err){$('queryStatus').textContent=err.message;}}else if(read){read.disabled=true;try{await api('/api/intel/report/read',{battle_ids:[Number(read.dataset.readReport)]});read.textContent='已加入读取队列，稍后重新打开此战报查看';}catch(err){read.disabled=false;read.textContent=err.message;}}});
$('closeIntelDialog').onclick=()=>{dialogRequest++;$('intelDialog').close();};
$('intelTable').addEventListener('click',event=>{
  const button=event.target.closest('[data-main-matchup]');
  if(button)openMainMatchup(rows[Number(button.dataset.mainMatchup)]);
  const subject=event.target.closest('[data-analysis-subject]');
  if(subject){analysisSubject=Number(subject.dataset.analysisSubject);offset=0;load();}
});
$('prevResult').onclick=()=>{offset=Math.max(0,offset-limit);load();};$('nextResult').onclick=()=>{offset+=limit;load();};
$('allCampaigns').onclick=()=>{for(const id of ['intelSearch','intelUnion','intelArmy','region','fromTime','toTime','campaign'])$(id).value='';offset=0;load();};
$('campaign').onchange=()=>{offset=0;load();};
$('exportView').onclick=async()=>{const b=$('exportView');b.disabled=true;$('exportStatus').textContent='正在导出全部筛选记录…';try{const r=await api('/api/intel/export',{...filters(),kind:view});$('exportStatus').textContent=`已保存 ${number(r.records)} 条：${r.path}`;}catch(err){$('exportStatus').textContent=err.message;}finally{b.disabled=false;}};
async function refreshMembers(){
 try{const s=await api('/api/intel/member-sources');$('memberSources').textContent=s.items.length?s.items.map(m=>
   m.name+' · '+(!m.enabled?'已停用':!m.last_received_at?'等待首次上报':
   (Date.now()/1000-m.last_received_at<=90?'近90秒有上报':'最近上报')+' '+date(m.last_received_at))).join(' | '):'多成员试跑 · 等待受邀盟友接入';}
 catch{$('memberSources').textContent='成员来源状态暂不可用';}
}
const linkedBattle=new URLSearchParams(location.search).get('battle_id');
if(/^\d+$/.test(linkedBattle||''))openReport(linkedBattle);
refreshConnection();refreshReader();refreshMembers();refreshCampaigns().catch(err=>{$('queryStatus').textContent=err.message;});load();setInterval(refreshConnection,10000);setInterval(refreshReader,3000);setInterval(refreshMembers,10000);
setInterval(()=>{if(!document.hidden&&!offset&&['operations','encounters','main-matchups'].includes(view)&&!$('intelDialog').open&&
  !$('queryForm').contains(document.activeElement))load();},15000);
