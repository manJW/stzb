'use strict';
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const {spawn} = require('node:child_process');

function probeInstance(origin, expected, timeout=2000, programId=null) {
  return new Promise((resolve,reject) => {
    const req=http.get(origin+'/api/instance',{timeout},res=>{
      if(res.statusCode!==200){res.resume();reject(new Error('安装服务未就绪'));return;}
      let body='';res.setEncoding('utf8');res.on('data',part=>{body+=part;if(body.length>4096)res.destroy(new Error('安装服务响应异常'));});
      res.on('error',reject);res.on('end',()=>{
        try {const info=JSON.parse(body);if(info.app!=='stzb-local-observer'||!info.local_install||info.instance_id!==expected)throw new Error('服务属于其他安装目录');if(programId&&info.program_id!==programId)throw new Error('服务仍属于旧程序版本');resolve(info);}
        catch(error){reject(error);}
      });
    });
    req.on('error',reject);req.on('timeout',()=>req.destroy(new Error('安装服务连接超时')));
  });
}

async function installedService({workspace,launcher,log,status,timeout=180000}) {
  const recordPath=path.join(workspace,'runtime','service.json');
  async function current() {
    try {
      const record=JSON.parse(fs.readFileSync(recordPath,'utf8'));
      if(!Number.isInteger(record.port)||record.port<1024||record.port>65535||!/^[a-f0-9]{32}$/.test(record.instance_id||''))return null;
      const origin=`http://127.0.0.1:${record.port}`;
      await probeInstance(origin,record.instance_id,2000,path.basename(path.dirname(launcher)));return origin;
    } catch {return null;}
  }
  const connected=await current();if(connected)return connected;
  status('正在识别当前游戏角色');
  const powershell=path.join(process.env.SystemRoot||'C:\\Windows','System32','WindowsPowerShell','v1.0','powershell.exe');
  const child=spawn(powershell,['-NoProfile','-File',launcher,'-Workspace',workspace,'-NoOpen','-Upgrade'],
    {windowsHide:true,cwd:path.dirname(launcher),stdio:['ignore','pipe','pipe']});
  let error=null;
  child.stdout.on('data',()=>{});child.stderr.on('data',data=>log(String(data).trim()));
  child.on('error',value=>{error=value;});child.on('exit',code=>{if(code)error=new Error('安装服务启动失败，请打开日志查看详情。');});
  const deadline=Date.now()+timeout;
  let startupPhase='';
  while(Date.now()<deadline){
    const origin=await current();if(origin)return origin;
    if(error)throw error;
    try{const startup=JSON.parse(fs.readFileSync(path.join(workspace,'runtime','startup.json'),'utf8'));if(startup.phase!==startupPhase){startupPhase=startup.phase;status(String(startup.message||'正在核对当前游戏').slice(0,160));}}catch{}
    await new Promise(resolve=>setTimeout(resolve,500));
  }
  throw new Error('等候游戏身份核对超时。请确认游戏已登录，或打开日志查看原因。');
}
module.exports={probeInstance,installedService};
