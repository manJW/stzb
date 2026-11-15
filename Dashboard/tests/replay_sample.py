"""Verify the live framer against the already validated local capture, offline."""
import collections
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from collector import DECODER, MessageFramer
from model import WorldState, write_json

capture = ROOT.parent / "Captures" / "game-validation-20261105-141321.pcapng"
received, reference, errors = [], [], []
for payload, client in DECODER.follow_stream(capture, 0):
    if client:
        continue
    DECODER.scan_frames(payload, False, {}, collections.Counter(),
                        lambda direction, mid, value: reference.append((mid, value)))
    framer = MessageFramer(lambda mid, value, stamp: received.append((mid, value)), lambda: errors.append(1))
    # Deliberately cut every frame across small, unrelated chunk boundaries.
    for offset in range(0, len(payload), 137):
        framer.feed(payload[offset:offset + 137], 1793859201)
assert received == reference, "Incremental decoder disagrees with validated offline decoder"
assert not errors, "Unexpected decode errors in validated sample"
test_root = ROOT / "runtime" / "test-runs"
test_root.mkdir(parents=True, exist_ok=True)
folder = Path(tempfile.mkdtemp(prefix="replay-", dir=test_root)).resolve()
try:
    state = WorldState(folder)
    for mid, value in received:
        state.apply(mid, value, 1793859201)
    summary = {"source": capture.name, "server_messages": len(received),
        "framer_matches_validated_decoder": True, "decode_errors": len(errors),
        "scene_messages_accepted": state.health["scenes"],
        "schema_errors": state.health["schema_errors"],
        "unique_armies": len(state.armies), "players": len(state.players),
        "historical_replay_only": True}
    assert summary["schema_errors"] == 0
    assert summary["scene_messages_accepted"] == 88
    assert summary["unique_armies"] == 461
    write_json(ROOT.parent / "Reports" / "dashboard-validation-20261105.json", summary)
    print(json.dumps(summary, ensure_ascii=True))
finally:
    if folder.parent != test_root.resolve() or not folder.name.startswith("replay-"):
        raise RuntimeError("Unexpected test cleanup target")
    shutil.rmtree(folder)
