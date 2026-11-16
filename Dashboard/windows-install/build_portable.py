"""Build a relocatable ZIP from the same verified payload as the desktop installer."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
from zipfile import ZipFile, ZIP_DEFLATED

spec=importlib.util.spec_from_file_location('desktop_distribution',Path(__file__).with_name('build_desktop_installer.py'))
distribution=importlib.util.module_from_spec(spec);spec.loader.exec_module(distribution)
sha=distribution.sha


def checked_files(payload):
    manifest=payload/'files.sha256'
    result=[];seen=set()
    for line in manifest.read_text(encoding='utf8').splitlines():
        expected,name=line.split('  ',1)
        source=(payload/name).resolve()
        if source in seen or not source.is_relative_to(payload.resolve()) or source.is_symlink():
            raise ValueError('Unsafe or duplicate payload path')
        if sha(source)!=expected:raise ValueError('Payload checksum mismatch: '+name)
        result.append((name,source));seen.add(source)
    actual={p.resolve()for p in payload.rglob('*')if p.is_file()and p!=manifest}
    if actual!=seen:raise ValueError('Payload contains unverified files')
    return result


def prepare(payload,destination):
    payload=payload.resolve();destination=destination.resolve()
    if destination.exists():raise FileExistsError('Choose a new portable output directory')
    files=checked_files(payload)
    distribution.verify_portable_payload(payload/'Dashboard')
    source_manifest=json.loads((payload/'build-manifest.json').read_text(encoding='utf8'))
    if source_manifest.get('runtime_pins')!=distribution.PINS:raise ValueError('Runtime provenance differs')
    for name,digest in source_manifest['backend_modules'].items():
        if sha(payload/'Dashboard'/name)!=digest:raise ValueError('Backend/source mismatch: '+name)
    app_sources=Path(__file__).resolve().parents[1]/'desktop/app'
    overrides={name:sha(app_sources/name)for name in ('main.js','portable-service.js','installed-service.js')}
    identity=sha(payload/'files.sha256')+json.dumps(overrides,sort_keys=True)
    program_id='app-'+hashlib.sha256(identity.encode('ascii')).hexdigest()[:16].upper()
    destination.mkdir(parents=True)
    program=destination/'Program'/program_id
    copied=[]
    for name,source in files:
        target=destination/name[len('Client/'):]if name.startswith('Client/')else program/name
        target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(source,target)
        copied.append((target,sha(source)))
    app=destination/'resources/app'
    config={'installed':True,'portable':True,'dashboardRoot':f'../../Program/{program_id}/Dashboard',
            'workspaceRoot':'../../Workspace','programRoot':f'../../Program/{program_id}',
            'serviceLauncher':f'../../Program/{program_id}/Start-Installed.ps1'}
    (app/'client-config.json').write_text(json.dumps(config,ensure_ascii=False,indent=2),encoding='utf8')
    for target,digest in copied:
        if target!=app/'client-config.json'and sha(target)!=digest:raise ValueError('Portable copy mismatch')
    for name in overrides:shutil.copyfile(app_sources/name,app/name)
    (destination/'Workspace').mkdir()
    (destination/'使用说明.txt').write_text('''率土战场 免安装版（Windows x64）

1. 完整解压 ZIP 到有写入权限的文件夹，保持目录结构完整，不要在压缩包内直接运行。
2. 登录大神桌面版里的率土游戏，然后双击本目录「率土战场.exe」。
3. 自动识别当前角色、区服和赛季；首次采集缺少驱动时，会提示打开随包的官方 Wireshark/Npcap 安装程序。
4. Workspace 保存本机配置、角色档案、战报和日志。搬迁前从托盘选择「退出便携版（停止后台）」，再移动整个文件夹。

免安装指看板本身无需安装或创建快捷方式；Wireshark/Npcap 采集驱动仍需首次安装。
本包不含示例账号、个人历史战报、密码或自动添加的安全软件排除项。
本程序目前没有受信任的代码签名，ZIP 也不能保证免受浏览器信誉拦截或安全软件检测。
如被拦截，请保留下载地址、完整提示和检测名称，以区分信誉警告与恶意软件检测。
files.sha256 可核对包内文件完整性；哈希校验不等同于杀毒结论或受信任的数字签名。
''',encoding='utf-8-sig')
    proof={'format':1,'version':source_manifest['version'],'distribution':'portable-zip',
           'program_id':program_id,'source_payload_manifest_sha256':sha(payload/'files.sha256'),
           'backend_modules':source_manifest['backend_modules'],'account_independent':True,
           'workspace':'Workspace','code_signed':False,'capture_driver_required':True,
           'desktop_overrides':overrides,'bootstrap':'bundled-python-direct'}
    (destination/'portable-manifest.json').write_text(json.dumps(proof,ensure_ascii=False,indent=2),encoding='utf8')
    rows=sorted(p for p in destination.rglob('*')if p.is_file())
    (destination/'files.sha256').write_text(''.join(sha(p)+'  '+p.relative_to(destination).as_posix()+'\n'for p in rows),encoding='utf8')
    return proof


def build(payload,work,output):
    if output.exists():raise FileExistsError('Portable archive already exists')
    destination=work/'STZB-Portable';proof=prepare(payload,destination)
    output.parent.mkdir(parents=True,exist_ok=True)
    with ZipFile(output,'w',ZIP_DEFLATED,compresslevel=4,allowZip64=True)as archive:
        for file in sorted(destination.rglob('*')):
            name='STZB-Portable/'+file.relative_to(destination).as_posix()
            if file.is_file():archive.write(file,name)
            elif not any(file.iterdir()):archive.writestr(name+'/',b'')
    result={'archive':str(output),'sha256':sha(output),'bytes':output.stat().st_size,
            'version':proof['version'],'backend_modules':len(proof['backend_modules']),
            'code_signed':False,'program_id':proof['program_id']}
    (work/'portable-build-result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    for flag in ('payload','work','output'):parser.add_argument('--'+flag,type=Path,required=True)
    args=parser.parse_args()
    print(json.dumps(build(args.payload,args.work,args.output),ensure_ascii=False),flush=True)
