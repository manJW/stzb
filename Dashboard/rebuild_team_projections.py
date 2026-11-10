"""Rebuild derived lineup views on a copy, preserving source evidence and a backup.

Stop the local service first. Only --apply replaces the database after validation.
"""
import argparse
import ctypes
import json
import os
import sqlite3
import socket
import time
import zlib
from pathlib import Path
from archive import Archive, dictionaries, number


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    if os.name == "nt":
        service = root / "runtime" / "service.json"
        pid = json.loads(service.read_text(encoding="utf-8")).get("pid") if service.exists() else None
        if pid:
            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
            if handle:
                code = ctypes.c_ulong()
                success = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
                ctypes.windll.kernel32.CloseHandle(handle)
                if not success or code.value == 259:
                    raise RuntimeError("Stop Dashboard and wait for its process to exit before rebuilding")
    else:
        try:
            socket.create_connection(("127.0.0.1", 8787), timeout=2).close()
        except ConnectionRefusedError:
            pass
        else:
            raise RuntimeError("Stop Dashboard before rebuilding derived views")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    original = (root / "data" / "intelligence.sqlite3").resolve()
    working = original.with_name("intelligence-rebuild-" + stamp + ".sqlite3")
    backup = original.with_name("intelligence-before-skill-fix-" + stamp + ".sqlite3")
    if working.exists() or backup.exists() or working.parent != original.parent:
        raise RuntimeError("Unexpected output paths")
    source = Archive(root)
    receipt_rows = source.db.execute("SELECT id,profile_id,message_id,payload,first_seen FROM messages ORDER BY first_seen").fetchall()
    profiles = source.db.execute("SELECT id,data FROM profiles").fetchall()
    dest = sqlite3.connect(working)
    source.db.backup(dest)
    dest.close()
    source.close()
    rebuilt = Archive(root, database_path=working)
    records = []
    for row in receipt_rows:
        for record in dictionaries(json.loads(zlib.decompress(row["payload"]))):
            if record.get("battle_id") and ("attack_name" in record or "defend_name" in record):
                records.append((number(record.get("time")), row["first_seen"], row["profile_id"], record, row["id"]))
    records.sort(key=lambda r: (r[0], r[1]))
    profile_map = {row["id"]: json.loads(row["data"]) for row in profiles}
    with rebuilt.lock, rebuilt.db:
        rebuilt.db.execute("DELETE FROM team_current")
        rebuilt.db.execute("DELETE FROM team_versions")
        rebuilt.db.execute("DELETE FROM observations WHERE kind='team_configuration_changed'")
        for event_at, observed_at, profile_id, record, message_ref in records:
            rebuilt.profile_id, rebuilt.profile = profile_id, profile_map[profile_id]
            # Link each normalized version to immutable battle evidence already
            # present in the copied database, without fabricating a new receipt.
            rebuilt._battle(record, observed_at, message_ref)
    integrity = rebuilt.db.execute("PRAGMA integrity_check").fetchone()[0]
    versions = rebuilt.db.execute("SELECT COUNT(*) FROM team_versions").fetchone()[0]
    if integrity != "ok" or (records and not versions):
        raise RuntimeError("Rebuilt data failed verification; original is unchanged")
    rebuilt.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    rebuilt.close()
    result = {"source_battle_receipts": len(records), "rebuilt_versions": versions,
        "integrity": integrity, "applied": args.apply, "working": str(working)}
    if args.apply:
        # Keep the entire pre-upgrade file. Both moves are single files in the
        # verified data directory; no source observations are deleted.
        os.replace(original, backup)
        try:
            os.replace(working, original)
        except OSError:
            os.replace(backup, original)
            raise
        result["backup"] = str(backup)
    (root.parent / "Reports" / ("lineup-rebuild-" + stamp + ".json")).write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
