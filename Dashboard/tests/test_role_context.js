'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
(async()=>{
 const nodes=new Map();const node=key=>{if(!nodes.has(key))nodes.set(key,{textContent:'',hidden:false});return nodes.get(key);};
 const groupLabel=node('groupLabel');node('group').closest=()=>groupLabel;
 let next={ready:true,profile_id:'one',statistics_title:'流浪军战绩',own_label:'同聚义',is_wanderer:true,
  has_organization:true,organization_label:'聚义',identity_label:'流浪军',report_scope:'所属聚义可访问战报'};
 let tick,reloads=0;const body={dataset:{}};
 const doc={hidden:false,body,querySelector:node,querySelectorAll:()=>[node('attendanceLink')],
  getElementById:node,addEventListener(){}};
 const ctx={document:doc,location:{pathname:'/attendance',reload(){reloads++;}},
  fetch:async()=>({ok:true,json:async()=>({...next})}),setInterval:fn=>{tick=fn;}};
 vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../static/role-context.js'),'utf8'),ctx);
 await new Promise(setImmediate);
 assert.equal(node('h1').textContent,'流浪军战绩');
 assert.equal(node('#relation option[value="ally"]').textContent,'同聚义');
 assert.equal(node('[data-column="joined_at"]').textContent,'加入聚义时间');
 next={...next,ready:false,profile_id:'two'};await tick();assert.equal(reloads,0,'Unverified role changes must not activate another scope');
 assert.equal(body.dataset.rolePending,'true','Cached views are covered while the account is unverified');
 next={...next,ready:true};await tick();assert.equal(reloads,1,'A verified role switch reloads the cached account view');
 next={...next,profile_id:'one',has_organization:false,report_scope:'本人可访问战报'};await tick();
 assert.equal(groupLabel.hidden,true);assert.equal(node('roleScopeNote').textContent,'本人可访问战报');
 next={...next,context_id:'same-role-new-union'};await tick();assert.equal(reloads,2,'The same role changing organization also invalidates cached views');
 console.log('PASS: wanderer labels, personal scope, and verified role changes');
})().catch(error=>{console.error(error);process.exitCode=1;});
