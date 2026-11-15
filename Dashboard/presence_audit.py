"""Bounded, read-only consistency checks against the received region notifications."""
from collections import Counter, OrderedDict, deque
import logging


class PresenceAudit:
    def __init__(self):
        self.epoch = None
        self.groups = OrderedDict()
        self.states = OrderedDict()
        self.transitions = Counter()
        self.checked = self.preserved = self.mismatches = 0
        self.last_at = 0
        self.issues = deque(maxlen=30)
        self.recent = deque(maxlen=80)

    def observe(self, state, value, at):
        if state.health.get('capture_source') != 'dashen':
            return
        if self.epoch != state.epoch:
            self.groups.clear()
            self.states.clear()
            self.epoch = state.epoch
        self.last_at = at
        scope, order = value[20], value[18]
        scoped = (isinstance(scope, list) and len(scope) == 2 and scope[0] == 2
                  and isinstance(scope[1], int) and scope[1] > 0 and isinstance(order, int) and order > 0)
        affected = set()
        if scoped:
            region = str(scope[1])
            # Replay each region's last notification independently of WorldState's visibility decision.
            for aid in value[7] if isinstance(value[7], list) else ():
                key = (order, str(aid))
                group = self.groups.setdefault(key, {'regions': {}, 'updated': False, 'removed': False})
                group['regions'][region] = False
                group['removed'] = True
                affected.add(key)
        for aid, raw in value[6].items() if isinstance(value[6], dict) else ():
            if not isinstance(raw, list) or len(raw) < 33:
                continue
            aid = str(aid)
            previous = self.states.get(aid)
            if previous is not None and previous != raw[0]:
                self.transitions[f'{previous}->{raw[0]}'] += 1
                self.recent.append({'army_id': aid, 'from_state': previous, 'to_state': raw[0], 'at': at,
                                    'from_wid': raw[2], 'target_wid': raw[3]})
            self.states[aid] = raw[0]
            self.states.move_to_end(aid)
            if scoped:
                key = (order, aid)
                group = self.groups.setdefault(key, {'regions': {}, 'updated': False, 'removed': False})
                group['regions'][region] = raw[0] != 0
                group['updated'] = raw[0] != 0
                affected.add(key)
        for key in affected:
            group, row = self.groups[key], state.armies.get(key[1])
            if not group['updated'] or not group['removed'] or not row or row.get('update_order') != key[0]:
                continue
            expected = any(group['regions'].values())
            stamp = (tuple(sorted(group['regions'].items())), bool(row.get('visible')))
            if group.get('checked') == stamp:
                continue
            group['checked'] = stamp
            self.checked += 1
            if expected and row.get('visible'):
                self.preserved += 1
            if expected != bool(row.get('visible')):
                self.mismatches += 1
                self.issues.append({'army_id': key[1], 'order': key[0], 'at': at,
                                    'expected_visible': expected, 'actual_visible': bool(row.get('visible')),
                                    'regions': dict(group['regions']), 'state_code': row['state_code']})
                logging.warning('Region presence mismatch for army %s at version %s; see runtime/presence-audit.json',
                                key[1], key[0])
        while len(self.groups) > 1024:
            self.groups.popitem(last=False)
        while len(self.states) > 2048:
            self.states.popitem(last=False)
        state.health['presence_audit'] = self.summary()

    def summary(self):
        return {'last_at': self.last_at, 'epoch': self.epoch, 'checked': self.checked,
                'preserved_cross_region': self.preserved, 'mismatches': self.mismatches,
                'transitions': dict(self.transitions), 'tracked_armies': len(self.states),
                'last_issue': self.issues[-1] if self.issues else None}

    def report(self):
        return {**self.summary(), 'recent_transitions': list(self.recent), 'issues': list(self.issues)}
