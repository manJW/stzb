'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const elements=new Map();
const element=id=>{
  if(!elements.has(id))elements.set(id,{innerHTML:'',textContent:'',dataset:{},isConnected:true,
    classList:{toggle(){},add(){},remove(){}},querySelectorAll:()=>[],setAttribute(){}});
  return elements.get(id);
};
const context=vm.createContext({document:{getElementById:element,querySelector:element,
  documentElement:{classList:{contains:()=>true}}},URL,URLSearchParams,Date,AbortSignal,
  localStorage:{getItem:()=>null},history:{replaceState(){}},location:{href:'http://127.0.0.1:8787/battlefield',search:''},
  setTimeout(){},clearTimeout(){},fetch:()=>new Promise(()=>{})});
vm.runInContext(fs.readFileSync(path.join(__dirname,'../static/app.js'),'utf8').split("$('settingsButton').onclick")[0],context);
vm.runInContext(`{
  const row={id:'a',owner:'p',player_name:'流浪玩家',is_wanderer:true,force_type:1,union_id:'1135',
    union_name:'聚义',relation:'enemy',state_code:1,signature:'1:13:1',kind:'attack',state_label:'进攻中',
    begin:100,end:Date.now()/1000+60,from:[740,887],target:[743,899],display_position:[740,887],troops:[],target_side:'own'};
  state={config:{self:{user_id:'self'}},health:{instance_id:'s'},live:true,armies:[row],alerts:[{army_id:'a',player_name:row.player_name,end:row.end,coord:row.target,is_wanderer:true}]};
  selected='a';renderDefensePanel=()=>{};renderLineupPanel=()=>{};
  renderDetails();
  if(!document.getElementById('armyDetails').innerHTML.includes('faction-wanderer'))throw new Error('Compact selected army lost wanderer color');
  if(armyPlayerButton(row).includes('faction-badge'))throw new Error('Garrison/popup players must not repeat faction text labels');
  if(wandererBadge({...row,is_wanderer:false})!=='')throw new Error('Normal or NPC was labeled as a wanderer');
  if(mapAttackColor(row)!==colors.enemy)throw new Error('Own-land wanderer attack is not red');
  if(mapAttackColor({...row,target_side:'other'})===colors.enemy)throw new Error('Other target was presented as own-land incoming');
  if(!mapAttackDescription(row).includes('流浪军'))throw new Error('Map hover lost wanderer identity');
}`,context);
console.log('Wanderer badges, compact selected detail, map hover and own/other target arrow colors passed');
