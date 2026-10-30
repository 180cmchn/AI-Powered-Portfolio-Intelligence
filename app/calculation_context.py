"""Deterministic, value-first views of completed arithmetic, without raw rows.

The full result is retained by the Runtime separately. These views are evidence
delivery, not model judgements, and deliberately never choose a direction.
"""
from __future__ import annotations

import copy
import hashlib
import json


def signature(tool, arguments, output):
    return hashlib.sha256(json.dumps([tool, arguments, output], sort_keys=True,
        ensure_ascii=False, default=str).encode()).hexdigest()


def projection(tool, output, arguments=None):
    if not isinstance(output, dict):
        return None
    arguments = arguments or {}
    if tool == 'calculate':
        inputs = output.get('inputs', arguments.get('values', []))
        return {'tool': tool, 'status': 'failed' if output.get('error') or output.get('failed') else 'ok',
            'operation': output.get('operation', arguments.get('operation')),
            'result': copy.deepcopy(output.get('result')), 'error': output.get('error'),
            'valid_samples': len(inputs) if isinstance(inputs, list) else None,
            'method': 'percent = a / b * 100; subtract = a - b; other operation uses the supplied ordered inputs',
            'unit': 'percent' if output.get('operation', arguments.get('operation')) == 'percent' else 'input-dependent; not inferred',
            'security': 'not supplied by calculate', 'date_range': 'not supplied by calculate',
            'source_material_version': 'not supplied by calculate'}
    if tool != 'market_indicators':
        return None
    # Scalars / small provenance objects only. No JSON-prefix slicing: numerical
    # results occur after hundreds of input rows in the original tool output.
    keys = ('status', 'material_id', 'material_identity', 'symbol', 'source', 'fetched_at',
        'as_of', 'source_status', 'adjustment', 'interval', 'source_requested_range',
        'source_truncated', 'requested_range', 'returned_range', 'valid_samples',
        'return_samples', 'window', 'return', 'moving_average', 'moving_average_gap',
        'volatility', 'relative_return', 'aligned_asset_return', 'aligned_benchmark_return',
        'comparison_status', 'aligned_samples', 'alignment', 'same_fetch_snapshot',
        'fetch_snapshot_comparison_basis', 'methods', 'reason', 'error', 'matched_rows')
    result = {'tool': tool, **{key: copy.deepcopy(output[key]) for key in keys if key in output},
        'units': {'return': 'ratio', 'relative_return': 'ratio difference',
                  'moving_average': 'source close price unit', 'volatility': 'ratio; not annualized'}}
    benchmark = output.get('benchmark')
    if isinstance(benchmark, dict):
        result['benchmark'] = {key: copy.deepcopy(benchmark[key]) for key in
            ('material_id', 'material_identity', 'symbol', 'source', 'source_status',
             'fetched_at', 'adjustment', 'interval', 'error') if key in benchmark}
    for key in ('excluded_asset_dates', 'excluded_benchmark_dates'):
        dates = output.get(key)
        if isinstance(dates, list):
            result[key] = {'count': len(dates), 'first': dates[0] if dates else None,
                           'last': dates[-1] if dates else None,
                           'full_list': 'retained with full inputs; not replaced by this range'}
    return result


def checkpoint_summaries(records):
    """Older calculate checkpoints remain attributable only to what they saved."""
    results = []
    for record in records if isinstance(records, list) else []:
        if not isinstance(record, dict):
            continue
        if isinstance(record.get('summary'), dict):
            results.append(copy.deepcopy(record['summary']))
        elif 'operation' in record and isinstance(record.get('result'), dict):
            summary = projection('calculate', record['result'], record)
            summary.update(agent_id=record.get('agent_id'), provenance='legacy checkpoint; original task/round unknown')
            results.append(summary)
    return results
