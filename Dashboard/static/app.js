'use strict';
let state=null, filter='all', query='', selected=null, inspectorKey='', clockOffset=0, apiConnected=false, watchIndex=0;
let selectedObservation=null;
let selectedMarch=null, alertPopupAnchor=null;
let gameLocating=false;
let alertPredictions={}, forecastKey='', forecastCheckedAt=0, forecastLoading=false, forecastFailed=false;
let campaigns=[], campaignFocus=new URLSearchParams(globalThis.location?.search).get('campaign')||'', campaignsCheckedAt=0;
const $=id=>document.getElementById(id);
const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const relationNames={self:'自己',ally:'同盟',friendly:'友好',enemy:'敌方',unknown:'未确认'};
const colors={self:'#2456bd',ally:'#276c7d',friendly:'#3173a7',enemy:'#af433c',unknown:'#60748f'};
const coord=p=>p?`${p[0]}, ${p[1]}`:'未知';
function alertTargetKey(value){
  if(!/^\d{1,4},\d{1,4}$/.test(value??''))return '';
  const point=value.split(',').map(Number);
  return point.every(n=>n>0)?point.join(','):'';
}
let alertFocus=alertTargetKey(new URLSearchParams(globalThis.location?.search).get('target'));
const focusedPoint=()=>alertFocus?alertFocus.split(',').map(Number):null;
function gameLocateButton(point,extraClass='',label='游戏内定位'){
  const key=alertTargetKey(point?.join(','));
  return state?.health.capture_source==='pc'&&key?`<button type="button" class="text-button game-locate ${extraClass}" data-game-coord="${key}">${esc(label)}</button>`:'';
}
async function locateInGame(button){
  const key=alertTargetKey(button.dataset.gameCoord);
  if(!key||gameLocating)return;
  gameLocating=true;
  button.disabled=true;
  try{
    const response=await fetch('/api/game/locate',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({coord:key.split(',').map(Number)}),signal:AbortSignal.timeout(15000)});
    const data=await response.json().catch(()=>null);
    if(!response.ok||!data?.ok)throw new Error(data?.error||
      (response.status===403?'定位请求未被本机看板接受，请刷新页面后重试':'本机看板未返回定位结果'));
    toast(`游戏已跳转到 ${coord(data.coord)}`);
  }catch(error){
    toast(error.name==='TimeoutError'?'游戏定位超时，请先查看游戏是否已跳转':error.message,true);
  }
  finally{gameLocating=false;button.disabled=false;}
}
const inRegion=(point,region)=>Boolean(point&&region?.length===4&&point[0]>=region[0]&&point[0]<=region[2]&&point[1]>=region[1]&&point[1]<=region[3]);
const activeCampaign=()=>campaigns.find(c=>c.id===campaignFocus);
const battleBlock=([x,y])=>Math.ceil(x/10)*1000+Math.ceil(y/10);
const inCampaignArea=(point,campaign)=>Boolean(point&&(campaign.current_blocks?
  campaign.current_blocks.includes(battleBlock(point)):inRegion(point,campaign.filters.region)));
function campaignNames(point,at){
  return campaigns.filter(c=>inCampaignArea(point,c)&&inCampaignTime(c.filters,at)).map(c=>c.name);
}
const inCampaignTime=(filters,at)=>(!filters.from_at||at>=filters.from_at)&&(!filters.to_at||at<=filters.to_at);
function campaignAlerts(alerts){
  const campaign=activeCampaign();
  if(!campaign)return alerts;
  const f=campaign.filters, stamp=now();
  return inCampaignTime(f,stamp)?alerts.filter(a=>inCampaignArea(a.coord,campaign)):[];
}
// In the PC battlefield view, +X goes down-right and +Y goes up-right.
function project([x,y]){
  const dx=x-map.x, dy=y-map.y;
  return {x:map.width/2+(dx+dy)*map.scale*.9,y:map.height/2+(dx-dy)*map.scale*.45};
}
function unproject(x,y){
  const horizontal=(x-map.width/2)/(map.scale*.9);
  const vertical=(y-map.height/2)/(map.scale*.45);
  return [map.x+(horizontal+vertical)/2,map.y+(horizontal-vertical)/2];
}
function frameAttackRoute(row){
  if(!row.from||!row.target)return;
  const margin=44;
  const a=project(row.from), b=project(row.target);
  const inside=p=>p.x>=margin&&p.x<=map.width-margin&&p.y>=margin&&p.y<=map.height-margin;
  if(inside(a)&&inside(b))return;
  const [x,y]=row.from, [tx,ty]=row.target;
  map.x=(x+tx)/2;
  map.y=(y+ty)/2;
  const width=Math.abs(b.x-a.x), height=Math.abs(b.y-a.y);
  map.scale=Math.max(3,map.scale*Math.min(1,
    Math.max(1,map.width-2*margin)/Math.max(1,width),
    Math.max(1,map.height-2*margin)/Math.max(1,height)));
  map.initialized=true;
}
const now=()=>Date.now()/1000+clockOffset;
const watchedTarget=()=>{const target=state.targets?.[Math.min(watchIndex,state.targets.length-1)];if(target)return target;const a=state.area,p=a?.length===4?[(a[0]+a[2])/2,(a[1]+a[3])/2]:state.armies.find(r=>r.display_position)?.display_position||[0,0];return{name:'当前视野',x:p[0],y:p[1],virtual:true};};
function duration(seconds){if(seconds==null)return '—';if(seconds<=0)return '待更新';const n=Math.floor(seconds);return `${String(Math.floor(n/3600)).padStart(2,'0')}:${String(Math.floor(n/60)%60).padStart(2,'0')}:${String(n%60).padStart(2,'0')}`;}
function ageText(seconds){return seconds<2?'刚刚':seconds<60?`${Math.floor(seconds)} 秒前`:`${Math.floor(seconds/60)} 分钟前`;}
function remaining(row){return row.stale?'观测过期':!state?.live?'数据中断':['garrison','stay'].includes(row.kind)&&row.end<=now()?'—':duration(row.end>0?row.end-now():null);}
function updateFreshness(){if(!state)return;state.live=apiConnected&&state.health.status==='live'&&now()-state.health.last_packet_at<=10;for(const r of state.armies){r.age=Math.max(0,now()-(r.observed_at||r.seen_at));r.stale=r.age>60||!r.visible||r.current_connection===false;}state.alerts=state.alerts.filter(a=>state.live&&state.armies.some(r=>r.id===a.army_id&&!r.stale&&r.age<=15&&a.end>now())).sort((a,b)=>a.end-b.end);state.counts.fresh=state.armies.filter(r=>!r.stale).length;state.counts.unmapped=state.armies.filter(r=>!r.stale&&!r.kind).length;}
function toast(text,error=false){$('toast').textContent=text;$('toast').className=`show${error?' error':''}`;clearTimeout(toast.timer);toast.timer=setTimeout(()=>$('toast').className='',3800);}
async function save(payload){const response=await fetch('/api/calibrate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});const data=await response.json();if(!response.ok||!data.ok)throw new Error(data.error||'保存失败');await refresh();toast('本地校准已保存');}
function visibleRows(){if(!state)return[];return state.armies.filter(r=>($('showHistory').checked||!r.stale)&&(filter==='all'||r.relation===filter||(filter==='ally'&&r.relation==='friendly'))&&(!query||`${r.player_name} ${r.union_name} ${r.id}`.toLowerCase().includes(query)));}
function selectedRow(){
  if(!selected)return null;
  const current=state?.armies.find(row=>row.id===selected);
  if(current)selectedObservation=current;
  return current||(selectedObservation?{...selectedObservation,stale:true}:null);
}
function setInspectorView(view, open=true, keepScroll=false){
  const details=view==='army'&&Boolean(selected);
  if(details||!open)dismissAlertPopup();
  $('armyDetails').hidden=!details;
  $('watchPanel').hidden=details;
  $('armyTab').disabled=!selected;
  $('armyTab').setAttribute('aria-pressed',String(details));
  $('watchTab').setAttribute('aria-pressed',String(!details));
  $('inspector').dataset.selected=String(Boolean(selected));
  if(!keepScroll)$('inspector').scrollTop=0;
  document.querySelector('.workspace').classList.toggle('inspector-open',open);
  $('compactWatchStatus').setAttribute('aria-expanded',String(open));
}
function selectArmy(id, incoming=null){
  dismissAlertPopup();
  if(selected!==id)selectedObservation=null;
  selected=id;
  selectedMarch=incoming?marchKey(incoming.row):null;
  alertPopupAnchor=incoming?.anchor??null;
  inspectorKey='';
  setInspectorView(id&&!incoming?'army':'watch',Boolean(id),Boolean(incoming));
  renderDetails();
  renderRoster();
  if(incoming){
    frameAttackRoute(incoming.row);
    renderWatch();
  }
  draw();
}
const rosterOrder=new Intl.Collator('zh-CN',{numeric:true});
const rosterButtons=new Map();
function renderRoster(){
  if($('rosterPane').hidden)return;
  const rows=visibleRows();
  const pinned=selectedRow();
  const matches=rows.some(row=>row.id===selected);
  $('listCount').textContent=`${rows.length} 支`;
  $('rosterSelection').hidden=!pinned;
  if(pinned){
    $('selectedPlayer').textContent=pinned.player_name;
    $('selectedNumber').textContent=`部队 ${pinned.id.slice(-6)}`;
    $('selectedStatus').textContent=pinned.stale?'历史观测':matches?pinned.state_label:'当前筛选外';
  }

  const entries=rows.filter(row=>row.id!==selected).sort((a,b)=>
    rosterOrder.compare(a.player_name,b.player_name)||rosterOrder.compare(a.id,b.id));
  const ids=new Set(entries.map(row=>row.id));
  for(const [id,button] of rosterButtons){
    if(ids.has(id))continue;
    button.remove();
    rosterButtons.delete(id);
  }
  const list=$('rosterRows');
  let next=list.firstElementChild;
  for(const row of entries){
    let button=rosterButtons.get(row.id);
    if(!button){
      button=document.createElement('button');
      button.type='button';
      button.dataset.id=row.id;
      rosterButtons.set(row.id,button);
    }
    button.className=`roster-item${row.stale?' stale':''}`;
    const position=coord(row.display_position), target=coord(row.target);
    const key=JSON.stringify([row.player_name,row.relation,row.union_name,position,target,row.state_label]);
    if(button.dataset.content!==key){
      button.innerHTML=`<div class="row-top"><span class="player-name">${esc(row.player_name)}</span><span class="relation-tag ${esc(row.relation)}">${relationNames[row.relation]}</span></div>
        <div class="row-union">${esc(row.union_name)} · 部队 ${esc(row.id.slice(-6))}</div>
        <div class="row-route"><span>${row.display_position?position:target}</span>${row.display_position&&row.target&&position!==target?`<span class="route-arrow">→</span><span>${target}</span>`:''}</div>
        <div class="row-bottom"><span>${esc(row.state_label)}</span><time></time></div>`;
      button.dataset.content=key;
    }
    updateRosterTime(button,row);
    if(button===next)next=next.nextElementSibling;
    else list.insertBefore(button,next);
  }
  const empty=$('rosterEmpty');
  empty.hidden=entries.length>0;
  if(!entries.length){
    empty.textContent=matches?'当前筛选只有已选部队':state?.counts.observed?'当前筛选下没有部队':'等待地图中的部队数据';
  }
}
function updateRosterTime(button,row){
  const timer=button.querySelector('time');
  const value=remaining(row);
  if(timer.textContent!==value)timer.textContent=value;
}
function troopDisplay(troops, label, observedAt=null){
  const chips=troops?.length
    ? troops.map(t=>`<span class="troop-slot" title="位置 ${esc(t.position)} · 编码 ${esc(t.code)}">${esc(t.name)}</span>`).join('')
    : '<span class="troop-unknown">暂未取得</span>';
  const time=observedAt?`<small>${esc(new Date(observedAt*1000).toLocaleString('zh-CN',{hour12:false}))}</small>`:'';
  return `<div class="troop-label">${esc(label)}</div><div class="troop-slots">${chips}</div>${time}`;
}
function detailFreshness(row){
  const age=ageText(Math.max(0,now()-(row.observed_at||row.seen_at)));
  return row.stale?'历史观测 · '+age:'最后观测 · '+age;
}
function unitLocation(row,showTimer){
  const point=row.display_position||row.target;
  const destination=row.display_position&&row.target&&coord(row.display_position)!==coord(row.target)?row.target:null;
  const label=row.display_position?row.position_label:row.target?'目标坐标':'坐标';
  const route=destination?`<span class="unit-route-arrow" aria-hidden="true">→</span><div><span>目标坐标</span><strong>${coord(destination)}</strong></div>`:'';
  const timer=showTimer?`<div class="unit-timer"><span>${esc(row.countdown_label)}</span><time id="detailCountdown">${remaining(row)}</time></div>`:'';
  return `<section class="unit-location" aria-label="位置与时间">
    <div class="unit-route"><div><span>${esc(label)}</span><strong>${coord(point)}</strong></div>${route}</div>
    ${timer}
    <div class="unit-location-footer"><p id="detailAge" class="unit-age">${detailFreshness(row)}</p>${gameLocateButton(destination||point)}</div>
  </section>`;
}
function renderDetails(){
  if(!state)return;
  const r=selectedRow();
  if(!r){
    if(selected){
      selected=null;
      setInspectorView('watch',false);
    }
    inspectorKey='';
    $('armyDetails').textContent='';
    return;
  }
  const showTimer=state.live&&!r.stale&&r.end>now();
  const key=[r.id,r.signature,r.relation,r.kind,r.stale,coord(r.display_position),coord(r.target),
    r.end,showTimer,state.live,state.health.capture_source,r.troops?.map(t=>t.code).join(',')].join('|');
  if(key===inspectorKey){
    const age=$('detailAge');
    if(age)age.textContent=detailFreshness(r);
    renderDefensePanel(r,$('defenseEvidence'));
    renderLineupPanel(r,$('lineupEvidence'));
    if(!$('armyDetails').hidden)renderRecentBattles($('armyBattles'),r.id);
    return;
  }
  inspectorKey=key;
  $('armyDetails').innerHTML=`
    <header class="unit-head">
      <h2>${esc(r.player_name)}</h2>
      <p>${esc(r.union_name)} · 部队 ${esc(r.id.slice(-6))}</p>
      <div class="unit-status"><span class="unit-relation ${r.relation}">${relationNames[r.relation]}</span><span>${esc(r.state_label)}</span></div>
    </header>
    ${unitLocation(r,showTimer)}
    <section id="lineupEvidence" class="lineup-evidence" aria-label="战报阵容"></section>
    <section id="armyBattles" class="lineup-evidence" aria-label="部队最近交战"></section>
    <section id="detailTroops" class="detail-troops" aria-label="部队兵种">${troopDisplay(r.troops,r.stale?'最近地图兵种':'地图兵种')}</section>
    <section id="defenseEvidence" class="lineup-evidence defense-evidence" aria-label="迎击参考" hidden></section>
    <details class="calibration-section">
      <summary>校准与依据</summary>
      <p>${r.position?'坐标来自最近地图观测。':'地图空心点标记出发坐标。'}${r.stale?' 当前为历史观测。':''}</p>
      <p>对照游戏核实状态后保存，校准将应用于相同状态标记。</p>
      <select id="stateKind" aria-label="游戏中观察到的部队状态">
        <option value="">选择实际状态</option>
        ${Object.entries(state.state_kinds).map(([k,v])=>`<option value="${k}" ${r.kind===k?'selected':''}>${esc(v)}</option>`).join('')}
      </select>
      <button id="saveState" class="button" ${r.stale?'disabled':''}>保存状态校准</button>
    </details>`;
  renderDefensePanel(r,$('defenseEvidence'));
  renderLineupPanel(r,$('lineupEvidence'));
  if(!$('armyDetails').hidden)renderRecentBattles($('armyBattles'),r.id);
  $('saveState').onclick=async()=>{
    if(!$('stateKind').value)return toast('请选择游戏中实际显示的状态',true);
    try{await save({action:'state',army_id:r.id,kind:$('stateKind').value});}catch(e){toast(e.message,true);}
  };
}
const battleOutcome={win:'胜',loss:'负',draw:'平',unknown:'结果待核对'};
const battleTroops=value=>Number.isInteger(value)?value.toLocaleString('zh-CN'):'待核对';
const battleClock=stamp=>new Date(stamp*1000).toLocaleTimeString('zh-CN',{hour12:false,timeZone:'Asia/Shanghai'});
function battleStatusMarkup(item){
  const when=esc(battleClock(item.event_at));
  const names=item.campaign?[item.campaign.name]:[];
  const evidence=item.association==='arrival'?' · 按抵达时间推定':item.association==='direct'?' · 同队号战报':'';
  return `<article class="recent-battle"><div class="lineup-meta"><time>${when}</time><span>${coord(item.coord)}${names.length?` · ${esc(names.join(' / '))}`:''}${evidence}</span><a href="/?battle_id=${encodeURIComponent(item.battle_id)}">战报 ↗</a></div>
    ${item.sides.map(side=>{
      const name=esc(side.player_name);
      const present=side.army_id&&state?.armies.some(row=>row.id===side.army_id);
      const player=present?`<button type="button" class="text-button" data-battle-army="${esc(side.army_id)}">${name}</button>`:`<strong>${name}</strong>`;
      return `<div class="recent-battle-side${side.side===item.side?' chosen':''}">${side.side==='attack'?'攻':'守'} · ${player} · ${battleOutcome[side.outcome]||battleOutcome.unknown}<br><small>战后 ${battleTroops(side.remaining_hp)} 兵 · 伤兵 ${battleTroops(side.wounded_hp)}</small>
        <details><summary>武将兵力</summary>${side.heroes.map(h=>`<p>${esc(h.name)} · ${battleTroops(h.initial_hp)} → ${battleTroops(h.remaining_hp)} · 伤兵 ${battleTroops(h.wounded_hp)}</p>`).join('')}</details></div>`;
    }).join('')}</article>`;
}
async function renderRecentBattles(panel,armyId){
  if(!panel)return;
  const scope=String(armyId);
  const changed=panel.dataset.scope!==scope;
  if(!changed&&(panel.dataset.loading==='true'||Date.now()-Number(panel.dataset.checkedAt)<4000))return;
  const request=Symbol();
  panel.recentRequest=request;
  panel.dataset.scope=scope;
  panel.dataset.loading='true';
  panel.dataset.checkedAt=String(Date.now());
  const heading='近 1 小时末次战斗';
  if(changed)panel.innerHTML=`<h3>${heading}</h3><p>读取战报中…</p>`;
  try{
    const response=await fetch('/api/intel/recent-battles?'+new URLSearchParams({army_id:armyId}),{cache:'no-store',signal:AbortSignal.timeout(4000)});
    if(!response.ok)throw new Error('战报暂不可用');
    const {items}=await response.json();
    if(!panel.isConnected||panel.dataset.scope!==scope||panel.recentRequest!==request)return;
    const content=`<h3>${heading}</h3>${items.length?items.map(battleStatusMarkup).join(''):'<p>暂无可关联的近期战报。</p>'}`;
    if(panel.dataset.rendered!==content){panel.innerHTML=content;panel.dataset.rendered=content;}
  }catch(error){
    if(panel.isConnected&&panel.dataset.scope===scope&&panel.recentRequest===request){panel.innerHTML=`<h3>${heading}</h3><p>战报暂不可用，稍后重试。</p>`;delete panel.dataset.rendered;}
  }finally{
    if(panel.recentRequest===request)panel.dataset.loading='false';
  }
}
const lineupModes={
  exact_army_id:['已关联的战报阵容','战报与当前队号有直接关联。阵容以战报时间为准。'],
  name_union_troop_candidate:['推定阵容','同盟和玩家名一致，逐位置兵种编码完全吻合。'],
  name_union_troop_family_candidate:['推定阵容','同盟和玩家名一致，逐位置兵种大类吻合。细分兵种编码有差异。'],
  name_union_candidates:['历史阵容','按同盟和玩家名列出，地图兵种暂未取得。'],
  no_troop_match:['阵容待匹配','当前兵种组合暂无吻合的历史战报。']
};
const forceNumber=value=>value.toLocaleString('zh-CN');
function reportStrength(heroes, field){
  if(!heroes.length||!heroes.every(h=>Number.isInteger(h[field])&&h[field]>=0))return '';
  const troops=heroes.reduce((sum,h)=>sum+h[field],0);
  return troops>0?`战报所列战前 ${forceNumber(troops)} 兵`:'';
}
function lineupDisplay(data){
  const [heading,basis]=lineupModes[data.match]||['战报阵容','当前尚无可关联的战报。'];
  const count=data.items.length;
  const exact=data.match==='exact_army_id';
  const items=data.items.map((item,index)=>{
    const heroes=item.data.heroes.map(h=>esc(h.name)+' '+esc(h.level??'?')+'级').join(' / ');
    const strength=reportStrength(item.data.heroes,'max_hp');
    const time=esc(new Date(item.event_at*1000).toLocaleString('zh-CN',{hour12:false}));
    const report=item.battle_id?`<a href="/?battle_id=${encodeURIComponent(item.battle_id)}">战报 ${esc(item.battle_id)} ↗</a>`:'';
    const expanded=exact&&index===0;
    return `<div class="lineup-entry">${expanded?'':`<strong>${heroes}</strong>`}${strength?`<p>${strength}</p>`:''}<div class="lineup-meta"><time>${time}</time>${report}</div>${heroLoadouts(item.data.heroes,expanded)}</div>`;
  });
  const history=exact&&count>1?`<details class="prediction-more"><summary>其他 ${count-1} 份同队号记录</summary>${items.slice(1).join('')}</details>`:'';
  return `<h3>${heading}${!exact&&count?' · '+count+' 套':''}</h3><p class="lineup-basis">${basis}</p>${exact?items[0]||'':items.join('')}${history}`;
}
function isAlert(id){return state.alerts.some(alert=>alert.army_id===id);}
function marchKey(row){
  const troops=row.troop_codes??row.troops?.map(t=>t.code)??[];
  return [row.id??row.army_id,row.begin,row.end,coord(row.from),coord(row.target),troops.join(',')].join('|');
}
function predictionFor(id){
  const prediction=alertPredictions[id];
  const army=prediction&&state.armies.find(row=>row.id===id);
  return army&&marchKey(army)===marchKey(prediction)?prediction:null;
}
async function loadAlertPredictions(){
  if(!state.alerts.length){forecastKey='';return;}
  if(forecastLoading)return;
  const ids=new Set(state.alerts.map(alert=>alert.army_id));
  const key=state.armies.filter(row=>ids.has(row.id)).map(marchKey).sort().join(';');
  if(key===forecastKey&&Date.now()-forecastCheckedAt<12000)return;
  forecastLoading=true;
  forecastKey=key;
  forecastCheckedAt=Date.now();
  try{
    const response=await fetch('/api/alert-lineups',{cache:'no-store',signal:AbortSignal.timeout(5000)});
    if(!response.ok)throw new Error('阵容分析暂不可用');
    alertPredictions=(await response.json()).predictions;
    forecastFailed=false;
  }catch(error){
    forecastFailed=true;
  }finally{
    forecastLoading=false;
    renderStatus();
    renderWatch();
    renderDetails();
    draw();
  }
}
const forceLabels={main:'主力',unknown:'未知',spartan:'斯巴达'};
const forceConfidence={linked:'同队号战报',inferred:'推定',reference:'兵力参考'};
const forceSources={same_player_exact:'同玩家、逐位置兵种吻合',same_player:'同玩家历史候选',arrival:'按抵达时间与坐标推定',direct:'队号直接关联'};
const forceReportClock=new Intl.DateTimeFormat('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false,timeZone:'Asia/Shanghai'});
const alertForce=(prediction,mapHeroCount)=>prediction?.force_assessment||
  (mapHeroCount?{kind:'spartan',map_hero_count:mapHeroCount}:{kind:'unknown'});
const mapForceBasis=count=>count?`地图 ${count} 将`:'';
const reportScene=npc=>npc===0?'玩家对战':npc===1?'打地／NPC':'';
const scoutHintSummary=hint=>`玩家对战 ${hint.sample_count} 份 · ${forceNumber(hint.min_hp)}${hint.min_hp===hint.max_hp?'':` 至 ${forceNumber(hint.max_hp)}`} 兵`;
const lineupConfidenceLabel=prediction=>({same_army:'阵容线索较强',matched_arrival:'阵容推定'})[prediction.lineup_confidence]||'阵容待确认';
const forceTitle=(kind,confidence)=>kind==='main'&&confidence==='inferred'?'推定主力':forceLabels[kind];
const skillMainBasis=assessment=>assessment.kind==='main'&&assessment.main_basis==='skills'?'每将 3 战法':'';
function forcePreview(assessment){
  const {kind,initial_hp,levels,source,event_at,confidence,map_hero_count,npc,spartan_hint}=assessment;
  if(spartan_hint)return `<small class="watch-force"><span class="watch-force-tag scout-hint">未知 · 偏斯巴达</span> ${scoutHintSummary(spartan_hint)}</small>`;
  const badge=`<span class="watch-force-tag ${kind}">${forceTitle(kind,confidence)}</span>`;
  const mapBasis=mapForceBasis(map_hero_count);
  const skillBasis=skillMainBasis(assessment);
  if(initial_hp==null&&!skillBasis)return `<small class="watch-force">${badge} ${mapBasis||'本队兵力暂无线索'}</small>`;
  const time=event_at?`<span class="watch-force-date">战报 ${esc(forceReportClock.format(event_at*1000))}${reportScene(npc)?` · ${reportScene(npc)}`:''}</span>`:'';
  const rank=levels?.map(level=>level||'—').join('/');
  const strength=initial_hp==null?'战前兵力待核对':`历史 ${forceNumber(initial_hp)} 兵`;
  const basis=[mapBasis||forceConfidence[confidence]||'战报',strength,rank&&`${rank} 级`,skillBasis].filter(Boolean).join(' · ');
  return `<small class="watch-force" title="${esc(forceSources[source]||'历史战报')}">${badge} ${basis}${time}</small>`;
}
function forceDetail(prediction){
  const assessment=alertForce(prediction);
  const {kind,battle_id,initial_hp,event_at,source,confidence,map_hero_count,npc,spartan_hint}=assessment;
  const mapBasis=mapForceBasis(map_hero_count);
  const hint=spartan_hint?`<div class="force-detail"><strong>未知 · 偏斯巴达</strong><p>${scoutHintSummary(spartan_hint)} · 同玩家、逐位置兵种吻合</p>
    ${spartan_hint.reports.map(r=>`<div class="lineup-meta"><time>${esc(forceReportClock.format(r.event_at*1000))}</time><span>${forceNumber(r.initial_hp)} 兵</span><a href="/?battle_id=${encodeURIComponent(r.battle_id)}">战报 ${esc(r.battle_id)} ↗</a></div>`).join('')}</div>`:'';
  if(!battle_id)return hint||`<p class="lineup-basis">${forceLabels[kind]} · ${mapBasis||'本队兵力暂无线索'}。</p>`;
  const report=prediction.force?.battle_id===battle_id?prediction.force:prediction.items.find(item=>item.battle_id===battle_id);
  const after=report?.remaining_hp==null?'':` · 战后 ${forceNumber(report.remaining_hp)} 兵`;
  const heroes=(report?.heroes||[]).map(h=>`${esc(h.name)} ${esc(h.level||'—')}级${h.red_stars==null?'':` · ${h.red_stars}红`}`).join(' / ');
  const time=esc(forceReportClock.format(event_at*1000));
  const title=hint?'其他历史兵力参考':`${forceTitle(kind,confidence)} · ${mapBasis||forceSources[source]||'历史战报'}`;
  const strength=initial_hp==null?'战前兵力待核对':`战前 ${forceNumber(initial_hp)} 兵`;
  const skillBasis=skillMainBasis(assessment);
  return `${hint}<div class="force-detail"><strong>${title}</strong><p>历史战报 · ${strength}${after}</p>${skillBasis?`<p>三将均 ≥40 级 · ${skillBasis}</p>`:''}<p>${heroes}</p>
    <div class="lineup-meta"><time>${time}</time><a href="/?battle_id=${encodeURIComponent(battle_id)}">战报 ${esc(battle_id)} ↗</a><span>${esc([reportScene(npc),forceSources[source]].filter(Boolean).join(' · '))}</span></div></div>`;
}
function heroLoadouts(heroes, expanded=false, showGear=true){
  if(!heroes.length)return '';
  const list=`<ul class="hero-loadouts" aria-label="武将配置">${heroes.map(h=>{
    const red=h.red_stars==null?'待核对':`${esc(h.red_stars)}红`;
    const skills=h.skills?.length?`<ul class="hero-skill-list" aria-label="战法">${h.skills.map(s=>
      `<li>${esc(s.name)}${s.level==null?'':` <span>${esc(s.level)}级</span>`}</li>`).join('')}</ul>`:
      '<span>战法未取得</span>';
    const gear=h.gear?`${esc(h.gear.name||`宝物 #${h.gear.id}`)} +${esc(h.gear.level)}${h.gear.forge_id?` · ${esc(h.gear.forge_name||`锻造 #${h.gear.forge_id}`)}`:''}`:
      h.gear_reported?'战报未列宝物':'宝物字段未取得';
    const level=h.level==null?'':` · ${esc(h.level)}级`;
    return `<li><div class="hero-loadout-title"><strong>${esc(h.name)}${level}</strong><span>红度 ${red} · ${esc(h.troop_name||'兵种未取得')}</span></div>
      ${skills}${showGear?`<div><span>武器</span> ${gear}</div>`:''}</li>`;
  }).join('')}</ul>`;
  return expanded?list:`<details class="prediction-skills"><summary>武将兵种、红度、战法与武器</summary>${list}</details>`;
}
function directAlertLoadout(prediction){
  if(!prediction?.same_army_report_id)return null;
  return prediction.items.find(item=>item.battle_id===prediction.same_army_report_id)||null;
}
function candidateReportTeam(report){
  if(report.report_army_id)return `战报队号 ${esc(report.report_army_id)}`;
  if(report.inferred_report_army_id)return `推定战报队号 ${esc(report.inferred_report_army_id)}`;
  return '';
}
function alertLoadoutMarkup(report, {candidate=false,showTime=true}={}){
  const label=candidate?(report.identity==='arrival'?'抵达吻合候选':'该玩家历史候选'):'同队号最新战报';
  const team=candidate?candidateReportTeam(report):'';
  const time=showTime&&report.event_at?`<time>${esc(forceReportClock.format(report.event_at*1000))}</time>`:'';
  const link=report.battle_id?`<a href="/?battle_id=${encodeURIComponent(report.battle_id)}">战报 ${esc(report.battle_id)} ↗</a>`:'';
  return `<div class="alert-loadout"><div class="lineup-meta"><strong>${label}</strong>${team?`<span>${team}</span>`:''}${time}${link}</div>
    ${heroLoadouts(report.heroes,true,!candidate)}</div>`;
}
const matchupScopes=[['build','同武将等级、兵种、战法、红度与宝物'],['tactics','同武将和战法等级'],['formation','同武将与站位']];
const winPercent=value=>`${(value*100).toFixed(0)}%`;
function defenseHistory(history){
  return matchupScopes.map(([key,label])=>{
    const stat=history[key];
    if(!stat.samples)return `<p>${label} · 暂无样本</p>`;
    const links=stat.battle_ids.map(id=>`<a href="/?battle_id=${encodeURIComponent(id)}">${esc(id)}</a>`).join(' · ');
    return `<p>${label} · ${stat.wins}胜 ${stat.losses}负 ${stat.draws}平<br>实测 ${winPercent(stat.historical_win_rate)} · 95%区间 ${stat.interval_95.map(winPercent).join('～')}<br>${links}</p>`;
  }).join('');
}
function defenseRecord(history){
  const strongest=matchupScopes.find(([key])=>history[key].samples);
  if(!strongest)return '历史交锋 · 暂无同阵容样本';
  const [key,label]=strongest, record=history[key];
  return `${label} · ${record.wins}胜 ${record.losses}负 ${record.draws}平 · ${winPercent(record.historical_win_rate)}`;
}
function defenseProbability(item){
  if(item.predicted_win_probability==null)return '胜率待估';
  return `估计约 ${Math.round(item.predicted_win_probability*20)*5}%`;
}
let garrisonTrialBusy=false, garrisonTrialLoading=false, garrisonTrialRevision=0;
let garrisonTrialState=null, garrisonWebError='', quickGuardAlert=null;
const garrisonActive=result=>['waiting','reading','submitting','marching','arrived','holding','returning'].includes(result?.phase);
const garrisonCancellable=result=>['waiting','reading'].includes(result?.phase);
function renderGarrisonTrial(result){
  const active=garrisonActive(result);
  $('armGarrisonTrial').disabled=active;
  $('cancelGarrisonTrial').disabled=!garrisonCancellable(result);
  const when=t=>garrisonClock.format(t*1000);
  const details=[result.team_slot&&`${result.team_slot} 队`,result.target&&`驻守点 ${coord(result.target)}`,result.enemy_name&&`触发来袭 ${result.enemy_name}`,
    result.depart_at&&`出发 ${when(result.depart_at)}`,
    result.arrived_at?`已到达 ${when(result.arrived_at)}`:result.game_arrival&&`预计到达 ${when(result.game_arrival)}`,
    result.actual_seconds&&`行军 ${duration(result.actual_seconds)}`,
    result.actual_lead!=null&&`比触发来袭预计${result.actual_lead>=0?'早':'晚'} ${Math.abs(result.actual_lead)} 秒`,
    result.return_started_at&&`开始返程 ${when(result.return_started_at)}`,
    result.returned_at?`返回起点 ${when(result.returned_at)}`:result.return_expected_at&&`预计返回 ${when(result.return_expected_at)}`];
  $('garrisonTrialStatus').textContent=result.message;
  $('garrisonTrialTimeline').textContent=details.filter(Boolean).join(' · ');
  const battle=result.return_battle, report=$('garrisonTrialReport');
  $('garrisonTrialOutcome').hidden=!battle;
  if(battle){
    const hp=battle.initial_hp!=null&&battle.remaining_hp!=null?` · 兵力 ${battle.initial_hp} → ${battle.remaining_hp}`:'';
    $('garrisonTrialCause').textContent=`${when(battle.event_at)} 在 ${coord(battle.coord)} 迎战 ${battle.attacker}，战败${hp}。`;
    report.href=`/?battle_id=${encodeURIComponent(battle.battle_id)}`;
    report.textContent=`战报 ${battle.battle_id} ↗`;
  }
}
async function updateGarrisonTrial(action,alert,team){
  if(state)$('settingsIdentityCheck').textContent=state.config.self.confirmed?'已核对':'待核对';
  if(state?.health.local_install){$('garrisonTrial').hidden=true;return;}
  if(garrisonTrialBusy||(!action&&garrisonTrialLoading))return;
  if(action){
    garrisonTrialBusy=true;
    garrisonTrialRevision++;
    garrisonWebError='';
  }else garrisonTrialLoading=true;
  const revision=garrisonTrialRevision;
  try{
    const options=action?{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({action,alert,...team}),signal:AbortSignal.timeout(10000)}:{cache:'no-store',signal:AbortSignal.timeout(4500)};
    const response=await fetch('/api/game/garrison-trial',options);
    const result=await response.json();
    if(!response.ok)throw new Error(result.error||'无法读取驻守任务');
    if(revision!==garrisonTrialRevision)return;
    if(result.requested_alert&&result.armed_at!==garrisonTrialState?.armed_at)garrisonWebError='';
    garrisonTrialState=result;
    renderGarrisonTrial(result);
  }catch(error){
    if(revision!==garrisonTrialRevision)return;
    const message=error.name==='TimeoutError'?'驻守请求超时，请等待看板刷新任务状态':error.message;
    $('garrisonTrialStatus').textContent=message;
    if(action){
      toast(message,true);
      if(action==='arm_alert'||action==='preview_alert')garrisonWebError=message;
    }else if(!garrisonTrialState){
      $('armGarrisonTrial').disabled=true;
      $('cancelGarrisonTrial').disabled=true;
    }
  }finally{
    if(action)garrisonTrialBusy=false;
    else garrisonTrialLoading=false;
    if(state)renderWatch();
  }
}
function garrisonTimingParts(result){
  const arrival=result?.arrived_at||result?.game_arrival||result?.latest_estimated_arrival||result?.preview_at;
  const arrivalText=arrival?`${result.arrived_at?'已到达':'预计到达'} ${garrisonClock.format(arrival*1000)}`:'';
  const lead=result?.actual_lead??result?.lead_seconds;
  const leadText=lead==null?'':lead>=1?`早 ${Math.round(lead)} 秒`:lead<=-1?`晚 ${Math.round(-lead)} 秒`:'几乎同时到达';
  return [result?.preview_seconds&&`行军 ${duration(result.preview_seconds)}`,arrivalText,leadText];
}
function garrisonWebSummary(){
  const result=garrisonTrialState, point=result?.requested_alert?.coord;
  const panel=$('garrisonWebStatus');
  panel.hidden=!garrisonWebError&&!point;
  if(panel.hidden)return;
  const parts=point?[`驻守 ${coord(point)}`,result.team_slot&&`${result.team_slot} 队`,result.message,
    ...garrisonTimingParts(result),result.returned_at&&`返回起点 ${garrisonClock.format(result.returned_at*1000)}`]:[];
  const message=garrisonWebError||parts.filter(Boolean).join(' · ');
  if($('garrisonWebMessage').textContent!==message)$('garrisonWebMessage').textContent=message;
  $('cancelWebGarrison').hidden=!point||!garrisonCancellable(result);
}
function garrisonAlertAction(alert,prediction){
  if(state.health.local_install||state.health.capture_source!=='pc'||prediction?.force_assessment?.kind!=='main')return '';
  const result=garrisonTrialState;
  const same=result?.enemy_id===alert.army_id&&result.enemy_arrival===alert.end&&
    result.target?.join(',')===alert.coord?.join(',');
  const active=garrisonActive(result);
  const unavailable=garrisonTrialBusy||active||!state.live||!state.reliable||alert.end-now()<=0;
  const status=same?`<small>${esc(result.message)}</small>`:'';
  if(same&&active)return `<div class="watch-garrison-action">${status}</div>`;
  return `<div class="watch-garrison-action">${status}<button type="button" class="text-button"
    data-garrison-alert="${esc(alert.army_id)}" data-garrison-alert-end="${esc(alert.end)}"
    data-garrison-alert-coord="${esc(alert.coord.join(','))}"
    title="选择本方待命队伍，预览行军时间或确认驻守"${unavailable?' disabled':''}>快捷驻守</button></div>`;
}
function quickGuardCandidates(point){
  return (state?.garrison_teams||[]).filter(team=>team.coord.join(',')!==point.join(','))
    .map(team=>({...team,distance:Math.hypot(team.coord[0]-point[0],team.coord[1]-point[1])}))
    .sort((a,b)=>a.distance-b.distance||a.slot-b.slot);
}
function currentQuickGuardAlert(){
  if(!quickGuardAlert)return null;
  return state?.alerts.find(a=>a.army_id===quickGuardAlert.army_id&&a.end===quickGuardAlert.end&&
    a.coord.join(',')===quickGuardAlert.coord.join(','));
}
function renderQuickGuard(){
  if(!$('quickGuardDialog').open||!quickGuardAlert)return;
  const alert=currentQuickGuardAlert(), point=quickGuardAlert.coord;
  const teams=quickGuardCandidates(point);
  const select=$('quickGuardTeam'), old=select.value;
  const markup='<option value="">请选择一支队伍</option>'+teams.map(team=>{
    const troops=team.troops.filter(t=>t.code>0).map(t=>t.name).join('／');
    const heroes=team.hero_names?.length
      ?`${team.hero_names.join('／')}${team.hero_source==='队伍编成'?'':`（${team.hero_source}）`}`
      :`武将待核对${troops?`（${troops}）`:''}`;
    const label=[`${team.slot} 队`,heroes,team.source_name&&`出发点「${team.source_name}」`,coord(team.coord),`约 ${team.distance.toFixed(1)} 格`]
      .filter(Boolean).join(' · ');
    return `<option value="${esc(team.id)}">${esc(label)}</option>`;
  }).join('');
  if(select.dataset.markup!==markup){
    select.innerHTML=markup;
    select.dataset.markup=markup;
    select.value=teams.some(t=>t.id===old)?old:teams.length===1?teams[0].id:'';
  }
  const result=garrisonTrialState, requested=result?.requested_alert;
  const previewForTeam=result?.preview_only&&requested?.army_id===quickGuardAlert.army_id&&
    requested.end===quickGuardAlert.end&&requested.coord.join(',')===point.join(',')&&
    (!select.value||result.team_id===select.value);
  const previewResult=$('quickGuardPreviewResult');
  previewResult.hidden=!previewForTeam;
  const previewText=previewForTeam?[result.message,...garrisonTimingParts(result)].filter(Boolean).join(' · '):'';
  if(previewResult.textContent!==previewText)previewResult.textContent=previewText;
  const active=garrisonActive(result);
  select.disabled=!alert||!state.live||!state.reliable||active||!teams.length;
  $('previewQuickGuard').disabled=select.disabled||!select.value||garrisonTrialBusy;
  $('submitQuickGuard').disabled=$('previewQuickGuard').disabled||alert.end-now()<=20;
  $('quickGuardHelp').textContent=!alert?'这条来袭已更新，请从最新卡片重新选择':
    active?'正在处理驻守任务，暂时不要操作游戏窗口':
    !teams.length?'当前地图暂无其他已核对的待命队伍':
    alert.end-now()<=20?'剩余时间不足 20 秒，当前可预览行军时间。':
    '预览只读取游戏行军时间。点「核对后驻守」会重新核对，能提前到才派出。';
}
function openQuickGuard(alert){
  quickGuardAlert=alert;
  $('quickGuardTarget').textContent=`目标 ${coord(alert.coord)} · ${alert.player_name}`;
  $('quickGuardTeam').dataset.markup='';
  $('quickGuardTeam').value='';
  $('quickGuardRecruit').checked=false;
  $('quickGuardDialog').showModal();
  renderQuickGuard();
}
const garrisonClock=new Intl.DateTimeFormat('zh-CN',{hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false,timeZone:'Asia/Shanghai'});
function garrisonPlanMarkup(alert,prediction){
  if(prediction?.force_assessment?.kind!=='main')return '';
  const plan=prediction.garrison_plan;
  const current=plan?.defender&&state.armies.find(row=>row.id===plan.army_id);
  const moment=now();
  const expiry=plan?.duration_seconds?moment+plan.duration_seconds:plan?.arrival_at;
  if(plan?.status!=='ready'||!current||current.stale||current.kind!==plan.defender.kind||
    marchKey(current)!==marchKey(plan.defender)||moment-plan.issued_at>20||
    (expiry!=null&&(expiry>=alert.end||(plan.availability==='arriving'&&expiry<=moment)))){
    return `<div class="garrison-plan"><small>${plan?'暂无可确认先到的本方队伍':'正在核对驻守队伍…'}</small></div>`;
  }
  const label={live_holding:'已在目标驻守',live_march:'客户端抵达时间',history_garrison_move:'同队号历史驻守行军',history_attack:'同队号历史普通行军'}[plan.source];
  const eta=plan.availability==='holding'?(plan.guard_since?`已驻守 ${garrisonClock.format(plan.guard_since*1000)} 起`:'已在目标驻守'):plan.duration_seconds?
    `预计 <time data-garrison-duration="${esc(plan.duration_seconds)}"></time> 到达（现在出发）`:
    `预计 ${garrisonClock.format(plan.arrival_at*1000)} 到达`;
  const lead=plan.duration_seconds?`<time data-garrison-deadline="${esc(alert.end)}" data-garrison-travel="${esc(plan.duration_seconds)}"></time>`:
    plan.guard_since||plan.arrival_at?duration(alert.end-(plan.guard_since||plan.arrival_at)):
    `<time data-garrison-deadline="${esc(alert.end)}" data-garrison-travel="0"></time>`;
  const matchup=plan.matchup, evidence=matchup?.probability_evidence;
  const win=matchup?.predicted_win_probability==null?'胜率待估':`迎击胜率 ${defenseProbability(matchup)}`;
  const model=evidence?.method==='same_build'?`同配置 ${evidence.lineup_cases} 场`:
    evidence?.method==='similar_main'?[evidence.lineup_cases?`相近阵容 ${evidence.lineup_cases} 场`:'相似交锋粗估',
      evidence.troop_cases?`兵力比例相近 ${evidence.troop_cases} 场`:''].filter(Boolean).join(' · '):
    matchup?'样本不足':'缺本队匹配战报';
  const report=plan.enemy_battle_id?`<a href="/?battle_id=${encodeURIComponent(plan.enemy_battle_id)}">${plan.enemy_identity==='direct'?'敌方同队号战报':'敌阵候选战报'} ↗</a>`:'';
  const basis=plan.speed_samples?`${label} ${plan.speed_samples} 次 · 预留 ${plan.buffer_seconds} 秒确认`:label;
  return `<div class="garrison-plan"><strong>驻守建议</strong>
    <strong>${esc(plan.player_name)} · 队 ${esc(plan.army_id)}</strong><p>${eta} · 提前 ${lead} · ${esc(win)}</p>
    <small>${esc(basis)} · ${esc(model)}</small>${matchup?.rules?.length?`<small>${esc(matchup.rules[0].text)}</small>`:''}
    ${report?`<div class="lineup-meta">${report}</div>`:''}</div>`;
}
function defenseReadiness(item){
  const label={holding:'已在目标驻守',arriving:'已在赶往目标，预计先抵达',late:'赶往目标，预计晚于敌军抵达',
    changed:'当前兵种与历史战报不同',observed:'已观察到队伍，能否及时到达待核对',unknown:'当前位置和能否及时到达待核对'};
  return label[item.availability]||label.unknown;
}
function defenseLead(data){
  if(data.low_force)return '<p class="defense-lead">最近同队号战报为低兵力，主力留给其他来袭。</p>';
  const pick=data.recommendation;
  if(!pick)return '<p class="defense-lead">暂无可点名的迎击队伍。</p>';
  const item=pick.item, enemy=data.hypotheses[pick.hypothesis_index];
  const source=enemy.identity==='direct'?'同队号最新战报':`历史候选 ${pick.hypothesis_index+1}`;
  const ready=['holding','arriving'].includes(item.availability);
  const evidence=item.probability_evidence;
  const probabilityBasis=evidence?.method==='similar_main'?`按双方历史配置估计 · ${evidence.strength_only?'跨阵容强度粗估':`${evidence.lineup_cases} 场相近本盟阵容`}${evidence.skill_cases?` · ${evidence.skill_cases} 场双方战法相近`:''}${evidence.troop_cases?` · 参考 ${evidence.troop_cases} 场兵力比例相近的交锋`:''}`:
    evidence?'双方沿用所列配置时估计':'可对照下方战绩与克制依据';
  const basis=item.rules[0]?.text||defenseRecord(item.history);
  const impact=data.arrival_at?` · 敌军预计 ${esc(new Date(data.arrival_at*1000).toLocaleTimeString('zh-CN',{hour12:false}))} 到` : '';
  return `<div class="defense-lead"><small>${ready?'到位线索':'优先核对'} · ${source}${impact}</small>
    <strong>${esc(item.player_name)} · ${esc(item.army_id?`队 ${item.army_id}`:'队号待确认')}</strong>
    <p>${item.heroes.map(h=>esc(h.name)).join(' / ')}</p><strong>${defenseProbability(item)}</strong>
    <p>${defenseReadiness(item)}${item.position?` · ${coord(item.position)}`:''}</p><p>${probabilityBasis}</p><p>${esc(basis)}</p></div>`;
}
function defenseDisplay(data){
  const title='<h3>迎击参考</h3>';
  if(!data.active)return `${title}<p>本次来袭已结束或视野已更新。</p>`;
  if(data.map_hero_count)return `${title}<p>${mapForceBasis(data.map_hero_count)} · 斯巴达。</p>`;
  if(!data.hypotheses.length)return `${title}<p>敌方阵容待确认，暂无可匹配的迎击方案。</p>`;
  const note=`<p>按敌方候选分别计算。使用本赛季双方主力的 ${data.historical_cases} 场历史守方交锋，平局计入分母。</p>`;
  const firstUseful=data.recommendation?.hypothesis_index??Math.max(0,data.hypotheses.findIndex(hyp=>hyp.items.length));
  return title+defenseLead(data)+note+data.hypotheses.map((hyp,index)=>`<details class="defense-hypothesis" ${index===firstUseful?'open':''}>
    <summary>${hyp.identity==='direct'?'同队号最新战报':`敌方候选 ${index+1}`} · ${hyp.heroes.map(h=>esc(h.name)).join(' / ')}</summary>
    ${hyp.items.map(item=>`<div class="lineup-entry"><strong>${esc(item.player_name)} · ${item.heroes.map(h=>esc(h.name)).join(' / ')}</strong>
      <p>${reportStrength(item.heroes,'initial_hp')||'历史兵力待核对'} · ${item.position?`当前观测位置 ${coord(item.position)} · 直线约 ${item.distance} 格`:'历史本盟阵容 · 当前位置待核对'}</p>
      <p>${defenseRecord(item.history)}</p>
      <p>${defenseProbability(item)}${item.probability_evidence?.strength_only?' · 跨阵容强度粗估':''} · ${defenseReadiness(item)}</p>
      ${item.rules.map(rule=>`<p>${esc(rule.text)}</p>`).join('')}${item.risks.map(rule=>`<p>反向风险 · ${esc(rule.text)}</p>`).join('')}
      <div class="lineup-meta"><time>${esc(new Date(item.event_at*1000).toLocaleString('zh-CN',{hour12:false}))}</time><a href="/?battle_id=${encodeURIComponent(item.battle_id)}">本盟配置战报 ↗</a></div>
      <details class="prediction-skills"><summary>历史交锋战绩</summary>${defenseHistory(item.history)}</details>${heroLoadouts(item.heroes)}</div>`).join('')||'<p>本盟档案里暂缺可比较战绩或明确的克制线索。</p>'}</details>`).join('');
}
async function renderDefensePanel(row,panel){
  if(!panel)return;
  if(!isAlert(row.id)){
    panel.hidden=true;
    delete panel.dataset.key;
    delete panel.dataset.rendered;
    return;
  }
  const key=marchKey(row);
  const changed=panel.dataset.key!==key;
  if(!changed&&(panel.dataset.loading===key||Date.now()-Number(panel.dataset.checkedAt)<12000))return;
  panel.hidden=false;
  panel.dataset.key=key;
  panel.dataset.loading=key;
  panel.dataset.checkedAt=String(Date.now());
  if(changed){
    panel.innerHTML='<h3>迎击参考</h3><p>正在对照本盟阵容和历史交锋…</p>';
    delete panel.dataset.rendered;
  }
  try{
    const response=await fetch('/api/alert-defense?'+new URLSearchParams({army_id:row.id}),{cache:'no-store',signal:AbortSignal.timeout(6000)});
    if(!response.ok)throw new Error('暂不可用');
    const data=await response.json();
    if(panel.isConnected&&panel.dataset.key===key){
      const markup=defenseDisplay(data);
      if(panel.dataset.rendered!==markup){panel.innerHTML=markup;panel.dataset.rendered=markup;}
    }
  }catch(error){
    if(panel.isConnected&&panel.dataset.key===key){
      panel.innerHTML='<h3>迎击参考</h3><button class="button" type="button">重试迎击分析</button>';
      delete panel.dataset.rendered;
      panel.querySelector('button').onclick=()=>{delete panel.dataset.key;renderDefensePanel(row,panel);};
    }
  }finally{
    if(panel.dataset.loading===key)delete panel.dataset.loading;
  }
}
function predictionPreview(id,mapHeroCount,prediction=predictionFor(id)){
  const force=prediction||mapHeroCount?forcePreview(alertForce(prediction,mapHeroCount)):'';
  if(!prediction)return `${force}<small class="watch-prediction">${forecastFailed?'阵容分析暂不可用':'正在分析阵容…'}</small>`;
  const [first]=prediction.items;
  if(!first)return `${force}<small class="watch-prediction">暂无该玩家的匹配战报</small>`;
  const label=first.identity==='direct'?'同队号最新战报':first.identity==='arrival'?'抵达吻合候选':'该玩家历史候选';
  const others=prediction.items.length-1;
  const evidence=[reportScene(first.npc),first.initial_hp==null?'':`历史 ${forceNumber(first.initial_hp)} 兵`,
    first.event_at?`战报 ${forceReportClock.format(first.event_at*1000)}`:''].filter(Boolean).join(' · ');
  const names=`<span class="watch-heroes">${first.heroes.map(h=>esc(h.name)).join(' / ')}</span>`;
  return `${force}<span class="watch-lineup"><small class="watch-prediction">${lineupConfidenceLabel(prediction)} · ${label}${others?` · 另 ${others} 套`:''}</small>
    ${names}${evidence?`<small>${esc(evidence)}</small>`:''}</span>`;
}
function mapLineupText(prediction){
  const first=prediction?.items[0];
  if(!first)return [prediction?'暂无可匹配阵容':'阵容待分析','兵力待核对 · 阵容待确认'];
  const names=first.heroes.map(hero=>hero.name).join(' / ');
  const strength=first.initial_hp==null?'兵力待核对':`历史 ${forceNumber(first.initial_hp)} 兵`;
  const detail=[strength,first.identity==='direct'?'同队号最新战报':'历史候选'].join(' · ');
  return [names,detail];
}
function mapLabelBox(point,width,height,bounds,blocked){
  const gap=22;
  const x=Math.max(bounds.left,Math.min(point.x-width/2,bounds.right-width));
  const y=Math.max(bounds.top,Math.min(point.y-height/2,bounds.bottom-height));
  const positions=[[point.x-gap-width,y],[point.x+gap,y],[x,point.y-gap-height],[x,point.y+gap]];
  for(const [left,top] of positions){
    const box={left,top,right:left+width,bottom:top+height};
    if(box.left<bounds.left||box.top<bounds.top||box.right>bounds.right||box.bottom>bounds.bottom)continue;
    if(!blocked.some(other=>box.left<other.right&&box.right>other.left&&box.top<other.bottom&&box.bottom>other.top))return box;
  }
  return null;
}
function selectedIncoming(alerts){
  if(!selectedMarch)return null;
  const alert=alerts.find(item=>item.army_id===selected&&item.end>now());
  const row=alert&&state.armies.find(item=>item.id===selected);
  return row&&marchKey(row)===selectedMarch?{alert,row}:null;
}
function alertPopupPosition(anchor, width, height, viewportWidth, viewportHeight){
  const gap=12, edge=8;
  const x=anchor.x+gap+width<=viewportWidth-edge?anchor.x+gap:anchor.x-gap-width;
  const y=anchor.y+gap+height<=viewportHeight-edge?anchor.y+gap:anchor.y-gap-height;
  return {left:Math.max(edge,Math.min(x,viewportWidth-width-edge)),top:Math.max(edge,Math.min(y,viewportHeight-height-edge))};
}
function dismissAlertPopup(restoreFocus=false){
  const panel=$('alertPopup');
  const returnToCard=restoreFocus&&!panel.hidden&&panel.contains(document.activeElement);
  alertPopupAnchor=null;
  panel.hidden=true;
  if(returnToCard)$('watchPanel').querySelector(`[data-watch-id="${CSS.escape(selected)}"]`)?.focus({preventScroll:true});
}
function renderAlertPopup(incoming){
  if(!alertPopupAnchor)return;
  if(!incoming){dismissAlertPopup(true);return;}
  const {row,alert}=incoming, panel=$('alertPopup'), body=$('alertPopupBody');
  const troops=row.troops?.filter(t=>t.code>0).map(t=>t.name).join(' / ');
  $('alertPopupTitle').textContent=row.player_name;
  $('alertPopupCountdown').textContent=duration(alert.end-now());
  const locate=$('alertPopupLocate');
  locate.hidden=state.health.capture_source!=='pc';
  locate.dataset.gameCoord=alert.coord.join(',');
  const markup=`<p class="alert-popup-route">${coord(row.from)} → ${coord(alert.coord)}</p>
    <p>${esc(row.union_name||'同盟未知')} · ${esc(troops||'兵种未知')}</p>${predictionPreview(row.id,alert.map_hero_count)}`;
  if(body.dataset.markup!==markup){body.innerHTML=markup;body.dataset.markup=markup;}
  panel.hidden=false;
  const {left,top}=alertPopupPosition(alertPopupAnchor,panel.offsetWidth,panel.offsetHeight,window.innerWidth,window.innerHeight);
  panel.style.left=`${left}px`;
  panel.style.top=`${top}px`;
}
function predictionDisplay(prediction){
  const {items,speed}=prediction;
  const linked=directAlertLoadout(prediction);
  const metric=speed?`直线等效 ${speed.grids_per_hour.toFixed(1)} 格/小时 · ${speed.distance.toFixed(1)} 格 · 用时 ${duration(speed.seconds)}`:'本次行军缺少完整起止数据';
  const note=prediction.last_army_conflict?'<p>同队号最近战报的兵种与本次不同，可能已换阵。</p>':'';
  const candidates=items.filter(item=>item!==linked).map((item,index)=>{
    const troops={exact:'逐位置兵种编码吻合',family:'逐位置兵种大类吻合',unknown:'缺少可比较的兵种'};
    const team=candidateReportTeam(item)||'战报未记队号';
    const basis=[team,item.identity==='arrival'?'历史抵达吻合':'',troops[item.troop_match]].filter(Boolean).join(' · ');
    const ref=item.speed;
    const speedBasis=ref?`历史 ${ref.samples} 次 · 中位数 ${ref.median.toFixed(1)} 格/小时 · 本次相差 ${Math.abs(ref.delta_pct).toFixed(1)}%`:'';
    const heroes=item.heroes.map(h=>esc(h.name)+(h.level?` ${esc(h.level)}级`:'')).join(' / ');
    const strength=item.initial_hp==null?'战前兵力待核对':`历史 ${forceNumber(item.initial_hp)} 兵`;
    const time=esc(forceReportClock.format(item.event_at*1000));
    const report=`<a href="/?battle_id=${encodeURIComponent(item.battle_id)}">战报 ${esc(item.battle_id)} ↗</a>`;
    return `<div class="lineup-entry"><strong>${index+1}. ${heroes}</strong><p>${basis}</p><p>${[reportScene(item.npc),strength].filter(Boolean).join(' · ')}</p>${speedBasis?`<p>${speedBasis}</p>`:''}
      <div class="lineup-meta"><time>${time}</time>${report}</div>${heroLoadouts(item.heroes)}</div>`;
  });
  const explanation=!items.length?'档案里暂无该玩家的匹配战报。':
    !candidates.length?'暂无其他阵容候选。':prediction.ambiguous?'多套候选的证据接近。':'候选来自该玩家的历史战报。';
  const heading=linked?'其他阵容候选':'该玩家历史候选';
  const other=candidates.length>3?`<details class="prediction-more"><summary>其余 ${candidates.length-3} 套候选</summary>${candidates.slice(3).join('')}</details>`:'';
  return `${linked?alertLoadoutMarkup(linked):''}${forceDetail(prediction)}<h3>${heading}${candidates.length?` · ${candidates.length} 套`:''}</h3><p class="lineup-basis">${metric}</p>${note}<p>${explanation}</p>${candidates.slice(0,3).join('')}${other}`;
}
function renderLineupPanel(row,panel){
  if(!panel)return;
  if(isAlert(row.id)){
    const prediction=predictionFor(row.id);
    const markup=prediction?predictionDisplay(prediction):`<p>${forecastFailed?'阵容分析暂不可用':'正在分析来袭阵容…'}</p>`;
    panel.dataset.source='forecast';
    if(panel.dataset.rendered!==markup){
      panel.innerHTML=markup;
      panel.dataset.rendered=markup;
    }
    return;
  }
  if(panel.dataset.source==='forecast'){
    panel.innerHTML='';
    delete panel.dataset.rendered;
    delete panel.dataset.checkedAt;
  }
  panel.dataset.source='archive';
  if(!panel.dataset.checkedAt||Date.now()-Number(panel.dataset.checkedAt)>=15000&&!panel.contains(document.activeElement))loadLineups(row,panel);
}
async function loadLineups(row,panel){
  if(!panel||panel.dataset.loading==='true')return;
  panel.dataset.loading='true';
  panel.dataset.checkedAt=String(Date.now());
  if(!panel.innerHTML)panel.innerHTML='<p>正在关联战报阵容…</p>';
  try{
    const response=await fetch('/api/archive/army_lineups?'+new URLSearchParams({army_id:row.id}));
    if(!response.ok)throw new Error('阵容档案暂未连接');
    const data=await response.json();
    if(selected!==row.id||!panel.isConnected||panel.dataset.source==='forecast')return;
    if(!row.troops?.length&&data.troops?.length){
      $('detailTroops').innerHTML=troopDisplay(data.troops,'最近归档兵种',data.troops_observed_at);
    }
    const codes=troops=>troops?.map(t=>t.code).join(',');
    const differentObservation=row.troops?.length&&data.troops?.length&&codes(row.troops)!==codes(data.troops);
    const markup=differentObservation&&data.match!=='exact_army_id'
      ? '<h3>阵容待匹配</h3><p>地图与档案的兵种观测不同，等待下一次核对。</p>'
      : lineupDisplay(data);
    if(panel.dataset.rendered!==markup){
      panel.innerHTML=markup;
      panel.dataset.rendered=markup;
    }
  }catch(error){
    if(panel.isConnected&&panel.dataset.source!=='forecast'){
      panel.textContent='阵容档案暂未连接';
      delete panel.dataset.rendered;
    }
  }finally{
    panel.dataset.loading='false';
  }
}
function scopedAlerts(){
  return alertFocus?state.alerts.filter(r=>r.coord?.join(',')===alertFocus):campaignAlerts(state.alerts);
}
function groupAlerts(alerts){
  const groups={main:[],unknown:[],spartan:[]};
  for(const alert of alerts){
    const {kind}=alertForce(predictionFor(alert.army_id),alert.map_hero_count);
    (groups[kind]||groups.unknown).push(alert);
  }
  return groups;
}
function renderAlertTargets(){
  const counts=new Map();
  const alerts=campaignAlerts(state.alerts);
  for(const alert of alerts){
    const key=alert.coord?.join(',');
    if(key)counts.set(key,(counts.get(key)||0)+1);
  }
  if(alertFocus&&!counts.has(alertFocus))counts.set(alertFocus,0);
  $('watchTargetControl').hidden=!counts.size;
  const select=$('alertTarget');
  const options=[...counts].sort(([a],[b])=>a.localeCompare(b,'zh-CN',{numeric:true}));
  const markup=`<option value="">${activeCampaign()?'战役目标':'全部目标'} · ${alerts.length} 支</option>`+options.map(([key,count])=>
    `<option value="${esc(key)}">${coord(key.split(','))} · ${count} 支</option>`).join('');
  if(select.dataset.markup!==markup){
    select.innerHTML=markup;
    select.dataset.markup=markup;
  }
  select.value=alertFocus;
}
function renderTargetIntel(){
  const point=focusedPoint();
  $('targetIntel').hidden=!point;
  if(!point)return;
  const fort=(state.forts||[]).find(item=>item.coord.join(',')===alertFocus);
  const names=campaignNames(point,now());
  $('targetIdentity').textContent=[coord(point),fort?.name,fort?fortLabel(fort):'',fort?.end_at?`预计 ${duration(fort.end_at-now())} 后完成`:'',...names].filter(Boolean).join(' · ');
  const locate=$('targetLocate');
  locate.hidden=state.health.capture_source!=='pc';
  locate.dataset.gameCoord=point.join(',');
}
const fortLabel=fort=>({building:'建设中',ready:'已建成',transition:'状态待核对'}[fort.status]||'状态待核对')+
  (fort.relation==='enemy'?' · 敌方要塞':['self','ally'].includes(fort.relation)?' · 本盟要塞':' · 要塞');
function renderWatch(){
  if(!state)return;
  const t=watchedTarget();
  const targetKey=JSON.stringify(state.targets);
  if($('watchSelect').dataset.key!==targetKey){
    $('watchSelect').innerHTML=state.targets.length?state.targets.map((p,i)=>`<option value="${i}">${esc(p.name)} · ${p.x},${p.y}</option>`).join(''):'<option value="0">全部已见战场</option>';
    $('watchSelect').dataset.key=targetKey;
  }
  $('watchSelect').value=String(watchIndex);
  $('focusTarget').innerHTML=t.virtual?'当前视野<span>↗</span>':`(${t.x}, ${t.y})<span>↗</span>`;

  const enemy=Object.values(state.config.relations).some(r=>r.relation==='enemy');
  const attack=Object.values(state.config.state_rules).some(r=>r.verified&&r.kind==='attack');
  const alerts=scopedAlerts();
  const incoming=selectedIncoming(alerts);
  renderAlertPopup(incoming);
  const {main,unknown,spartan}=groupAlerts(alerts);
  const focus=focusedPoint();
  const campaign=activeCampaign();
  const candidates=state.live&&state.reliable?campaignAlerts(state.candidates).filter(r=>r.pending_reason&&r.end>now()&&
    (!focus||r.coord?.join(',')===alertFocus)).sort((a,b)=>a.end-b.end):[];
  const targetText=focus||campaign?` · 全域 ${state.alerts.length} 支`:'';
  $('watchStatus').textContent=alerts.length?`主力 ${main.length} · 未知 ${unknown.length} · 斯巴达 ${spartan.length}${targetText}`
    :!state.live?'数据未更新，来袭判断暂停'
    :!state.reliable?'等待完整地图观测，来袭判断暂缓'
    :!enemy||!attack?'等待敌方关系与进攻状态校准':focus||campaign?`${focus?coord(focus):campaign.name} 暂无已确认来袭${targetText}`:'全部已观测战场暂无已确认来袭';
  renderAlertTargets();
  renderTargetIntel();
  const badge=$('watchBadge');
  badge.textContent=String(state.alerts.length);
  badge.hidden=!state.alerts.length;
  const compact=$('compactWatchStatus');
  compact.textContent=state.alerts.length?`来袭预警 · ${state.alerts.length} 支`:'来袭预警';
  compact.dataset.alert=String(Boolean(state.alerts.length));

  garrisonWebSummary();
  renderQuickGuard();
  $('watchMain').hidden=!main.length;
  $('watchMainCount').textContent=`${main.length} 支`;
  renderWatchRows($('mainEntries'),main,true,incoming?.row.id);
  $('watchUnknown').hidden=!unknown.length;
  $('watchUnknownCount').textContent=`${unknown.length} 支`;
  renderWatchRows($('watchEntries'),unknown,true,incoming?.row.id);
  $('watchSpartan').hidden=!spartan.length;
  $('watchSpartanCount').textContent=`${spartan.length} 支`;
  renderWatchRows($('spartanEntries'),spartan,true,incoming?.row.id);
  const pending=$('watchCandidates');
  pending.hidden=!candidates.length;
  if(pending.hidden)pending.open=false;
  $('watchCandidateCount').textContent=`${candidates.length} 支${focus?` · ${coord(focus)}`:''}`;
  renderWatchRows($('candidateEntries'),pending.open?candidates:[],false);
  $('calibrationCount').textContent=`已校准 ${new Set(Object.values(state.config.state_rules).filter(r=>r.verified).map(r=>r.kind)).size} 种状态`;
}
function renderWatchRows(list,rows,confirmed,selectedId){
  const markup=rows.map(r=>{
    const names=confirmed?campaignNames(r.coord,now()):[];
    const prediction=confirmed?predictionFor(r.army_id):null;
    const linked=directAlertLoadout(prediction);
    const report=linked||prediction?.items[0];
    const loadout=report?.heroes?.length?alertLoadoutMarkup(report,{candidate:!linked,showTime:false}):'';
    const garrison=garrisonPlanMarkup(r,prediction);
    const trial=confirmed?garrisonAlertAction(r,prediction):'';
    const status=prediction?'暂无该玩家的匹配战报':forecastFailed?'阵容分析暂不可用':'正在分析阵容…';
    const preview=confirmed&&!loadout?`<small class="watch-prediction">${status}</small>`:'';
    const inferred=prediction?.force_assessment;
    const force=confirmed&&inferred?.kind==='main'&&inferred.confidence==='inferred'
      ? `<small class="watch-force"><span class="watch-force-tag main">推定主力</span> ${esc(forceSources[inferred.source]||'战报推定')}</small>`:'';
    const identity=confirmed
      ? `<strong>目标 ${coord(r.coord)}</strong><small>${esc(r.player_name)}${names.length?` · ${esc(names.join(' / '))}`:''}</small>${force}${preview}`
      : `<strong>${esc(r.player_name)}</strong><small>${coord(r.coord)} · ${esc(r.pending_reason)}</small>`;
    const entry=`<button type="button" class="watch-entry${confirmed?' confirmed':''}" data-watch-id="${esc(r.army_id)}"${confirmed?` aria-pressed="${r.army_id===selectedId}"`:''}>
      <span>${identity}</span>
      <span class="watch-eta">${confirmed?'<small>抵达倒计时</small>':''}<time data-watch-end="${esc(r.end||0)}"></time></span>
    </button>`;
    const locate=confirmed?gameLocateButton(r.coord,'watch-game-locate',garrison?'定位驻守点':'游戏内定位'):'';
    const card=locate?`<div class="watch-card">${entry}${locate}</div>`:entry;
    return loadout||garrison||trial?`<article class="watch-record">${card}${garrison}${trial}${loadout}</article>`:card;
  }).join('');
  if(list.dataset.markup!==markup){
    list.innerHTML=markup;
    list.dataset.markup=markup;
  }
  list.querySelectorAll('[data-watch-end]').forEach(el=>{
    const seconds=Number(el.dataset.watchEnd)-now();
    el.textContent=seconds>0?duration(seconds):'';
  });
  list.querySelectorAll('[data-garrison-duration]').forEach(el=>{
    el.textContent=garrisonClock.format((now()+Number(el.dataset.garrisonDuration))*1000);
  });
  list.querySelectorAll('[data-garrison-deadline]').forEach(el=>{
    el.textContent=duration(Math.max(0,Number(el.dataset.garrisonDeadline)-now()-Number(el.dataset.garrisonTravel)));
  });
}
function setNotice(message,level='',showAlerts=level==='alert'){
  $('notice').className=level?`notice ${level}`:'notice';
  if($('noticeText').textContent!==message)$('noticeText').textContent=message;
  $('noticeAlerts').hidden=!showAlerts;
}
function renderStatus(){if(!state)return;const h=state.health;const recent=h.last_packet_at?Math.max(0,now()-h.last_packet_at):null;$('liveDot').className=`dot ${state.live?'live':h.status==='error'?'error':''}`;$('connectionText').textContent=state.live?(h.capture_source==='pc'?'PC版 · 实时接收':'MuMu · 实时接收'):h.status==='waiting'?'等待游戏连接':'数据更新中断';$('connectionAge').textContent=recent===null?'等待游戏数据':`最后收到 ${ageText(recent)}`;$('ownName').textContent=state.config.self.name;$('ownUnion').textContent=state.config.self.union_name;$('freshCount').textContent=state.counts.fresh;$('observedCount').textContent=`/ 已见 ${state.counts.observed}`;$('messageCount').textContent=`${h.messages.toLocaleString()} 条消息`;
  if(state.alerts.length){
    const alerts=scopedAlerts();
    const {main,unknown,spartan}=groupAlerts(alerts);
    const focus=focusedPoint();
    const scope=focus?coord(focus):activeCampaign()?.name;
    const count=scope?` · 全域 ${state.alerts.length} 支`:'';
    if(main.length)setNotice(`主力 ${main.length} 支 · 最近目标 ${coord(main[0].coord)} · 共 ${alerts.length} 支来袭${count}`,'alert');
    else if(unknown.length)setNotice(`未知 ${unknown.length} 支 · 优先关注目标 ${coord(unknown[0].coord)} · 共 ${alerts.length} 支来袭${count}`,'alert');
    else if(spartan.length)setNotice(`斯巴达 ${spartan.length} 支 · 最近目标 ${coord(spartan[0].coord)}${count}`,'',true);
    else setNotice(`${scope||'当前范围'} 暂无已确认来袭${count}`,'',true);
  }else if(!state.live){
    setNotice(`${h.message}。过期观测不参与来袭提醒。`,'warning');
  }else if(h.identity_pending){
    setNotice(h.identity_message||'等待读取 PC 游戏的登录角色。','warning');
  }else if(!state.reliable){
    setNotice(h.schema_errors?'游戏数据格式变化，状态判断已暂停。':'地图数据暂未更新，来袭判断暂停。','warning');
  }else if(state.counts.unmapped){
    setNotice(`正在接收数据 · ${state.counts.unmapped} 支部队的状态尚待对照游戏校准。倒计时暂表示该状态的预计结束时间。`);
  }else{
    setNotice('正在接收游戏数据 · 已观测位置与出发坐标分别标注，视野外变化可能无法取得。');
  }
}
async function refresh(){try{const response=await fetch('/api/state',{cache:'no-store'});if(!response.ok)throw new Error('本机服务未响应');state=await response.json();apiConnected=true;clockOffset=state.server_now-Date.now()/1000;updateFreshness();renderStatus();renderRoster();renderDetails();renderWatch();loadAlertPredictions();loadMapCampaigns();if(!map.initialized&&(state.armies.length||alertFocus))fitMap();$('mapEmpty').style.display=visibleRows().length||(state.forts||[]).length||alertFocus?'none':'flex';draw();}catch(e){apiConnected=false;updateFreshness();renderWatch();if(selectedMarch)draw();$('liveDot').className='dot error';$('connectionText').textContent='服务未连接';setNotice('本机看板服务暂未响应，正在重试。','warning');}}
function tick(){
  if(!state)return;
  const staleBefore=state.counts.fresh;
  const alertsBefore=state.alerts.length;
  updateFreshness();
  if(apiConnected)renderStatus();
  renderWatch();
  if(staleBefore!==state.counts.fresh){
    renderRoster();
    renderDetails();
    draw();
  }else{
    for(const row of $('rosterPane').hidden?[]:state.armies){
      const button=rosterButtons.get(row.id);
      if(button)updateRosterTime(button,row);
    }
    if(alertsBefore!==state.alerts.length||state.armies.some(activeEnemyAttack))draw();
  }
  const row=selectedRow();
  if(row&&$('detailCountdown'))$('detailCountdown').textContent=remaining(row);
  if(row&&$('detailAge'))$('detailAge').textContent=detailFreshness(row);
}
function renderEvents(){const events=state?.events||[];$('eventHistory').innerHTML=events.length?events.map(e=>`<div class="history-entry"><div><strong>${esc(e.player_name)}</strong><time>${new Date(e.at*1000).toLocaleTimeString('zh-CN',{hour12:false})}</time></div><p>${esc(e.label)} · ${esc(e.state_label)}</p><small>${coord(e.from)} → ${coord(e.target)}</small></div>`).join(''):'<p class="help">尚未捕获战场变化。保持游戏地图在线后自动记录。</p>';}
function openSettings(){if(!state)return;const c=state.config,t=watchedTarget();$('settingsIdentity').textContent=`${c.self.name} / ${c.self.union_name}`;$('targetName').value=t.name;$('targetX').value=t.x;$('targetY').value=t.y;const own=c.self.union_id;$('unionSelect').innerHTML='<option value="">选择已观测同盟</option>'+Object.entries(state.unions).filter(([id])=>id!==own&&id!=='0').sort((a,b)=>a[1].localeCompare(b[1],'zh-CN')).map(([id,name])=>`<option value="${esc(id)}">${esc(name)}</option>`).join('');const enemy=Object.values(c.relations).some(r=>r.relation==='enemy');const attack=Object.values(c.state_rules).some(r=>r.verified&&r.kind==='attack');$('alertChecklist').innerHTML=`<li class="done">本方身份已核对</li><li class="done">全域记录，不按坐标过滤</li><li class="${enemy?'done':''}">至少一个敌方同盟已确认</li><li class="${attack?'done':''}">进攻状态已对照游戏校准</li><li>只对新鲜数据启用正式提醒</li>`;renderEvents();$('settingsDialog').showModal();updateGarrisonTrial();}
function renderMapCampaigns(){
  const select=$('mapCampaign');
  const markup='<option value="">全部战场</option>'+campaigns.map(c=>`<option value="${esc(c.id)}">${esc(c.name)}</option>`).join('');
  if(select.dataset.markup!==markup){select.innerHTML=markup;select.dataset.markup=markup;}
  select.value=campaignFocus;
}
async function loadMapCampaigns(force=false){
  if(!force&&Date.now()-campaignsCheckedAt<20000)return;
  campaignsCheckedAt=Date.now();
  try{
    const response=await fetch('/api/intel/official-campaigns',{cache:'no-store',signal:AbortSignal.timeout(4000)});
    if(!response.ok)throw new Error('战役暂不可用');
    const {items}=await response.json();
    const firstLoad=!campaigns.length;
    campaigns=items.filter(c=>c.filters.region?.length===4&&!c.filters.to_at);
    renderMapCampaigns();
    renderStatus();renderWatch();
    if(firstLoad&&activeCampaign()&&!alertFocus)fitMap();
  }catch(error){if(force)throw error;}
}
function setMapScope(point,campaign,center=false){
  alertFocus=alertTargetKey(point);
  campaignFocus=campaign;
  const url=new URL(location.href);
  for(const [key,value] of [['target',alertFocus],['campaign',campaignFocus]]){
    if(value)url.searchParams.set(key,value);else url.searchParams.delete(key);
  }
  history.replaceState(null,'',url);
  $('mapCampaign').value=campaignFocus;
  setInspectorView('watch');
  renderStatus();renderWatch();
  if(center)fitMap();else draw();
}
$('armyTab').onclick=()=>setInspectorView('army');
$('watchTab').onclick=()=>setInspectorView('watch');
$('compactWatchStatus').onclick=()=>setInspectorView('watch');
$('noticeAlerts').onclick=()=>{
  setInspectorView('watch');
  $('watchTab').focus({preventScroll:true});
};
$('closeInspector').onclick=()=>selectArmy(null);
$('rosterSelection').onclick=()=>setInspectorView('army');
$('settingsButton').onclick=openSettings;$('editTarget').onclick=openSettings;$('closeSettings').onclick=()=>$('settingsDialog').close();$('settingsDialog').addEventListener('click',e=>{if(e.target===$('settingsDialog'))$('settingsDialog').close();});
$('armGarrisonTrial').onclick=()=>updateGarrisonTrial('arm');
$('cancelGarrisonTrial').onclick=()=>updateGarrisonTrial('cancel');
$('cancelWebGarrison').onclick=()=>updateGarrisonTrial('cancel');
$('closeQuickGuard').onclick=()=>$('quickGuardDialog').close();
$('quickGuardTeam').onchange=renderQuickGuard;
function requestQuickGuard(action){
  const alert=currentQuickGuardAlert(), team_id=$('quickGuardTeam').value;
  if(!alert||!team_id||!quickGuardCandidates(alert.coord).some(team=>team.id===team_id)){renderQuickGuard();return;}
  const dispatch=action==='arm_alert';
  if(dispatch)$('quickGuardDialog').close();
  updateGarrisonTrial(action,{army_id:alert.army_id,coord:alert.coord,end:alert.end},
    {team_id,allow_recruit:dispatch&&$('quickGuardRecruit').checked});
}
$('previewQuickGuard').onclick=()=>requestQuickGuard('preview_alert');
$('submitQuickGuard').onclick=()=>requestQuickGuard('arm_alert');
setInterval(()=>{if($('settingsDialog').open||state?.health.capture_source==='pc')updateGarrisonTrial();},2000);
$('unionSelect').onchange=()=>{$('unionRelation').value=state.config.relations[$('unionSelect').value]?.relation||'unknown';};
$('targetForm').onsubmit=async e=>{e.preventDefault();try{await save({action:state.targets.length?'target':'target_add',target_index:watchIndex,name:$('targetName').value,x:Number($('targetX').value),y:Number($('targetY').value)});focusTarget();}catch(error){toast(error.message,true);}};
$('addTarget').onclick=async()=>{try{await save({action:'target_add',name:$('targetName').value,x:Number($('targetX').value),y:Number($('targetY').value)});watchIndex=state.targets.length-1;renderWatch();focusTarget();}catch(e){toast(e.message,true);}};
$('watchSelect').onchange=e=>{watchIndex=Number(e.target.value);renderWatch();focusTarget();};
$('watchCandidates').ontoggle=renderWatch;
$('alertTarget').onchange=e=>setMapScope(e.target.value,e.target.value?'':campaignFocus,true);
$('mapCampaign').onchange=e=>{if(state)setMapScope('',e.target.value,true);};
$('unionForm').onsubmit=async e=>{e.preventDefault();try{await save({action:'relation',union_id:$('unionSelect').value,relation:$('unionRelation').value});}catch(error){toast(error.message,true);}};
document.querySelector('.tabs').onclick=e=>{const b=e.target.closest('[data-filter]');if(!b)return;filter=b.dataset.filter;document.querySelectorAll('[data-filter]').forEach(el=>el.classList.toggle('active',el===b));renderRoster();draw();};$('search').oninput=e=>{query=e.target.value.toLowerCase().trim();renderRoster();draw();};$('showHistory').onchange=()=>{renderRoster();draw();};
$('roster').onclick=e=>{
  const row=e.target.closest('[data-id]');
  if(!row)return;
  const hadFocus=document.activeElement===row;
  selectArmy(row.dataset.id);
  if(hadFocus)$('rosterSelection').focus({preventScroll:true});
};
$('watchPanel').onclick=e=>{
  const dispatch=e.target.closest('[data-garrison-alert]');
  if(dispatch){
    const {garrisonAlert:id,garrisonAlertEnd:end,garrisonAlertCoord:point}=dispatch.dataset;
    const alert=state?.alerts.find(a=>a.army_id===id&&a.end===Number(end)&&a.coord.join(',')===point);
    if(!alert||alert.end<=now()){toast('这条来袭已更新，请使用最新卡片',true);return;}
    openQuickGuard(alert);
    return;
  }
  const button=e.target.closest('[data-watch-id]');
  if(!button)return;
  const id=button.dataset.watchId, row=state?.armies.find(item=>item.id===id);
  if(!button.classList.contains('confirmed')||!row){selectArmy(id);return;}
  const rect=button.getBoundingClientRect(), keyboard=e.detail===0;
  const anchor=keyboard?{x:rect.left,y:rect.top+rect.height/2}:{x:e.clientX,y:e.clientY};
  selectArmy(id,{row,anchor});
  if(keyboard&&!$('alertPopup').hidden)$('closeAlertPopup').focus({preventScroll:true});
};
$('closeAlertPopup').onclick=()=>dismissAlertPopup(true);
$('alertPopupDetails').onclick=()=>{setInspectorView('army');$('armyTab').focus({preventScroll:true});};
document.addEventListener('click',e=>{const button=e.target.closest('[data-game-coord]');if(button)locateInGame(button);});
document.addEventListener('pointerdown',e=>{
  if(alertPopupAnchor&&!e.target.closest('#alertPopup,[data-watch-id]'))dismissAlertPopup();
});
document.addEventListener('keydown',e=>{if(e.key==='Escape'&&alertPopupAnchor)dismissAlertPopup(true);});
window.addEventListener('resize',()=>{if(alertPopupAnchor)renderAlertPopup(selectedIncoming(scopedAlerts()));});
$('inspector').onclick=e=>{const button=e.target.closest('[data-battle-army]');if(button)selectArmy(button.dataset.battleArmy);};
const canvas=$('map'),ctx=canvas.getContext('2d');const map={x:0,y:0,scale:17,width:600,height:600,initialized:false,points:[],drag:null,moved:false};
function fitMap(){if(!state)return;const a=activeCampaign()?.filters.region||state.area;if(a&&a.length===4){map.x=(a[0]+a[2])/2;map.y=(a[1]+a[3])/2;map.scale=Math.max(5,Math.min(24,map.width/(Math.abs(a[2]-a[0])+Math.abs(a[3]-a[1])+10)));}else{focusTarget();}const focus=focusedPoint();if(focus)[map.x,map.y]=focus;map.initialized=true;draw();}
function focusTarget(){if(state){map.x=watchedTarget().x;map.y=watchedTarget().y;}map.initialized=true;draw();}
function diamond(point,size,fill,stroke){ctx.beginPath();ctx.moveTo(point.x,point.y-size);ctx.lineTo(point.x+size*1.35,point.y);ctx.lineTo(point.x,point.y+size);ctx.lineTo(point.x-size*1.35,point.y);ctx.closePath();if(fill){ctx.fillStyle=fill;ctx.fill();}if(stroke){ctx.strokeStyle=stroke;ctx.stroke();}}
function activeEnemyAttack(row){
  return state?.live&&!row.stale&&row.relation==='enemy'&&row.kind==='attack'&&row.from&&row.target&&row.end>now();
}
function mapArmyPoint(row,at){
  if(!activeEnemyAttack(row)||!row.begin||row.end<=row.begin)return row.display_position;
  const fraction=Math.max(0,Math.min(1,(at-row.begin)/(row.end-row.begin)));
  return row.from.map((start,index)=>start+(row.target[index]-start)*fraction);
}
function mapHit(points,x,y){
  return points.map(p=>({...p,d:Math.hypot(p.x-x,p.y-y)})).filter(p=>p.d<=p.radius)
    .sort((a,b)=>(a.d-b.d)||Number(Boolean(b.point))-Number(Boolean(a.point)))[0];
}
function mapTarget(point,kind){
  const p=project(point);
  if(p.x< -24||p.x>map.width+24||p.y< -24||p.y>map.height+24)return;
  map.points.push({...p,point,radius:kind==='fort'?18:16});
  return p;
}
function drawMapFort(fort){
  const p=mapTarget(fort.coord,'fort');
  if(!p)return;
  ctx.save();ctx.strokeStyle=colors[fort.relation]||colors.unknown;ctx.lineWidth=2;
  ctx.fillStyle=fort.status==='ready'?ctx.strokeStyle:'#fffbf0';
  if(fort.status!=='ready')ctx.setLineDash(fort.status==='building'?[4,2]:[1,2]);
  ctx.fillRect(p.x-8,p.y-8,16,16);ctx.strokeRect(p.x-8,p.y-8,16,16);
  ctx.restore();
}
function drawSelectedAttackRoute(origin, position, target){
  const line=(a,b)=>{ctx.beginPath();ctx.moveTo(a.x,a.y);ctx.lineTo(b.x,b.y);ctx.stroke();};
  ctx.save();ctx.lineCap='round';
  ctx.strokeStyle='#fff';ctx.lineWidth=8;line(origin,target);
  ctx.strokeStyle='#a75d55';ctx.lineWidth=2.5;ctx.setLineDash([5,4]);line(origin,position);
  ctx.strokeStyle='#b42318';ctx.lineWidth=4.5;ctx.setLineDash([]);line(position,target);
  ctx.lineWidth=2;ctx.fillStyle='#fff';ctx.beginPath();ctx.arc(origin.x,origin.y,4,0,Math.PI*2);ctx.fill();ctx.stroke();
  diamond(target,11,'#fff2ee','#b42318');
  ctx.restore();
}
function drawMapArmy(row,at,scope,highlighted=false){
  const attack=activeEnemyAttack(row), chosen=row.id===selected;
  if(attack&&scope&&!scope(row.target))return;
  const position=mapArmyPoint(row,at)||(highlighted?row.from:null);
  if(!position)return;
  const p=project(position), t=row.target?project(row.target):null;
  const origin=row.from?project(row.from):p;
  const color=attack&&(!row.incoming||row.age>15)?'#916132':colors[row.relation]||colors.unknown;
  if(highlighted&&t)drawSelectedAttackRoute(origin,p,t);
  else if(t&&(attack||chosen)){
    ctx.save();ctx.strokeStyle=color;ctx.lineWidth=chosen?2:1.3;ctx.globalAlpha=row.stale?.35:.7;
    ctx.setLineDash([5,4]);ctx.beginPath();ctx.moveTo(origin.x,origin.y);ctx.lineTo(t.x,t.y);ctx.stroke();ctx.restore();
  }
  if(p.x< -24||p.x>map.width+24||p.y< -24||p.y>map.height+24)return;
  ctx.save();ctx.globalAlpha=row.stale?.3:1;ctx.strokeStyle=color;ctx.lineWidth=2;
  if((attack||highlighted)&&t){
    ctx.translate(p.x,p.y);ctx.rotate(Math.atan2(t.y-origin.y,t.x-origin.x));ctx.fillStyle=color;
    ctx.beginPath();ctx.moveTo(highlighted?13:10,0);ctx.lineTo(-7,-6);ctx.lineTo(-4,0);ctx.lineTo(-7,6);ctx.closePath();ctx.fill();
  }else{
    ctx.fillStyle=row.position?color:'#f5f8fd';ctx.beginPath();ctx.arc(p.x,p.y,chosen?9:7,0,Math.PI*2);ctx.fill();ctx.stroke();
  }
  ctx.restore();
  if(chosen){ctx.save();ctx.strokeStyle='#2456bd80';ctx.lineWidth=2;ctx.beginPath();ctx.arc(p.x,p.y,13,0,Math.PI*2);ctx.stroke();ctx.fillStyle=color;ctx.font='11px Segoe UI, Microsoft YaHei';ctx.fillText(row.player_name,p.x+14,p.y-10);ctx.restore();}
  map.points.push({...p,id:row.id,radius:attack?18:16});
}
function drawMapLineups(alerts,at){
  if(!alerts.length)return;
  const bounds={left:8,top:72,right:map.width-8,bottom:map.height-57};
  const titleFont='600 12px Segoe UI, Microsoft YaHei', detailFont='11px Segoe UI, Microsoft YaHei';
  const points=new Map(map.points.filter(point=>point.id).map(point=>[point.id,point]));
  const markers=[], labels=[];
  const priority={main:0,unknown:1,spartan:2};
  for(const alert of alerts){
    const point=points.get(alert.army_id);
    if(!point||point.x<0||point.x>map.width||point.y<0||point.y>map.height||alert.end<=at)continue;
    markers.push({left:point.x-10,right:point.x+10,top:point.y-8,bottom:point.y+8});
    const chosen=alert.army_id===selected, prediction=predictionFor(alert.army_id);
    if(!prediction&&!chosen)continue;
    const {kind}=alertForce(prediction,alert.map_hero_count);
    labels.push({point,chosen,prediction,rank:chosen?-1:priority[kind]??1});
  }
  labels.sort((a,b)=>a.rank-b.rank);
  const blocked=[...markers];
  ctx.save();
  ctx.textBaseline='top';
  for(const {point,chosen,prediction} of labels){
    const [title,detail]=mapLineupText(prediction);
    ctx.font=titleFont;
    const titleWidth=ctx.measureText(title).width;
    ctx.font=detailFont;
    const width=Math.min(bounds.right-bounds.left,Math.ceil(Math.max(titleWidth,ctx.measureText(detail).width))+14);
    const box=mapLabelBox(point,width,34,bounds,blocked)||(chosen?mapLabelBox(point,width,34,bounds,[]):null);
    if(!box)continue;
    blocked.push({left:box.left-4,top:box.top-3,right:box.right+4,bottom:box.bottom+3});
    const x=Math.max(box.left,Math.min(point.x,box.right)), y=Math.max(box.top,Math.min(point.y,box.bottom));
    const length=Math.hypot(x-point.x,y-point.y);
    ctx.lineWidth=1;
    ctx.strokeStyle=chosen?'#af433c':'#91a1b7';
    ctx.beginPath();ctx.moveTo(point.x+(x-point.x)*10/length,point.y+(y-point.y)*10/length);ctx.lineTo(x,y);ctx.stroke();
    ctx.fillStyle=chosen?'#fff8f4':'#ffffff';
    ctx.fillRect(box.left,box.top,width,34);ctx.strokeRect(box.left+.5,box.top+.5,width-1,33);
    ctx.fillStyle='#172740';ctx.font=titleFont;ctx.fillText(title,box.left+7,box.top+5,width-14);
    ctx.fillStyle='#53647e';ctx.font=detailFont;ctx.fillText(detail,box.left+7,box.top+19,width-14);
  }
  ctx.restore();
}
function draw(){const w=map.width,h=map.height;ctx.clearRect(0,0,w,h);ctx.fillStyle='#f5f8fd';ctx.fillRect(0,0,w,h);const corners=[[0,0],[w,0],[0,h],[w,h]].map(([x,y])=>unproject(x,y));const xs=corners.map(p=>p[0]),ys=corners.map(p=>p[1]);const step=map.scale<10?5:1;const loX=Math.floor(Math.min(...xs)/step)*step,hiX=Math.ceil(Math.max(...xs)/step)*step,loY=Math.floor(Math.min(...ys)/step)*step,hiY=Math.ceil(Math.max(...ys)/step)*step;ctx.lineWidth=.6;
  for(let x=loX;x<=hiX;x+=step){const a=project([x,loY]),b=project([x,hiY]);ctx.strokeStyle=x%5===0?'#c4d1e4':'#e3eaf4';ctx.beginPath();ctx.moveTo(a.x,a.y);ctx.lineTo(b.x,b.y);ctx.stroke();}for(let y=loY;y<=hiY;y+=step){const a=project([loX,y]),b=project([hiX,y]);ctx.strokeStyle=y%5===0?'#c4d1e4':'#e3eaf4';ctx.beginPath();ctx.moveTo(a.x,a.y);ctx.lineTo(b.x,b.y);ctx.stroke();}
  map.points=[];
  if(!state)return;
  const focus=focusedPoint(), campaign=activeCampaign(), region=campaign?.filters.region, at=now();
  const scope=focus?point=>point?.join(',')===alertFocus:region?point=>inCampaignTime(campaign.filters,at)&&inCampaignArea(point,campaign):null;
  if(region&&!focus){
    const areas=campaign.current_blocks?.map(block=>{
      const x=Math.floor(block/1000)*10,y=block%1000*10;
      return [x-9,y-9,x,y];
    })||[region];
    ctx.save();ctx.strokeStyle='#826630';ctx.lineWidth=2;ctx.setLineDash([5,3]);
    for(const [x0,y0,x1,y1] of areas){
      ctx.beginPath();
      for(const [index,point] of [[x0-.5,y0-.5],[x1+.5,y0-.5],[x1+.5,y1+.5],[x0-.5,y1+.5]].entries()){
        const p=project(point);if(index)ctx.lineTo(p.x,p.y);else ctx.moveTo(p.x,p.y);
      }
      ctx.closePath();ctx.stroke();
    }
    ctx.restore();
  }
  const mark=watchedTarget(), target=focus||(mark.virtual?null:[mark.x,mark.y]);
  if(target){
    const p=mapTarget(target);
    if(p){ctx.save();ctx.lineWidth=2;diamond(p,14,'#f4d58d30','#a77830');ctx.fillStyle='#795419';ctx.font='11px Segoe UI, Microsoft YaHei';ctx.fillText(focus?coord(focus):mark.name,p.x+21,p.y-8);ctx.restore();}
  }
  for(const fort of state.forts||[])drawMapFort(fort);
  const alerts=scopedAlerts(), plotted=new Set();
  for(const alert of alerts){
    const key=alert.coord.join(',');
    if(plotted.has(key))continue;
    plotted.add(key);
    const p=mapTarget(alert.coord);
    if(p){ctx.save();ctx.lineWidth=2;diamond(p,9,null,colors.enemy);ctx.restore();}
  }
  const highlighted=selectedIncoming(alerts)?.row;
  const rows=visibleRows().filter(row=>row!==highlighted);
  for(const row of rows)if(!activeEnemyAttack(row))drawMapArmy(row,at,scope);
  for(const row of rows)if(activeEnemyAttack(row))drawMapArmy(row,at,scope);
  if(highlighted)drawMapArmy(highlighted,at,scope,true);
  drawMapLineups(alerts,at);
}
function resize(){const rect=canvas.getBoundingClientRect(),dpr=Math.min(window.devicePixelRatio||1,2);map.width=rect.width;map.height=rect.height;canvas.width=Math.round(rect.width*dpr);canvas.height=Math.round(rect.height*dpr);ctx.setTransform(dpr,0,0,dpr,0,0);draw();}new ResizeObserver(resize).observe(canvas);
canvas.onpointerdown=e=>{
  map.drag={x:e.offsetX,y:e.offsetY,anchor:unproject(e.offsetX,e.offsetY)};
  map.moved=false;
  canvas.setPointerCapture(e.pointerId);
};
canvas.onpointermove=e=>{
  const point=unproject(e.offsetX,e.offsetY);
  if(map.drag){
    const {x,y,anchor}=map.drag;
    if(Math.abs(e.offsetX-x)+Math.abs(e.offsetY-y)>4)map.moved=true;
    if(map.moved){map.x+=anchor[0]-point[0];map.y+=anchor[1]-point[1];draw();}
  }
  const [x,y]=map.drag?.anchor??point;
  $('mapCoordinates').textContent=`X ${Math.round(x)} · Y ${Math.round(y)}`;
};
canvas.onpointerup=e=>{
  if(state&&map.drag&&!map.moved){
    const hit=mapHit(map.points,e.offsetX,e.offsetY);
    if(hit?.id)selectArmy(hit.id);
    else setMapScope((hit?.point||unproject(e.offsetX,e.offsetY).map(Math.round)).join(','),'');
  }
  map.drag=null;
};
canvas.onpointercancel=()=>map.drag=null;canvas.addEventListener('wheel',e=>{e.preventDefault();map.scale=Math.max(3,Math.min(70,map.scale*(e.deltaY<0?1.12:.89)));draw();},{passive:false});$('zoomIn').onclick=()=>{map.scale=Math.min(70,map.scale*1.25);draw();};$('zoomOut').onclick=()=>{map.scale=Math.max(3,map.scale/1.25);draw();};$('fitView').onclick=fitMap;$('focusTarget').onclick=focusTarget;
if(alertFocus)setInspectorView('watch');
refresh();setInterval(refresh,2000);setInterval(tick,1000);
