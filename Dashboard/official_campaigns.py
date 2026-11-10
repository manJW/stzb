"""World-map battle events received from the PC client."""
import json
import zlib

from observation_codec import integers, number


# Titles verified on the game's world-event list for this season. The server
# sends the event and map coordinate; the client renders its county title.
VERIFIED_NAMES = {('X0000', 'example-season'): {139: '谯之战', 265: '怀之战', 300: '城父之战'}}


def world_events(message_id, payload):
    if message_id == 3807 and isinstance(payload, list):
        for row in payload:
            if isinstance(row, list) and len(row) >= 7 and row[1] == 1:
                yield row[0], row[3], row[4], row[5], row[6]
    elif message_id == 3811 and isinstance(payload, dict) and payload.get('event_type') == 1:
        yield (payload.get('event_id'), payload.get('wid'), payload.get('begin_time'),
               payload.get('end_time'), payload.get('event_data'))


def map_block(x, y):
    return ((x - 1) // 10 + 1) * 1000 + (y - 1) // 10 + 1


def bounds(blocks):
    if not blocks:
        return None
    coords = [divmod(block, 1000) for block in blocks]
    return [(min(x for x, _ in coords) - 1) * 10 + 1, (min(y for _, y in coords) - 1) * 10 + 1,
            max(x for x, _ in coords) * 10, max(y for _, y in coords) * 10]


def campaigns_from_messages(rows, profile):
    names = VERIFIED_NAMES.get((profile['server'], profile['season']), {})
    own = str(profile['union_id'])
    events = {}
    for message in rows:
        for raw_id, wid, start, end, details in world_events(message['message_id'], json.loads(zlib.decompress(message['payload']))):
            event_id, wid, start, end = map(number, (raw_id, wid, start, end))
            if not event_id or not start or not isinstance(details, str):
                continue
            try:
                data = json.loads(details)
            except ValueError:
                continue
            if not isinstance(data, dict):
                continue
            participants = data.get('partake_union')
            if not isinstance(participants, list) or len(participants) % 6:
                continue
            unions = {str(uid) for uid in participants[::6] if number(uid) > 0}
            blocks = {b for b in integers(data.get('belong_block_ids')) if 0 < b // 1000 <= 200 and 0 < b % 1000 <= 200}
            if not blocks or not unions:
                continue
            event = events.setdefault(event_id, {'blocks': set(), 'unions': set(), 'from_at': start, 'to_at': 0})
            event['blocks'].update(blocks)
            event['unions'].update(unions)
            event['from_at'] = min(start, event['from_at'])
            if end:
                event['to_at'] = max(end, event['to_at'])
            event.update(center=list(divmod(wid, 10000)), current_blocks=sorted(blocks), observed_at=message['first_seen'])
    return {str(event_id): {'id':str(event_id), 'name':names.get(event_id, f'官方战役 #{event_id}'),
            'name_verified':event_id in names, 'source':'pc_world_event', 'center':event['center'],
            'blocks':sorted(event['blocks']), 'current_blocks':event['current_blocks'], 'unions':sorted(event['unions']),
            'observed_at':event['observed_at'],
            'filters':{'from_at':event['from_at'], 'to_at':event['to_at'], 'region':bounds(event['current_blocks'])}}
            for event_id, event in events.items() if own in event['unions'] and own not in ('', '0')}


def in_campaign(campaign, block, at, unions, *, current=False):
    timing = campaign['filters']
    return (block in campaign['current_blocks' if current else 'blocks'] and at >= timing['from_at']
            and (not timing['to_at'] or at <= timing['to_at']) and set(unions) <= set(campaign['unions']))


def report_campaign(campaigns, block, at, unions):
    if not block:
        return None
    matched = [event for event in campaigns.values() if in_campaign(event, block, at, unions)]
    if len(matched) != 1:
        return None
    event = matched[0]
    return {k:event[k] for k in ('id', 'name', 'name_verified', 'source')}
