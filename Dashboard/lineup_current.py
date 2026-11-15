"""Whole-report updates of the same three physical hero cards, never an army-ID merge."""
import hashlib
import json

from observation_codec import map_troop_codes, report_troop_codes, short_lineup_hero_count


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
    if not identity or not identity['pairs']:
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
        if other['pairs'] is not None and other['pairs'] != identity['pairs']:
            rejected.append({'battle_id':report['battle_id'], 'reason':'card_template_conflict'})
            continue
        selected, selected_identity = report, other
    evidence = {'source':'same_card_instances' if selected is not anchor else 'army_reference',
                'anchor_battle_id':anchor['battle_id'], 'battle_id':selected['battle_id'],
                'anchor_army_id':army_id, 'role_id':identity['role_id'],
                'card_instances':list(selected_identity['cards']),
                'complete_roster':selected_identity['pairs'] is not None, 'rejected':rejected}
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
    reported = report_troop_codes(current.get('troop_raw'),current['side'])
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
