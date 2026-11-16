'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),os=require('node:os'),http=require('node:http'),net=require('node:net');
const {captureProgram,owns,availablePort,stopPortableService,portableService,legacyInstalledOwner,launchLock}=require('./app/portable-service');
(async()=>{
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'stzb-portable-test-'));
  const workspace=path.join(root,'用户 空格','Workspace'),program=path.join(root,'Program','app-'+'A'.repeat(16));
  fs.mkdirSync(path.join(workspace,'runtime'),{recursive:true});
  const capture=path.join(root,'custom capture','dumpcap.exe');fs.mkdirSync(path.dirname(capture));fs.writeFileSync(capture,'test-only');
  let driver=true;
  const run=async(exe,args)=>{
    if(args.includes('/s'))return 'HKEY_LOCAL_MACHINE\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\{custom-guid}\r\n    DisplayName REG_SZ Wireshark\r\n';
    if(args[1]?.includes('{custom-guid}')&&args.at(-1)==='InstallLocation')return '    InstallLocation    REG_SZ    '+path.dirname(capture)+'\r\n';
    if(args[1]?.includes('Services\\npcap'))return driver?'HKEY_LOCAL_MACHINE\\SYSTEM\\CurrentControlSet\\Services\\npcap':'';
    return '';
  };
  try{
    assert.equal(await captureProgram(workspace,run),capture,'Custom Wireshark drive/GUID key must be discovered');
    driver=false;assert.equal(await captureProgram(workspace,run),null,'Capture executable alone does not provide a driver');
    const id='a'.repeat(32),token='b'.repeat(64);let stopped=0;
    const server=http.createServer((req,res)=>{
      res.setHeader('Content-Type','application/json');res.setHeader('Connection','close');
      if(req.url==='/api/stop'){assert.equal(req.headers['x-local-stop'],token);stopped++;res.end('{}');server.close();}
      else res.end(JSON.stringify({app:'stzb-local-observer',local_install:true,program_id:path.basename(program),instance_id:id}));
    });
    await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
    const record={port:server.address().port,instance_id:id,stop_token:token,pid:12345};
    const owner={program,workspace,instance_id:id};
    fs.writeFileSync(path.join(workspace,'runtime','service.json'),JSON.stringify(record));
    fs.writeFileSync(path.join(workspace,'runtime','portable-owner.json'),JSON.stringify({...owner,workspace:path.join(root,'previous-computer','Workspace')}));
    try{
      assert.equal(owns(owner,workspace,program,id),true);
      assert.equal(owns(owner,path.join(root,'different','Workspace'),program,id),false);
      await assert.rejects(stopPortableService({workspace,program,isAlive:()=>false}),/其他目录/);
      await assert.rejects(portableService({workspace,program,log(){},status(){}}),/其他目录/);
      assert.equal(stopped,0,'Copied runtime must never stop the original installation');
      fs.writeFileSync(path.join(workspace,'runtime','portable-owner.json'),JSON.stringify({...owner,pid:record.pid,instance_id:null}));
      const reused=await portableService({workspace,program,log(){},status(){},confirmDriver(){throw Error('Existing backend must be reused');}});
      assert.equal(reused,'http://127.0.0.1:'+record.port,'Recover the owned startup after a client interruption');
      await stopPortableService({workspace,program,isAlive:()=>false});assert.equal(stopped,1);
    }finally{if(server.listening)await new Promise(resolve=>server.close(resolve));}
    const blocker=net.createServer();
    const blocked=await new Promise(resolve=>{blocker.once('error',()=>resolve(false));blocker.listen(8787,'127.0.0.1',()=>resolve(true));});
    try{const port=await availablePort();assert.ok(port>=1024&&port<=65535);assert.notEqual(port,8787);}
    finally{if(blocked)await new Promise(resolve=>blocker.close(resolve));}
    const installRoot=path.join(root,'旧版 安装'),installedWorkspace=path.join(installRoot,'Workspace'),installedProgram=path.join(installRoot,'app-'+'A'.repeat(16));
    const service={record:{pid:12345,instance_id:id},info:{program_id:path.basename(installedProgram)}};
    const processInfo={ProcessId:12345,ExecutablePath:path.join(installedProgram,'Python','python.exe'),CommandLine:`python.exe server.py --local-install "${installedWorkspace}" --port 8787`};
    assert.equal(legacyInstalledOwner(service,installedWorkspace,installedProgram,processInfo).program,installedProgram);
    assert.equal(legacyInstalledOwner(service,path.join(root,'copied','Workspace'),path.join(root,'copied',path.basename(installedProgram)),processInfo),null);
    assert.equal(legacyInstalledOwner(service,installedWorkspace,installedProgram,{...processInfo,ExecutablePath:path.join(root,'foreign','python.exe')}),null);
    assert.equal(legacyInstalledOwner(service,installedWorkspace,installedProgram,{...processInfo,CommandLine:'python.exe server.py --local-install C:\\Other'}),null);
    const runtime=path.join(workspace,'runtime'),release=await launchLock(runtime);let acquired=false;
    const waiting=launchLock(runtime).then(unlock=>{acquired=true;return unlock;});
    await new Promise(resolve=>setTimeout(resolve,30));assert.equal(acquired,false,'Concurrent clients cannot start duplicate collectors');
    release();const releaseSecond=await waiting;releaseSecond();assert.equal(fs.existsSync(path.join(runtime,'service-launch.lock')),false);
    fs.writeFileSync(path.join(runtime,'service-launch.lock'),JSON.stringify({pid:2147483647,token:'dead-launcher'}));
    const staleRelease=await launchLock(runtime);staleRelease();
    console.log('PASS: portable custom-driver discovery, driver requirement, moved-folder ownership, graceful stop and occupied-port fallback');
  }finally{
    assert.ok(path.resolve(root).startsWith(path.resolve(os.tmpdir())+path.sep)&&path.basename(root).startsWith('stzb-portable-test-'));
    fs.rmSync(root,{recursive:true,force:true});
  }
})().catch(error=>{console.error(error);process.exitCode=1;});
