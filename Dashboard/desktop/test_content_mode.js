'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const listeners=new Map(),classes=new Set();
const nodes=new Map([['.topbar',{hidden:false}],['.map-pane',{hidden:false}],['.roster-pane',{hidden:true}]]);
let watchClicks=0,lastKey,detailsVisible=false;
const context=vm.createContext({require:name=>{
  assert.equal(name,'electron');return {ipcRenderer:{on:(key,fn)=>listeners.set(key,fn),send(){}}};
},Event:class{constructor(type){this.type=type;}},KeyboardEvent:class{constructor(type,options){this.type=type;Object.assign(this,options);}},
document:{dispatchEvent:event=>{lastKey=event;},documentElement:{classList:{toggle:(name,value)=>value?classes.add(name):classes.delete(name)}},
  querySelector:selector=>nodes.get(selector),getElementById:id=>{
    if(id==='armyDetails')return {hidden:!detailsVisible};
    return {click(){if(id==='watchTab')watchClicks++;}};
  }},
window:{addEventListener(){}},setInterval(){}});
vm.runInContext(fs.readFileSync(path.join(__dirname,'app/content-preload.js'),'utf8'),context);
const mode=listeners.get('desktop:mode');
mode({}, {compact:true});mode({}, {compact:true});
assert.equal(classes.has('desktop-compact'),true);
assert.equal(nodes.get('.map-pane').hidden,true,'The compact native view must hide the real-time map itself');
assert.equal(nodes.get('.topbar').hidden,true);assert.equal(watchClicks,2);
detailsVisible=true;mode({}, {compact:true});
assert.equal(watchClicks,2,'A game-selected army must remain visible on entering compact mode');
assert.equal(lastKey.type,'desktop:mode-change','The page rerenders the same selected army in its compact layout');
mode({}, {compact:false});
assert.equal(classes.has('desktop-compact'),false);
assert.equal(nodes.get('.map-pane').hidden,false,'Returning to the full window restores the map');
assert.equal(nodes.get('.topbar').hidden,false);
assert.equal(nodes.get('.roster-pane').hidden,true,'A pane originally hidden must remain hidden');
listeners.get('desktop:clear-focus')();
assert.equal(lastKey.key,'Escape');assert.equal(lastKey.type,'keydown');
assert.equal(lastKey.bubbles,true);assert.equal(lastKey.cancelable,true);
let shellKey;const actions=[],routes=[];
vm.runInNewContext(fs.readFileSync(path.join(__dirname,'app/shell.js'),'utf8'),{
  document:{querySelectorAll:()=>[],addEventListener:(name,fn)=>{if(name==='keydown')shellKey=fn;}},
  window:{desktop:{onRoute(){},onMode(){},onStatus(){},onLoading(){},action:value=>actions.push(value),navigate:value=>routes.push(value)}}});
shellKey({key:'Escape',preventDefault(){}});
for(const extra of [{isComposing:true},{defaultPrevented:true},{ctrlKey:true},{altKey:true},{metaKey:true},{shiftKey:true}])
  shellKey({key:'Escape',preventDefault(){},...extra});
assert.deepEqual(actions,['clear-focus'],'Only unmodified Escape forwards from the desktop shell');
shellKey({key:'1',ctrlKey:true,preventDefault(){}});assert.deepEqual(routes,['map']);
console.log('PASS: compact content hides map/header, opens the sidebar, and restores original visibility across repeated mode messages');
