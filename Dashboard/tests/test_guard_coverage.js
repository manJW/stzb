'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const elements=new Map(),saved=new Map();let clock=1000000;
function element(id){if(!elements.has(id))elements.set(id,{hidden:false,textContent:'',innerHTML:'',dataset:{},style:{},
  classList:{toggle(){},add(){},remove(){}},querySelectorAll:()=>[],setAttribute(){}});return elements.get(id);}
const context=vm.createContext({document:{getElementById:element,querySelector:element},
  Date:class extends Date{static now(){return clock;}},URLSearchParams,URL,
  localStorage:{getItem:key=>saved.get(key)||null,setItem:(key,value)=>saved.set(key,value)},
  location:{search:'',href:'http://127.0.0.1:8787/battlefield'},history:{replaceState(){}},setTimeout(){},clearTimeout(){}});
const source=fs.readFileSync(path.join(__dirname,'../static/app.js'),'utf8');
vm.runInContext(source.split("$('settingsButton').onclick")[0],context);
const base={id:'100',owner:'enemy1',player_name:'敌方甲',union_id:'88',union_name:'敌盟',relation:'enemy',
  state_code:6,kind:'garrison',begin:800,end:900,reside_time:999,position:[10,10],from:[10,10],target:[10,10],
  visible:true,current_connection:true,stale:false};
function reset(rows=[base],tiles=[]){vm.runInContext(`state=${JSON.stringify({live:true,reliable:true,area:[1,1,20,20],
  health:{resync_required:false},counts:{fresh:rows.length},config:{self:{user_id:'self'}},armies:rows,forts:[],land_tiles:tiles,alerts:[]})};
  mapTerrain.data=null;alertFocus='11,11';`,context);}
reset();
assert.equal(vm.runInContext('guardCoverageIndex().size',context),9);
for(const p of [[9,9],[11,11],[10,10]])assert.equal(vm.runInContext(`guardCoverageIndex().get('${p}')?.entries.length`,context),1);
assert.equal(vm.runInContext("guardCoverageIndex().has('12,10')",context),false);
assert.equal(vm.runInContext("guardEncounter([10,10]).confirmed.length",context),1);
assert.equal(vm.runInContext("guardEncounter([11,11]).confirmed.length",context),0,'An unknown tile must not be asserted as defended');
assert.equal(vm.runInContext("guardEncounter([11,11]).unverified.length",context),1);
const tile=(p,owner='enemy2',union='88')=>({coord:p,owner_id:owner,union_id:union,relation:'enemy'});
reset([base],[tile([11,11]),tile([9,9],'other','99'),tile([9,10],'0','0')]);
assert.equal(vm.runInContext('guardEncounter([11,11]).confirmed.length',context),1,'Diagonal same-alliance tile is covered');
assert.equal(vm.runInContext('guardEncounter([9,9]).unverified.length',context),0,'A different enemy alliance is not protected merely because it is also red');
assert.equal(vm.runInContext('guardCoverageIndex().has("9,10")',context),false,'Neutral land is not protected');
reset([base,{...base,id:'200',owner:'enemy2',from:[12,11],target:[12,11],position:[12,11],end:950,reside_time:1},base],
  [tile([11,11])]);
assert.equal(vm.runInContext('guardEncounter([11,11]).confirmed.length',context),2,'One army must appear once when coverage overlaps');
assert.deepEqual([...vm.runInContext('guardEncounter([11,11]).confirmed.map(e=>e.row.id)',context)],['200','100'],
  'Client list uses guard EndTime descending, not fort ResideTime');
assert.equal(vm.runInContext("placeArmyGroups().has('11,11')",context),false,'Radiated defenders must not become physical occupants');
reset([base,...[3,4,5,7,25].map(n=>({...base,id:String(n),state_code:n,kind:null})),
  {...base,id:'hidden',visible:false},{...base,id:'old',stale:true},{...base,id:'disconnected',current_connection:false}]);
assert.equal(vm.runInContext('guardEncounter([10,10]).confirmed.length',context),1,'Only active observed guards belong in coverage');
reset([{...base,relation:'unknown'}]);
assert.equal(vm.runInContext('guardEncounter([10,10]).confirmed.length',context),1,'Unknown diplomacy must not hide a directly observed guard');
reset([{...base,position:[21,10],from:[21,10],target:[21,10]}],[tile([20,10])]);
assert.equal(vm.runInContext('guardEncounter([20,10]).confirmed.length',context),1,'A guard just outside the viewport can radiate onto its edge');
assert.equal(vm.runInContext('guardCoverageIndex().has("21,10")',context),false,'Do not render cells outside the subscribed area');
assert.equal(vm.runInContext('placeArmyGroups().size',context),0,'Coverage halo must not expand the physical unit list');
vm.runInContext('state.live=false',context);
assert.equal(vm.runInContext('guardCoverageIndex().size',context),0);
reset();vm.runInContext('state.health.resync_required=true',context);
assert.equal(vm.runInContext('guardCoverageIndex().size',context),0);
reset();vm.runInContext('state.area=[10,10,20,20]',context);
assert.equal(vm.runInContext('guardCoverageIndex().size',context),4,'Clip coverage to the currently subscribed battlefield');
reset([base],[tile([11,11])]);
vm.runInContext('mapTerrain.size=20;mapTerrain.data=new Uint8Array(400);mapTerrain.data[10*20+10]=1',context);
assert.equal(vm.runInContext('guardCoverageIndex().has("11,11")',context),false,'Ordinary land guards do not defend river tiles');
reset([base],[tile([11,11])]);
let reads=0;context.countRead=()=>reads++;
vm.runInContext('requestMapPlaceLoadout=()=>countRead();renderGuardCoverage()',context);
assert.ok(element('guardCoverageCount').textContent.includes('本格 0 · 周边 1'));
assert.ok(element('guardCoverageList').innerHTML.includes('辐射自 10, 10'));
assert.ok(element('guardCoverageNote').textContent.includes('待实战核对'));
assert.equal(reads,1);
let fills=0,strokes=0;context.ink=()=>fills++;context.outline=()=>strokes++;
vm.runInContext(`const map={x:10,y:10,scale:30,width:600,height:400};
 const ctx={save(){},restore(){},beginPath(){},closePath(){},moveTo(){},lineTo(){},setLineDash(){},fill(){ink();},stroke(){outline();}}`,context);
vm.runInContext(source.slice(source.indexOf('function drawGuardCoverage('),source.indexOf('function draw(){')),context);
vm.runInContext('drawGuardCoverage(null)',context);assert.ok(fills>0&&strokes>0);
const before=fills;vm.runInContext('displayPreferences.guardCoverage=false;drawGuardCoverage(null)',context);
assert.equal(fills,before,'Rendering switch must suppress the entire coverage layer');
assert.equal(vm.runInContext('guardEncounter([11,11]).confirmed.length',context),1,'Rendering switch must not suppress collected defenders');
vm.runInContext('renderWatch=()=>{};renderDetails=()=>{};draw=()=>{};setDisplayPreference("guardCoverage",false)',context);
assert.equal(JSON.parse(saved.values().next().value).guardCoverage,false);
assert.equal(vm.runInContext('readDisplayPreferences().guardCoverage',context),false);
// A direct guard belongs in the encounter list once, while other stationary teams remain visible.
const troops=[21,23,32].map(code=>({code}));
reset([{...base,troops},{...base,id:'200',end:950,troops},
  {...base,id:'300',state_code:5,kind:'stay',troops}]);
vm.runInContext("alertFocus='10,10'",context);
const report={match:'exact_army_id',items:[{player_name:base.player_name,union_id:base.union_id,event_at:900,
  data:{heroes:troops.map((t,i)=>({name:['甲','乙','丙'][i],position:i+1,troop_code:t.code,red_stars:i,
    remaining_hp:[5000,0,6000][i],wounded_hp:[0,2000,0][i],skills:[]}))}}]};
context.report=report;
vm.runInContext('for(const row of state.armies)placeLoadouts.set(placeLoadoutKey(row),{at:Date.now(),data:report});renderGuardCoverage();renderPlaceArmies()',context);
const guardMarkup=element('guardCoverageList').innerHTML,otherMarkup=element('placeArmiesList').innerHTML;
for(const id of ['100','200']){
  assert.equal((guardMarkup.match(new RegExp(`data-guard-army="${id}"`,'g'))||[]).length,1);
  assert.ok(!otherMarkup.includes(`data-place-army="${id}"`),'A guard must not be displayed twice in the sidebar');
}
assert.ok(otherMarkup.includes('data-place-army="300"'),'Another army from the same player must remain visible');
assert.equal(element('placeArmies').hidden,false);
assert.ok(element('placeArmiesSummary').textContent.includes('要塞内/停留 1'));
assert.ok(guardMarkup.includes('guard-report-meta')&&guardMarkup.includes('最近战报')&&guardMarkup.includes('战后余兵 13,000'),
  'The retained guard card must keep the removed duplicate card\'s latest report and remaining HP');
assert.equal(vm.runInContext("placeArmyGroups().get('10,10').rows.length",context),3,
  'Sidebar deduplication must not alter physical occupancy or map counts');
vm.runInContext('state={...state,armies:state.armies.map(r=>({...r,state_code:6,kind:"garrison"}))};renderGuardCoverage();renderPlaceArmies()',context);
assert.equal(element('placeArmies').hidden,true,'Hide the empty second section when all physical teams are active guards');
assert.ok(!element('placeArmiesList').innerHTML.includes('data-place-army='));
assert.equal((element('guardCoverageList').innerHTML.match(/data-guard-army=/g)||[]).length,3);
vm.runInContext('state={...state,armies:state.armies.map(r=>r.id==="100"?{...r,state_code:25,kind:"stay"}:r)};renderGuardCoverage();renderPlaceArmies()',context);
assert.equal(element('placeArmies').hidden,false,'A team leaving garrison state must return to the stationary team section');
assert.ok(element('placeArmiesList').innerHTML.includes('data-place-army="100"'));
assert.ok(!element('guardCoverageList').innerHTML.includes('data-guard-army="100"'));
console.log('Nine-cell coverage, cross-tile ownership, arrival order, deduplication, stale suppression, terrain, clipping and saved display switch passed');
