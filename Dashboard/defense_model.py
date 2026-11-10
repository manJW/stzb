"""Shrink comparable defender outcomes toward the observed alliance baseline."""
from functools import lru_cache
from math import exp, log

from lineup_prediction import troop_total


def profile(heroes):
    ids = frozenset(h['id'] for h in heroes)
    level = sum(h.get('level') or 0 for h in heroes)
    red = sum(h['red_stars'] for h in heroes) if all(h.get('red_stars') is not None for h in heroes) else None
    gear = sum((h.get('gear') or {}).get('level', 0) for h in heroes) if all(h.get('gear_reported') for h in heroes) else None
    skills = frozenset(s['id'] for h in heroes for s in h['skills']) if all(h.get('skills') for h in heroes) else None
    return ids, (level, red, gear), skills, troop_total(heroes, 'initial_hp')


def context(own, enemy):
    gaps = tuple(a - b if a is not None and b is not None else None for a, b in zip(own[1], enemy[1]))
    troop_gap = log(own[3] / enemy[3]) if own[3] and enemy[3] else None
    return own[0], enemy[0], (*gaps, troop_gap), (own[2], enemy[2])


def estimate(records, matchup, prior, *, use_skills=False, use_troops=False):
    own, enemy, gaps, *skill_pairs = matchup
    lineup_weight = lineup_wins = power_weight = power_wins = 0.
    skill_weight = skill_wins = power_support = 0.
    lineup_cases = power_cases = 0
    skill_cases = troop_cases = 0
    troop_gap = gaps[3] if use_troops and len(gaps) > 3 else None
    for (past_own, past_enemy, past_gaps, *past_skill_pairs), won in records:
        overlap = len(own & past_own)
        if overlap >= 2:
            weight = (overlap / 3) ** 3
            if enemy == past_enemy:
                weight *= 1.5
            lineup_weight += weight
            lineup_wins += weight * won
            lineup_cases += 1
        comparable = [(a, b, scale) for a, b, scale in zip(gaps, past_gaps, (7, 6, 12))
                      if a is not None and b is not None]
        if len(comparable) >= 2:
            weight = exp(-sum(abs(a - b) / scale for a, b, scale in comparable))
            power_support += weight
            if troop_gap is not None and len(past_gaps) > 3 and past_gaps[3] is not None:
                weight *= exp(-abs(troop_gap - past_gaps[3]) / .35)
                troop_cases += weight >= .25
            power_weight += weight
            power_wins += weight * won
            power_cases += weight >= .25
        if use_skills and skill_pairs and past_skill_pairs and all(skill_pairs[0]) and all(past_skill_pairs[0]):
            shared = [len(a & b) / len(a | b) for a, b in zip(skill_pairs[0], past_skill_pairs[0])]
            if min(shared) >= .25:
                weight = shared[0] * shared[1]
                skill_weight += weight
                skill_wins += weight * won
                skill_cases += 1
    if power_support < 2:
        return None
    by_lineup = (lineup_wins + 7 * prior) / (lineup_weight + 7)
    by_power = (power_wins + 7 * prior) / (power_weight + 7)
    probability = .65 * by_lineup + .35 * by_power
    if skill_weight >= 2:
        probability = .8 * probability + .2 * (skill_wins + 7 * prior) / (skill_weight + 7)
    return {'probability': probability, 'lineup_cases': lineup_cases, 'power_cases': power_cases, 'troop_cases': troop_cases,
            'skill_cases': skill_cases if skill_weight >= 2 else 0, 'strength_only': lineup_weight < .75}


def observations(cases):
    return tuple((case['event_at'], case['available_at'], case['model_context'], case['outcome'] == 'win') for case in cases)


@lru_cache(maxsize=8)
def validate(records, use_skills=False, *, use_troops=False, start=.7, end=1.):
    """Replay the newest 30%, admitting outcomes only after archive receipt."""
    available = iter(sorted(records, key=lambda row: row[1]))
    next_case = next(available, None)
    known, wins = [], 0
    model_error = base_error = 0.
    evaluated = 0
    for at, _, matchup, won in records[int(len(records) * start):int(len(records) * end)]:
        while next_case and next_case[1] < at:
            _, _, earlier_matchup, earlier_won = next_case
            known.append((earlier_matchup, earlier_won))
            wins += earlier_won
            next_case = next(available, None)
        prior = (wins + 1) / (len(known) + 2)
        prediction = estimate(known, matchup, prior, use_skills=use_skills, use_troops=use_troops)
        if prediction:
            model_error += (prediction['probability'] - won) ** 2
            base_error += (prior - won) ** 2
            evaluated += 1
    return {'samples': evaluated, 'model_brier': model_error / evaluated if evaluated else None,
            'baseline_brier': base_error / evaluated if evaluated else None,
            'validated': evaluated >= 20 and model_error < base_error}
