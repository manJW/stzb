const assert=require('node:assert/strict');
const http=require('node:http');
const path=require('node:path');
const {probeInstance,installedService}=require('./app/installed-service');
(async()=>{
  const id='a'.repeat(32);let wrong=false,programId='app-NEW';
  const server=http.createServer((req,res)=>{res.setHeader('Content-Type','application/json');res.end(JSON.stringify({app:'stzb-local-observer',instance_id:wrong?'b'.repeat(32):id,local_install:true,program_id:programId}));});
  await new Promise(r=>server.listen(0,'127.0.0.1',r));
  try{const origin='http://127.0.0.1:'+server.address().port;await probeInstance(origin,id,2000,'app-NEW');programId='app-OLD';await assert.rejects(probeInstance(origin,id,2000,'app-NEW'),/旧程序版本/);wrong=true;await assert.rejects(probeInstance(origin,id),/其他安装目录/);}
  finally{await new Promise(r=>server.close(r));}
  const native=require('./app/portable-service'),original=native.nativeService;
  const workspace=path.resolve('测试 安装/Workspace'),launcher=path.resolve('测试 安装/app-'+'A'.repeat(16)+'/Start-Installed.ps1');
  const confirmDriver=async()=>false,openDriver=async()=>'';
  let received;
  native.nativeService=async options=>{received=options;return 'http://127.0.0.1:43210';};
  try{
    assert.equal(await installedService({workspace,launcher,confirmDriver,openDriver}),'http://127.0.0.1:43210');
    assert.equal(received.workspace,workspace);assert.equal(received.program,path.dirname(launcher));
    assert.equal(received.distribution,'installed');assert.equal(received.confirmDriver,confirmDriver);assert.equal(received.openDriver,openDriver);
  }finally{native.nativeService=original;}
  console.log('PASS: installed client rejects foreign instances and stale program versions');
})().catch(error=>{console.error(error);process.exitCode=1;});
