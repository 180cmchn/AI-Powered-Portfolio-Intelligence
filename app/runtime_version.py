"""Read-only process-start provenance; never assigns current HEAD to an old process."""
from datetime import datetime
import hashlib
from pathlib import Path
import subprocess
from .calendar import SHANGHAI


def source_digest():
    root = Path(__file__).resolve().parents[1]
    entries = []
    for path in sorted((root / 'app').rglob('*')):
        if path.is_file() and '__pycache__' not in path.parts and path.suffix in ('.py', '.js', '.html', '.css', '.txt'):
            entries.append((str(path.relative_to(root)), hashlib.sha256(path.read_bytes()).hexdigest()))
    return hashlib.sha256(repr(entries).encode()).hexdigest()


def startup_version(store):
    root = Path(__file__).resolve().parents[1]
    def git(*args):
        try:
            return subprocess.check_output(['git', *args], cwd=root, text=True, stderr=subprocess.DEVNULL, timeout=2).strip()
        except (OSError, subprocess.SubprocessError):
            return None
    return {'started_at': datetime.now(SHANGHAI).isoformat(), 'commit_at_start': git('rev-parse', 'HEAD'),
            'working_tree_dirty_at_start': bool(git('status', '--porcelain', '--untracked-files=no')),
            'source_sha256_at_start': source_digest(), 'schema_version': store.meta('schema_version'),
            'provenance': 'source files observed at application creation; restart required after source edits',
            'database_mode': store.mode}
