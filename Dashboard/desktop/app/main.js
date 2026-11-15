'use strict';

const {app, BrowserWindow, WebContentsView, Menu, Tray, nativeImage, nativeTheme, ipcMain, shell, screen, dialog, session} = require('electron');
const fs = require('node:fs');
const path = require('node:path');
const {pathToFileURL} = require('node:url');
const {spawn} = require('node:child_process');
const {ROUTES, routeForUrl, contentBounds, windowBounds, probeService} = require('./core');
const {createWindowMode} = require('./window-mode');

app.setName('率土战场');
const configPath = path.join(__dirname, 'client-config.json');
const config = fs.existsSync(configPath) ? JSON.parse(fs.readFileSync(configPath, 'utf8')) : {};
const dashboardRoot = path.resolve(__dirname, config.dashboardRoot || '../../../Dashboard');
const workspaceRoot = config.workspaceRoot ? path.resolve(__dirname, config.workspaceRoot) : dashboardRoot;
const runtime = path.join(workspaceRoot, 'runtime', 'desktop');
fs.mkdirSync(runtime, {recursive: true});
app.setPath('userData', path.join(runtime, 'profile'));
app.setPath('logs', path.join(runtime, 'logs'));
app.setAppUserModelId('STLab.Battlefield.Desktop');
let origin = 'http://127.0.0.1:8787';
const shellUrl = pathToFileURL(path.join(__dirname, 'shell.html')).href;
const stateFile = path.join(runtime, 'window.json');
let saved = {};
try { saved = JSON.parse(fs.readFileSync(stateFile, 'utf8')); } catch {}
let mainWindow, tray, windowMode, activeRoute = '', quitting = false, starting = null, switchSerial = 0;
let statisticsTitle = '同盟考勤';
const views = new Map();
// Scoped rules win ties against the existing page styles without modifying the web version.
const themeCss = `@scope (html.desktop-workspace) { ${fs.readFileSync(path.join(__dirname, 'workspace.css'), 'utf8')} }`;
const log = message => fs.appendFileSync(path.join(runtime, 'desktop.log'), `${new Date().toISOString()} ${message}\n`);
const send = (channel, value) => { if (mainWindow && !mainWindow.isDestroyed()) mainWindow.webContents.send(channel, value); };

function validShell(event) { return mainWindow && event.sender === mainWindow.webContents && event.senderFrame.url === shellUrl; }
function saveWindow() {
  if (!mainWindow || mainWindow.isDestroyed()) return;
  try { fs.writeFileSync(stateFile, JSON.stringify({...windowMode.snapshot(), route: activeRoute}, null, 2)); }
  catch (error) { log(`window state: ${error.message}`); }
}

async function ensureService() {
  if (starting) return starting;
  starting = (async () => {
    if (config.installed) {
      const {installedService} = require('./installed-service');
      origin = await installedService({workspace:workspaceRoot, launcher:path.resolve(__dirname,config.serviceLauncher), log, status:text=>send('desktop:status',{text,tone:'waiting'})});
      return;
    }
    try { await probeService(origin); return; } catch (error) {
      if (/不是率土/.test(error.message)) throw error;
    }
    const launcher = path.join(dashboardRoot, 'Start-Dashboard.ps1');
    if (!fs.existsSync(launcher)) throw new Error('找不到本机看板服务，请检查客户端与 Dashboard 的目录位置。');
    send('desktop:status', {text: '正在启动本机服务', tone: 'waiting'});
    const powershell = path.join(process.env.SystemRoot || 'C:\\Windows', 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe');
    const child = spawn(powershell, ['-NoProfile', '-File', launcher], {cwd: dashboardRoot, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe']});
    child.stdout.on('data', () => {});
    child.stderr.on('data', chunk => log(`service: ${String(chunk).trim()}`));
    let launchError = null;
    child.on('error', error => { launchError = error; });
    child.on('exit', code => { if (code) launchError = new Error('本机服务启动失败，请打开日志查看原因'); });
    const deadline = Date.now() + 45000;
    while (Date.now() < deadline) {
      if (launchError) throw launchError;
      try { await probeService(origin); return; } catch (error) {
        if (/不是率土/.test(error.message)) throw error;
      }
      await new Promise(resolve => setTimeout(resolve, 500));
    }
    throw new Error('本机服务未能启动。请检查游戏采集环境，或打开日志查看原因。');
  })().finally(() => { starting = null; });
  return starting;
}

function layout() {
  if (!mainWindow || mainWindow.isDestroyed()) return;
  const [width, height] = mainWindow.getContentSize();
  for (const {view} of views.values()) view.setBounds(contentBounds(width, height,windowMode?.compact));
}

async function openExternal(url) {
  try {
    const target = new URL(url);
    if (target.protocol === 'https:' && !target.username && !target.password) await shell.openExternal(target.href);
  } catch (error) { log(`external link: ${error.message}`); }
}

function newView(key) {
  const view = new WebContentsView({webPreferences: {
    preload: path.join(__dirname, 'content-preload.js'), partition: 'persist:stzb-workspace',
    nodeIntegration: false, contextIsolation: true, sandbox: true, webSecurity: true,
    backgroundThrottling: key !== 'map', spellcheck: false,
  }});
  view.setBackgroundColor('#f7f8fa');
  mainWindow.contentView.addChildView(view);
  view.setVisible(false);
  const entry = {view, ready: false, loading: null};
  views.set(key, entry);
  layout();
  const contents = view.webContents;
  contents.on('before-input-event', (event, input) => {
    if (input.type === 'keyDown' && input.control && ['1', '2', '3', '4'].includes(input.key)) {
      event.preventDefault(); showRoute(['map', 'intel', 'archive', 'attendance'][Number(input.key) - 1]).catch(showError);
    }
  });
  contents.on('dom-ready', () => {
    contents.insertCSS(themeCss).catch(error => log(error.message));
    contents.send('desktop:mode',{compact:Boolean(windowMode?.compact)});
  });
  contents.on('will-navigate', (event, url) => {
    const route = routeForUrl(url, origin);
    if (!route) { event.preventDefault(); return; }
    if (route !== key) { event.preventDefault(); showRoute(route, url).catch(showError); }
  });
  contents.on('will-redirect', (event, url) => { if (!routeForUrl(url, origin)) event.preventDefault(); });
  contents.setWindowOpenHandler(({url}) => {
    const route = routeForUrl(url, origin);
    if (route) showRoute(route, url).catch(showError);
    else openExternal(url);
    return {action: 'deny'};
  });
  contents.on('render-process-gone', (_event, details) => {
    log(`renderer ${key}: ${details.reason}`);
    entry.ready = false;
    if (key === activeRoute) showError(new Error('页面意外停止，请点击重新连接。'));
  });
  contents.on('unresponsive', () => { if (key === activeRoute) send('desktop:status', {text: '页面正在响应', tone: 'waiting'}); });
  contents.on('console-message', (_event, level, message) => { if (level >= 2) log(`${key}: ${message}`); });
  return entry;
}

async function showRoute(key, url) {
  if (!ROUTES[key]) return;
  if (windowMode?.compact && key !== 'map') await windowMode.set(false);
  const serial = ++switchSerial;
  send('desktop:route', key);
  const entry = views.get(key) || newView(key);
  if (!entry.ready || (url && url !== entry.view.webContents.getURL())) {
    send('desktop:loading', {show: true, text: '正在读取本机数据'});
    if (!entry.loading) entry.loading = entry.view.webContents.loadURL(url || origin + ROUTES[key]).finally(() => { entry.loading = null; });
    await entry.loading;
    entry.ready = true;
  }
  if (serial !== switchSerial) return;
  activeRoute = key;
  log(`route ready: ${key}`);
  for (const [route, {view}] of views) view.setVisible(route === key);
  entry.view.webContents.send('desktop:read-status');
  send('desktop:loading', {show: false});
  saveWindow();
}

function showError(error) {
  log(`error: ${error.stack || error.message}`);
  for (const {view} of views.values()) view.setVisible(false);
  send('desktop:loading', {show: true, error: true, text: error.message || '本机服务暂未连接'});
  send('desktop:status', {text: '连接待恢复', tone: 'error'});
}

function restoreWindow() { if (!mainWindow) return; if (mainWindow.isMinimized()) mainWindow.restore(); mainWindow.show(); mainWindow.focus(); }

function updateTray() {
  if (!tray) return;
  tray.setContextMenu(Menu.buildFromTemplate([
    {label:'打开率土战场',click:restoreWindow},
    {label:'小窗置顶',type:'checkbox',checked:Boolean(windowMode?.compact),click:() => {
      windowMode.set(!windowMode.compact).then(restoreWindow).catch(showError);
    }},
    {label:'实时地图',click:() => { restoreWindow();showRoute('map').catch(showError); }},
    {label:statisticsTitle,click:() => { restoreWindow();showRoute('attendance').catch(showError); }},
    {label:'赛季档案',click:() => { restoreWindow();showRoute('archive').catch(showError); }},
    {type:'separator'},
    {label:'退出客户端（保留后台采集）',click:() => { quitting=true;app.quit(); }},
  ]));
}

function createWindow() {
  const primary = screen.getPrimaryDisplay().workArea;
  const requested = Number.isFinite(saved.x) && Number.isFinite(saved.y) ? screen.getDisplayNearestPoint({x: saved.x, y: saved.y}).workArea : primary;
  mainWindow = new BrowserWindow({...windowBounds(saved, requested), minWidth: 1080, minHeight: 680,
    title: '率土战场', icon: path.join(__dirname, 'icon.png'), show: false,
    titleBarStyle: 'hidden', titleBarOverlay: {color: '#f1eee7', symbolColor: '#526079', height: 43},
    backgroundColor: '#f7f8fa', autoHideMenuBar: true,
    webPreferences: {preload: path.join(__dirname, 'shell-preload.js'), nodeIntegration: false, contextIsolation: true, sandbox: true},
  });
  mainWindow.webContents.on('will-navigate', event => event.preventDefault());
  windowMode=createWindowMode({window:mainWindow,screen,saved,selectMap:() => showRoute('map'),changed:value => {
    send('desktop:mode',value);
    views.get('map')?.view.webContents.send('desktop:mode',value);
    layout();saveWindow();updateTray();
    log(`compact ${value.compact?'on':'off'}; alwaysOnTop=${mainWindow.isAlwaysOnTop()}`);
  }});
  windowMode.initialize();
  const maintainCompactPin = () => {
    windowMode.ensurePinned();
    if(windowMode.compact)log(`compact visible; alwaysOnTop=${mainWindow.isAlwaysOnTop()}`);
  };
  mainWindow.on('show',maintainCompactPin);
  mainWindow.on('restore',maintainCompactPin);
  mainWindow.on('blur',maintainCompactPin);
  mainWindow.webContents.on('console-message', (_event, level, message) => { if (level >= 2) log(`shell: ${message}`); });
  mainWindow.webContents.setWindowOpenHandler(() => ({action: 'deny'}));
  mainWindow.on('resize', layout);
  mainWindow.on('close', event => {
    saveWindow();
    if (!quitting) { event.preventDefault(); mainWindow.hide(); }
  });
  mainWindow.on('closed', () => {
    for (const {view} of views.values()) if (!view.webContents.isDestroyed()) view.webContents.close();
    views.clear(); mainWindow = null;
  });
  mainWindow.once('ready-to-show', () => { if (!windowMode.compact && saved.maximized) mainWindow.maximize(); mainWindow.show(); });
  mainWindow.webContents.once('did-finish-load', async () => {
    send('desktop:mode',{compact:windowMode.compact});
    try { await ensureService(); await showRoute(windowMode.compact?'map':ROUTES[saved.route] ? saved.route : 'map'); }
    catch (error) { showError(error); }
  });
  mainWindow.loadFile(path.join(__dirname, 'shell.html'));
  tray = new Tray(nativeImage.createFromPath(path.join(__dirname, 'icon.png')));
  tray.setToolTip('率土战场 · 本机采集');
  updateTray();
  tray.on('double-click', restoreWindow);
}

ipcMain.handle('desktop:navigate', async (event, key) => {
  if (!validShell(event) || !ROUTES[key]) return;
  try { await showRoute(key); } catch (error) { showError(error); }
});
ipcMain.handle('desktop:action', async (event, action) => {
  if (!validShell(event)) return;
  try {
    if (action === 'settings') { await showRoute('map'); views.get('map').view.webContents.send('desktop:settings'); }
    if (action === 'compact') await windowMode.set(!windowMode.compact);
      if (action === 'clear-focus' && activeRoute === 'map') views.get('map')?.view.webContents.send('desktop:clear-focus');
    if (action === 'browser') await shell.openExternal(origin + (ROUTES[activeRoute] || '/battlefield'));
    if (action === 'logs') await shell.openPath(runtime);
    if (action === 'retry') {
      await ensureService();
      const entry = views.get(activeRoute);
      if (entry) entry.ready = false;
      await showRoute(activeRoute || 'map');
    }
  } catch (error) { showError(error); }
});
ipcMain.on('desktop:content-status', (event, value) => {
  const entry = views.get(activeRoute);
  if (!entry || event.sender !== entry.view.webContents || !routeForUrl(event.senderFrame.url, origin)) return;
  if (!value || typeof value !== 'object') return;
  if (['同盟考勤','流浪军战绩','个人战绩'].includes(value.statisticsTitle) && statisticsTitle !== value.statisticsTitle) {
    statisticsTitle = value.statisticsTitle;updateTray();
  }
  send('desktop:status', {text: String(value.text || '').slice(0, 80), tone: ['live', 'waiting', 'error'].includes(value.tone) ? value.tone : 'waiting',
    statisticsTitle,
    identity: String(value.identity || '').slice(0, 80), union: String(value.union || '').slice(0, 80)});
});

if (!app.requestSingleInstanceLock()) { app.quit(); }
else {
  app.on('second-instance', restoreWindow);
  app.on('before-quit', () => { quitting = true; saveWindow(); });
  app.whenReady().then(() => {
    nativeTheme.themeSource = 'light';
    Menu.setApplicationMenu(null);
    const workspaceSession = session.fromPartition('persist:stzb-workspace');
    workspaceSession.setPermissionRequestHandler((_contents, _permission, callback) => callback(false));
    workspaceSession.setPermissionCheckHandler(() => false);
    workspaceSession.webRequest.onBeforeRequest((details, callback) => {
      const allowed = details.url.startsWith(origin + '/') || details.url.startsWith('blob:' + origin + '/') || details.url.startsWith('data:');
      callback({cancel: !allowed});
    });
    createWindow();
  }).catch(error => { dialog.showErrorBox('率土战场启动失败', error.message); app.quit(); });
}
