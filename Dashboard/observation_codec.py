"""Shared business-message helpers for the archive and member collector."""
import hashlib
import json
import re

TROOP_NAMES = {0: "空位", 1: "弓兵", 2: "步兵", 3: "骑兵"}


def number(value, default=0):
    try:
        return int(value)
    except (ValueError, TypeError, OverflowError):
        return default


def packed(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(packed(value).encode("utf-8")).hexdigest()


def coord(wid):
    x, y = divmod(number(wid), 10000)
    return [x, y] if 0 < x < 10000 and 0 < y < 10000 else None


def integers(value, separator=","):
    return [number(part) for part in str(value or "").split(separator) if part.strip()]


def map_troop_codes(value):
    if not isinstance(value, str):
        return ()
    try:
        pairs = [tuple(map(int, slot.split(","))) for slot in value.rstrip(";").split(";")]
    except ValueError:
        return ()
    if len(pairs) != 3 or any(len(pair) != 2 for pair in pairs):
        return ()
    if any(pair != (-1, -1) and (pair[0] not in (1, 2, 3) or pair[1] <= 0 or pair[1] % 10 not in (1, 2, 3))
           for pair in pairs):
        return ()
    codes = tuple(max(0, pair[1]) for pair in pairs)
    return codes if any(codes) else ()


def report_troop_codes(value, side):
    if not isinstance(value, str) or side not in ("attack", "defend"):
        return ()
    try:
        codes = tuple(map(int, value.rstrip(",").split(",")))
    except ValueError:
        return ()
    # ReportProfileInfo reverses the defender list before reading slots 1–3.
    if side == "defend":
        codes = codes[::-1]
    if len(codes) != 4 or codes[0] != 0:
        return ()
    slots = codes[1:]
    return slots if any(slots) and all(code == 0 or code > 0 and code % 10 in (1, 2, 3) for code in slots) else ()


def troop_families(codes):
    # The PC map renderer selects the model from the last digit of each code.
    return tuple(code % 10 for code in codes)


def describe_troops(codes):
    return [{"position": position, "code": code, "name": TROOP_NAMES[family]}
            for position, (code, family) in enumerate(zip(codes, troop_families(codes)), 1)]


def short_lineup_hero_count(codes):
    if len(codes) != 3:
        return None
    occupied = sum(code > 0 for code in codes)
    return occupied if occupied in (1, 2) and codes.count(0) == 3 - occupied else None


def dictionaries(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from dictionaries(child)
    elif isinstance(value, list):
        for child in value:
            yield from dictionaries(child)


def clean(value):
    """Only business bodies are stored; authentication-like keys are excluded."""
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items()
                if not re.search(r"password|passwd|token|session|cookie|secret|credential|device_id|phone|email", str(k), re.I)}
    if isinstance(value, list):
        return [clean(v) for v in value]
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_+/=\-]{32,}", value) and not value.isdigit():
        return {"opaque_value_removed": True, "length": len(value)}
    return value

