"use strict";
const $=id=>document.getElementById(id);
const escapeHtml=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const num=value=>value==null?'—':Number(value).toLocaleString('zh-CN',{maximumFractionDigits:4});
const date=value=>value?new Date(value*1000).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false}):'—';
const day=offset=>new Date(Date.now()+8*3600000+offset*86400000).toISOString().slice(0,10);
const attendanceColumns=[['player_name','玩家名'],['joined_at','进盟时间'],['group_name','分组'],['total_score','总积分'],['total_rank','总排名'],
 ['total_main_win_rate','累计主力胜率'],['total_exchange','累计交换比'],['score','期间积分'],['rank','期间排名'],
 ['cumulative_honor','累计武勋'],['new_honor','新增武勋'],['capture_low','翻地1—4'],['capture_high','翻地5—9'],['demolitions','拆除'],
 ['spartan','斯巴达场次'],['main','主力场次'],['main_win_rate','期间主力胜率'],['exchange','期间交换比'],['demolished','被拆'],
 ['lost_land','被翻地'],['capture_unknown','占领待核'],['siege_damage','拆除耐久'],['honor','战报武勋'],['pve_samples','非玩家作战']];
const attendanceValue=(r,key)=>key==='joined_at'?date(r[key]):key.includes('win_rate')?(r[key]==null?'—':num(r[key])+'%'):['player_name','group_name'].includes(key)?escapeHtml(r[key]||'—'):num(r[key]);
function teamNames(heroes){
 return heroes?.length?[...heroes].sort((a,b)=>a.position-b.position).map(h=>h.name||'武将资料未齐').join(' / '):'队伍资料未齐';
}
function reportMatch(row){
 const own=escapeHtml(teamNames(row.own_heroes)),opponent=escapeHtml(row.opponent_name||'对方资料未齐');
 return `<a class="report-match" href="/?battle_id=${encodeURIComponent(row.battle_id)}" target="_blank" rel="noopener" title="打开这场战报"><strong>${own}</strong><span>对手：${opponent} · ${escapeHtml(teamNames(row.opponent_heroes))} ↗</span></a>`;
}
let latest=null,request=null,sequence=0,detailSequence=0;
$('attendanceHeaders').innerHTML=attendanceColumns.map(([key,label])=>`<th data-column="${key}">${escapeHtml(label)}</th>`).join('');
$('from').value=$('to').value=day(0);
function params(){return new URLSearchParams({from:$('from').value,to:$('to').value,group:$('group').value,search:$('search').value.trim(),include_pve:'1'});}
async function get(url,options={}){const response=await fetch(url,options);const body=await response.json();if(!response.ok)throw new Error(body.error||'暂时无法读取数据');return body;}
async function load(){
 const serial=++sequence;request?.abort();request=new AbortController();$('updated').textContent='正在更新…';$('updated').classList.remove('error');
 try{
  const data=await get('/api/attendance?'+params(),{signal:request.signal});if(serial!==sequence)return;latest=data;
  if(data.role_context){document.querySelector('h1').textContent=data.role_context.statistics_title;
   $('group').closest('label').hidden=!data.role_context.has_organization;}
  $('identity').innerHTML=`<span id="ownName">${escapeHtml(data.profile.account_name)}</span> · <span id="ownUnion">${escapeHtml(data.profile.union_name)}</span> · ${escapeHtml(data.profile.server_label||data.profile.server)} · ${escapeHtml(data.profile.season_label||data.profile.season)}`;
  $('connectionStatus').textContent='已连接';$('connectionStatus').dataset.live='true';
  const group=$('group').value;$('group').innerHTML='<option value="">全部分组</option>'+(data.groups||[]).map(v=>`<option value="${escapeHtml(v)}">${escapeHtml(v)}</option>`).join('');$('group').value=group;
  const s=data.summary||{};$('summary').textContent=data.message||`${num(s.players)} 位玩家 · ${num(s.pvp)} 场玩家参战 · ${num(s.sides)} 条出战记录`;
  $('rows').innerHTML=data.items.map(row=>`<tr>${attendanceColumns.map(([key])=>`<td data-column="${key}">${key==='player_name'?`<button data-player="${escapeHtml(row.player_key)}">${escapeHtml(row.player_name)}</button>`:attendanceValue(row,key)}</td>`).join('')}</tr>`).join('');
  $('empty').hidden=!!data.items.length;$('updated').textContent=data.pending?`正在整理 ${num(data.pending)} 份战报`:`查询于 ${date(data.updated_at)}`;
  $('retention').textContent=data.retain_from?'记录起点：'+date(data.retain_from)+'；更早历史已停止补采。':'';
 }catch(error){if(error.name==='AbortError')return;$('updated').textContent=error.message;$('updated').classList.add('error');$('connectionStatus').textContent='读取暂缓';$('connectionStatus').dataset.live='false';}
}
$('filters').addEventListener('submit',event=>{event.preventDefault();load();});
for(const [id,offset]of [['today',0],['yesterday',-1]])$(id).addEventListener('click',()=>{$('from').value=$('to').value=day(offset);load();});
$('rulesOpen').addEventListener('click',()=>{
 if(!latest)return;$('enabled').checked=latest.rules.enabled;$('ruleError').textContent='';
 $('weights').innerHTML=Object.entries(latest.metrics).map(([key,label])=>`<label>${escapeHtml(label)} · 每单位积分<input data-metric="${key}" type="number" min="-1000000" max="1000000" step="any" value="${latest.rules.weights[key]??''}" placeholder="不计分"></label>`).join('');$('rulesDialog').showModal();
});
$('rulesClose').addEventListener('click',()=>$('rulesDialog').close());
$('rulesForm').addEventListener('submit',async event=>{
 event.preventDefault();const weights={};document.querySelectorAll('[data-metric]').forEach(input=>{if(input.value!=='')weights[input.dataset.metric]=Number(input.value);});
 try{await get('/api/attendance/rules',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:$('enabled').checked,weights})});$('rulesDialog').close();load();}catch(error){$('ruleError').textContent=error.message;}
});
$('rows').addEventListener('click',async event=>{
 const button=event.target.closest('[data-player]');if(!button)return;const serial=++detailSequence;const q=params();q.set('player_key',button.dataset.player);q.set('include_pve','1');
 $('detailTitle').textContent=button.textContent+' · 参战明细';$('detailStatus').textContent='正在读取…';$('detailRows').textContent='';$('detailDialog').showModal();
 try{const data=await get('/api/attendance?'+q);if(serial!==detailSequence)return;
 $('detailStatus').textContent=data.items.length?`最近 ${data.items.length} 条记录，最多展示 200 条`:'未见可关联参战记录';
   $('detailRows').innerHTML=data.items.map(r=>`<tr><td>${escapeHtml(date(r.event_at))}</td><td>${reportMatch(r)}</td><td>${r.wid?Math.floor(r.wid/10000)+','+r.wid%10000:'—'}</td><td>${r.side==='attack'?'进攻':'防守'}</td><td>${({main:'主力',spartan:'斯巴达',unknown:'未知'})[r.category]}${r.pvp?'':' · 非玩家作战'}</td><td>${({win:'胜',loss:'败',draw:'平',unknown:'未核实'})[r.outcome]||'未核实'}</td><td>${num(r.honor)}</td></tr>`).join('')+(data.events||[]).map(r=>`<tr><td>${escapeHtml(date(r.event_at))}</td><td>${escapeHtml(({capture:'翻地',occupy:'成功占领（原归属待核）',neutral_occupation:'中立地占领',transfer:'盟内转地',ownership_gap:'间断期间变化',building_change:'建筑变化待核',abandoned:'归属解除'})[r.kind]||r.kind)}</td><td>${Math.floor(r.wid/10000)},${r.wid%10000}</td><td colspan="4">${r.countable?'有结果证据':'不计贡献'}${r.level?' · '+r.level+'级地':''}</td></tr>`).join('');
 }catch(error){if(serial===detailSequence)$('detailStatus').textContent=error.message;}
});
$('detailClose').addEventListener('click',()=>{detailSequence++;$('detailDialog').close();});
$('export').addEventListener('click',()=>{
 if(!latest)return;const columns=attendanceColumns;
 const cell=value=>{let text=String(value??'');if(/^[\s]*[=+@-]/.test(text))text="'"+text;return '"'+text.replaceAll('"','""')+'"';};
 const csv='\ufeff'+[columns.map(c=>c[1]),...latest.items.map(r=>columns.map(c=>c[0]==='joined_at'?(r.joined_at?date(r.joined_at):''):r[c[0]]))].map(row=>row.map(cell).join(',')).join('\r\n');
 const url=URL.createObjectURL(new Blob([csv],{type:'text/csv;charset=utf-8'}));const a=document.createElement('a');a.href=url;a.download=`同盟考勤_${$('from').value}_${$('to').value}.csv`;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
});
load();setInterval(()=>{if(!document.hidden&&!$('rulesDialog').open&&!$('detailDialog').open)load();},15000);
