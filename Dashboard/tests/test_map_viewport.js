'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'../static/app.js'),'utf8'),elements=new Map(),saved=new Map(),events=new Map();
const element=id=>{if(!elements.has(id))elements.set(id,{dataset:{},textContent:'',innerHTML:'',style:{},
  classList:{toggle(){},add(){},remove(){}},querySelectorAll:()=>[],setAttribute(){}});return elements.get(id);};
const context=vm.createContext({document:{getElementById:element,querySelector:element,
  documentElement:{classList:{contains:()=>false}}},URL,URLSearchParams,AbortSignal,
  Date:class extends Date{static now(){return 1000000;}},
  location:{href:'http://127.0.0.1:8787/battlefield',search:''},history:{replaceState(){}},
  localStorage:{getItem:()=>null},sessionStorage:{getItem:k=>saved.get(k),setItem:(k,v)=>saved.set(k,v),removeItem:k=>saved.delete(k)},
  window:{addEventListener:(key,fn)=>events.set(key,fn)},setTimeout(){},clearTimeout(){}});
vm.runInContext(source.split("$('settingsButton').onclick")[0],context);
vm.runInContext(`const map={x:969,y:904,scale:31,width:900,height:650,initialized:true,followArea:true,drag:null};
  const canvas={setPointerCapture(){},addEventListener(){}};
  function draw(){} function renderMapSkillTooltip(){} function closeMapPlacePopup(){} function clearMapHover(){}
  state={live:true,reliable:true,area:[729,889,748,908],health:{resync_required:false},
    config:{self:{user_id:'12319'}},game_focus:{event_id:'old-click',updated_at:900,coord:[969,904]},map_config_id:6};
  alertFocus='969,904';`,context);
vm.runInContext(source.slice(source.indexOf('function fitMap(){'),source.indexOf('function mapArmyPoint(')),context);
vm.runInContext('syncMapViewport()',context);
assert.equal(vm.runInContext('map.x',context),738.5);
assert.equal(vm.runInContext('map.y',context),898.5);
assert.equal(vm.runInContext('map.scale',context),31,'Automatic recovery preserves zoom');
assert.equal(vm.runInContext('alertFocus',context),'969,904','Recovery does not delete a pinned target');
vm.runInContext('state.area=[730,889,749,908];syncMapViewport()',context);
assert.equal(vm.runInContext('map.x',context),739.5,'A one-tile client pan must follow even while the two views overlap');
assert.equal(vm.runInContext('map.scale',context),31,'Client following must never zoom');
vm.runInContext('map.x=969;map.y=904;map.followArea=false;syncMapViewport()',context);
assert.equal(vm.runInContext('map.x',context),969,'Intentional map panning is respected');
vm.runInContext('map.followArea=true;state.health.resync_required=true;syncMapViewport()',context);
assert.equal(vm.runInContext('map.x',context),969,'A partial reconnect cannot move the camera using old area data');
vm.runInContext('state.health.resync_required=false;state.game_focus.updated_at=999;syncMapViewport()',context);
assert.equal(vm.runInContext('map.x',context),969,'Give the game click its map subscription response first');
vm.runInContext('state.game_focus.updated_at=900;state.area=[731,889,750,908];syncMapViewport()',context);
assert.equal(vm.runInContext('map.x',context),740.5,'Moving the client resumes following after a manual dashboard pan');
vm.runInContext('map.drag={};state.area=[732,889,751,908];syncMapViewport()',context);
assert.equal(vm.runInContext('map.x',context),740.5,'Do not interrupt an active dashboard drag');
vm.runInContext('map.drag=null;syncMapViewport()',context);
assert.equal(vm.runInContext('map.x',context),741.5,'Apply the deferred game view after dragging ends');
vm.runInContext('map.x=969;map.y=904;state.game_focus.updated_at=999;state.area=[733,889,752,908];syncMapViewport()',context);
assert.equal(vm.runInContext('map.x',context),742.5,'A later client pan must not be suppressed by a recent earlier click');
vm.runInContext('map.gameClickCentered=true;map.x=969;map.y=904;state.area=[734,889,753,908];syncMapViewport()',context);
assert.equal(vm.runInContext('map.x',context),969,'The same update that selects a game tile retains exact tile centering');
vm.runInContext('state.game_focus.updated_at=900',context);
vm.runInContext('state.area=[729,889,748,908];syncMapViewport()',context);
vm.runInContext('state.game_focus.updated_at=900;fitMap()',context);
assert.equal(vm.runInContext('map.x',context),738.5,'Fit-view must not return to an old target outside the current game area');
vm.runInContext('map.x=739;map.y=900;map.scale=29;rememberMapViewport();map.initialized=false;restoreMapViewport()',context);
assert.deepEqual(JSON.parse(vm.runInContext('JSON.stringify([map.x,map.y,map.scale])',context)),[739,900,29]);
assert.equal(vm.runInContext('gameFocusEventId',context),'old-click','Old game selection cannot undo a restored camera');
vm.runInContext('rememberMapViewport();map.initialized=false;state.area=[730,889,749,908];restoreMapViewport()',context);
assert.equal(vm.runInContext('map.x',context),739.5,'Reloading after an overlapping client pan uses the new game view');
assert.equal(vm.runInContext('map.scale',context),29,'Reloading preserves zoom');
vm.runInContext(`rememberMapViewport();{
  const previous=JSON.parse(sessionStorage.getItem('stzb.map.reload.v1'));
  delete previous.followVersion;previous.x=970;previous.y=905;
  sessionStorage.setItem('stzb.map.reload.v1',JSON.stringify(previous));
  map.initialized=false;restoreMapViewport();}`,context);
assert.equal(vm.runInContext('map.x',context),739.5,'Migrate the old sticky viewport to the current client area');
assert.equal(vm.runInContext('map.scale',context),29,'Migration also preserves zoom');
vm.runInContext('rememberMapViewport();map.initialized=false;state.config.self.user_id="other";restoreMapViewport()',context);
assert.equal(vm.runInContext('map.initialized',context),false,'A different role must not inherit the viewport');
vm.runInContext('state.config.self.user_id="12319";map.initialized=true;map.scale=29',context);
vm.runInContext(source.slice(source.indexOf('canvas.onpointerdown='),source.indexOf('canvas.addEventListener(\'wheel\'')),context);
vm.runInContext('canvas.onpointerdown({offsetX:100,offsetY:100,pointerId:1});',context);
assert.equal(vm.runInContext('Boolean(map.drag)',context),true);
events.get('blur')();
assert.equal(vm.runInContext('map.drag',context),null,'Window switching cancels captured dragging');
vm.runInContext('map.drag={x:100,y:100,anchor:[1,1]};canvas.onpointermove({buttons:0,offsetX:300,offsetY:200,pointerType:"mouse"})',context);
assert.equal(vm.runInContext('map.drag',context),null,'A released button must not keep dragging from an old world coordinate');
console.log('Map recovery: stale-area blank view, preserved zoom, manual pan, partial snapshots, reload state and lost dragging passed');
