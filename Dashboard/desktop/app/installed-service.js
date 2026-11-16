'use strict';
const path = require('node:path');
const http = require('node:http');

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

async function installedService({launcher,...options}) {
  // Keep the installed entry point/config compatible while sharing the exact
  // same native Python bootstrap, driver checks and ownership rules.
  const {nativeService}=require('./portable-service');
  return nativeService({...options,program:path.dirname(path.resolve(launcher)),distribution:'installed'});
}
module.exports={probeInstance,installedService};
