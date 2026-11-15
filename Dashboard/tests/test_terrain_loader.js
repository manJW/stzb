'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
let requests=0;
const context=vm.createContext({AbortSignal,Date,Uint8Array,
 fetch:async()=>{requests++;return {ok:true,headers:{get:key=>key==='X-Map-Size'?'10':'34'},
   arrayBuffer:async()=>new Uint8Array(100).buffer};}});
const source=fs.readFileSync(path.join(__dirname,'../static/app.js'),'utf8');
vm.runInContext(source.slice(0,source.indexOf('const displayPreferenceKey')),context);
vm.runInContext(`let draws=0;function draw(){draws++;}
 state={config:{self:{user_id:'1'}},health:{},map_config_id:34,armies:[{id:'keep'}]};`,context);
(async()=>{
 await vm.runInContext('loadMapTerrain()',context);
 assert.equal(vm.runInContext('mapTerrain.data.length',context),100);
 assert.equal(vm.runInContext('draws',context),1);
 await vm.runInContext('loadMapTerrain()',context);
 assert.equal(requests,1,'Static terrain should be loaded once, not per polling cycle');
 vm.runInContext('state.health.scope_pending=true',context);
 await vm.runInContext('loadMapTerrain()',context);
 assert.equal(vm.runInContext('mapTerrain.data',context),null);
 assert.equal(requests,1);
 context.fetch=async()=>({ok:false});
 vm.runInContext('state.health.scope_pending=false;state.config.self.user_id="2"',context);
 await vm.runInContext('loadMapTerrain()',context);
 assert.equal(vm.runInContext('mapTerrain.data',context),null);
 assert.equal(vm.runInContext('state.armies[0].id',context),'keep');
 assert.ok(vm.runInContext('mapTerrain.retryAt>Date.now()',context));
 console.log('Terrain load caching, scope changes and non-blocking failure passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
