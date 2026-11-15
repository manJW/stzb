'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const source=fs.readFileSync(require('node:path').join(__dirname,'../static/intelligence.js'),'utf8');
const context={URLSearchParams};vm.createContext(context);
vm.runInContext(source.match(/function groupParams\([\s\S]*?\n\}/)[0],context);
vm.runInContext(source.match(/function timelineDetailHeroes\([\s\S]*?\n\}/)[0],context);
context.panel={archivePlayer:{player_name:'player',union_id:'1',identity:''},archiveQuery:new URLSearchParams()};
context.team={army_id:'',team_ref:'cards:0123456789abcdef01234567'};
assert.equal(vm.runInContext("groupParams(panel,'events',team).get('team')",context),context.team.team_ref);
context.data={player_name:'player',army_id:'9001',known_at_start:{heroes:[{name:'older'}]},reports:[
  {battle_id:'1',event_at:100,side:'attack',sides:[{side:'attack',player_name:'player',army_id:'9001',heroes:[{name:'old'}]}]},
  {battle_id:'2',event_at:200,side:'defend',sides:[
    {side:'attack',player_name:'other',army_id:'9001',heroes:[{name:'wrong opponent'}]},
    {side:'defend',player_name:'player',army_id:null,heroes:[{name:'current'}]}]}]};
assert.equal(vm.runInContext('timelineDetailHeroes(data)[0].name',context),'current');
context.data.reports[1].sides[1].heroes=[];
assert.equal(vm.runInContext('timelineDetailHeroes(data).length',context),0,'never restore an older build when newest report is incomplete');
console.log('Archive groups: report-only team navigation, selected-side roster, latest report and incomplete evidence passed');
