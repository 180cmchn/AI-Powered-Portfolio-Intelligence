#!/usr/bin/env python3
"""Read-only version inspection; does not import main, create Store or migrate."""
import argparse,json,sqlite3,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import httpx
from app.config import db_path
from app.runtime_version import source_digest

p=argparse.ArgumentParser(description=__doc__)
p.add_argument('--url',default='http://127.0.0.1:8765')
p.add_argument('--database',type=Path)
a=p.parse_args()
if not a.url.startswith(('http://127.0.0.1:','http://localhost:','http://[::1]:')):
    raise SystemExit('Only explicit local loopback service inspection is supported')
path=a.database or db_path('live')
result={'repository_source_sha256':source_digest(),'running_process_version':'unknown_until_reported_by_service','database_access':'read_only'}
if path.exists():
    with sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True) as db:
        row=db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        result['database_schema_version']=json.loads(row[0]) if row else None
try:
    r=httpx.get(a.url.rstrip('/')+'/api/status',timeout=5,trust_env=False);r.raise_for_status();status=r.json()
    result.update(service_http_status=r.status_code,worker=status.get('worker'),service_time=status.get('time'),
                  running_process_version=status.get('runtime_version') or 'legacy_service_does_not_report_loaded_version')
except httpx.HTTPError as exc:
    result['service_error_type']=type(exc).__name__
print(json.dumps(result,ensure_ascii=False,indent=2))
