"""Read-only process-start provenance; never assigns current HEAD to an old process."""
from datetime import datetime
import hashlib
import os
from pathlib import Path
import subprocess
import uuid
from urllib.parse import urlsplit
from .calendar import SHANGHAI

API_CONTRACT = 9
REQUIRED_ROUTES = {
    '/api/status': 'get', '/api/health': 'get', '/api/providers': 'get',
    '/api/agent-roles': 'get', '/api/accounts': 'get', '/api/research': 'get',
    '/api/forecasts': 'get', '/api/forecast-batches': 'get',
    '/api/environment-connection': 'get',
}


def env_file_stamp(root=None):
    path = (root or Path(__file__).resolve().parents[1]) / '.env'
    try:
        stat = path.stat()
        return {'exists': True, 'mtime_ns': stat.st_mtime_ns, 'size': stat.st_size}
    except FileNotFoundError:
        return {'exists': False}


def environment_launch_settings():
    """Non-secret launch settings; never hash or publish credential contents."""
    from .config import defaults
    cfg = defaults()
    base = cfg['model_base_url']
    try:
        parts = urlsplit(base)
        if parts.username or parts.password or parts.query or parts.fragment or not parts.hostname:
            base = 'invalid_endpoint'
    except ValueError:
        base = 'invalid_endpoint'
    return {'model_base_url': base, 'model_protocol': cfg['model_protocol'],
            'legacy_model_name': cfg['model_name'], 'reasoning_effort': cfg['reasoning_effort'],
            'key_env_name': cfg['model_key_env'], 'key_present': bool(os.getenv(cfg['model_key_env'])),
            'quote_provider': cfg['quote_provider'], 'quote_fallback_provider': cfg['quote_fallback_provider'],
            'model_native_search': cfg['model_native_search'], 'research_provider': cfg['research_provider']}


def static_snapshot():
    root = Path(__file__).resolve().parent / 'static'
    return {str(path.relative_to(root)): path.read_bytes() for path in sorted(root.rglob('*')) if path.is_file()}


def static_digest(snapshot):
    return hashlib.sha256(repr([(name, hashlib.sha256(body).hexdigest()) for name, body in sorted(snapshot.items())]).encode()).hexdigest()


def source_digest():
    root = Path(__file__).resolve().parents[1]
    entries = []
    for path in sorted([*(root / 'app').rglob('*'), *(root / 'scripts').glob('*.py'), root / 'start.command', root / 'stop.command', root / 'uv.lock']):
        if path.is_file() and '__pycache__' not in path.parts and path.suffix in ('.py', '.js', '.html', '.css', '.txt', '.command', '.lock'):
            entries.append((str(path.relative_to(root)), hashlib.sha256(path.read_bytes()).hexdigest()))
    return hashlib.sha256(repr(entries).encode()).hexdigest()


def startup_version(store, static=None):
    root = Path(__file__).resolve().parents[1]
    def git(*args):
        try:
            return subprocess.check_output(['git', *args], cwd=root, text=True, stderr=subprocess.DEVNULL, timeout=2).strip()
        except (OSError, subprocess.SubprocessError):
            return None
    return {'started_at': datetime.now(SHANGHAI).isoformat(), 'commit_at_start': git('rev-parse', 'HEAD'),
            'api_contract': API_CONTRACT, 'instance_id': os.getenv('APP_INSTANCE_ID') or uuid.uuid4().hex,
            'pid': os.getpid(), 'port': int(os.getenv('APP_PORT', '8765')),
            'project_path': str(root), 'database_path': str(store.path.resolve()),
            'database_identity': hashlib.sha256(str(store.path.resolve()).encode()).hexdigest(),
            'env_file_at_start': env_file_stamp(root),
            'environment_settings_at_start': environment_launch_settings(),
            'static_sha256_at_start': static_digest(static if static is not None else static_snapshot()),
            'working_tree_dirty_at_start': bool(git('status', '--porcelain', '--untracked-files=no')),
            'source_sha256_at_start': source_digest(), 'schema_version': store.meta('schema_version'),
            'provenance': 'source files observed at application creation; restart required after source edits',
            'database_mode': store.mode}
