#!/usr/bin/env python3
"""Verify an explicitly selected existing database through an isolated upgrade copy."""
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import urllib.request
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]


def counts(path):
    result = {}
    with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True) as db:
        names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in ('accounts', 'snapshots', 'materials', 'analyses', 'forecasts', 'model_providers'):
            if table in names:
                result[table] = db.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]
        result['schema'] = json.loads(db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0])
    return result


def run(source, output):
    source = source.resolve()
    result = {'kind': 'real_existing_database_isolated_copy', 'observed_at': datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),
              'source_access': 'SQLite mode=ro backup only', 'daily_service_changed': False, 'paid_requests': 0}
    before = counts(source)
    result['source_before'] = before
    with tempfile.TemporaryDirectory(prefix='guanlan-upgrade-copy-') as folder:
        temporary = Path(folder)
        target = temporary / 'copy.sqlite3'
        with sqlite3.connect(source.as_uri() + '?mode=ro', uri=True) as original, sqlite3.connect(target) as saved:
            original.backup(saved)
        target.chmod(0o600)
        # Configuration change is limited to the disposable copy: no inherited
        # key, scheduled research, real quotes or migrated user role execution.
        with sqlite3.connect(target) as db:
            row = db.execute("SELECT value FROM meta WHERE key='settings'").fetchone()
            settings = json.loads(row[0]) if row else {}
            settings.update(model_name='', model_key_env='NINTH_DISABLED_TEST_KEY', model_base_url='https://example.invalid/v1',
                            scheduled_research_enabled=False, market_refresh_enabled=False,
                            forecast_evaluation_enabled=False, quote_provider='manual', quote_fallback_provider='none')
            db.execute("INSERT OR REPLACE INTO meta VALUES('settings',?)", (json.dumps(settings),))
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        env = {k: os.environ[k] for k in ('HOME', 'PATH', 'LANG', 'TMPDIR') if k in os.environ}
        env.update(APP_DB_PATH=str(target), APP_RUNTIME_DIR=str(temporary), APP_PORT=str(port),
                   APP_MODE='live', PYTHON_DOTENV_DISABLED='1', MODEL_NAME='', MODEL_KEY_ENV='NINTH_DISABLED_TEST_KEY')
        cmd = [sys.executable, str(ROOT / 'scripts/manage.py')]
        try:
            started = subprocess.run(cmd + ['start', '--mode', 'live'], env=env, cwd=ROOT, capture_output=True, timeout=35)
            result['start_exit'] = started.returncode
            if started.returncode:
                result['error'] = 'isolated_upgrade_start_failed'
            else:
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                result['routes'] = {}
                for route in ('/api/health', '/api/providers', '/api/agent-roles', '/api/accounts', '/api/forecasts', '/api/research'):
                    with opener.open(f'http://127.0.0.1:{port}' + route, timeout=5) as response:
                        result['routes'][route] = response.status
                result['copy_after'] = counts(target)
                result['business_counts_preserved'] = all(result['copy_after'].get(k) == v for k, v in before.items() if k != 'schema')
                result['before_v9_backup'] = target.with_name(target.name + '.before-v9.bak').exists()
                with sqlite3.connect(target) as db:
                    result['integrity'] = db.execute('PRAGMA integrity_check').fetchone()[0]
        finally:
            stopped = subprocess.run(cmd + ['stop', '--mode', 'live'], env=env, cwd=ROOT, capture_output=True, timeout=20)
            result['stop_exit'] = stopped.returncode
    result['source_after'] = counts(source)
    result['source_unchanged'] = result['source_after'] == before
    result['success'] = bool(result.get('start_exit') == 0 and result.get('stop_exit') == 0 and result.get('business_counts_preserved') and result.get('integrity') == 'ok' and result['source_unchanged'])
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['success'] else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    raise SystemExit(run(args.source, args.output))
