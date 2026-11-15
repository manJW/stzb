'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'../static/app.js'),'utf8').split("$('settingsButton').onclick")[0];
const elements=new Map(),requests=[];
const element=id=>{if(!elements.has(id))elements.set(id,{innerHTML:'',dataset:{},isConnected:true,
  classList:{toggle(){}},setAttribute(){},querySelectorAll:()=>[]});return elements.get(id);};
const context=vm.createContext({document:{getElementById:element,querySelector:element},URL,URLSearchParams,AbortSignal,
  Date:class extends Date{static now(){return 1000000;}},location:{href:'http://127.0.0.1:8787/battlefield',search:''},
  history:{replaceState(){}},localStorage:{getItem:()=>null},setTimeout(){},clearTimeout(){},
  fetch:(url)=>new Promise(resolve=>requests.push({url,resolve}))});
vm.runInContext(source,context);
const row={owner:'p',player_name:'同玩家',union_id:'1008',army_group_id:113310331,relation:'enemy',
  signature:'3:0:1',state_code:3,begin:900,end:1100,from:[1133,993],target:[1137,977],troops:[{code:21}]};
context.rows=[{...row,id:'A'},{...row,id:'B'},{...row,id:'C'}];
vm.runInContext(`state={live:true,health:{instance_id:'service'},config:{self:{user_id:'me'}},alerts:[],armies:rows,
 army_groups:[{id:'113310331',member_ids:['A','B','C'],from:[1133,993],target:[1137,977],begin:900,end:1100}]};
 selectedGroup='113310331';renderDetails();`,context);
assert.equal(requests.length,3);
assert.equal(new Set(requests.map(r=>new URL('http://local'+r.url).searchParams.get('army_id'))).size,3);
const response=(name,bid,match='exact_army_id')=>({match,current_report_id:bid,items:[{
 player_name:'同玩家',union_id:'1008',battle_id:bid,event_at:999,data:{heroes:[{id:1,position:1,name,level:40,
 red_stars:3,troop_code:21,remaining_hp:100,wounded_hp:0,skills:[]}],army_group_name:'襄城军团一',report_army_group_id:'113310331'}}]});
(async()=>{
 requests[0].resolve({ok:true,json:async()=>response('A队武将','1')});
 requests[1].resolve({ok:true,json:async()=>response('B队武将','2')});
 requests[2].resolve({ok:true,json:async()=>response('不可借用','3','name_union_troop_candidate')});
 for(let i=0;i<12;i++)await Promise.resolve();
 assert.ok(element('groupLineup-A').innerHTML.includes('A队武将'));
 assert.ok(!element('groupLineup-A').innerHTML.includes('B队武将'));
 assert.ok(element('groupLineup-B').innerHTML.includes('B队武将'));
 assert.ok(!element('groupLineup-C').innerHTML.includes('不可借用'));
 assert.ok(element('groupLineup-C').innerHTML.includes('未核实'));
 assert.ok(!element('groupLineup-A').innerHTML.includes(' open'), 'Tactics default to collapsed for each member');
 vm.runInContext('renderDetails()',context);assert.equal(requests.length,3,'Stable group refresh shares per-army cache');
 assert.ok(element('armyDetails').innerHTML.includes('襄城军团一'),'Known legion appears only as a badge on each personal team');
 assert.ok(!element('armyDetails').innerHTML.includes('selectedGroupTitle'),'No separate legion panel/header');
 assert.equal((element('armyDetails').innerHTML.match(/class="selected-army-card/g)||[]).length,3);
 console.log('Army-group members: distinct IDs, independent same-player lineups, no candidate substitution and collapsed tactics passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
