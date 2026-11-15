'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'../static/app.js'),'utf8');
const refresh=source.slice(source.indexOf('async function refresh(){'),source.indexOf('function tick(){'));
const elements=new Map();
let requests=0, finish;
const context=vm.createContext({AbortSignal, Date,
  fetch:async(url,options)=>{
    requests++;
    assert.ok(options.signal instanceof AbortSignal);
    return new Promise(resolve=>{finish=resolve;});
  },
  $:id=>{if(!elements.has(id))elements.set(id,{});return elements.get(id);},
});
vm.runInContext(`let state=null,refreshing=false,apiConnected=false,clockOffset=0,alertFocus='',pendingUiReload=false;
  const map={initialized:true};
  function updateFreshness(){} function renderStatus(){} function renderRoster(){}
  function renderDetails(){} function renderWatch(){} function draw(){}
  function loadAlertPredictions(){} function loadMapCampaigns(){} function fitMap(){}
  function syncGameFocus(){} function syncGameArmyFocus(){} function loadMapTerrain(){} function syncClientRelationColors(){}
  function rememberMapViewport(){} function restoreMapViewport(){} function syncMapViewport(){}
  ${refresh}`,context);
(async()=>{
  const first=vm.runInContext('refresh()',context);
  await vm.runInContext('refresh()',context);
  assert.equal(requests,1,'a slow poll must not overlap a second poll');
  finish({ok:true,json:async()=>({server_now:1000,counts:{fresh:1}})});
  await first;
  assert.equal(vm.runInContext('apiConnected',context),true);
  const second=vm.runInContext('refresh()',context);
  finish({ok:false});
  await second;
  assert.equal(vm.runInContext('refreshing',context),false);
  assert.equal(elements.get('connectionText').textContent,'服务未连接');
  vm.runInContext("function renderWatch(){throw new Error('render failed');}",context);
  const third=vm.runInContext('refresh()',context);
  finish({ok:true,json:async()=>({server_now:1001,counts:{fresh:1}})});
  await assert.rejects(third,/render failed/);
  assert.equal(vm.runInContext('apiConnected',context),true,'rendering errors must not be reported as server outages');
  assert.equal(requests,3);
  let reloads=0;
  context.location={reload:()=>{reloads++;}};
  context.document={activeElement:null};
  vm.runInContext("state.health={instance_id:'old-service'}",context);
  const restarted=vm.runInContext('refresh()',context);
  finish({ok:true,json:async()=>({health:{instance_id:'new-service'},server_now:1002,counts:{fresh:1}})});
  await restarted;
  assert.equal(reloads,1,'A service restart reloads changed UI without asking the user to reopen the client');
  vm.runInContext("pendingUiReload=false;state.health={instance_id:'same-service',ui_revision:'old-ui'};function renderWatch(){}",context);
  elements.set('settingsDialog',{open:true});
  const editing=vm.runInContext('refresh()',context);
  finish({ok:true,json:async()=>({health:{instance_id:'same-service',ui_revision:'new-ui'},server_now:1003,counts:{fresh:1}})});
  await editing;
  assert.equal(reloads,1,'Editing settings must defer a frontend reload');
  elements.get('settingsDialog').open=false;
  const settled=vm.runInContext('refresh()',context);
  finish({ok:true,json:async()=>({health:{instance_id:'same-service',ui_revision:'new-ui'},server_now:1004,counts:{fresh:1}})});
  await settled;
  assert.equal(reloads,2,'A pending UI update loads without restarting collection after the dialog closes');
  console.log('UI polling scenarios passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
