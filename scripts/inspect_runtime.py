#!/usr/bin/env python3
"""Read-only local instance/API/config diagnosis. Never imports Store or migrates."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import httpx
from app.config import db_path, defaults
from app.runtime_version import REQUIRED_ROUTES, API_CONTRACT, env_file_stamp, source_digest, static_snapshot, static_digest
from scripts.manage import read_record, process_identity


def inspect(url, database=None):
    parsed = urlsplit(url)
    if parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', 'localhost', '::1') or not parsed.port or parsed.username or parsed.password or parsed.path not in ('', '/') or parsed.query or parsed.fragment:
        raise ValueError('Only explicit local loopback service inspection is supported')
    config = defaults()
    configured = database or db_path('live')
    path = Path(configured).resolve()
    result = {'project_path': str(ROOT), 'python': sys.executable, 'python_version': sys.version.split()[0],
              'repository_source_sha256': source_digest(), 'api_contract': API_CONTRACT,
              'env_file': env_file_stamp(), 'database_path': str(path), 'database_access': 'read_only',
              'runtime_version': 'unknown_until_reported_by_service', 'requests': []}
    try:
        result['disk_commit'] = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True, stderr=subprocess.DEVNULL).strip()
        result['disk_dirty'] = bool(subprocess.check_output(['git', 'status', '--porcelain', '--untracked-files=no'], cwd=ROOT, text=True, stderr=subprocess.DEVNULL).strip())
    except (OSError, subprocess.SubprocessError):
        result['disk_commit'] = 'unknown'
    result['lockfile_sha256'] = hashlib.sha256((ROOT / 'uv.lock').read_bytes()).hexdigest()
    if path.exists():
        try:
            with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as db:
                for name in ('schema_version', 'mode'):
                    row = db.execute('SELECT value FROM meta WHERE key=?', (name,)).fetchone()
                    result['database_' + name] = json.loads(row[0]) if row else None
                row = db.execute("SELECT value FROM meta WHERE key='settings'").fetchone()
                if row:
                    config.update(json.loads(row[0]))
                result['database_has_saved_settings'] = bool(row)
                result['agent_config_migrated'] = bool(db.execute("SELECT 1 FROM meta WHERE key='agent_config_migration'").fetchone())
        except sqlite3.Error as exc:
            result['database_error_type'] = type(exc).__name__
    result['legacy_configuration'] = {'source': 'saved_settings_over_environment_defaults',
        'endpoint_host': urlsplit(str(config.get('model_base_url', ''))).hostname,
        'protocol': config.get('model_protocol'), 'legacy_model_present': bool(config.get('model_name')),
        'key_present_in_current_diagnostic_environment': bool(os.getenv(config.get('model_key_env') or 'MODEL_API_KEY')),
        'note': 'These are not proof of the running process environment or a selected Agent model.'}
    data = Path(os.getenv('APP_RUNTIME_DIR', str(ROOT / 'data')))
    result['pidfiles'] = {}
    for mode in ('live', 'demo'):
        record = read_record(data / f'{mode}.pid')
        if record:
            result['pidfiles'][mode] = {k: record[k] for k in ('pid', 'started', 'mode', 'port', 'legacy', 'database_path') if k in record}
            result['pidfiles'][mode]['process'] = process_identity(record.get('pid', 0))
    try:
        pids = subprocess.check_output(['lsof', '-nP', f'-iTCP:{parsed.port}', '-sTCP:LISTEN', '-t'], text=True, stderr=subprocess.DEVNULL).split()
        result['listeners'] = []
        for raw in dict.fromkeys(pids):
            pid = int(raw)
            item = process_identity(pid) or {'pid': pid, 'identity': 'unavailable'}
            try:
                cwd = subprocess.check_output(['lsof', '-a', '-p', raw, '-d', 'cwd', '-Fn'], text=True, stderr=subprocess.DEVNULL)
                item['cwd'] = next((line[1:] for line in cwd.splitlines() if line.startswith('n')), None)
            except (OSError, subprocess.SubprocessError):
                item['cwd'] = 'unavailable'
            result['listeners'].append(item)
    except subprocess.CalledProcessError as exc:
        result['listeners'] = [] if exc.returncode == 1 else 'unavailable'
    except OSError:
        result['listeners'] = 'unavailable'
    with httpx.Client(base_url=url.rstrip('/'), timeout=2, trust_env=False) as client:
        for route in dict.fromkeys(['/api/health', '/api/status', '/openapi.json', *REQUIRED_ROUTES]):
            record = {'method': 'GET', 'path': route}
            try:
                response = client.get(route)
                record.update(status=response.status_code, content_type=response.headers.get('content-type', ''), instance=response.headers.get('x-guanlan-instance'))
                if response.status_code == 404:
                    record['classification'] = 'local_route_missing_or_incompatible_service'
                elif response.status_code >= 500:
                    record['classification'] = 'local_api_failure'
                elif response.status_code == 200:
                    try:
                        body = response.json()
                    except ValueError:
                        record['classification'] = 'invalid_json'
                        body = None
                    if route == '/api/status' and isinstance(body, dict):
                        result['runtime_version'] = body.get('runtime_version') or 'legacy_service_does_not_report_loaded_version'
                        result['service_mode'] = body.get('mode')
                        result['configuration_readiness'] = body.get('configuration_readiness', 'legacy_unknown')
                    if route == '/openapi.json' and isinstance(body, dict):
                        result['missing_routes'] = [f'{method.upper()} {p}' for p, method in REQUIRED_ROUTES.items() if method not in body.get('paths', {}).get(p, {})]
                    if route == '/api/environment-connection' and isinstance(body, dict):
                        result['environment_connection'] = body
            except httpx.HTTPError as exc:
                record['classification'] = 'timeout' if isinstance(exc, httpx.TimeoutException) else 'network_unreachable'
                record['error_type'] = type(exc).__name__
            result['requests'].append(record)
            if record.get('classification') == 'network_unreachable':
                break
        version = result['runtime_version']
        if isinstance(version, dict):
            result['source_matches_disk'] = version.get('source_sha256_at_start') == result['repository_source_sha256']
            result['static_matches_disk'] = version.get('static_sha256_at_start') == static_digest(static_snapshot())
            result['database_matches_requested'] = version.get('database_path') == str(path)
            result['api_compatible'] = version.get('api_contract') == API_CONTRACT
        for asset in ('index.html', 'app.js', 'sixth.js'):
            try:
                response = client.get('/' if asset == 'index.html' else '/static/' + asset)
                result.setdefault('static_responses', {})[asset] = {'status': response.status_code,
                    'sha256': hashlib.sha256(response.content).hexdigest(), 'content_type': response.headers.get('content-type'),
                    'matches_disk': response.status_code == 200 and response.content == (ROOT / 'app/static' / asset).read_bytes()}
            except httpx.HTTPError:
                break
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--url', default='http://127.0.0.1:8765')
    p.add_argument('--database', type=Path)
    args = p.parse_args()
    print(json.dumps(inspect(args.url, args.database), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
