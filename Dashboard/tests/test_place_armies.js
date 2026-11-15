'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'../static/app.js'),'utf8');
const elements=new Map();
let clock=1000000,requests=0,finish;
const element=id=>{
  if(!elements.has(id))elements.set(id,{innerHTML:'',textContent:'',dataset:{},style:{},hidden:false,isConnected:true,
    classList:{add(){},remove(){},toggle(){}},setAttribute(){},querySelectorAll:()=>[],contains:()=>false});
  return elements.get(id);
};
const context=vm.createContext({document:{getElementById:element,querySelector:element,querySelectorAll:()=>[]},
  window:{innerWidth:1000,innerHeight:700},Date:class extends Date{static now(){return clock;}},URL,URLSearchParams,AbortSignal,
  localStorage:{getItem:()=>null,setItem(){}},location:{href:'http://127.0.0.1:8787/battlefield',search:''},
  history:{replaceState(){}},setTimeout(){},clearTimeout(){},
  fetch:async()=>{requests++;return new Promise(resolve=>{finish=resolve;});}});
vm.runInContext(source.split("$('settingsButton').onclick")[0],context);
const base={visible:true,current_connection:true,stale:false,observed_at:1000,seen_at:1000,age:0,end:800,begin:700,
  from:[987,925],target:[987,925],position:[987,925],display_position:[987,925],state_code:6,kind:'garrison',
  relation:'enemy',owner:'enemy-owner',union_id:'1008',player_name:'同名玩家',troops:[{position:1,code:23,name:'轻骑兵'},
    {position:2,code:172,name:'白衣死士'},{position:3,code:32,name:'禁卫'}]};
const rows=Array.from({length:15},(_,i)=>({...base,id:String(8000+i)}));
rows.push({...base,id:'own',owner:'12319',player_name:'本人',relation:'self',union_id:'1009',state_code:5,kind:null});
rows.push({...base,id:'ally',relation:'ally',union_id:'1009'});
rows.push({...base,id:'home-is-not-location',state_code:5,position:[990,920],from:[990,920],target:[990,920],reside:[987,925]});
rows.push({...base,id:'returning',state_code:4,kind:null,position:null,from:[987,925],target:[990,920],end:1100,begin:900});
rows.push({...base,id:'arriving',state_code:3,kind:null,position:null,from:[990,920],target:[987,925],end:1100,begin:900});
rows.push({...base,id:'old-cache',stale:true});
rows.push({...base,id:'hidden',visible:false});
rows.push({...base,id:'old-connection',current_connection:false});
rows.push({...base,id:'out-of-scene',position:[1000,925],from:[1000,925],target:[1000,925]});
const ownMove={...base,id:'own-moving',relation:'self',state_code:3,kind:null,position:null,
  from:[985,923],target:[987,925],begin:900,end:1100};
rows.push(ownMove);
vm.runInContext(`state=${JSON.stringify({live:true,reliable:true,area:[976,913,995,932],server_now:1000,
  counts:{fresh:rows.length-1},health:{resync_required:false},config:{self:{user_id:'12319'}},armies:rows,
  alerts:[],forts:[],land_tiles:[]})};alertFocus='987,925';
 const canvas={};const map={x:987,y:925,width:1000,height:700,scale:30,points:[],placeLabels:[]};
 const badgeInk=[],arrowInk=[];const ctx={save(){},restore(){},fillRect(){},strokeRect(){},
   measureText:t=>({width:t.length*6}),fillText(t){badgeInk.push({text:t,color:this.fillStyle});}};
 function drawAttackArrow(p,a,b,color){arrowInk.push({p,color});}function drawSelectedAttackRoute(){}
`,context);
vm.runInContext(source.slice(source.indexOf('function ownMarchPoint('),source.indexOf('function drawMapLineups(')),context);
assert.equal(vm.runInContext("placeArmyGroups().get('987,925').rows.length",context),17);
assert.equal(vm.runInContext("placeArmyGroups().get('987,925').rows.filter(r=>r.player_name==='同名玩家'&&r.relation==='enemy').length",context),15);
assert.equal(vm.runInContext("new Set(placeArmyGroups().get('987,925').rows.map(r=>r.id)).size",context),17);
vm.runInContext('renderPlaceArmies();renderAlertTargets();drawPlaceArmies(null);drawOwnMarches(1000,null)',context);
assert.equal(element('alertTarget').textContent,'987, 925','The focused place is a text label, without duplicate unit counts');
assert.equal(element('watchTargetControl').hidden,false);
assert.equal(element('placeArmiesSummary').textContent,'要塞内/停留 1');
assert.ok(!element('placeArmiesList').innerHTML.includes('faction-badge'),'Queue faction is conveyed by background color, without an extra label');
for(let i=0;i<15;i++)assert.ok(!element('placeArmiesList').innerHTML.includes(`data-place-army="${8000+i}"`),
  'Active guards are shown in encounter order and must not be duplicated in the physical sidebar list');
assert.equal(vm.runInContext("map.placeLabels.find(b=>b.point.join(',')==='987,925').kind",context),'place-armies');
assert.equal(vm.runInContext("badgeInk.some(b=>b.text==='我1')&&badgeInk.some(b=>b.text==='敌驻15')",context),true);
assert.equal(vm.runInContext('arrowInk[0].color',context),'#218a47');
assert.equal(vm.runInContext('JSON.stringify(ownMarchPoint(state.armies.at(-1),1000))',context),'[986,924]');
assert.equal(vm.runInContext('ownMarchPoint(state.armies.at(-1),1100)',context),null,'Arrival time is not proof of stationary arrival');
vm.runInContext('state.reliable=false',context);
assert.equal(vm.runInContext('placeArmyGroups().size',context),0);
assert.equal(vm.runInContext('ownMarchPoint(state.armies.at(-1),1000)',context),null);
vm.runInContext('state.reliable=true',context);
const heroes=['司马师','司马懿','司马炎'].map((name,i)=>({position:i+1,name,id:100+i,level:45,red_stars:i,
  troop_code:[23,172,32][i],troop_name:['轻骑兵','白衣死士','禁卫'][i],remaining_hp:1000,wounded_hp:0,
  skill_slot_ids:[1,2,3],skills:[1,2,3].map(id=>({id,name:'战法'+id,level:10})),gear:{id:1,name:'新武器',level:10}}));
const data={match:'exact_army_id',items:[
  {player_name:'本人',union_id:'1009',event_at:800,battle_id:'old',data:{heroes:heroes.map(h=>({...h,gear:{...h.gear,name:'旧武器'}}))}},
  {player_name:'本人',union_id:'1009',event_at:900,battle_id:'new',data:{heroes}},
  {player_name:'其他玩家',union_id:'1009',event_at:950,battle_id:'other',data:{heroes:heroes.map(h=>({...h,name:'串队武将'}))}}]};
const own=rows.find(r=>r.id==='own');
vm.runInContext(`const detailRow=${JSON.stringify(own)},responseData=${JSON.stringify(data)};`,context);
const markup=vm.runInContext('placeLoadoutMarkup(detailRow,responseData)',context);
assert.ok(markup.includes('司马师')&&markup.includes('红度 2红')&&markup.includes('战法1')&&markup.includes('战法2')&&markup.includes('战法3'));
assert.ok(!markup.includes('旧武器')&&!markup.includes('串队武将'));
const overview=vm.runInContext('placeLoadoutOverview(detailRow,responseData)',context);
assert.equal(overview.roster,'司马师45级(0红) / 司马懿45级(1红) / 司马炎45级(2红)');
assert.ok(overview.rosterMarkup.includes('lineup-hero-level')&&overview.rosterMarkup.includes('等级 45级'));
assert.ok(!overview.rosterMarkup.includes('style='),'Roster density must work without CSP-blocked inline styles');
assert.ok(overview.rosterMarkup.includes('lineup-hero-name">司马师</span>')&&overview.rosterMarkup.includes('aria-label="红度 0红">0红</span>'));
const unsafeName=vm.runInContext("lineupOverview([{name:'<img src=x>',position:1,red_stars:5}]).rosterMarkup",context);
assert.ok(!unsafeName.includes('<img')&&unsafeName.includes('&lt;img src=x&gt;'),'Roster markup must escape player-supplied names');
assert.ok(overview.meta.includes('最近战报')&&overview.meta.includes('08:15:00')&&overview.meta.includes('战后余兵 3,000'));
vm.runInContext('const shuffled={...responseData,items:responseData.items.map(r=>({...r,data:{heroes:[...r.data.heroes].reverse()}}))}',context);
assert.equal(vm.runInContext('placeLoadoutOverview(detailRow,shuffled).roster',context),overview.roster,'Roster order follows camp positions');
vm.runInContext('const noHp={...responseData,items:responseData.items.map(r=>({...r,data:{heroes:r.data.heroes.map((h,i)=>({...h,remaining_hp:i===0?null:h.remaining_hp}))}}))}',context);
assert.ok(vm.runInContext('placeLoadoutOverview(detailRow,noHp).meta',context).includes('战后余兵 未知'),'Never substitute pre-battle HP or an older report');
vm.runInContext('const zeroHp={...responseData,items:responseData.items.map(r=>({...r,data:{heroes:r.data.heroes.map(h=>({...h,remaining_hp:0}))}}))}',context);
assert.ok(vm.runInContext('placeLoadoutOverview(detailRow,zeroHp).meta',context).endsWith('战后余兵 0'));
vm.runInContext("const arrivalData={...responseData,match:'matched_arrival'};",context);
const arrivalMarkup=vm.runInContext('placeLoadoutMarkup(detailRow,arrivalData)',context);
assert.equal((arrivalMarkup.match(/hero-loadouts/g)||[]).length,1,
  'A verified arrival has one current roster, not several player-wide alternatives');
vm.runInContext("const unverifiedData={...responseData,match:'name_union_troop_candidate'};",context);
const unverifiedMarkup=vm.runInContext('placeLoadoutMarkup(detailRow,unverifiedData)',context);
assert.ok(!unverifiedMarkup.includes('<details')&&!unverifiedMarkup.includes('<summary'),
  'The selected configuration displays directly inside the existing team expansion');
assert.equal((unverifiedMarkup.match(/hero-loadouts/g)||[]).length,1,
  'A real-time team displays one best matching lineup rather than all player history');
assert.ok(unverifiedMarkup.includes('战法1')&&unverifiedMarkup.includes('红度 2红'));
assert.ok(!unverifiedMarkup.includes('串队武将'),'Other players must remain excluded');
vm.runInContext(`const alternatives={...unverifiedData,items:unverifiedData.items.map(item=>item.battle_id==='old'?
  {...item,data:{heroes:item.data.heroes.map(h=>({...h,name:'另一队旧武将'}))}}:item)};
  alternatives.items.push({...alternatives.items[1],event_at:1000,battle_id:'wrong-troops',
    data:{heroes:unverifiedData.items[1].data.heroes.map(h=>({...h,name:'不吻合兵种',troop_code:1}))}});`,context);
const oneLineup=vm.runInContext('placeLoadoutMarkup(detailRow,alternatives)',context);
assert.ok(oneLineup.includes('司马师')&&!oneLineup.includes('另一队旧武将')&&!oneLineup.includes('不吻合兵种'),
  'Select the latest same-player matching troops before limiting to one lineup');
assert.equal(vm.runInContext('placeLoadoutOverview(detailRow,alternatives).roster',context),overview.roster,
  'Summary and expanded tactics must reference the same selected report');
assert.equal(vm.runInContext('alternatives.items.length',context),4,'Other reports remain available for archive analysis');
vm.runInContext(`const tied={...unverifiedData,items:[
  {...unverifiedData.items[1],battle_id:'9'},
  {...unverifiedData.items[1],battle_id:'10',data:{heroes:unverifiedData.items[1].data.heroes.map(h=>({...h,name:'同秒新记录'}))}}]};`,context);
assert.ok(vm.runInContext('placeLoadoutMarkup(detailRow,tied)',context).includes('同秒新记录'),
  'Equal battle times use the newer numeric report ID consistently');

assert.ok(markup.includes('45级'),'Expanded general names include levels');
for(const extra of ['新武器','轻骑兵','白衣死士','禁卫','兵种','战后','3,000','战报','要塞内'])assert.ok(!markup.includes(extra),extra+' should not be visible');
const roster=element('placeArmiesList').innerHTML;
for(const extra of ['place-army-meta','place-army-troops','队 own'])assert.ok(!roster.includes(extra),extra+' should not be visible');
assert.ok(roster.includes('data-place-state')&&roster.includes('要塞内'),'Physical membership also displays the actual observed state');
assert.ok(roster.includes('data-place-details="own"')&&roster.includes('army-player-button'),
  'A stationary team must have a player-name entry to its full details');
assert.equal((roster.match(/<details class="place-army(?: [^"]*)?"/g)||[]).length,1);
assert.ok(!/<details class="place-army(?: [^"]*)?"[^>]*\sopen(?:>|\s)/.test(roster),'Tactics are collapsed for every new team');
vm.runInContext("openPlaceArmies.add('own');document.getElementById('placeArmiesList').dataset.key='';renderPlaceArmies()",context);
assert.ok(element('placeArmiesList').innerHTML.includes('data-place-army="own" open'),'An explicitly expanded team stays expanded after a refresh');
vm.runInContext(`const mismatch={...responseData,items:responseData.items.map(item=>({...item,data:{heroes:item.data.heroes.map(h=>({...h,troop_code:1}))}}))};`,context);
assert.ok(vm.runInContext('placeLoadoutMarkup(detailRow,mismatch)',context).includes('未取得'));
const panel=element('lazyLoadout');context.testPanel=panel;
panel.closest=()=>({querySelector:selector=>element(selector==='.place-army-roster'?'summaryRoster':'summaryReport')});
(async()=>{
  const first=vm.runInContext('loadPlaceArmy(detailRow,testPanel)',context);
  await vm.runInContext('loadPlaceArmy(detailRow,testPanel)',context);
  assert.equal(requests,1,'Repeated polls must share the in-flight archive request');
  finish({ok:true,json:async()=>data});await first;
  assert.ok(panel.innerHTML.includes('司马炎'));
  assert.equal(element('summaryRoster').innerHTML,overview.rosterMarkup);
  assert.equal(element('summaryRoster').title,overview.roster);
  assert.equal(element('summaryReport').textContent,overview.meta);
  await vm.runInContext('loadPlaceArmy(detailRow,testPanel)',context);
  assert.equal(requests,1,'An open roster must use the short-lived archive cache');
  clock+=31000;
  const second=vm.runInContext('loadPlaceArmy(detailRow,testPanel)',context);
  assert.equal(requests,2);finish({ok:true,json:async()=>data});await second;
  context.detailPanel=element('fullDetails');
  vm.runInContext("selected='own'",context);
  const detailed=vm.runInContext('loadLineups(detailRow,detailPanel)',context);
  finish({ok:true,json:async()=>vm.runInContext('alternatives',context)});await detailed;
  assert.equal((context.detailPanel.innerHTML.match(/hero-loadouts/g)||[]).length,1,
    'Non-attacking full details must also show just the selected same-player lineup');
  assert.ok(!context.detailPanel.innerHTML.includes('另一队旧武将')&&!context.detailPanel.innerHTML.includes('不吻合兵种'));
  console.log('Place membership, separate army IDs, green own markers, safe marching, exact-owner loadouts and cached requests passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
