"""Installation bootstrap; the collector, decoder and analysis stay shared."""
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from collector import Collector, discover_pc_connection, discover_dashen_connection, NO_WINDOW
from installed_mode import InstalledCollector, logged_in_role, archive_scope, LOGIN_SNAPSHOT, ROLE_DETAIL, ACCOUNT_UPDATE
from model import default_config, write_json

ROOT = Path(__file__).resolve().parent
TOOLS = ROOT.parent / 'Tools' / 'android-report-probe-20261112'


def discover_game():
    script = """[Console]::OutputEncoding=[Text.Encoding]::UTF8;
    $all=Get-CimInstance Win32_Process;
    @($all|Where-Object {$_.Name -in @('stzb.exe','GLEmulator.exe')}|
      Select-Object Name,ExecutablePath)|ConvertTo-Json -Compress"""
    result = subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',script],
                            capture_output=True,encoding='utf8',errors='replace',timeout=20,creationflags=NO_WINDOW)
    found = json.loads(result.stdout or '[]')
    if isinstance(found, dict): found = [found]
    connected = []
    for item in found:
        path = item.get('ExecutablePath')
        if not path: continue
        mode = 'pc' if item['Name'].lower() == 'stzb.exe' else 'dashen'
        connection = (discover_pc_connection if mode == 'pc' else discover_dashen_connection)(path)
        if connection: connected.append((mode,path,connection))
    if len(connected) > 1: raise RuntimeError('检测到多个已登录游戏，请保留一个游戏连接后重新打开客户端。')
    if not connected: raise RuntimeError('未发现已登录的大神桌面版或网易 PC 官服，请先打开游戏进入角色。')
    return connected[0]


def inspect_identity(mode, connection):
    if mode == 'dashen':
        from android_report_bridge import inspect_logged_in_identity
        return inspect_logged_in_identity(ROOT, connection, tools=TOOLS)
    # The PC build uses the same Mono identity tables. No helper executable is bundled.
    sys.path.insert(0, str(TOOLS/'bindings'))
    import frida
    assets = ROOT/'android_reader_assets'
    manifest = json.loads((assets/'identity-manifest.json').read_text(encoding='utf-8-sig'))
    binary = (assets/manifest['file']).read_bytes()
    if hashlib.sha256(binary).hexdigest() != manifest['sha256']: raise RuntimeError('身份组件校验失败')
    session = frida.attach(int(connection['pid']))
    script = None
    try:
        script = session.create_script((assets/'mono_managed_loader.js').read_text(encoding='utf8')+'\n'+(assets/'mono_identity.js').read_text(encoding='utf8'))
        script.load(); script.exports_sync.initialize(manifest['file'],list(binary),manifest['sha256'])
        return script.exports_sync.identify()
    finally:
        if script: script.unload()
        session.detach()


def validated_identity(value):
    if value.get('error'):
        raise ValueError('等候游戏角色登录：'+str(value['error'])[:120])
    for key in ('user_id','account_role_id','union_id'):
        if not re.fullmatch(r'[0-9]{1,12}',str(value.get(key,''))): raise ValueError('角色编号未核对')
    if str(value['user_id']) != str(value['account_role_id']) or int(value['user_id']) <= 0:
        raise ValueError('当前角色编号不一致')
    if not isinstance(value.get('account_name'),str) or not value['account_name'].strip(): raise ValueError('角色名称未核对')
    scope = archive_scope(value.get('server_id'),value.get('season_id'))
    if not scope or value.get('game_requests_sent') != 0: raise ValueError('当前区服或赛季未核对')
    if type(value.get('force_type'))is not int or value['force_type']<0:
        raise ValueError('当前角色的势力身份尚未读齐，等待自动重新核对')
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',str(value.get('battle_role_id',''))):
        raise ValueError('当前角色的战报身份尚未读齐，等待自动重新核对')
    result = {key:str(value.get(key,'')) for key in ('user_id','account_role_id','account_name','union_id','union_name')}
    if not isinstance(value.get('union_name'),str)or not value['union_name'].strip():
        result['union_name']='无所属势力'if result['union_id']=='0'else'所属势力名称待核'
    if type(value.get('force_type'))is int:
        result['force_type']=value['force_type']
        result['is_wanderer']=value['force_type']==1
    if re.fullmatch(r'[A-Za-z0-9_-]{1,100}',str(value.get('battle_role_id',''))):
        result['battle_role_id']=str(value['battle_role_id'])
    if str(value.get('network_user_id','')).isdigit() and 0 < int(value['network_user_id']) <= 2147483647:
        result['network_user_id'] = str(value['network_user_id'])
    result['scope'] = scope
    return result


def waiting_installation(root,settings,error):
    """Start the service while offline; reconnect automatically after login."""
    path=root/'archive-profile.json'
    profile=json.loads(path.read_text(encoding='utf-8-sig'))if path.exists()else dict(
        server='pending',season='pending',account_role_id='0',account_name='待识别',union_id='0',union_name='待核对')
    config_path=root/'config.json'
    config=json.loads(config_path.read_text(encoding='utf-8-sig'))if config_path.exists()else default_config()
    config['self']['confirmed']=False
    settings={**settings,'mode':settings.get('mode')if settings.get('mode')in ('pc','dashen')else'dashen','auto_identity':True}
    settings.pop('profile_id',None)
    write_json(config_path,config);write_json(root/'capture-source.json',settings)
    write_json(root/'runtime'/'startup.json',{'phase':'waiting_for_role','message':str(error)[:200]})
    return profile,settings


def prepare_installation(root, settings, *, timeout=110):
    deadline = time.monotonic()+timeout
    while True:
        try:
            mode, executable, connection = discover_game()
            identity = validated_identity(inspect_identity(mode, connection))
            break
        except (RuntimeError,ValueError) as error:
            write_json(root/'runtime'/'startup.json',{'phase':'waiting_for_role','message':str(error)[:200]})
            if time.monotonic() >= deadline: raise
            time.sleep(3)
    scope = dict(identity['scope'])
    # A bundled archive contributes labels/data only after its numeric scope matches.
    seed = ROOT/'season-seed.sqlite3'
    if seed.exists():
        import sqlite3
        with sqlite3.connect(seed.resolve().as_uri()+'?mode=ro',uri=True) as db:
            manifest = json.loads(db.execute('SELECT data FROM manifest').fetchone()[0])
        if all(manifest.get(key)==scope[key] for key in ('server','season')):
            scope['server_label'] = manifest.get('server_label',scope['server'])
    profile = {**scope, **{key:identity[key] for key in ('account_role_id','account_name','union_id','union_name')}}
    profile.update({k:identity[k]for k in ('force_type','is_wanderer','battle_role_id')if k in identity})
    config_path = root/'config.json'
    config = json.loads(config_path.read_text(encoding='utf-8-sig')) if config_path.exists() else default_config()
    old_scope = None
    if (root/'archive-profile.json').exists():
        old_scope = json.loads((root/'archive-profile.json').read_text(encoding='utf-8-sig'))
    from role_context import perspective_id
    if not old_scope or perspective_id(old_scope)!=perspective_id(profile):
        from observation_codec import digest
        cache=root/'data'/'role-settings';cache.mkdir(parents=True,exist_ok=True)
        if old_scope and all(old_scope.get(k)for k in ('server','season','account_role_id')):
            old_key=perspective_id(old_scope)
            write_json(cache/(old_key+'.json'),{'profile':old_scope,
                'relations':{k:v for k,v in config.get('relations',{}).items()if v.get('manual_override')},
                'targets':config.get('targets',[]),'state_rules':config.get('state_rules',{})})
        key=perspective_id(profile)
        saved={}
        if(cache/(key+'.json')).exists():saved=json.loads((cache/(key+'.json')).read_text(encoding='utf8'))
        else:
            # Read old per-role preferences only when their organization and
            # force still match; a new perspective never inherits diplomacy.
            legacy=digest([profile[k]for k in ('server','season','account_role_id')])[:24]
            if(cache/(legacy+'.json')).exists():
                prior=json.loads((cache/(legacy+'.json')).read_text(encoding='utf8'));owner=prior.get('profile',{})
                if str(owner.get('union_id'))==profile['union_id']and owner.get('force_type',0)==profile.get('force_type',0):saved=prior
        config['relations']=saved.get('relations',{});config['targets']=saved.get('targets',[])
        defaults=json.loads((ROOT/'capture-defaults.json').read_text(encoding='utf8'))['state_rules']
        config['state_rules']={**defaults,**saved.get('state_rules',{})}
    config['self'] = {'user_id':identity['user_id'],'name':identity['account_name'],'union_id':identity['union_id'],
                      'union_name':identity['union_name'],'confirmed':True,'evidence':'当前游戏角色表与在线连接一致'}
    config['self'].update({k:identity[k]for k in ('force_type','is_wanderer','battle_role_id')if k in identity})
    if not config.get('state_rules'):
        config['state_rules'] = json.loads((ROOT/'capture-defaults.json').read_text(encoding='utf8'))['state_rules']
    settings = {**settings,'mode':mode,'executable':executable,'emulator_executable':executable,'auto_identity':True}
    for obsolete in ('account_confirmation','confirmed_at','server','season'):settings.pop(obsolete,None)
    from observation_codec import digest
    settings['profile_id']=digest([profile[k]for k in ('server','season','account_role_id')])[:24]
    write_json(config_path,config); write_json(root/'archive-profile.json',profile)
    write_json(root/'capture-source.json',settings)
    write_json(root/'runtime'/'identity.json',{**identity,'scope':scope,'verified_at':time.time(),'source':mode})
    write_json(root/'runtime'/'startup.json',{'phase':'identity_ready','message':'已核对当前角色，正在读取赛季档案'})
    return profile,settings


class PortableCollector(InstalledCollector):
    def __init__(self,state,archive,settings,seed_path=None):
        super().__init__(state,archive,settings,seed_path)
        self.incoming_ids = None
        self.identity_change_callback=None
        self.identity_check_at=0
        self.auto_discovery=bool(settings.get('auto_identity')or settings.get('distribution_v2'))

    def _discover(self):
        if not self.auto_discovery:return super()._discover()
        try:mode,executable,connection=discover_game()
        except RuntimeError as error:
            self._wait_for_identity(str(error));return None
        if mode!=self.source:
            self._wait_for_identity('已识别游戏来源变化，正在自动重新接入')
            if self.identity_change_callback:self.identity_change_callback({'source':mode})
            return None
        self.pc_executable=self.dashen_executable=executable
        return connection

    def _verify_capture(self,connection,process,started_at):
        if not super()._verify_capture(connection,process,started_at):return False
        if time.monotonic()-self.identity_check_at<30:return True
        self.identity_check_at=time.monotonic()
        try:
            identity=validated_identity(inspect_identity(self.source,connection))
            return self._confirm_identity(identity,'在线角色表持续核对',time.time())
        except Exception as error:
            self._wait_for_identity('当前角色暂未核对，暂停入库后自动重试')
            self.state.health['identity_error']=type(error).__name__
            return False

    def _prepare_connection(self,connection):
        self._wait_for_identity()
        self.connection = connection
        try:
            identity = validated_identity(inspect_identity(self.source,connection))
            self._confirm_identity(identity,'当前已登录角色表、区服和赛季与连接一致',time.time())
        except Exception as error:
            self.state.health['identity_message'] = str(error)[:200]

    def _bind_scope(self,scope):
        super()._bind_scope(scope)
        settings = json.loads((self.state.root/'capture-source.json').read_text(encoding='utf8'))
        settings['profile_id'] = self.archive.profile_id
        write_json(self.state.root/'capture-source.json',settings)

    def _confirm_identity(self,identity,evidence,observed_at):
        profile=self.archive.profile;scope=identity.get('scope')or{}
        same_role=(profile['account_role_id']==identity['account_role_id']and
                   all(not scope.get(k)or profile[k]==scope[k]for k in ('server','season')))
        if same_role:
            identity={**{k:profile[k]for k in ('force_type','is_wanderer','battle_role_id')if k in profile},**identity}
        changed=(profile['account_role_id']not in ('0',identity['account_role_id'])or
                 any(scope.get(k)and profile[k]!=scope[k]for k in ('server','season')))
        perspective_changed=same_role and (profile['union_id']!=identity['union_id']or
            'force_type'in identity and profile.get('force_type')!=identity['force_type'])
        if (changed or perspective_changed)and self.identity_change_callback:
            self._wait_for_identity('已识别账号、势力或赛季变化，正在自动切换采集范围')
            self.identity_change_callback(identity)
            return False
        ready=super()._confirm_identity(identity,evidence,observed_at)
        if ready:
            extra={k:identity[k]for k in ('force_type','is_wanderer','battle_role_id')if k in identity}
            with self.state.lock:
                self.state.config['self'].update(extra)
                write_json(self.state.config_path,self.state.config)
            if any(self.archive.profile.get(k)!=v for k,v in extra.items()):
                self.archive.activate_profile({**self.archive.profile,**extra})
            previous={}
            try: previous=json.loads((self.state.root/'runtime'/'identity.json').read_text(encoding='utf8'))
            except (OSError,ValueError): pass
            network=identity.get('network_user_id')
            if not network and previous.get('user_id')==identity['user_id']: network=previous.get('network_user_id')
            write_json(self.state.root/'runtime'/'identity.json',{**identity,'network_user_id':network,'verified_at':observed_at})
        return ready

    def _message(self,mid,value,observed_at):
        if mid==ACCOUNT_UPDATE:
            from archive import table_updates
            if any(table=='Tb_user'for _,table,_ in table_updates(value)):self.identity_check_at=0
        if mid == LOGIN_SNAPSHOT:
            raw = logged_in_role(value)
            if raw and raw.get('scope'):
                raw['account_role_id'] = raw['user_id']
                if not self._confirm_identity(raw,'登录消息与当前角色编号一致',observed_at): return
            else:
                self._wait_for_identity('游戏正在切换登录角色，等候当前角色重新核对')
                return
        if mid == ROLE_DETAIL:
            # Historical PC code called the main-city tile ID a role ID. It is
            # deliberately excluded from installation identity and profile keys.
            return
        if self.identity_ready and self.archive_ready:
            Collector._message(self,mid,value,observed_at)
