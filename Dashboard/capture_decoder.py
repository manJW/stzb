"""Decode bounded, read-only message frames for the dashboard collector."""
import json
import re
import zlib

MAX_FRAME = 16 * 1024 * 1024
MAX_JSON = 8 * 1024 * 1024


def json_value(data):
    if len(data) > MAX_JSON:
        raise ValueError('JSON body exceeds diagnostic limit')
    for encoding in ('utf-8', 'gbk'):
        try:
            text = data.rstrip(b'\0').decode(encoding)
        except UnicodeError:
            continue
        try:
            return json.loads(text)
        except (ValueError, RecursionError):
            if text.startswith(('{', '[')):
                normalized = re.sub(r'([{,])(-?\d+):', r'\1"\2":', text)
                try:
                    return json.loads(normalized)
                except (ValueError, RecursionError):
                    pass
    raise ValueError('Not a supported JSON body')


def inflate(data):
    decoder = zlib.decompressobj()
    result = decoder.decompress(data, MAX_JSON + 1)
    if len(result) > MAX_JSON or not decoder.eof:
        raise ValueError('Incomplete or oversized compressed body')
    return result


def decode_body(frame, client):
    header = 57 if client else 13
    encoding = frame[header - 1]
    raw = frame[header:]
    candidates = []
    if encoding == 2:
        candidates.append(raw)
    elif encoding == 3:
        for compressed in (raw, raw[4:]):
            try:
                candidates.append(inflate(compressed))
            except (zlib.error, ValueError):
                pass
    elif encoding == 5:
        keys = (frame[55],) if client else tuple(dict.fromkeys((0x98, frame[7])))
        for key in keys:
            plain = bytes(byte ^ key for byte in raw)
            candidates.append(plain)
            try:
                candidates.append(inflate(plain))
            except (zlib.error, ValueError):
                pass
    for candidate in candidates:
        try:
            return json_value(candidate)
        except ValueError:
            continue
    raise ValueError('Unsupported body encoding or JSON shape')
