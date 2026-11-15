'use strict';
window.FormationCatalog=(()=>{
 const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
 const num=v=>v==null?'—':Number(v).toLocaleString('zh-CN',{maximumFractionDigits:2});
 const date=t=>t?new Date(t*1000).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false}):'—';
 const rate=n=>n==null?'—':(n*100).toFixed(1)+'%';
 const kinds={main:'主力',spartan:'斯巴达',unknown:'未知'};
 const ordered=heroes=>[...(heroes||[])].sort((a,b)=>a.position-b.position);
 const roster=heroes=>ordered(heroes).map(h=>`<span class="formation-hero"><b>${esc(h.name)}</b><small>${esc(h.level??'?')}级</small><em>${esc(h.red_stars??'?')}红</em></span>`).join('<span class="formation-slash">/</span>')||'资料待补齐';
 const endpoint=(view,q={})=>'/api/intel/formations?'+new URLSearchParams({...q,view});
 async function get(url){const r=await fetch(url,{cache:'no-store'});const d=await r.json();if(!r.ok)throw Error(d.error||'查询暂缓，请重试');return d;}
 function skillTable(heroes){return `<div class="formation-loadout">${ordered(heroes).map(h=>`<section><strong>${esc(h.name)} ${esc(h.level??'?')}级 <em>${esc(h.red_stars??'?')}红</em></strong>${[0,1,2].map(i=>{
   const id=h.skill_slot_ids?.[i],s=id===0?null:id?(h.skills||[]).find(s=>s.id===id):(h.skills||[])[i];
   return `<div><span>${esc(s?.name||(id===0?'空槽':'未取得'))}</span><small>${s?.level==null?'':esc(s.level)+'级'}</small></div>`;
 }).join('')}<small>${esc(h.troop_name||'兵种未取得')} · ${esc(h.gear?.name||'武器未取得')}${h.gear?.level!=null?' +'+esc(h.gear.level):''}</small></section>`).join('')}</div>`;}
 const summary=r=>`主力对阵 ${num(r.main_samples)} 场 · 胜率 ${rate(r.main_win_rate)} · 交换比 ${r.main_zero_loss?'己方零损':num(r.main_exchange)}`;
 function teamsMarkup(data){
  return ['main','spartan','unknown'].map(kind=>{
   const teams=data.items.filter(r=>r.category===kind);if(!teams.length)return '';
   return `<details class="formation-category ${kind}"><summary>${kinds[kind]} <b>${teams.length}</b></summary>${teams.map(row=>`<details class="formation-team" data-formation="${esc(row.formation_id)}" data-current-config="${esc(row.config_id)}" data-player-name="${esc(row.player_name)}"><summary><span><span class="formation-roster">${roster(row.heroes)}</span><small>${summary(row)}</small></span><time>${esc(date(row.latest_at))}</time></summary><div class="formation-team-body"></div></details>`).join('')}</details>`;
  }).join('')||'<p class="formation-empty">暂无满足条件的队伍。</p>';
 }
 function render(root,data,filters={}){
  root.classList.add('formation-catalog');root.catalogFilters={...filters};
  root.innerHTML=`<p class="formation-status">${data.pending?'正在整理 '+num(data.pending)+' 份战报':'按最新配置归类'} · 队伍与战法详情默认收起</p>`+data.items.map(p=>`<details class="formation-player" data-unit-player="${esc(p.player_key)}"><summary><strong>${esc(p.player_name)}</strong><span class="formation-counts"><b class="main">主力 ${num(p.main_count)}</b><b class="spartan">斯巴达 ${num(p.spartan_count)}</b><b class="unknown">未知 ${num(p.unknown_count)}</b></span><time>${esc(date(p.at))}</time></summary><div class="formation-player-body"></div></details>`).join('')+(!data.items.length?'<p class="formation-empty">暂无已建立阵容的战报记录。</p>':'');
  if(root.catalogBound)return;root.catalogBound=true;
  root.addEventListener('toggle',async event=>{
   const node=event.target;if(!node.open||node.loaded||node.loading)return;
   if(node.matches('[data-unit-player]')){
    const body=node.querySelector('.formation-player-body');node.loading=true;body.textContent='正在读取队伍…';
    try{const d=await get(endpoint('teams',{...root.catalogFilters,player_key:node.dataset.unitPlayer,limit:100,offset:0}));if(!node.isConnected)return;body.innerHTML=teamsMarkup(d);node.loaded=true;}catch(e){body.textContent=e.message+'；收起后可重试。';}finally{node.loading=false;}
   }else if(node.matches('[data-formation]')){
    node.loading=true;const body=node.querySelector('.formation-team-body');body.textContent='正在读取配置和战报…';
    const q={...root.catalogFilters,formation_id:node.dataset.formation,limit:30,offset:0};
    try{
     const history=root.catalogFilters.display_mode==='archive';
     const [configs,events]=await Promise.all([get(endpoint('configs',{...q,...(!history?{config_id:node.dataset.currentConfig}:{})})),history?get(endpoint('events',q)):get(endpoint('matchups',{...q,config_id:node.dataset.currentConfig}))]);if(!node.isConnected)return;
     body.innerHTML=`<div class="formation-section-head">${history?'配置变更与成绩':'当前配置与成绩'}</div><div class="formation-configs">${configRows(configs.items)}</div>${history&&configs.has_more?'<button type="button" class="formation-config-more">加载更早配置</button>':''}`+(history?
       `<div class="formation-section-head">战报时间线</div><div class="formation-events">${eventRows(events.items)}</div>${events.has_more?'<button type="button" class="formation-more">加载更早战报</button>':''}`:
       `<div class="formation-section-head">当前配置 · 主力对阵</div>${events.items.map(r=>`<article class="formation-matchup"><span class="formation-roster">${roster(r.opponent_heroes)}</span><small>${num(r.samples)} 场 · 胜率 ${rate(r.main_win_rate)} · 交换比 ${num(r.main_exchange)}</small></article>`).join('')||'<p class="formation-empty">暂无双方主力的对阵样本。</p>'}<p class="formation-statline">历史表现按已收到样本统计，不作为下一场战斗的获胜概率。</p><a href="/archive?player=${encodeURIComponent(node.dataset.playerName)}">查看配置变更与历次战报 ↗</a>`);
     node.configCount=configs.items.length;node.eventCount=events.items.length;node.eventQuery=q;node.loaded=true;
    }catch(e){body.textContent=e.message+'；收起后可重试。';}finally{node.loading=false;}
   }
  },true);
  root.addEventListener('click',async event=>{
   const more=event.target.closest('.formation-more,.formation-config-more');if(!more)return;
   const node=more.closest('[data-formation]');more.disabled=true;
   try{const config=more.classList.contains('formation-config-more');const d=await get(endpoint(config?'configs':'events',{...node.eventQuery,offset:config?node.configCount:node.eventCount}));node.querySelector(config?'.formation-configs':'.formation-events').insertAdjacentHTML('beforeend',config?configRows(d.items):eventRows(d.items));if(config)node.configCount+=d.items.length;else node.eventCount+=d.items.length;if(!d.has_more)more.remove();}catch(e){more.textContent=e.message;}finally{more.disabled=false;}
  });
 }
 function configRows(items){return items.map(c=>`<details class="formation-version"><summary><span>${esc(date(c.latest_at))} · ${num(c.samples)} 条作战记录</span><span>${summary(c)}</span></summary>${skillTable(c.heroes)}<p class="formation-statline">玩家对战 ${num(c.pvp_samples)} 场：${num(c.wins)}胜 / ${num(c.draws)}平 / ${num(c.defeats)}负 · 非玩家作战 ${num(c.pve_samples)} 场</p></details>`).join('');}
 function eventRows(rows){return rows.map(r=>`<article class="formation-event"><time>${esc(date(r.event_at))}</time><span>${r.pvp?'玩家交战':'NPC等非玩家作战'} · ${esc(({win:'胜',loss:'负',draw:'平'})[r.outcome]||'结果待核')}<small>${esc(r.place_name||'交战地')}（${Math.floor(r.wid/10000)},${r.wid%10000}）</small>${(r.marches||[]).map(m=>`<small>行军 ${m.from_x},${m.from_y} → ${m.to_x},${m.to_y} · ${esc(date(m.begin_at))} → ${esc(date(m.end_at))}</small>`).join('')}</span><a href="/?battle_id=${encodeURIComponent(r.battle_id)}">查看对阵战报 ↗</a></article>`).join('');}
 function mount(root){
  root.innerHTML=`<section class="records"><div class="records-heading"><h2>玩家与队伍档案</h2></div><form class="formation-search"><label>玩家<input name="player" placeholder="玩家名"></label><label>武将／战法<input name="search" placeholder="武将或战法名"></label><label>坐标<input name="coord" placeholder="例如 1015,926"></label><label>官方战役<select name="official_campaign_id"><option value="">全赛季</option></select></label><label>开始时间<input name="from_at" type="datetime-local"></label><label>结束时间<input name="to_at" type="datetime-local"></label><label>类型<select name="category"><option value="">全部队伍</option><option value="main">主力</option><option value="spartan">斯巴达</option><option value="unknown">未知</option></select></label><button class="primary">查询</button><button type="reset">重置</button></form><div class="formation-results"></div><div class="formation-pages"><button class="formation-prev">上一页</button><span></span><button class="formation-next">下一页</button></div></section>
   <details class="records formation-land"><summary>翻地、被翻与地块变化</summary><p class="help">仅显示已收到的结果和已观测变化；不能确认的事件不计入贡献。</p><div class="formation-land-events"></div></details>`;
  const form=root.querySelector('form'),results=root.querySelector('.formation-results');let offset=0,lastQuery={},serial=0;
  form.elements.namedItem('player').value=new URLSearchParams(location.search).get('player')||'';
  async function refresh(){const token=++serial;const q={relation:'all',limit:25,offset,display_mode:'archive'};
   for(const [k,v]of new FormData(form)){if(String(v).trim())q[k]=k.endsWith('_at')?Date.parse(v+'+08:00')/1000:v;}
   try{const d=await get(endpoint('players',q));if(token!==serial)return;lastQuery=q;render(results,d,q);root.querySelector('.formation-prev').disabled=offset===0;root.querySelector('.formation-next').disabled=!d.has_more;root.querySelector('.formation-pages span').textContent='第 '+(offset/25+1)+' 页';}catch(e){let status=root.querySelector('.formation-query-error');if(!status){status=document.createElement('p');status.className='formation-query-error';form.after(status);}status.textContent=e.message;}
  }
  form.onsubmit=e=>{e.preventDefault();offset=0;refresh();};form.onreset=()=>{offset=0;setTimeout(refresh,0);};
  root.querySelector('.formation-prev').onclick=()=>{offset=Math.max(0,offset-25);refresh();};root.querySelector('.formation-next').onclick=()=>{offset+=25;refresh();};
  root.querySelector('.formation-land').addEventListener('toggle',async e=>{if(!e.target.open)return;const box=root.querySelector('.formation-land-events');box.textContent='正在读取…';try{
   const d=await get('/api/intel/territory-events?'+new URLSearchParams({from_at:lastQuery.from_at||0,to_at:lastQuery.to_at||0,coord:lastQuery.coord||'',player:lastQuery.player||'',official_campaign_id:lastQuery.official_campaign_id||'',limit:100}));
   const labels={capture:'翻地',occupy:'成功占领（原归属待核）',neutral_occupation:'占领中立地',transfer:'同盟内转地',abandoned:'归属解除',ownership_gap:'间断期间归属变化',building_change:'建筑变化待核'};
   box.innerHTML=d.items.map(r=>`<article class="formation-event"><time>${esc(date(r.event_at))}</time><span>${esc(labels[r.kind]||r.kind)} · ${esc(r.actor_name||'玩家待核')}<small>${Math.floor(r.wid/10000)},${r.wid%10000}${r.level?' · '+r.level+'级地':''} · ${r.countable?'有结果证据':'不计贡献'}</small></span>${r.battle_id?`<a href="/?battle_id=${encodeURIComponent(r.battle_id)}">查看战报 ↗</a>`:''}</article>`).join('')||'<p class="formation-empty">暂无符合条件的地块事件。</p>';
  }catch(err){box.textContent=err.message;}});
  get('/api/intel/official-campaigns').then(d=>{form.elements.namedItem('official_campaign_id').innerHTML='<option value="">全赛季</option>'+(d.items||[]).filter(c=>c.name_verified).map(c=>`<option value="${esc(c.id)}">${esc(c.name)}</option>`).join('');}).catch(()=>{});
  refresh();return {refresh};
 }
 return {render,mount,roster,skillTable};
})();
if(document.getElementById('formationArchive'))window.formationArchive=FormationCatalog.mount(document.getElementById('formationArchive'));
