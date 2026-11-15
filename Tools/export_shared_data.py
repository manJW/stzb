"""Export consistent business data without raw logins, captures or member keys."""
import argparse
import datetime
import lzma
import hashlib
import json
import re
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

EMPTY_TABLES = {'messages', 'client_table_events', 'pilot_members', 'message_fields',
                'message_redactions', 'report_read_jobs'}
PRIVATE_KEYS = {'password', 'passwd', 'access_token', 'refresh_token', 'auth_token',
                'session_key', 'authorization', 'cookie', 'login_marker',
                '_key_resfile_login_roleid', 'key_hash', 'stop_token'}
PRIVATE_PATTERN = re.compile(r'"(' + '|'.join(re.escape(k) for k in PRIVATE_KEYS) + r')"\s*:', re.I)


def sha(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def clean(value):
    if isinstance(value, dict):
        return {k: clean(v) for k, v in value.items() if k.lower() not in PRIVATE_KEYS}
    if isinstance(value, list):
        return [clean(v) for v in value]
    return value


def cell(value):
    if isinstance(value, bytes):
        raise ValueError('Unexpected binary business field; review before publication')
    if isinstance(value, str) and PRIVATE_PATTERN.search(value):
        value = json.dumps(clean(json.loads(value)), ensure_ascii=False, separators=(',', ':'))
    return value


def export(database, output, source_version, active_profile=None):
    output.mkdir(parents=True, exist_ok=False)
    with tempfile.TemporaryDirectory(prefix='stzb-share-', dir=output.parent) as temporary:
        snapshot = Path(temporary) / 'intelligence.sqlite3'
        stamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
        counts = {}
        with closing(sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True, timeout=30)) as source:
            source.execute('BEGIN')
            schema = source.execute("SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' ORDER BY type DESC,name").fetchall()
            profiles = [{'id': row[0], 'profile': clean(json.loads(row[1]))}
                        for row in source.execute('SELECT id,data FROM profiles')]
            with closing(sqlite3.connect(snapshot)) as target:
                target.execute('PRAGMA journal_mode=OFF')
                target.execute('PRAGMA synchronous=OFF')
                for kind, table, sql in schema:
                    if kind == 'table':
                        target.execute(sql)
                for kind, table, sql in schema:
                    if kind != 'table':
                        continue
                    if table in EMPTY_TABLES:
                        counts[table] = 0
                        continue
                    cursor = source.execute('SELECT * FROM "' + table.replace('"', '""') + '"')
                    insert = 'INSERT INTO "' + table.replace('"', '""') + '" VALUES(' + ','.join('?' for _ in cursor.description) + ')'
                    count = 0
                    while True:
                        rows = cursor.fetchmany(1000)
                        if not rows:
                            break
                        target.executemany(insert, (tuple(cell(v) for v in row) for row in rows))
                        count += len(rows)
                    counts[table] = count
                    print(json.dumps({'table': table, 'rows': count}), flush=True)
                for kind, name, sql in schema:
                    if kind in ('index', 'view', 'trigger'):
                        target.execute(sql)
                target.commit()
                assert target.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
                for table in EMPTY_TABLES:
                    if table in counts:
                        assert target.execute('SELECT COUNT(*) FROM "' + table + '"').fetchone()[0] == 0
            source.rollback()
        compressed = Path(temporary) / 'intelligence.sqlite3.xz'
        with snapshot.open('rb') as src, lzma.open(compressed, 'wb', preset=3) as dst:
            for chunk in iter(lambda: src.read(1024 * 1024), b''):
                dst.write(chunk)
        parts = []
        with compressed.open('rb') as stream:
            while True:
                payload = stream.read(4 * 1024 * 1024)
                if not payload:
                    break
                name = f'intelligence.sqlite3.xz.{len(parts) + 1:04d}.part'
                path = output / name
                path.write_bytes(payload)
                parts.append({'file': name, 'bytes': len(payload), 'sha256': sha(path)})
        manifest = {'format': 1, 'compression': 'xz', 'source_version': source_version, 'snapshot_at_utc': stamp,
                    'snapshot_bytes': snapshot.stat().st_size, 'snapshot_sha256': sha(snapshot),
                    'compressed_bytes': compressed.stat().st_size, 'compressed_sha256': sha(compressed),
                    'excluded_table_contents': sorted(EMPTY_TABLES), 'table_counts': counts,
                    'active_profile': active_profile, 'profiles': profiles, 'parts': parts}
        (output / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print(json.dumps({'snapshot_bytes': manifest['snapshot_bytes'], 'compressed_bytes': manifest['compressed_bytes'],
                          'parts': len(parts), 'battle_reports': counts.get('battle_reports'),
                          'report_bodies': counts.get('report_bodies')}, ensure_ascii=False), flush=True)
        return manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--source-version', default='0.2.4')
    parser.add_argument('--active-profile')
    args = parser.parse_args()
    export(args.database, args.output, args.source_version, args.active_profile)
