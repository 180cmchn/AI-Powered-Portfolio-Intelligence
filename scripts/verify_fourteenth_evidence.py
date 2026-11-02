#!/usr/bin/env python3
"""Export only visible historical evidence from an explicitly selected isolated run.

No model calls, Store initialization, daily database access or hidden reasoning.
The full package is private; the public manifest contains identities and hashes.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode()).hexdigest()


def selected(value, keys):
    return {key: value[key] for key in keys if key in value}


def build_package(source: Path, job_id: str):
    source = source.resolve()
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    with sqlite3.connect(source.as_uri() + '?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        row = db.execute('SELECT * FROM synthesis_checkpoints WHERE job_id=?', (job_id,)).fetchone()
        if row is None:
            raise ValueError('No saved checkpoint for this exact historical task; do not reconstruct prose')
        body = json.loads(row['body'])
        snap = db.execute('SELECT body,created_at FROM snapshots WHERE account_id=? AND version=?',
                          (row['account_id'], row['input_version'])).fetchone()
        if snap is None:
            raise ValueError('Historical account snapshot missing')
        account = selected(json.loads(snap['body']), ('cash', 'holdings', 'preferences', 'version'))
        reports = [selected(report, ('report_id', 'agent_id', 'role_id', 'title', 'summary',
                   'findings', 'material_ids', 'open_questions', 'research_round',
                   'material_delivery_ids', 'calculation_ids')) for report in body.get('reports', [])]
        calculations = [selected(item, ('tool', 'arguments', 'output', 'agent_id', 'source_task_id',
                        'input_version', 'research_round', 'calculation_id', 'summary', 'signature'))
                        for item in body.get('calculations', [])]
        materials = [selected(item, ('id', 'url', 'source_title', 'classification', 'access', 'fetched_at'))
                     for item in body.get('materials', [])]
        receipts = [selected(item, ('id', 'agent_id', 'material_id', 'tool', 'body', 'delivered_at'))
                    for item in body.get('material_deliveries', [])]
        package = {'schema_version': 1, 'kind': 'real_historical_visible_evidence',
            'historical_task_id': job_id, 'historical_checkpoint_at': row['created_at'],
            'historical_snapshot_at': snap['created_at'], 'data_as_of': body.get('data_as_of'),
            'input_version': row['input_version'], 'account': account,
            'metrics': body.get('metrics', {}), 'reports': reports, 'calculations': calculations,
            'materials': materials, 'material_deliveries': receipts,
            'open_questions': body.get('open_questions', []),
            'unresolved_tasks': [selected(item, ('agent_id', 'title', 'task', 'status', 'error'))
                                 for item in body.get('unresolved_tasks', [])],
            'boundary': 'Historical simulated ledger, not a daily account or current market opinion. '
                        'Visible saved reports/calculations only; source-link access does not mean full text was read. '
                        'This reconstructed fixed bundle is not claimed byte-identical to historical HTTP input.'}
    after = hashlib.sha256(source.read_bytes()).hexdigest()
    if before != after:
        raise RuntimeError('Source changed while reading; package cannot be frozen')
    manifest = {'schema_version': 1, 'kind': package['kind'], 'historical_task_id': job_id,
        'historical_checkpoint_at': row['created_at'], 'data_as_of': package['data_as_of'],
        'source_database_sha256_before': before, 'source_database_sha256_after': after,
        'package_sha256': digest(package), 'private_package': True,
        'reports': [{'report_id': item.get('report_id'), 'role_id': item.get('role_id'),
                     'visible_content_sha256': digest(item), 'characters': len(item.get('summary', ''))}
                    for item in reports],
        'calculations': [{'calculation_id': item.get('calculation_id'), 'tool': item.get('tool'),
                          'content_sha256': digest(item), 'status': item.get('summary', {}).get('status')}
                         for item in calculations],
        'material_index_count': len(materials), 'material_delivery_count': len(receipts),
        'unresolved_task_count': len(package['unresolved_tasks']),
        'accounting': selected(account, ('cash', 'version')),
        'boundary': package['boundary'], 'paid_model_requests': 0}
    return package, manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--isolated-source', type=Path, required=True)
    parser.add_argument('--historical-job', required=True)
    parser.add_argument('--private-output', type=Path, required=True)
    parser.add_argument('--manifest', type=Path, required=True)
    args = parser.parse_args()
    if args.private_output.exists() or args.manifest.exists():
        parser.error('New output paths required; preserve prior attempts')
    package, manifest = build_package(args.isolated_source, args.historical_job)
    args.private_output.parent.mkdir(parents=True, exist_ok=True)
    args.private_output.parent.chmod(0o700)
    args.private_output.write_text(json.dumps(package, ensure_ascii=False, indent=2) + '\n')
    args.private_output.chmod(0o600)
    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    args.manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'package_sha256': manifest['package_sha256'], 'reports': len(package['reports']),
                      'calculations': len(package['calculations']), 'paid_model_requests': 0}))

if __name__ == '__main__':
    main()
