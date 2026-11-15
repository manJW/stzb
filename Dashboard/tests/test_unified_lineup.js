'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const elements=new Map(),requests=[];
const element=id=>{
  if(!elements.has(id))elements.set(id,{innerHTML:'',textContent:'',dataset:{},isConnected:true,
    classList:{add(){},remove(){},toggle(){}},querySelectorAll:()=>[],setAttribute(){}});
  return elements.get(id);
};
const context=vm.createContext({document:{getElementById:element,querySelector:element,
  documentElement:{classList:{contains:()=>true}}},URL,URLSearchParams,AbortSignal,Date,
  location:{href:'http://127.0.0.1:8787/battlefield',search:''},history:{replaceState(){}},
  localStorage:{getItem:()=>null},setTimeout(){},clearTimeout(){},
  fetch:(url,options)=>new Promise(resolve=>requests.push({url,options,resolve}))});
vm.runInContext(fs.readFileSync(path.join(__dirname,'../static/app.js'),'utf8').split("$('settingsButton').onclick")[0],context);
const heroes=['荀彧','司马懿','曹操'].map((name,i)=>({id:i+1,name,position:i+1,level:41+i%2,
  red_stars:i===0?5:0,skills:[{id:1,name:'战法',level:10}],remaining_hp:4000,wounded_hp:100}));
context.heroes=heroes;
vm.runInContext(`{
  const row={id:'82407103',owner:'12417',player_name:'Mus丶肥肥',union_id:'1008',
    begin:100,end:200,from:[738,886],target:[741,899],troops:[{code:21},{code:11},{code:31}]};
  state={health:{instance_id:'s'},armies:[row,{...row,id:'another'}],alerts:[{army_id:row.id}]};
  selected=row.id;
  renderStatus=()=>{};renderWatch=()=>{};renderDetails=()=>{};draw=()=>{};
  old={army_id:row.id,owner:row.owner,player_name:row.player_name,union_id:row.union_id,
    begin:100,end:200,from:row.from,target:row.target,troop_codes:[21,11,31],
    source_revision:1,evaluated_at:150,current_report_id:'6238523',current_resolution:null,
    display_verified:false,display_report:{battle_id:'6238523',event_at:90,heroes},
    items:[],force_assessment:{kind:'main'}};
  old.items=[old.display_report];
  acceptLivePrediction(row,old);
  const selectedMarkup=compactForecastLoadout(row,old);
  if(!selectedMarkup.includes('荀彧')||!selectedMarkup.includes('5红'))throw new Error('Unverified personal forecast disappeared on selection');
  if(currentLineupReport(old)!==mapLineupReport(old,'main'))throw new Error('Map and detail do not share the same source');
  if(/data-selected-tactics="82407103"\\s+open/.test(selectedMarkup))throw new Error('Tactics must start collapsed');
  if(compactForecastLoadout(state.armies[1],old).includes('荀彧'))throw new Error('Another personal army borrowed the forecast');
  fresh={...old,source_revision:2,evaluated_at:160,display_verified:true,current_report_id:'6464045',
    display_report:{battle_id:'6464045',event_at:155,heroes:heroes.map((h,i)=>i?h:{...h,name:'最新武将',red_stars:4})}};
  const data={live_lineup:true,prediction:fresh,items:[{battle_id:'6464045',event_at:155,
    player_name:row.player_name,union_id:row.union_id,data:{heroes:fresh.display_report.heroes}}]};
  cacheLiveLineup(row,data);
  if(!compactArchiveLoadout(row,data,placeLoadoutReports(row,data)).includes('最新武将'))throw new Error('Detail did not adopt latest report');
  if(currentLineupReport(predictionFor(row.id)).battle_id!=='6464045')throw new Error('Ground card did not update together');
  if(acceptLivePrediction(row,old))throw new Error('Older revision overwrote the latest report');
  const ambiguous={...fresh,source_revision:3,evaluated_at:170,display_verified:false,display_report:null,force_assessment:{kind:'unknown'}};
  acceptLivePrediction(row,ambiguous);
  if(currentLineupReport(ambiguous)!==null||mapLineupReport(ambiguous,'unknown')!==null||compactForecastLoadout(row,ambiguous).includes('最新武将'))
    throw new Error('Explicit unknown restored a historical candidate');
  acceptLivePrediction(row,fresh);
  // Reset to the verified newer result for an in-flight old batch response.
  alertPredictions[row.id]=fresh;
}`,context);
(async()=>{
  const pending=vm.runInContext('loadAlertPredictions()',context);
  requests[0].resolve({ok:true,json:async()=>({predictions:{'82407103':context.old}})});
  await pending;
  assert.equal(vm.runInContext("predictionFor('82407103').current_report_id",context),'6464045');
  console.log('Unified live lineup: inferred/verified source parity, latest report, collapsed skills, personal-army isolation and stale batch protection passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
