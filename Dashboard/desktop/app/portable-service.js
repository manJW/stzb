'use strict';
const fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto');
const http=require('node:http'),net=require('node:net');
const {execFile,spawn}=require('node:child_process');
const {probeInstance}=require('./installed-service');
const DRIVER_SHA256='bf9b5ce8a89f244c376a9b1a946276eaa06463dde3e33069a34d7f102f5878cf';
const delay=ms=>new Promise(resolve=>setTimeout(resolve,ms));
const read=file=>{try{return JSON.parse(fs.readFileSync(file,'utf8').replace(/^\uFEFF/,''));}catch{return null;}};
const samePath=(a,b)=>typeof a==='string'&&typeof b==='string'&&path.resolve(a).toLowerCase()===path.resolve(b).toLowerCase();
const write=(file,value)=>{const temp=file+'.'+crypto.randomUUID()+'.tmp';fs.writeFileSync(temp,JSON.stringify(value,null,2));fs.renameSync(temp,file);};
const isFile=file=>{try{return fs.statSync(file).isFile();}catch{return false;}};
const command=(exe,args)=>new Promise(resolve=>execFile(exe,args,{windowsHide:true,timeout:10000,encoding:'utf8'},(error,stdout)=>resolve(error?'':stdout)));

async function captureProgram(workspace,run=command){
  const saved=read(path.join(workspace,'install-settings.json'));
  const candidates=[saved?.dumpcap,...[process.env.ProgramFiles,process.env['ProgramFiles(x86)']].filter(Boolean).map(p=>path.join(p,'Wireshark','dumpcap.exe'))];
  const onPath=await run('where.exe',['dumpcap.exe']);candidates.push(...onPath.trim().split(/\r?\n/).filter(Boolean));
  for(const hive of ['HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\Wireshark',
    'HKLM\\SOFTWARE\\WOW6432Node\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\Wireshark',
    'HKCU\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\Wireshark']){
    const value=await run('reg.exe',['query',hive,'/v','InstallLocation']);
    const match=value.match(/\bREG_(?:EXPAND_)?SZ\s+([^\r\n]+)/);
    if(match)candidates.push(path.join(match[1].trim(),'dumpcap.exe'));
  }
  // NSIS/MSI may use a versioned or GUID uninstall key and a custom install drive.
  for(const hive of ['HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall',
    'HKLM\\SOFTWARE\\WOW6432Node\\Microsoft\\Windows\\CurrentVersion\\Uninstall',
    'HKCU\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall']){
    const matches=await run('reg.exe',['query',hive,'/s','/f','Wireshark','/d']);
    for(const key of [...new Set(matches.split(/\r?\n/).map(line=>line.trim()).filter(line=>/^HKEY_(LOCAL_MACHINE|CURRENT_USER)\\/.test(line)))].slice(0,16)){
      const location=await run('reg.exe',['query',key,'/v','InstallLocation']);
      const match=location.match(/\bREG_(?:EXPAND_)?SZ\s+([^\r\n]+)/);
      if(match)candidates.push(path.join(match[1].trim(),'dumpcap.exe'));
      const uninstall=await run('reg.exe',['query',key,'/v','UninstallString']);
      const commandLine=uninstall.match(/\bREG_(?:EXPAND_)?SZ\s+([^\r\n]+)/)?.[1];
      const executable=commandLine?.match(/^\s*"([^"]+\.exe)"/)?.[1]||commandLine?.match(/^\s*(.+?\.exe)(?:\s|$)/)?.[1];
      if(executable)candidates.push(path.join(path.dirname(executable),'dumpcap.exe'));
    }
  }
  const executable=candidates.find(candidate=>typeof candidate==='string'&&isFile(candidate));
  const driver=await run('reg.exe',['query','HKLM\\SYSTEM\\CurrentControlSet\\Services\\npcap']);
  return executable&&driver.trim()?path.resolve(executable):null;
}

async function serviceRecord(workspace){
  const record=read(path.join(workspace,'runtime','service.json'));
  if(!Number.isInteger(record?.port)||record.port<1024||record.port>65535||!/^[a-f0-9]{32}$/.test(record.instance_id||''))return null;
  try{const origin='http://127.0.0.1:'+record.port;const info=await probeInstance(origin,record.instance_id);return {record,info,origin};}
  catch{return null;}
}

function owns(owner,workspace,program,instance){
  return Boolean(owner&&samePath(owner.workspace,workspace)&&samePath(owner.program,program)&&owner.instance_id===instance);
}

function stopRecord(service){
  return new Promise((resolve,reject)=>{
    if(!/^[a-f0-9]{64}$/.test(service.record.stop_token||'')){reject(new Error('后台退出凭据未核对'));return;}
    const req=http.request(service.origin+'/api/stop',{method:'POST',timeout:10000,headers:{'X-Local-Stop':service.record.stop_token}},res=>{
      res.resume();res.on('end',()=>res.statusCode===200?resolve():reject(new Error('后台正在保存，请稍后再试')));
    });req.on('error',reject);req.on('timeout',()=>req.destroy(new Error('后台退出超时')));req.end();
  });
}

function processAlive(pid){
  if(!Number.isInteger(pid)||pid<=0)return false;
  try{process.kill(pid,0);return true;}catch(error){return error.code==='EPERM';}
}

function legacyInstalledOwner(service,workspace,program,processInfo){
  const previous=service?.info?.program_id;
  if(!/^app-[0-9A-F]{16}$/.test(previous||'')||!samePath(workspace,path.join(path.dirname(program),'Workspace')))return null;
  const previousProgram=path.join(path.dirname(program),previous);
  const argument=processInfo?.CommandLine?.match(/(?:^|\s)--local-install\s+(?:"([^"]+)"|(\S+))/);
  if(!samePath(processInfo?.ExecutablePath,path.join(previousProgram,'Python','python.exe'))||
     !samePath(argument?.[1]||argument?.[2],workspace)||processInfo?.ProcessId!==service.record.pid)return null;
  return {workspace,program:previousProgram,pid:service.record.pid,instance_id:service.record.instance_id};
}

async function inspectLegacyProcess(pid){
  if(!Number.isInteger(pid)||pid<=0)return null;
  // A one-time read-only process query migrates pre-native installations.
  // It does not execute a downloaded script or change execution policy.
  const exe=path.join(process.env.SystemRoot||'C:\\Windows','System32','WindowsPowerShell','v1.0','powershell.exe');
  const output=await command(exe,['-NoProfile','-NonInteractive','-Command',
    `[Console]::OutputEncoding=[Text.Encoding]::UTF8;Get-CimInstance Win32_Process -Filter "ProcessId=${pid}" | Select-Object ProcessId,ExecutablePath,CommandLine | ConvertTo-Json -Compress`]);
  try{return JSON.parse(output);}catch{return null;}
}

async function launchLock(runtime){
  const file=path.join(runtime,'service-launch.lock'),token=crypto.randomUUID(),deadline=Date.now()+180000;
  while(Date.now()<deadline){
    try{
      const fd=fs.openSync(file,'wx');
      try{fs.writeFileSync(fd,JSON.stringify({pid:process.pid,token}));}finally{fs.closeSync(fd);}
      return ()=>{if(read(file)?.token===token)fs.unlinkSync(file);};
    }catch(error){
      if(error.code!=='EEXIST')throw error;
      const owner=read(file),valid=Number.isInteger(owner?.pid)&&owner.pid>0&&typeof owner.token==='string';
      try{if(valid&&!processAlive(owner.pid)||!valid&&Date.now()-fs.statSync(file).mtimeMs>10000){fs.unlinkSync(file);continue;}}
      catch(error){if(error.code!=='ENOENT')throw error;}
      await delay(250);
    }
  }
  throw new Error('另一客户端正在启动后台，请稍后重试');
}

async function stopPortableService({workspace,program,isAlive=processAlive}){
  const service=await serviceRecord(workspace);if(!service)return;
  const owner=read(path.join(workspace,'runtime','portable-owner.json'));
  if(!owns(owner,workspace,program,service.record.instance_id)||service.info.program_id!==path.basename(program))throw new Error('后台属于其他目录，已停止退出操作');
  await stopRecord(service);
  const deadline=Date.now()+30000;
  while(Date.now()<deadline){if(!await serviceRecord(workspace)&&!isAlive(service.record.pid))return;await delay(250);}
  throw new Error('后台仍在保存数据，请稍后再退出');
}

function availablePort(){
  return new Promise((resolve,reject)=>{
    const server=net.createServer();let fallback=false;
    server.on('error',error=>{if(!fallback&&error.code==='EADDRINUSE'){fallback=true;server.listen(0,'127.0.0.1');}else reject(error);});
    server.on('listening',()=>{const port=server.address().port;server.close(error=>error?reject(error):resolve(port));});
    server.listen(8787,'127.0.0.1');
  });
}

async function startService({workspace,program,log,status,confirmDriver,openDriver,distribution='portable',inspectProcess=inspectLegacyProcess,timeout=180000}){
  workspace=path.resolve(workspace);program=path.resolve(program);
  const runtime=path.join(workspace,'runtime');fs.mkdirSync(runtime,{recursive:true});
  const ownerFile=path.join(runtime,'portable-owner.json');let owner=read(ownerFile);
  const old=await serviceRecord(workspace);
  if(old&&!owner&&distribution==='installed'){
    owner=legacyInstalledOwner(old,workspace,program,await inspectProcess(old.record.pid));
    if(owner)write(ownerFile,owner);
  }
  if(old&&owns(owner,workspace,program,old.record.instance_id)&&old.info.program_id===path.basename(program))return old.origin;
  if(old&&owner&&samePath(owner.workspace,workspace)&&samePath(owner.program,program)&&
    owner.pid===old.record.pid&&old.info.program_id===path.basename(program)){
    write(ownerFile,{...owner,instance_id:old.record.instance_id});return old.origin;
  }
  // Only a previous version in this same Workspace may be stopped during an upgrade.
  let upgrading=false;
  if(old&&owner&&samePath(owner.workspace,workspace)&&samePath(path.dirname(owner.program||''),path.dirname(program))&&
    /^app-[0-9A-F]{16}$/.test(path.basename(owner.program||''))&&owner.instance_id===old.record.instance_id){
    status('正在保存旧版后台数据');await stopPortableService({workspace,program:owner.program});
    upgrading=true;
  }
  if(old&&!upgrading)throw new Error('后台属于其他目录，已停止启动，原数据保持不变');
  let dumpcap=await captureProgram(workspace);
  if(!dumpcap){
    const installer=path.join(program,'Prerequisites','Wireshark-4.6.9-x64.exe');
    const digest=crypto.createHash('sha256').update(fs.readFileSync(installer)).digest('hex');
    if(digest!==DRIVER_SHA256)throw new Error('官方采集组件校验失败，已停止启动');
    if(!await confirmDriver())throw new Error('采集驱动尚未安装，可稍后重新打开客户端');
    const error=await openDriver(installer);if(error)throw new Error('无法打开官方采集安装程序：'+error);
    status('请完成官方 Wireshark/Npcap 安装，随后自动继续');
    const deadline=Date.now()+600000;
    while(Date.now()<deadline&&!dumpcap){await delay(1500);dumpcap=await captureProgram(workspace);}
    if(!dumpcap)throw new Error('尚未检测到采集驱动；如安装程序提示重启，请重启后打开客户端');
  }
  write(path.join(workspace,'install-settings.json'),{distribution_v2:true,dumpcap,executable:'auto'});
  const python=path.join(program,'Python','python.exe'),server=path.join(program,'Dashboard','server.py');
  if(!isFile(python)||!isFile(server))throw new Error('程序文件不完整，请保留完整安装或解压目录');
  const port=await availablePort();status('正在自动识别当前游戏角色');
  const out=fs.openSync(path.join(runtime,'server-out.log'),'a'),err=fs.openSync(path.join(runtime,'server-error.log'),'a');
  let child;
  try{child=spawn(python,['-X','utf8','-B',server,'--local-install',workspace,'--port',String(port)],
    {cwd:path.dirname(server),windowsHide:true,stdio:['ignore',out,err]});}
  finally{fs.closeSync(out);fs.closeSync(err);}
  let failure=null;child.on('error',error=>{failure=error;});child.on('exit',code=>{failure=new Error('后台已退出（'+code+'），请打开日志查看原因');});
  if(child.pid)write(ownerFile,{workspace,program,pid:child.pid,instance_id:null});
  child.unref();const deadline=Date.now()+timeout;let phase='';
  while(Date.now()<deadline){
    if(failure)throw failure;
    const service=await serviceRecord(workspace);
    if(service&&service.record.pid===child.pid&&service.info.program_id===path.basename(program)){
      write(ownerFile,{workspace,program,pid:child.pid,instance_id:service.record.instance_id});return service.origin;
    }
    const startup=read(path.join(runtime,'startup.json'));
    if(startup?.phase!==phase){phase=startup?.phase;status(String(startup?.message||'正在核对当前游戏').slice(0,160));}
    await delay(500);
  }
  log('Native bootstrap timed out');throw new Error('等候后台启动超时，请打开日志查看原因');
}

async function portableService(options){
  const runtime=path.join(path.resolve(options.workspace),'runtime');fs.mkdirSync(runtime,{recursive:true});
  const release=await launchLock(runtime);
  try{return await startService(options);}finally{release();}
}

module.exports={portableService,nativeService:portableService,stopPortableService,captureProgram,owns,availablePort,legacyInstalledOwner,launchLock};
