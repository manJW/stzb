'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'../static/app.js'),'utf8').split("$('settingsButton').onclick")[0];
const elements=new Map(),requests=[];
const element=id=>{
  if(!elements.has(id))elements.set(id,{innerHTML:'',textContent:'',dataset:{},isConnected:true,hidden:false,
    contains:()=>false,querySelectorAll:()=>[],setAttribute(){},classList:{toggle(){},add(){},remove(){}}});
  return elements.get(id);
};
const context=vm.createContext({document:{documentElement:{classList:{contains:name=>name==='desktop-compact'}},
  getElementById:element,querySelector:element,activeElement:{}},URL,URLSearchParams,AbortSignal,
  Date:class extends Date{static now(){return 1000000;}},location:{href:'http://127.0.0.1:8787/battlefield',search:''},
  localStorage:{getItem:()=>null},history:{replaceState(){}},setTimeout(){},clearTimeout(){},
  fetch:(url,options)=>new Promise(resolve=>requests.push({url,options,resolve}))});
vm.runInContext(source,context);
const row={id:'a',owner:'p',player_name:'同一个玩家',union_id:'u',relation:'enemy',state_code:4,
  signature:'4:0:1',from:[1137,980],target:[1145,974],display_position:[1137,980],
  from_name:'襄城要塞',target_name:'曲阳要塞',begin:900,end:1100,troops:[{code:23},{code:172},{code:43}],
  countdown_label:'行军剩余',stale:false};
const heroes=['荀彧','司马懿','曹操'].map((name,i)=>({position:i+1,id:i+1,name,level:42,red_stars:[5,0,1][i],
  troop_code:[23,172,43][i],troop_name:'不展示兵种',skill_slot_ids:[1,2,3],
  remaining_hp:8000,wounded_hp:100,skills:[{id:1,name:'驱虎吞狼',level:10},{id:2,name:'战法二',level:8},{id:3,name:'战法三',level:9}]}));
const item={player_name:row.player_name,union_id:row.union_id,battle_id:'2',event_at:950,data:{heroes}};
const data={match:'same_card_instances',current_report_id:'2',items:[item]};
context.responseData=data;
vm.runInContext(`state={config:{self:{user_id:'self'}},health:{instance_id:'service'},live:true,alerts:[],
  armies:[${JSON.stringify(row)},{...${JSON.stringify(row)},id:'b'}]};selected='a';clockOffset=0;
  closeMapPlacePopup=()=>{};dismissAlertPopup=()=>{};renderRoster=()=>{};draw=()=>{};setInspectorView=()=>{};`,context);
const flush=async()=>{for(let i=0;i<8;i++)await Promise.resolve();};
(async()=>{
  vm.runInContext('renderDetails()',context);
  const shell=element('armyDetails').innerHTML;
  assert.ok(shell.includes('selected-army-card')&&shell.includes(row.player_name));
  assert.equal((shell.match(/selected-route-row/g)||[]).length,2);
  assert.ok(shell.includes('襄城要塞（1137, 980）')&&shell.includes('曲阳要塞（1145, 974）'));
  assert.ok(shell.includes('返程中')&&shell.includes('detailCountdown'));
  for(const unwanted of ['detailTroops','defenseEvidence','校准与依据','部队 a'])assert.ok(!shell.includes(unwanted));
  requests[0].resolve({ok:true,json:async()=>data});await flush();
  const panel=element('lineupEvidence'),markup=panel.innerHTML;
  assert.ok(markup.includes('42级')&&markup.includes('5红')&&markup.includes('0红')&&markup.includes('1红'));
  assert.ok(markup.includes('最近战报')&&markup.includes('战后余兵 24,300'));
  assert.ok(markup.includes('data-selected-tactics="a"')&&!/data-selected-tactics="a"\s+open/.test(markup),
    'Tactics are closed for a newly selected army');
  assert.ok(markup.includes('驱虎吞狼')&&!markup.includes('不展示兵种'));
  for(const unwanted of ['同队号战报','其他历史','同一组三张','已关联的战报阵容'])assert.ok(!markup.includes(unwanted));

  context.newData={...data,current_report_id:'3',items:[{...item,battle_id:'3',event_at:990,
    data:{heroes:heroes.map((h,i)=>i? h:{...h,name:'换阵新武将',red_stars:4})}}]};
  vm.runInContext('selectedTacticsOpen=true',context);
  const update=vm.runInContext('loadLineups(state.armies[0],document.getElementById("lineupEvidence"))',context);
  requests[1].resolve({ok:true,json:async()=>context.newData});await update;
  assert.ok(panel.innerHTML.includes('换阵新武将')&&panel.innerHTML.includes('4红')&&!panel.innerHTML.includes('荀彧'));
  assert.ok(/data-selected-tactics="a"\s+open/.test(panel.innerHTML),'An explicit expansion survives same-army report updates');

  const late=vm.runInContext('loadLineups(state.armies[0],document.getElementById("lineupEvidence"))',context);
  // A new detail shell replaces its child panel in a real DOM.
  panel.isConnected=false;elements.delete('lineupEvidence');
  vm.runInContext("selectArmy('b')",context);
  const otherPanel=element('lineupEvidence');
  assert.equal(vm.runInContext('selectedTacticsOpen',context),false,'Another army starts with tactics collapsed');
  const before=otherPanel.innerHTML;
  requests[2].resolve({ok:true,json:async()=>data});await late;
  assert.equal(otherPanel.innerHTML,before,'An old reply cannot replace the other army of the same player');
  requests[3].resolve({ok:true,json:async()=>({...data,match:'name_union_troop_candidate'})});await flush();
  assert.ok(!otherPanel.innerHTML.includes('荀彧')&&otherPanel.innerHTML.includes('未核实'),
    'Matching player and troop types alone cannot supply another army roster');
  vm.runInContext('state.armies[1].state_code=7;state.armies[1].target_type=13;state.armies[1].signature="7:13:2";renderDetails()',context);
  assert.ok(element('armyDetails').innerHTML.includes('战斗中'),'Compact state updates with the actual army');

  context.prediction={army_id:'a',current_report_id:'2',current_resolution:{anchor_battle_id:'1',battle_id:'2'},items:[{battle_id:'2',event_at:950,heroes}]};
  const predicted=vm.runInContext('compactForecastLoadout(state.armies[0],prediction)',context);
  assert.ok(predicted.includes('荀彧'));
  assert.ok(!vm.runInContext('compactForecastLoadout(state.armies[1],prediction)',context).includes('荀彧'),
    'A different army ID cannot use the same-player forecast');
  assert.ok(!vm.runInContext('compactForecastLoadout(state.armies[0],{...prediction,current_resolution:{unresolved:"missing"}})',context).includes('荀彧'));
  assert.ok(!vm.runInContext('compactForecastLoadout(state.armies[0],{...prediction,current_resolution:null,same_army_report_id:"old-army-report"})',context).includes('荀彧'),
    'An old army reference cannot verify a different current player-wide guess');
  console.log('Compact selected army: route/time, single verified lineup, collapsed tactics, latest refresh and same-player switch isolation passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
