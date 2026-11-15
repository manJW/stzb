'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const context=vm.createContext({URLSearchParams,Date,location:{search:''},document:{getElementById:()=>({}),querySelector:()=>({})}});
vm.runInContext(fs.readFileSync(path.join(__dirname,'../static/app.js'),'utf8').split("$('settingsButton').onclick")[0],context);
const heroes=[{position:1,name:'张机',remaining_hp:3290,wounded_hp:3364},
  {position:2,name:'袁术',remaining_hp:2699,wounded_hp:3098},{position:3,name:'典韦',remaining_hp:4861,wounded_hp:1667}];
const original=JSON.stringify(heroes);
context.heroes=heroes;
assert.equal(vm.runInContext('postBattleTroops(heroes)',context),18979);
assert.equal(vm.runInContext('postBattleTroopsText(heroes)',context),'战后余兵 18,979');
assert.ok(vm.runInContext('lineupOverview(heroes,1000).meta',context).endsWith('战后余兵 18,979'));
assert.ok(vm.runInContext('mapLineupText("玩家甲","main",{heroes,event_at:1000})[2]',context).endsWith('战后余兵 18,979'));
for(const rows of [[],null,[{remaining_hp:100}], [{remaining_hp:100,wounded_hp:null}],
  [{remaining_hp:null,wounded_hp:100}], [{remaining_hp:100,wounded_hp:-1}],
  [{remaining_hp:100,wounded_hp:'20'}],[{remaining_hp:100,wounded_hp:0.5}],
  [...heroes,{remaining_hp:100,wounded_hp:undefined}]]){
  context.rows=rows;
  assert.equal(vm.runInContext('postBattleTroops(rows)',context),null,'Incomplete or invalid evidence cannot become a numeric total');
}
context.rows=[{remaining_hp:0,wounded_hp:100},{remaining_hp:0,wounded_hp:200}];
assert.equal(vm.runInContext('postBattleTroops(rows)',context),300,'Zero active troops can still leave wounded survivors');
assert.equal(vm.runInContext('postBattleTroops([{remaining_hp:0,wounded_hp:0}])',context),0);
assert.equal(JSON.stringify(heroes),original,'Display totals must not modify stored components');
console.log('Survivors plus wounded are shared by sidebar and map; missing wounds stay unknown, zero survivors retain wounded, and raw components stay separate');
