"""Reported model counters, with unknowns kept separate from explicit zeroes."""
from __future__ import annotations

import copy
import json
import math
from urllib.parse import urlsplit


def numeric(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def normalized_usage(request):
    usage = request.get('usage')
    usage = usage if isinstance(usage, dict) else {}
    # Select one protocol vocabulary; never add synonymous or forwarder fields.
    responses = request.get('protocol') == 'responses'
    primary = 'input_tokens' if responses else 'prompt_tokens'
    if primary not in usage:
        # Observed compatible envelopes sometimes use the other vocabulary.
        # The actual protocol's field wins if both aliases are present.
        if 'input_tokens' in usage:
            responses = True
        elif 'prompt_tokens' in usage:
            responses = False
    input_key, output_key = ('input_tokens', 'output_tokens') if responses else ('prompt_tokens', 'completion_tokens')
    details = usage.get(input_key.replace('_tokens', '_tokens_details'))
    details = details if isinstance(details, dict) else {}
    output_details = usage.get(output_key.replace('_tokens', '_tokens_details'))
    output_details = output_details if isinstance(output_details, dict) else {}
    values = {'input_tokens': usage.get(input_key), 'cache_read_tokens': details.get('cached_tokens'),
        'output_tokens': usage.get(output_key), 'reasoning_tokens': output_details.get('reasoning_tokens'),
        # No inference from total input, or vendor-specific billing ticks.
        'cache_write_tokens': details.get('cache_write_tokens')
            if urlsplit(request.get('base_url') or '').hostname == 'api.openai.com' else None}
    result, anomalies = {}, []
    for key, value in values.items():
        result[key] = value if numeric(value) else None
        if value is not None and (not numeric(value) or value < 0 or int(value) != value):
            anomalies.append(key + '_invalid')
    inp, cached, out, reasoning = (result[k] for k in ('input_tokens', 'cache_read_tokens', 'output_tokens', 'reasoning_tokens'))
    result['uncached_input_tokens'] = inp - cached if inp is not None and cached is not None else None
    result['visible_output_tokens'] = out - reasoning if out is not None and reasoning is not None else None
    if inp is not None and cached is not None and cached > inp:
        anomalies.append('cache_read_exceeds_input')
    if out is not None and reasoning is not None and reasoning > out:
        anomalies.append('reasoning_exceeds_output')
    result['anomalies'] = anomalies
    return result


def _add_counters(total, values):
    for key, value in values.items():
        if isinstance(value, dict):
            if isinstance(total.get(key), (int, float)):
                continue  # Conflicting vendor shapes remain visible per request.
            _add_counters(total.setdefault(key, {}), value)
        elif numeric(value):
            if isinstance(total.get(key), dict):
                continue
            total[key] = total.get(key, 0) + value


def _summary(requests):
    usage, complete, anomalous = {}, [], 0
    normalized_rows = []
    for request in requests:
        _add_counters(usage, request.get('usage') or {})
        row = normalized_usage(request)
        normalized_rows.append(row)
        anomalous += bool(row['anomalies'])
        if row['input_tokens'] is not None and row['cache_read_tokens'] is not None:
            complete.append(row)
    denominator = sum(row['input_tokens'] for row in complete)
    numerator = sum(row['cache_read_tokens'] for row in complete)
    count = len(requests)
    cache = {'requests': count, 'complete_requests': len(complete), 'unknown_requests': count - len(complete),
        'coverage': len(complete) / count if count else None, 'scope': 'reported_samples',
        'reported_input_tokens': denominator if complete else None,
        'cache_read_tokens': numerator if complete else None,
        'uncached_input_tokens': denominator - numerator if complete else None,
        'weighted_read_ratio': numerator / denominator if denominator else None,
        'anomalous_requests': anomalous}
    counters = ('input_tokens', 'cache_read_tokens', 'uncached_input_tokens', 'output_tokens',
                'reasoning_tokens', 'visible_output_tokens', 'cache_write_tokens')
    known = {key: sum(row[key] is not None for row in normalized_rows) for key in counters}
    totals = {key: sum(row[key] for row in normalized_rows if row[key] is not None) if known[key] else None
              for key in counters}
    totals.update(scope='reported_values', known_requests=known,
        coverage={key: known[key] / count if count else None for key in counters})
    return {'requests': count, 'duration_ms': round(sum(r.get('duration_ms', 0) for r in requests), 2),
        'failed_requests': sum(bool(r.get('error')) for r in requests), 'usage': usage or None,
        'usage_reported_requests': sum(r.get('usage') is not None for r in requests), 'cache_summary': cache,
        'normalized_totals': totals}


def usage_summary(requests):
    result, grouped, sequence = _summary(requests), {}, []
    fields = ('provider_id', 'model', 'reported_model', 'reasoning_effort', 'reported_reasoning_effort', 'protocol', 'account_id', 'task_id', 'agent_id',
              'session_id', 'session_origin', 'cache_epoch', 'stage')
    for request in requests:
        prefix = request.get('prefix_diagnostic') or {}
        category = ('failed' if request.get('error') else 'first_session_request' if prefix.get('change_reason') == 'first'
            else 'first_epoch_request' if prefix.get('epoch_changed') else 'continuation')
        identity = {key: request.get(key) for key in fields}
        identity.update(route=(request.get('model_network') or {}).get('route'), request_category=category)
        key = json.dumps(identity, sort_keys=True, default=str)
        grouped.setdefault(key, {'identity': identity, 'requests': []})['requests'].append(request)
        search = request.get('native_search_requested')
        sequence.append({**identity, **normalized_usage(request),
            'normalized_usage': normalized_usage(request),
            'native_search_requested': search if isinstance(search, bool) else None,
            'cache_epoch_reason': request.get('cache_epoch_reason'),
            **{key: copy.deepcopy(request.get(key)) for key in ('local_request_id', 'request_id', 'response_id',
                'started_at', 'duration_ms', 'error', 'cache_hint', 'prefix_diagnostic')},
            'cost_known': bool(request.get('cost_known')), 'cost': copy.deepcopy(request.get('cost'))})
    result['groups'] = [{**group['identity'], **_summary(group['requests'])} for group in grouped.values()]
    result['sequence'] = sequence
    return result


def compact_usage_summary(summary):
    """Expose cache counts for task lists without returning request or group rows."""
    if not isinstance(summary, dict) or summary.get('cache_summary') is None:
        return None
    reasons = {}
    breakdown = {key: dict(count=0, paired=0, reported_input_tokens=0, cache_read_tokens=0)
                 for key in ('non_search_all', 'non_search_cross_turn', 'search_all')}
    for row in summary.get('sequence') or ():
        change = (row.get('prefix_diagnostic') or {}).get('change_reason')
        epoch = row.get('cache_epoch_reason')
        reason = change or epoch or 'unknown'
        reasons[reason] = reasons.get(reason, 0) + 1
        search = row.get('native_search_requested')
        if search is True:
            cohorts = ['search_all']
        elif search is False:
            cohorts = ['non_search_all']
            if change != 'first' and epoch != 'first' and row.get('request_category') != 'first_session_request':
                cohorts.append('non_search_cross_turn')
        else:
            continue
        usage = row.get('normalized_usage') or row
        inp, read = usage.get('input_tokens'), usage.get('cache_read_tokens')
        for key in cohorts:
            counts = breakdown[key]
            counts['count'] += 1
            if numeric(inp) and numeric(read):
                counts['paired'] += 1
                counts['reported_input_tokens'] += inp
                counts['cache_read_tokens'] += read
    for counts in breakdown.values():
        inp = counts['reported_input_tokens']
        counts['weighted_read_ratio'] = counts['cache_read_tokens'] / inp if inp else None
        if not counts['paired']:
            counts.update(reported_input_tokens=None, cache_read_tokens=None)
    return {**{key: summary.get(key) for key in ('requests', 'duration_ms', 'failed_requests')},
        'cache_summary': copy.deepcopy(summary.get('cache_summary')),
        'normalized_totals': copy.deepcopy(summary.get('normalized_totals')),
        'prefix_reason_counts': reasons, 'native_search_breakdown': breakdown}
