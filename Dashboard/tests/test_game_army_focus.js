'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'../static/app.js'),'utf8').split("$('settingsButton').onclick")[0];
const context=vm.createContext({document:{getElementById:()=>({})},URL,URLSearchParams,Date:class extends Date{static now(){return 1000000;}},
  location:{href:'http://127.0.0.1:8787/battlefield',search:''},history:{replaceState(){}},localStorage:{getItem:()=>null}});
vm.runInContext(source,context);
vm.runInContext(`state={armies:[{id:'a',visible:true,current_connection:true,stale:false}],
 game_army_focus:{event_id:'1',army_id:'a',source:'client_selected_army',selected_at:1000}};
 const map={};const selections=[];selectArmy=id=>{selected=id;selections.push(id);};clockOffset=0;`,context);
vm.runInContext('syncGameArmyFocus();syncGameArmyFocus()',context);
assert.equal(vm.runInContext('selected',context),'a');
assert.equal(vm.runInContext('selections.length',context),1,'The same game event cannot repeatedly steal the inspector');
vm.runInContext(`state.game_army_focus={...state.game_army_focus,event_id:'2',army_id:'not-yet-seen'};syncGameArmyFocus();`,context);
assert.equal(vm.runInContext('selected',context),'a');
vm.runInContext("state.armies.push({id:'not-yet-seen',visible:true,stale:false});syncGameArmyFocus()",context);
assert.equal(vm.runInContext('selected',context),'not-yet-seen');
vm.runInContext(`state.game_army_focus={...state.game_army_focus,event_id:'3',army_id:null};syncGameArmyFocus();`,context);
assert.equal(vm.runInContext('selected',context),null);
vm.runInContext(`selected='manually-selected';state.game_army_focus.event_id='4';syncGameArmyFocus()`,context);
assert.equal(vm.runInContext('selected',context),'manually-selected','A cancellation cannot close a manually selected different army');
vm.runInContext(`state.game_army_focus={...state.game_army_focus,event_id:'5',army_id:'a',selected_at:990};syncGameArmyFocus()`,context);
assert.equal(vm.runInContext('selected',context),'manually-selected','An expired game selection must not steal focus');
vm.runInContext(`state.game_army_focus={...state.game_army_focus,event_id:'6',selected_at:1000};
 state.game_focus={message_at:1001};syncGameArmyFocus()`,context);
assert.equal(vm.runInContext('selected',context),'manually-selected','A newer ground click wins over a delayed army event');
vm.runInContext(`const tileChanges=[];focusWatchTarget=point=>tileChanges.push(point);
 state.game_focus={event_id:'same-army-click-tile',source:'dashen_click',coord:[986,923],clicked_at:1000.1,updated_at:1000.2};
 syncGameFocus()`,context);
assert.equal(vm.runInContext('tileChanges.length',context),0,'Tile traffic caused by the same army click cannot replace its details');
vm.runInContext(`clockOffset=2;state.game_focus={...state.game_focus,event_id:'later-ground-click',clicked_at:1001};syncGameFocus()`,context);
assert.equal(vm.runInContext('tileChanges.length',context),1,'An actual later ground click still works');
vm.runInContext('clockOffset=0',context);
assert.equal(vm.runInContext('observedArmyLabel({state_code:7,target_type:13})',context),'战斗中');
assert.equal(vm.runInContext('placeArmyStateText({state_code:7,target_type:13,end:1122})',context),'战斗中 00:02:02');
assert.equal(vm.runInContext('placeArmyStateText({state_code:7,target_type:13,end:999})',context),'战斗中',
  'Timer expiry cannot invent a return or garrison state');
console.log('Exact game-army selection, deduplication, pending rows, cancel and stale-event isolation passed');
