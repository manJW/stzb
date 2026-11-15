"""Link exact local client selections to current map armies without game orders."""
from collections import deque
import json
import re
import threading
import time


class GameArmyFocus:
    def __init__(self, state, bridge, connection_provider, *, interval=.5, relation_observer=None, palette_observer=None):
        self.state, self.bridge, self.connection_provider = state, bridge, connection_provider
        self.interval = interval
        self.relation_observer = relation_observer
        self.palette_observer = palette_observer
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True, name='selected-game-army')
        self.published = None
        self.events = deque(maxlen=30)
        self.path = state.root/'runtime'/'game-army-focus.json'

    def start(self):
        self.thread.start()

    def close(self):
        self.stop_event.set()
        if self.thread.is_alive():
            self.thread.join(timeout=3)

    def apply(self, value, connection, received_at):
        if not isinstance(value, dict) or value.get('game_requests_sent') != 0:
            return False
        # Opening a game's army panel can immediately cancel its map highlight.
        # Preserve the exact last personal selection, even when select/cancel occur between polls.
        if value.get('status') in ('cancelled','unsupported_selection') and value.get('last_sequence'):
            value = {**value,'army_id':value.get('last_army_id'),'sequence':value['last_sequence'],
                     'group_id':value.get('last_group_id'),'selected_at':value.get('last_selected_at'),
                     'status':'selected_group' if value.get('last_group_id') else 'selected'}
        reader, sequence, selected_at = value.get('reader_id'), value.get('sequence'), value.get('selected_at')
        if (not isinstance(reader, str) or not re.fullmatch('[a-f0-9]{32}', reader)
                or type(sequence) is not int or sequence <= 0
                or type(selected_at) not in (int, float) or selected_at <= 0 or selected_at > received_at+1):
            return False
        army = value.get('army_id')
        group = value.get('group_id')
        status = value.get('status')
        if (status == 'selected' and (not isinstance(army, str) or not re.fullmatch('[1-9][0-9]{0,8}', army))
                or status not in ('selected','selected_group','cancelled','unsupported_selection')
                or status != 'selected' and army is not None
                or status == 'selected_group' and (not isinstance(group,str) or not re.fullmatch('[1-9][0-9]{0,8}',group))
                or status != 'selected_group' and group is not None):
            return False
        key = (reader, sequence)
        if connection != self.connection_provider():
            return False
        repeated = key == self.published
        if not self.state.set_game_army_focus(army, connection, received_at, selected_at, repeated=repeated,group_id=group):
            return False
        if repeated:
            return False
        self.published = key
        self.events.append({'army_id':army,'group_id':group,'selected_at':selected_at,'received_at':received_at,'status':status})
        self.state.health['game_army_focus'] = {'status':'ready','last_selected_at':selected_at,
                                               'army_id':army,'group_id':group,'method':'client_selection_event'}
        return True

    def step(self):
        connection = self.connection_provider()
        if not connection:
            return False
        value = self.bridge.read_map_selection()
        if self.relation_observer and isinstance(value,dict) and isinstance(value.get('union_relations'),dict):
            self.relation_observer(value['union_relations'],connection,time.time())
        if self.palette_observer and isinstance(value,dict) and isinstance(value.get('relation_colors'),dict):
            self.palette_observer(value['relation_colors'],connection,time.time())
        if isinstance(value,dict) and value.get('sequence') == 0:
            with self.state.lock:
                self.state.health['game_army_focus'] = {'status':'listening','method':'client_selection_event'}
        changed = self.apply(value, connection, time.time())
        if changed:
            self.path.write_text(json.dumps({'events':list(self.events)},ensure_ascii=False,indent=2),encoding='utf8')
        return changed

    def run(self):
        while not self.stop_event.is_set():
            delay = self.interval
            try:
                self.step()
            except Exception as error:
                with self.state.lock:
                    self.state.health['game_army_focus'] = {'status':'retrying','error':type(error).__name__,
                                                           'message':str(error)[:160]}
                delay = 5
            self.stop_event.wait(delay)
