'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const elements=new Map();
const element=id=>{if(!elements.has(id))elements.set(id,{textContent:'',innerHTML:'',className:'',dataset:{},classList:{add(){},remove(){},toggle(){}},checked:false,appendChild(){},setAttribute(){},querySelectorAll:()=>[]});return elements.get(id);};
const context=vm.createContext({document:{getElementById:element,createElement:()=>({className:'',innerHTML:'',isConnected:true}),querySelector:element,querySelectorAll:()=>[]},fetch:async()=>({ok:true,json:async()=>({items:[]})}),Date:class extends Date{static now(){return 1000000;}},URLSearchParams,clearTimeout(){},setTimeout(){}});
const source=fs.readFileSync(require('node:path').join(__dirname,'../static/app.js'),'utf8').split("$('settingsButton').onclick")[0];
vm.runInContext(source,context);
vm.runInContext('const map={x:1010,y:925,scale:17,width:600,height:400}',context);
const projection=vm.runInContext(`(() => {
  const center=project([1010,925]);
  const upperLeft=project([1009,925]);
  const lowerLeft=project([1010,924]);
  const mark=project([1017,931]);
  return {center,upperLeft,lowerLeft,restored:unproject(mark.x,mark.y)};
})()`,context);
assert.ok(projection.upperLeft.x<projection.center.x&&projection.upperLeft.y<projection.center.y);
assert.ok(projection.lowerLeft.x<projection.center.x&&projection.lowerLeft.y>projection.center.y);
assert.deepEqual([...projection.restored],[1017,931]);
vm.runInContext(`{
  const inside=alertPopupPosition({x:1250,y:690},280,205,1280,720);
  if(inside.left!==958||inside.top!==473)throw new Error('窗口边缘的速览未翻转');
  const corner=alertPopupPosition({x:2,y:2},280,205,320,260);
  if(corner.left!==14||corner.top!==14)throw new Error('小窗口的速览定位不正确');
  frameAttackRoute({from:[1030,960],target:[1070,985]});
  for(const point of [[1030,960],[1070,985]]){
    const p=project(point);
    if(p.x<43||p.x>557||p.y<43||p.y>357)throw new Error('选中路线的起止坐标未进入画面');
  }
  const bounds={left:8,top:72,right:320,bottom:260};
  const point={x:295,y:245};
  const label=mapLabelBox(point,160,34,bounds,[]);
  if(label.right>=point.x||label.bottom>bounds.bottom)throw new Error('地图边缘的队伍标签越界');
  const next=mapLabelBox(point,160,34,bounds,[label]);
  if(!next||next.bottom>label.top)throw new Error('地图队伍标签没有避让');
  if(mapLabelBox(point,160,34,bounds,[bounds]))throw new Error('拥挤时应省略重叠标签');
  const [title,detail]=mapLineupText({items:[{heroes:[{name:'司马炎'},{name:'武将乙'},{name:'典韦'}],initial_hp:25101}],
    force_assessment:{initial_hp:2822},lineup_confidence:'uncertain'});
  if(title!=='司马炎 / 武将乙 / 典韦'||detail!=='历史 25,101 兵 · 历史候选')throw new Error('地图标签混用了兵力和阵容依据');
  const fallback=mapLineupText({items:[],force_assessment:{initial_hp:300}});
  if(!fallback[1].includes('兵力待核对'))throw new Error('地图标签借用了其他历史兵力');
}`,context);
const base={server_now:1000,health:{status:'live',last_packet_at:1000},live:true,counts:{},alerts:[{army_id:'1',end:1100}],armies:[{id:'1',seen_at:1000,visible:true,end:1100,from:[1000,900],target:[1015,926],display_position:[1000,900],signature:'a',relation:'enemy',kind:null,player_name:'A',union_name:'B',position_label:'出发坐标',countdown_label:'状态倒计时'}],state_kinds:{}};
function reset(data=base){vm.runInContext(`state=${JSON.stringify(data)};apiConnected=true;clockOffset=0;selected='1';selectedObservation=null;inspectorKey='';filter='all';`,context);}
reset();vm.runInContext('updateFreshness()',context);assert.equal(vm.runInContext('state.alerts.length',context),1);
vm.runInContext(`{
  selectedMarch=marchKey(state.armies[0]);
  if(!selectedIncoming(state.alerts)||selectedIncoming([]))throw new Error('来袭消失时应撤下路线');
  if(selectedIncoming([{...state.alerts[0],end:now()}]))throw new Error('到达后仍显示来袭速览');
  state.armies[0].target=[1016,926];
  if(selectedIncoming(state.alerts))throw new Error('同队号的新行军继承了旧来袭高亮');
  selectedMarch=null;
}`,context);
vm.runInContext('apiConnected=false;updateFreshness()',context);assert.equal(vm.runInContext('state.live',context),false);assert.equal(vm.runInContext('state.alerts.length',context),0);assert.equal(vm.runInContext('remaining(state.armies[0])',context),'数据中断');
reset({...base,armies:[{...base.armies[0],seen_at:930}]});vm.runInContext('updateFreshness()',context);assert.equal(vm.runInContext('state.armies[0].stale',context),true);assert.equal(vm.runInContext('remaining(state.armies[0])',context),'观测过期');
reset();vm.runInContext('renderDetails();state.armies[0].target=[1016,927];renderDetails()',context);assert.ok(element('armyDetails').innerHTML.includes('1016, 927'));
vm.runInContext("state.health.capture_source='pc';renderDetails()",context);
assert.ok(element('armyDetails').innerHTML.includes('data-game-coord="1016,927"'));
assert.ok(!element('armyDetails').innerHTML.includes('data-game-coord="1000,900"'));
vm.runInContext('selected=null;renderDetails()',context);assert.equal(element('armyDetails').textContent,'');
reset({...base,armies:[{...base.armies[0],seen_at:930,observed_at:1000,current_connection:true}]});vm.runInContext('updateFreshness()',context);assert.equal(vm.runInContext('state.armies[0].stale',context),false);
vm.runInContext('state.armies[0].current_connection=false;updateFreshness()',context);assert.equal(vm.runInContext('state.armies[0].stale',context),true);
reset({...base,targets:[],area:[800,700,820,740],candidates:[],config:{relations:{},state_rules:{}}});
vm.runInContext('renderWatch()',context);assert.equal(vm.runInContext('watchedTarget().x',context),810);assert.equal(vm.runInContext('watchedTarget().y',context),720);
assert.equal(element('watchStatus').textContent,'主力 0 · 未知 1 · 斯巴达 0');

const alerts=Array.from({length:6},(_,i)=>({army_id:String(i+1),end:1200-i*10,coord:[1100+i,900],player_name:`来袭${i}`}));
reset({...base,reliable:true,health:{...base.health,messages:10},
  armies:alerts.map(a=>({...base.armies[0],id:a.army_id})),alerts,
  targets:[{name:'分析标记',x:1015,y:926}],
  candidates:[...Array.from({length:5},(_,i)=>({army_id:String(7+i),coord:[1015,926],end:1110+i,
    player_name:`待核对部队${i}`,pending_reason:'地块归属待核对'})),
    {army_id:'12',coord:[1015,926],end:1120,player_name:'返程部队'}],
  config:{self:{name:'测试',union_name:'测试盟'},relations:{enemy:{relation:'enemy'}},state_rules:{attack:{verified:true,kind:'attack'}}}});
vm.runInContext('updateFreshness();renderStatus();renderWatch()',context);
const confirmed=element('watchEntries').innerHTML;
assert.equal((confirmed.match(/class="watch-entry confirmed"/g)||[]).length,6);
assert.ok(confirmed.indexOf('目标 1105, 900')<confirmed.indexOf('目标 1100, 900'));
assert.ok(element('noticeText').textContent.includes('目标 1105, 900'));
assert.equal(element('noticeAlerts').hidden,false);
assert.equal(element('watchBadge').textContent,'6');
assert.equal(element('watchCandidateCount').textContent,'5 支');
assert.equal(element('candidateEntries').innerHTML,'');
element('watchCandidates').open=true;
vm.runInContext('renderWatch()',context);
assert.ok(!confirmed.includes('待核对部队')&&element('candidateEntries').innerHTML.includes('待核对部队4'));
assert.ok(!element('candidateEntries').innerHTML.includes('返程部队'));
vm.runInContext(`{
  const low=state.armies.find(r=>r.id==='6');
  const main=state.armies.find(r=>r.id==='2');
  const stats={source:'direct',confidence:'linked',levels:[41]};
  alertPredictions={
    '6':{...low,items:[],force_assessment:{...stats,kind:'spartan',initial_hp:300}},
    '2':{...main,items:[],force_assessment:{...stats,kind:'main',initial_hp:28100}}
  };
  renderStatus();renderWatch();
}`,context);
assert.ok(element('mainEntries').innerHTML.includes('data-watch-id="2"'));
assert.ok(element('spartanEntries').innerHTML.includes('data-watch-id="6"'));
assert.ok(!element('watchEntries').innerHTML.includes('目标 1105, 900'));
assert.ok(element('noticeText').textContent.includes('主力 1 支 · 最近目标 1101, 900'));
vm.runInContext(`{
  const row=state.armies[0];
  const ally={...row,id:'defender',player_name:'本方武将',kind:'garrison_move',end:1050,stale:false};
  state.armies.push(ally);
  const plan={status:'ready',source:'live_march',availability:'arriving',issued_at:1000,arrival_at:1050,
    army_id:ally.id,player_name:ally.player_name,defender:{...ally},enemy_battle_id:'20',
    matchup:{predicted_win_probability:.6,probability_evidence:{method:'similar_main',lineup_cases:12}}};
  const prediction={force_assessment:{kind:'main'},garrison_plan:plan};
  const markup=garrisonPlanMarkup({end:1100},prediction);
  for(const label of ['本方武将','队 defender','提前 00:00:50','迎击胜率 估计约 60%','客户端抵达时间','相近阵容 12 场']){
    if(!markup.includes(label))throw new Error('来袭驻守建议缺少 '+label);
  }
  if(garrisonPlanMarkup({end:1050},prediction).includes('队 defender'))throw new Error('同时到达不应推荐');
  ally.end=1080;
  if(garrisonPlanMarkup({end:1100},prediction).includes('队 defender'))throw new Error('队伍状态变化后应撤下旧建议');
  ally.kind='garrison'; ally.begin=990; ally.end=990;
  prediction.garrison_plan={...plan,source:'live_holding',availability:'holding',arrival_at:null,guard_since:990,defender:{...ally}};
  if(!garrisonPlanMarkup({end:1100},prediction).includes('提前 00:01:50'))throw new Error('已开始驻守的队伍仍应显示提前量');
  state.armies.pop();
}`,context);
vm.runInContext(`{
  const assessment={kind:'main',main_basis:'skills',confidence:'linked',source:'direct',
    battle_id:'10',event_at:800,initial_hp:null,levels:[40,40,40]};
  const preview=forcePreview(assessment);
  const detail=forceDetail({force_assessment:assessment,items:[]});
  if(!preview.includes('每将 3 战法')||!preview.includes('战前兵力待核对')||!detail.includes('battle_id=10'))
    throw new Error('按战法认定的主力应在缺少兵力时显示判定依据和战报');
}`,context);
vm.runInContext(`{
  alertFocus='1101,900';
  for(const alert of state.alerts)if(['3','5','6'].includes(alert.army_id))alert.coord=[1101,900];
  const old=state.armies.find(row=>row.id==='5');
  alertPredictions['5']={...old,items:[],force_assessment:{kind:'unknown',confidence:'reference',initial_hp:200}};
  renderStatus();renderWatch();
}`,context);
assert.equal(element('alertTarget').value,'1101,900');
assert.equal(element('watchStatus').textContent,'主力 1 · 未知 2 · 斯巴达 1 · 全域 6 支');
assert.ok(!element('watchEntries').innerHTML.includes('data-watch-id="4"'));
assert.ok(element('watchEntries').innerHTML.indexOf('data-watch-id="5"')<element('watchEntries').innerHTML.indexOf('data-watch-id="3"'));
assert.ok(element('watchEntries').innerHTML.includes('暂无该玩家的匹配战报'));
assert.equal(element('watchBadge').textContent,'6');
vm.runInContext(`{
  const point=alertFocus;
  campaigns=[{id:'test-campaign',name:'测试战役',filters:{region:[1104,900,1105,900],from_at:900,to_at:1100}}];
  alertFocus=''; campaignFocus='test-campaign';
  if(scopedAlerts().map(a=>a.army_id).join(',')!=='')throw new Error('区域外的来袭进入了战役范围');
  campaigns[0].filters.region=[1101,900,1101,900];
  if(scopedAlerts().length!==4||campaignNames([1101,900],1000)[0]!=='测试战役'||campaignNames([1101,900],1200).length)
    throw new Error('战役范围或时间筛选错误');
  alertFocus=point;campaignFocus='';campaigns=[];
}`,context);
vm.runInContext(`{
  const rain=state.armies.find(r=>r.id==='3');
  const cavalry=state.armies.find(r=>r.id==='5');
  const candidate=hp=>({troop_match:'exact',event_at:800,heroes:hp.map((initial_hp,i)=>({name:'候选'+i,level:40,initial_hp}))});
  const localMain=candidate([9000,9800,9000]);
  alertPredictions['3']={...rain,items:[localMain],force_assessment:{kind:'main',confidence:'inferred',initial_hp:27800,event_at:800}};
  alertPredictions['5']={...cavalry,items:[candidate([6340,8400,4823])],force_assessment:{kind:'unknown',confidence:'reference',initial_hp:19563,event_at:800}};
  renderStatus();renderWatch();
}`,context);
assert.ok(element('mainEntries').innerHTML.includes('该玩家历史候选'));
assert.ok(!element('mainEntries').innerHTML.includes('历史 27,800 兵'));
assert.ok(!element('watchEntries').innerHTML.includes('历史 19,563 兵'));
assert.ok(element('mainEntries').innerHTML.includes('data-watch-id="3"'));
vm.runInContext(`{
  const prediction=alertPredictions['3'];
  prediction.items=[];
  prediction.force_assessment={kind:'unknown'};
  renderWatch();
}`,context);
assert.ok(element('watchEntries').innerHTML.includes('data-watch-id="3"'));
assert.ok(element('watchEntries').innerHTML.includes('暂无该玩家的匹配战报'));
vm.runInContext(`{
  const p=alertPredictions['3'];
  p.lineup_confidence='uncertain';
  p.items=[{event_at:800,initial_hp:25101,npc:1,heroes:[{name:'司马炎'}]}];
  p.force_assessment={kind:'unknown',initial_hp:2822,spartan_hint:{sample_count:2,min_hp:300,max_hp:300}};
  renderWatch();
}`,context);
const popupPreview=vm.runInContext("predictionPreview('3',0,alertPredictions['3'])",context);
assert.ok(popupPreview.includes('未知 · 偏斯巴达'));
assert.ok(popupPreview.includes('玩家对战 2 份 · 300 兵'));
assert.ok(popupPreview.includes('阵容待确认'));
assert.ok(popupPreview.includes('打地／NPC · 历史 25,101 兵'));
assert.ok(!popupPreview.includes('2,822'));
assert.ok(!element('watchEntries').innerHTML.includes('watch-force'));
vm.runInContext(`{
  const prediction=alertPredictions['3'];
  prediction.force_assessment={kind:'spartan',confidence:'inferred',initial_hp:200,event_at:800,levels:[12,12],source:'same_player_exact'};
  renderWatch();
}`,context);
assert.equal(element('watchSpartan').hidden,false);
assert.ok(element('spartanEntries').innerHTML.includes('data-watch-id="3"'));
assert.ok(element('spartanEntries').innerHTML.includes('data-watch-id="6"'));
assert.ok(!element('watchEntries').innerHTML.includes('data-watch-id="3"'));
vm.runInContext('alertPredictions={};alertFocus=""',context);
vm.runInContext('renderWatch()',context);
assert.ok(element('watchEntries').innerHTML.includes('正在分析阵容'));
assert.ok(!element('watchEntries').innerHTML.includes('本队兵力暂无线索'));
vm.runInContext(`state.alerts.find(a=>a.army_id==='3').map_hero_count=1;renderWatch()`,context);
assert.ok(element('spartanEntries').innerHTML.includes('data-watch-id="3"'));
assert.ok(!element('watchEntries').innerHTML.includes('data-watch-id="3"'));
vm.runInContext('apiConnected=false;updateFreshness();renderStatus();renderWatch()',context);
assert.equal(element('noticeAlerts').hidden,true);
assert.equal(element('watchBadge').hidden,true);
assert.equal(element('watchEntries').innerHTML,'');
reset({...base,armies:[{...base.armies[0],troops:[{position:1,code:21,name:'弓兵'}]}]});
vm.runInContext('renderDetails()',context);
assert.ok(element('armyDetails').innerHTML.includes('弓兵'));
vm.runInContext("state.armies[0].troops=[{position:1,code:23,name:'骑兵'}];renderDetails()",context);
assert.ok(element('armyDetails').innerHTML.includes('骑兵'));
const list=vm.runInContext(`lineupDisplay({match:'name_union_troop_candidate',items:Array.from({length:5},(_,i)=>({battle_id:String(500+i),event_at:1000,data:{heroes:[{name:'测试阵容'+i,level:40}]}}))})`,context);
assert.ok(list.includes('推定阵容 · 5 套')&&list.includes('兵种编码完全吻合')&&list.includes('/?battle_id=504'));
vm.runInContext(`{
  const heroes=[{name:'曹仁',position:1,troop_code:32,troop_name:'步兵',red_stars:2,
    skills:[{name:'垒实迎击',level:7},{name:'战必断金',level:8},{name:'反计之策',level:1}],gear:{name:'博浪',level:10}},
    {name:'张春华',position:2,troop_code:11,troop_name:'弓兵',red_stars:0},
    {name:'曹彰',position:3,troop_code:23,troop_name:'骑兵'}];
  const force={association:'direct',heroes,event_at:900,battle_id:'4875830'};
  const sameArmy={...force,report_army_id:'1',identity:'direct'};
  const prediction={force,items:[sameArmy],same_army_report_id:'4875830'};
  const linked=directAlertLoadout(prediction);
  if(linked!==sameArmy||directAlertLoadout({...prediction,same_army_report_id:null})||
     directAlertLoadout({...prediction,same_army_report_id:'wrong'}))throw new Error('界面应按后端核对的同队号战报展示');
  const markup=alertLoadoutMarkup(linked);
  for(const field of ['武将配置','红度 2红','步兵','垒实迎击 <span>7级','博浪 +10','红度 0红']){
    if(!markup.includes(field))throw new Error('缺少配置字段 '+field);
  }
  if(markup.includes('<details')||(markup.match(/hero-loadout-title/g)||[]).length!==3)throw new Error('确认配置应直接展示三名武将');
  const previous=alertPredictions;
  const row=state.armies[0];
  const alert={army_id:row.id,coord:[1015,926],player_name:row.player_name,end:1100};
  const candidate={heroes,event_at:900,battle_id:'42',initial_hp:10000,report_army_id:'9'};
  alertPredictions={[row.id]:{...row,items:[candidate],force_assessment:{kind:'unknown'}}};
  const container=$('candidateAttackTest');
  renderWatchRows(container,[alert],true);
  for(const field of ['该玩家历史候选','战报队号 9','battle_id=42','红度 0红','红度 待核对','战法未取得',
    '<li>垒实迎击 <span>7级</span></li><li>战必断金 <span>8级</span></li><li>反计之策 <span>1级</span></li>']){
    if(!container.innerHTML.includes(field))throw new Error('来袭列表缺少候选字段 '+field);
  }
  if(container.innerHTML.includes('watch-lineup')||container.innerHTML.includes('watch-force')||container.innerHTML.includes('<time>'))
    throw new Error('来袭列表仍展示重复摘要或战报时间');
  const details=predictionDisplay({...prediction,items:[sameArmy,candidate],force_assessment:{kind:'unknown'}});
  const candidateStart=details.indexOf('其他阵容候选 · 1 套');
  if(candidateStart<0||!details.slice(0,candidateStart).includes('同队号最新战报')||
     !details.slice(candidateStart).includes('战报队号 9')||details.slice(candidateStart).includes('4875830'))
    throw new Error('同队号战报和其他队号候选应分开');
  alertPredictions[row.id].items=[];
  renderWatchRows(container,[alert],true);
  if(!container.innerHTML.includes('暂无该玩家的匹配战报')||container.innerHTML.includes('hero-loadouts'))
    throw new Error('缺少该玩家战报时应留空配置');
  alertPredictions=previous;
}`,context);
reset({...base,armies:[{...base.armies[0],display_position:[1014,925],target:[1014,925],kind:'garrison',end:0,position_label:'驻守坐标'}]});
vm.runInContext("setInspectorView('army');renderDetails()",context);
assert.equal(element('armyDetails').hidden,false);
assert.equal(element('watchPanel').hidden,true);
const guard=element('armyDetails').innerHTML;
assert.equal((guard.match(/1014, 925/g)||[]).length,1);
assert.ok(!guard.includes('detailCountdown')&&guard.includes('驻守坐标'));
assert.ok(!guard.includes('重伤'));
vm.runInContext("setInspectorView('watch')",context);
assert.equal(element('armyDetails').hidden,true);
assert.equal(element('watchPanel').hidden,false);
reset();
vm.runInContext('renderRoster()',context);
assert.equal(element('rosterSelection').hidden,false);
assert.equal(element('selectedNumber').textContent,'部队 1');
vm.runInContext("filter='self';renderRoster()",context);
assert.equal(element('selectedStatus').textContent,'当前筛选外');
assert.equal(element('listCount').textContent,'0 支');
vm.runInContext("state.armies=[];setInspectorView('army');renderRoster();renderDetails()",context);
assert.equal(element('selectedStatus').textContent,'历史观测');
assert.equal(element('armyDetails').hidden,false);
assert.ok(element('armyDetails').innerHTML.includes('历史观测')&&!element('armyDetails').innerHTML.includes('detailCountdown'));
vm.runInContext('selected=null;renderRoster()',context);
assert.equal(element('rosterSelection').hidden,true);
console.log('UI state scenarios passed');
