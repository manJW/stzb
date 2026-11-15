'use strict';
const assert=require('node:assert/strict');
const {createWindowMode}=require('./app/window-mode');
function setup({maximized=false,saved={},selectMap,resetTopOnResize=false,taskbarDropsPin=false}={}){
  const window={bounds:{x:50,y:70,width:1250,height:817},maximized,minimized:false,destroyed:false,top:false,
    getNormalBounds(){return {...this.bounds};},isMaximized(){return this.maximized;},unmaximize(){this.maximized=false;},
    maximize(){this.maximized=true;},isMinimized(){return this.minimized;},restore(){this.minimized=false;},
    isDestroyed(){return this.destroyed;},isAlwaysOnTop(){return this.top;},setMinimumSize(w,h){this.minimum=[w,h];},setMaximizable(v){this.maximizable=v;},
    setAlwaysOnTop(v,level){this.top=v&&(!taskbarDropsPin||level==='pop-up-menu');this.topLevel=level;},setTitleBarOverlay(v){this.overlay=v;},setBounds(v){this.bounds={...v};if(resetTopOnResize)this.top=false;}};
  const events=[];let mapRequests=0;
  const mode=createWindowMode({window,saved:{maximized,...saved},
    screen:{getDisplayMatching:()=>({workArea:{x:0,y:0,width:1920,height:1040}})},
    selectMap:async()=>{mapRequests++;if(selectMap)await selectMap();},changed:v=>events.push(v)});
  mode.initialize();return {window,mode,events,requests:()=>mapRequests};
}
(async()=>{
  const s=setup(),original={...s.window.bounds};
  await s.mode.set(true);
  assert.equal(s.requests(),1);assert.equal(s.mode.compact,true);assert.equal(s.window.top,true);
  assert.deepEqual(s.window.minimum,[360,420]);assert.equal(s.window.maximizable,false);
  assert.equal(s.window.bounds.width,420);assert.equal(s.window.bounds.height,760);
  assert.equal(s.mode.snapshot().width,1250,'Small bounds must not overwrite the normal window');
  s.window.bounds={x:900,y:150,width:480,height:800};
  await s.mode.set(false);
  assert.deepEqual(s.window.bounds,original);assert.equal(s.window.top,false);assert.equal(s.window.maximizable,true);
  assert.deepEqual(s.window.minimum,[1080,680]);assert.deepEqual(s.mode.snapshot().compactBounds,{x:900,y:150,width:480,height:800});
  await s.mode.set(true);assert.equal(s.window.bounds.width,480,'Remember a resized compact window');
  const reopened=setup({saved:s.mode.snapshot()});
  assert.equal(reopened.mode.compact,true);assert.equal(reopened.window.top,true);assert.equal(reopened.window.bounds.width,480);
  const max=setup({maximized:true});await max.mode.set(true);assert.equal(max.window.maximized,false);
  await max.mode.set(false);assert.equal(max.window.maximized,true,'Restore the full window maximized state');
  const failing=setup({selectMap:async()=>{throw new Error('no map');}});
  await assert.rejects(failing.mode.set(true),/no map/);assert.equal(failing.mode.compact,false);assert.equal(failing.window.top,false);
  let finish;const pending=setup({selectMap:()=>new Promise(resolve=>{finish=resolve;})});
  const first=pending.mode.set(true);await pending.mode.set(true);assert.equal(pending.requests(),1);
  finish();await first;assert.equal(pending.mode.compact,true);
  const nativeResize=setup({resetTopOnResize:true});await nativeResize.mode.set(true);
  assert.equal(nativeResize.window.top,true,'Apply pinning after Windows changes the native window size');
  nativeResize.window.top=false;nativeResize.mode.ensurePinned();
  assert.equal(nativeResize.window.top,true,'Reassert native pinning after show/restore/focus changes');
  await nativeResize.mode.set(false);nativeResize.mode.ensurePinned();
  assert.equal(nativeResize.window.top,false,'The full window must stay unpinned');
  const taskbar=setup({taskbarDropsPin:true,saved:{compact:true}});
  assert.equal(taskbar.window.top,true,'Use a native level that is not ordered behind a non-topmost taskbar');
  assert.equal(taskbar.window.topLevel,'pop-up-menu');
  taskbar.window.top=false;taskbar.mode.ensurePinned();assert.equal(taskbar.window.top,true);
  await taskbar.mode.set(false);assert.equal(taskbar.window.top,false);
  console.log('PASS: native compact pinning, shared map selection, normal/compact bounds, restart persistence, maximized restore, failure and rapid toggles');
})().catch(e=>{console.error(e);process.exitCode=1;});
