'use strict';
const {ipcRenderer} = require('electron');
let previous = '';
const compactVisibility = new Map();
function readStatus() {
  const text = document.querySelector('#connectionText')?.textContent || document.querySelector('#connectionStatus')?.textContent || document.querySelector('#archiveStatus')?.textContent || '本机已连接';
  const dot = document.querySelector('#liveDot');
  const label = document.querySelector('#connectionStatus, #archiveStatus');
  const tone = dot?.classList.contains('error') ? 'error' : (dot?.classList.contains('live') || label?.dataset.live === 'true' ? 'live' : 'waiting');
  const profileLabel = document.querySelector('#identity, #profileLabel')?.textContent || '';
  const value = {text: text.trim(), tone,
    statisticsTitle: document.body?.dataset?.statisticsTitle || '',
    identity: document.querySelector('#ownName')?.textContent?.trim() || profileLabel.split('·').at(-1).trim(),
    union: document.querySelector('#ownUnion')?.textContent?.trim() || ''};
  if(document.body?.dataset?.rolePending==='true')Object.assign(value,{text:'正在自动识别当前账号',tone:'waiting',identity:'正在识别',union:''});
  const serialized = JSON.stringify(value);
  if (serialized !== previous) { previous = serialized; ipcRenderer.send('desktop:content-status', value); }
}
ipcRenderer.on('desktop:settings', () => document.getElementById('settingsButton')?.click());
ipcRenderer.on('desktop:clear-focus', () => document.dispatchEvent(new KeyboardEvent('keydown',
  {key:'Escape',bubbles:true,cancelable:true})));
ipcRenderer.on('desktop:read-status', () => { previous = ''; readStatus(); });
ipcRenderer.on('desktop:mode', (_event, value) => {
  const compact=value?.compact === true;
  document.documentElement.classList.toggle('desktop-compact',compact);
  if (compact) {
    for (const selector of ['.topbar','.map-pane','.roster-pane']) {
      const node=document.querySelector(selector);
      if (!node) continue;
      if (!compactVisibility.has(node)) compactVisibility.set(node,node.hidden);
      node.hidden=true;
    }
  } else {
    for (const [node,hidden] of compactVisibility) node.hidden=hidden;
    compactVisibility.clear();
  }
  if (compact) {
    document.getElementById('closeMapPlacePopup')?.click();
    document.getElementById('closeAlertPopup')?.click();
    // Keep a game-selected army visible when switching to the pinned window.
    if(document.getElementById('armyDetails')?.hidden!==false)document.getElementById('watchTab')?.click();
  }
  document.dispatchEvent(new Event('desktop:mode-change'));
});
window.addEventListener('DOMContentLoaded', () => {
  document.documentElement.classList.add('desktop-workspace');
  const heading = document.querySelector('.topbar .brand h1');
  if (heading) heading.textContent = '实时地图';
  // Reuse the existing page controls and business logic; only the shell changes.
  readStatus();
  setInterval(readStatus, 2000);
});
