'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'../static/app.js'),'utf8');
const saved=new Map(),elements=new Map();
const element=id=>{
  if(!elements.has(id))elements.set(id,{textContent:'',innerHTML:'',dataset:{},style:{},
    classList:{add(){},remove(){},toggle(){}},checked:false,setAttribute(){},querySelectorAll:()=>[],
    offsetWidth:180,offsetHeight:50});
  return elements.get(id);
};
const storage={getItem:key=>saved.get(key)||null,setItem:(key,value)=>saved.set(key,value)};
const context=vm.createContext({document:{getElementById:element,querySelector:element,querySelectorAll:()=>[]},
  localStorage:storage,Date:class extends Date{static now(){return 1000000;}},URL,URLSearchParams,
  location:{href:'http://127.0.0.1:8787/battlefield',search:''},history:{replaceState(){}},
  setTimeout(){},clearTimeout(){},window:{innerWidth:1000,innerHeight:700}});
vm.runInContext(source.split("$('settingsButton').onclick")[0],context);
vm.runInContext(`const map={x:985,y:923,scale:39,width:980,height:640,initialized:true,
  points:[],lineupLabels:[],landmarkLabels:[],hover:null};function draw(){}`,context);
const rows=['spartan','main','unknown'].map((kind,index)=>({id:String(index+1),owner:'enemy',
  visible:true,current_connection:true,seen_at:1000,observed_at:1000,stale:false,age:0,
  from:[980,920],target:[986+index,923],begin:900,end:1100,kind:'attack',signature:'1:0:0',
  relation:'enemy',player_name:kind,union_name:'敌盟',target_side:'own',target_ownership_fresh:true,
  troops:index===0?[{code:1},{code:0},{code:0}]:[]}));
const alerts=rows.map(row=>({army_id:row.id,player_name:row.player_name,from:row.from,
  coord:row.target,end:row.end,target_name:'要塞'+row.id}));
const state={server_now:1000,live:true,reliable:true,health:{status:'live',last_packet_at:1000,last_scene_at:1000,messages:3},
  counts:{},targets:[],area:[975,913,995,933],forts:[],land_tiles:[],candidates:[],unions:{},
  armies:rows,alerts,config:{self:{name:'测试',union_name:'本盟'},relations:{enemy:{relation:'enemy'}},
    state_rules:{attack:{verified:true,kind:'attack'}}}};
const predictions=Object.fromEntries(rows.map(row=>[row.id,{...row,items:[],force_assessment:{kind:row.player_name}}]));
vm.runInContext(`state=${JSON.stringify(state)};alertPredictions=${JSON.stringify(predictions)};
  apiConnected=true;alertFocus='986,923';selected='1';selectedMarch=marchKey(state.armies[0]);`,context);
const raw=vm.runInContext('JSON.stringify([state.armies,state.alerts])',context);
vm.runInContext("setDisplayPreference('showSpartans',false)",context);
assert.equal(element('showSpartanAlerts').checked,false);
assert.equal(element('watchBadge').textContent,'0');
assert.equal(element('compactWatchStatus').textContent,'来袭预警');
assert.equal(vm.runInContext('selected',context),null);
assert.equal(vm.runInContext('scopedAlerts().length',context),2);
assert.equal(vm.runInContext('mapArmyEnabled(state.armies[0])',context),false);
assert.equal(vm.runInContext('mapArmyEnabled(state.armies[1])&&mapArmyEnabled(state.armies[2])',context),true);
assert.equal(vm.runInContext('JSON.stringify([state.armies,state.alerts])',context),raw);
assert.ok(!element('watchEntries').innerHTML.includes('>spartan<'));
assert.equal(vm.runInContext('map.scale',context),39);
assert.equal(JSON.parse(saved.get('stzb.battlefield.display.v1')).showSpartans,false);
assert.equal(vm.runInContext('readDisplayPreferences().showSpartans',context),false);
vm.runInContext(`{
  const one={...state.armies[2],id:'4',troops:[{code:23},{code:0},{code:0}]};
  if(mapArmyEnabled(one))throw new Error('地图上已确认的单将斯巴达也应隐藏');
  if(!mapArmyEnabled({...one,troops:[]})||!mapArmyEnabled({...one,relation:'ally'})||
     !mapArmyEnabled({...one,kind:'garrison'}))throw new Error('不能误隐藏未知、本盟或驻守部队');
  for(const tick of mapCoordinateTicks()){
    const actual=unproject(tick.x,tick.y)[tick.axis==='X'?0:1];
    if(Math.abs(actual-tick.value)>1e-8)throw new Error('坐标标尺与地图投影不一致');
  }
  state.game_focus={coord:[985,923],name:'游戏里的要塞名'};
  if(mapPlaceName([985,923])!=='游戏里的要塞名')throw new Error('关注点应使用游戏回传的名称');
}`,context);
// Exercise the actual canvas path: hidden Spartans must produce no arrow.
vm.runInContext(`const canvas={classList:{toggle(){}},getBoundingClientRect:()=>({top:90})};
  const ink=[];let gridStrokes=0;const ctx={save(){},restore(){},beginPath(){},
  moveTo(){},lineTo(){},arc(){},closePath(){},translate(){},rotate(){},scale(){},setLineDash(){},
  stroke(){if(['#c4d1e4','#e3eaf4'].includes(this.strokeStyle))gridStrokes++;},
  clearRect(){},fillRect(){},strokeRect(){},fillText(){},measureText:text=>({width:text.length*6}),
  fill(){ink.push(this.fillStyle);}};`,context);
vm.runInContext(source.slice(source.indexOf('function mapArmyPoint('),source.indexOf('function mapHit(')),context);
vm.runInContext(source.slice(source.indexOf('function mapHit('),source.indexOf('function mapTarget(')),context);
vm.runInContext(source.slice(source.indexOf('function mapTarget('),source.indexOf('function resize(')),context);
vm.runInContext('draw()',context);
assert.equal(vm.runInContext('ink.length',context),2);
vm.runInContext("ink.length=0;setDisplayPreference('showSpartans',true)",context);
assert.equal(vm.runInContext('ink.length',context),3);
assert.equal(element('watchBadge').textContent,'1');
assert.ok(vm.runInContext('gridStrokes>0',context));
const viewport=vm.runInContext('JSON.stringify([map.x,map.y,map.scale])',context);
vm.runInContext("gridStrokes=0;ink.length=0;setDisplayPreference('mapGrid',false)",context);
assert.equal(vm.runInContext('gridStrokes',context),0);
assert.equal(vm.runInContext('ink.length',context),3);
assert.ok(vm.runInContext('map.axisLabels.length>0',context));
assert.equal(vm.runInContext('JSON.stringify([map.x,map.y,map.scale])',context),viewport);
assert.equal(vm.runInContext('JSON.stringify([state.armies,state.alerts])',context),raw);
assert.equal(element('showMapGrid').checked,false);
assert.equal(vm.runInContext('readDisplayPreferences().mapGrid',context),false);
vm.runInContext("gridStrokes=0;setDisplayPreference('mapGrid',true)",context);
assert.ok(vm.runInContext('gridStrokes>0',context));
vm.runInContext(`{
  alertFocus='985,923';
  state.forts=[{coord:[985,923],name:'弋阳要塞',relation:'ally',status:'ready'},
    {coord:[988,923],name:'其他要塞',relation:'enemy',status:'ready'}];
  draw();
  const label=map.landmarkLabels.find(item=>item.point.join(',')===alertFocus);
  if(!label||mapLandmarkHit((label.left+label.right)/2,(label.top+label.bottom)/2)!==label)
    throw new Error('要塞文字标注应能点击到同一个地块');
  if(map.landmarkLabels.length!==1)throw new Error('只有选中的目标应显示固定标签');
  const p=project([988,923]);
  map.hover={x:p.x,y:p.y,anchor:{x:p.x,y:p.y+90}};
  renderMapSkillTooltip(scopedAlerts());
  const panel=$('mapSkillTooltip');
  const placePanel=$('mapPlacePopup'),placeTitle=$('mapPlacePopupTitle').textContent;
  if(panel.hidden&&(placePanel.hidden||!placeTitle.includes('其他要塞')||!placeTitle.includes('988, 923')))
    throw new Error('未选中要塞应在悬停时立即显示名称和坐标；有队伍时应显示队伍弹窗');
  if(canvas.title)throw new Error('不应同时出现重复的浏览器原生提示');
  clearMapHover();
  if(!panel.hidden||map.landmarkLabels.length!==1)throw new Error('移开只隐藏悬停提示，选中标签应保留');
  const chosen=project([985,923]);
  map.hover={x:chosen.x,y:chosen.y,anchor:{x:chosen.x,y:chosen.y+90}};
  renderMapSkillTooltip(scopedAlerts());
  if(!panel.hidden)throw new Error('选中目标已有常驻标签，不应重复弹出悬停提示');
  clearMapHover();
  alertFocus='988,923';draw();
  if(map.landmarkLabels.length!==1||map.landmarkLabels[0].point.join(',')!=='988,923')
    throw new Error('切换选择后应只保留新目标的固定标签');
  alertFocus='985,923';draw();
  // Even densely packed army labels must not erase the selected place label.
  map.lineupLabels=[{left:0,top:0,right:map.width,bottom:map.height}];
  map.landmarkLabels=[];drawLandmarkLabels(scopedAlerts());
  if(map.landmarkLabels.length!==1)throw new Error('标签拥挤时仍应保留选中目标');
}`,context);
vm.runInContext("setDisplayPreference('mapLabels',false)",context);
assert.equal(vm.runInContext('map.landmarkLabels.length',context),1,'Hover preference must not hide the selected label');
vm.runInContext(`{
  const p=project([988,923]);map.hover={x:p.x,y:p.y,anchor:{x:p.x,y:p.y+90}};
  renderMapSkillTooltip(scopedAlerts());
  if(!$('mapSkillTooltip').hidden)throw new Error('关闭悬停信息开关应隐藏其他要塞提示');
  clearMapHover();alertFocus='';draw();
}`,context);
assert.equal(vm.runInContext('map.landmarkLabels.length',context),0);
storage.getItem=()=>'{bad json';
assert.equal(vm.runInContext('readDisplayPreferences().showSpartans',context),true);
storage.getItem=()=>JSON.stringify({showSpartans:false,mapLabels:false});
assert.equal(vm.runInContext('readDisplayPreferences().mapGrid',context),true);
assert.equal(vm.runInContext('readDisplayPreferences().showSpartans',context),false);
const html=fs.readFileSync(path.join(__dirname,'../static/index.html'),'utf8');
assert.ok(html.includes('<details class="settings-advanced">'));
for(const id of ['showSpartanAlerts','showMapLabels','showMapGrid'])assert.equal((html.match(new RegExp(`id="${id}"`,'g'))||[]).length,1);
console.log('Display preferences, arrows, selected persistent labels and hover-only fort tooltips passed');
