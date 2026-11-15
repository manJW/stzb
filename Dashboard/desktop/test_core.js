'use strict';
const assert = require('node:assert/strict');
const http = require('node:http');
const {routeForUrl, contentBounds, windowBounds, compactWindowBounds, probeService} = require('./app/core');

async function run() {
  const origin = 'http://127.0.0.1:8787';
  assert.equal(routeForUrl(origin + '/?battle_id=5912345', origin), 'intel');
  assert.equal(routeForUrl(origin + '/archive?search=时运', origin), 'archive');
  for (const url of ['https://example.com/battlefield', 'http://127.0.0.1:8788/battlefield',
    'http://127.0.0.1:8787.evil.com/battlefield', 'http://name:secret@127.0.0.1:8787/battlefield',
    origin + '/api/stop', 'file:///C:/Windows/notepad.exe', 'javascript:alert(1)']) {
    assert.equal(routeForUrl(url, origin), null, url);
  }
  assert.deepEqual(contentBounds(1500, 950), {x:184,y:44,width:1316,height:906});
  assert.deepEqual(contentBounds(420,760,true),{x:0,y:32,width:420,height:728});
  assert.deepEqual(compactWindowBounds({}, {x:0,y:0,width:1920,height:1040}),{x:1484,y:48,width:420,height:760});
  assert.deepEqual(compactWindowBounds({x:9000,y:-300,width:800,height:900},{x:-1920,y:0,width:1280,height:720}),
    {x:-1440,y:0,width:800,height:720});
  const bounds = windowBounds({x:6000,y:-400,width:1600,height:1000}, {x:0,y:0,width:1366,height:768});
  assert.deepEqual(bounds, {x:0,y:0,width:1366,height:768});
  const left = windowBounds({x:-2000,y:200,width:1200,height:800}, {x:-1920,y:0,width:1920,height:1080});
  assert.equal(left.x, -1920); assert.equal(left.y, 200);
  let mode = 'ok';
  const server = http.createServer((_req, res) => {
    if (mode === 'wait') return;
    res.writeHead(mode === 'failure' ? 500 : 200, {'Content-Type':'text/html;charset=utf-8'});
    res.end(mode === 'wrong' ? '<html>another app</html>' : '<title>率土 · 战场观察</title><canvas id="map"></canvas><section id="watchEntries"></section>');
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const local = `http://127.0.0.1:${server.address().port}`;
  try {
    assert.equal((await probeService(local)).app, 'stzb-local-observer');
    mode='wrong'; await assert.rejects(probeService(local), /不是率土/);
    mode='failure'; await assert.rejects(probeService(local), /尚未就绪/);
    mode='wait'; await assert.rejects(probeService(local, 50), /超时/);
  } finally { await new Promise(resolve => server.close(resolve)); }
  console.log('PASS: local origin isolation, battle links, window restore, content bounds, service identity/errors/timeout');
}
run().catch(error => {console.error(error);process.exitCode=1;});
