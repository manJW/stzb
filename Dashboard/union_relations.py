"""Read and follow the logged-in client's alliance diplomacy, without game orders."""
import json
import re
import threading
import time

from model import write_json
from observation_codec import digest


LABELS = {0:'unknown', 1:'friendly', 2:'enemy', 3:'unknown'}


def positive(value, *, zero=False):
    if type(value) is not int or value < (0 if zero else 1) or value > 2147483647:
        raise ValueError('同盟关系编号格式变化')
    return value


def records(value):
    if not isinstance(value,list) or len(value)>512:
        raise ValueError('同盟关系表格式变化')
    result={}
    for row in value:
        if not isinstance(row,dict):
            raise ValueError('同盟关系记录格式变化')
        item={k:positive(row.get(k),zero=k in ('relationship','next_time','cron_time'))
              for k in ('id','union_id','target_union_id','relationship','next_time','cron_time')}
        if item['relationship'] not in LABELS or item['id'] in result:
            raise ValueError('同盟关系编码变化或记录重复')
        result[item['id']]=item
    return result


def effective(rows, own, directional):
    """Mirror UnionRelationData.ResetCachedData/CalculateRealRelationship."""
    if not own:
        return {}
    outgoing,incoming={},{}
    for row in rows.values():
        a,b,kind=row['union_id'],row['target_union_id'],row['relationship']
        if a==b:
            continue
        if a==own:
            outgoing[b]=kind
            if not directional:
                incoming[b]=kind
        elif directional and b==own:
            incoming[a]=kind
    result={}
    for target in outgoing.keys()|incoming.keys():
        a,b=outgoing.get(target,0),incoming.get(target,0)
        result[str(target)]=2 if 2 in (a,b) else 0 if 0 in (a,b) else 1
    return result


class ClientUnionRelations:
    def __init__(self,state,archive,connection_provider):
        self.state,self.archive,self.connection_provider=state,archive,connection_provider
        self.rows={};self.mode=None;self.own=None;self.bound=None;self.full=False
        self.last_at=0;self.fingerprint=None
        self.path=state.root/'runtime'/'client-union-relations.json'
        self.lock=threading.RLock()
        self.palette_bound=None;self.palette_at=0;self.palette_fingerprint=None
        try:
            old=json.loads(self.path.read_text(encoding='utf-8'))
            if old.get('profile_id')==archive.profile_id and type(old.get('directional')) is bool:
                self.mode=old['directional']
        except (OSError,ValueError):
            pass

    def context(self,connection):
        with self.state.lock:
            own=dict(self.state.config['self']);h=self.state.health;epoch=self.state.epoch
            if (not connection or connection!=self.connection_provider() or connection!=h.get('connection')
                    or h.get('status')!='live' or h.get('identity_pending') or h.get('scope_pending')
                    or not own.get('confirmed') or str(own['union_id'])!=str(self.archive.profile['union_id'])):
                return None
        key=(self.archive.profile_id,own['user_id'],own['union_id'],digest(connection),epoch)
        if key!=self.bound:
            if self.bound and self.bound[0]!=key[0]:
                self.mode=None
            self.rows={};self.own=int(own['union_id']);self.full=False;self.last_at=0;self.fingerprint=None
            self.bound=key
        return own,key

    def cache(self,frame,connection,received_at):
        with self.lock:
            return self._cache(frame,connection,received_at)

    def palette(self,frame,connection,received_at):
        """Only publish a complete, fresh palette from this verified role/session."""
        with self.lock:
            context=self.context(connection)
            if context is None:
                return False
            own,key=context
            if self.palette_bound!=key:
                self.palette_bound=key;self.palette_at=0;self.palette_fingerprint=None
                with self.state.lock:self.state.health['relation_colors']={'status':'waiting'}
            if not isinstance(frame,dict) or frame.get('status')!='ready':
                if isinstance(frame,dict) and frame.get('status')in ('waiting','busy','unavailable','scope_changed'):
                    error=frame.get('error_type','')
                    with self.state.lock:
                        self.state.health['relation_colors']={'status':frame['status'],
                            'error_type':error if isinstance(error,str)and re.fullmatch('[A-Za-z]{0,80}',error)else ''}
                return False
            try:
                expected_server=re.search(r'\d+$',self.archive.profile['server'])
                if (frame.get('game_requests_sent')!=0 or frame.get('source')!='client_map_palette'
                        or str(frame.get('user_id')) not in (own['user_id'],str(self.archive.profile['account_role_id']))
                        or str(frame.get('union_id'))!=own['union_id'] or not expected_server
                        or str(frame.get('server_id'))!=expected_server[0]
                        or type(frame.get('color_mode'))is not int or frame['color_mode']not in range(4)):
                    return False
                at=frame.get('read_at')
                if type(at)not in (int,float) or not received_at-10<=at<=received_at+1 or at<self.palette_at:
                    return False
                styles=frame.get('styles')
                if not isinstance(styles,dict) or set(styles)!={'self','union','other','enemy','bandit','friend_other','friend_bandit'}:
                    return False
                for style in styles.values():
                    if not isinstance(style,dict) or set(style)!={'inside','outside'} or any(
                            not isinstance(color,str)or not re.fullmatch('#[0-9a-f]{6}',color)for color in style.values()):
                        return False
                result={k:frame[k]for k in ('source','color_mode','styles')}
                result.update(status='ready',user_id=own['user_id'],own_union_id=own['union_id'],
                              last_seen_at=at,profile_id=self.archive.profile_id)
                fingerprint=digest([key,result['color_mode'],styles])
                with self.state.lock:
                    if self.context(connection)!=context:return False
                    self.state.health['relation_colors']=result
                self.palette_at=at
                if fingerprint!=self.palette_fingerprint:
                    write_json(self.state.root/'runtime'/'client-relation-colors.json',result)
                    self.palette_fingerprint=fingerprint
                return True
            except (ValueError,TypeError,KeyError,OverflowError):
                return False

    def _cache(self,frame,connection,received_at):
        try:
            context=self.context(connection)
            if context is None or not isinstance(frame,dict) or frame.get('status')!='ready':
                return False
            own,key=context
            positive(frame.get('server_id'));positive(frame.get('union_id'),zero=True)
            if (frame.get('game_requests_sent')!=0 or type(frame.get('directional')) is not bool
                    or str(frame.get('user_id')) not in (own['user_id'],str(self.archive.profile['account_role_id']))
                    or str(frame['union_id'])!=own['union_id']):
                raise ValueError('客户端同盟关系不属于当前角色')
            at=frame.get('read_at')
            if type(at) not in (int,float) or not received_at-10<=at<=received_at+1:
                raise ValueError('客户端同盟关系快照已过期')
            if at<self.last_at:
                return False
            rows=records(frame.get('records'))
            expected=effective(rows,frame['union_id'],frame['directional'])
            actual={}
            pairs=frame.get('effective')
            if not isinstance(pairs,list) or len(pairs)>512:
                raise ValueError('客户端合成关系格式变化')
            for pair in pairs:
                if not isinstance(pair,list) or len(pair)!=2 or type(pair[1]) is not int or pair[1] not in (0,1,2):
                    raise ValueError('客户端合成关系编码变化')
                uid=str(positive(pair[0]))
                if uid in actual:
                    raise ValueError('客户端合成关系重复')
                actual[uid]=pair[1]
            if actual!=expected:
                raise ValueError('客户端关系缓存正在变化，等待一致快照')
            self.rows,self.mode,self.full=rows,frame['directional'],True
            return self.publish(own,key,at,'client_cache',actual)
        except (ValueError,TypeError,KeyError,OverflowError) as error:
            self.error(str(error))
            return False

    def packet(self,mid,value,connection,at):
        with self.lock:
            return self._packet(mid,value,connection,at)

    def _packet(self,mid,value,connection,at):
        if mid not in (6326,6327):
            return False
        try:
            context=self.context(connection)
            if context is None or self.mode is None or at<self.last_at:
                return False
            own,key=context
            if (not isinstance(value,list) or len(value)<2 or not isinstance(value[0],list)
                    or len(value[0])<2 or not isinstance(value[1],list) or len(value[1])<2):
                raise ValueError('客户端关系通知格式变化')
            updated=records(value[0][1])
            removed=value[1][1]
            if not isinstance(removed,list) or len(removed)>512:
                raise ValueError('客户端关系删除列表格式变化')
            removed=[positive(uid)for uid in removed]
            if mid==6327 and not self.full:
                return False
            rows={} if mid==6326 else dict(self.rows)
            for uid in removed:
                rows.pop(uid,None)
            rows.update(updated)
            if len(rows)>512:
                raise ValueError('客户端关系记录超过核对范围')
            self.rows,self.full=rows,True
            return self.publish(own,key,at,'notify_'+str(mid),effective(rows,self.own,self.mode))
        except (ValueError,TypeError,KeyError,OverflowError) as error:
            self.error(str(error))
            return False

    def publish(self,own,key,at,source,resolved):
        frame={'profile_id':key[0],'user_id':own['user_id'],'own_union_id':own['union_id'],
               'directional':self.mode,'effective':resolved,'records':list(self.rows.values())}
        fingerprint=digest(frame)
        self.last_at=max(self.last_at,at)
        with self.state.lock:
            self.state.health['union_relations']={'status':'ready','source':source,'last_seen_at':at,
                'own_union_id':own['union_id'],'user_id':own['user_id'],
                'friendly':sum(code==1 for code in resolved.values()),'enemy':sum(code==2 for code in resolved.values())}
        if fingerprint==self.fingerprint:
            return False
        entries={uid:{'relation':LABELS[code],'source':'client','client_code':code,'verified_at':at,
                      'own_union_id':own['union_id'],'user_id':own['user_id'],'profile_id':key[0]}
                 for uid,code in resolved.items() if uid!=own['union_id']}
        with self.archive.lock,self.archive.db:
            if self.archive.profile_id!=key[0]:
                return False
            self.archive._event('client_union_relations',own['union_id'],{**frame,'source':source},at,None)
            self.archive.intelligence.analysis_cache.clear()
        with self.state.lock:
            if (self.bound!=key or self.state.config['self']['user_id']!=own['user_id']
                    or self.state.config['self']['union_id']!=own['union_id'] or self.state.epoch!=key[4]):
                return False
            self.state.client_relations=entries
            overrides={uid:r for uid,r in self.state.config['relations'].items() if r.get('manual_override')
                       and r.get('own_user_id') in (None, own['user_id'])}
            combined={**entries,**overrides}
            if combined!=self.state.config['relations']:
                self.state.config['relations']=combined
                write_json(self.state.config_path,self.state.config)
        write_json(self.path,{**frame,'observed_at':at,'source':source})
        self.fingerprint=fingerprint
        return True

    def error(self,message):
        with self.state.lock:
            status=self.state.health.get('union_relations',{})
            self.state.health['union_relations']={**status,'status':status.get('status','waiting'),
                                                  'last_error':message[:160]}
