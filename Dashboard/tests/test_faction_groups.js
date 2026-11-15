'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'../static/app.js'),'utf8'),elements=new Map();
const element=id=>{
  if(!elements.has(id))elements.set(id,{innerHTML:'',textContent:'',value:'',dataset:{},style:{},isConnected:true,
    classList:{toggle(){},add(){},remove(){}},querySelectorAll:()=>[],setAttribute(){}});
  return elements.get(id);
};
const context=vm.createContext({document:{getElementById:element,querySelector:element,
  documentElement:{classList:{contains:()=>true}}},URL,URLSearchParams,AbortSignal,
  Date:class extends Date{static now(){return 1000000;}},localStorage:{getItem:()=>null},
  history:{replaceState(){}},location:{href:'http://127.0.0.1:8787/battlefield',search:''},
  setTimeout(){},clearTimeout(){},fetch:()=>new Promise(()=>{})});
vm.runInContext(source.split("$('settingsButton').onclick")[0],context);
vm.runInContext(`
  const map={x:10,y:10,scale:30,width:600,height:400,points:[],placeLabels:[]};
  const inks=[],ctx={save(){},restore(){},translate(){},scale(){},rotate(){},setLineDash(){},
    beginPath(){},closePath(){},moveTo(){},lineTo(){},stroke(){inks.push(['stroke',this.strokeStyle]);},
    fill(){inks.push(['fill',this.fillStyle]);},fillRect(){inks.push(['fillRect',this.fillStyle]);},
    strokeRect(){inks.push(['strokeRect',this.strokeStyle]);},measureText:t=>({width:t.length*6}),fillText(){}};
`,context);
vm.runInContext(source.slice(source.indexOf('function mapTarget('),source.indexOf('function drawMapLineups(')),context);
vm.runInContext(source.slice(source.indexOf('function mapArmyPoint('),source.indexOf('function mapHit(')),context);
vm.runInContext(`{
  const names=['己方','敌对盟','友好盟','普通中立','流浪军','敌对流浪军','友好流浪军'];
  state={live:true,reliable:true,health:{resync_required:false},armies:[],alerts:[],
    config:{self:{user_id:'12319',union_id:'1009'},relations:{}},players:[],unions:{}};
  const palette=new Set();
  for(const [i,[key,info]] of Object.entries(factionGroups).entries()){
    const row={id:key,owner:'200',union_id:'88',player_name:'玩家',faction_group:key,
      relation:key==='own'?'ally':key==='friendly_union'||key==='friendly_wanderer'?'friendly':'enemy',
      is_wanderer:key.includes('wanderer'),coord:[10,10],owner_id:'200',status:'ready'};
    if(factionLabel(row)!==names[i])throw new Error('Wrong faction label: '+key);
    if(!factionBadge(row).includes(info.label)||!factionBadge(row).includes(factionClass(row)))throw new Error('Missing faction badge');
    palette.add(mapSideBorder(row)+'/'+mapSideColor(row));
    inks.length=0;drawMapLand(row);
    if(!inks.some(([kind,color])=>kind==='strokeRect'&&color===(info.border||info.color)))throw new Error('Land outline drift: '+key);
    if(info.border&&!inks.some(([kind,color])=>kind==='fillRect'&&color===info.color+'20'))throw new Error('Friendly land lost inner identity: '+key);
    inks.length=0;drawMapFort(row);
    if(!inks.some(([kind,color])=>kind==='fill'&&color===info.color))throw new Error('Fort color drift: '+key);
    if(!inks.some(([kind,color])=>kind==='stroke'&&color===(info.border||info.color)))throw new Error('Fort outline drift: '+key);
    if(!placeArmyCountText([row]).includes(info.label+' 1'))throw new Error('Queue count lost faction: '+key);
    if(mapAttackColor({...row,target_side:'own'})!==colors.enemy)throw new Error('Own target routes must remain red');
  }
  if(palette.size!==7)throw new Error('Factions need distinct outline/inner color pairs');
  if(factionGroups.friendly_union.border!=='#3173a7'||factionGroups.friendly_union.color!==factionGroups.enemy_union.color||
    factionGroups.friendly_wanderer.border!=='#3173a7'||factionGroups.friendly_wanderer.color!==factionGroups.wanderer.color)
    throw new Error('Friendly factions must wrap the original red/purple identity in blue');
  if(factionGroup({relation:'enemy',is_wanderer:true})!=='wanderer')throw new Error('Threat fallback cannot declare hostility');
  const attack={id:'a',player_name:'敌流',faction_group:'enemy_wanderer',is_wanderer:true,
    relation:'enemy',target_side:'own',kind:'attack',from:[9,10],target:[11,10],begin:900,end:1100,
    age:0,observed_at:1000,visible:true,current_connection:true,stale:false};
  inks.length=0;drawMapArmy(attack,1000,null);
  if(!inks.some(([kind,color])=>kind==='stroke'&&color===colors.enemy))throw new Error('Attack route color changed');
  if(!inks.some(([kind,color])=>kind==='fillRect'&&color===factionGroups.enemy_wanderer.color))throw new Error('Army marker lacks faction color');
  selected='a';state.armies=[attack];renderDefensePanel=()=>{};renderLineupPanel=()=>{};renderDetails();
  if(!$('armyDetails').innerHTML.includes('faction-enemy_wanderer'))throw new Error('Pinned window lacks faction');
  state.armies=[{...attack,faction_group:'friendly_wanderer',relation:'friendly'}];renderDetails();
  if(!$('armyDetails').innerHTML.includes('faction-friendly_wanderer')||$('armyDetails').innerHTML.includes('faction-enemy_wanderer'))throw new Error('Selected card did not follow relationship update');
  if($('armyDetails').innerHTML.includes('faction-badge'))throw new Error('Selected team should use faction background without a text badge');
  state.unions={'1009':'己方','88':'聚义'};state.config.relations={'1012':{relation:'friendly',source:'client'},
    'player:200':{relation:'enemy',manual_override:true,scope:'player',own_user_id:'12319'}};
  state.players=[{id:'200',name:'流浪玩家',union_id:'0',is_wanderer:true},{id:'201',name:'己方玩家',union_id:'1009'},
    {id:'202',name:'普通玩家',union_id:'88'}];
  $('relationScope').value='union';renderRelationOptions();
  if(!$('unionSelect').innerHTML.includes('1012')||$('unionSelect').innerHTML.includes('player:200')||$('unionSelect').innerHTML.includes('value="0"'))throw new Error('Bad alliance choices');
  $('relationScope').value='player';renderRelationOptions();
  if(!$('unionSelect').innerHTML.includes('流浪玩家')||$('unionSelect').innerHTML.includes('己方玩家'))throw new Error('Bad player choices');
  $('relationSearch').value='流浪玩家';renderRelationOptions();
  if($('unionSelect').innerHTML.includes('普通玩家')||!$('unionSelect').innerHTML.includes('value="200"'))throw new Error('Player search did not narrow choices');
  $('unionSelect').value='200';renderRelationSelection();
  if($('unionRelation').value!=='enemy')throw new Error('Player override selection lost');
}`,context);
const html=fs.readFileSync(path.join(__dirname,'../static/index.html'),'utf8');
for(const key of ['own','enemy_union','friendly_union','neutral','wanderer','enemy_wanderer','friendly_wanderer'])
  assert.ok(html.includes('legend-faction faction-'+key));
console.log('Seven factions: map cells, forts, army markers, compact cards, counts and safe manual scopes passed');
