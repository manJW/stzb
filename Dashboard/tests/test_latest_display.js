'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const elements = new Map();
const element = id => {
  if (!elements.has(id)) elements.set(id, {textContent:'', innerHTML:'', dataset:{},
    classList:{add(){},remove(){},toggle(){}},setAttribute(){},querySelectorAll(){return []}});
  return elements.get(id);
};
const context = vm.createContext({document:{getElementById:element,querySelector:element,
  querySelectorAll:()=>[],createElement:()=>({})}, Date, URL, URLSearchParams,
  location:{href:'http://127.0.0.1:8787/battlefield',search:''},history:{replaceState(){}},
  fetch:async()=>({ok:true,json:async()=>({items:[]})}),setTimeout(){},clearTimeout(){}});
const source = fs.readFileSync(require('node:path').join(__dirname,'../static/app.js'),'utf8')
  .split("$('settingsButton').onclick")[0];
vm.runInContext(source, context);
vm.runInContext(`{
  const make=(id,name,red)=>({battle_id:id,heroes:[{id:1,position:1,name,red_stars:red,level:40,
    skills:[],skill_slot_ids:[0,0,0]}],initial_hp:100,event_at:900});
  const old=make('1','旧配置',5), latest=make('2','新配置',1);
  const p={army_id:'A',current_report_id:'2',current_resolution:{source:'same_card_instances'},
    same_army_report_id:'1',items:[old,latest],force_assessment:{kind:'spartan',battle_id:'2'}};
  state={armies:[{id:'A'}],alerts:[],health:{capture_source:'dashen'}};
  alertPredictions={A:p};
  if(currentLineupReport(p)!==latest||mapLineupReport(p,'spartan')!==latest)
    throw new Error('地图和侧栏必须共同选择新战报');
  const summary=watchHeroSummary({army_id:'A'});
  if(summary!=='新配置40级(1红)')throw new Error('紧凑侧栏使用了旧武将、等级或旧红度');
  const details=watchArmyDetails({army_id:'A',coord:[1,2]},null);
  if(!details.includes('新配置')||details.includes('旧配置'))throw new Error('展开侧栏未采用最新配置');
  const unknown={...p,current_resolution:{unresolved:'incomplete_roster'},force_assessment:{kind:'unknown',battle_id:'2'}};
  if(currentLineupReport(unknown)!==null||mapLineupReport(unknown,'unknown')!==null)
    throw new Error('新战报缺字段时不能回退显示旧主力');
}`, context);
assert.ok(true);
console.log('Latest lineup is shared by map, compact sidebar and expanded card; incomplete new report never restores old build');
