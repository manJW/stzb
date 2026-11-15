'use strict';
let state=null, selected=null, inspectorKey='', clockOffset=0, apiConnected=false, watchIndex=0;
let refreshing=false;
let pendingUiReload=false;
let gameFocusEventId='';
const mapTerrain={data:null,size:0,key:'',pending:false,retryAt:0};
function terrainSessionKey(){
  return [state?.config.self.user_id,state?.map_config_id||'',
    Boolean(state?.health.scope_pending||state?.health.identity_pending)].join(':');
}
async function loadMapTerrain(){
  const key=terrainSessionKey();
  if(mapTerrain.key!==key){mapTerrain.data=null;mapTerrain.size=0;mapTerrain.key=key;mapTerrain.retryAt=0;}
  if(mapTerrain.data||mapTerrain.pending||Date.now()<mapTerrain.retryAt||
    state?.health.scope_pending||state?.health.identity_pending)return;
  mapTerrain.pending=true;
  try{
    const response=await fetch('/api/map-terrain',{cache:'no-store',signal:AbortSignal.timeout(6000)});
    if(!response.ok)throw new Error('地图地形未就绪');
    const size=Number(response.headers.get('X-Map-Size')),config=Number(response.headers.get('X-Map-Config'));
    const data=new Uint8Array(await response.arrayBuffer());
    if(!Number.isInteger(size)||size<1||size>4096||data.length!==size*size||
      state?.map_config_id&&state.map_config_id!==config)throw new Error('地图地形不匹配');
    if(key===terrainSessionKey()&&mapTerrain.key===key){mapTerrain.data=data;mapTerrain.size=size;draw();}
  }catch{if(mapTerrain.key===key)mapTerrain.retryAt=Date.now()+30000;}
  finally{mapTerrain.pending=false;}
}
const displayPreferenceKey='stzb.battlefield.display.v1';
function readDisplayPreferences(){
  try{
    const saved=JSON.parse(globalThis.localStorage?.getItem(displayPreferenceKey)||'{}');
    return {showSpartans:saved?.showSpartans!==false,mapLabels:saved?.mapLabels!==false,mapGrid:saved?.mapGrid!==false,guardCoverage:saved?.guardCoverage!==false};
  }catch{return {showSpartans:true,mapLabels:true,mapGrid:true,guardCoverage:true};}
}
let displayPreferences=readDisplayPreferences();
function renderDisplayPreferences(){
  $('showSpartanAlerts').checked=displayPreferences.showSpartans;
  $('showMapLabels').checked=displayPreferences.mapLabels;
  $('showMapGrid').checked=displayPreferences.mapGrid;
  $('showGuardCoverage').checked=displayPreferences.guardCoverage;
}
function setDisplayPreference(key,value){
  if(!['showSpartans','mapLabels','mapGrid','guardCoverage'].includes(key))return;
  displayPreferences={...displayPreferences,[key]:Boolean(value)};
  try{globalThis.localStorage?.setItem(displayPreferenceKey,JSON.stringify(displayPreferences));}
  catch{toast('设置已生效；当前浏览器无法保存，重新打开后需再次设置',true);}
  const row=selectedRow();
  if(row&&!mapArmyEnabled(row)){
    selected=null;selectedMarch=null;dismissAlertPopup();setInspectorView('watch');
  }
  renderDisplayPreferences();renderWatch();renderDetails();draw();
}
let selectedMarch=null, alertPopupAnchor=null, selectedGroup=null;
let gameLocating=false;
let mapPlacePopup=null, mapPlaceHoverBlocked='', mapPlacePendingReads=0;
let alertPredictions={}, forecastKey='', forecastCheckedAt=0, forecastLoading=false, forecastFailed=false;
let campaigns=[], campaignFocus=new URLSearchParams(globalThis.location?.search).get('campaign')||'', campaignsCheckedAt=0;
const $=id=>document.getElementById(id);
const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const relationNames={self:'自己',ally:'同盟',friendly:'友好',enemy:'敌方',unknown:'未确认'};
const colors={self:'#2456bd',ally:'#276c7d',friendly:'#3173a7',enemy:'#af433c',unknown:'#60748f'};
const factionGroups={
  own:{label:'己方',color:'#276c7d',short:'盟'},
  enemy_union:{label:'敌对盟',color:'#af433c',short:'敌'},
  friendly_union:{label:'友好盟',color:'#af433c',border:'#3173a7',short:'友'},
  neutral:{label:'普通中立',color:'#737d8c',short:'中'},
  wanderer:{label:'流浪军',color:'#8453ab',short:'流'},
  enemy_wanderer:{label:'敌对流浪军',color:'#bd5a18',short:'敌流'},
  friendly_wanderer:{label:'友好流浪军',color:'#8453ab',border:'#3173a7',short:'友流'}
};
const defaultFactionStyles=JSON.parse(JSON.stringify(factionGroups));
const defaultRelationColors={...colors};
let clientPaletteKey='',clientSelfPalette=null,clientPaletteSheet=null;
function blendPaletteColor(hex,white){
  return '#'+[1,3,5].map(i=>Math.round(parseInt(hex.slice(i,i+2),16)*(1-white)+255*white).toString(16).padStart(2,'0')).join('');
}
function softenGameColor(hex){
  // Keep the client's hue while bounding saturation and lightness for a dense map.
  const rgb=[1,3,5].map(i=>parseInt(hex.slice(i,i+2),16)/255);
  const high=Math.max(...rgb),low=Math.min(...rgb),delta=high-low,light=(high+low)/2;
  let hue=0;
  if(delta){
    hue=(high===rgb[0]?(rgb[1]-rgb[2])/delta:high===rgb[1]?(rgb[2]-rgb[0])/delta+2:(rgb[0]-rgb[1])/delta+4)/6;
    hue=(hue+1)%1;
  }
  const saturation=Math.min(.46,delta?delta/(1-Math.abs(2*light-1))*.62:0);
  const level=Math.max(.40,Math.min(.61,.50+(light-.50)*.55));
  const chroma=(1-Math.abs(2*level-1))*saturation,offset=level-chroma/2;
  const x=chroma*(1-Math.abs(hue*6%2-1));
  const channels=[[chroma,x,0],[x,chroma,0],[0,chroma,x],[0,x,chroma],[x,0,chroma],[chroma,0,x]][Math.min(5,Math.floor(hue*6))];
  return '#'+channels.map(c=>Math.round((c+offset)*255).toString(16).padStart(2,'0')).join('');
}
function softRelationStyle(style){return {inside:softenGameColor(style.inside),outside:softenGameColor(style.outside)};}
function paletteCss(selector,style){
  // Game colors identify the relation; darker text remains readable on a light card.
  const ink=blendPaletteColor(style.inside,0).replace(/[0-9a-f]{2}/g,part=>Math.round(parseInt(part,16)*.48).toString(16).padStart(2,'0'));
  return `${selector}{--faction-ink:${ink};--faction-border:${style.outside};--faction-tint:${blendPaletteColor(style.inside,.93)};--faction-fill:${style.inside};}`;
}
function syncClientRelationColors(){
  const p=state?.health?.relation_colors,own=state?.config?.self;
  const keys={own:'union',enemy_union:'enemy',friendly_union:'friend_other',neutral:'other',
    wanderer:'bandit',enemy_wanderer:'bandit',friendly_wanderer:'friend_bandit'};
  const names=['self',...new Set(Object.values(keys))];
  const valid=p?.status==='ready'&&p.source==='client_map_palette'&&state.health.status==='live'&&
    !state.health.identity_pending&&!state.health.scope_pending&&p.user_id===own?.user_id&&p.own_union_id===own?.union_id&&
    Number.isFinite(p.last_seen_at)&&state.server_now-p.last_seen_at<=30&&p.last_seen_at<=state.server_now+1&&
    names.every(key=>['inside','outside'].every(part=>/^#[0-9a-f]{6}$/.test(p.styles?.[key]?.[part])));
  const key=valid?JSON.stringify([p.user_id,p.own_union_id,p.color_mode,p.styles]):'';
  if(key===clientPaletteKey)return;
  clientPaletteKey=key;
  if(!valid){
    for(const group of Object.keys(factionGroups)){
      delete factionGroups[group].border;Object.assign(factionGroups[group],defaultFactionStyles[group]);
    }
    Object.assign(colors,defaultRelationColors);ownArmyColor='#218a47';clientSelfPalette=null;
    if(clientPaletteSheet)clientPaletteSheet.replaceSync('');
    return;
  }
  const css=[];
  for(const [group,name]of Object.entries(keys)){
    const style=softRelationStyle(p.styles[name]);
    Object.assign(factionGroups[group],{color:style.inside,border:style.outside});
    css.push(paletteCss('.faction-'+group,style));
  }
  clientSelfPalette=softRelationStyle(p.styles.self);ownArmyColor=clientSelfPalette.inside;
  Object.assign(colors,{self:ownArmyColor,ally:factionGroups.own.color,friendly:factionGroups.friendly_union.border,
    enemy:factionGroups.enemy_union.color});
  css.push(paletteCss('.faction-own.faction-self',clientSelfPalette),
    'html .inspector .place-armies[class*="faction-"]>summary{background:var(--faction-tint);color:var(--faction-ink);}',
    '.legend-faction[class*="faction-"]{background:var(--faction-fill);}',
    `.legend-own-army{background:${ownArmyColor};}`);
  // A constructed sheet keeps the existing strict style-src policy intact.
  if(!clientPaletteSheet){clientPaletteSheet=new CSSStyleSheet();document.adoptedStyleSheets=[...document.adoptedStyleSheets,clientPaletteSheet];}
  clientPaletteSheet.replaceSync(css.join('\n'));
}
function factionGroup(row){
  if(Object.hasOwn(factionGroups,row?.faction_group))return row.faction_group;
  if(['self','ally'].includes(row?.relation))return 'own';
  const relation=row?.declared_relation||(!row?.is_wanderer?row?.relation:'unknown');
  return row?.is_wanderer?({enemy:'enemy_wanderer',friendly:'friendly_wanderer'}[relation]||'wanderer'):
    ({enemy:'enemy_union',friendly:'friendly_union'}[relation]||'neutral');
}
function factionLabel(row){return factionGroups[factionGroup(row)].label;}
function factionClass(row){return 'faction-'+factionGroup(row)+(row?.relation==='self'?' faction-self':'');}
function factionBadge(row){
  const group=factionGroup(row);
  return `<small class="faction-badge ${factionClass(row)}${row?.is_wanderer?' wanderer-badge':''}">${factionGroups[group].label}${group==='own'&&row?.is_wanderer?' · 流浪军':''}</small>`;
}
const captureNames={pc:'PC版',mumu:'MuMu',dashen:'大神模拟器'};
const coord=p=>p?`${p[0]}, ${p[1]}`:'未知';
const placeLabel=(point,name)=>name&&point&&name.replace(/\s/g,'')!==point.join(',')?`${name}（${coord(point)}）`:coord(point);
function alertTargetKey(value){
  if(!/^\d{1,4},\d{1,4}$/.test(value??''))return '';
  const point=value.split(',').map(Number);
  return point.every(n=>n>0)?point.join(','):'';
}
let alertFocus=alertTargetKey(new URLSearchParams(globalThis.location?.search).get('target'));
const focusedPoint=()=>alertFocus?alertFocus.split(',').map(Number):null;
const isFocusedTarget=point=>Boolean(alertFocus&&point?.join(',')===alertFocus);
function gameLocateButton(point){
  const key=alertTargetKey(point?.join(','));
  return state?.health.capture_source==='pc'&&key?`<button type="button" class="text-button game-locate" data-game-coord="${key}">游戏内定位</button>`:'';
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
function mapCoordinateTicks(){
  if(map.width<180||map.height<200)return [];
  const step=[5,10,20,50,100].find(n=>n*map.scale*.9>=54)||100;
  const ticks=[],left=14,right=map.width-14,top=90,bottom=map.height-82;
  const add=(axis,a,b,start,end)=>{
    const low=Math.min(a,b),high=Math.max(a,b);
    for(let value=Math.ceil(low/step)*step;value<=high;value+=step){
      if(value<1||value>9999)continue;
      const position=start+(value-a)/(b-a)*(end-start);
      ticks.push({axis,value,x:axis==='X'?position:right,y:axis==='X'?bottom:position});
    }
  };
  add('X',unproject(left,bottom)[0],unproject(right,bottom)[0],left,right);
  add('Y',unproject(right,top)[1],unproject(right,bottom)[1],top,bottom);
  return ticks;
}
function mapPlaceName(point){
  if(!state||!point)return '';
  const key=point.join(','),focus=state.game_focus;
  if(focus?.coord?.join(',')===key&&focus.name)return focus.name;
  return (state.forts||[]).find(item=>item.coord.join(',')===key)?.name||
    (state.land_tiles||[]).find(item=>item.coord.join(',')===key)?.name||
    state.alerts.find(item=>item.coord?.join(',')===key)?.target_name||'';
}
const now=()=>Date.now()/1000+clockOffset;
function activeObservedAttack(row){
  return state?.live&&!row.stale&&['enemy','unknown'].includes(row.relation)&&row.kind==='attack'&&
    row.from&&row.target&&row.from.some((value,index)=>value!==row.target[index])&&row.end>now();
}
const watchedTarget=()=>{const target=state.targets?.[Math.min(watchIndex,state.targets.length-1)];if(target)return target;const a=state.area,p=a?.length===4?[(a[0]+a[2])/2,(a[1]+a[3])/2]:currentArmies().find(r=>r.display_position)?.display_position||[0,0];return{name:'当前视野',x:p[0],y:p[1],virtual:true};};
function duration(seconds){if(seconds==null)return '—';if(seconds<=0)return '待更新';const n=Math.floor(seconds);return `${String(Math.floor(n/3600)).padStart(2,'0')}:${String(Math.floor(n/60)%60).padStart(2,'0')}:${String(n%60).padStart(2,'0')}`;}
function ageText(seconds){return seconds<2?'刚刚':seconds<60?`${Math.floor(seconds)} 秒前`:`${Math.floor(seconds/60)} 分钟前`;}
function remaining(row){return row.stale?'观测过期':!state?.live?'数据中断':['garrison','stay'].includes(row.kind)&&row.end<=now()?'—':duration(row.end>0?row.end-now():null);}
function subscriptionActive(row){
  return Boolean(state?.live&&state.reliable!==false&&row.visible&&row.current_connection===true&&
    row.subscription_valid_until>0&&now()<=row.subscription_valid_until);
}
function updateFreshness(){
  if(!state)return;
  const h=state.health, stamp=now();
  const validUntil=state.transport_valid_until??(h.last_packet_at+10);
  state.live=apiConnected&&h.status==='live'&&stamp<=validUntil;
  if(state.transport_valid_until!=null)state.reliable=state.live&&!h.schema_errors&&Boolean(h.last_scene_at)&&
    (!h.resync_required||stamp-h.last_scene_at<=15);
  for(const r of state.armies){
    r.age=Math.max(0,stamp-(r.observed_at||r.seen_at));
    r.stale=(r.age>60&&!subscriptionActive(r))||!r.visible||r.current_connection===false;
  }
  state.alerts=state.alerts.filter(a=>state.live&&state.reliable!==false&&state.armies.some(r=>
    r.id===a.army_id&&!r.stale&&(r.age<=15||subscriptionActive(r))&&a.end>stamp)).sort((a,b)=>a.end-b.end);
  state.counts.fresh=state.armies.filter(r=>!r.stale).length;
  state.counts.unmapped=state.armies.filter(r=>!r.stale&&!r.kind).length;
}
function toast(text,error=false){$('toast').textContent=text;$('toast').className=`show${error?' error':''}`;clearTimeout(toast.timer);toast.timer=setTimeout(()=>$('toast').className='',3800);}
async function save(payload){const response=await fetch('/api/calibrate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});const data=await response.json();if(!response.ok||!data.ok)throw new Error(data.error||'保存失败');await refresh();toast('本地校准已保存');}
function currentArmies(){return state?.armies.filter(row=>!row.stale)||[];}
let ownArmyColor='#218a47';
const placeArmyOrder={self:0,ally:1,friendly:2,enemy:3,unknown:4};
const openPlaceArmies=new Set(),placeLoadouts=new Map();
const lineupRefreshMs=15000;
let selectedTacticsOpen=false;
const compactInspector=()=>document.documentElement?.classList?.contains?.('desktop-compact')===true;
let placeIndexState=null,placeIndexStamp='',placeIndex=new Map();
function observedArmyLabel(row){
  // Verified against the official client's ArmyState enum; location is checked separately.
  if(row.state_code===7&&row.target_type===13)return '战斗中';
  return ({1:'进攻中',2:'调动中',3:'前往驻守',4:'返程中',5:'要塞内',6:'驻守中',7:'战间休整',25:'停留'})[row.state_code]||row.state_label||'状态未确认';
}
function placeArmyStateText(row){
  const label=observedArmyLabel(row);
  return label==='战斗中'&&row.end>now()?`${label} ${duration(row.end-now())}`:label;
}
function placeArmyGroups(){
  const stamp=[Math.floor(now()),state?.live,state?.reliable,state?.health.resync_required,state?.counts.fresh].join('|');
  if(placeIndexState===state&&placeIndexStamp===stamp)return placeIndex;
  placeIndexState=state;placeIndexStamp=stamp;placeIndex=new Map();
  if(!state?.live||state.reliable===false||state.health.resync_required)return placeIndex;
  const ids=new Set();
  for(const row of currentArmies()){
    // Never use a home/reside coordinate or a march destination as a present garrison.
    const point=row.position;
    if(ids.has(row.id)||row.visible===false||row.current_connection===false||!point||
      coord(row.from)!==coord(row.target)||coord(point)!==coord(row.from)||!inRegion(point,state.area))continue;
    ids.add(row.id);
    const key=point.join(',');
    if(!placeIndex.has(key))placeIndex.set(key,{point,rows:[]});
    placeIndex.get(key).rows.push(row);
  }
  for(const group of placeIndex.values())group.rows.sort((a,b)=>(placeArmyOrder[a.relation]??4)-(placeArmyOrder[b.relation]??4)||
    a.player_name.localeCompare(b.player_name,'zh-CN')||a.id.localeCompare(b.id,undefined,{numeric:true}));
  return placeIndex;
}
function placeArmyCountText(rows){
  const own=rows.filter(row=>row.relation==='self').length;
  return [own?`自己 ${own}`:'',...Object.entries(factionGroups).map(([key,group])=>{
    const n=rows.filter(row=>row.relation!=='self'&&factionGroup(row)===key).length;return n?`${group.label} ${n}`:'';
  })].filter(Boolean).join(' · ');
}
let guardIndexState=null,guardIndexStamp='',guardIndex=new Map();
function guardCoverageIndex(){
  // One nine-cell index per snapshot/clock tick; never query the archive per tile.
  const stamp=[Math.floor(now()),state?.live,state?.reliable,state?.health.resync_required].join('|');
  if(guardIndexState===state&&guardIndexStamp===stamp)return guardIndex;
  guardIndexState=state;guardIndexStamp=stamp;guardIndex=new Map();
  if(!state?.live||state.reliable===false||state.health.resync_required)return guardIndex;
  const tiles=new Map([...(state.land_tiles||[]),...(state.forts||[])].map(t=>[t.coord.join(','),t]));
  const realId=value=>value!=null&&String(value)!==''&&String(value)!=='0';
  const ids=new Set(),area=state.area;
  if(!Array.isArray(area)||area.length!==4)return guardIndex;
  const halo=[area[0]-1,area[1]-1,area[2]+1,area[3]+1];
  for(const row of currentArmies()){
    // Being inside a fort, resting, staying, or heading to guard is not an active guard.
    const center=row.position;
    if(row.state_code!==6||ids.has(row.id)||row.visible===false||row.current_connection===false||!center||
      coord(row.from)!==coord(row.target)||coord(center)!==coord(row.from)||!inRegion(center,halo))continue;
    ids.add(row.id);
    for(let dx=-1;dx<=1;dx++)for(let dy=-1;dy<=1;dy++){
      const point=[center[0]+dx,center[1]+dy];
      if(!inRegion(point,state.area)||point.some(n=>n<1))continue;
      const tile=tiles.get(point.join(',')),direct=dx===0&&dy===0;
      const sameOwner=tile&&realId(tile.owner_id)&&String(tile.owner_id)===String(row.owner);
      const sameUnion=tile&&realId(tile.union_id)&&realId(row.union_id)&&String(tile.union_id)===String(row.union_id);
      const knownOther=tile&&(realId(tile.union_id)&&realId(row.union_id)||tile.owner_id==='0');
      if(!direct&&!sameOwner&&!sameUnion&&knownOther)continue;
      // Land armies do not project normal nine-cell defense onto rivers/mountains.
      const terrain=mapTerrain.data&&point.every(n=>n<=mapTerrain.size)?
        mapTerrain.data[(point[0]-1)*mapTerrain.size+point[1]-1]&7:null;
      if(!direct&&[1,3].includes(terrain))continue;
      const key=point.join(',');
      if(!guardIndex.has(key))guardIndex.set(key,{point,entries:[]});
      guardIndex.get(key).entries.push({row,center,direct,
        confirmed:direct||Boolean(sameOwner||sameUnion)});
    }
  }
  return guardIndex;
}
function guardEncounter(point){
  const entries=guardCoverageIndex().get(point?.join(','))?.entries||[];
  // EndTime is used for REINFORCE in ArmyListAssist.GetListInPos/FightSortRuler.
  // ResideTime is for being inside a fort; it must not replace guard arrival time.
  const timed=e=>e.row.end>0&&e.row.end<=now()&&!e.row.army_group_id;
  const compare=(a,b)=>Number(timed(b))-Number(timed(a))||
    (timed(a)&&timed(b)?b.row.end-a.row.end||String(a.row.id).localeCompare(String(b.row.id),undefined,{numeric:true}):0);
  return {confirmed:entries.filter(e=>e.confirmed).sort(compare),
    unverified:entries.filter(e=>!e.confirmed).sort(compare)};
}
function renderGuardCoverage(){
  const section=$('guardCoverage'),point=focusedPoint();
  section.hidden=!point;
  if(!point)return;
  const {confirmed,unverified}=guardEncounter(point),list=$('guardCoverageList');
  const direct=confirmed.filter(e=>e.direct).length;
  $('guardCoverageTitle').textContent='防守覆盖 · 预计触敌顺序';
  $('guardCoverageCount').textContent=`本格 ${direct} · 周边 ${confirmed.length-direct}`;
  $('guardCoverageNote').textContent=confirmed.length?
    '按驻守到达时间后到在前估算；跨格迎战顺序待实战核对。':
    '暂无已确认覆盖；未观测到不代表没有驻守。';
  const markup=(entries,uncertain)=>entries.map((entry,index)=>{
    const {row,center,direct}=entry,cached=placeLoadouts.get(placeLoadoutKey(row));
    requestMapPlaceLoadout(row);
    const overview=cached?.data?placeLoadoutOverview(row,cached.data):lineupOverview();
    const order=!uncertain&&row.end>0&&row.end<=now()&&!row.army_group_id?String(index+1):'?';
    const group=String(row.union_id&&String(row.union_id)!=='0'?row.union_id:row.owner);
    return `<details class="guard-unit ${esc(row.relation)} ${factionClass(row)}" data-guard-army="${esc(row.id)}" data-guard-group="${esc(group)}">
      <summary><span class="guard-order" title="同一防守方内的预计顺序">${order}</span><strong>${armyPlayerButton(row)}</strong>
      <span></span>
      <small>${uncertain?'归属未确认 · ':''}${direct?'本格驻守':'辐射自 '+coord(center)}${row.end>0&&row.end<=now()?' · 到达 '+esc(forceReportClock.format(row.end*1000)):''}</small>
      <span class="lineup-roster">${overview.rosterMarkup}</span>
      <small class="guard-report-meta">${esc(overview.meta)}</small></summary>
      <div class="guard-unit-loadout">${cached?.data?placeLoadoutMarkup(row,cached.data):'<p>正在读取武将…</p>'}</div></details>`;
  }).join('');
  // Separate opposing alliances: never invent an encounter queue spanning both sides.
  const groups=new Map();
  for(const entry of confirmed){const key=String(entry.row.union_id&&String(entry.row.union_id)!=='0'?entry.row.union_id:entry.row.owner);
    if(!groups.has(key))groups.set(key,[]);groups.get(key).push(entry);}
  const html=[...groups.values()].map(entries=>(groups.size>1&&entries[0].row.union_name?
    `<p class="guard-coverage-note">${esc(entries[0].row.union_name)}</p>`:'')+markup(entries,false)).join('')+
    (unverified.length?`<p class="guard-coverage-note">范围内另有 ${unverified.length} 队，目标归属未确认</p>`+markup(unverified,true):'');
  if(list.dataset.markup!==html){
    const open=new Set([...list.querySelectorAll('details[open]')].map(el=>el.dataset.guardArmy));
    list.innerHTML=html;list.dataset.markup=html;
    list.querySelectorAll('details').forEach(el=>{el.open=open.has(el.dataset.guardArmy);});
  }
}
function placeLoadoutKey(row){return [state?.health?.instance_id,state?.config?.self?.user_id,row.owner,row.union_id,row.player_name,row.id,row.troops?.map(t=>t.code),state?.health?.report_priority?.updates?.[row.id]].join('|');}
function lineupObservationKey(row){
  return [placeLoadoutKey(row),row.signature,row.state_code,row.begin,row.end,coord(row.from),coord(row.target)].join('|');
}
function armyPlayerButton(row){
  return `<button type="button" class="army-player-button" data-place-details="${esc(row.id)}" title="查看部队详情">${esc(row.player_name)}</button>${armyGroupBadge(row)}`;
}
function wandererBadge(row){
  return row?.is_wanderer?factionBadge(row):'';
}
function rememberArmyGroup(row,data){
  if(!['exact_army_id','matched_arrival','same_card_instances'].includes(data.match))return;
  const report=data.items?.find(item=>item.battle_id===data.current_report_id);
  const gid=String(row.army_group_id||'');
  if(report?.data.army_group_name&&String(report.data.report_army_group_id)===gid){
    const old=resolvedGroupNames.get(gid);
    if(!old||old.at<=report.event_at)resolvedGroupNames.set(gid,{name:report.data.army_group_name,at:report.event_at});
    while(resolvedGroupNames.size>128)resolvedGroupNames.delete(resolvedGroupNames.keys().next().value);
  }
}
function armyGroupBadge(row){
  if(!row.army_group_id)return '';
  const gid=String(row.army_group_id),name=resolvedGroupNames.get(gid)?.name||row.army_group_name||`军团 ${gid}`;
  return `<small class="army-group-badge" title="${esc(name)}">${esc(name)}</small>`;
}
function placeLoadoutReports(row,data){
  if(data.live_lineup){
    const report=data.prediction?.army_id===row.id?currentLineupReport(data.prediction):null;
    return report?(data.items||[]).filter(item=>item.battle_id===report.battle_id&&item.player_name===row.player_name&&String(item.union_id)===String(row.union_id)).slice(0,1):[];
  }
  if(row.army_group_id&&!['exact_army_id','matched_arrival','same_card_instances'].includes(data.match))return [];
  const items=(data.items||[]).filter(item=>item.player_name===row.player_name&&String(item.union_id)===String(row.union_id))
    .sort((a,b)=>b.event_at-a.event_at||String(b.battle_id??'').localeCompare(String(a.battle_id??''),undefined,{numeric:true}));
  const live=row.troops?.filter(t=>t.code>0).map(t=>t.code)||[];
  if(!live.length&&['exact_army_id','matched_arrival','same_card_instances'].includes(data.match)){
    return items.filter(item=>!data.current_report_id||item.battle_id===data.current_report_id).slice(0,1);
  }
  const matching=items.filter(item=>{
    const heroes=[...(item.data?.heroes||[])].sort((a,b)=>a.position-b.position);
    const codes=heroes.map(h=>h.troop_code);
    return live.length&&codes.length===live.length&&codes.every((code,i)=>Number.isInteger(code)&&code===live[i]);
  });
  // Keep the summary and expanded real-time card on the same best current reference.
  // Other matching lineups remain in the archive response and are not current armies.
  return matching.slice(0,1);
}
function placeLoadoutOverview(row,data){
  const report=placeLoadoutReports(row,data)[0];
  if(!report)return lineupOverview();
  return lineupOverview(report.data.heroes,report.event_at);
}
function postBattleTroops(heroes){
  if(!Array.isArray(heroes)||!heroes.length)return null;
  let total=0;
  for(const hero of heroes){
    const remaining=hero?.remaining_hp,wounded=hero?.wounded_hp;
    if(!Number.isSafeInteger(remaining)||remaining<0||!Number.isSafeInteger(wounded)||wounded<0)return null;
    total+=remaining+wounded;
    if(!Number.isSafeInteger(total))return null;
  }
  return total;
}
function postBattleTroopsText(heroes){
  const total=postBattleTroops(heroes);
  return `战后余兵 ${total==null?'未知':forceNumber(total)}`;
}
function lineupOverview(roster,eventAt){
  const heroes=[...(roster||[])].sort((a,b)=>(a.position??0)-(b.position??0));
  if(!heroes.length)return {roster:'武将信息未取得',rosterMarkup:esc('武将信息未取得'),meta:'最近战报未取得'};
  const names=heroes.map(h=>`${h.name}${Number.isInteger(h.level)&&h.level>0?h.level:'?'}级(${h.red_stars==null?'红度未知':h.red_stars+'红'})`).join(' / ');
  const nameUnits=heroes.reduce((sum,h)=>sum+Array.from(String(h.name??'')).reduce((width,c)=>width+(/[^\x00-\xff]/.test(c)?1:.6),0),0)||1;
  const rosterMarkup='<span class="lineup-inline" data-density="'+(nameUnits>10?'long':nameUnits>8?'medium':'short')+'">'+heroes.map(h=>{
    const redness=h.red_stars==null?'红度未知':h.red_stars+'红';
    const tone=h.red_stars===0?' is-zero':h.red_stars===5?' is-full':'';
    const level=Number.isInteger(h.level)&&h.level>0?`${h.level}级`:'?级';
    return `<span class="lineup-hero"><span class="lineup-hero-name">${esc(h.name)}</span><span class="lineup-hero-level" aria-label="${level==='?级'?'等级未取得':esc('等级 '+level)}">${esc(level)}</span><span class="lineup-redness${tone}" aria-label="${esc('红度 '+redness)}">${esc(redness)}</span></span>`;
  }).join('<span class="lineup-separator" aria-hidden="true">/</span>')+'</span>';
  const time=eventAt>0?forceReportClock.format(eventAt*1000):'时间未知';
  return {roster:names,rosterMarkup,meta:`最近战报 ${time} · ${postBattleTroopsText(heroes)}`};
}
function armyRosterLabel(row){
  const cached=placeLoadouts.get(placeLoadoutKey(row))?.data;
  const report=cached&&placeLoadoutReports(row,cached)[0];
  if(report)return lineupOverview(report.data.heroes).roster;
  const prediction=predictionFor(row.id);
  const live=prediction?.army_id===row.id&&!prediction.current_resolution?.unresolved?currentLineupReport(prediction):null;
  return live?.heroes?.length?lineupOverview(live.heroes).roster:'武将资料未核实';
}
function placeLoadoutMarkup(row,data){
  const reports=placeLoadoutReports(row,data);
  if(!reports.length)return '<p>武将信息暂未取得</p>';
  const exact=['exact_army_id','matched_arrival','same_card_instances'].includes(data.match);
  return reports.map(item=>`<div class="place-report"${exact?'':' title="该玩家的配置记录，当前队伍对应关系待核对"'}>
    ${heroLoadouts([...item.data.heroes].sort((a,b)=>a.position-b.position),true,false,{showTroops:false,showLevels:true})}</div>`).join('');
}
async function loadPlaceArmy(row,panel){
  const key=placeLoadoutKey(row),cached=placeLoadouts.get(key),stamp=Date.now();
  if(cached?.pending)return;
  const apply=data=>{
    if(!panel.isConnected||placeLoadoutKey(row)!==key||!placeArmyGroups().get(focusedPoint()?.join(','))?.rows.some(r=>r.id===row.id&&lineupObservationKey(r)===lineupObservationKey(row)))return;
    rememberArmyGroup(row,data);
    const markup=placeLoadoutMarkup(row,data);
    if(panel.dataset.markup!==markup){panel.innerHTML=markup;panel.dataset.markup=markup;}
    const details=panel.closest?.('.place-army');
    if(details){
      const overview=placeLoadoutOverview(row,data);
      const rosterLabel=details.querySelector('.place-army-roster');
      if(rosterLabel&&rosterLabel.dataset.markup!==overview.rosterMarkup){
        rosterLabel.innerHTML=overview.rosterMarkup;rosterLabel.dataset.markup=overview.rosterMarkup;rosterLabel.title=overview.roster;
      }
      const reportLabel=details.querySelector('.place-army-report-meta');
      if(reportLabel&&reportLabel.textContent!==overview.meta){reportLabel.textContent=overview.meta;reportLabel.title=overview.meta;}
    }
  };
  if(cached&&stamp-cached.at<lineupRefreshMs){if(cached.data)apply(cached.data);return;}
  const entry={at:stamp,pending:true,data:cached?.data};placeLoadouts.set(key,entry);
  if(!panel.innerHTML)panel.innerHTML='<p>正在读取该玩家的战报配置…</p>';
  try{
    const response=await fetch('/api/army-lineups?'+new URLSearchParams({army_id:row.id}),
      {cache:'no-store',signal:AbortSignal.timeout(6000)});
    if(!response.ok)throw new Error('档案未连接');
    entry.data=cacheLiveLineup(row,await response.json());apply(entry.data);
  }catch{if(panel.isConnected)panel.textContent='阵容档案暂未连接，稍后自动重试。';entry.at=Date.now()-15000;}
  finally{entry.pending=false;while(placeLoadouts.size>120)placeLoadouts.delete(placeLoadouts.keys().next().value);}
}
function renderPlaceArmies(){
  const section=$('placeArmies'),list=$('placeArmiesList'),point=focusedPoint();
  section.hidden=!point;
  if(!point)return;
  const coverage=guardEncounter(point);
  const guardIds=new Set([...coverage.confirmed,...coverage.unverified].map(entry=>entry.row.id));
  // Keep each army in one sidebar section. Physical occupancy and map badges stay intact.
  const rows=(placeArmyGroups().get(point.join(','))?.rows||[]).filter(row=>!guardIds.has(row.id));
  const groups=new Set(rows.map(factionGroup));
  for(const key of Object.keys(factionGroups))section.classList.toggle('faction-'+key,groups.size===1&&groups.has(key));
  section.hidden=!rows.length&&guardIds.size>0;
  const sectionName=rows.some(row=>![5,25].includes(row.state_code))?'此处队伍':'要塞内/停留';
  $('placeArmiesSummary').textContent=`${sectionName} ${rows.length}`;
  const key=[point.join(','),...rows.map(row=>[row.id,row.owner,row.union_id,row.player_name,row.relation,factionGroup(row),row.force_type,row.state_code,row.troops?.map(t=>t.code)].join(':'))].join('|');
  if(list.dataset.key!==key){
    list.dataset.key=key;
    list.innerHTML=rows.map(row=>`<details class="place-army ${factionClass(row)}" data-place-army="${esc(row.id)}"${openPlaceArmies.has(row.id)?' open':''}>
      <summary class="place-army-head"><span class="place-army-heading"><strong>${armyPlayerButton(row)}</strong><span class="place-army-roster lineup-roster" aria-label="大营、中军、前锋武将及红度">正在读取武将…</span></span><span class="place-side ${esc(row.relation)}"><span data-place-state>${esc(placeArmyStateText(row))}</span></span><small class="place-army-report-meta">正在读取最近战报…</small></summary>
      <div class="place-army-loadout"></div></details>`).join('')||
      '<p class="place-empty">当前未收到此处队伍，不代表没有驻军。</p>';
  }
  for(const details of list.querySelectorAll('.place-army')){
    const row=rows.find(r=>r.id===details.dataset.placeArmy);
    if(row){
      const status=details.querySelector('[data-place-state]');
      if(status)status.textContent=placeArmyStateText(row);
      loadPlaceArmy(row,details.querySelector('.place-army-loadout'));
    }
  }
}
function selectedRow(){
  return selected?state?.armies.find(row=>row.id===selected&&!row.stale):null;
}
function setInspectorView(view, open=true, keepScroll=false){
  const details=view==='army'&&Boolean(selected||selectedGroup);
  if(details||!open)dismissAlertPopup();
  $('armyDetails').hidden=!details;
  $('watchPanel').hidden=details;
  $('armyTab').disabled=!selected&&!selectedGroup;
  $('armyTab').setAttribute('aria-pressed',String(details));
  $('watchTab').setAttribute('aria-pressed',String(!details));
  $('inspector').dataset.selected=String(Boolean(selected||selectedGroup));
  if(!keepScroll)$('inspector').scrollTop=0;
  document.querySelector('.workspace').classList.toggle('inspector-open',open);
  $('compactWatchStatus').setAttribute('aria-expanded',String(open));
}
function selectArmy(id, incoming=null){
  selectedGroup=null;
  closeMapPlacePopup();
  dismissAlertPopup();
  if(id!==selected)selectedTacticsOpen=false;
  selected=id;
  selectedMarch=incoming?marchKey(incoming.row):null;
  alertPopupAnchor=incoming?.anchor??null;
  inspectorKey='';
  setInspectorView(id&&!incoming?'army':'watch',Boolean(id),Boolean(incoming));
  renderDetails();
  renderRoster();
  if(incoming){
    renderWatch();
  }
  draw();
}
const rosterOrder=new Intl.Collator('zh-CN',{numeric:true});
const rosterButtons=new Map();
function renderRoster(){
  if($('rosterPane').hidden)return;
  const rows=currentArmies();
  const pinned=selectedRow();
  $('listCount').textContent=`${rows.length} 支`;
  $('rosterSelection').hidden=!pinned;
  if(pinned){
    $('selectedPlayer').textContent=pinned.player_name;
    $('selectedNumber').textContent=armyRosterLabel(pinned);
    $('selectedStatus').textContent=pinned.state_label;
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
    button.className='roster-item '+factionClass(row);
    const position=coord(row.display_position), target=coord(row.target);
    const key=JSON.stringify([row.player_name,row.relation,factionGroup(row),row.union_name,position,target,row.state_label,armyRosterLabel(row)]);
    if(button.dataset.content!==key){
      button.innerHTML=`<div class="row-top"><span class="player-name">${esc(row.player_name)}</span></div>
        <div class="row-union">${esc(armyRosterLabel(row))}</div>
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
    empty.textContent=pinned?'只有已选部队':'等待当前部队数据';
  }
}
function updateRosterTime(button,row){
  const timer=button.querySelector('time');
  const value=remaining(row);
  if(timer.textContent!==value)timer.textContent=value;
}
const troopText=(name,code)=>code>3&&['弓兵','步兵','骑兵'].includes(name)?`${name} #${code}`:name;
function liveTroopText(troops){
  return (troops||[]).map(t=>`${t.position}位 ${troopText(t.name,t.code)}`).join(' · ');
}
function troopDisplay(troops, label, observedAt=null){
  const chips=troops?.length
    ? troops.map(t=>`<span class="troop-slot" title="位置 ${esc(t.position)} · 编码 ${esc(t.code)}">${esc(troopText(t.name,t.code))}</span>`).join('')
    : '<span class="troop-unknown">暂未取得</span>';
  const time=observedAt?`<small>${esc(new Date(observedAt*1000).toLocaleString('zh-CN',{hour12:false}))}</small>`:'';
  return `<div class="troop-label">${esc(label)}</div><div class="troop-slots">${chips}</div>${time}`;
}
function detailFreshness(row){
  const age=ageText(Math.max(0,now()-(row.observed_at||row.seen_at)));
  return '最后观测 · '+age;
}
function unitLocation(row,showTimer){
  const point=row.display_position||row.target;
  const destination=row.display_position&&row.target&&coord(row.display_position)!==coord(row.target)?row.target:null;
  const label=row.display_position?row.position_label:row.target?'目标坐标':'坐标';
  const route=destination?`<span class="unit-route-arrow" aria-hidden="true">→</span><div><span>目标</span><strong>${esc(placeLabel(destination,row.target_name))}</strong></div>`:'';
  const timer=showTimer?`<div class="unit-timer"><span>${esc(row.countdown_label)}</span><time id="detailCountdown">${remaining(row)}</time></div>`:'';
  return `<section class="unit-location" aria-label="位置与时间">
    <div class="unit-route"><div><span>${esc(label)}</span><strong>${esc(placeLabel(point,row.display_position?row.from_name:row.target_name))}</strong></div>${route}</div>
    ${timer}
    <div class="unit-location-footer"><p id="detailAge" class="unit-age">${detailFreshness(row)}</p>${gameLocateButton(destination||point)}</div>
  </section>`;
}
const marchClock=new Intl.DateTimeFormat('zh-CN',{hour:'2-digit',minute:'2-digit',second:'2-digit',hourCycle:'h23',timeZone:'Asia/Shanghai'});
function compactArmyLocation(row){
  const routeRow=(label,point,name,at)=>`<div class="selected-route-row"><span>${label}</span><strong title="${esc(placeLabel(point,name))}">${esc(placeLabel(point,name))}</strong><time title="${esc(label==='出发'?'出发时间':'预计到达时间')}">${at>0?esc(marchClock.format(at*1000)):'—'}</time></div>`;
  return `<section class="selected-army-route" aria-label="出发和目标地点及时间">${routeRow('出发',row.from,row.from_name,row.begin)}${routeRow('目标',row.target,row.target_name,row.end)}</section>`;
}
function selectedLoadoutMarkup(row,report,message='该队武将资料未核实'){
  if(!report?.heroes?.length)return `<p class="selected-loadout-empty">${esc(message)}</p>`;
  const heroes=[...report.heroes].sort((a,b)=>a.position-b.position);
  const overview=lineupOverview(heroes,report.event_at);
  return `<div class="selected-army-roster lineup-roster" aria-label="大营、中军、前锋武将等级及红度">${overview.rosterMarkup}</div><p class="selected-army-report">${esc(overview.meta)}</p>
    <details class="selected-army-tactics watch-record" data-selected-tactics="${esc(row.id)}"${selectedTacticsOpen?' open':''}><summary>战法</summary><div class="alert-loadout">${heroLoadouts(heroes,true,false,{showTroops:false,showLevels:true})}</div></details>`;
}
function compactArchiveLoadout(row,data,primary){
  if(data.live_lineup)return compactForecastLoadout(row,data.prediction);
  const verified=['exact_army_id','matched_arrival','same_card_instances'].includes(data.match)&&!data.current_resolution?.unresolved;
  const report=verified?primary.find(item=>item.player_name===row.player_name&&String(item.union_id)===String(row.union_id)&&
    (!data.current_report_id||item.battle_id===data.current_report_id)):null;
  return selectedLoadoutMarkup(row,report?{heroes:report.data.heroes,event_at:report.event_at}:null,
    data.match==='current_report_incomplete'?'该队最新战报资料未齐':'该队武将资料未核实');
}
function compactForecastLoadout(row,prediction){
  const resolution=prediction?.current_resolution;
  let report=null;
  if(prediction?.army_id===row.id&&!resolution?.unresolved){
    if(Object.hasOwn(prediction,'display_report'))report=currentLineupReport(prediction);
    else if(resolution?.anchor_battle_id&&resolution.battle_id===prediction.current_report_id)report=currentLineupReport(prediction);
    else if(prediction.same_army_report_id&&(!prediction.current_report_id||prediction.current_report_id===prediction.same_army_report_id))
      report=prediction.items.find(item=>item.battle_id===prediction.same_army_report_id);
  }
  return selectedLoadoutMarkup(row,report,prediction?.current_resolution?.unresolved?'该队最新战报资料未齐':'该队武将资料未核实');
}
const resolvedGroupNames=new Map();
function renderGroupDetails(){
  const group=state?.army_groups?.find(g=>g.id===selectedGroup);
  const rows=(group?.member_ids||[]).map(id=>state.armies.find(r=>r.id===id&&!r.stale&&String(r.army_group_id)===selectedGroup)).filter(Boolean);
  const key=['group',selectedGroup,...rows.map(row=>lineupObservationKey(row)+factionGroup(row)+armyGroupBadge(row))].join('|');
  if(inspectorKey!==key){
    inspectorKey=key;
    $('armyDetails').innerHTML=rows.map(row=>`<article class="selected-army-card ${esc(row.relation)} ${factionClass(row)}"><header class="selected-army-head"><strong>${esc(row.player_name)}${armyGroupBadge(row)}</strong><span>${esc(observedArmyLabel(row))}</span></header>${compactArmyLocation(row)}<section id="groupLineup-${esc(row.id)}"><p>正在读取该队资料…</p></section></article>`).join('')||'<p>等待当前队伍数据</p>';
  }
  for(const row of rows){
    const panel=$('groupLineup-'+row.id),key=placeLoadoutKey(row),cached=placeLoadouts.get(key);
    const apply=data=>{
      if(!panel?.isConnected||selectedGroup!==String(row.army_group_id)||placeLoadoutKey(row)!==key||!state.armies.some(r=>r.id===row.id&&lineupObservationKey(r)===lineupObservationKey(row)))return;
      const primary=placeLoadoutReports(row,data);
      const markup=compactArchiveLoadout(row,data,primary);
      rememberArmyGroup(row,data);
      if(panel.dataset.rendered!==markup){panel.innerHTML=markup;panel.dataset.rendered=markup;}
      if(primary.length)reportLineupDisplay(row,data);
    };
    if(cached?.data)apply(cached.data);
    if(cached?.pending||cached&&Date.now()-cached.at<lineupRefreshMs)continue;
    const entry={at:Date.now(),pending:true,data:cached?.data};placeLoadouts.set(key,entry);
    fetch('/api/army-lineups?'+new URLSearchParams({army_id:row.id}),
      {cache:'no-store',signal:AbortSignal.timeout(6000)})
      .then(response=>{if(!response.ok)throw new Error('资料查询暂未完成');return response.json();})
      .then(data=>{entry.data=cacheLiveLineup(row,data);apply(entry.data);})
      .catch(()=>{entry.at=Date.now()-lineupRefreshMs+3000;})
      .finally(()=>{entry.pending=false;});
  }
}
function renderDetails(){
  if(!state)return;
  if(selectedGroup){renderGroupDetails();return;}
  const r=selectedRow();
  if(!r){
    if(selected){
      selected=null;
      selectedMarch=null;
      dismissAlertPopup();
      setInspectorView('watch');
    }
    inspectorKey='';
    $('armyDetails').textContent='';
    return;
  }
  const showTimer=state.live&&r.end>now(),compact=compactInspector();
  const key=[r.id,r.signature,r.relation,r.kind,coord(r.display_position),coord(r.target),r.from_name,r.target_name,
    r.begin,r.end,showTimer,state.live,state.health.capture_source,r.troops?.map(t=>t.code).join(','),compact,r.force_type,factionGroup(r),armyGroupBadge(r)].join('|');
  if(key===inspectorKey){
    const age=$('detailAge');
    if(age)age.textContent=detailFreshness(r);
    renderDefensePanel(r,$('defenseEvidence'));
    renderLineupPanel(r,$('lineupEvidence'));
    return;
  }
  inspectorKey=key;
  if(compact){
    $('armyDetails').innerHTML=`<article class="selected-army-card ${esc(r.relation)} ${factionClass(r)}"><header class="selected-army-head"><strong>${esc(r.player_name)}${armyGroupBadge(r)}</strong><span class="selected-army-state">${esc(observedArmyLabel(r))}</span>${showTimer?`<time id="detailCountdown" aria-label="${esc(r.countdown_label)}">${remaining(r)}</time>`:''}</header>
      ${compactArmyLocation(r)}<section id="lineupEvidence" class="selected-army-loadout" aria-label="队伍武将与战法"></section></article>`;
    renderLineupPanel(r,$('lineupEvidence'));
    return;
  }
  $('armyDetails').innerHTML=`
    <header class="unit-head ${factionClass(r)}">
      <h2>${esc(r.player_name)}${armyGroupBadge(r)}</h2>
      <p id="unitRosterLabel">${esc(armyRosterLabel(r))}</p>
      <div class="unit-status"><span>${esc(observedArmyLabel(r))}</span></div>
    </header>
    ${unitLocation(r,showTimer)}
    <section id="lineupEvidence" class="lineup-evidence" aria-label="战报阵容"></section>
    <section id="detailTroops" class="detail-troops" aria-label="部队兵种">${troopDisplay(r.troops,'地图兵种')}</section>
    <section id="defenseEvidence" class="lineup-evidence defense-evidence" aria-label="迎击参考" hidden></section>
    <details class="calibration-section">
      <summary>校准与依据</summary>
      <p>${r.position?'坐标来自最近地图观测。':'地图空心点标记出发坐标。'}</p>
      <p>对照游戏核实状态后保存，校准将应用于相同状态标记。</p>
      <select id="stateKind" aria-label="游戏中观察到的部队状态">
        <option value="">选择实际状态</option>
        ${Object.entries(state.state_kinds).map(([k,v])=>`<option value="${k}" ${r.kind===k?'selected':''}>${esc(v)}</option>`).join('')}
      </select>
      <button id="saveState" class="button">保存状态校准</button>
    </details>`;
  renderDefensePanel(r,$('defenseEvidence'));
  renderLineupPanel(r,$('lineupEvidence'));
  $('saveState').onclick=async()=>{
    if(!$('stateKind').value)return toast('请选择游戏中实际显示的状态',true);
    try{await save({action:'state',army_id:r.id,kind:$('stateKind').value});}catch(e){toast(e.message,true);}
  };
}
const lineupModes={
  same_card_instances:['最近配置记录','同一组三张武将卡的最近战报；历史关联继续保留在后台。'],
  current_report_incomplete:['最近资料未齐','新记录缺少字段或兵种有变化，等待核对。'],
  exact_army_id:['已关联的战报阵容','战报与当前队伍有直接关联。阵容以战报时间为准。'],
  matched_arrival:['已关联的战报阵容','战报与这支队伍的行军抵达唯一对应。'],
  name_union_troop_candidate:['历史阵容','来自该玩家的历史战报。'],
  name_union_troop_family_candidate:['历史阵容','来自该玩家的历史战报。'],
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
  const exact=['exact_army_id','matched_arrival','same_card_instances'].includes(data.match);
  const items=data.items.map((item,index)=>{
    const heroes=item.data.heroes.map(h=>esc(h.name)+' '+esc(h.level??'?')+'级').join(' / ');
    const strength=reportStrength(item.data.heroes,'max_hp');
    const time=esc(new Date(item.event_at*1000).toLocaleString('zh-CN',{hour12:false}));
    const report=item.battle_id?`<a href="/?battle_id=${encodeURIComponent(item.battle_id)}">战报 ${esc(item.battle_id)} ↗</a>`:'';
    const expanded=exact&&index===0;
    return `<div class="lineup-entry">${expanded?'':`<strong>${heroes}</strong>`}${strength?`<p>${strength}</p>`:''}<div class="lineup-meta"><time>${time}</time>${report}</div>${heroLoadouts(item.data.heroes,expanded)}</div>`;
  });
  const history=exact&&count>1?`<details class="prediction-more"><summary>其他 ${count-1} 份同队记录</summary>${items.slice(1).join('')}</details>`:'';
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
  return army&&marchKey(army)===marchKey(prediction)&&
    (!Object.hasOwn(prediction,'display_report')||String(army.owner)===String(prediction.owner)&&army.player_name===prediction.player_name&&String(army.union_id)===String(prediction.union_id))?prediction:null;
}
function acceptLivePrediction(row,prediction){
  if(!prediction||prediction.army_id!==row.id||marchKey(row)!==marchKey(prediction))return false;
  if(Object.hasOwn(prediction,'display_report')&&(String(row.owner)!==String(prediction.owner)||row.player_name!==prediction.player_name||String(row.union_id)!==String(prediction.union_id)))return false;
  const old=predictionFor(row.id);
  if(old&&Object.hasOwn(old,'display_report')&&Object.hasOwn(prediction,'display_report')&&
    (old.source_revision>prediction.source_revision||old.source_revision===prediction.source_revision&&old.evaluated_at>prediction.evaluated_at))return false;
  alertPredictions[row.id]=prediction;
  return true;
}
function cacheLiveLineup(row,data){
  if(!data.live_lineup)return data;
  if(!state.armies.some(r=>r.id===row.id&&lineupObservationKey(r)===lineupObservationKey(row)))return {...data,items:[],prediction:null};
  if(acceptLivePrediction(row,data.prediction))return data;
  const current=predictionFor(row.id);
  if(!current)return {...data,items:[],prediction:null};
  const report=currentLineupReport(current);
  return {...data,prediction:current,current_report_id:report?.battle_id,
    match:current.display_verified?'same_card_instances':'live_reference',items:report?[{
      player_name:row.player_name,union_id:row.union_id,battle_id:report.battle_id,event_at:report.event_at,data:{heroes:report.heroes}}]:[]};
}
async function loadAlertPredictions(){
  if(!state.alerts.length){forecastKey='';return;}
  if(forecastLoading)return;
  const ids=new Set(state.alerts.map(alert=>alert.army_id));
  const key=state.armies.filter(row=>ids.has(row.id)).map(row=>marchKey(row)+'|'+(state.health.report_priority?.updates?.[row.id]||'')).sort().join(';');
  if(key===forecastKey&&Date.now()-forecastCheckedAt<12000)return;
  forecastLoading=true;
  forecastKey=key;
  forecastCheckedAt=Date.now();
  const service=state.health.instance_id,observations=new Map(state.armies.map(row=>[row.id,lineupObservationKey(row)]));
  try{
    const response=await fetch('/api/alert-lineups?include_garrison=0',{cache:'no-store',signal:AbortSignal.timeout(5000)});
    if(!response.ok)throw new Error('阵容分析暂不可用');
    const predictions=(await response.json()).predictions;
    if(state.health.instance_id!==service)return;
    for(const [id,prediction] of Object.entries(predictions)){
      const row=state.armies.find(row=>row.id===id);
      if(row&&observations.get(id)===lineupObservationKey(row))acceptLivePrediction(row,prediction);
    }
    for(const id of Object.keys(alertPredictions))if(!state.armies.some(row=>row.id===id))delete alertPredictions[id];
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
const forceLabels={main:'主力',spartan:'斯巴达',unknown:'未知'};
const forceReportClock=new Intl.DateTimeFormat('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false,timeZone:'Asia/Shanghai'});
function alertForce(prediction,mapHeroCount){
  const assessment=prediction?.force_assessment;
  if(assessment)return Object.hasOwn(forceLabels,assessment.kind)?assessment:{...assessment,kind:'unknown'};
  return mapHeroCount?{kind:'spartan',map_hero_count:mapHeroCount}:{kind:'unknown'};
}
const mapForceBasis=count=>count?`地图 ${count} 将`:'';
const reportScene=npc=>npc===0?'玩家对战':npc===1?'打地／NPC':'';
const forceBadge=kind=>`<span class="watch-force-tag ${kind}">${forceLabels[kind]}</span>`;
function heroLoadouts(heroes, expanded=false, showGear=true, {showTroops=true,showLevels=true}={}){
  if(!heroes.length)return '';
  const list=`<ul class="hero-loadouts" aria-label="武将配置">${heroes.map(h=>{
    const red=h.red_stars==null?'待核对':`${esc(h.red_stars)}红`;
    const skillRow=s=>`<li>${esc(s.name)}${s.level==null?'':` <span>${esc(s.level)}级</span>`}</li>`;
    const skillRows=h.skill_slot_ids?h.skill_slot_ids.map((id,i)=>{
      if(id==null||id===0)return `<li>第${i+1}槽 <span>${id===0?'空槽':'未采到'}</span></li>`;
      return skillRow(h.skills?.find(s=>s.id===id)||{name:`战法 #${id}`});
    }):h.skills?.map(skillRow);
    const skills=skillRows?.length?`<ul class="hero-skill-list" aria-label="战法">${skillRows.join('')}</ul>`:'<span>战法未取得</span>';
    const gear=h.gear?`${esc(h.gear.name||`宝物 #${h.gear.id}`)} +${esc(h.gear.level)}${h.gear.forge_id?` · ${esc(h.gear.forge_name||`锻造 #${h.gear.forge_id}`)}`:''}`:
      h.gear_reported?'战报未列宝物':'宝物字段未取得';
    const level=!showLevels||h.level==null?'':` · ${esc(h.level)}级`;
    return `<li><div class="hero-loadout-title"><strong>${esc(h.name)}${level}</strong><span>红度 ${red}${showTroops?` · ${esc(troopText(h.troop_name,h.troop_code)||'兵种未取得')}`:''}</span></div>
      ${skills}${showGear?`<div><span>武器</span> ${gear}</div>`:''}</li>`;
  }).join('')}</ul>`;
  return expanded?list:`<details class="prediction-skills"><summary>武将兵种、红度、战法与武器</summary>${list}</details>`;
}
function directAlertLoadout(prediction){
  if(!prediction?.same_army_report_id)return null;
  return prediction.items.find(item=>item.battle_id===prediction.same_army_report_id)||null;
}
function alertLoadoutMarkup(report, {sameArmy=false,compact=false}={}){
  if(compact)return `<div class="alert-loadout">${heroLoadouts(report.heroes,true,false,{showTroops:false,showLevels:true})}</div>`;
  const label=sameArmy?'同队战报':'历史战报';
  const team='';
  const time=!compact&&report.event_at?`<time>${esc(forceReportClock.format(report.event_at*1000))}</time>`:'';
  const strength=!compact&&Number.isFinite(report.initial_hp)?`<span>历史 ${forceNumber(report.initial_hp)} 兵</span>`:'';
  const link=report.battle_id?`<a href="/?battle_id=${encodeURIComponent(report.battle_id)}">战报 ${esc(report.battle_id)} ↗</a>`:'';
  return `<div class="alert-loadout"><div class="lineup-meta"><strong>${label}</strong>${team?`<span>${team}</span>`:''}${time}${strength}${link}</div>
    ${heroLoadouts(report.heroes,true,sameArmy||!compact)}</div>`;
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
let garrisonTrialState=null, quickGuardAlert=null;
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
  }else garrisonTrialLoading=true;
  const revision=garrisonTrialRevision;
  try{
    const options=action?{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({action,alert,...team}),signal:AbortSignal.timeout(10000)}:{cache:'no-store',signal:AbortSignal.timeout(4500)};
    const response=await fetch('/api/game/garrison-trial',options);
    const result=await response.json();
    if(!response.ok)throw new Error(result.error||'无法读取驻守任务');
    if(revision!==garrisonTrialRevision)return;
    garrisonTrialState=result;
    renderGarrisonTrial(result);
  }catch(error){
    if(revision!==garrisonTrialRevision)return;
    const message=error.name==='TimeoutError'?'驻守请求超时，请等待看板刷新任务状态':error.message;
    $('garrisonTrialStatus').textContent=message;
    if(action)toast(message,true);
    else if(!garrisonTrialState){
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
function garrisonAlertAction(alert,prediction){
  if(state.health.local_install||state.health.capture_source!=='pc'||prediction?.force_assessment?.kind!=='main')return '';
  const result=garrisonTrialState;
  const same=result?.enemy_id===alert.army_id&&result.enemy_arrival===alert.end&&
    result.target?.join(',')===alert.coord?.join(',');
  const active=garrisonActive(result);
  const unavailable=garrisonTrialBusy||active||!state.live||!state.reliable||alert.end-now()<=0;
  if(same&&active)return `<div class="watch-garrison-action"><small>${esc(result.message)}</small></div>`;
  return `<div class="watch-garrison-action"><button type="button" class="text-button"
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
  $('quickGuardTarget').textContent=`目标 ${placeLabel(alert.coord,alert.target_name)} · ${alert.player_name}`;
  $('quickGuardTeam').dataset.markup='';
  $('quickGuardTeam').value='';
  $('quickGuardRecruit').checked=false;
  $('quickGuardDialog').showModal();
  renderQuickGuard();
}
const garrisonClock=new Intl.DateTimeFormat('zh-CN',{hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false,timeZone:'Asia/Shanghai'});
function defenseReadiness(item){
  const label={holding:'已在目标驻守',arriving:'已在赶往目标，预计先抵达',late:'赶往目标，预计晚于敌军抵达',
    changed:'当前兵种与历史战报不同',observed:'已观察到队伍，能否及时到达待核对',unknown:'当前位置和能否及时到达待核对'};
  return label[item.availability]||label.unknown;
}
function defenseLead(data){
  if(data.low_force)return '<p class="defense-lead">最近同队战报为低兵力，主力留给其他来袭。</p>';
  const pick=data.recommendation;
  if(!pick)return '<p class="defense-lead">暂无可点名的迎击队伍。</p>';
  const item=pick.item, enemy=data.hypotheses[pick.hypothesis_index];
  const source=enemy.identity==='direct'?'同队战报':'历史战报';
  const ready=['holding','arriving'].includes(item.availability);
  const evidence=item.probability_evidence;
  const probabilityBasis=evidence?.method==='similar_main'?`按双方历史配置估计 · ${evidence.strength_only?'跨阵容强度粗估':`${evidence.lineup_cases} 场相近己方阵容`}${evidence.skill_cases?` · ${evidence.skill_cases} 场双方战法相近`:''}${evidence.troop_cases?` · 参考 ${evidence.troop_cases} 场兵力比例相近的交锋`:''}`:
    evidence?'双方沿用所列配置时估计':'可对照下方战绩与克制依据';
  const basis=item.rules[0]?.text||defenseRecord(item.history);
  const impact=data.arrival_at?` · 敌军预计 ${esc(new Date(data.arrival_at*1000).toLocaleTimeString('zh-CN',{hour12:false}))} 到` : '';
  return `<div class="defense-lead"><small>${ready?'到位线索':'优先核对'} · ${source}${impact}</small>
    <strong>${esc(item.player_name)} · ${esc(lineupOverview(item.heroes).roster)}</strong>
    <p>${item.heroes.map(h=>esc(h.name)).join(' / ')}</p><strong>${defenseProbability(item)}</strong>
    <p>${defenseReadiness(item)}${item.position?` · ${coord(item.position)}`:''}</p><p>${probabilityBasis}</p><p>${esc(basis)}</p></div>`;
}
function defenseDisplay(data){
  const title='<h3>迎击参考</h3>';
  if(!data.active)return `${title}<p>本次来袭已结束或视野已更新。</p>`;
  if(data.map_hero_count)return `${title}<p>${mapForceBasis(data.map_hero_count)} · 斯巴达。</p>`;
  if(!data.hypotheses.length)return `${title}<p>敌方阵容待确认，暂无可匹配的迎击方案。</p>`;
  const note=`<p>参考本赛季 ${data.historical_cases} 场历史交锋。</p>`;
  const firstUseful=data.recommendation?.hypothesis_index??Math.max(0,data.hypotheses.findIndex(hyp=>hyp.items.length));
  return title+defenseLead(data)+note+data.hypotheses.map((hyp,index)=>`<details class="defense-hypothesis" ${index===firstUseful?'open':''}>
    <summary>${hyp.identity==='direct'?'同队战报':'历史战报'} · ${hyp.heroes.map(h=>esc(h.name)).join(' / ')}</summary>
    ${hyp.items.map(item=>`<div class="lineup-entry"><strong>${esc(item.player_name)} · ${item.heroes.map(h=>esc(h.name)).join(' / ')}</strong>
      <p>${reportStrength(item.heroes,'initial_hp')||'历史兵力待核对'} · ${item.position?`当前观测位置 ${coord(item.position)} · 直线约 ${item.distance} 格`:'历史己方阵容 · 当前位置待核对'}</p>
      <p>${defenseRecord(item.history)}</p>
      <p>${defenseProbability(item)}${item.probability_evidence?.strength_only?' · 跨阵容强度粗估':''} · ${defenseReadiness(item)}</p>
      ${item.rules.map(rule=>`<p>${esc(rule.text)}</p>`).join('')}${item.risks.map(rule=>`<p>反向风险 · ${esc(rule.text)}</p>`).join('')}
      <div class="lineup-meta"><time>${esc(new Date(item.event_at*1000).toLocaleString('zh-CN',{hour12:false}))}</time><a href="/?battle_id=${encodeURIComponent(item.battle_id)}">己方配置战报 ↗</a></div>
      <details class="prediction-skills"><summary>历史交锋战绩</summary>${defenseHistory(item.history)}</details>${heroLoadouts(item.heroes)}</div>`).join('')||'<p>己方档案里暂缺可比较战绩或明确的克制线索。</p>'}</details>`).join('');
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
    panel.innerHTML='<h3>迎击参考</h3><p>正在对照己方阵容和历史交锋…</p>';
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
  const force=forceBadge(alertForce(prediction,mapHeroCount).kind);
  if(!prediction)return `${force}<small class="watch-prediction">${forecastFailed?'阵容分析暂不可用':'正在分析阵容…'}</small>`;
  const first=currentLineupReport(prediction);
  if(!first)return `${force}<small class="watch-prediction">暂无该玩家的匹配战报</small>`;
  const label=first.identity==='direct'?'同队战报':'历史战报';
  const evidence=[reportScene(first.npc),first.initial_hp==null?'':`历史 ${forceNumber(first.initial_hp)} 兵`,
    first.event_at?`战报 ${forceReportClock.format(first.event_at*1000)}`:''].filter(Boolean).join(' · ');
  const names=`<span class="watch-heroes">${first.heroes.map(h=>esc(h.name)).join(' / ')}</span>`;
  return `${force}<span class="watch-lineup"><small class="watch-prediction">${label}</small>
    ${names}${evidence?`<small>${esc(evidence)}</small>`:''}</span>`;
}
function currentLineupReport(prediction){
  if(!prediction||prediction.current_resolution?.unresolved)return null;
  if(Object.hasOwn(prediction,'display_report'))return prediction.display_report;
  if(state?.armies?.find(row=>row.id===prediction.army_id)?.army_group_id&&!prediction.current_resolution?.anchor_battle_id)return null;
  if(prediction.current_report_id)return prediction.items.find(item=>item.battle_id===prediction.current_report_id)||
    (prediction.force?.battle_id===prediction.current_report_id?prediction.force:null)||null;
  const battleId=prediction.current_report_id||
    (prediction.force_assessment?.kind==='main'?prediction.force_assessment.battle_id:null);
  return (battleId&&prediction.items.find(item=>item.battle_id===battleId))||
    (battleId&&prediction.force?.battle_id===battleId?prediction.force:null)||prediction.items[0]||null;
}
function mapLineupReport(prediction,kind){
  if(prediction&&Object.hasOwn(prediction,'display_report'))return currentLineupReport(prediction);
  if(prediction?.current_resolution?.unresolved)return null;
  if(state?.armies?.find(row=>row.id===prediction?.army_id)?.army_group_id&&!prediction.current_resolution?.anchor_battle_id)return null;
  if(!prediction?.current_report_id&&kind!=='main')return prediction?.items[0];
  return currentLineupReport(prediction);
}
function mapLineupText(player,kind,report){
  const heroes=report?.heroes||[];
  if(kind==='main'){
    const roster=heroes.map(h=>`${h.name} ${h.red_stars==null?'红度未知':h.red_stars+'红'}`).join(' / ');
    const remaining=postBattleTroopsText(heroes);
    const time=report?.event_at?`战报 ${forceReportClock.format(report.event_at*1000)}`:'战报时间未知';
    return [`主力 · ${player}`,roster||'阵容未取得',`${time} · ${remaining}`];
  }
  if(!heroes.length)return ['阵容未取得','兵力未知'];
  const names=heroes.map(hero=>hero.name).join(' / ');
  return [names,report.initial_hp==null?'兵力未知':`战前 ${forceNumber(report.initial_hp)} 兵`];
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
  const row=alert&&selectedRow();
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
  if(returnToCard)$('watchPanel').querySelector('[data-watch-army][aria-pressed="true"]')?.focus({preventScroll:true});
}
function renderAlertPopup(incoming){
  if(!alertPopupAnchor)return;
  if(!incoming){dismissAlertPopup(true);return;}
  const {row,alert}=incoming, panel=$('alertPopup'), body=$('alertPopupBody');
  const troops=liveTroopText(row.troops);
  $('alertPopupTitle').textContent=row.player_name;
  $('alertPopupCountdown').textContent=duration(alert.end-now());
  const locate=$('alertPopupLocate');
  locate.hidden=state.health.capture_source!=='pc';
  locate.dataset.gameCoord=alert.coord.join(',');
  const markup=`<p class="alert-popup-route">${esc(placeLabel(row.from,row.from_name))} → ${esc(placeLabel(alert.coord,alert.target_name))}</p>
    <p>${esc(row.union_name||'同盟未知')} · ${esc(troops||'兵种未知')}</p>${predictionPreview(row.id,alert.map_hero_count)}`;
  if(body.dataset.markup!==markup){body.innerHTML=markup;body.dataset.markup=markup;}
  panel.hidden=false;
  const {left,top}=alertPopupPosition(alertPopupAnchor,panel.offsetWidth,panel.offsetHeight,window.innerWidth,window.innerHeight);
  panel.style.left=`${left}px`;
  panel.style.top=`${top}px`;
}
function predictionDisplay(prediction){
  const {items}=prediction;
  const linked=directAlertLoadout(prediction);
  const first=currentLineupReport(prediction);
  const others=items.filter(item=>item!==first);
  const history=others.length?`<details class="prediction-more"><summary>其他历史战报 · ${others.length} 份</summary>${others.map(item=>alertLoadoutMarkup(item,{sameArmy:item===linked})).join('')}</details>`:'';
  return `${forceBadge(alertForce(prediction).kind)}${first?alertLoadoutMarkup(first,{sameArmy:first===linked}):'<p>暂无该玩家的匹配战报。</p>'}${history}`;
}
const displayedLineups=new Set();
function reportLineupDisplay(row,data){
  const version=state?.health?.report_priority?.updates?.[row.id];
  const report=data.items?.find(item=>item.battle_id===data.current_report_id);
  if(!version||!report||version!==data.current_report_id+':'+report.configuration_hash||
    !['exact_army_id','matched_arrival','same_card_instances'].includes(data.match)||document.visibilityState==='hidden')return;
  const key=row.id+'|'+version;
  if(displayedLineups.has(key))return;
  displayedLineups.add(key);
  while(displayedLineups.size>256)displayedLineups.delete(displayedLineups.values().next().value);
  fetch('/api/intel/priority/displayed',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({army_id:row.id,version}),signal:AbortSignal.timeout(3000)})
    .catch(()=>displayedLineups.delete(key));
}
function renderLineupPanel(row,panel){
  if(!panel)return;
  if(isAlert(row.id)||predictionFor(row.id)&&Object.hasOwn(predictionFor(row.id),'display_report')){
    const prediction=predictionFor(row.id);
    const markup=compactInspector()||prediction&&Object.hasOwn(prediction,'display_report')?compactForecastLoadout(row,prediction):prediction?predictionDisplay(prediction):`${forceBadge(alertForce(null,state.alerts.find(a=>a.army_id===row.id)?.map_hero_count).kind)}<p>${forecastFailed?'阵容分析暂不可用':'正在分析来袭阵容…'}</p>`;
    panel.dataset.source='live';
    if(panel.dataset.rendered!==markup){
      panel.innerHTML=markup;
      panel.dataset.rendered=markup;
    }
    if(panel.dataset.observation!==lineupObservationKey(row)||!panel.dataset.checkedAt||Date.now()-Number(panel.dataset.checkedAt)>=lineupRefreshMs)loadLineups(row,panel);
    return;
  }
  if(panel.dataset.source==='forecast'){
    panel.innerHTML='';
    delete panel.dataset.rendered;
    delete panel.dataset.checkedAt;
  }
  panel.dataset.source='archive';
  if(panel.dataset.observation!==lineupObservationKey(row)||!panel.dataset.checkedAt||Date.now()-Number(panel.dataset.checkedAt)>=lineupRefreshMs)loadLineups(row,panel);
}
async function loadLineups(row,panel){
  if(!panel||panel.dataset.loading==='true')return;
  const observation=lineupObservationKey(row);
  const isCurrent=()=>selected===row.id&&panel.isConnected&&panel.dataset.source!=='forecast'
    &&Boolean(selectedRow())&&lineupObservationKey(selectedRow())===observation;
  panel.dataset.loading='true';
  panel.dataset.observation=observation;
  panel.dataset.checkedAt=String(Date.now());
  if(!panel.innerHTML)panel.innerHTML='<p>正在关联战报阵容…</p>';
  try{
    const response=await fetch('/api/army-lineups?'+new URLSearchParams({army_id:row.id}),
      {cache:'no-store',signal:AbortSignal.timeout(6000)});
    if(!response.ok)throw new Error('阵容档案暂未连接');
    let data=await response.json();
    if(!isCurrent())return;
    data=cacheLiveLineup(row,data);
    rememberArmyGroup(row,data);
    if($('unitRosterLabel'))$('unitRosterLabel').textContent=armyRosterLabel(row);
    if(!compactInspector()&&!row.troops?.length&&data.troops?.length){
      $('detailTroops').innerHTML=troopDisplay(data.troops,'最近归档兵种',data.troops_observed_at);
    }
    const codes=troops=>troops?.map(t=>t.code).join(',');
    const differentObservation=row.troops?.length&&data.troops?.length&&codes(row.troops)!==codes(data.troops);
    const verified=['exact_army_id','matched_arrival','same_card_instances'].includes(data.match);
    const primary=!row.troops?.length&&verified?data.items.slice(0,1):placeLoadoutReports(row,data);
    const markup=differentObservation&&data.match!=='exact_army_id'
      ? compactInspector()?selectedLoadoutMarkup(row,null):'<h3>阵容待匹配</h3><p>地图与档案的兵种观测不同，等待下一次核对。</p>'
      : compactInspector()||data.live_lineup?compactArchiveLoadout(row,data,primary):lineupDisplay({...data,items:primary});
    if(panel.dataset.rendered!==markup){
      panel.innerHTML=markup;
      panel.dataset.rendered=markup;
    }
    if(primary.length&&!differentObservation)reportLineupDisplay(row,data);
    if(data.live_lineup){renderWatch();draw();}
  }catch(error){
    if(isCurrent()){
      // A bounded-query timeout is not evidence of a changed/unknown roster.
      // Keep this army's last successful response while retrying; a successful
      // newer incomplete report still replaces it through the normal path.
      if(!panel.dataset.rendered)panel.textContent='阵容档案暂未连接';
      panel.dataset.checkedAt=String(Date.now()-lineupRefreshMs+3000);
    }
  }finally{
    panel.dataset.loading='false';
    if(!isCurrent())delete panel.dataset.checkedAt;
  }
}
function scopedAlerts(){
  return campaignAlerts(enabledAlerts(state.alerts));
}
function focusedAlerts(){
  return alertFocus?enabledAlerts(state.alerts).filter(alert=>isFocusedTarget(alert.coord)):[];
}
function enabledAlerts(alerts){
  return displayPreferences.showSpartans?alerts:alerts.filter(alert=>
    alertForce(predictionFor(alert.army_id),alert.map_hero_count).kind!=='spartan');
}
function mapArmyEnabled(row){
  if(displayPreferences.showSpartans||row.kind!=='attack'||!['enemy','unknown'].includes(row.relation))return true;
  const alert=state.alerts.find(item=>item.army_id===row.id);
  const troops=row.troops;
  const count=troops?.length===3&&troops.every(t=>Number.isInteger(t.code)&&t.code>=0)
    ?troops.filter(t=>t.code>0).length:0;
  const shortCount=count>0&&count<3?count:null;
  return alertForce(predictionFor(row.id),alert?.map_hero_count??shortCount).kind!=='spartan';
}
function groupAlerts(alerts){
  const groups={main:[],unknown:[],spartan:[]};
  for(const alert of alerts){
    const {kind}=alertForce(predictionFor(alert.army_id),alert.map_hero_count);
    (groups[kind]||groups.unknown).push(alert);
  }
  return groups;
}
const openWatchTargets=new Set(), openWatchArmies=new Set();
let expandedWatchFocus='';
const autoExpandedWatchArmies=new Set();
// New arrivals open their target group; individual tactics require a click.
function expandFocusedAlerts(alerts){
  const changed=expandedWatchFocus!==alertFocus;
  if(changed){expandedWatchFocus=alertFocus;autoExpandedWatchArmies.clear();}
  const active=new Set(alerts.map(watchMarchKey));
  for(const key of autoExpandedWatchArmies)if(!active.has(key))autoExpandedWatchArmies.delete(key);
  let added=false;
  for(const alert of alerts){
    const key=watchMarchKey(alert);
    if(!autoExpandedWatchArmies.has(key)){
      autoExpandedWatchArmies.add(key);added=true;
    }
  }
  if(alertFocus&&(changed||added))openWatchTargets.add(`alert:${alertFocus}`);
}
const watchPriority={main:0,unknown:1,spartan:2};
const watchMarchKey=alert=>[alert.army_id,alert.from?.join(','),alert.coord?.join(','),alert.end].join('|');
function watchTargetGroups(alerts){
  const targets=new Map();
  for(const alert of alerts){
    const point=alert.coord?.join(',');
    if(!point)continue;
    const key=`alert:${point}`;
    if(!targets.has(key))targets.set(key,{key,coord:alert.coord,name:alert.target_name,teams:new Map()});
    const target=targets.get(key), previous=target.teams.get(alert.army_id);
    target.name=alert.target_name||target.name;
    if(previous&&previous.alert.end>=alert.end)continue;
    const assessment=alertForce(predictionFor(alert.army_id),alert.map_hero_count);
    target.teams.set(alert.army_id,{alert,assessment,rank:watchPriority[assessment.kind]??watchPriority.unknown});
  }
  for(const target of targets.values()){
    target.teams=[...target.teams.values()].sort((a,b)=>a.rank-b.rank||a.alert.end-b.alert.end);
    target.rank=target.teams[0].rank;
    target.next=target.teams.reduce((end,team)=>Math.min(end,team.alert.end),Infinity);
  }
  return [...targets.values()].sort((a,b)=>Number(isFocusedTarget(b.coord))-Number(isFocusedTarget(a.coord))||
    a.rank-b.rank||a.teams[0].alert.end-b.teams[0].alert.end);
}
function mapRecoveryMessage(){
  const recovery=state.health.map_recovery;
  return ['recovering','waiting_for_map'].includes(recovery?.status)?recovery.message:
    recovery?'正在重新接收当前地图':'等待游戏刷新完整地图';
}
function routeStatus(visible){
  const h=state.health;
  if(!state.live)return h.last_packet_at?'地图数据已过期 · 等待游戏更新':'等待新的游戏数据';
  const warnings=[];
  if(h.resync_required)warnings.push(mapRecoveryMessage());
  if(h.schema_errors)warnings.push('地图数据需要核对');
  else if(state.reliable===false)warnings.push('地图观测已过期');
  const attacks=currentArmies().filter(row=>mapArmyEnabled(row)&&activeObservedAttack(row));
  const expired=attacks.filter(row=>row.age>15&&!subscriptionActive(row)).length;
  if(expired)warnings.push(`进攻观测过期 ${expired} 支`);
  const oldTargets=new Set(attacks.filter(row=>row.target_side!=='unknown'&&row.target_ownership_fresh===false).map(row=>coord(row.target)));
  if(oldTargets.size)warnings.push(`目标归属过期 ${oldTargets.size} 处`);
  const unknownTargets=new Set(attacks.filter(row=>!row.target_side||row.target_side==='unknown').map(row=>coord(row.target)));
  if(unknownTargets.size)warnings.push(`目标归属未确认 ${unknownTargets.size} 处`);
  if(warnings.length)return warnings.join(' · ');
  if(visible)return '';
  if(h.resync_required)return '等待游戏刷新地图视野';
  if(!state.reliable)return h.schema_errors?'地图数据需要核对':'地图观测暂未更新';
  return '当前地图没有可显示的进攻路线';
}
function mapAttackColor(row){
  return row.target_side==='own'?colors.enemy:row.target_side==='other'?'#64748b':'#9aa6b5';
}
function mapAttackDescription(row){
  const side=row.target_side==='own'?'攻击己方地块':row.target_side==='other'?'攻击非己方地块':'目标归属未确认';
  const warnings=[];
  if(row.age>15&&!subscriptionActive(row))warnings.push('进攻观测已过期');
  if(row.target_side&&row.target_side!=='unknown'&&row.target_ownership_fresh===false)warnings.push('目标归属已过期');
  return [row.player_name,factionLabel(row),side,...warnings].filter(Boolean).join(' · ');
}
function visibleRouteMarker(start,target,width,height){
  const inset=12;
  let low=0, high=1;
  for(const [axis,size] of [['x',width],['y',height]]){
    const delta=target[axis]-start[axis];
    if(size<=inset*2)return null;
    if(delta===0){
      if(start[axis]<inset||start[axis]>size-inset)return null;
      continue;
    }
    const a=(inset-start[axis])/delta, b=(size-inset-start[axis])/delta;
    low=Math.max(low,Math.min(a,b));
    high=Math.min(high,Math.max(a,b));
    if(high<=low)return null;
  }
  const mid=(low+high)/2;
  return {x:start.x+(target.x-start.x)*mid,y:start.y+(target.y-start.y)*mid};
}
function renderAlertTargets(){
  const point=focusedPoint();
  $('watchTargetControl').hidden=!point;
  const label=$('alertTarget');
  const text=point?placeLabel(point,mapPlaceName(point)):'未选择地块';
  if(label.textContent!==text){
    label.textContent=text;
    label.title=text;
  }
}
const fortLabel=fort=>({building:'在建',ready:'已完成',transition:'状态同步中'}[fort.status]||'状态同步中')+
  ' · '+factionLabel(fort)+'要塞';
const landLabel=land=>factionLabel(land)+'占领地';
function renderWatch(){
  if(!state)return;
  const t=watchedTarget();
  const targetKey=JSON.stringify(state.targets);
  if($('watchSelect').dataset.key!==targetKey){
    $('watchSelect').innerHTML=state.targets.length?state.targets.map((p,i)=>`<option value="${i}">${esc(p.name)} · ${p.x},${p.y}</option>`).join(''):'<option value="0">全部已见战场</option>';
    $('watchSelect').dataset.key=targetKey;
  }
  $('watchSelect').value=String(watchIndex);
  $('focusTarget').innerHTML=alertFocus?`关注 ${esc(placeLabel(focusedPoint(),mapPlaceName(focusedPoint())))}<span>↗</span>`:
    t.virtual?'当前视野<span>↗</span>':`(${t.x}, ${t.y})<span>↗</span>`;

  const enemy=Object.values(state.config.relations).some(r=>r.relation==='enemy');
  const attack=Object.values(state.config.state_rules).some(r=>r.verified&&r.kind==='attack');
  const alerts=focusedAlerts();
  expandFocusedAlerts(alerts);
  const incoming=selectedIncoming(alerts);
  renderAlertPopup(incoming);
  const {main,unknown,spartan}=groupAlerts(alerts);
  const enabledCount=alerts.length;
  const hiddenCount=state.alerts.filter(alert=>isFocusedTarget(alert.coord)).length-enabledCount;
  $('watchStatus').textContent=!alertFocus?'点击看板或游戏中的地块，查看该处敌袭'
    :alerts.length?`主力 ${main.length} · 斯巴达 ${spartan.length} · 未知 ${unknown.length}`
    :!state.live?'数据未更新，来袭判断暂停'
    :state.health.resync_required?mapRecoveryMessage()
    :!state.reliable?`${routeStatus(0)}，来袭判断暂缓`
    :!enemy&&!state.config.self.is_wanderer?'在「设置」确认敌盟后启用来袭预警'
    :!attack?'等待进攻状态校准':hiddenCount?'该地块斯巴达已隐藏':'该地块暂无已确认敌袭';
  $('watchStatus').title=$('watchStatus').textContent;
  renderAlertTargets();
  renderPlaceArmies();
  renderGuardCoverage();
  const badge=$('watchBadge');
  badge.textContent=String(enabledCount);
  badge.hidden=!enabledCount;
  const compact=$('compactWatchStatus');
  compact.textContent=enabledCount?`来袭预警 · ${enabledCount} 支`:'来袭预警';
  compact.dataset.alert=String(Boolean(enabledCount));

  $('cancelWebGarrison').hidden=!garrisonTrialState?.requested_alert||!garrisonCancellable(garrisonTrialState);
  renderQuickGuard();
  renderWatchGroups($('watchEntries'),alerts,incoming?.row.id);
  $('calibrationCount').textContent=`已校准 ${new Set(Object.values(state.config.state_rules).filter(r=>r.verified).map(r=>r.kind)).size} 种状态`;
}
function watchArmyDetails(r,selectedId){
  const prediction=predictionFor(r.army_id);
  const linked=directAlertLoadout(prediction);
  const report=currentLineupReport(prediction);
  const loadout=report?.heroes?.length?alertLoadoutMarkup(report,{sameArmy:report===linked,compact:true}):'';
  const trial=garrisonAlertAction(r,prediction);
  const status=prediction?'暂无该玩家的匹配战报':forecastFailed?'阵容分析暂不可用':'正在分析阵容…';
  const locate=gameLocateButton(r.coord);
  return `<div class="watch-record"><div class="watch-route">
    ${locate?`<div class="watch-route-links">${locate}</div>`:''}
    ${!loadout?`<small class="watch-prediction">${status}</small>`:''}</div>${trial}${loadout}</div>`;
}
function watchHeroSummary(alert){
  const heroes=currentLineupReport(predictionFor(alert.army_id))?.heroes;
  return lineupOverview(heroes).roster;
}
function renderWatchGroups(list,alerts,selectedId){
  const markup=watchTargetGroups(alerts).map(({key,coord:point,name,teams,rank,next})=>{
    const expanded=openWatchTargets.has(key), focused=isFocusedTarget(point);
    const counts=Object.entries(forceLabels).map(([kind,label])=>{
      const n=teams.filter(team=>team.assessment.kind===kind).length;
      return n?`<span class="watch-force-tag ${kind}">${label} ${n}</span>`:'';
    }).join(' ');
    const items=expanded?teams.map(({alert,assessment})=>{
      const row=state.armies.find(r=>r.id===alert.army_id);
      const march=watchMarchKey(alert), open=openWatchArmies.has(march), {kind}=assessment;
      const chosen=alert.army_id===selectedId;
      const report=currentLineupReport(predictionFor(alert.army_id));
      const overview=lineupOverview(report?.heroes,report?.event_at);
      return `<div class="watch-army ${kind} ${factionClass(row||alert)}${chosen?' is-selected':''}"><button type="button" class="watch-toggle watch-army-toggle" data-watch-army="${esc(march)}" aria-expanded="${open}" aria-pressed="${chosen}">
        <span class="watch-army-summary"><span class="watch-army-heading"><strong>${esc(alert.player_name)}</strong>${armyGroupBadge(row||{})} ${forceBadge(kind)}</span><span class="watch-hero-summary lineup-roster" aria-label="大营、中军、前锋武将及红度">${overview.rosterMarkup}</span><small class="watch-report-meta">${esc(overview.meta)}</small></span>
        <time data-watch-end="${esc(alert.end)}"></time></button>${open?watchArmyDetails(alert,selectedId):''}</div>`;
    }).join(''):'';
    return `<div class="watch-target-group${rank===watchPriority.main?' has-main':''}${focused?' is-focused':''}"><button type="button" class="watch-toggle" data-watch-target="${esc(key)}" aria-expanded="${expanded}">
      <span><strong>${esc(placeLabel(point,name))}</strong>${focused?'<span class="watch-focus-label">关注</span>':''}${counts?`<small>${counts}</small>`:''}</span>
      <span class="watch-group-eta"><strong>${teams.length} 支</strong><time aria-label="最近抵达倒计时" data-watch-end="${esc(next)}"></time></span></button>${items}</div>`;
  }).join('');
  if(list.dataset.markup!==markup){
    const active=document.activeElement;
    const selector=list.contains?.(active)&&['watchTarget','watchArmy'].find(key=>active.dataset[key]);
    list.innerHTML=markup;
    list.dataset.markup=markup;
    if(selector){
      const attr=selector.replace(/[A-Z]/g,c=>'-'+c.toLowerCase());
      list.querySelector(`[data-${attr}="${CSS.escape(active.dataset[selector])}"]`)?.focus({preventScroll:true});
    }
  }
  list.querySelectorAll('[data-watch-end]').forEach(el=>{
    const seconds=Number(el.dataset.watchEnd)-now();
    const value=seconds>0?duration(seconds):'';
    if(el.textContent!==value)el.textContent=value;
  });
}
function renderStatus(){
  if(!state)return;
  const h=state.health;
  const recent=h.last_packet_at?Math.max(0,now()-h.last_packet_at):null;
  $('liveDot').className=`dot ${state.live?'live':h.status==='error'?'error':''}`;
  $('connectionText').textContent=!state.live?(h.status==='waiting'?'等待游戏连接':'数据更新中断'):
    h.identity_pending?'等待读取角色':h.scope_pending?'正在核对区服和赛季':
    h.resync_required&&h.map_recovery?'正在恢复当前地图':
    `${captureNames[h.capture_source]||'游戏'} · ${recent>10?'地图已连接':'实时接收'}`;
  $('connectionAge').textContent=recent===null?'等待游戏数据':`最后收到 ${ageText(recent)}`;
  $('ownName').textContent=state.config.self.name;
  $('ownUnion').textContent=(state.config.self.is_wanderer?'流浪军 · ':'')+(state.config.self.union_name||'无所属势力');
  const sync=h.union_relations;
  $('unionRelationStatus').textContent=sync?.status==='ready'?`已同步游戏内关系 · 友好 ${sync.friendly} · 敌对 ${sync.enemy}`:'等待同步游戏内关系';
  $('messageCount').textContent=`${h.messages.toLocaleString()} 条消息`;
}
function rememberMapViewport(){
  if(!state||!map.initialized)return;
  try{globalThis.sessionStorage?.setItem('stzb.map.reload.v1',JSON.stringify({
    user:state.config.self.user_id,config:state.map_config_id,area:state.area,at:now(),
    x:map.x,y:map.y,scale:map.scale,followArea:map.followArea!==false,followVersion:2}));}catch{}
}
function restoreMapViewport(){
  if(map.initialized||!state?.config?.self||state.health.identity_pending||state.health.scope_pending)return;
  try{
    const storage=globalThis.sessionStorage,raw=storage?.getItem('stzb.map.reload.v1');
    if(!raw)return;
    storage.removeItem('stzb.map.reload.v1');
    const saved=JSON.parse(raw),area=state.area,old=saved.area;
    if(saved.user!==state.config.self.user_id||saved.config!==state.map_config_id||
      now()-saved.at<0||now()-saved.at>120||![saved.x,saved.y,saved.scale].every(Number.isFinite)||
      saved.scale<3||saved.scale>70||!area||!old)return;
    const sameArea=old.length===4&&old.every((value,index)=>value===area[index]);
    const sameView=sameArea&&saved.followVersion===2&&!(state.game_focus?.updated_at>saved.at);
    Object.assign(map,{x:sameView?saved.x:(area[0]+area[2])/2,y:sameView?saved.y:(area[1]+area[3])/2,
      scale:saved.scale,followArea:sameView?saved.followArea:true,initialized:true,drag:null,gameArea:[...area]});
    if(sameView)gameFocusEventId=state.game_focus?.event_id||'';
  }catch{}
}
function syncMapViewport(){
  const area=state?.area;
  const justCentered=map.gameClickCentered===true;map.gameClickCentered=false;
  if(map.drag||activeCampaign()||!state?.live||state.reliable===false||
    state.health.resync_required||!Array.isArray(area)||area.length!==4||map.width<=0||map.height<=0)return;
  const changed=Array.isArray(map.gameArea)&&area.some((value,index)=>value!==map.gameArea[index]);
  map.gameArea=[...area];
  if(!map.initialized)return;
  const click=state.game_focus;
  // Explicit game clicks center the selected tile; a subsequent camera pan
  // follows the new game area, including small moves that overlap the old view.
  if((!changed||justCentered)&&click?.updated_at>now()-3&&coord(click.coord)===coord([map.x,map.y]))return;
  if(changed){
    map.x=(area[0]+area[2])/2;map.y=(area[1]+area[3])/2;
    map.followArea=true;return;
  }
  if(map.followArea===false)return;
  const corners=[[area[0],area[1]],[area[0],area[3]],[area[2],area[1]],[area[2],area[3]]].map(project);
  if(Math.max(...corners.map(p=>p.x))>=0&&Math.min(...corners.map(p=>p.x))<=map.width&&
    Math.max(...corners.map(p=>p.y))>=0&&Math.min(...corners.map(p=>p.y))<=map.height)return;
  map.x=(area[0]+area[2])/2;map.y=(area[1]+area[3])/2;
}
async function refresh(){
  if(refreshing)return;
  refreshing=true;
  let next;
  try{
    const response=await fetch('/api/state',{cache:'no-store',signal:AbortSignal.timeout(5000)});
    if(!response.ok)throw new Error('本机服务未响应');
    next=await response.json();
  }catch(error){
    apiConnected=false;
    updateFreshness();renderRoster();renderDetails();renderWatch();draw();
    $('liveDot').className='dot error';
    $('connectionText').textContent='服务未连接';
    return;
  }finally{
    refreshing=false;
  }
  const restarted=state?.health?.instance_id&&next.health?.instance_id&&state.health.instance_id!==next.health.instance_id;
  const uiChanged=state?.health?.ui_revision&&next.health?.ui_revision&&state.health.ui_revision!==next.health.ui_revision;
  if((pendingUiReload||restarted||uiChanged)&&typeof globalThis.location?.reload==='function'){
    pendingUiReload=true;
    if(!map.drag&&!$('settingsDialog').open&&!$('quickGuardDialog').open&&
      !document.activeElement?.isContentEditable&&!document.activeElement?.closest?.('input,textarea,select')){
      rememberMapViewport();globalThis.location.reload();return;
    }
  }
  state=next;
  syncClientRelationColors();
  apiConnected=true;
  clockOffset=state.server_now-Date.now()/1000;
  restoreMapViewport();
  syncGameFocus();
  syncGameArmyFocus();
  syncMapViewport();
  updateFreshness();renderStatus();renderRoster();renderDetails();renderWatch();
  loadAlertPredictions();loadMapCampaigns();loadMapTerrain();
  if(!map.initialized&&(state.counts.fresh||alertFocus))fitMap();
  draw();
}
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
    if(alertsBefore!==state.alerts.length||state.armies.some(row=>activeObservedAttack(row)||row.relation==='self'&&[1,2,3,4].includes(row.state_code)))draw();
  }
  const row=selectedRow();
  if(row&&$('detailCountdown'))$('detailCountdown').textContent=remaining(row);
  if(row&&$('detailAge'))$('detailAge').textContent=detailFreshness(row);
}
function openSettings(){
  if(!state)return;
  renderDisplayPreferences();
  const c=state.config;
  $('settingsIdentity').textContent=`${c.self.name} / ${c.self.union_name}`;
  $('relationSearch').value='';
  renderRelationOptions();
  const enemy=Object.values(c.relations).some(r=>r.relation==='enemy');
  const attack=Object.values(c.state_rules).some(r=>r.verified&&r.kind==='attack');
  $('alertChecklist').innerHTML=`<li class="${c.self.confirmed?'done':''}">本方身份已核对</li><li class="done">全域记录，不按坐标过滤</li><li class="${enemy?'done':''}">至少一个敌方同盟已确认</li><li class="${attack?'done':''}">进攻状态已对照游戏校准</li><li>只对有效观测启用提醒</li>`;
  $('settingsDialog').showModal();updateGarrisonTrial();
}
function renderRelationOptions(){
  if(!state)return;
  const own=state.config.self,personal=$('relationScope').value==='player',previous=$('unionSelect').value;
  const players=(state.players||[]).filter(row=>String(row.id)!==own.user_id&&
    (String(row.union_id)==='0'||String(row.union_id)!==own.union_id));
  const known={...(state.unions||{})};
  for(const id of Object.keys(state.config.relations))if(/^[1-9]\d*$/.test(id)&&!known[id])known[id]='同盟 '+id;
  for(const row of players)if(row.union_id&&row.union_id!=='0'&&!known[row.union_id])known[row.union_id]=row.union_name||'同盟 '+row.union_id;
  const allChoices=personal?players.map(row=>[String(row.id),`${row.name} · ${row.is_wanderer?'流浪军 · ':''}${row.union_name||'无同盟／聚义'} · ${row.id}`]):
    Object.entries(known).filter(([id])=>/^[1-9]\d*$/.test(id)&&id!==own.union_id);
  const term=($('relationSearch').value||'').trim().toLocaleLowerCase();
  const choices=allChoices.filter(([id,label])=>!term||(id+' '+label).toLocaleLowerCase().includes(term));
  choices.sort((a,b)=>a[1].localeCompare(b[1],'zh-CN'));
  $('relationTargetLabel').textContent=personal?'玩家':'同盟／聚义';
  $('unionSelect').innerHTML='<option value="">请选择</option>'+choices.map(([id,name])=>`<option value="${esc(id)}">${esc(name)}</option>`).join('');
  $('unionSelect').value=choices.some(([id])=>id===previous)?previous:'';
  renderRelationSelection();
}
function renderRelationSelection(){
  const id=$('unionSelect').value,personal=$('relationScope').value==='player';
  const entry=state.config.relations[personal?'player:'+id:id];
  $('unionRelation').value=entry?.manual_override&&(!entry.own_user_id||entry.own_user_id===state.config.self.user_id)?entry.relation:'auto';
  $('relationSelectionHint').textContent=!id?'手动设置优先；选择自动可恢复客户端关系。':personal?
    '仅修改该玩家；恢复自动后跟随所属盟／聚义的关系。':'该设置适用于这个盟／聚义；单个玩家的手动设置优先。';
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
    renderStatus();renderWatch();
    if(firstLoad&&activeCampaign()&&!alertFocus)fitMap();
  }catch(error){if(force)throw error;}
}
function mapPlaceIncoming(point){
  if(!state?.live||state.reliable===false||state.health.resync_required)return [];
  const key=point.join(','), found=new Map();
  for(const alert of scopedAlerts()){
    if(alert.coord?.join(',')===key&&alert.end>now())found.set(alert.army_id,alert);
  }
  for(const row of currentArmies()){
    if(row.relation!=='enemy'||!activeObservedAttack(row)||!mapArmyEnabled(row)||row.target?.join(',')!==key)continue;
    if(!found.has(row.id)){
      const count=row.troops?.filter(t=>t.code>0).length;
      found.set(row.id,{army_id:row.id,player_name:row.player_name,coord:point,from:row.from,end:row.end,
        map_hero_count:count>0&&count<3?count:null});
    }
  }
  return [...found.values()].sort((a,b)=>(watchPriority[alertForce(predictionFor(a.army_id),a.map_hero_count).kind]??1)-
    (watchPriority[alertForce(predictionFor(b.army_id),b.map_hero_count).kind]??1)||a.end-b.end);
}
function closeMapPlacePopup(suppress=false){
  if(suppress&&mapPlacePopup)mapPlaceHoverBlocked=mapPlacePopup.point.join(',');
  mapPlacePopup=null;
  $('mapPlacePopup').hidden=true;
}
function openMapPlacePopup(point,anchor,pinned=true){
  if(mapPlacePopup?.pinned&&!pinned)return;
  mapPlacePopup={point:[...point],anchor:{...anchor},pinned};
  if(pinned)mapPlaceHoverBlocked='';
  $('mapSkillTooltip').hidden=true;
  renderMapPlacePopup();
}
function toggleMapPlaceFocus(point,anchor){
  const key=alertTargetKey(point.join(','));
  if(!key)return false;
  if(alertFocus===key){
    closeMapPlacePopup();
    mapPlaceHoverBlocked=key;
    setMapScope('',campaignFocus);
    return false;
  }
  setMapScope(key,'');
  openMapPlacePopup(point,anchor,true);
  return true;
}
function requestMapPlaceLoadout(row){
  const key=placeLoadoutKey(row),cached=placeLoadouts.get(key),stamp=Date.now();
  if(cached?.pending||cached&&stamp-cached.at<lineupRefreshMs||mapPlacePendingReads>=2)return;
  const entry={at:stamp,pending:true,data:cached?.data};
  placeLoadouts.set(key,entry);
  mapPlacePendingReads++;
  fetch('/api/army-lineups?'+new URLSearchParams({army_id:row.id}),
    {cache:'no-store',signal:AbortSignal.timeout(6000)})
    .then(response=>{if(!response.ok)throw new Error('档案未连接');return response.json();})
    .then(data=>{entry.data=cacheLiveLineup(row,data);})
    .catch(()=>{entry.failed=true;entry.at=Date.now()-15000;})
    .finally(()=>{
      mapPlacePendingReads=Math.max(0,mapPlacePendingReads-1);
      entry.pending=false;
      while(placeLoadouts.size>120)placeLoadouts.delete(placeLoadouts.keys().next().value);
      if(mapPlacePopup)renderMapPlacePopup();
      renderGuardCoverage();
    });
}
function mapPlaceRowLoadout(row,prediction){
  if(prediction){
    const report=currentLineupReport(prediction);
    return report?.heroes?.length?heroLoadouts(report.heroes,true,false,{showTroops:false,showLevels:false}):
      '<p class="map-place-empty">武将信息暂未取得</p>';
  }
  if(!row)return '<p class="map-place-empty">武将信息暂未取得</p>';
  const cached=placeLoadouts.get(placeLoadoutKey(row));
  requestMapPlaceLoadout(row);
  return cached?.data?placeLoadoutMarkup(row,cached.data):
    `<p class="map-place-empty">${cached?.failed?'武将资料暂未连接':'正在读取武将资料…'}</p>`;
}
function mapPlaceTravelling(point){
  if(!state?.live||state.reliable===false||state.health.resync_required||!inRegion(point,state.area))return [];
  const incoming=new Set(mapPlaceIncoming(point).map(row=>row.army_id));
  return currentArmies().filter(row=>row.visible!==false&&row.current_connection!==false&&
    [1,2,3,4].includes(row.state_code)&&row.end>now()&&mapArmyEnabled(row)&&row.from&&row.target&&coord(row.from)!==coord(row.target)&&
    !incoming.has(row.id)&&(coord(row.from)===coord(point)||coord(row.target)===coord(point)))
    .sort((a,b)=>a.end-b.end||a.id.localeCompare(b.id,undefined,{numeric:true}));
}
function renderMapPlacePopup(){
  const popup=mapPlacePopup,panel=$('mapPlacePopup');
  if(!popup){panel.hidden=true;return;}
  const {point,anchor,pinned}=popup,key=point.join(',');
  const rows=placeArmyGroups().get(key)?.rows||[],incoming=mapPlaceIncoming(point);
  const covering=guardEncounter(point).confirmed.filter(entry=>!entry.direct);
  panel.hidden=false;
  panel.classList.toggle('is-preview',!pinned);
  $('closeMapPlacePopup').hidden=!pinned;
  $('mapPlacePopupTitle').textContent=placeLabel(point,mapPlaceName(point));
  const stationed=rows.map(row=>`<article class="map-place-unit ${factionClass(row)}"><header><strong>${armyPlayerButton(row)}</strong>
    <span class="place-side ${esc(row.relation)}">${esc(placeArmyStateText(row))}</span></header>
    ${mapPlaceRowLoadout(row,null)}</article>`).join('');
  const attacks=incoming.map(alert=>{
    const row=state.armies.find(row=>row.id===alert.army_id),prediction=predictionFor(alert.army_id);
    const kind=alertForce(prediction,alert.map_hero_count).kind;
    return `<article class="map-place-unit ${factionClass(row||alert)}"><header><strong>${row?armyPlayerButton(row):esc(alert.player_name)}</strong>${forceBadge(kind)}
      <time data-place-popup-end="${esc(alert.end)}"></time></header>
      ${mapPlaceRowLoadout(row,prediction)}</article>`;
  }).join('');
  const coverage=covering.map(({row,center})=>`<article class="map-place-unit ${factionClass(row)}"><header><strong>${armyPlayerButton(row)}</strong>
    <span>驻守 ${esc(coord(center))}</span></header>${mapPlaceRowLoadout(row,null)}</article>`).join('');
  const travelling=mapPlaceTravelling(point);
  const moving=travelling.map(row=>`<article class="map-place-unit ${factionClass(row)}"><header><strong>${armyPlayerButton(row)}</strong>
    <span>${esc(observedArmyLabel(row))}</span><time data-place-popup-end="${esc(row.end)}"></time></header>
    <p class="map-place-route">${esc(coord(row.from))} → ${esc(coord(row.target))}</p></article>`).join('');
  const markup=`<section class="map-place-group"><h3>此处队伍 <span>${rows.length}</span></h3>
    ${stationed||'<p class="map-place-empty">暂无已观测队伍</p>'}</section>
    ${covering.length?`<section class="map-place-group"><h3>周边辐射驻守 <span>${covering.length}</span></h3>${coverage}</section>`:''}
    <section class="map-place-group"><h3>敌袭队伍 <span>${incoming.length}</span></h3>
    ${attacks||'<p class="map-place-empty">暂无已观测敌袭</p>'}</section>
    ${travelling.length?`<section class="map-place-group"><h3>关联行军 <span>${travelling.length}</span></h3>${moving}</section>`:''}`;
  const body=$('mapPlacePopupBody');
  if(body.dataset.point!==key){body.scrollTop=0;body.dataset.point=key;}
  if(body.dataset.markup!==markup){body.innerHTML=markup;body.dataset.markup=markup;}
  body.querySelectorAll('[data-place-popup-end]').forEach(el=>{
    const value=duration(Number(el.dataset.placePopupEnd)-now());
    if(el.textContent!==value)el.textContent=value;
  });
  const position=alertPopupPosition(anchor,panel.offsetWidth,panel.offsetHeight,window.innerWidth,window.innerHeight);
  panel.style.left=`${position.left}px`;
  panel.style.top=`${position.top}px`;
}

function setMapScope(point,campaign){
  closeMapPlacePopup();
  selected=null;selectedGroup=null;selectedMarch=null;dismissAlertPopup();
  alertFocus=alertTargetKey(point);
  campaignFocus=campaign;
  if(alertFocus)openWatchTargets.add(`alert:${alertFocus}`);
  const url=new URL(location.href);
  for(const [key,value] of [['target',alertFocus],['campaign',campaignFocus]]){
    if(value)url.searchParams.set(key,value);else url.searchParams.delete(key);
  }
  history.replaceState(null,'',url);
  setInspectorView('watch');
  renderStatus();renderWatch();
  draw();
}
function focusWatchTarget(point){
  setMapScope(point,campaignFocus);
  if(alertFocus)focusTarget();
}
function handleMapEscape(event){
  const target=event.target===document?document.activeElement:event.target;
  if(event.key!=='Escape'||event.defaultPrevented||event.isComposing||
    event.ctrlKey||event.altKey||event.metaKey||event.shiftKey||
    target?.isContentEditable||target?.closest?.('input,textarea,select')||
    $('settingsDialog').open||$('quickGuardDialog').open)return;
  if(!alertFocus&&!mapPlacePopup&&!alertPopupAnchor)return;
  if(mapPlacePopup)closeMapPlacePopup(true);
  if(alertPopupAnchor)dismissAlertPopup(true);
  if(alertFocus){
    mapPlaceHoverBlocked=alertFocus;
    setMapScope('',campaignFocus);
  }
  map.gameClickCentered=false;
  event.preventDefault();
}
function syncGameFocus(){
  const focus=state?.game_focus;
  if(!focus?.event_id||focus.event_id===gameFocusEventId)return;
  gameFocusEventId=focus.event_id;
  const armyFocus=state.game_army_focus,clickedAt=focus.clicked_at??focus.message_at??focus.updated_at;
  if((armyFocus?.army_id||armyFocus?.group_id)&&armyFocus.source==='client_selected_army'&&
     Math.abs(clickedAt-armyFocus.selected_at)<=.3)return;
  const point=alertTargetKey(focus.coord?.join(','));
  if(point&&focus.source==='dashen_click'&&focus.updated_at>0&&now()>=focus.updated_at){
    if(now()-focus.updated_at<=3){focusWatchTarget(point);map.followArea=true;map.gameClickCentered=true;}
    else setMapScope(point,campaignFocus);
  }
}
let gameArmyFocusEventId=null,gameSelectedArmy=null;
function syncGameArmyFocus(){
  const focus=state?.game_army_focus;
  if(!focus?.event_id||focus.event_id===gameArmyFocusEventId)return;
  if(focus.source!=='client_selected_army'||!focus.selected_at||now()<focus.selected_at){
    gameArmyFocusEventId=focus.event_id;return;
  }
  if(now()-(focus.validated_at??focus.updated_at??focus.selected_at)>5)return;
  const tileAt=state.game_focus?.message_at??state.game_focus?.updated_at??0;
  if(tileAt>focus.selected_at+.25){gameArmyFocusEventId=focus.event_id;return;}
  if(focus.group_id){
    if(!state.army_groups?.some(g=>g.id===focus.group_id))return;
    gameArmyFocusEventId=focus.event_id;
    selected=null;selectedGroup=focus.group_id;selectedTacticsOpen=false;inspectorKey='';
    setInspectorView('army');renderDetails();return;
  }
  if(focus.army_id==null){
    gameArmyFocusEventId=focus.event_id;
    if(selectedGroup||selected&&selected===gameSelectedArmy)selectArmy(null);
    gameSelectedArmy=null;return;
  }
  const row=state.armies.find(row=>row.id===focus.army_id&&!row.stale&&row.visible!==false&&row.current_connection!==false);
  if(!row)return; // A map update may arrive just after the local selection event.
  gameArmyFocusEventId=focus.event_id;
  gameSelectedArmy=row.id;
  selectArmy(row.id);
}
$('armyTab').onclick=()=>setInspectorView('army');
$('watchTab').onclick=()=>setInspectorView('watch');
$('compactWatchStatus').onclick=()=>setInspectorView('watch');
$('closeInspector').onclick=()=>selectArmy(null);
$('rosterSelection').onclick=()=>setInspectorView('army');
$('settingsButton').onclick=openSettings;$('editTarget').onclick=openSettings;$('closeSettings').onclick=()=>$('settingsDialog').close();$('settingsDialog').addEventListener('click',e=>{if(e.target===$('settingsDialog'))$('settingsDialog').close();});
$('showSpartanAlerts').onchange=e=>setDisplayPreference('showSpartans',e.target.checked);
$('showMapLabels').onchange=e=>setDisplayPreference('mapLabels',e.target.checked);
$('showMapGrid').onchange=e=>setDisplayPreference('mapGrid',e.target.checked);
$('showGuardCoverage').onchange=e=>setDisplayPreference('guardCoverage',e.target.checked);
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
$('relationScope').onchange=()=>{$('unionSelect').value='';$('relationSearch').value='';renderRelationOptions();};
$('relationSearch').oninput=renderRelationOptions;
$('unionSelect').onchange=renderRelationSelection;
$('watchSelect').onchange=e=>{watchIndex=Number(e.target.value);renderWatch();focusTarget();};
$('clearMapScope').onclick=()=>setMapScope('',alertFocus?campaignFocus:'');
$('unionForm').onsubmit=async e=>{e.preventDefault();try{
  const scope=$('relationScope').value==='player'?'player':'union';
  await save({action:'relation',scope,[scope==='player'?'player_id':'union_id']:$('unionSelect').value,relation:$('unionRelation').value});
  toast('关系已更新');
}catch(error){toast(error.message,true);}};
$('roster').onclick=e=>{
  const row=e.target.closest('[data-id]');
  if(!row)return;
  const hadFocus=document.activeElement===row;
  selectArmy(row.dataset.id);
  if(hadFocus)$('rosterSelection').focus({preventScroll:true});
};
$('watchPanel').onclick=e=>{
  const placeDetails=e.target.closest('[data-place-details]');
  if(placeDetails){e.preventDefault();selectArmy(placeDetails.dataset.placeDetails);return;}
  const toggle=e.target.closest('[data-watch-target],[data-watch-army]');
  if(toggle){
    const {watchTarget,watchArmy}=toggle.dataset;
    const expanded=watchTarget?openWatchTargets:openWatchArmies, key=watchTarget||watchArmy;
    const wasOpen=expanded.has(key);
    if(watchTarget){
      focusWatchTarget(watchTarget.slice('alert:'.length));
      if(wasOpen){expanded.delete(key);renderWatch();}
    }else{
      if(wasOpen)expanded.delete(key);else expanded.add(key);
      const alert=state?.alerts.find(item=>watchMarchKey(item)===watchArmy);
      const row=alert&&state.armies.find(item=>item.id===alert.army_id&&!item.stale);
      if(row&&alert.end>now()){
        setMapScope(alert.coord.join(','),campaignFocus);
        selectArmy(row.id,{row});
      }else renderWatch();
    }
    return;
  }
  const dispatch=e.target.closest('[data-garrison-alert]');
  if(dispatch){
    const {garrisonAlert:id,garrisonAlertEnd:end,garrisonAlertCoord:point}=dispatch.dataset;
    const alert=state?.alerts.find(a=>a.army_id===id&&a.end===Number(end)&&a.coord.join(',')===point);
    if(!alert||alert.end<=now()){toast('这条来袭已更新，请使用最新卡片',true);return;}
    openQuickGuard(alert);
    return;
  }
};
$('placeArmiesList').addEventListener('toggle',e=>{
  const details=e.target;
  if(!details.matches('.place-army'))return;
  const id=details.dataset.placeArmy;
  if(details.open){openPlaceArmies.add(id);renderPlaceArmies();}else openPlaceArmies.delete(id);
},true);
$('closeMapPlacePopup').onclick=()=>closeMapPlacePopup(true);
$('mapPlacePopupBody').onclick=e=>{
  const button=e.target.closest('[data-place-details]');
  if(button){e.preventDefault();selectArmy(button.dataset.placeDetails);}
};
$('closeAlertPopup').onclick=()=>dismissAlertPopup(true);
$('alertPopupDetails').onclick=()=>{setInspectorView('army');$('armyTab').focus({preventScroll:true});};
document.addEventListener('click',e=>{const button=e.target.closest('[data-game-coord]');if(button)locateInGame(button);});
document.addEventListener('pointerdown',e=>{
  if(mapPlacePopup?.pinned&&!e.target.closest('#mapPlacePopup,#map'))closeMapPlacePopup(true);
  if(alertPopupAnchor&&!e.target.closest('#alertPopup,[data-watch-army]'))dismissAlertPopup();
});
document.addEventListener('keydown',handleMapEscape);
window.addEventListener('resize',()=>{if(alertPopupAnchor)renderAlertPopup(selectedIncoming(scopedAlerts()));});
const canvas=$('map'),ctx=canvas.getContext('2d');const map={x:0,y:0,scale:17,width:600,height:600,initialized:false,points:[],lineupLabels:[],hover:null,drag:null,moved:false};
function fitMap(){if(!state)return;const a=activeCampaign()?.filters.region||state.area;if(a&&a.length===4){map.x=(a[0]+a[2])/2;map.y=(a[1]+a[3])/2;map.scale=Math.max(5,Math.min(24,map.width/(Math.abs(a[2]-a[0])+Math.abs(a[3]-a[1])+10)));}else{focusTarget();}map.drag=null;map.followArea=true;map.initialized=true;draw();}
function focusTarget(){if(state){const point=focusedPoint();[map.x,map.y]=point||[watchedTarget().x,watchedTarget().y];map.followArea=!point||!state.area||inRegion(point,state.area);}map.drag=null;map.initialized=true;draw();}
function mapArmyPoint(row,at){
  if(!activeObservedAttack(row)||!row.begin||row.end<=row.begin)return row.display_position;
  const fraction=Math.max(0,Math.min(1,(at-row.begin)/(row.end-row.begin)));
  return row.from.map((start,index)=>start+(row.target[index]-start)*fraction);
}
function mapHit(points,x,y){
  return points.map(p=>({...p,d:Math.hypot(p.x-x,p.y-y)})).filter(p=>p.d<=p.radius)
    .sort((a,b)=>(a.d-b.d)||Number(Boolean(b.point))-Number(Boolean(a.point)))[0];
}
function mapLineupHit(x,y){
  return map.lineupLabels.find(box=>x>=box.left&&x<=box.right&&y>=box.top&&y<=box.bottom);
}
function mapLandmarkHit(x,y){
  return (map.landmarkLabels||[]).find(box=>x>=box.left&&x<=box.right&&y>=box.top&&y<=box.bottom);
}
function mapPlaceBadgeHit(x,y){
  return (map.placeLabels||[]).find(box=>x>=box.left&&x<=box.right&&y>=box.top&&y<=box.bottom);
}
function renderMapSkillTooltip(alerts){
  if(mapPlacePopup?.pinned){$('mapSkillTooltip').hidden=true;return;}
  const panel=$('mapSkillTooltip'), hover=map.hover;
  const label=hover&&mapLineupHit(hover.x,hover.y);
  const hit=hover&&(label||mapPlaceBadgeHit(hover.x,hover.y)||mapLandmarkHit(hover.x,hover.y)||mapHit(map.points,hover.x,hover.y));
  const fort=hit?.point&&(state?.forts||[]).find(item=>coord(item.coord)===coord(hit.point));
  const land=hit?.point&&(state?.land_tiles||[]).find(item=>coord(item.coord)===coord(hit.point));
  const army=hit?.id&&state.armies.find(item=>item.id===hit.id);
  const present=hit?.point&&placeArmyGroups().get(hit.point.join(','));
  canvas.title=fort?'':
    land?`${placeLabel(land.coord,land.name)} · ${landLabel(land)}`:army?
      army.relation==='self'?`${army.player_name} · ${observedArmyLabel(army)}`:mapAttackDescription(army):
    hit?.point?placeLabel(hit.point,mapPlaceName(hit.point)):'';
  const alert=hit?.id&&alerts.find(item=>item.army_id===hit.id);
  const prediction=alert&&predictionFor(alert.army_id);
  const report=prediction&&mapLineupReport(prediction,alertForce(prediction,alert.map_hero_count).kind);
  const incoming=hit?.point?mapPlaceIncoming(hit.point):[];
  const pointKey=hit?.point?.join(',')||'';
  if(mapPlaceHoverBlocked&&pointKey!==mapPlaceHoverBlocked)mapPlaceHoverBlocked='';
  if(displayPreferences.mapLabels&&pointKey&&pointKey!==mapPlaceHoverBlocked&&
    (present?.rows.length||incoming.length||guardEncounter(hit.point).confirmed.length)){
    panel.hidden=true;
    canvas.title='';
    openMapPlacePopup(hit.point,hover.anchor,false);
    return;
  }
  if(mapPlacePopup&&!mapPlacePopup.pinned)closeMapPlacePopup();
  const showFort=Boolean(fort&&displayPreferences.mapLabels&&!isFocusedTarget(fort.coord));
  const showPlace=Boolean(hit?.kind==='place-armies'&&present?.rows.length);
  panel.hidden=!showFort&&!showPlace&&!report?.heroes?.length;
  panel.classList.toggle('landmark-hover',showFort||showPlace);
  canvas.classList.toggle('skill-hover',!panel.hidden);
  if(panel.hidden)return;
  const markup=showFort||showPlace?
    `<header><strong>${esc(fort?.name||mapPlaceName(hit.point)||'地块')}</strong></header><div class="map-landmark-coordinate">${esc(coord(hit.point))}</div>
    ${present?.rows.length?`<p>已观测 ${present.rows.length} 队 · ${esc(placeArmyCountText(present.rows))}</p><p>${esc([...new Set(present.rows.map(r=>r.player_name))].slice(0,5).join('、'))}</p><p>点击驻守标记展开全部队伍</p>`:''}`:
    `<header><strong>${esc(alert.player_name)}</strong><span>战法</span></header>${heroLoadouts(report.heroes,true,false)}`;
  if(panel.dataset.markup!==markup){panel.innerHTML=markup;panel.dataset.markup=markup;}
  const height=panel.offsetHeight;
  const position=alertPopupPosition(hover.anchor,panel.offsetWidth,height,window.innerWidth,window.innerHeight);
  if(label){
    const canvasTop=canvas.getBoundingClientRect().top;
    const below=canvasTop+label.bottom+8;
    position.top=below+height<=window.innerHeight-8?below:Math.max(8,canvasTop+label.top-height-8);
  }
  panel.style.left=`${position.left}px`;
  panel.style.top=`${position.top}px`;
}
function clearMapHover(){
  if(mapPlacePopup&&!mapPlacePopup.pinned)closeMapPlacePopup();
  map.hover=null;
  renderMapSkillTooltip([]);
}
function mapTarget(point,kind){
  const p=project(point);
  if(p.x< -24||p.x>map.width+24||p.y< -24||p.y>map.height+24)return;
  map.points.push({...p,point,radius:kind==='fort'?18:16});
  return p;
}
const mapSideColor=row=>typeof row==='string'?
  (row==='enemy'?colors.enemy:['self','ally','friendly'].includes(row)?colors.ally:null):
  row?.relation==='self'&&clientSelfPalette?clientSelfPalette.inside:factionGroups[factionGroup(row)].color;
const mapSideBorder=row=>row?.relation==='self'&&clientSelfPalette?clientSelfPalette.outside:
  factionGroups[factionGroup(row)].border||mapSideColor(row);
const mapTerrainStyles={
  neutral:{fill:'#f0f2f5',border:'#b8c0cc'},
  water:{fill:'#dceffc',border:'#88bed6',ink:'#398bb0'},
  mountain:{fill:'#eee5d8',border:'#aa9480',ink:'#675344'}
};
function mapLandHalfSize(){return Math.max(5,Math.min(8,map.scale*.4));}
function drawMapLand(land){
  const color=mapSideColor(land);
  if(!color||factionGroup(land)==='neutral'&&['', '0'].includes(String(land.owner_id??''))){
    const [x,y]=land.coord,code=mapTerrain.data?.[(x-1)*mapTerrain.size+y-1]&7;
    if(!mapTerrain.data||code!==1&&code!==3)drawMapCell(land.coord,'neutral',true);
    return;
  }
  const p=mapTarget(land.coord,'land');
  if(!p)return;
  const size=mapLandHalfSize();
  const border=mapSideBorder(land);
  ctx.save();ctx.strokeStyle=border;ctx.lineWidth=isFocusedTarget(land.coord)?2.5:border!==color?1.7:1.3;
  ctx.fillStyle=color+(border!==color?'20':'12');ctx.fillRect(p.x-size,p.y-size,size*2,size*2);
  ctx.strokeRect(p.x-size,p.y-size,size*2,size*2);ctx.restore();
}
function drawMapFort(fort){
  const color=mapSideColor(fort);
  if(!color){drawMapCell(fort.coord,'neutral',true);return;}
  const p=mapTarget(fort.coord,'fort');
  if(!p)return;
  const scale=Math.min(1,Math.max(.6,map.scale/20));
  const border=mapSideBorder(fort);
  ctx.save();ctx.translate(p.x,p.y);ctx.scale(scale,scale);ctx.strokeStyle=border;ctx.lineWidth=isFocusedTarget(fort.coord)?2.5:border!==color?1.7:1.6;
  ctx.fillStyle=fort.status==='ready'?color:border!==color?color+'22':'#fffdf5';
  if(fort.status!=='ready')ctx.setLineDash(fort.status==='building'?[4,2]:[1,3]);
  ctx.beginPath();
  const outline=[[-10,9],[-10,-9],[-5,-9],[-5,-5],[-2,-5],[-2,-9],[2,-9],[2,-5],[5,-5],[5,-9],[10,-9],[10,9]];
  outline.forEach(([x,y],index)=>{if(index)ctx.lineTo(x,y);else ctx.moveTo(x,y);});
  ctx.closePath();ctx.fill();ctx.stroke();
  if(fort.status==='ready'){ctx.fillStyle='#fff';ctx.fillRect(-2,3,4,6);}
  ctx.restore();
}
function drawFocusedTarget(){
  const point=focusedPoint();
  if(!point)return;
  const p=project(point),size=Math.max(13,Math.min(20,map.scale*.7));
  ctx.save();ctx.setLineDash([]);
  ctx.strokeStyle='#fff';ctx.lineWidth=5;ctx.strokeRect(p.x-size,p.y-size,size*2,size*2);
  ctx.strokeStyle='#b77912';ctx.lineWidth=2.5;ctx.strokeRect(p.x-size,p.y-size,size*2,size*2);
  ctx.restore();
}
function drawMapCell(point,kind='neutral',interactive=false){
  const p=project(point),half=mapLandHalfSize();
  if(p.x< -half||p.x>map.width+half||p.y< -half||p.y>map.height+half)return;
  const style=mapTerrainStyles[kind]||mapTerrainStyles.neutral;
  ctx.save();ctx.setLineDash([]);
  ctx.fillStyle=style.fill;ctx.fillRect(p.x-half,p.y-half,half*2,half*2);
  ctx.strokeStyle=style.border;ctx.lineWidth=1;
  ctx.strokeRect(p.x-half,p.y-half,half*2,half*2);
  // The symbol appears only when tile spacing is large enough to read it.
  // It is fixed geometry: no animation or additional polling is required.
  if(map.scale>=24&&half>=7&&style.ink){
    ctx.strokeStyle=style.ink;ctx.lineWidth=1.2;ctx.lineCap='round';ctx.lineJoin='round';
    if(kind==='water'){
      for(const offset of [-.28,.28]){
        const y=p.y+half*offset;
        ctx.beginPath();ctx.moveTo(p.x-half*.66,y);
        ctx.bezierCurveTo(p.x-half*.44,y-half*.2,p.x-half*.22,y-half*.2,p.x,y);
        ctx.bezierCurveTo(p.x+half*.22,y+half*.2,p.x+half*.44,y+half*.2,p.x+half*.66,y);
        ctx.stroke();
      }
    }else if(kind==='mountain'){
      ctx.beginPath();ctx.moveTo(p.x-half*.7,p.y+half*.4);
      ctx.lineTo(p.x-half*.18,p.y-half*.45);ctx.lineTo(p.x+half*.36,p.y+half*.4);
      ctx.moveTo(p.x+half*.1,p.y+half*.1);ctx.lineTo(p.x+half*.38,p.y-half*.3);
      ctx.lineTo(p.x+half*.75,p.y+half*.4);ctx.stroke();
    }
  }
  ctx.restore();
  // Small land cells must not steal clicks from nearby marching armies.
  if(interactive)map.points.push({...p,point,kind:'land',radius:Math.min(6,half+2)});
}
function drawMapTerrain(){
  const area=state.area;
  if(!mapTerrain.data||!Array.isArray(area)||area.length!==4||!area.every(Number.isFinite))return;
  const corners=[[0,0],[map.width,0],[0,map.height],[map.width,map.height]].map(([x,y])=>unproject(x,y));
  // Only visit the intersection of the canvas and the game's current scene.
  // Panning or zooming out must not spread terrain beyond the battlefield.
  const minX=Math.max(1,Math.ceil(area[0]),Math.floor(Math.min(...corners.map(p=>p[0]))));
  const maxX=Math.min(mapTerrain.size,Math.floor(area[2]),Math.ceil(Math.max(...corners.map(p=>p[0]))));
  const minY=Math.max(1,Math.ceil(area[1]),Math.floor(Math.min(...corners.map(p=>p[1]))));
  const maxY=Math.min(mapTerrain.size,Math.floor(area[3]),Math.ceil(Math.max(...corners.map(p=>p[1]))));
  const occupied=new Set([...(state.land_tiles||[]),...(state.forts||[])]
    .filter(t=>t.status||factionGroup(t)!=='neutral'||!['', '0'].includes(String(t.owner_id??''))).map(t=>t.coord.join(',')));
  for(let x=minX;x<=maxX;x++)for(let y=minY;y<=maxY;y++){
    // Client MapAllData: x-major, one byte per tile, terrain in the low 3 bits.
    const code=mapTerrain.data[(x-1)*mapTerrain.size+y-1]&7,point=[x,y];
    if(occupied.has(point.join(',')))continue;
    if(code===1)drawMapCell(point,'water');
    else if(code===3)drawMapCell(point,'mountain');
    // Gray is the background for unoccupied/other land in the subscribed area;
    // it never creates ownership or expands the enemy-alert collection scope.
    else drawMapCell(point,'neutral');
  }
}
function drawMapCoordinates(){
  map.axisLabels=[];
  ctx.save();ctx.font='10px Consolas, Microsoft YaHei';ctx.textBaseline='middle';
  for(const tick of mapCoordinateTicks()){
    const text=`${tick.axis} ${tick.value}`,width=ctx.measureText(text).width+10,height=18;
    const left=tick.axis==='X'?Math.max(2,Math.min(map.width-width-2,tick.x-width/2)):tick.x-width;
    const top=tick.y-height/2;
    ctx.fillStyle='#fffffff0';ctx.fillRect(left,top,width,height);
    ctx.fillStyle='#53647e';ctx.textAlign='left';ctx.fillText(text,left+5,tick.y);
    map.axisLabels.push({left,top,right:left+width,bottom:top+height});
  }
  ctx.restore();
}
function drawLandmarkLabels(alerts){
  const bounds={left:8,top:82,right:map.width-8,bottom:map.height-84};
  if(bounds.right-bounds.left<100||bounds.bottom-bounds.top<40)return;
  const attacked=new Set(alerts.map(alert=>alert.coord?.join(',')));
  // Only the selected target stays labelled; other forts use the hover tooltip.
  const candidates=(state.forts||[]).filter(fort=>isFocusedTarget(fort.coord)).map(fort=>({...fort,point:fort.coord}));
  const focus=focusedPoint();
  if(focus&&!candidates.some(item=>isFocusedTarget(item.point)))
    candidates.push({point:focus,name:mapPlaceName(focus)||'地块'});
  const items=candidates.map(item=>{
    const focused=isFocusedTarget(item.point),p=project(item.point);
    return {...item,p,focused,rank:focused?0:attacked.has(item.point.join(','))?1:2};
  }).filter(item=>item.p.x>=8&&item.p.x<=map.width-8&&item.p.y>=82&&item.p.y<=map.height-84&&
    (item.focused||map.scale>=18||map.scale>=10&&item.rank===1))
    .sort((a,b)=>a.rank-b.rank||Math.hypot(a.p.x-map.width/2,a.p.y-map.height/2)-
      Math.hypot(b.p.x-map.width/2,b.p.y-map.height/2));
  const fixed=[...map.lineupLabels,...(map.placeLabels||[]),...(map.axisLabels||[])];
  const blocked=[...fixed,...map.points.filter(p=>p.point).map(p=>
    ({left:p.x-8,right:p.x+8,top:p.y-8,bottom:p.y+8}))];
  ctx.save();ctx.textBaseline='top';ctx.textAlign='left';
  for(const item of items){
    if(map.landmarkLabels.length>=8)break;
    const title=item.name||'要塞',detail=coord(item.point);
    ctx.font='11px Segoe UI, Microsoft YaHei';
    const width=Math.min(154,Math.max(ctx.measureText(title).width,ctx.measureText(detail).width)+16),height=34;
    const left=Math.max(bounds.left,Math.min(bounds.right-width,item.p.x+12));
    const top=Math.max(bounds.top,Math.min(bounds.bottom-height,item.p.y-height-10));
    const box=mapLabelBox(item.p,width,height,bounds,blocked)||
      mapLabelBox(item.p,width,height,bounds,fixed)||{left,top,right:left+width,bottom:top+height};
    if(!box)continue;
    const color=item.focused?'#b77912':mapSideBorder(item);
    ctx.fillStyle=item.focused?'#fffaf0':'#fffffff2';ctx.fillRect(box.left,box.top,width,height);
    ctx.strokeStyle=color;ctx.lineWidth=1;ctx.strokeRect(box.left+.5,box.top+.5,width-1,height-1);
    ctx.fillStyle=item.focused?'#805300':'#344963';ctx.fillText(title,box.left+8,box.top+4,width-16);
    ctx.fillStyle='#53647e';ctx.font='10px Consolas, Microsoft YaHei';ctx.fillText(detail,box.left+8,box.top+19,width-16);
    map.landmarkLabels.push({...box,point:item.point});
    const padded={left:box.left-3,top:box.top-3,right:box.right+3,bottom:box.bottom+3};
    blocked.push(padded);fixed.push(padded);
  }
  ctx.restore();
}
function drawSelectedAttackRoute(origin, position, target, color){
  const line=(a,b)=>{ctx.beginPath();ctx.moveTo(a.x,a.y);ctx.lineTo(b.x,b.y);ctx.stroke();};
  ctx.save();ctx.lineCap='round';
  ctx.strokeStyle='#fff';ctx.lineWidth=8;line(origin,target);
  ctx.strokeStyle=color;ctx.lineWidth=2.5;ctx.setLineDash([5,4]);line(origin,position);
  ctx.strokeStyle=color;ctx.lineWidth=4.5;ctx.setLineDash([]);line(position,target);
  ctx.restore();
}
function drawAttackArrow(point,origin,target,color,large=false){
  ctx.save();
  ctx.translate(point.x,point.y);
  ctx.rotate(Math.atan2(target.y-origin.y,target.x-origin.x));
  ctx.fillStyle=color;
  ctx.beginPath();ctx.moveTo(large?13:10,0);ctx.lineTo(-7,-6);ctx.lineTo(-4,0);ctx.lineTo(-7,6);ctx.closePath();ctx.fill();
  ctx.restore();
}
function drawMapArmy(row,at,scope,highlighted=false){
  const attack=activeObservedAttack(row);
  if(!attack||scope&&!scope(row.target))return;
  const position=mapArmyPoint(row,at)||(highlighted?row.from:null);
  if(!position)return;
  const p=project(position), t=row.target?project(row.target):null;
  const origin=row.from?project(row.from):p;
  const color=mapAttackColor(row);
  if(highlighted&&t)drawSelectedAttackRoute(origin,p,t,color);
  else if(t){
    ctx.save();ctx.strokeStyle=color;ctx.lineWidth=1.3;ctx.globalAlpha=.6;
    ctx.setLineDash([5,4]);ctx.beginPath();ctx.moveTo(origin.x,origin.y);ctx.lineTo(t.x,t.y);ctx.stroke();ctx.restore();
  }
  const inView=p.x>=0&&p.x<=map.width&&p.y>=0&&p.y<=map.height;
  const attackMarker=(attack||highlighted)&&t?(inView?p:visibleRouteMarker(p,t,map.width,map.height)):null;
  // The route encodes target ownership; the white-edged troop marker encodes faction.
  if(attackMarker){
    const info=factionGroups[factionGroup(row)];
    ctx.save();ctx.strokeStyle=info.border||'#fff';ctx.lineWidth=info.border?2.5:2;
    const size=highlighted?8:6;
    ctx.fillStyle=factionGroups[factionGroup(row)].color;ctx.fillRect(attackMarker.x-size,attackMarker.y-size,size*2,size*2);
    ctx.strokeRect(attackMarker.x-size,attackMarker.y-size,size*2,size*2);ctx.restore();
    drawAttackArrow(attackMarker,origin,t,color,highlighted);
  }
  if(p.x< -24||p.x>map.width+24||p.y< -24||p.y>map.height+24)return Boolean(attackMarker);
  map.points.push({...p,id:row.id,radius:attack?18:16});
  return Boolean(attackMarker);
}
function ownMarchPoint(row,at){
  if(row.relation!=='self'||row.stale||row.visible===false||row.current_connection===false||
    !state?.live||state.reliable===false||state.health.resync_required||![1,2,3,4].includes(row.state_code)||
    !row.from||!row.target||coord(row.from)===coord(row.target)||!row.begin||row.end<=row.begin||at<row.begin||at>=row.end)return null;
  const ratio=(at-row.begin)/(row.end-row.begin);
  const point=row.from.map((value,i)=>value+(row.target[i]-value)*ratio);
  return inRegion(point,state.area)?point:null;
}
function drawOwnMarches(at,scope){
  for(const row of currentArmies()){
    const point=ownMarchPoint(row,at);
    if(!point||scope&&!scope(point))continue;
    const p=project(point),from=project(row.from),target=project(row.target);
    if(p.x< -18||p.x>map.width+18||p.y< -18||p.y>map.height+18)continue;
    if(selected===row.id)drawSelectedAttackRoute(from,p,target,ownArmyColor);
    drawAttackArrow(p,from,target,ownArmyColor,selected===row.id);
    map.points.push({...p,id:row.id,radius:16});
  }
}
function drawPlaceArmies(scope){
  for(const group of placeArmyGroups().values()){
    if(scope&&!scope(group.point))continue;
    const own=group.rows.filter(row=>row.relation==='self').length;
    const badges=[own&&{text:`我${own}`,color:ownArmyColor},...Object.entries(factionGroups).map(([key,info])=>{
      const count=group.rows.filter(row=>row.relation!=='self'&&factionGroup(row)===key&&row.state_code===6).length;
      return count&&{text:`${info.short}驻${count}`,color:info.color,border:info.border};
    })].filter(Boolean);
    const p=project(group.point);
    if(p.x< -20||p.x>map.width+20||p.y< -20||p.y>map.height+20)continue;
    let offset=9;
    ctx.save();ctx.font='600 10px Microsoft YaHei,Segoe UI';ctx.textBaseline='middle';ctx.textAlign='left';
    for(const badge of badges){
      const width=Math.ceil(ctx.measureText(badge.text).width)+10,height=17,left=p.x+offset,top=p.y-20;
      ctx.fillStyle=badge.color;ctx.fillRect(left,top,width,height);
      ctx.strokeStyle=badge.border||'#fff';ctx.lineWidth=badge.border?2:1;ctx.strokeRect(left,top,width,height);
      ctx.fillStyle='#fff';ctx.fillText(badge.text,left+5,top+height/2);
      map.placeLabels.push({left,top,right:left+width,bottom:top+height,point:group.point,kind:'place-armies'});
      offset+=width+3;
    }
    ctx.restore();
  }
}
function drawMapLineups(alerts,at){
  if(!alerts.length)return;
  const bounds={left:8,top:72,right:map.width-8,bottom:map.height-57};
  const titleFont='600 12px Segoe UI, Microsoft YaHei', detailFont='11px Segoe UI, Microsoft YaHei';
  const points=new Map(map.points.filter(point=>point.id).map(point=>[point.id,point]));
  const markers=[], labels=[];
  for(const alert of alerts){
    const point=points.get(alert.army_id);
    if(!point||point.x<0||point.x>map.width||point.y<0||point.y>map.height||alert.end<=at)continue;
    markers.push({left:point.x-10,right:point.x+10,top:point.y-8,bottom:point.y+8});
    const chosen=alert.army_id===selected, prediction=predictionFor(alert.army_id);
    if(!prediction&&!chosen)continue;
    const {kind}=alertForce(prediction,alert.map_hero_count);
    labels.push({point,chosen,kind,player:alert.player_name,report:mapLineupReport(prediction,kind),
      rank:kind==='main'?(chosen?-2:-1):chosen?0:watchPriority[kind]});
  }
  labels.sort((a,b)=>a.rank-b.rank);
  const blocked=[...markers,...(map.placeLabels||[]),...(map.axisLabels||[])];
  ctx.save();
  ctx.textBaseline='top';
  for(const {point,chosen,kind,player,report} of labels){
    const main=kind==='main', lines=mapLineupText(player,kind,report);
    const textWidths=lines.map((line,index)=>{
      ctx.font=index===0?titleFont:detailFont;
      return ctx.measureText(line).width;
    });
    const width=Math.min(bounds.right-bounds.left,Math.ceil(Math.max(...textWidths))+16);
    const height=6+lines.length*14;
    const box=mapLabelBox(point,width,height,bounds,blocked)||
      (chosen?mapLabelBox(point,width,height,bounds,blocked.slice(markers.length)):null);
    if(!box)continue;
    map.lineupLabels.push({...box,id:point.id});
    blocked.push({left:box.left-4,top:box.top-3,right:box.right+4,bottom:box.bottom+3});
    const x=Math.max(box.left,Math.min(point.x,box.right)), y=Math.max(box.top,Math.min(point.y,box.bottom));
    const length=Math.hypot(x-point.x,y-point.y);
    ctx.lineWidth=1;
    ctx.strokeStyle=chosen||main?'#af433c':'#91a1b7';
    ctx.beginPath();ctx.moveTo(point.x+(x-point.x)*10/length,point.y+(y-point.y)*10/length);ctx.lineTo(x,y);ctx.stroke();
    ctx.fillStyle=chosen||main?'#fff8f4':'#ffffff';
    ctx.fillRect(box.left,box.top,width,height);ctx.strokeRect(box.left+.5,box.top+.5,width-1,height-1);
    if(main){ctx.fillStyle='#af433c';ctx.fillRect(box.left,box.top,3,height);}
    lines.forEach((line,index)=>{
      ctx.fillStyle=index===0?(main?'#9d3029':'#172740'):main&&index===1?'#81352e':'#53647e';
      ctx.font=index===0?titleFont:detailFont;
      ctx.fillText(line,box.left+8,box.top+5+14*index,width-16);
    });
  }
  ctx.restore();
}
function drawGuardCoverage(scope){
  if(!displayPreferences.guardCoverage)return;
  const cells=guardCoverageIndex();
  const palette=Object.fromEntries(Object.entries(factionGroups).map(([key,info])=>[key,{ink:info.border||info.color,fill:info.color+'24'}]));
  const side=factionGroup;
  ctx.save();ctx.lineWidth=1.2;
  for(const cell of cells.values()){
    if(scope&&!scope(cell.point))continue;
    const p=project(cell.point),margin=map.scale*2;
    if(p.x< -margin||p.x>map.width+margin||p.y< -margin||p.y>map.height+margin)continue;
    const sides=[...new Set(cell.entries.map(e=>side(e.row)))];
    const corners=[[-.5,-.5],[.5,-.5],[.5,.5],[-.5,.5]].map(([dx,dy])=>project([cell.point[0]+dx,cell.point[1]+dy]));
    for(const [index,team] of sides.entries()){
      const confirmed=cell.entries.some(e=>e.confirmed&&side(e.row)===team),color=palette[team];
      ctx.beginPath();corners.forEach((q,i)=>i?ctx.lineTo(q.x,q.y):ctx.moveTo(q.x,q.y));ctx.closePath();
      ctx.fillStyle=confirmed?color.fill:'#8695aa0b';ctx.fill();
      ctx.strokeStyle=color.ink;ctx.setLineDash(confirmed?[]:[3,3]);
      // Draw the perimeter only, keeping the nine covered cells readable without a second grid.
      const neighbors=[[0,-1],[1,0],[0,1],[-1,0]];
      neighbors.forEach(([dx,dy],edge)=>{
        const neighbor=cells.get([cell.point[0]+dx,cell.point[1]+dy].join(','));
        if(neighbor?.entries.some(e=>side(e.row)===team&&e.confirmed===confirmed)&&(!scope||scope(neighbor.point)))return;
        const a=corners[edge],b=corners[(edge+1)%4];
        ctx.beginPath();ctx.moveTo(a.x,a.y+index*2);ctx.lineTo(b.x,b.y+index*2);ctx.stroke();
      });
    }
  }
  ctx.restore();
}
function draw(){const w=map.width,h=map.height;ctx.clearRect(0,0,w,h);ctx.fillStyle='#f5f8fd';ctx.fillRect(0,0,w,h);
  if(canvas.dataset){canvas.dataset.viewCenter=JSON.stringify([map.x,map.y]);canvas.dataset.viewScale=String(map.scale);
    canvas.dataset.gameArea=JSON.stringify(map.gameArea||null);}
  canvas.setAttribute?.('aria-description',`视野中心 ${map.x.toFixed(1)}, ${map.y.toFixed(1)}；缩放 ${map.scale.toFixed(1)}`);
  if(displayPreferences.mapGrid){
  const corners=[[0,0],[w,0],[0,h],[w,h]].map(([x,y])=>unproject(x,y));const xs=corners.map(p=>p[0]),ys=corners.map(p=>p[1]);const step=map.scale<10?5:1;const loX=Math.floor(Math.min(...xs)/step)*step,hiX=Math.ceil(Math.max(...xs)/step)*step,loY=Math.floor(Math.min(...ys)/step)*step,hiY=Math.ceil(Math.max(...ys)/step)*step;ctx.lineWidth=.6;
  for(let x=loX;x<=hiX;x+=step){const a=project([x,loY]),b=project([x,hiY]);ctx.strokeStyle=x%5===0?'#c4d1e4':'#e3eaf4';ctx.beginPath();ctx.moveTo(a.x,a.y);ctx.lineTo(b.x,b.y);ctx.stroke();}for(let y=loY;y<=hiY;y+=step){const a=project([loX,y]),b=project([hiX,y]);ctx.strokeStyle=y%5===0?'#c4d1e4':'#e3eaf4';ctx.beginPath();ctx.moveTo(a.x,a.y);ctx.lineTo(b.x,b.y);ctx.stroke();}
  }
  map.points=[];
  map.lineupLabels=[];
  map.landmarkLabels=[];
  map.placeLabels=[];
  if(!state)return;
  drawMapTerrain();
  drawMapCoordinates();
  const focus=focusedPoint(), campaign=activeCampaign(), region=campaign?.filters.region, at=now();
  const scope=region?point=>inCampaignTime(campaign.filters,at)&&inCampaignArea(point,campaign):null;
  drawGuardCoverage(scope);
  for(const land of state.land_tiles||[])drawMapLand(land);
  for(const fort of state.forts||[])drawMapFort(fort);
  drawFocusedTarget();
  const alerts=scopedAlerts();
  const highlighted=selectedIncoming(alerts)?.row;
  const current=currentArmies().filter(mapArmyEnabled);
  $('mapEmpty').style.display=current.some(activeObservedAttack)||(state.forts||[]).some(f=>mapSideColor(f))||
    (state.land_tiles||[]).length||mapTerrain.data||placeArmyGroups().size||current.some(row=>ownMarchPoint(row,at))?'none':'flex';
  const rows=current.filter(row=>row!==highlighted);
  let arrows=0;
  const attacks=rows.filter(activeObservedAttack).sort((a,b)=>Number(isFocusedTarget(a.target))-Number(isFocusedTarget(b.target)));
  for(const row of attacks)if(drawMapArmy(row,at,scope))arrows++;
  if(highlighted&&drawMapArmy(highlighted,at,scope,true))arrows++;
  drawOwnMarches(at,scope);
  drawPlaceArmies(scope);
  const status=$('mapRouteStatus'), message=routeStatus(arrows);
  if(status.textContent!==message)status.textContent=message;
  status.hidden=!message;
  $('clearMapScope').hidden=!focus&&!scope;
  $('clearMapScope').textContent=focus?'取消关注':'全部战场';
  drawMapLineups(alerts,at);
  drawLandmarkLabels(alerts);
  if(map.hover)renderMapSkillTooltip(alerts);
  if(mapPlacePopup)renderMapPlacePopup();
}
function resize(){const rect=canvas.getBoundingClientRect(),dpr=Math.min(window.devicePixelRatio||1,2);map.width=rect.width;map.height=rect.height;canvas.width=Math.round(rect.width*dpr);canvas.height=Math.round(rect.height*dpr);ctx.setTransform(dpr,0,0,dpr,0,0);draw();}new ResizeObserver(resize).observe(canvas);
canvas.onpointerdown=e=>{
  clearMapHover();
  map.drag={x:e.offsetX,y:e.offsetY,anchor:unproject(e.offsetX,e.offsetY)};
  map.moved=false;
  canvas.setPointerCapture(e.pointerId);
};
canvas.onpointermove=e=>{
  if(map.drag&&e.buttons===0)map.drag=null;
  const point=unproject(e.offsetX,e.offsetY);
  if(map.drag){
    const {x,y,anchor}=map.drag;
    if(Math.abs(e.offsetX-x)+Math.abs(e.offsetY-y)>4)map.moved=true;
    if(map.moved){map.followArea=false;closeMapPlacePopup();map.x+=anchor[0]-point[0];map.y+=anchor[1]-point[1];draw();}
  }else if(e.pointerType!=='touch'){
    map.hover={x:e.offsetX,y:e.offsetY,anchor:{x:e.clientX,y:e.clientY}};
    renderMapSkillTooltip(state?scopedAlerts():[]);
  }
  const [x,y]=map.drag?.anchor??point;
  $('mapCoordinates').textContent=`X ${Math.round(x)} · Y ${Math.round(y)}`;
};
canvas.onpointerup=e=>{
  if(state&&map.drag&&!map.moved){
    const hit=mapLineupHit(e.offsetX,e.offsetY)||mapPlaceBadgeHit(e.offsetX,e.offsetY)||mapLandmarkHit(e.offsetX,e.offsetY)||mapHit(map.points,e.offsetX,e.offsetY);
    if(hit?.id)selectArmy(hit.id);
    else{
      const point=hit?.point||unproject(e.offsetX,e.offsetY).map(Math.round);
      const focused=toggleMapPlaceFocus(point,{x:e.clientX,y:e.clientY});
      if(focused&&hit?.kind==='place-armies'){$('placeArmies').open=true;renderPlaceArmies();}
    }
  }
  map.drag=null;
};
canvas.onpointerleave=clearMapHover;
canvas.onpointercancel=()=>{map.drag=null;clearMapHover();};
canvas.onlostpointercapture=()=>{map.drag=null;};
window.addEventListener('blur',()=>{map.drag=null;clearMapHover();});
canvas.addEventListener('wheel',e=>{e.preventDefault();map.scale=Math.max(3,Math.min(70,map.scale*(e.deltaY<0?1.12:.89)));draw();},{passive:false});$('zoomIn').onclick=()=>{map.scale=Math.min(70,map.scale*1.25);draw();};$('zoomOut').onclick=()=>{map.scale=Math.max(3,map.scale/1.25);draw();};$('fitView').onclick=fitMap;$('focusTarget').onclick=focusTarget;
if(alertFocus)setInspectorView('watch');
document.addEventListener('desktop:mode-change',()=>{inspectorKey='';renderDetails();});
document.addEventListener('toggle',event=>{
  if(event.target.isConnected&&event.target.dataset?.selectedTactics===selected)selectedTacticsOpen=event.target.open;
},true);
document.addEventListener('visibilitychange',()=>{if(document.hidden)map.drag=null;else refresh();});
window.addEventListener('focus',refresh);
refresh();setInterval(refresh,2000);setInterval(tick,1000);
