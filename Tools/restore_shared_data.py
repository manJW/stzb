"""Verify and restore the shared SQLite snapshot, without replacing an existing DB."""
import argparse
import gzip
import hashlib
import json
import lzma
import shutil
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path


def sha(path):
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def restore(manifest_path, database):
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    if manifest.get('format') != 1:
        raise ValueError('Unsupported snapshot format')
    database = database.resolve()
    if database.exists() or any(Path(str(database) + suffix).exists() for suffix in ('-wal', '-shm')):
        raise FileExistsError('Choose an empty destination; existing database/WAL files will not be replaced')
    database.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='stzb-restore-', dir=database.parent) as temporary:
        compression = manifest.get('compression', 'gzip')
        if compression not in ('gzip', 'xz'):
            raise ValueError('Unsupported compression format')
        archive = Path(temporary) / ('snapshot.xz' if compression == 'xz' else 'snapshot.gz')
        with archive.open('wb') as target:
            for part in manifest['parts']:
                name = part['file']
                if Path(name).name != name or '/' in name or '\\' in name:
                    raise ValueError('Invalid snapshot part path')
                source = manifest_path.parent / name
                if source.stat().st_size != part['bytes'] or sha(source) != part['sha256']:
                    raise ValueError('Snapshot part verification failed: ' + name)
                with source.open('rb') as stream:
                    shutil.copyfileobj(stream, target)
        if archive.stat().st_size != manifest['compressed_bytes'] or sha(archive) != manifest['compressed_sha256']:
            raise ValueError('Compressed snapshot verification failed')
        restored = Path(temporary) / 'snapshot.sqlite3'
        opener = lzma.open if compression == 'xz' else gzip.open
        with opener(archive, 'rb') as source, restored.open('wb') as target:
            shutil.copyfileobj(source, target)
        if restored.stat().st_size != manifest['snapshot_bytes'] or sha(restored) != manifest['snapshot_sha256']:
            raise ValueError('Restored database verification failed')
        with closing(sqlite3.connect(restored.as_uri() + '?mode=ro', uri=True)) as db:
            if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise ValueError('SQLite integrity verification failed')
            for table, count in manifest['table_counts'].items():
                escaped = table.replace('"', '""')
                if db.execute('SELECT COUNT(*) FROM "' + escaped + '"').fetchone()[0] != count:
                    raise ValueError('Row count mismatch: ' + table)
        # Exclusive creation refuses replacement on Windows and other platforms.
        with database.open('xb') as target, restored.open('rb') as source:
            shutil.copyfileobj(source, target)
    print(json.dumps({'database': str(database), 'source_version': manifest['source_version'],
                      'battle_reports': manifest['table_counts']['battle_reports'],
                      'report_bodies': manifest['table_counts']['report_bodies'], 'verified': True}, ensure_ascii=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, default=Path(__file__).resolve().parents[1] / 'SharedData/2026-11-15/manifest.json')
    parser.add_argument('--database', type=Path, required=True)
    args = parser.parse_args()
    restore(args.manifest.resolve(), args.database)
