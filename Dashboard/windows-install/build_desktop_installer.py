"""Self-contained, hash-verified desktop distribution with one shared backend."""
import argparse
import ast
import hashlib
import importlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

ROOT=Path(__file__).resolve().parent.parent
HERE=ROOT/'windows-install'
PINS={
 'python-3.13.14-embed-amd64.zip':'90b4e5b9898b72d744650524bff92377c367f44bd5fbd09e3148656c080ad907',
 'pillow.whl':'9dda0788a9932776a75d8bcc020076edc308ce2508b51f6739a45e3158b3fc42',
 'tzdata-2026.6-py2.py3-none-any.whl':'0a10eec6e13ef16b627fabed9a5f2ba61a62b1c67f327305f4e7e412a70b4aec',
 'electron-v45.3.1-win32-x64.zip':'d92d1aa4700e3a7e7f174731f5086ebd9c21f9773e642b260160fe0a6ebe12d2',
 'frida-17.26.2-cp37-abi3-win_amd64.whl':'60a77e5ff364cee68ee627fe1833bf872e28cb8bff966b62208ca7828fdaa34f',
 'frida-server-17.26.2':'c671eee6c39d15e57782715ff22029828a68e23c51d5c571d99080a7f6564fe6',
 'Wireshark-4.6.9-x64.exe':'bf9b5ce8a89f244c376a9b1a946276eaa06463dde3e33069a34d7f102f5878cf',
}

def sha(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for part in iter(lambda:f.read(1024*1024),b''):h.update(part)
    return h.hexdigest()

def checked(path):
    if sha(path)!=PINS[path.name]:raise ValueError(f'Pinned artifact mismatch: {path.name}')
    return path

def extract(path,destination):
    with ZipFile(checked(path)) as z:
        for item in z.infolist():
            if not (destination/item.filename).resolve().is_relative_to(destination.resolve()):raise ValueError('Unsafe artifact path')
        z.extractall(destination)


def reuse_runtime(source,payload):
    """Reuse only hash-checked runtimes from a prior local distribution."""
    manifest=json.loads((source/'build-manifest.json').read_text(encoding='utf8'))
    if manifest.get('runtime_pins')!=PINS:raise ValueError('Runtime provenance/pins differ from this builder')
    count=0
    for line in (source/'files.sha256').read_text(encoding='utf8').splitlines():
        expected,relative=line.split('  ',1)
        if not relative.startswith(('Python/','Client/'))or relative.startswith('Client/resources/app/'):continue
        original=(source/relative).resolve();target=(payload/relative).resolve()
        if not original.is_relative_to(source.resolve())or not target.is_relative_to(payload.resolve()):raise ValueError('Unsafe runtime path')
        if sha(original)!=expected:raise ValueError('Prior runtime checksum mismatch: '+relative)
        target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(original,target);count+=1
    for required in ('Python/python.exe','Client/率土战场.exe'):
        if not(payload/required).is_file():raise ValueError('Missing verified runtime: '+required)
    return {'source_manifest_sha256':sha(source/'build-manifest.json'),'verified_files':count}


def verify_portable_payload(dashboard):
    for name in ('config.json','capture-source.json','archive-profile.json','season-seed.sqlite3','data','runtime'):
        if(dashboard/name).exists():raise ValueError('Personal/example state must not enter a universal package: '+name)
    for name in ('identity','reader','map-selection'):
        manifest=json.loads((dashboard/'android_reader_assets'/(name+'-manifest.json')).read_text(encoding='utf-8-sig'))
        if sha(dashboard/'android_reader_assets'/manifest['file'])!=manifest['sha256']:raise ValueError('Reader mismatch: '+name)

def main():
    p=argparse.ArgumentParser()
    for flag in ('output','work','tools','capture_installer'):
        p.add_argument('--'+flag.replace('_','-'),type=Path,required=True)
    for flag in ('downloads','electron_zip','runtime_from'):
        p.add_argument('--'+flag.replace('_','-'),type=Path)
    args=p.parse_args();work=args.work.resolve();work.mkdir(parents=True,exist_ok=True)
    payload=work/'payload'
    if payload.exists():raise FileExistsError('Choose a new build work directory')
    dashboard=payload/'Dashboard';dashboard.mkdir(parents=True)
    sources=sorted(ROOT.glob('*.py'))
    # Ship all local modules, including dynamic imports; never keep a stale allowlist.
    for file in sources:shutil.copyfile(file,dashboard/file.name)
    local={file.stem for file in sources}
    for file in sources:
        for node in ast.walk(ast.parse(file.read_text(encoding='utf-8-sig'))):
            names=[a.name.split('.')[0] for a in node.names] if isinstance(node,ast.Import) else [node.module.split('.')[0]] if isinstance(node,ast.ImportFrom) and node.module else []
            for name in names:
                if name in local and not (dashboard/(name+'.py')).is_file():raise ValueError('Missing local dependency: '+name)
    for folder in ('static','catalog','android_reader_assets'):shutil.copytree(ROOT/folder,dashboard/folder)
    for name in ('field-definitions.json','capture-defaults.json','Windows-Ocr.ps1'):shutil.copyfile(ROOT/name,dashboard/name)
    tools=payload/'Tools';tools.mkdir()
    report_tools=tools/'android-report-probe-20261112';report_tools.mkdir()
    shutil.copyfile(checked(args.tools/'frida-server-17.26.2'),report_tools/'frida-server-17.26.2')
    shutil.copyfile(args.tools/'tools-manifest.json',report_tools/'tools-manifest.json')
    extract(args.tools/'frida-17.26.2-cp37-abi3-win_amd64.whl',report_tools/'bindings')
    python=payload/'Python';client=payload/'Client';runtime_origin=None
    if args.runtime_from:runtime_origin=reuse_runtime(args.runtime_from,payload)
    else:
        if not args.downloads or not args.electron_zip:raise ValueError('Provide runtime-from or downloads/electron-zip')
        extract(args.downloads/'python-3.13.14-embed-amd64.zip',python)
        extract(args.downloads/'pillow.whl',python/'site-packages')
        extract(args.downloads/'tzdata-2026.6-py2.py3-none-any.whl',python/'site-packages')
        with (python/'python313._pth').open('a',encoding='ascii') as f:f.write('\nsite-packages\n..\\Dashboard\n..\\Tools\\android-report-probe-20261112\\bindings\n')
        extract(args.electron_zip,client)
        (client/'electron.exe').rename(client/'率土战场.exe')
    app=client/'resources'/'app';shutil.copytree(ROOT/'desktop'/'app',app)
    (app/'client-config.json').write_text(json.dumps({'installed':True,'dashboardRoot':'../../../Dashboard',
        'workspaceRoot':'../../../../Workspace','serviceLauncher':'../../../Start-Installed.ps1'},indent=2),encoding='utf8')
    # Windows PowerShell 5.1 requires a BOM for Chinese script text.
    (payload/'Start-Installed.ps1').write_text((HERE/'Start-DesktopService.ps1').read_text(encoding='utf-8-sig'),encoding='utf-8-sig')
    prerequisite=payload/'Prerequisites';prerequisite.mkdir()
    shutil.copyfile(checked(args.capture_installer),prerequisite/args.capture_installer.name)
    shutil.copyfile(HERE/'桌面安装说明.txt',payload/'使用说明.txt')
    verify_portable_payload(dashboard)
    subprocess.run([str(python/'python.exe'),'-X','utf8','-B','-c',
        'import importlib,pathlib; from zoneinfo import ZoneInfo; assert str(ZoneInfo("Asia/Shanghai"))=="Asia/Shanghai"; import PIL.Image,frida; [importlib.import_module(p.stem) for p in sorted(pathlib.Path(".").glob("*.py"))]; print("All installed backend imports and timezone verified")'],cwd=dashboard,check=True)
    # Refuse a release if any shipped algorithm or catalogue differs from the source snapshot.
    parity={file.name:sha(file) for file in sources}
    for name,value in parity.items():
        if sha(dashboard/name)!=value:raise ValueError('Source/package mismatch: '+name)
    manifest={'format':3,'version':'0.2.7','backend_modules':parity,'seed_scope':None,'account_independent':True,
        'example_data_included':False,'runtime_origin':runtime_origin,
        'bootstrap':'bundled-python-direct','code_signed':False,
        'electron':'45.3.1','python':'3.13.14','frida':'17.26.2','network':'loopback-only','runtime_pins':PINS}
    (payload/'build-manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf8')
    files=sorted(x for x in payload.rglob('*') if x.is_file())
    (payload/'files.sha256').write_text('\n'.join(sha(x)+'  '+x.relative_to(payload).as_posix() for x in files)+'\n',encoding='utf8')
    archive=work/'desktop.zip'
    with ZipFile(archive,'w',ZIP_DEFLATED,compresslevel=4,allowZip64=True) as z:
        for file in sorted(payload.rglob('*')):
            if file.is_file():z.write(file,file.relative_to(payload).as_posix())
    for name,value in parity.items():
        if sha(ROOT/name)!=value:raise ValueError('Source changed during package build: '+name)
    for folder in ('static','catalog','android_reader_assets'):
        for source in (ROOT/folder).rglob('*'):
            if source.is_file()and sha(source)!=sha(dashboard/source.relative_to(ROOT)):
                raise ValueError('Asset changed during package build: '+str(source.relative_to(ROOT)))
    for name in ('field-definitions.json','capture-defaults.json','Windows-Ocr.ps1'):
        if sha(ROOT/name)!=sha(dashboard/name):raise ValueError('Default asset changed during build: '+name)
    for source in (ROOT/'desktop/app').rglob('*'):
        if source.is_file()and sha(source)!=sha(app/source.relative_to(ROOT/'desktop/app')):
            raise ValueError('Desktop source changed during build: '+source.name)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    csc=Path(os.environ.get('WINDIR','C:/Windows'))/'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
    subprocess.run([str(csc),'/nologo','/target:winexe','/platform:x64','/optimize+','/codepage:65001',
        '/reference:System.Windows.Forms.dll','/reference:System.Drawing.dll','/reference:System.IO.Compression.dll',
        '/reference:System.IO.Compression.FileSystem.dll',f'/win32icon:{ROOT/"desktop/app/icon.ico"}',
        f'/resource:{archive},desktop.zip',f'/out:{args.output}',str(HERE/'DesktopInstaller.cs')],check=True)
    result={'installer':str(args.output),'sha256':sha(args.output),'bytes':args.output.stat().st_size,'backend_modules':len(sources),'account_independent':True,'seed':None}
    (work/'build-result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps(result,ensure_ascii=False),flush=True)

if __name__=='__main__':main()
