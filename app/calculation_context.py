"""Deterministic, value-first views of completed arithmetic, without raw rows.

The full result is retained by the Runtime separately. These views are evidence
delivery, not model judgements, and deliberately never choose a direction.
"""
from __future__ import annotations

import copy
import hashlib
import json


# Keep complete short expressions, not a prefix of a large series. This is an
# evidence-size bound inside the existing context budget, not a new reservation.
_SHORT_INPUT_CHARS = 512


def _calculate_inputs(output, arguments):
    if 'inputs' in output:
        inputs, source = output['inputs'], 'output.inputs'
    elif 'values' in arguments:
        inputs, source = arguments['values'], 'arguments.values'
    else:
        return {'inputs_visibility': {'status': 'unavailable', 'source': None,
                                     'reason': 'inputs not recorded'}}
    visibility = {'status': 'archive_only', 'source': source, 'archive_ref': 'full_inputs'}
    if not isinstance(inputs, list):
        visibility['reason'] = 'inputs are not a scalar list'
    elif any(type(value) not in (str, int, float) for value in inputs):
        visibility['reason'] = 'inputs are not a scalar list'
    elif any(isinstance(value, str) and len(value) > _SHORT_INPUT_CHARS for value in inputs):
        visibility['reason'] = 'input representation exceeds short-expression bound'
    else:
        try:
            size = len(json.dumps(inputs, ensure_ascii=False, allow_nan=False))
        except (TypeError, ValueError, OverflowError):
            visibility['reason'] = 'inputs are not finite JSON scalars'
        else:
            if size <= _SHORT_INPUT_CHARS:
                return {'inputs': copy.deepcopy(inputs),
                        'inputs_visibility': {'status': 'complete', 'source': source}}
            visibility['reason'] = 'input representation exceeds short-expression bound'
    return {'inputs_visibility': visibility}


def signature(tool, arguments, output):
    return hashlib.sha256(json.dumps([tool, arguments, output], sort_keys=True,
        ensure_ascii=False, default=str).encode()).hexdigest()


def projection(tool, output, arguments=None):
    if not isinstance(output, dict):
        return None
    arguments = arguments or {}
    if tool == 'calculate':
        inputs = output.get('inputs', arguments.get('values'))
        return {'tool': tool, 'status': 'failed' if output.get('error') or output.get('failed') else 'ok',
            'operation': output.get('operation', arguments.get('operation')),
            'result': copy.deepcopy(output.get('result')), 'error': output.get('error'),
            'valid_samples': len(inputs) if isinstance(inputs, list) else None,
            'method': 'percent = a / b * 100 (ratio, not percent change); subtract = a - b; percent-valued return differences are percentage points',
            'unit': 'percent' if output.get('operation', arguments.get('operation')) == 'percent' else 'input-dependent; not inferred',
            'security': 'not supplied by calculate', 'date_range': 'not supplied by calculate',
            'source_material_version': 'not supplied by calculate',
            **_calculate_inputs(output, arguments)}
    if tool == 'query_market_rows':
        geometry_fields = ('signed_body', 'real_body', 'upper_shadow', 'lower_shadow')
        requested = arguments.get('fields') or output.get('fields') or []
        if not any(field in requested for field in geometry_fields):
            return None
        keys = ('status', 'material_id', 'material_identity', 'symbol', 'source', 'source_url',
            'fetched_at', 'as_of', 'source_status', 'adjustment', 'interval', 'source_requested_range',
            'source_truncated', 'requested_range', 'returned_range', 'access', 'order', 'selection',
            'fields', 'matched_rows', 'stored_rows', 'offset', 'requested_limit', 'row_count',
            'has_more', 'next_offset', 'missing_exact_date', 'missing_fields', 'truncated',
            'remaining_stored_rows', 'error_kind', 'error', 'reason', 'next_action',
            'coverage_note', 'derived_field_methods', 'derived_field_unit', 'derived_field_basis')
        rows = output.get('rows') or []
        return {'tool': tool, **{key: copy.deepcopy(output[key]) for key in keys if key in output},
            'candle_geometry': [{key: copy.deepcopy(row[key]) for key in
                ('time', *geometry_fields, 'candle_status', 'candle_reason') if key in row} for row in rows],
            'units': {key: 'source OHLC price unit' for key in geometry_fields if key in requested},
            'geometry_scope': 'all returned rows; unrequested fields not inferred; full returned result archived in full_inputs; original OHLC available from the saved material identity'}
    if tool != 'market_indicators':
        return None
    # Scalars / small provenance objects only. No JSON-prefix slicing: numerical
    # results occur after hundreds of input rows in the original tool output.
    keys = ('status', 'material_id', 'material_identity', 'symbol', 'source', 'fetched_at',
        'as_of', 'source_status', 'adjustment', 'interval', 'source_requested_range',
        'source_truncated', 'requested_range', 'returned_range', 'valid_samples',
        'return_samples', 'window', 'return', 'return_period', 'moving_average', 'moving_average_period', 'moving_average_gap',
        'tail_close_streak', 'close_to_ma_change_pct', 'invalid_close_dates', 'incompatible_adjustment_dates',
        'volatility', 'relative_return', 'aligned_asset_return', 'aligned_benchmark_return',
        'comparison_status', 'aligned_samples', 'alignment', 'same_fetch_snapshot',
        'fetch_snapshot_comparison_basis', 'methods', 'reason', 'error', 'matched_rows')
    result = {'tool': tool, **{key: copy.deepcopy(output[key]) for key in keys if key in output},
        'units': {'return': 'ratio', 'relative_return': 'ratio difference; multiply by 100 for percentage points',
                  'moving_average': 'source close price unit', 'volatility': 'ratio; not annualized'}}
    result['units'].update(close_to_ma_change_pct='percent_change', tail_close_streak='change_intervals')
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
