'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const source=fs.readFileSync(path.join(__dirname,'../static/app.js'),'utf8');
const elements=new Map(); let clock=1000000,requests=0,finish,scopedUrl;
const element=id=>{
  if(!elements.has(id))elements.set(id,{innerHTML:'',textContent:'',hidden:false,dataset:{},style:{},
    scrollTop:0,offsetWidth:430,offsetHeight:400,isConnected:true,
    classList:{add(){},remove(){},toggle(){}},setAttribute(){},querySelectorAll:()=>[]});
  return elements.get(id);
};
const context=vm.createContext({document:{getElementById:element,querySelector:element,querySelectorAll:()=>[]},
  Date:class extends Date{static now(){return clock;}},URL,URLSearchParams,AbortSignal,
  location:{href:'http://127.0.0.1:8787/battlefield',search:''},history:{replaceState(_data,_unused,url){scopedUrl=String(url);}},
  window:{innerWidth:1000,innerHeight:700},setTimeout(){},clearTimeout(){},
  fetch:async()=>{requests++;return new Promise(resolve=>{finish=resolve;});}});
vm.runInContext(source.split("$('settingsButton').onclick")[0],context);
vm.runInContext(`const map={x:974,y:908,scale:29,width:800,height:600};
  function draw(){}function renderStatus(){}function renderWatch(){}
  function setInspectorView(){}function dismissAlertPopup(){}
  state={live:true,reliable:true,area:[970,900,985,920],health:{resync_required:false},counts:{fresh:4},
    forts:[{coord:[974,908],name:'弋阳要塞'}],land_tiles:[],config:{self:{user_id:'12319'}},
    armies:[],alerts:[],targets:[]};`,context);
const army={id:'own',owner:'12319',union_id:'1009',player_name:'自己',relation:'self',
  visible:true,current_connection:true,stale:false,position:[974,908],from:[974,908],target:[974,908],
  kind:'stay',state_code:5,troops:[{code:23},{code:23},{code:23}],seen_at:1000,observed_at:1000};
const attack={...army,id:'enemy',owner:'enemy',union_id:'1008',player_name:'敌军',relation:'enemy',
  position:null,from:[980,908],target:[974,908],kind:'attack',state_code:1,begin:900,end:1100};
const other={...attack,id:'other',target:[979,909],end:1120};
const unknownTarget={...attack,id:'unlisted',player_name:'未列预警的敌军',end:1150};
const ally={...attack,id:'ally',relation:'ally'};
const expired={...attack,id:'expired',end:999};
const transferring={...attack,id:'transferring',player_name:'调动样本',kind:null,state_code:2,from:[974,908],target:[975,908]};
const returning={...attack,id:'returning',player_name:'返程样本',kind:'retreat',state_code:4,from:[974,908],target:[973,907]};
const alert={army_id:'enemy',player_name:'敌军',from:attack.from,coord:attack.target,end:1100};
const heroes=['武将甲','武将乙','武将丙'].map((name,i)=>({name,id:i+1,position:i+1,red_stars:i,
  level:40,troop_code:23,troop_name:'轻骑兵',skill_slot_ids:[1,2,3],
  skills:[1,2,3].map(id=>({id,name:'战法'+id,level:10})),gear:{name:'不显示的武器',level:10}}));
vm.runInContext(`state.armies=${JSON.stringify([army,attack,other,unknownTarget,ally,expired,transferring,returning])};
  state.alerts=${JSON.stringify([alert,{...alert}, {...alert,army_id:'other',coord:other.target,end:1120}])};
  alertPredictions={enemy:{...${JSON.stringify(attack)},items:[{battle_id:'2',heroes:${JSON.stringify(heroes)}}],
    force_assessment:{kind:'main',battle_id:'2'},current_report_id:'2'},
    unlisted:{...${JSON.stringify(unknownTarget)},items:[],force_assessment:{kind:'spartan'}}};
  placeLoadouts.set(placeLoadoutKey(state.armies[0]),{at:Date.now(),data:{match:'exact_army_id',items:[
    {player_name:'自己',union_id:'1009',event_at:900,battle_id:'1',data:{heroes:${JSON.stringify(heroes)}}}]}});`,context);
const raw=vm.runInContext('JSON.stringify(state.alerts)',context);
const viewport=vm.runInContext('JSON.stringify(map)',context);
assert.equal(vm.runInContext('toggleMapPlaceFocus([974,908],{x:990,y:690})',context),true);
assert.equal(vm.runInContext('alertFocus',context),'974,908');
assert.equal(vm.runInContext('mapPlacePopup.pinned',context),true);
assert.equal(vm.runInContext('JSON.stringify(map)',context),viewport,'Clicking a tile must not change zoom or center');
assert.equal(vm.runInContext('JSON.stringify(state.alerts)',context),raw,'Selecting one target must not remove other attacks');
assert.deepEqual(Array.from(vm.runInContext('mapPlaceIncoming([974,908]).map(a=>a.army_id)',context)),['enemy','unlisted']);
const body=element('mapPlacePopupBody').innerHTML;
assert.ok(body.includes('此处队伍 <span>1')&&body.includes('敌袭队伍 <span>2'));
for(const text of ['关联行军','调动样本','调动中','返程样本','返程中','data-place-details="transferring"','data-place-details="own"'])
  assert.ok(body.includes(text),text);
assert.ok(!body.includes('data-place-details="expired"'),'An expired march must not be listed as a current moving team');
assert.ok(!vm.runInContext('mapPlaceTravelling([974,908]).some(row=>row.id==="enemy")',context),
  'An incoming attacker must not be duplicated in associated marches');
for(const text of ['自己','敌军','武将甲','红度 2红','战法1'])assert.ok(body.includes(text),text);
for(const text of ['不显示的武器','轻骑兵','队 own','队 enemy'])assert.ok(!body.includes(text),text);
assert.ok(parseFloat(element('mapPlacePopup').style.left)<=562&&parseFloat(element('mapPlacePopup').style.top)<=292,'Popup must fit in viewport');
clock+=1000;vm.runInContext('renderMapPlacePopup()',context);
assert.equal(element('mapPlacePopupBody').innerHTML,body,'Timer ticks must not replace the list or close expanded details');
assert.equal(vm.runInContext('toggleMapPlaceFocus([974,908],{x:990,y:690})',context),false);
assert.equal(vm.runInContext('alertFocus',context),'');
assert.equal(element('mapPlacePopup').hidden,true);
assert.equal(vm.runInContext('JSON.stringify(state.alerts)',context),raw);
assert.equal(vm.runInContext('mapPlaceHoverBlocked',context),'974,908');
vm.runInContext("openMapPlacePopup([974,908],{x:200,y:200},false)",context);
assert.equal(vm.runInContext('alertFocus',context),'','Hovering must not select a tile');
vm.runInContext("displayPreferences.showSpartans=false",context);
assert.deepEqual(Array.from(vm.runInContext('mapPlaceIncoming([974,908]).map(a=>a.army_id)',context)),['enemy']);
assert.equal(vm.runInContext('mapPlaceTravelling([974,908]).some(row=>row.id==="unlisted")',context),false,
  'The associated-march section must not reintroduce hidden Spartans');
vm.runInContext("state.live=false",context);
assert.equal(vm.runInContext('mapPlaceIncoming([974,908]).length',context),0);
vm.runInContext("state.live=true;closeMapPlacePopup();",context);
// Escape clears a tile selected from either the map or the game, without clearing observations.
let escapeHandler;
context.document.addEventListener=(name,handler)=>{if(name==='keydown')escapeHandler=handler;};
vm.runInContext(source.slice(source.indexOf("document.addEventListener('keydown'"),
  source.indexOf("window.addEventListener('resize'")),context);
assert.equal(typeof escapeHandler,'function');
vm.runInContext(`function focusTarget(){}
  state.game_focus={event_id:'click-1',coord:[974,908],updated_at:1000,source:'dashen_click'};
  syncGameFocus();campaignFocus='official-battle';selected='enemy';selectedMarch='march';
  openMapPlacePopup([974,908],{x:200,y:200},true);`,context);
const escapeEvent=extra=>({key:'Escape',target:{closest:()=>null},prevented:false,
  preventDefault(){this.prevented=true;},...extra});
for(const extra of [{key:'Enter'},{isComposing:true},{defaultPrevented:true},{ctrlKey:true},{altKey:true},{shiftKey:true},{metaKey:true},
  {target:{isContentEditable:true}},{target:{closest:()=>({tagName:'INPUT'})}}]){
  escapeHandler(escapeEvent(extra));
  assert.equal(vm.runInContext('alertFocus',context),'974,908','Editing and handled keys preserve focus');
}
context.document.activeElement={closest:()=>({tagName:'INPUT'})};
escapeHandler(escapeEvent({target:context.document}));
assert.equal(vm.runInContext('alertFocus',context),'974,908','Desktop forwarding must respect the content input focus');
context.document.activeElement=null;
for(const id of ['settingsDialog','quickGuardDialog']){
  element(id).open=true;const event=escapeEvent();escapeHandler(event);
  assert.equal(vm.runInContext('alertFocus',context),'974,908','Escape in a modal must not cancel the background tile');
  assert.equal(event.prevented,false,'Leave the native dialog Escape action available');element(id).open=false;
}
const event=escapeEvent();escapeHandler(event);
assert.equal(event.prevented,true);
assert.equal(vm.runInContext('alertFocus',context),'');
assert.equal(vm.runInContext('selected',context),null);
assert.equal(vm.runInContext('selectedMarch',context),null);
assert.equal(vm.runInContext('mapPlacePopup',context),null);
assert.equal(vm.runInContext('mapPlaceHoverBlocked',context),'974,908');
assert.equal(vm.runInContext('campaignFocus',context),'official-battle');
assert.equal(new URL(scopedUrl).searchParams.has('target'),false);
assert.equal(new URL(scopedUrl).searchParams.get('campaign'),'official-battle');
assert.deepEqual(JSON.parse(vm.runInContext('JSON.stringify([map.x,map.y,map.scale])',context)),
  [JSON.parse(viewport).x,JSON.parse(viewport).y,JSON.parse(viewport).scale],'Escape must not pan or zoom the map');
assert.equal(vm.runInContext('map.followArea',context),true,'Escape restores following without moving the camera');
assert.equal(vm.runInContext('JSON.stringify(state.alerts)',context),raw,'Escape does not delete other incoming observations');
vm.runInContext('syncGameFocus()',context);
assert.equal(vm.runInContext('alertFocus',context),'','Polling the same game click must not restore the cancelled tile');
vm.runInContext("state.game_focus={...state.game_focus,event_id:'click-2',coord:[975,908]};syncGameFocus()",context);
assert.equal(vm.runInContext('alertFocus',context),'975,908','A new game click still focuses its tile');
vm.runInContext("setMapScope('','');",context);
const noSelection=escapeEvent();escapeHandler(noSelection);
assert.equal(noSelection.prevented,false,'Escape without a tile or popup leaves other keyboard behavior alone');
vm.runInContext(source.slice(source.indexOf("$('mapPlacePopupBody').onclick"),source.indexOf("$('closeAlertPopup').onclick")),context);
vm.runInContext('let openedArmy;selectArmy=id=>{openedArmy=id;}',context);
element('mapPlacePopupBody').onclick({target:{closest:()=>({dataset:{placeDetails:'returning'}})},preventDefault(){}});
assert.equal(vm.runInContext('openedArmy',context),'returning','A non-attacking marching team can open full details');
assert.equal(requests,0,'Shared sidebar cache should avoid extra requests');
vm.runInContext("placeLoadouts.clear();requestMapPlaceLoadout(state.armies[0]);requestMapPlaceLoadout(state.armies[0]);closeMapPlacePopup();",context);
assert.equal(requests,1,'Hover requests must share an in-flight request');
finish({ok:true,json:async()=>({match:'exact_army_id',items:[]})});
setImmediate(()=>{assert.equal(element('mapPlacePopup').hidden,true);console.log('Tile click toggles focus; hover/click popup retains other attacks, deduplicates armies, respects Spartan filtering, shares archive cache and keeps scrolling stable');});
