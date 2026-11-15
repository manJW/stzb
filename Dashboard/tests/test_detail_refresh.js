'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'../static/app.js'),'utf8').split("$('settingsButton').onclick")[0];
const elements=new Map();
const element=id=>{if(!elements.has(id))elements.set(id,{innerHTML:'',textContent:'',dataset:{},isConnected:true,
  contains:()=>false,querySelectorAll:()=>[],setAttribute(){},classList:{toggle(){},add(){},remove(){}}});return elements.get(id);};
let clock=1000000,requests=[];
const context=vm.createContext({document:{getElementById:element,querySelector:element,activeElement:{}},
  URL,URLSearchParams,AbortSignal,Date:class extends Date{static now(){return clock;}},
  localStorage:{getItem:()=>null},location:{href:'http://127.0.0.1:8787/battlefield',search:''},
  history:{replaceState(){}},setTimeout(){},clearTimeout(){},
  fetch:(url,options)=>new Promise(resolve=>requests.push({url,options,resolve}))});
vm.runInContext(source,context);
vm.runInContext(`state={config:{self:{user_id:'self'}},health:{instance_id:'service'},alerts:[],armies:[]};
  selected='a'; const testRow={id:'a',owner:'p',player_name:'玩家',union_id:'u',state_code:5,
    signature:'5:0:1',troops:[{code:21}],from:[1,2],target:[1,2]}; state.armies=[testRow];`,context);
const data={match:'name_union_troop_candidate',items:[{player_name:'玩家',union_id:'u',battle_id:'2',event_at:900,
  data:{heroes:[{position:1,name:'新武将',id:1,level:40,red_stars:3,troop_code:21,skills:[]}]}}]};
context.testPanel=element('lineupEvidence');
const panel=context.testPanel;
const flush=async()=>{for(let i=0;i<8;i++)await Promise.resolve();};
(async()=>{
  panel.innerHTML='旧阵容';panel.dataset.checkedAt=String(clock-16000);panel.contains=()=>true;
  vm.runInContext('renderLineupPanel(testRow,testPanel)',context);
  assert.equal(requests.length,1,'Keeping keyboard focus in details must not freeze newer reports');
  assert.equal(requests[0].options?.cache,'no-store');
  assert.ok(requests[0].options?.signal,'Archive details requests must have a bounded timeout');
  requests[0].resolve({ok:true,json:async()=>data});await flush();
  assert.ok(panel.innerHTML.includes('新武将'));
  assert.equal(panel.dataset.loading,'false');

  const stable=panel.innerHTML;
  const old=vm.runInContext('loadLineups(testRow,testPanel)',context);
  vm.runInContext("state.armies=[{...testRow,state_code:4,signature:'4:0:2',troops:[{code:22}]}]",context);
  requests[1].resolve({ok:true,json:async()=>({...data,items:[]})});await old;
  assert.equal(panel.innerHTML,stable,'A late reply for the old march cannot overwrite the current team details');

  const next=vm.runInContext('loadLineups(state.armies[0],testPanel)',context);
  vm.runInContext("state.health.instance_id='replacement-service'",context);
  requests[2].resolve({ok:true,json:async()=>({...data,items:[]})});await next;
  assert.equal(panel.innerHTML,stable,'Responses from the previous service/session must be discarded');

  const failed=vm.runInContext('loadLineups(state.armies[0],testPanel)',context);
  vm.runInContext("state.armies=[{...state.armies[0],signature:'5:0:3',state_code:5}]",context);
  requests[3].resolve({ok:false});await failed;
  assert.equal(panel.innerHTML,stable,'A failed obsolete request cannot clear a newer detail view');

  const busy=vm.runInContext('loadLineups(state.armies[0],testPanel)',context);
  requests[4].resolve({ok:false,status:503});await busy;
  assert.equal(panel.innerHTML,stable,'A busy query must retain the same army\'s last successful lineup');
  assert.ok(panel.dataset.rendered,'Retain the successful markup fingerprint during a transient failure');
  clock+=3100;
  vm.runInContext('renderLineupPanel(state.armies[0],testPanel)',context);
  assert.equal(requests.length,6,'Retry after three seconds instead of waiting a full refresh interval');
  requests[5].resolve({ok:true,json:async()=>({...data,match:'current_report_incomplete',items:[]})});await flush();
  assert.ok(!panel.innerHTML.includes('新武将'),'A successful newer incomplete response must remove the older roster');

  context.verified={...data,match:'same_card_instances',current_report_id:'2'};
  assert.equal(vm.runInContext('placeLoadoutReports({...testRow,troops:[]},verified).length',context),1,
    'A verified current report is also visible in the stationary summary when live troop fields are missing');
  assert.equal(vm.runInContext('placeLoadoutReports({...testRow,troops:[]},{...verified,match:"name_union_candidates"}).length',context),0,
    'Missing live troop fields do not justify borrowing another unverified formation');
  vm.runInContext("state.armies=[testRow];state.health.report_priority={updates:{a:'2:new-config'}}",context);
  panel.dataset.checkedAt=String(clock);
  const count=requests.length;
  vm.runInContext('renderLineupPanel(testRow,testPanel)',context);
  assert.equal(requests.length,count+1,'A completed priority result must bypass the 15-second UI cache');
  const fresh={...data,match:'same_card_instances',current_report_id:'2',
    items:data.items.map(item=>({...item,configuration_hash:'new-config'}))};
  requests[count].resolve({ok:true,json:async()=>fresh});await flush();
  assert.ok(requests[count+1].url.endsWith('/api/intel/priority/displayed'));
  assert.equal(JSON.parse(requests[count+1].options.body).version,'2:new-config');
  context.staleVersion={...fresh,items:fresh.items.map(item=>({...item,configuration_hash:'old-config'}))};
  vm.runInContext('displayedLineups.clear();reportLineupDisplay(testRow,staleVersion)',context);
  assert.equal(requests.length,count+2,'An older configuration must not count as the new displayed lineup');
  console.log('Focused details refresh, bounded requests, late-reply isolation and consistent verified summaries passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
