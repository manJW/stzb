'use strict';

const http = require('node:http');
const ROUTES = Object.freeze({map: '/battlefield', intel: '/', archive: '/archive', attendance: '/attendance'});

function routeForUrl(value, origin) {
  try {
    const url = new URL(value);
    if (url.origin !== origin || url.username || url.password) return null;
    return Object.keys(ROUTES).find(key => ROUTES[key] === url.pathname) || null;
  } catch { return null; }
}

function contentBounds(width, height, compact = false) {
  if (compact) return {x:0,y:32,width:Math.max(1,width),height:Math.max(1,height-32)};
  return {x: 184, y: 44, width: Math.max(1, width - 184), height: Math.max(1, height - 44)};
}

function compactWindowBounds(saved = {}, area) {
  const width = Math.min(area.width, Math.max(360, Number(saved.width) || 420));
  const height = Math.min(area.height, Math.max(420, Number(saved.height) || 760));
  const x = Number.isFinite(saved.x) ? saved.x : area.x + area.width - width - 16;
  const y = Number.isFinite(saved.y) ? saved.y : area.y + 48;
  return {width,height,x:Math.round(Math.min(Math.max(x,area.x),area.x+area.width-width)),
    y:Math.round(Math.min(Math.max(y,area.y),area.y+area.height-height))};
}

function windowBounds(saved, area) {
  const width = Math.min(area.width, Math.max(1080, Number(saved.width) || 1500));
  const height = Math.min(area.height, Math.max(680, Number(saved.height) || 950));
  const x = Number.isFinite(saved.x) ? saved.x : area.x + (area.width - width) / 2;
  const y = Number.isFinite(saved.y) ? saved.y : area.y + (area.height - height) / 2;
  return {width, height, x: Math.round(Math.min(Math.max(x, area.x), area.x + area.width - width)),
    y: Math.round(Math.min(Math.max(y, area.y), area.y + area.height - height))};
}

function probeService(origin, timeout = 2500) {
  return new Promise((resolve, reject) => {
    // Read a static service fingerprint, avoiding the archive lock during heavy queries.
    const request = http.get(`${origin}/battlefield`, {timeout}, response => {
      if (response.statusCode !== 200) { response.resume(); reject(new Error('本机服务尚未就绪')); return; }
      let bytes = 0, chunks = [];
      response.on('data', chunk => {
        bytes += chunk.length;
        if (bytes > 16 * 1024 * 1024) { response.destroy(new Error('本机响应超出限制')); return; }
        chunks.push(chunk);
      });
      response.on('error', reject);
      response.on('end', () => {
        try {
          const html = Buffer.concat(chunks).toString('utf8');
          if (!html.includes('<title>率土 · 战场观察</title>') || !html.includes('id="watchEntries"') || !html.includes('id="map"')) {
            throw new Error('此端口不是率土看板，请检查本机服务');
          }
          resolve({app: 'stzb-local-observer'});
        } catch (error) { reject(error); }
      });
    });
    request.on('timeout', () => request.destroy(new Error('连接本机服务超时')));
    request.on('error', reject);
  });
}

module.exports = {ROUTES, routeForUrl, contentBounds, windowBounds, compactWindowBounds, probeService};
