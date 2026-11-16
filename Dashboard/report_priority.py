"""Prioritize observed battle transitions without changing lineup identity rules."""
from collections import OrderedDict
import copy
import json
import threading
import time

from lineup_projection import VERIFIED
from model import write_json


class ReportPriority:
    def __init__(self, archive, harvester, reader, provider):
        self.archive, self.harvester, self.reader, self.provider = archive, harvester, reader, provider
        self.profile_id = archive.profile_id
        self.path = archive.root/'runtime'/('report-priority-'+self.profile_id+'.json')
        self.lock = threading.RLock()
        self.seen, self.events, self.updates = OrderedDict(), OrderedDict(), OrderedDict()
        self.focus = None
        self.head_due = 0
        self.published = {'active':0,'updates':{},'events':[]}

    def snapshot(self):
        # The map polling endpoint must never wait for evidence/database work.
        return copy.deepcopy(self.published)

    def _publish(self):
        self.published = {'active':sum(e['status']=='waiting' for e in self.events.values()),
                          'updates':dict(self.updates), 'events':copy.deepcopy(list(self.events.values())[-40:])}
        write_json(self.path,self.published)

    def displayed(self, army_id, version):
        with self.lock:
            event = self.events.get(army_id)
            if (not event or not event.get('lineup_ready_at') or self.updates.get(army_id)!=version
                    or event.get('shown_at')):
                return False
            event.update(shown_at=time.time(),shown_seconds=round(time.time()-event['trigger_at'],3))
            self._publish()
            return True

    def _previous(self, row):
        # On restart the preceding state can already be archived. An ordinary
        # manual recall before arrival must not masquerade as a post-battle return.
        with self.archive.read_snapshot(timeout=3) as db:
            old = db.db.execute('''SELECT state_code,begin_at AS begin,end_at AS end FROM live_army_states
                WHERE profile_id=? AND army_id=? AND observed_at<?
                ORDER BY observed_at DESC LIMIT 1''',
                (self.profile_id,row['id'],row['seen_at']-.01)).fetchone()
            return dict(old) if old else None

    def trigger(self, row, reason, observed_at, battle_after=None):
        aid = row['id']
        current = self.events.get(aid)
        if current and current['status']=='waiting':
            if reason not in current['reasons']: current['reasons'].append(reason)
            if battle_after is not None:
                current['battle_after'] = max(current.get('battle_after') or 0,battle_after)
            return
        self.events.pop(aid,None)
        self.events[aid] = {'army_id':aid, 'reasons':[reason], 'trigger_at':observed_at,
            'detected_at':time.time(), 'battle_after':battle_after, 'status':'waiting',
            'next_check':0, 'deadline':time.time()+120, 'owner':row.get('owner')}
        while len(self.events)>200: self.events.popitem(last=False)
        self.head_due = 0

    @staticmethod
    def ready(data, battle_after=None):
        if data.get('match') not in VERIFIED or data.get('current_resolution',{}).get('unresolved'):
            return None
        bid = data.get('current_report_id')
        row = next((r for r in data.get('items',[]) if r['battle_id']==bid),None)
        if not row or battle_after is not None and row['event_at']<battle_after:
            return None
        heroes = row['data'].get('heroes') or []
        if not heroes or any(not h.get('id') or not h.get('level') or h.get('red_stars') is None
                             or h.get('skills') is None or len(h.get('skill_slot_ids',[]))!=3
                             or any(s is None for s in h['skill_slot_ids']) for h in heroes):
            return None
        return row

    def step(self):
        if self.archive.profile_id!=self.profile_id: return
        snapshot = self.provider()
        if not snapshot: return
        now = time.time()
        rows = {r['id']:r for r in snapshot.get('armies',[])}
        changed = False
        with self.lock:
            for aid,row in rows.items():
                key = tuple(row.get(k) for k in ('owner','state_code','state_id','begin','end'))
                previous = self.seen.get(aid)
                if not previous or previous[0]!=key:
                    self.seen[aid]=(key,dict(row)); self.seen.move_to_end(aid)
                    if now-row.get('seen_at',0)<=15 and 0<=now-row.get('begin',0)<=90:
                        if row['state_code']==7 and row.get('target_type')!=13:
                            self.trigger(row,'battle_rest',row['seen_at'],row['begin']-2);changed=True
                        elif row['state_code'] in (4,13):
                            old = previous[1] if previous else self._previous(row)
                            if old and (old['state_code'] in (7,11) or old['state_code'] in (1,12)
                                        and abs(row['begin']-old['end'])<=15):
                                after = (old['begin'] if old['state_code']==7 else
                                         row['begin'] if old['state_code']==11 else old['end'])-2
                                self.trigger(row,'post_battle_retreat',row['seen_at'],after);changed=True
            while len(self.seen)>4096: self.seen.popitem(last=False)
            focus = snapshot.get('focus')
            focus_key = (focus,snapshot.get('focus_event'),tuple(snapshot.get('focus_members') or ()))
            if focus_key!=self.focus:
                self.focus = focus_key
                for focused_id in snapshot.get('focus_members') or [focus]:
                    row = rows.get(focused_id)
                    if not row:continue
                    with self.archive.read_snapshot(timeout=3) as db:
                        data = db.query('army_lineups',army_id=focused_id)
                    if self.ready(data) is None:
                        self.trigger(row,'selected_incomplete',now);changed=True
            for event in list(self.events.values()):
                if event['status']!='waiting' or event['next_check']>now: continue
                event['next_check']=now+2
                aid = event['army_id']
                if aid in rows and rows[aid].get('owner')!=event['owner']:
                    self.updates.pop(aid,None)
                    event['status']='owner_changed';changed=True;continue
                with self.archive.read_snapshot(timeout=3) as db:
                    data = db.query('army_lineups',army_id=aid)
                    ready = self.ready(data,event['battle_after'])
                    if ready:
                        full = db.db.execute('''SELECT observed_at FROM report_bodies
                            WHERE profile_id=? AND battle_id=? AND TRIM(body)<>'' ''',
                            (self.profile_id,ready['battle_id'])).fetchone()
                        received = db.db.execute('SELECT observed_at FROM battle_reports WHERE profile_id=? AND battle_id=?',
                                                (self.profile_id,ready['battle_id'])).fetchone()
                if ready:
                    event['battle_id']=ready['battle_id']
                    if not event.get('lineup_ready_at'):
                        event.update(lineup_ready_at=now,lineup_seconds=round(now-event['trigger_at'],3),
                                     report_received_at=received[0] if received else None,
                                     heroes=[{k:h.get(k) for k in ('id','name','level','red_stars')} for h in ready['data']['heroes']])
                        changed=True
                    version=ready['battle_id']+':'+ready['configuration_hash']
                    if self.updates.get(aid)!=version:
                        self.updates[aid]=version;self.updates.move_to_end(aid);changed=True
                    event['status']='done'
                    if full:event['full_at']=full[0]
                    changed=True;continue
                if now>=event['deadline']:
                    event['status']='timed_out';changed=True;continue
                # Enrichment only: no binding is created by requesting personal
                # reports. The shared resolver still proves each army separately.
                self.reader.prioritize_army(aid,urgent=True,current=data)
                self.harvester.yield_history(4)
                if not ready and time.monotonic()>=self.head_due:
                    self.harvester.request_latest();self.head_due=time.monotonic()+5
            while len(self.updates)>256:self.updates.popitem(last=False)
            if changed:self._publish()
