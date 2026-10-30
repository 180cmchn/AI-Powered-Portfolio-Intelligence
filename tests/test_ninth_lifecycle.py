"""Real handbook entrypoints in disposable repositories, ports and databases."""
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import subprocess
import sys
import time

import httpx
import pytest

from app.main import create_app
from app.runtime_lock import DatabaseLease
from app.store import Store
from scripts.backup import backup
from scripts import manage

ROOT = Path(__file__).resolve().parents[1]


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


@pytest.fixture
def repository(tmp_path):
    repo = tmp_path / 'copy with spaces'
    repo.mkdir()
    for directory in ('app', 'scripts'):
        shutil.copytree(ROOT / directory, repo / directory, ignore=shutil.ignore_patterns('__pycache__'))
    for name in ('start.command', 'stop.command', 'uv.lock', 'pyproject.toml'):
        shutil.copy2(ROOT / name, repo / name)
    (repo / '.venv').symlink_to(Path(sys.prefix), target_is_directory=True)
    (repo / 'data').mkdir()
    shutil.copy2(ROOT / 'data/calendar_2026.json', repo / 'data/calendar_2026.json')
    port = free_port()
    env = {k: os.environ[k] for k in ('PATH', 'HOME', 'TMPDIR', 'LANG') if k in os.environ}
    env.update(APP_PORT=str(port), APP_MODE='live', APP_DB_PATH=str(repo / 'data/test.sqlite3'),
               PYTHON_DOTENV_DISABLED='1', MODEL_KEY_ENV='UNSET_NINTH_KEY', MODEL_NAME='',
               QUOTE_PROVIDER='manual', QUOTE_FALLBACK_PROVIDER='none')
    yield repo, env, port
    # Cleanup only the process registered by this isolated repository.
    subprocess.run([sys.executable, str(repo / 'scripts/manage.py'), 'stop'], env=env, cwd=repo, capture_output=True, timeout=15)


def control(repository, action, *args, handbook=False):
    repo, env, _ = repository
    if handbook and shutil.which('zsh'):
        cmd = [shutil.which('zsh'), str(repo / ('start.command' if action == 'start' else 'stop.command')), '--mode', 'live', *args]
    else:
        cmd = [sys.executable, str(repo / 'scripts/manage.py'), action, '--mode', 'live', *args]
    return subprocess.run(cmd, cwd=repo, env=env, text=True, capture_output=True, timeout=35)


def test_handbook_cold_start_idempotence_static_freeze_and_restart(repository):
    repo, env, port = repository
    started = control(repository, 'start', handbook=True)
    assert started.returncode == 0, started.stdout + started.stderr
    with httpx.Client(base_url=f'http://127.0.0.1:{port}', trust_env=False) as client:
        health = client.get('/api/health').json()
        assert health['ready'] and health['worker_running'] and not health['missing_routes']
        version = health['runtime_version']
        assert version['port'] == port and version['database_path'] == env['APP_DB_PATH']
        for path in ('/api/providers', '/api/agent-roles', '/api/accounts', '/api/research', '/api/forecasts', '/api/environment-connection'):
            assert client.get(path).status_code == 200, path
        assert not client.get('/api/status').json()['model_configured']
        assert control(repository, 'start').returncode == 0
        changed_env = {**env, 'MODEL_PROTOCOL': 'chat_completions'}
        changed = control((repo, changed_env, port), 'start')
        assert changed.returncode != 0 and 'environment_settings_changed' in changed.stderr
        assert json.loads((repo / 'data/live.pid').read_text())['pid'] == version['pid']
        old_static = client.get('/static/app.js').content
        with (repo / 'app/static/app.js').open('a') as file:
            file.write('\n// updated disk requires restart\n')
        assert client.get('/static/app.js').content == old_static
        assert client.get('/api/status').json()['runtime_version'] == version
        stale = control(repository, 'start')
        assert stale.returncode != 0 and '安全重启' in stale.stderr
        assert control(repository, 'start', '--port', str(free_port())).returncode != 0
        assert client.get('/api/health').json()['runtime_version']['pid'] == version['pid']
    stopped = control(repository, 'stop', handbook=True)
    assert stopped.returncode == 0, stopped.stdout + stopped.stderr
    assert not (repo / 'data/live.pid').exists()
    restarted = control(repository, 'start')
    assert restarted.returncode == 0, restarted.stderr
    with httpx.Client(base_url=f'http://127.0.0.1:{port}', trust_env=False) as client:
        assert client.get('/static/app.js').content != old_static
        assert client.get('/api/health').json()['runtime_version']['instance_id'] != version['instance_id']


def test_foreign_port_never_migrates_target(repository):
    repo, env, port = repository
    with socket.socket() as foreign:
        foreign.bind(('127.0.0.1', port))
        foreign.listen()
        result = control(repository, 'start')
        assert result.returncode != 0 and '端口' in result.stderr
        assert not Path(env['APP_DB_PATH']).exists()
        result = subprocess.run([sys.executable, str(repo / 'scripts/serve.py')], env=env, cwd=repo, text=True, capture_output=True, timeout=10)
        assert result.returncode != 0 and '尚未打开或迁移' in result.stderr
        assert not Path(env['APP_DB_PATH']).exists()


def test_stale_pid_recovers_but_pid_reuse_is_not_killed(repository):
    repo, env, _ = repository
    pidfile = repo / 'data/live.pid'
    pidfile.write_text('99999999')
    assert control(repository, 'start').returncode == 0
    assert control(repository, 'stop').returncode == 0
    pidfile.write_text(json.dumps({'pid': os.getpid(), 'started': 'different process identity'}))
    for action in ('start', 'stop'):
        result = control(repository, action)
        assert result.returncode != 0 and 'PID' in result.stderr
    assert pidfile.exists()


def test_database_lock_precedes_store_migration(repository):
    repo, env, _ = repository
    path = Path(env['APP_DB_PATH'])
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE meta (key TEXT PRIMARY KEY,value TEXT)')
        db.execute("INSERT INTO meta VALUES('schema_version','3')")
        db.execute("INSERT INTO meta VALUES('mode','\"live\"')")
    with DatabaseLease(path):
        result = control(repository, 'start')
        assert result.returncode != 0
        with sqlite3.connect(path) as db:
            assert db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == '3'
        assert not path.with_name(path.name + '.before-v9.bak').exists()
        assert not (repo / 'data/live.pid').exists()


def test_old_schema_upgrade_and_consistent_backup(repository):
    repo, env, _ = repository
    path = Path(env['APP_DB_PATH'])
    store = Store(path)
    store.set_meta('schema_version', 3)
    store.set_meta('ninth_preservation_marker', 'preserve')
    result = control(repository, 'start')
    assert result.returncode == 0, result.stderr
    saved = backup(path, repo / 'snapshot.sqlite3', 'live')
    with sqlite3.connect(saved) as db:
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert db.execute("SELECT value FROM meta WHERE key='ninth_preservation_marker'").fetchone()[0] == '"preserve"'
        assert db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == '9'
    with sqlite3.connect(path.with_name(path.name + '.before-v9.bak')) as db:
        assert db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == '3'


def test_stop_timeout_does_not_claim_success(monkeypatch):
    monkeypatch.setattr(manage, 'owned', lambda record: True)
    signals = []
    monkeypatch.setattr(manage.os, 'kill', lambda pid, sig: signals.append((pid, sig)))
    assert not manage.stop_owned({'pid': 12345}, timeout=0)
    assert len(signals) == 1


def test_factory_lock_before_store_and_release_on_failed_initialization(tmp_path):
    path = tmp_path / 'future.sqlite3'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT)')
        db.execute("INSERT INTO meta VALUES('schema_version','100')")
    with pytest.raises(ValueError, match='数据库版本比程序更新'):
        create_app(path, worker=True)
    with DatabaseLease(path):
        with pytest.raises(RuntimeError, match='未打开或迁移'):
            create_app(path, worker=True)


def test_handbook_environment_connection_without_model(repository):
    import uuid
    repo, env, port = repository
    env.pop('PYTHON_DOTENV_DISABLED')
    env.pop('MODEL_NAME')
    env.pop('MODEL_KEY_ENV')
    secret = uuid.uuid4().hex + uuid.uuid4().hex
    (repo / '.env').write_text('MODEL_BASE_URL=https://example.invalid/v1\nMODEL_PROTOCOL=responses\nMODEL_API_KEY=' + secret + '\n')
    result = control(repository, 'start', handbook=True)
    assert result.returncode == 0, 'Isolated handbook environment startup failed'
    with httpx.Client(base_url=f'http://127.0.0.1:{port}', trust_env=False) as client:
        providers = client.get('/api/providers').json()
        assert len(providers) == 1 and providers[0]['key_state'] == 'available'
        status = client.get('/api/status').json()
        assert not status['model_configured']
        assert status['configuration_readiness']['provider_saved']
        assert not status['configuration_readiness']['manager_ready']
        preview = client.get('/api/environment-connection').json()
        assert preview['source']['fields']['MODEL_API_KEY']['source'] == 'dotenv'
        for route in ('/api/providers', '/api/status', '/api/environment-connection', '/api/agent-roles'):
            assert secret not in client.get(route).text
        with (repo / '.env').open('a') as file:
            file.write('\n# changed configuration must not silently reuse worker\n')
        changed = control(repository, 'start')
        assert changed.returncode != 0 and 'environment_file_changed' in changed.stderr
    assert secret not in (repo / 'data/live.log').read_text()
    assert secret.encode() not in Path(env['APP_DB_PATH']).read_bytes()


def test_legacy_pidfile_stop_requires_mode_and_database_evidence(repository):
    repo, env, port = repository
    assert control(repository, 'start').returncode == 0
    pidfile = repo / 'data/live.pid'
    record = json.loads(pidfile.read_text())
    (repo / 'data/demo.pid').write_text(str(record['pid']))
    wrong = subprocess.run([sys.executable, str(repo / 'scripts/manage.py'), 'stop', '--mode', 'demo', '--port', str(port)],
                           env=env, cwd=repo, capture_output=True, text=True, timeout=15)
    assert wrong.returncode != 0 and '模式不匹配' in wrong.stderr
    assert (repo / 'data/demo.pid').exists()
    pidfile.write_text(str(record['pid']))
    assert control(repository, 'stop').returncode == 0


def test_process_identity_requires_entry_front_boundary(monkeypatch):
    entry = str(manage.ROOT / 'scripts/serve.py')
    def command(args, **kwargs):
        return {'command=': '/usr/bin/python /different-prefix' + entry,
                'lstart=': 'Thu Oct 29 01:00:00 2026', 'stat=': 'S'}[args[-1]]
    monkeypatch.setattr(manage.subprocess, 'check_output', command)
    assert not manage.process_identity(123)['owned_entry']


def test_stop_timeout_preserves_pidfile(monkeypatch, tmp_path):
    data = tmp_path / 'runtime'
    data.mkdir()
    pidfile = data / 'live.pid'
    record = {'pid': 12345, 'started': 'verified-start', 'mode': 'live', 'port': 12345, 'database_path': str(tmp_path / 'db.sqlite3')}
    pidfile.write_text(json.dumps(record))
    monkeypatch.setenv('APP_RUNTIME_DIR', str(data))
    monkeypatch.setattr(manage, 'owned', lambda item: True)
    monkeypatch.setattr(manage, 'verify_stop_target', lambda *args: None)
    monkeypatch.setattr(manage, 'stop_owned', lambda *args: False)
    monkeypatch.setattr(sys, 'argv', ['manage.py', 'stop', '--mode', 'live', '--port', '12345'])
    with pytest.raises(SystemExit, match='保留 pidfile'):
        manage.main()
    assert json.loads(pidfile.read_text()) == record
