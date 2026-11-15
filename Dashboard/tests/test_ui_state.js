'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const elements=new Map();
const element=id=>{if(!elements.has(id))elements.set(id,{textContent:'',innerHTML:'',className:'',dataset:{},classList:{add(){},remove(){},toggle(){}},checked:false,appendChild(){},setAttribute(){},querySelectorAll:()=>[]});return elements.get(id);};
const context=vm.createContext({document:{getElementById:element,createElement:()=>({className:'',innerHTML:'',isConnected:true}),querySelector:element,querySelectorAll:()=>[]},fetch:async()=>({ok:true,json:async()=>({items:[]})}),Date:class extends Date{static now(){return 1000000;}},URL,URLSearchParams,location:{href:'http://127.0.0.1:8787/battlefield',search:''},history:{replaceState(){}},clearTimeout(){},setTimeout(){}});
const source=fs.readFileSync(require('node:path').join(__dirname,'../static/app.js'),'utf8').split("$('settingsButton').onclick")[0];
const page=fs.readFileSync(require('node:path').join(__dirname,'../static/index.html'),'utf8');
assert.ok(!/<select\b[^>]*\bid="alertTarget"/.test(page),'Current place is read-only text');
assert.ok(page.indexOf('id="watchEntries"')<page.indexOf('id="placeArmies"'),'Incoming attacks precede stationed teams');
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
  const bounds={left:8,top:72,right:320,bottom:260};
  const point={x:295,y:245};
  const label=mapLabelBox(point,160,34,bounds,[]);
  if(label.right>=point.x||label.bottom>bounds.bottom)throw new Error('地图边缘的队伍标签越界');
  const next=mapLabelBox(point,160,34,bounds,[label]);
  if(!next||next.bottom>label.top)throw new Error('地图队伍标签没有避让');
  if(mapLabelBox(point,160,34,bounds,[bounds]))throw new Error('拥挤时应省略重叠标签');
  const main={battle_id:'2',event_at:1000,initial_hp:25101,heroes:[
    {name:'司马炎',red_stars:5,remaining_hp:8000,wounded_hp:200},{name:'武将乙',red_stars:0,remaining_hp:0,wounded_hp:500},{name:'典韦',remaining_hp:2000,wounded_hp:100}]};
  const other={...main,battle_id:'1',initial_hp:2822};
  const prediction={items:[other,main],force_assessment:{kind:'main',battle_id:'2'}};
  const source=mapLineupReport(prediction,'main');
  const [player,roster,meta]=mapLineupText('玩家甲','main',source);
  if(source!==main||player!=='主力 · 玩家甲'||roster!=='司马炎 5红 / 武将乙 0红 / 典韦 红度未知'||
     !meta.startsWith('战报 ')||!meta.endsWith('战后余兵 10,800'))throw new Error('地图主力标签应显示同一战报的红度、时间及剩余兵力与伤兵合计');
  if(mapLineupReport({...prediction,items:[other],force:main},'main')!==main)throw new Error('主力标签应使用认定主力的战报');
  const missing=mapLineupText('玩家甲','main',{...main,heroes:[{name:'司马炎',remaining_hp:null}]});
  if(!missing[2].endsWith('战后余兵 未知'))throw new Error('缺失战后兵力不能用战前兵力代替');
  const defeated=mapLineupText('玩家甲','main',{...main,heroes:main.heroes.map(h=>({...h,remaining_hp:0,wounded_hp:0}))});
  if(!defeated[2].endsWith('战后余兵 0'))throw new Error('已知余兵为零应照常显示');
  const [title,detail]=mapLineupText('玩家甲','unknown',mapLineupReport(prediction,'unknown'));
  if(title!=='司马炎 / 武将乙 / 典韦'||detail!=='战前 2,822 兵')throw new Error('普通地图标签应使用对应的战前兵力');
  const marker=visibleRouteMarker({x:-30,y:60},{x:230,y:60},200,120);
  if(marker?.x!==100||marker.y!==60||visibleRouteMarker({x:-20,y:8},{x:220,y:8},200,120))
    throw new Error('画面内的进攻路径应显示方向标记');
}`,context);
const base={server_now:1000,health:{status:'live',last_packet_at:1000},live:true,counts:{},alerts:[{army_id:'1',end:1100}],armies:[{id:'1',seen_at:1000,visible:true,end:1100,from:[1000,900],target:[1015,926],display_position:[1000,900],signature:'a',relation:'enemy',kind:null,player_name:'A',union_name:'B',position_label:'出发坐标',countdown_label:'状态倒计时'}],state_kinds:{}};
function reset(data=base){vm.runInContext(`state=${JSON.stringify(data)};apiConnected=true;clockOffset=0;selected='1';selectedMarch=null;inspectorKey='';`,context);}
reset();vm.runInContext('updateFreshness()',context);assert.equal(vm.runInContext('state.alerts.length',context),1);
reset({...base,transport_valid_until:1038,reliable:true,
  health:{...base.health,last_packet_at:980,last_scene_at:980,resync_required:false},
  armies:[{...base.armies[0],seen_at:700,observed_at:980,current_connection:true,subscription_valid_until:1038}]});
vm.runInContext(`{
  clockOffset=19;updateFreshness();
  if(!state.live||!state.reliable||state.alerts.length!==1||state.armies[0].stale)
    throw new Error('静默19秒但同一地图订阅仍有效时不应清空来袭');
  clockOffset=39;updateFreshness();
  if(state.live||state.reliable||state.alerts.length)throw new Error('失效订阅不能继续预警');
}`,context);
reset({...base,transport_valid_until:1100,reliable:true,
  health:{...base.health,last_scene_at:1000,resync_required:false},
  armies:[{...base.armies[0],seen_at:700,observed_at:900,current_connection:true,subscription_valid_until:1100}]});
vm.runInContext(`{
  updateFreshness();
  if(state.armies[0].stale||state.alerts.length!==1)throw new Error('增量订阅不应要求部队反复更新');
  state.armies[0].visible=false;updateFreshness();
  if(state.alerts.length||!state.armies[0].stale)throw new Error('移出视野必须撤下来袭');
}`,context);
reset();vm.runInContext('updateFreshness()',context);
vm.runInContext(`{
  const own={...state.armies[0],kind:'attack',target_side:'own',target_ownership_fresh:true,age:1};
  if(mapAttackColor(own)!==colors.enemy||mapAttackColor({...own,incoming:false,age:40,target_ownership_fresh:false})!==colors.enemy)
    throw new Error('己方目标不能因未预警或观测过期变成其他颜色');
  if(mapAttackColor({...own,target_side:'other',incoming:true})!=='#64748b'||
     mapAttackColor({...own,target_side:'unknown'})!=='#9aa6b5')
    throw new Error('其他目标与归属未知应分开着色');
  state.armies=[own];state.reliable=true;state.health.last_scene_at=1000;
  if(routeStatus(1)!=='')throw new Error('正常进攻不应提示数据过期');
  own.age=20;own.target_ownership_fresh=false;
  if(!routeStatus(1).includes('进攻观测过期 1 支')||!routeStatus(1).includes('目标归属过期 1 处')||
     !mapAttackDescription(own).includes('攻击己方地块 · 进攻观测已过期 · 目标归属已过期'))
    throw new Error('有箭头时也应单独提示观测和归属过期');
  state.armies.push({...own,id:'duplicate'});
  if(!routeStatus(2).includes('目标归属过期 1 处'))throw new Error('同一地块不能重复统计归属过期');
  own.age=1;own.target_ownership_fresh=true;own.target_side='unknown';
  if(!routeStatus(2).includes('目标归属未确认 1 处'))throw new Error('未知归属不能冒充其他方');
  state.live=false;
  if(!routeStatus(0).includes('地图数据已过期'))throw new Error('连接中断应提示数据过期');
  state.live=true;state.health.resync_required=true;
  state.health.map_recovery={status:'ready',message:'当前地图已自动恢复'};
  if(routeStatus(1).includes('当前地图已自动恢复'))throw new Error('再次中断时不能沿用上次恢复成功的提示');
  state.health.map_recovery={status:'recovering',message:'正在自动恢复当前地图'};
  if(!routeStatus(1).includes('正在自动恢复当前地图'))throw new Error('自动恢复中应显示真实状态');
}`,context);
reset();vm.runInContext('updateFreshness()',context);
vm.runInContext(`{
  const route={...state.armies[0],kind:'attack',relation:'unknown',incoming:false};
  if(!activeObservedAttack(route))throw new Error('敌我未确认的已知进攻路线应有箭头');
  if(activeObservedAttack({...route,kind:'retreat'})||activeObservedAttack({...route,target:route.from})||
     activeObservedAttack({...route,relation:'ally'})||activeObservedAttack({...route,end:now()}))
    throw new Error('地图进攻箭头混入了其他状态');
}`,context);
vm.runInContext(`{
  selectedMarch=marchKey(state.armies[0]);
  if(!selectedIncoming(state.alerts)||selectedIncoming([]))throw new Error('来袭消失时应撤下路线');
  if(selectedIncoming([{...state.alerts[0],end:now()}]))throw new Error('到达后仍显示来袭速览');
  state.armies[0].stale=true;
  if(selectedIncoming(state.alerts))throw new Error('过期部队仍显示来袭高亮');
  state.armies[0].stale=false;
  state.armies[0].target=[1016,926];
  if(selectedIncoming(state.alerts))throw new Error('同队号的新行军继承了旧来袭高亮');
  selectedMarch=null;
}`,context);
vm.runInContext('apiConnected=false;updateFreshness()',context);assert.equal(vm.runInContext('state.live',context),false);assert.equal(vm.runInContext('state.alerts.length',context),0);assert.equal(vm.runInContext('remaining(state.armies[0])',context),'数据中断');
reset({...base,armies:[{...base.armies[0],seen_at:930}]});vm.runInContext('updateFreshness()',context);assert.equal(vm.runInContext('state.armies[0].stale',context),true);assert.equal(vm.runInContext('remaining(state.armies[0])',context),'观测过期');
for(const [code,label] of [[5,'要塞内'],[25,'停留'],[2,'调动中'],[4,'返程中']]){
  reset({...base,alerts:[],armies:[{...base.armies[0],state_code:code,state_label:'状态待校准',kind:null}]});
  vm.runInContext('renderDetails()',context);
  assert.ok(element('armyDetails').innerHTML.includes(`<span>${label}</span>`),
    'Details must remain available outside attack/garrison: '+label);
}
reset();vm.runInContext('renderDetails();state.armies[0].target=[1016,927];renderDetails()',context);assert.ok(element('armyDetails').innerHTML.includes('1016, 927'));
vm.runInContext("state.health.capture_source='pc';renderDetails()",context);
assert.ok(element('armyDetails').innerHTML.includes('data-game-coord="1016,927"'));
assert.ok(!element('armyDetails').innerHTML.includes('data-game-coord="1000,900"'));
vm.runInContext('selected=null;renderDetails()',context);assert.equal(element('armyDetails').textContent,'');
reset({...base,armies:[{...base.armies[0],seen_at:930,observed_at:1000,current_connection:true}]});vm.runInContext('updateFreshness()',context);assert.equal(vm.runInContext('state.armies[0].stale',context),false);
vm.runInContext('state.armies[0].current_connection=false;updateFreshness()',context);assert.equal(vm.runInContext('state.armies[0].stale',context),true);
reset({...base,targets:[],area:[800,700,820,740],candidates:[],config:{relations:{},state_rules:{}}});
vm.runInContext('renderWatch()',context);
assert.equal(element('watchStatus').textContent,'点击看板或游戏中的地块，查看该处敌袭');
assert.equal(element('watchEntries').innerHTML,'');
vm.runInContext("garrisonTrialState={phase:'returned',requested_alert:{coord:[1007,947]}};renderWatch()",context);
assert.equal(element('cancelWebGarrison').hidden,true);
vm.runInContext("garrisonTrialState.phase='reading';renderWatch()",context);
assert.equal(element('cancelWebGarrison').hidden,false);
vm.runInContext('garrisonTrialState=null',context);

const alerts=Array.from({length:6},(_,i)=>({army_id:String(i+1),end:1200-i*10,coord:[1100+i,900],player_name:`来袭${i}`}));
reset({...base,reliable:true,health:{...base.health,messages:10},
  armies:alerts.map(a=>({...base.armies[0],id:a.army_id})),alerts,
  targets:[{name:'分析标记',x:1015,y:926}],candidates:[],
  config:{self:{name:'测试',union_name:'测试盟'},relations:{enemy:{relation:'enemy'}},state_rules:{attack:{verified:true,kind:'attack'}}}});
vm.runInContext('updateFreshness();renderStatus();renderWatch()',context);
assert.equal(element('watchEntries').innerHTML,'','No selection must not show all attacks');
assert.equal(element('watchBadge').hidden,true);
assert.equal(element('watchTargetControl').hidden,true);
assert.equal(element('alertTarget').textContent,'未选择地块');
vm.runInContext(`function draw(){} function focusTarget(){}
  setMapScope('1101,900','');`,context);
assert.equal((element('watchEntries').innerHTML.match(/data-watch-target=/g)||[]).length,1);
assert.equal((element('watchEntries').innerHTML.match(/data-watch-army=/g)||[]).length,1);
assert.ok(element('watchEntries').innerHTML.includes('data-watch-army="2|'));
assert.ok(element('watchEntries').innerHTML.includes('武将信息未取得'));
assert.ok(!element('watchEntries').innerHTML.includes('class="watch-record"'),'Incoming tactics start collapsed');
assert.equal(element('watchBadge').textContent,'1');
assert.equal(element('alertTarget').textContent,'1101, 900');
assert.equal(element('alertTarget').title,'1101, 900');
assert.equal(vm.runInContext('scopedAlerts().length',context),6,'The map keeps all attacks');
vm.runInContext(`{
  for(const alert of state.alerts)if(['3','5','6'].includes(alert.army_id))alert.coord=[1101,900];
  const low=state.armies.find(r=>r.id==='6'),main=state.armies.find(r=>r.id==='2');
  alertPredictions={
    '6':{...low,items:[],force_assessment:{kind:'spartan',initial_hp:300}},
    '2':{...main,items:[],force_assessment:{kind:'main',initial_hp:28100}}
  };
  renderWatch();
}`,context);
assert.equal(element('watchStatus').textContent,'主力 1 · 斯巴达 1 · 未知 2');
assert.equal((element('watchEntries').innerHTML.match(/data-watch-army=/g)||[]).length,4);
assert.ok(!element('watchEntries').innerHTML.includes('data-watch-army="4|'));
assert.ok(!fs.readFileSync(require('node:path').join(__dirname,'../static/index.html'),'utf8').includes('id="targetIntel"'));
assert.equal(element('watchBadge').textContent,'4');
assert.ok(element('watchEntries').innerHTML.indexOf('data-watch-army="2|')<element('watchEntries').innerHTML.indexOf('data-watch-army="6|'));
vm.runInContext(`{
  const key=watchMarchKey(state.alerts.find(a=>a.army_id==='2'));
  openWatchArmies.delete(key);renderWatch();
  if(openWatchArmies.has(key))throw new Error('A timer refresh must preserve manual collapse');
  state.alerts.push({...state.alerts[1],army_id:'7',end:1300});
  state.armies.push({...state.armies[1],id:'7'});renderWatch();
  if(openWatchArmies.has(watchMarchKey(state.alerts.at(-1))))throw new Error('New incoming tactics should remain collapsed');
  if(openWatchArmies.has(key))throw new Error('A new team must not reopen a manually collapsed team');
  state.alerts.pop();state.armies.pop();
  setMapScope('1100,900','');
}`,context);
assert.equal(element('watchBadge').textContent,'1');
assert.ok(element('watchEntries').innerHTML.includes('data-watch-army="1|'));
assert.ok(!element('watchEntries').innerHTML.includes('data-watch-army="2|'));
vm.runInContext("setMapScope('999,999','')",context);
assert.equal(element('watchEntries').innerHTML,'');
assert.equal(element('watchStatus').textContent,'该地块暂无已确认敌袭');
assert.equal(vm.runInContext('state.alerts.length',context),6,'Empty selection must not remove map attacks');
vm.runInContext(`{
  state.game_focus={event_id:'focused-game-click-1',source:'dashen_click',coord:[1101,900],updated_at:999};
  syncGameFocus();
}`,context);
assert.equal(vm.runInContext('alertFocus',context),'1101,900');
assert.equal(element('alertTarget').textContent,'1101, 900','A game click updates the read-only place label');
assert.equal((element('watchEntries').innerHTML.match(/data-watch-army=/g)||[]).length,4);
assert.equal((element('watchEntries').innerHTML.match(/class="watch-record"/g)||[]).length,0,'Game click lists the attacks but keeps tactics collapsed');
vm.runInContext(`{
  const key=watchMarchKey(state.alerts[1]);openWatchArmies.delete(key);
  syncGameFocus();renderWatch();
  if(openWatchArmies.has(key))throw new Error('Repeating the same game event must not reset expansion');
  displayPreferences.showSpartans=false;renderWatch();
}`,context);
assert.equal(element('watchBadge').textContent,'3');
assert.ok(!element('watchEntries').innerHTML.includes('data-watch-army="6|'));
vm.runInContext(`{
  displayPreferences.showSpartans=true;
  campaigns=[{id:'test-campaign',name:'测试战役',filters:{region:[1100,900,1100,900],from_at:900,to_at:1100}}];
  campaignFocus='test-campaign';
  if(scopedAlerts().length!==1||focusedAlerts().length!==4)throw new Error('Tile selection must remain exact even outside an analysis region');
  campaignFocus='';campaigns=[];
  setMapScope('','');
}`,context);
assert.equal(element('watchEntries').innerHTML,'');
assert.equal(vm.runInContext('scopedAlerts().length',context),6);
vm.runInContext(`{
  for(const [assessment,label] of [
    [{kind:'main',confidence:'inferred'},'主力'],
    [{kind:'spartan',confidence:'inferred',spartan_basis:'two_skills_each'},'斯巴达'],
    [{kind:'unknown',review_state:'linked_report'},'未知']
  ]){
    const preview=predictionPreview('2',null,{items:[],force_assessment:assessment});
    if(!preview.includes('>'+label+'</span>')||/推定|候选|偏斯巴达/.test(preview))throw new Error('Only three force labels should be visible');
  }
  setMapScope('1101,900','');
  apiConnected=false;updateFreshness();renderStatus();renderWatch();
}`,context);
assert.equal(element('watchStatus').textContent,'数据未更新，来袭判断暂停');
assert.equal(element('watchBadge').hidden,true);
assert.equal(element('watchEntries').innerHTML,'');
reset({...base,armies:[{...base.armies[0],troops:[{position:1,code:21,name:'弓兵'}]}]});
vm.runInContext('renderDetails()',context);
assert.ok(element('armyDetails').innerHTML.includes('弓兵'));
vm.runInContext("state.armies[0].troops=[{position:1,code:23,name:'骑兵'}];renderDetails()",context);
assert.ok(element('armyDetails').innerHTML.includes('骑兵'));
const list=vm.runInContext(`lineupDisplay({match:'name_union_troop_candidate',items:Array.from({length:5},(_,i)=>({battle_id:String(500+i),event_at:1000,data:{heroes:[{name:'测试阵容'+i,level:40}]}}))})`,context);
assert.ok(list.includes('历史阵容 · 5 套')&&list.includes('来自该玩家的历史战报')&&list.includes('/?battle_id=504'));
vm.runInContext(`{
  const heroes=[{name:'曹仁',level:42,position:1,troop_code:32,troop_name:'步兵',red_stars:2,
    skills:[{name:'垒实迎击',level:7},{name:'战必断金',level:8},{name:'反计之策',level:1}],gear:{name:'博浪',level:10}},
    {name:'张春华',level:40,position:2,troop_code:11,troop_name:'弓兵',red_stars:0},
    {name:'曹彰',position:3,troop_code:23,troop_name:'骑兵'}];
  const force={association:'direct',heroes,event_at:900,battle_id:'4875830'};
  const sameArmy={...force,report_army_id:'1',identity:'direct'};
  const prediction={force,items:[sameArmy],same_army_report_id:'4875830'};
  const linked=directAlertLoadout(prediction);
  if(linked!==sameArmy||directAlertLoadout({...prediction,same_army_report_id:null})||
     directAlertLoadout({...prediction,same_army_report_id:'wrong'}))throw new Error('界面应按后端核对的同队战报展示');
  const markup=alertLoadoutMarkup(linked,{sameArmy:true});
  for(const field of ['同队战报','红度 2红','步兵','垒实迎击 <span>7级','博浪 +10','红度 0红']){
    if(!markup.includes(field))throw new Error('缺少配置字段 '+field);
  }
  if(markup.includes('<details')||(markup.match(/hero-loadout-title/g)||[]).length!==3)throw new Error('确认配置应直接展示三名武将');
  const slots=heroLoadouts([{name:'曹彰',skill_slot_ids:[10,0,null],skills:[{id:10,name:'战必断金',level:8}]}],true);
  if(!slots.includes('战必断金')||!slots.includes('第2槽 <span>空槽')||!slots.includes('第3槽 <span>未采到')){
    throw new Error('已取得、空槽和未采到应分别展示');
  }
  const previous=alertPredictions;
  const row=state.armies[0];
  const alert={army_id:row.id,coord:[1015,926],target_name:'陈要塞',from:[1000,900],from_name:'谯要塞<2>',player_name:row.player_name,end:1100};
  const candidate={heroes,event_at:900,battle_id:'42',initial_hp:10000,report_army_id:'9'};
  alertPredictions={[row.id]:{...row,items:[candidate],force_assessment:{kind:'main',confidence:'inferred',source:'arrival'},
    garrison_plan:{status:'ready',player_name:'本方武将'}}};
  const captureSource=state.health.capture_source;
  state.health.capture_source='pc';
  const container=$('candidateAttackTest');
  container.innerHTML=watchArmyDetails(alert);
  for(const field of ['游戏内定位','快捷驻守','红度 0红','红度 待核对','战法未取得',
    '<li>垒实迎击 <span>7级</span></li><li>战必断金 <span>8级</span></li><li>反计之策 <span>1级</span></li>']){
    if(!container.innerHTML.includes(field))throw new Error('来袭列表缺少战报字段 '+field);
  }
  if(watchHeroSummary(alert)!=='曹仁42级(2红) / 张春华40级(0红) / 曹彰?级(红度未知)')throw new Error('队伍摘要应展示同一份战报的武将、等级和红度');
  for(const repeated of ['陈要塞','抵达倒计时','推定主力','驻守建议','本方武将','data-watch-end','<time>','地图速览',
    'watch-route-meta','来源 谯要塞','历史战报','战报队号 9','battle_id=42','博浪']){
    if(container.innerHTML.includes(repeated))throw new Error('展开详情展示了重复摘要或驻守建议 '+repeated);
  }
  const details=predictionDisplay({...prediction,items:[sameArmy,candidate],force_assessment:{kind:'unknown'}});
  const historyStart=details.indexOf('其他历史战报 · 1 份');
  if(historyStart<0||!details.slice(0,historyStart).includes('同队战报')||
     details.slice(historyStart).includes('战报队号 9')||details.slice(historyStart).includes('4875830')||
     !details.slice(historyStart).includes('历史 10,000 兵')||!details.slice(historyStart).includes('博浪 +10'))
    throw new Error('应分开显示同队战报和其他历史战报的配置');
  const latestDetails=predictionDisplay({...prediction,items:[{...candidate,event_at:950},sameArmy],force_assessment:{kind:'spartan'}});
  const latestHistory=latestDetails.indexOf('其他历史战报 · 1 份');
  if(!latestDetails.slice(0,latestHistory).includes('battle_id=42')||
     !latestDetails.slice(latestHistory).includes('同队战报')||!latestDetails.slice(latestHistory).includes('4875830'))
    throw new Error('队伍详情应先展示较新的匹配战报');
  alertPredictions[row.id].items=[];
  container.innerHTML=watchArmyDetails(alert);
  if(!container.innerHTML.includes('暂无该玩家的匹配战报')||container.innerHTML.includes('hero-loadouts'))
    throw new Error('缺少该玩家战报时应留空配置');
  alertPredictions=previous;
  state.health.capture_source=captureSource;
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
assert.equal(element('selectedNumber').textContent,'武将资料未核实');
vm.runInContext(`{
  const row=state.armies[0],key=placeLoadoutKey(row);
  placeLoadouts.set(key,{data:{match:'exact_army_id',current_report_id:'team-label',items:[{battle_id:'team-label',player_name:row.player_name,union_id:row.union_id,event_at:900,data:{heroes:[{name:'曹操',position:1,level:40,red_stars:0}]}}]}});
  if(armyRosterLabel(row)!=='曹操40级(0红)')throw new Error('队伍入口应展示本队武将红度等级');
  placeLoadouts.delete(key);
}`,context);
vm.runInContext("state.armies[0].stale=true;setInspectorView('army');renderRoster();renderDetails()",context);
assert.equal(vm.runInContext('currentArmies().length',context),0);
assert.equal(vm.runInContext('selected',context),null);
assert.equal(element('listCount').textContent,'0 支');
assert.equal(element('rosterSelection').hidden,true);
assert.equal(element('armyDetails').hidden,true);
assert.equal(element('watchPanel').hidden,false);
reset();
vm.runInContext("renderDetails();state.armies=[];renderDetails()",context);
assert.equal(vm.runInContext('selected',context),null);
assert.equal(element('armyDetails').textContent,'');
reset({...base,reliable:true,health:{...base.health,messages:1},targets:[],candidates:[],
  config:{self:{name:'测试',union_name:'测试盟'},relations:{},state_rules:{}},
  armies:[{...base.armies[0],kind:'attack',from:[800,800],target:[1090,1000]}],
  alerts:[{army_id:'1',coord:[1090,1000],end:1100}]});
vm.runInContext(`{
  function draw(){}
  map.x=1010;map.y=925;map.scale=39;map.initialized=true;
  setMapScope('1090,1000','');
  if(map.x!==1010||map.y!==925||map.scale!==39)throw new Error('关注目标不应移动或缩放地图');
  selectArmy('1',{row:state.armies[0]});
  if(map.x!==1010||map.y!==925||map.scale!==39||!selectedIncoming(state.alerts))
    throw new Error('远距离来袭高亮应保持原缩放和位置');
}`,context);
const targetDrawing=fs.readFileSync(require('node:path').join(__dirname,'../static/app.js'),'utf8');
vm.runInContext(targetDrawing.slice(targetDrawing.indexOf('function focusTarget()'),targetDrawing.indexOf('function mapArmyPoint(')),context);
vm.runInContext(`{
  const scale=map.scale;
  focusWatchTarget('1090,1000');
  if(map.x!==1090||map.y!==1000||map.scale!==scale||selectedMarch!==null||selected!==null)
    throw new Error('点击目标应居中、清除队伍路线选择并保留缩放');
  selectArmy('1',{row:state.armies[0]});
  if(map.x!==1090||map.y!==1000||map.scale!==scale||!selectedIncoming(state.alerts))
    throw new Error('点击队伍应仅高亮路线并保留当前视图');
  const total=state.alerts.length;
  state.game_focus={event_id:'game-click-1',coord:[987,923],updated_at:now(),source:'dashen_click'};
  syncGameFocus();
  if(map.x!==987||map.y!==923||map.scale!==scale||alertFocus!=='987,923'||
     selected!==null||state.alerts.length!==total)
    throw new Error('游戏选中地块应居中关注并保留缩放及其他敌袭');
  setMapScope('989,922','');syncGameFocus();
  if(alertFocus!=='989,922')throw new Error('同一次游戏点击不能反复覆盖网页手动关注');
  state.game_focus.event_id='game-click-2';syncGameFocus();
  if(alertFocus!=='987,923')throw new Error('再次点击同一游戏地块也应同步关注');
  const beforeOldFocus=[map.x,map.y,map.scale].join(',');
  state.game_focus={...state.game_focus,event_id:'old-click',coord:[988,924],updated_at:now()-60};
  syncGameFocus();
  if(alertFocus!=='988,924')throw new Error('网页在后台停留后仍应接收本次游戏会话最后点击的地块');
  if([map.x,map.y,map.scale].join(',')!==beforeOldFocus)throw new Error('过期点选只恢复地块信息，不得把镜头拉回旧位置');
}`,context);
// Exercise the real canvas route functions, including the selected-route overlay.
const routeDrawing=fs.readFileSync(require('node:path').join(__dirname,'../static/app.js'),'utf8');
vm.runInContext(routeDrawing.slice(routeDrawing.indexOf('function mapArmyPoint('),routeDrawing.indexOf('function mapHit(')),context);
vm.runInContext(`
  const routeInk={strokes:[],arrows:[],widths:[]};
  const ctx={save(){},restore(){},beginPath(){},moveTo(){},lineTo(){},closePath(){},
    translate(){},rotate(){},setLineDash(){},stroke(){routeInk.strokes.push(this.strokeStyle);routeInk.widths.push(this.lineWidth);},
    fillRect(){},strokeRect(){},fill(){routeInk.arrows.push(this.fillStyle);}};
`,context);
vm.runInContext(routeDrawing.slice(routeDrawing.indexOf('function drawSelectedAttackRoute('),routeDrawing.indexOf('function drawMapLineups(')),context);
reset();vm.runInContext(`{
  const row={...state.armies[0],kind:'attack',target_side:'own',age:30,incoming:false,target_ownership_fresh:false};
  map.x=1010;map.y=925;map.scale=10;map.points=[];alertFocus=row.target.join(',');
  if(!drawMapArmy(row,1000,null,true)||routeInk.arrows.at(-1)!==colors.enemy||
     routeInk.strokes.some(color=>color!=='#fff'&&color!==colors.enemy))
    throw new Error('选中或过期的己方进攻应保持红色箭头和路线');
  routeInk.strokes=[];routeInk.arrows=[];
  row.target_side='other';
  drawMapArmy(row,1000,null,true);
  if(routeInk.arrows.at(-1)!=='#64748b'||routeInk.strokes.some(color=>color!=='#fff'&&color!=='#64748b'))
    throw new Error('关注或选中其他方进攻不能把路线染成己方敌袭红色');
  routeInk.widths=[];drawMapArmy(row,1000,null,false);
  if(routeInk.widths.some(width=>width!==1.3))throw new Error('只关注目标时不能加粗进攻路线');
}`,context);
console.log('UI state scenarios passed');
