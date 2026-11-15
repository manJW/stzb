'use strict';
const {contextBridge, ipcRenderer} = require('electron');
contextBridge.exposeInMainWorld('desktop', {
  navigate: key => ipcRenderer.invoke('desktop:navigate', key),
  action: key => ipcRenderer.invoke('desktop:action', key),
  onRoute: callback => ipcRenderer.on('desktop:route', (_event, value) => callback(value)),
  onStatus: callback => ipcRenderer.on('desktop:status', (_event, value) => callback(value)),
  onLoading: callback => ipcRenderer.on('desktop:loading', (_event, value) => callback(value)),
  onMode: callback => ipcRenderer.on('desktop:mode', (_event, value) => callback(value)),
});
