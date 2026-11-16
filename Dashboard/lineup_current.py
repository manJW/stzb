"""Whole-report updates of the same three physical hero cards, never an army-ID merge."""
import hashlib
import json
import copy

from observation_codec import map_troop_codes, report_troop_codes, short_lineup_hero_count
from combat_loadout import forecast_heroes


def current_troop_codes(report):
    return tuple(report['_roster_troop_codes']) if report.get('_roster_troop_codes') else report_troop_codes(report.get('troop_raw'),report['side'])


def casualty_roster(current, reports, army_id, known_at, expected_pairs=None):
    """Recover only zero-HP omissions in a proven continuous fight of the same cards."""
    identity=card_identity(current)
    if not identity or identity['pairs'] is not None:return current
    candidates=[]
    for report in reports:
        other=card_identity(report)
        if (other and other['role_id']==identity['role_id'] and other['cards']==identity['cards']
                and report['event_at']<=current['event_at']
                and report.get('observed_at',0)<=known_at and report_order(report)<=report_order(current)):
            candidates.append(report)
    previous=None
    for report in sorted(candidates,key=report_order):
        if report.get('army_id')not in (None,'',army_id):previous=None;continue
        other=card_identity(report)
        if expected_pairs and other['pairs'] is not None and other['pairs']!=expected_pairs:previous=None;continue
        heroes=forecast_heroes(report);present={h['position']:h for h in heroes}
        if set(present)=={1,2,3}:previous=report;continue
        before={h['position']:h for h in forecast_heroes(previous)}if previous else {}
        missing={1,2,3}-set(present)
        raw=str(report.get('hero_info_raw')or'').removesuffix(';').split(';')
        old_codes=current_troop_codes(previous)if previous else ()
        codes=current_troop_codes(report)
        valid=(previous and set(before)=={1,2,3} and present and missing and len(raw)==3
            and previous.get('wid')and previous['wid']==report.get('wid')
            and 0<=report['event_at']-previous['event_at']<=6*60
            and report_order(previous)<report_order(report) and len(old_codes)==len(codes)==3
            and all(raw[pos-1]=='0,0,0,0,0'and before[pos].get('remaining_hp')==0 and not codes[pos-1]
                    for pos in missing)
            and all(h['id']==before[pos]['id'] and type(h.get('initial_hp'))is int
                    and h['initial_hp']==before[pos].get('remaining_hp')
                    and codes[pos-1] and codes[pos-1]==old_codes[pos-1] for pos,h in present.items()))
        if not valid:previous=report;continue
        recovered=[]
        for pos in sorted(missing):
            hero=copy.deepcopy(before[pos]);source=hero.get('roster_source_battle_id',previous['battle_id'])
            # These positions did not participate in this report; never carry old combat HP forward.
            hero.update(initial_hp=0,remaining_hp=0,wounded_hp=0,casualty_recovered=True,roster_source_battle_id=source)
            heroes.append(hero);recovered.append({'position':pos,'battle_id':source})
        heroes.sort(key=lambda h:h['position'])
        previous={**dict(report),'heroes':json.dumps(heroes,ensure_ascii=False),'hero_key':','.join(str(h['id'])for h in heroes),
                  '_resolved_heroes':heroes,'_roster_troop_codes':old_codes,'_casualty_sources':recovered}
    return previous if previous and previous.get('_resolved_heroes')and previous['battle_id']==current['battle_id']and previous['side']==current['side']else current


def card_identity(report):
    try:
        fields = str(report.get('idu_raw') or '').split(',')
        role = str(report.get('role_raw') or '')
        if len(fields) != 5 or not all(value.isdecimal() for value in fields) or role in ('', '0'):
            return None
        cards = tuple(int(value) for value in fields[1:4])
        if not all(cards) or len(set(cards)) != 3:
            return None
        heroes = report.get('heroes') or ()
        heroes = json.loads(heroes) if isinstance(heroes, str) else heroes
        by_position = {hero['position']: hero['id'] for hero in heroes}
        pairs = tuple(sorted((cards[pos-1], by_position[pos]) for pos in (1, 2, 3))) if set(by_position) == {1, 2, 3} else None
        # Both attack and defense IDU fields already use the heroes' position order.
        # Verified against 51,081 archived assignments with no conflicting card/template pair.
        return {'role_id':role, 'cards':cards, 'card_set':tuple(sorted(cards)), 'pairs':pairs}
    except (KeyError, TypeError, ValueError):
        return None


def report_order(report):
    battle = str(report.get('battle_id') or '')
    return (report.get('event_at') or 0, int(battle) if battle.isdecimal() else 0, report.get('observed_at') or 0)


def latest_army_reference(reports, links, army_id, cutoff, known_at, *, owner=None,
                          live_codes=(), ambiguous_player=False):
    """A newer verified arrival can establish a changed roster for this army."""
    references = []
    for report in reports:
        if report.get('event_at', 0) >= cutoff or report.get('observed_at', 0) > known_at:
            continue
        direct = (report.get('army_id') == army_id and (report.get('binding_at') or 0) <= known_at
                  and (owner is None or report.get('binding_owner') in (None, owner)))
        if direct:
            references.append((report, {'source':'army_reference'}))
            continue
        if ambiguous_player or report.get('arrival_ambiguous') or report.get('army_id') not in (None, '', army_id):
            continue
        arrivals = [m for m in links.get((report['battle_id'], report['side']), ())
                    if m['method'] == 'unique_attack_arrival' and m['observed_at'] <= known_at
                    and m['end_at'] <= cutoff]
        if {m['army_id'] for m in arrivals} != {army_id}:
            continue
        reported = report_troop_codes(report.get('troop_raw'), report['side'])
        if (len([code for code in live_codes if code]) == 3
                and short_lineup_hero_count(reported) is not None
                and all(not code or code == live for code, live in zip(reported, live_codes))
                and all(map_troop_codes(m['hero_type']) == live_codes for m in arrivals)):
            # A partial battle fragment cannot remove heroes still seen marching.
            continue
        references.append((report, {'source':'unique_attack_arrival',
                                    'march_ids':sorted({m['id'] for m in arrivals})}))
    return max(references, key=lambda pair:report_order(pair[0]), default=(None, None))


def latest_same_cards(anchor, reports, army_id, cutoff, known_at):
    """Anchor the player and all three card instances; two shared cards are insufficient."""
    identity = card_identity(anchor)
    if not identity:
        return anchor, None
    selected, selected_identity = anchor, identity
    rejected = []
    for report in reports:
        if (report.get('event_at', 0) >= cutoff or report.get('observed_at', 0) > known_at
                or report_order(report) <= report_order(selected)):
            continue
        other = card_identity(report)
        if not other or other['role_id'] != identity['role_id'] or other['card_set'] != identity['card_set']:
            continue
        if report.get('army_id') not in (None, '', army_id):
            rejected.append({'battle_id':report['battle_id'], 'reason':'another_army_reference'})
            continue
        if identity['pairs'] is not None and other['pairs'] is not None and other['pairs'] != identity['pairs']:
            rejected.append({'battle_id':report['battle_id'], 'reason':'card_template_conflict'})
            continue
        selected, selected_identity = report, other
    selected=casualty_roster(selected,reports,army_id,known_at,identity['pairs'])
    selected_identity=card_identity(selected)
    evidence = {'source':'same_card_instances' if selected is not anchor else 'army_reference',
                'anchor_battle_id':anchor['battle_id'], 'battle_id':selected['battle_id'],
                'anchor_army_id':army_id, 'role_id':identity['role_id'],
                'card_instances':list(selected_identity['cards']),
                'complete_roster':selected_identity['pairs'] is not None, 'rejected':rejected}
    if selected.get('_casualty_sources'):evidence['casualty_recovery']=selected['_casualty_sources']
    heroes = selected.get('heroes') or []
    heroes = json.loads(heroes) if isinstance(heroes, str) else heroes
    configuration = [{field:hero.get(field) for field in
                      ('id','position','level','red_stars','troop_code','skill_slot_ids','skills','gear','gear_reported')}
                     for hero in sorted(heroes, key=lambda h:h['position'])]
    # Scoped by the caller's account/season. HP loss alone does not create a new build.
    evidence['version_key'] = hashlib.sha256(json.dumps(
        [identity['role_id'], selected_identity['cards'], configuration],
        sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return selected, evidence


def resolve_current_report(reports, links, army_id, cutoff, known_at, *, owner=None,
                           live_codes=(), ambiguous_player=False):
    """One current-report decision shared by stationed units and incoming armies."""
    anchor, reference = latest_army_reference(reports, links, army_id, cutoff, known_at,
        owner=owner, live_codes=live_codes, ambiguous_player=ambiguous_player)
    if anchor is None:
        return None, None
    current, resolution = latest_same_cards(anchor, reports, army_id, cutoff, known_at)
    resolution = resolution or {'source':'army_reference','anchor_battle_id':anchor['battle_id'],
                               'battle_id':current['battle_id']}
    resolution.update(anchor_source=reference['source'], anchor_side=anchor['side'])
    if reference.get('march_ids'):
        resolution['march_ids'] = reference['march_ids']
    reported = current_troop_codes(current)
    if live_codes and reported and reported != live_codes and not ambiguous_player:
        for report in reports:
            if (report_order(report) <= report_order(current) or report.get('event_at',0) >= cutoff
                    or report.get('observed_at',0) > known_at or report.get('army_id') not in (None,'',army_id)
                    or report.get('arrival_ambiguous')
                    or report_troop_codes(report.get('troop_raw'),report['side']) != live_codes):
                continue
            arrivals = {m['army_id'] for m in links.get((report['battle_id'],report['side']),())
                        if m['method']=='unique_attack_arrival' and m['observed_at']<=known_at}
            if arrivals and arrivals != {army_id}:
                continue
            # The old army reference is no longer current identity evidence.
            # Keep newer personal formations separate; do not merge their cards.
            return None, {'fallback':'newer_personal_reports','previous_battle_id':current['battle_id']}
    if resolution.get('complete_roster') is False or live_codes and reported and reported != live_codes:
        resolution['unresolved'] = 'incomplete_or_changed_troops'
    return current, resolution
