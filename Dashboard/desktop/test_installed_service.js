const assert=require('node:assert/strict');
const http=require('node:http');
const {probeInstance}=require('./app/installed-service');
(async()=>{
  const id='a'.repeat(32);let wrong=false,programId='app-NEW';
  const server=http.createServer((req,res)=>{res.setHeader('Content-Type','application/json');res.end(JSON.stringify({app:'stzb-local-observer',instance_id:wrong?'b'.repeat(32):id,local_install:true,program_id:programId}));});
  await new Promise(r=>server.listen(0,'127.0.0.1',r));
  try{const origin='http://127.0.0.1:'+server.address().port;await probeInstance(origin,id,2000,'app-NEW');programId='app-OLD';await assert.rejects(probeInstance(origin,id,2000,'app-NEW'),/旧程序版本/);wrong=true;await assert.rejects(probeInstance(origin,id),/其他安装目录/);}
  finally{await new Promise(r=>server.close(r));}
  console.log('PASS: installed client rejects foreign instances and stale program versions');
})().catch(error=>{console.error(error);process.exitCode=1;});
