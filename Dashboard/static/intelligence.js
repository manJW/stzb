'use strict';
const $=id=>document.getElementById(id);
const e=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const date=t=>t?new Date(t*1000).toLocaleString('zh-CN',{hour12:false,timeZone:'Asia/Shanghai'}):'未知';
const n=v=>Number(v||0).toLocaleString('zh-CN');
const sourceName=s=>({world_scene:'地图观测',battle_report:'战报',alliance_roster:'成员列表',legacy_cache:'已保存观测'})[s]||s;
const xy=(x,y)=>x==null||y==null?'未知':`${x},${y}`;
let summary=null,kind='battles',offset=0,limit=100,rows=[],lastUnionKey='',requestId=0;
const helps={teams:'按资料实际发生时间维护最新配置，并保留历史。未关联队伍的阵容单独标注“待关联”，无法确认换将前后是否同一队伍；战法证据可能早于最新武将记录。',players:'人物标识按来源保留。世界人物标识与战报角色标识尚未确认关联时，会有同名身份条目；完整名单覆盖率单独核对。',marches:'保留每次行军的起终点和客户端下发的开始、结束时间。“直线等效速度”是推算指标，单位为格/小时，不能当成游戏面板速度。',battles:'个人与同盟战报统一索引。采集时间和战报发生时间分开保存，历史翻页不会把旧阵容覆盖到新阵容上。',events:'记录全部已接收的战场变化，跨重启保存；战役名称与示例坐标不限制采集。',definitions:'字段来源、更新规则与持久化策略。尚未验证的编码保留原始值。'};
async function api(path){const r=await fetch(path,{cache:'no-store'});if(!r.ok){const body=await r.json().catch(()=>null);throw new Error(body?.error||`本机服务返回 ${r.status}`);}return r.json();}
function table(headers,body){return `<table><thead><tr>${headers.map(h=>`<th>${e(h)}</th>`).join('')}</tr></thead><tbody>${body.join('')}</tbody></table>`;}
function heroText(h){return `<div class="hero-line">${e(h.name)} <span class="muted">${e(h.red_stars??'?')}红 · ${e(h.level??'?')}级</span></div>`;}
function versionHeroes(v){return heroesSummary(v.data?.heroes||[]);}
async function refreshSummary(){try{summary=await api('/api/archive/summary');const c=summary.counts,p=summary.profile;$('profileLabel').textContent=`${p.server} · ${p.season_label||p.season} · ${p.account_name}`;$('reportsCount').textContent=n(c.battle_reports);$('marchesCount').textContent=n(c.march_samples);$('fullReportsCount').textContent=n(c.report_bodies);$('storagePath').textContent=summary.database;$('updateTime').textContent=`更新 ${new Date().toLocaleTimeString('zh-CN',{hour12:false,timeZone:'Asia/Shanghai'})}`;
 const unionKey=JSON.stringify(summary.unions.map(u=>[u.union_id,u.name]));if(unionKey!==lastUnionKey){const selected=$('unionFilter').value;$('unionFilter').innerHTML='<option value="">全部已观测同盟</option>'+summary.unions.filter(u=>u.union_id!=='0').map(u=>`<option value="${e(u.union_id)}">${e(u.name||u.union_id)}</option>`).join('');$('unionFilter').value=selected;lastUnionKey=unionKey;}
 const own=summary.unions.find(u=>u.union_id===p.union_id);$('coverage').textContent=`本盟名单：${n(c.roster_members)} 条成员记录；共 ${own?n(own.observed_members):'0'} 个已观测名字${own?.expected_members?` / 名单人数 ${own.expected_members}`:'；完整名单总人数待核对'}。已保存 ${n(c.team_versions)} 条阵容版本；已核实关联 ${n(c.bound_teams)} 队；${n(c.unbound_formations)} 份阵容尚待核实队伍关联。`;
 const progress=summary.report_loading;$('reportLoading').textContent=progress?`同盟战报已收到 ${n(progress.distinct_alliance_batches)} 批；目前最早战斗 ${date(progress.oldest_battle_at)}。可继续翻页，重复记录自动合并；尚未确认已到历史末页。`:'正在读取历史采集进度…';
 const live=await api('/api/state');$('archiveStatus').textContent=live.health.archive_errors?'归档出现错误，请检查':live.live?'持续接收并归档 · '+({pc:'PC版',dashen:'大神桌面版',mumu:'MuMu'}[live.health.capture_source]||'游戏'):'等待游戏连接 · '+({pc:'PC版',dashen:'大神桌面版',mumu:'MuMu'}[live.health.capture_source]||'游戏');$('archiveStatus').classList.toggle('error',Boolean(live.health.archive_errors));$('archiveStatus').dataset.live=String(live.live&&!live.health.archive_errors);
 }catch(error){$('archiveStatus').textContent='本机服务未连接';$('archiveStatus').classList.add('error');$('archiveStatus').dataset.live='false';}}
async function load(){const id=++requestId;$('tabHelp').textContent=helps[kind];$('unionFilter').disabled=['definitions','events'].includes(kind);$('exportButton').disabled=kind==='definitions';try{
 if(kind==='definitions'){const d=await api('/api/archive/definitions');if(id!==requestId)return;rows=d.fields;$('dataTable').innerHTML=table(['字段','来源','性质','更新与保留规则'],rows.map(r=>`<tr><td>${e(r.name)}</td><td class="wrap">${e(sourceName(r.source))}</td><td>${e(r.kind)}</td><td class="wrap">${e(r.policy)}</td></tr>`));$('pageInfo').textContent='字段定义';$('previousPage').disabled=$('nextPage').disabled=true;return;}
 const q=new URLSearchParams({search:$('archiveSearch').value.trim(),union_id:$('unionFilter').value,limit:String(limit),offset:String(offset)});const data=await api(`/api/archive/${kind}?${q}`);if(id!==requestId)return;rows=data.items;$('pageInfo').textContent=`第 ${offset/limit+1} 页 · ${rows.length} 条`;$('previousPage').disabled=offset===0;$('nextPage').disabled=rows.length<limit;render();
 }catch(error){$('dataTable').innerHTML=`<div class="empty error">${e(error.message)}</div>`;}}
function render(){if(!rows.length){$('dataTable').innerHTML='<div class="empty">此筛选下尚无资料。游戏中加载名单、详情或战报后会持续补充。</div>';return;}
 if(kind==='teams'){$('dataTable').innerHTML=table(['玩家 / 同盟','队伍关联','最近已知武将','战法','配置发生时间','版本'],rows.map((r,i)=>`<tr><td>${e(r.player_name)}<small>${e(r.union_name||r.union_id)}</small></td><td><span class="badge ${r.binding==='exact_army_id'?'known':''}">${r.binding==='exact_army_id'?'已关联队伍':'队伍待关联'}</span><small>${e(heroesSummary(r.data.heroes))}</small></td><td>${r.data.heroes.map(heroText).join('')}</td><td class="wrap">${r.data.heroes.map(h=>`<div class="hero-line">${h.skills===null?'未取得战法':h.skills?.length?h.skills.map(s=>`${e(s.name)} ${e(s.level)}级`).join(' / '):'已观测为空'}</div>`).join('')}</td><td>${e(date(r.event_at))}<small>采集 ${e(date(r.observed_at))}</small></td><td><button class="detail-link" data-team="${i}">查看详情与历史 ↗</button></td></tr>`));}
 else if(kind==='players')$('dataTable').innerHTML=table(['玩家','同盟','身份来源','世界标识 / 角色标识','资料时间'],rows.map(r=>`<tr><td>${e(r.name)}</td><td>${e(r.union_name||r.union_id)}</td><td>${e(sourceName(r.source))}</td><td class="raw-code">${e(r.world_uid||'—')} / ${e(r.role_id||'—')}</td><td>${e(date(r.event_at))}</td></tr>`));
 else if(kind==='marches')$('dataTable').innerHTML=table(['玩家 / 队伍','起点 → 目标','开始 → 预计结束','计划时长','直线等效速度','兵种原始编码 / 累计样本'],rows.map(r=>`<tr><td>${e(r.player_name||'身份待关联')}<small>${e(heroesSummary(r.display_heroes))}</small></td><td>${xy(r.from_x,r.from_y)} → ${xy(r.to_x,r.to_y)}</td><td>${e(date(r.begin_at))}<small>${e(date(r.end_at))}</small></td><td>${n(Math.round(r.duration_seconds))} 秒</td><td>${Number(r.straight_grids_per_hour).toFixed(2)} 格/小时<small>直线距离 ${Number(r.straight_distance).toFixed(2)} 格</small></td><td class="raw-code">${e(r.hero_type)}<small>${n(r.samples)} 次行军样本 · 状态 ${e(r.state_code)}</small></td></tr>`));
 else if(kind==='battles')$('dataTable').innerHTML=table(['战报','战斗发生时间','地点','攻方','守方','结果编码'],rows.map(r=>`<tr><td>${e(r.battle_id)}</td><td>${e(date(r.event_at))}<small>采集 ${e(date(r.observed_at))}</small></td><td>${e(r.place_name||'未命名地点')}<small>${xy(Math.floor(r.wid/10000),r.wid%10000)}</small></td><td>${e(r.data.attack_name||'—')}<small>${e(r.data.attack_union_name||'')}</small></td><td>${e(r.data.defend_name||'—')}<small>${e(r.data.defend_union_name||'')}</small></td><td>${e(r.data.result??'未知')}</td></tr>`));
 else $('dataTable').innerHTML=table(['时间','记录类别','对象','观测内容'],rows.map(r=>`<tr><td>${e(date(r.observed_at))}</td><td>${e(({army_state:'部队状态',out_of_view:'失去观测',view_coverage:'视野范围',team_configuration_changed:'队伍配置变化',roster_member:'成员资料'})[r.kind]||r.kind)}</td><td class="raw-code">${e(['army_state','out_of_view','team_configuration_changed'].includes(r.kind)?heroesSummary(r.display_heroes):r.entity_key)}</td><td class="wrap raw-code">${e(JSON.stringify(r.data))}</td></tr>`));
}
async function openTeam(row){$('teamTitle').textContent=`${row.player_name} · ${heroesSummary(row.data.heroes)}`;$('teamBody').innerHTML=`<p class="help">${row.binding==='exact_army_id'?'已关联该队，可查看其行军。':'队伍关联证据缺失；目前是阵容档案，尚不能认定对应地图上的哪支行军队伍。'}<br>资料发生于 ${e(date(row.event_at))}。下方显示各字段证据时间。</p><div class="team-heroes">${row.data.heroes.map(h=>`<section class="hero-detail"><strong>${e(h.name)} · ${e(h.red_stars??'?')}红</strong><p>Lv.${e(h.level??'?')} · ${e(h.country||'')} · 基础兵种 ${e(h.base_troop||'未知')}</p><p>武将 ID ${e(h.id)}${h.last_known_only?' · 最近记录未直接观测此位置':''}</p>${h.skills===null?'<p>当前未取得战法</p>':(h.skills||[]).map(s=>`<div class="skill">${e(s.name)} · ${e(s.level)}级 <span class="muted">#${e(s.id)}</span></div>`).join('')}<p>等级证据 ${e(date(h.field_times?.level||row.event_at))}</p><p>战法证据 ${e(date(h.field_times?.skills))}</p><p>该战报剩余兵力 ${h.remaining_hp==null?'未知':n(h.remaining_hp)}</p></section>`).join('')}</div><p class="help">实战兵种原始编码 ${e(row.data.hero_type_raw||'未知')}；进阶编码 ${e(row.data.type_advance_raw||'未知')}。历史兵力不是当前实时兵力。</p><h3>全部已保存版本</h3><div id="teamVersions">正在读取…</div>`;$('teamDialog').showModal();try{const d=await api('/api/archive/team_history?'+new URLSearchParams({team_key:row.team_key,limit:'100'}));$('teamVersions').innerHTML=d.items.map(v=>`<div class="version">${e(versionHeroes(v))}<small>${e(date(v.event_at))} · 战报 ${e(v.battle_id)} · 版本 ${e(v.configuration_hash.slice(0,10))}</small></div>`).join('');}catch(err){$('teamVersions').textContent=err.message;}}
$('archiveViews').onclick=event=>{
  const button=event.target.closest('[data-kind]');
  if(!button)return;
  kind=button.dataset.kind;
  offset=0;
  $('archiveViews').querySelectorAll('[data-kind]').forEach(item=>{
    const active=item===button;
    item.classList.toggle('active',active);
    item.setAttribute('aria-pressed',String(active));
  });
  load();
};
$('dataTable').onclick=event=>{const button=event.target.closest('[data-team]');if(button){const row=rows[Number(button.dataset.team)];openTeam(row);if(row.data.binding_evidence){const proof=row.data.binding_evidence,note=document.createElement('p');note.className='help';note.textContent=proof.source==='world_army_battle_reference'?`队伍关联依据：地图队伍直接携带战报 ${proof.battle_id}，玩家、同盟与同一队伍匹配。证据采集于 ${date(proof.observed_at)}。`:`队伍关联依据：本方战报 ${proof.battle_id} 与地图队伍的所有者、武将和战斗坐标一致。证据采集于 ${date(proof.observed_at)}。`;$('teamBody').prepend(note);}}};
$('closeTeam').onclick=()=>$('teamDialog').close();$('closeProfile').onclick=()=>$('profileDialog').close();
$('unionFilter').onchange=()=>{offset=0;load();};let searchTimer;$('archiveSearch').oninput=()=>{clearTimeout(searchTimer);searchTimer=setTimeout(()=>{offset=0;load();},350);};
$('previousPage').onclick=()=>{offset=Math.max(0,offset-limit);load();};$('nextPage').onclick=()=>{offset+=limit;load();};$('refreshButton').onclick=()=>{refreshSummary();load();};
$('exportButton').onclick=()=>{const blob=new Blob([JSON.stringify({profile:summary?.profile,kind,items:rows},null,2)],{type:'application/json;charset=utf-8'});const url=URL.createObjectURL(blob),link=document.createElement('a');link.href=url;link.download=`stzb-${kind}-${Date.now()}.json`;link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);};
$('profileButton').onclick=()=>{if(!summary)return;for(const [key,value]of Object.entries(summary.profile)){const input=$('profileForm').elements.namedItem(key);if(input)input.value=value;}$('profileResult').textContent='';$('profileDialog').showModal();};
$('profileForm').onsubmit=async event=>{event.preventDefault();try{const r=await fetch('/api/archive/profile',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(Object.fromEntries(new FormData(event.target)))});const data=await r.json();if(!r.ok)throw new Error(data.error||'保存失败');offset=0;await refreshSummary();await load();$('profileDialog').close();}catch(err){$('profileResult').textContent=err.message;}};
let harvestBusy=false,harvestLimitTouched=false;
function renderHarvest(s){$('harvestStatus').textContent=s.reason;$('harvestProgress').textContent=`本任务已读取 ${n(s.pages)} 页 · 新增 ${n(s.new_reports)} 份 · 本轮 ${n(s.run_pages)}/${n(s.max_pages)} 页。翻页位置自动保存。`;if(!harvestLimitTouched||s.status==='running')$('harvestPages').value=s.max_pages;$('harvestStart').disabled=harvestBusy||s.status==='running';$('harvestResume').disabled=harvestBusy||!s.can_resume;$('harvestPause').disabled=harvestBusy||s.status!=='running';$('harvestPages').disabled=harvestBusy||s.status==='running';}
async function refreshHarvest(){if(harvestBusy)return;try{const status=await api('/api/harvest/status');if(status.available===false){$('harvestStatus').textContent=status.reason;for(const id of ['harvestPages','harvestStart','harvestResume','harvestPause'])$(id).disabled=true;return;}renderHarvest(status);}catch(err){$('harvestStatus').textContent='历史读取状态暂不可用';}}
async function harvestAction(action){if(harvestBusy)return;harvestBusy=true;for(const id of ['harvestStart','harvestResume','harvestPause'])$(id).disabled=true;try{const payload=action==='pause'?{}:{max_pages:Number($('harvestPages').value),max_minutes:20};const r=await fetch('/api/harvest/'+action,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});const s=await r.json();if(!r.ok)throw new Error(s.error||'操作未完成');harvestBusy=false;renderHarvest(s);refreshSummary();}catch(err){harvestBusy=false;await refreshHarvest();$('harvestStatus').textContent=err.message;}}
$('harvestStart').onclick=()=>harvestAction('start');$('harvestResume').onclick=()=>harvestAction('resume');$('harvestPause').onclick=()=>harvestAction('pause');
$('harvestPages').oninput=()=>{harvestLimitTouched=true;};

const timelineLimit=25;
const teamEventLimit=20;
const timelineFields={player:'timelinePlayer',army_id:'timelineArmy',coord:'timelineCoord',from_at:'timelineFrom',to_at:'timelineTo'};
const chinaTime={timeZone:'Asia/Shanghai'};
const dayFormat=new Intl.DateTimeFormat('zh-CN',{...chinaTime,month:'2-digit',day:'2-digit'});
const clockFormat=new Intl.DateTimeFormat('zh-CN',{...chinaTime,hour:'2-digit',minute:'2-digit',second:'2-digit',hourCycle:'h23'});
const archiveOutcomes={win:'胜',loss:'负',draw:'平'};
const associations={coordinate_time_match:'同一队伍 · 坐标和抵达时间吻合',unique_attack_arrival:'按唯一抵达记录推定',
  same_card_instances:'同一玩家的三张武将卡一致',ambiguous_marches:'存在多条可能的行军',direct:'战报已关联队伍'};
let timelinePage=0,timelineItems=[],timelineRequest=0,detailRequest=0,timelineQuery=new URLSearchParams();
const timelineTeamChoices=new Map();
let timelineChoiceProfile='';
function rememberTimelineTeams(player,items){
  const scope=JSON.stringify([summary?.profile?.server,summary?.profile?.season,summary?.profile?.account_role_id]);
  if(scope!==timelineChoiceProfile){timelineTeamChoices.clear();timelineChoiceProfile=scope;}
  for(const row of items)if(row.army_id)timelineTeamChoices.set(row.army_id,`${player.player_name} · ${heroesSummary(row.display_heroes)}`);
  const select=$('timelineArmy'),selected=select.value;
  select.innerHTML='<option value="">全部队伍</option>'+[...timelineTeamChoices].map(([id,label])=>`<option value="${e(id)}">${e(label)}</option>`).join('');
  select.value=selected;
}

function timelinePosition(x,y){return x&&y?xy(x,y):'坐标待补';}
function timelineRoute(row){
  const target=timelinePosition(row.to_x,row.to_y);
  if(row.kind!=='march')return row.place_name?`${row.place_name}（${target}）`:target;
  return `${timelinePosition(row.from_x,row.from_y)} → ${target}`;
}
function renderTimeline(){
  if(!timelineItems.length){
    $('timelineResults').innerHTML='<div class="archive-empty"><strong>没有符合条件的记录</strong><p>可缩短玩家名，或放宽坐标和时间范围。</p></div>';
    return;
  }
  $('timelineResults').innerHTML=timelineItems.map(row=>`<details class="archive-player" data-archive-player>
    <summary><span class="archive-group-name"><strong>${e(row.player_name||'玩家待核对')}</strong><small>${e(row.union_name||(row.union_id&&row.union_id!=='0'?`同盟 ${row.union_id}`:'同盟待核对'))}${row.identity&&row.player_name?' · 身份分开记录':''}</small></span>
    <span class="archive-group-count"><strong>${n(row.team_count)} 支</strong>${row.report_only_team_count?`<small>其中 ${n(row.report_only_team_count)} 支仅有战报资料</small>`:''}${row.pending_count?`<small>${n(row.pending_count)} 份战报资料待补齐</small>`:''}</span>
    <time>${e(date(row.at))}</time></summary><div class="archive-team-list"></div></details>`).join('');
  $('timelineResults').querySelectorAll('[data-archive-player]').forEach((panel,index)=>{
    panel.archivePlayer=timelineItems[index];
    panel.archiveQuery=new URLSearchParams(timelineQuery);
  });
}
function groupParams(panel,view,team=null){
  const player=panel.archivePlayer;
  const params=new URLSearchParams(panel.archiveQuery);
  params.set('view',view);
  params.set('group_name',player.player_name);
  params.set('group_union',player.union_id);
  params.set('group_identity',player.identity);
  if(team)params.set('team',team.team_ref||team.army_id||'pending');
  return params;
}
async function loadTeams(panel){
  if(panel.archiveTeams||panel.archiveLoading)return;
  panel.archiveLoading=true;
  const content=panel.querySelector('.archive-team-list');
  content.textContent='正在读取该玩家的队伍…';
  try{
    const data=await api(`/api/intel/team-log-groups?${groupParams(panel,'teams')}`);
    if(!panel.isConnected)return;
    panel.archiveTeams=data.items;
    rememberTimelineTeams(panel.archivePlayer,data.items);
    content.innerHTML=data.items.map(row=>`<details class="archive-team${row.team_ref==='pending'?' archive-team-pending':''}" data-archive-team>
      <summary><span><strong>${row.army_id||row.report_only?e(heroesSummary(row.display_heroes)):'战报资料待补齐'}</strong><small>${n(row.record_count)} 条${row.army_id?'行军或战报':'战报'}${row.report_only?' · 仅战报资料，地图队伍未对应':''}</small></span><time>${e(date(row.at))}</time></summary>
      <div class="archive-team-events"></div></details>`).join('')||'<p class="archive-muted">当前筛选下没有队伍记录。</p>';
    content.querySelectorAll('[data-archive-team]').forEach((team,index)=>{team.archiveTeam=data.items[index];});
  }catch(error){if(panel.isConnected)content.textContent=error.message;}
  finally{panel.archiveLoading=false;}
}
const formationLabels={first:'阵容记录',lineup:'换阵',changed:'配置变化'};
function eventMarkup(row,index){
  const battle=row.kind==='battle', formation=row.kind==='formation';
  const event=battle?'战报':formation?(formationLabels[row.outcome]||'阵容记录'):'行军';
  const evidence=battle?(row.has_full_report?'有全文':'战报摘要'):formation?'配置证据':'路线记录';
  const detail=battle?`${row.side==='attack'?'进攻方':'防守方'} · ${archiveOutcomes[row.outcome]||'结果待核对'}`:
    formation?'查看前后配置':row.end_at?`预计 ${clockFormat.format(row.end_at*1000)} 到达`:'行军时间待补';
  return `<button type="button" class="timeline-row" data-timeline-row="${index}">
    <span class="timeline-time"><strong>${e(dayFormat.format(row.at*1000))}</strong><small>${e(clockFormat.format(row.at*1000))}</small></span>
    <span class="timeline-person"><strong>${event}</strong><small>${e(detail)}</small></span>
    <span class="timeline-route"><strong>${e(timelineRoute(row))}</strong></span>
    <span class="timeline-evidence"><span>${e(evidence)}</span><span class="timeline-arrow" aria-hidden="true">›</span></span></button>`;
}
async function loadTeamEvents(panel){
  if(panel.archiveLoading)return;
  panel.archiveLoading=true;
  const content=panel.querySelector('.archive-team-events');
  const rows=panel.archiveRecords||[];
  const button=content.querySelector('[data-archive-more]');
  if(button)button.disabled=true;
  if(!rows.length)content.textContent='正在读取队伍时间线…';
  try{
    const playerPanel=panel.closest('[data-archive-player]');
    const params=groupParams(playerPanel,'events',panel.archiveTeam);
    params.set('limit',teamEventLimit);
    params.set('offset',rows.length);
    const data=await api(`/api/intel/team-log-groups?${params}`);
    if(!panel.isConnected)return;
    const next=rows.concat(data.items);
    panel.archiveRecords=next;
    if(!rows.length)content.innerHTML='<div class="archive-event-list"></div>';
    button?.remove();
    content.querySelector('.archive-event-list').insertAdjacentHTML('beforeend',data.items.map((row,i)=>eventMarkup(row,rows.length+i)).join(''));
    if(!next.length)content.innerHTML='<p class="archive-muted">当前筛选下没有记录。</p>';
    if(data.has_more)content.insertAdjacentHTML('beforeend','<button type="button" class="archive-more" data-archive-more>加载更早记录</button>');
  }catch(error){
    if(!panel.isConnected)return;
    if(button){button.disabled=false;button.textContent='读取失败，重试';}else content.textContent=error.message;
  }finally{panel.archiveLoading=false;}
}
async function loadTimeline(){
  const request=++timelineRequest;
  $('timelineStatus').textContent='正在读取记录…';
  $('timelineNext').disabled=$('timelinePrevious').disabled=true;
  try{
    const params=new URLSearchParams(timelineQuery);
    params.set('limit',timelineLimit);
    params.set('offset',timelinePage*timelineLimit);
    const data=await api(`/api/intel/team-log-groups?${params}`);
    if(request!==timelineRequest)return;
    timelineItems=data.items;
    renderTimeline();
    $('timelineStatus').textContent=`本页 ${n(timelineItems.length)} 名玩家 · 按最近记录排序`;
    $('timelinePage').textContent=`第 ${timelinePage+1} 页`;
    $('timelinePrevious').disabled=timelinePage===0;
    $('timelineNext').disabled=!data.has_more;
  }catch(error){
    if(request!==timelineRequest)return;
    timelineItems=[];
    $('timelineResults').innerHTML='<div class="archive-empty">查询未完成</div>';
    $('timelineStatus').textContent=error.message;
  }
}
function applyTimelineFilters(){
  timelineQuery=new URLSearchParams();
  for(const [name,id] of Object.entries(timelineFields)){
    const value=$(id).value.trim();
    if(value)timelineQuery.set(name,name.endsWith('_at')?Date.parse(`${value}+08:00`)/1000:value);
  }
  timelinePage=0;
  loadTimeline();
}
function heroesSummary(heroes){return [...(heroes||[])].sort((a,b)=>(a.position??0)-(b.position??0)).map(h=>`${h.name||'武将未核实'} ${h.red_stars==null?'?':h.red_stars}红 ${h.level??'?'}级`).join(' / ')||'武将资料未核实';}
function heroLoadout(heroes,showLosses=false){
  if(!heroes.length)return '<p class="archive-muted">没有可用的武将记录。</p>';
  return `<div class="archive-heroes">${heroes.map(h=>`<div>
    <strong>${e(h.name)}</strong><small>${e(h.level==null?'等级待补':`${h.level} 级`)} · ${e(h.troop_name||h.base_troop||'兵种待补')}${h.red_stars!=null?` · ${e(h.red_stars)} 红`:''}</small>
    ${h.skills?.length?`<ul>${h.skills.map(s=>`<li><span>${e(s.name)}</span><small>${s.level==null?'':`${e(s.level)} 级`}</small></li>`).join('')}</ul>`:`<p class="archive-muted">${Array.isArray(h.skills)?'已记录为空':'战法待补'}</p>`}
    ${h.gear?`<small>宝物 ${e(h.gear.name)}${h.gear.level!=null?` +${e(h.gear.level)}`:''}${h.gear.forge_name?` · ${e(h.gear.forge_name)}`:''}</small>`:''}
    ${showLosses&&h.remaining_hp!=null?`<small>战后 ${n(h.remaining_hp)} 兵${h.wounded_hp!=null?` · 伤兵 ${n(h.wounded_hp)}`:''}</small>`:''}
    </div>`).join('')}</div>`;
}
function battleDetail(report){
  const result=report.sides.find(side=>side.side===report.side)?.outcome;
  return `<article class="archive-battle">
    <header><div><h4>战报 ${e(report.battle_id)}${result&&archiveOutcomes[result]?` · ${archiveOutcomes[result]}`:''}</h4>
      <p>${e(date(report.event_at))} · ${e(report.place_name||'交战地')} ${e(report.coord?xy(...report.coord):'')}${associations[report.association]?` · ${e(associations[report.association])}`:''}</p></div>
      ${report.has_full_report?`<button type="button" data-battle-body="${e(report.battle_id)}">查看全文</button>`:'<span class="archive-muted">仅有摘要</span>'}</header>
    ${report.sides.map(side=>`<section class="archive-battle-side"><h5>${side.side==='attack'?'攻方':'守方'} · ${e(side.player_name||'玩家待核对')}</h5>${heroLoadout(side.heroes,true)}</section>`).join('')}
    <pre class="archive-original" hidden></pre>
  </article>`;
}
function renderTimelineDetail(data){
  if(data.kind==='formation'){
    const change=data.formation;
    return `<section class="archive-detail-section"><h3>${change.previous_heroes.length?'阵容变化':'首次阵容记录'}</h3>
      <p class="archive-muted">${e(date(change.event_at))}</p>
      ${change.previous_heroes.length?`<div class="archive-compare"><div><small>上一配置</small>${heroLoadout(change.previous_heroes)}</div><div><small>本次配置</small>${heroLoadout(change.heroes)}</div></div>`:heroLoadout(change.heroes)}
      ${change.battle_id?`<a href="/?battle_id=${encodeURIComponent(change.battle_id)}">查看来源战报 ${e(change.battle_id)} ↗</a>`:''}</section>`;
  }
  const march=data.march;
  const start=data.known_at_start;
  return `<section class="archive-detail-section"><h3>${march?'行军路线':'交战记录'}</h3>
    ${march?`<div class="archive-detail-route"><div><small>出发</small><strong>${e(timelinePosition(march.from_x,march.from_y))}</strong><time>${e(date(march.begin_at))}</time></div>
      <span aria-hidden="true">→</span><div><small>目标 · 预计到达</small><strong>${e(timelinePosition(march.to_x,march.to_y))}</strong><time>${e(date(march.end_at))}</time></div></div>
      <p class="archive-muted">计划时长 ${n(march.duration_seconds)} 秒</p>`:
      `<p class="archive-muted">${data.army_id?'已归入该队伍，本次行军路线未采到。':'这份战报单独归档，队伍与行军路线待关联。'}</p>`}
    ${data.army_id?'<button type="button" id="timelineFollowArmy">查看该队全部记录</button>':''}</section>
    <section class="archive-detail-section"><h3>${march?'出发前最近已知阵容':'交战时最近已知阵容'}</h3>
      ${start?`<p class="archive-muted">资料时间 ${e(date(start.event_at))}${start.battle_id?` · 战报 ${e(start.battle_id)}`:''}</p>${heroLoadout(start.heroes)}`:'<p class="archive-muted">当时的同队阵容证据待补。</p>'}</section>
    <section class="archive-detail-section"><h3>${march?'关联战报':'战报详情'}</h3>
      ${data.reports.length?data.reports.map(battleDetail).join(''):'<p class="archive-muted">暂无可关联的战报。</p>'}
      ${data.possible_reports.length?`<details class="archive-possible"><summary>待核对战报 · ${data.possible_reports.length} 份</summary>${data.possible_reports.map(battleDetail).join('')}</details>`:''}</section>
    <section class="archive-detail-section"><h3>同队阵容变化</h3>
      ${data.changes.length?`<p class="archive-muted">展示本条记录前后各最多 12 条阵容证据。</p>${data.changes.map(change=>`<details class="archive-change"><summary>
        <time>${e(date(change.event_at))}</time><strong>${e(heroesSummary(change.heroes))}</strong></summary>
        ${change.previous_heroes.length?`<div class="archive-compare"><div><small>上一配置</small>${heroLoadout(change.previous_heroes)}</div><div><small>本次配置</small>${heroLoadout(change.heroes)}</div></div>`:heroLoadout(change.heroes)}
        ${change.battle_id?`<a href="/?battle_id=${encodeURIComponent(change.battle_id)}">查看来源战报 ${e(change.battle_id)} ↗</a>`:''}</details>`).join('')}`:
        '<p class="archive-muted">暂无可用的同队阵容版本。</p>'}</section>`;
}
function timelineDetailHeroes(data){
  if(data.formation)return data.formation.heroes||[];
  const latest=[...(data.reports||[])].sort((a,b)=>(b.event_at||0)-(a.event_at||0)||Number(b.battle_id)-Number(a.battle_id))[0];
  if(latest)return latest.sides?.find(side=>side.side===latest.side&&side.player_name===data.player_name)?.heroes||[];
  return data.known_at_start?.heroes||[];
}
async function openTimelineDetail(row){
  const request=++detailRequest;
  $('timelineDetailTitle').textContent=row.player_name||'队伍记录';
  $('timelineDetailMeta').textContent=heroesSummary(row.display_heroes);
  $('timelineDetail').textContent='正在读取行军、阵容和战报…';
  $('timelineDialog').showModal();
  try{
    const params=new URLSearchParams({entry:row.kind,id:row.id,side:row.side||'',grouped:'1',group_identity:row.identity||''});
    const data=await api(`/api/intel/team-log-detail?${params}`);
    if(request!==detailRequest||!$('timelineDialog').open)return;
    $('timelineDetail').innerHTML=renderTimelineDetail(data);
    const roster=timelineDetailHeroes(data);
    $('timelineDetailMeta').textContent=heroesSummary(roster);
    const follow=$('timelineFollowArmy');
    if(follow)follow.onclick=()=>{
      $('timelineDialog').close();
      $('timelineFilters').reset();
      $('timelinePlayer').value=data.player_name;
      $('timelineArmy').value=data.army_id;
      applyTimelineFilters();
    };
  }catch(error){if(request===detailRequest)$('timelineDetail').textContent=error.message;}
}
$('timelineFilters').onsubmit=event=>{event.preventDefault();applyTimelineFilters();};
$('timelineClear').onclick=()=>{$('timelineFilters').reset();applyTimelineFilters();};
$('timelinePrevious').onclick=()=>{if(timelinePage>0){timelinePage--;loadTimeline();}};
$('timelineNext').onclick=()=>{timelinePage++;loadTimeline();};
$('timelineResults').onclick=event=>{
  const more=event.target.closest('[data-archive-more]');
  if(more){loadTeamEvents(more.closest('[data-archive-team]'));return;}
  const entry=event.target.closest('[data-timeline-row]');
  const row=entry&&entry.closest('[data-archive-team]').archiveRecords?.[Number(entry.dataset.timelineRow)];
  if(row)openTimelineDetail(row);
};
$('timelineResults').addEventListener('toggle',event=>{
  const panel=event.target;
  if(!panel.open)return;
  if(panel.archivePlayer)loadTeams(panel);
  else if(panel.archiveTeam&&!panel.archiveRecords)loadTeamEvents(panel);
},true);
$('timelineClose').onclick=()=>$('timelineDialog').close();
$('timelineDialog').addEventListener('close',()=>{detailRequest++;});
$('timelineDetail').onclick=async event=>{
  const button=event.target.closest('[data-battle-body]');
  if(!button)return;
  const original=button.closest('.archive-battle').querySelector('.archive-original');
  if(!original.hidden){original.hidden=true;button.textContent='查看全文';return;}
  if(original.dataset.loaded){original.hidden=false;button.textContent='收起全文';return;}
  button.disabled=true;
  original.hidden=false;
  original.textContent='正在读取已保存全文…';
  try{
    const data=await api('/api/intel/report?'+new URLSearchParams({battle_id:button.dataset.battleBody}));
    original.textContent=data.full_report?.body||'这份战报暂无全文。';
    original.dataset.loaded='true';
    button.textContent='收起全文';
  }catch(error){original.textContent=error.message;}
  finally{button.disabled=false;}
};

const initialSearch=new URLSearchParams(location.search).get('search');
if(initialSearch){$('archiveSearch').value=initialSearch;$('archiveReference').open=true;}
$('openArchiveReference').onclick=()=>{$('archiveReference').open=true;};
$('archiveReference').ontoggle=()=>{if($('archiveReference').open)load();};
refreshSummary();refreshHarvest();if(!document.getElementById('formationArchive'))applyTimelineFilters();
if($('archiveReference').open)load();
setInterval(refreshSummary,10000);
setInterval(refreshHarvest,5000);
