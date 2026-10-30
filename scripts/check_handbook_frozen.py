#!/usr/bin/env python3
"""Install frozen dependencies and exercise the documented commands in a temp copy."""
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import tempfile
import urllib.request
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]


def run(output=None):
    result = {'started_at': datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),
              'kind': 'real_handbook_frozen_isolated', 'daily_service_changed': False,
              'daily_database_changed': False, 'paid_requests': 0}
    with tempfile.TemporaryDirectory(prefix='guanlan-frozen-') as folder:
        repo = Path(folder) / 'project with spaces'
        repo.mkdir()
        for name in ('app', 'scripts'):
            shutil.copytree(ROOT / name, repo / name, ignore=shutil.ignore_patterns('__pycache__'))
        for name in ('uv.lock', 'pyproject.toml', 'start.command', 'stop.command'):
            shutil.copy2(ROOT / name, repo / name)
        (repo / 'data').mkdir()
        shutil.copy2(ROOT / 'data/calendar_2026.json', repo / 'data/calendar_2026.json')
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        secret = secrets.token_urlsafe(32)
        (repo / '.env').write_text('MODEL_BASE_URL=https://example.invalid/v1\nMODEL_PROTOCOL=responses\nMODEL_API_KEY=' + secret + '\n')
        (repo / '.env').chmod(0o600)
        env = {k: os.environ[k] for k in ('PATH', 'HOME', 'LANG', 'TMPDIR') if k in os.environ}
        env.update(APP_PORT=str(port), APP_DB_PATH=str(repo / 'data/live.sqlite3'), QUOTE_PROVIDER='manual', QUOTE_FALLBACK_PROVIDER='none')
        command = [shutil.which('uv') or 'uv', 'sync', '--frozen']
        sync = subprocess.run(command, cwd=repo, env=env, capture_output=True, text=True, timeout=180)
        result['frozen_sync_exit'] = sync.returncode
        if sync.returncode:
            result['error'] = 'frozen_dependency_sync_failed'
        else:
            python = str(repo / '.venv/bin/python')
            result['python'] = subprocess.check_output([python, '--version'], cwd=repo, env=env, text=True).strip()
            zsh = shutil.which('zsh')
            start = [zsh, './start.command', '--mode', 'live'] if zsh else [python, 'scripts/manage.py', 'start', '--mode', 'live']
            stop = [zsh, './stop.command', '--mode', 'live'] if zsh else [python, 'scripts/manage.py', 'stop', '--mode', 'live']
            result['shell_entry'] = 'actual_zsh_start.command' if zsh else 'equivalent_python_management_entry'
            try:
                started = subprocess.run(start, cwd=repo, env=env, capture_output=True, text=True, timeout=35)
                result['start_exit'] = started.returncode
                if started.returncode:
                    result['error'] = 'handbook_start_failed'
                else:
                    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                    def get(path):
                        with opener.open(f'http://127.0.0.1:{port}' + path, timeout=5) as response:
                            raw = response.read().decode()
                            if secret in raw:
                                raise RuntimeError('write_only_secret_exposed')
                            return json.loads(raw)
                    health, status, providers = get('/api/health'), get('/api/status'), get('/api/providers')
                    result.update(api_ready=health['ready'], api_contract=health['runtime_version']['api_contract'],
                        schema=health['runtime_version']['schema_version'], routes_missing=health['missing_routes'],
                        providers_count=len(providers), model_configured=status['model_configured'],
                        provider_saved=status['configuration_readiness']['provider_saved'],
                        next_step=status['configuration_readiness']['next_step'])
                    again = subprocess.run(start, cwd=repo, env=env, capture_output=True, text=True, timeout=35)
                    result['repeated_start_exit'] = again.returncode
                    result['same_instance'] = get('/api/health')['runtime_version']['instance_id'] == health['runtime_version']['instance_id']
                    result['secrets_absent_from_log_and_db'] = all(secret.encode() not in path.read_bytes() for path in (repo / 'data').iterdir() if path.is_file())
                    result['success'] = bool(health['ready'] and result['provider_saved'] and not result['model_configured'] and result['same_instance'] and result['secrets_absent_from_log_and_db'])
            finally:
                stopped = subprocess.run(stop, cwd=repo, env=env, capture_output=True, text=True, timeout=20)
                result['stop_exit'] = stopped.returncode
    result['finished_at'] = datetime.now(ZoneInfo('Asia/Shanghai')).isoformat()
    if output:
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get('success') and result.get('stop_exit') == 0 else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    raise SystemExit(run(parser.parse_args().output))
