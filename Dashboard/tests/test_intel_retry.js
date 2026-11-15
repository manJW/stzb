const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync(require('node:path').join(__dirname,'../static/warroom.js'),'utf8');
const code=source.slice(source.indexOf('async function refreshSummary('),source.indexOf('function render(data)'));
const nodes=new Map();const node=id=>{if(!nodes.has(id))nodes.set(id,{textContent:'',innerHTML:'',value:'',hidden:true,contains:()=>false});return nodes.get(id);};
let requests=[],timers=new Map(),timerId=0,selected='a';
const context=vm.createContext({AbortController,Date,Math,Number,document:{hidden:false},
 $:node,number:x=>String(x),help:{personnel:'players'},filters:()=>({search:selected}),
 query:(kind,f,extra)=>kind+'?'+f.search+(extra?'&'+extra.offset:''),
 updateAdvancedFilterCount(){},render(){node('intelTable').innerHTML='valid-table';},
 api:(path,body,options)=>new Promise((resolve,reject)=>{requests.push({path,resolve,reject,options});}),
 setTimeout:(fn,delay)=>{timers.set(++timerId,{fn,delay});return timerId;},clearTimeout:id=>timers.delete(id),
});
vm.runInContext(`let summaryRequest=null,listRequest=null,displayedPath='',retryTimer=null,retryAttempt=0,lastQueryFailed=false;
let view='personnel',offset=0,limit=100,rows=[],requestId=0;
${code}`,context);
const flush=async()=>{for(let i=0;i<5;i++)await Promise.resolve();};
(async()=>{
 const first=vm.runInContext('load()',context);vm.runInContext('load()',context);
 assert.equal(requests.length,1,'duplicate list loads share one pending request');
 requests.shift().resolve({items:[{id:1}],total:1});await first;
 assert.equal(node('intelTable').innerHTML,'valid-table');
 requests.shift().resolve({observed_names:1,observed_linked_armies:0,observed_inferred_only_armies:0,observed_armies:0});await flush();
 const next=vm.runInContext('load()',context);const failure=Object.assign(Error('busy'),{transient:true});requests.shift().reject(failure);await next;
 assert.equal(node('intelTable').innerHTML,'valid-table','503 retains the last successful table');
 assert.ok(node('queryStatus').textContent.includes('保留上次结果'));
 let retry=[...timers.values()].find(t=>t.delay===2000);assert.ok(retry,'retry scheduled');
 timers.delete([...timers].find(([id,t])=>t===retry)[0]);retry.fn();await flush();requests.shift().resolve({items:[{id:2}],total:1});await flush();
 assert.equal(vm.runInContext('rows[0].id',context),2,'automatic retry replaces old rows after success');
 const stale=vm.runInContext('load()',context);const old=requests.shift();selected='b';const changed=vm.runInContext('load()',context);const current=requests.shift();
 assert.equal(old.options.signal.aborted,true,'new conditions cancel the previous request');
 current.resolve({items:[{id:3}],total:1});await changed;
 old.resolve({items:[{id:999}],total:1});await stale;
 assert.equal(vm.runInContext('rows[0].id',context),3,'late responses do not overwrite new filters');
 // Initial failures also back off; they must not reset to two seconds on every retry.
 selected='c';const cold=vm.runInContext('load()',context);requests.find(r=>r.path==='personnel?c&0').reject(failure);requests=requests.filter(r=>r.path!=='personnel?c&0');await cold;
 const two=[...timers.values()].find(t=>t.delay===2000);timers.delete([...timers].find(([id,t])=>t===two)[0]);two.fn();await flush();
 requests.find(r=>r.path==='personnel?c&0').reject(failure);requests=requests.filter(r=>r.path!=='personnel?c&0');await flush();
 assert.ok([...timers.values()].some(t=>t.delay===4000),'retry delay increases after repeated failures');
 console.log('Intelligence queries: deduplication, preserved rows, retry/backoff, cancellation and stale response isolation passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
