#!/usr/bin/env python3
"""Online read-only backup of an explicit source, upgrade disposable copy twice."""
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.domain import now
from app.store import Store

TABLES = ('accounts', 'snapshots', 'jobs', 'analyses', 'materials', 'forecasts',
          'forecast_evaluations', 'model_providers', 'provider_connections', 'agent_definitions',
          'account_agent_overrides', 'research_records')


def audit(path):
    with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True) as db:
        tables = {}
        for table in TABLES:
            rows = sorted(db.execute('SELECT * FROM ' + table).fetchall(), key=repr)
            tables[table] = {'count': len(rows), 'sha256': hashlib.sha256(
                json.dumps(rows, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()}
        return {'schema': json.loads(db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]),
                'integrity': db.execute('PRAGMA integrity_check').fetchone()[0], 'tables': tables}


def run(source, output):
    result = {'at': now(), 'source_access': 'SQLite mode=ro online backup only',
        'daily_service_stopped': False, 'model_requests': 0}
    with tempfile.TemporaryDirectory(prefix='guanlan-schema11-') as folder:
        target = Path(folder) / 'upgrade.sqlite3'
        with sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True) as origin, sqlite3.connect(target) as copy:
            origin.backup(copy)
        target.chmod(0o600)
        before = audit(target)
        result['source_snapshot'] = before
        first = Store(target, 'live')
        result['first'] = audit(target)
        backup = target.with_name(target.name + '.before-v11.bak')
        result['backup_exists'] = backup.exists()
        backup_digest = hashlib.sha256(backup.read_bytes()).hexdigest()
        result['backup_matches_preupgrade_rows'] = audit(backup) == before
        baseline = {}
        for a in first.accounts():
            book = first.ledger(a['id'])
            baseline[a['id']] = {'entries': len(book['entries']),
                'cash_matches': book['cash'] == a['cash'],
                'source': book['entries'][-1]['source'],
                'all_old_availability_unknown': all(p['available_quantity'] == '0' for p in book['positions'])}
        result['baselines'] = baseline
        second = Store(target, 'live')
        result['second'] = audit(target)
        result['backup_unchanged_after_repeat'] = hashlib.sha256(backup.read_bytes()).hexdigest() == backup_digest
        result['repeat_does_not_duplicate_baseline'] = all(len(second.ledger(a['id'])['entries']) == baseline[a['id']]['entries'] for a in second.accounts())
        result['preserved_all_business_rows'] = result['first']['tables'] == before['tables'] == result['second']['tables']
        result['success'] = (before['schema'] == 10 and result['first']['schema'] == result['second']['schema'] == 11
            and result['first']['integrity'] == result['second']['integrity'] == 'ok'
            and result['preserved_all_business_rows'] and result['backup_matches_preupgrade_rows']
            and result['backup_unchanged_after_repeat'] and result['repeat_does_not_duplicate_baseline'])
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'success': result['success'], 'preserved_all_business_rows': result['preserved_all_business_rows'],
        'source_schema': before['schema'], 'target_schema': result['second']['schema']}, ensure_ascii=False))
    return 0 if result['success'] else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    raise SystemExit(run(args.source, args.output))
