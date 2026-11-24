"""Bounded row access and reproducible arithmetic over immutable saved market data."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import json
import statistics

FIELDS = {'time', 'open', 'close', 'high', 'low', 'volume', 'amount', 'amplitude_pct', 'change_pct', 'change', 'turnover_pct'}
CANDLE_METHODS = {'signed_body': 'close-open', 'real_body': 'abs(close-open)',
                  'upper_shadow': 'high-max(open,close)', 'lower_shadow': 'min(open,close)-low'}
FIELDS.update(CANDLE_METHODS)


def _price(value):
    try:
        result = Decimal(str(value))
        return result if result.is_finite() and result > 0 else None
    except (InvalidOperation, TypeError, ValueError):
        return None


def _candle(row):
    prices = {key: _price(row.get(key)) for key in ('open', 'high', 'low', 'close')}
    result = dict.fromkeys(CANDLE_METHODS)
    missing = [key for key in prices if row.get(key) is None]
    invalid = [key for key, value in prices.items() if value is None]
    if invalid:
        return {**result, 'candle_status': 'unavailable' if missing else 'invalid',
                'candle_reason': ('missing_ohlc:' + ','.join(missing)) if missing else 'nonfinite_or_nonpositive_ohlc'}
    opening, high, low, close = (prices[key] for key in ('open', 'high', 'low', 'close'))
    if high < max(opening, close) or low > min(opening, close) or low > high:
        return {**result, 'candle_status': 'invalid', 'candle_reason': 'inconsistent_ohlc_bounds'}
    return {'signed_body': str(close-opening), 'real_body': str(abs(close-opening)),
            'upper_shadow': str(high-max(opening, close)), 'lower_shadow': str(min(opening, close)-low),
            'candle_status': 'ok', 'candle_reason': None}


def _tail_close_streak(rows, data, adjustment):
    """Count comparable adjacent saved observations, never invent trading dates."""
    result = {'status': 'insufficient_data', 'direction': None, 'change_intervals': 0,
              'baseline_date': data[-1][0] if data else None, 'first_change_date': None,
              'end_date': data[-1][0] if data else None, 'boundary': 'selection_start',
              'preceding_date': None, 'left_censored': True, 'calendar_continuity': 'unverified'}
    if not data:
        return {**result, 'boundary': 'no_rows'}
    if not adjustment or adjustment == 'unknown':
        return {**result, 'status': 'unverified_adjustment', 'change_intervals': None,
                'baseline_date': None, 'boundary': 'unknown_adjustment'}
    def issue(index):
        if rows[index].get('adjustment', adjustment) != adjustment:
            return 'incompatible_adjustment'
        if data[index][1] is None:
            return 'missing_or_invalid_close'
        return None
    if last_issue := issue(len(data)-1):
        return {**result, 'change_intervals': None, 'baseline_date': None, 'boundary': last_issue}
    for index in range(len(data)-1, 0, -1):
        if boundary := issue(index-1):
            result.update(boundary=boundary, preceding_date=data[index-1][0])
            break
        change = data[index][1] - data[index-1][1]
        direction = 'up' if change > 0 else 'down' if change < 0 else 'flat'
        if result['direction'] is not None and direction != result['direction']:
            result.update(boundary='direction_change', preceding_date=data[index-1][0], left_censored=False)
            break
        result.update(status='ok', direction=direction, change_intervals=result['change_intervals']+1,
                      baseline_date=data[index-1][0], first_change_date=data[index][0])
    return result


def data_from_material(material):
    if material.get('access') != 'structured_data' or material.get('representation') == 'agent_extraction':
        raise ValueError('材料不是已保存的结构化原始行情；链接和模型提取不能升级为行情')
    body = json.loads(material.get('content') or '{}')
    if not isinstance(body, dict) or not isinstance(body.get('rows'), list):
        raise ValueError('材料没有结构化行')
    return body


def selected_rows(body, start=None, end=None):
    for value in (start, end):
        if value:
            date.fromisoformat(value)
    if start and end and start > end:
        raise ValueError('开始日期不能晚于结束日期')
    rows = []
    for row in body['rows']:
        stamp = str(row.get('time') or '')
        if not stamp:
            continue
        day = stamp[:10]
        date.fromisoformat(day)
        if (not start or day >= start) and (not end or day <= end):
            rows.append(row)
    return sorted(rows, key=lambda r: r['time'])


def provenance(material, body):
    return {'material_id': material['id'], 'material_identity': {k: material.get(k) for k in
            ('document_version_id', 'document_version', 'content_hash', 'representation_kind', 'read_completeness')},
            'symbol': body.get('symbol') or body.get('benchmark') or body.get('sector_id'),
            'source': body.get('source'), 'source_url': body.get('source_url'), 'fetched_at': body.get('fetched_at'),
            'as_of': body.get('as_of'), 'source_status': body.get('status', 'unknown'),
            'error': str(body['error'])[:300] if body.get('error') else None,
            'adjustment': body.get('adjustment', 'unknown'),
            'interval': body.get('interval', 'unknown'), 'source_requested_range': body.get('requested_range'),
            'source_truncated': bool(body.get('truncated') or body.get('provider_cap_reached')),
            'volume_unit': body.get('volume_unit'), 'amount_unit': body.get('amount_unit'),
            **{k: body[k] for k in ('fallback_from', 'primary_failure', 'fallback_note', 'cache_hit',
                'cache_age_seconds', 'cache_stale', 'error_kind', 'failure_stage', 'next_action') if k in body}}


def query_rows(material, *, start=None, end=None, fields=None, limit=30, offset=0, order='desc', symbol=None, max_chars=6000):
    body = data_from_material(material)
    meta = provenance(material, body)
    if symbol and symbol != meta['symbol']:
        raise ValueError('指定证券与材料身份不同')
    if order not in ('asc', 'desc'):
        raise ValueError('排序仅支持 asc 或 desc')
    fields = fields or ['time', 'open', 'close', 'high', 'low', 'volume', 'amount']
    if not isinstance(fields, list) or not fields or len(fields) > len(FIELDS) or any(f not in FIELDS for f in fields):
        raise ValueError('仅支持 time/OHLCV/成交额、供应商涨跌及实体/影线字段')
    fields = list(dict.fromkeys(['time', *fields]))
    rows = selected_rows(body, start, end)
    if order == 'desc':
        rows.reverse()
    limit, offset = min(100, max(1, int(limit))), max(0, int(offset))
    output = {**meta, 'status': body.get('status', 'unknown'), 'access': 'structured_rows',
              'requested_range': {'start': start, 'end': end}, 'order': order, 'fields': fields,
              'selection': 'latest_rows_first' if order == 'desc' else 'earliest_rows_first',
              'matched_rows': len(rows), 'stored_rows': len(body['rows']), 'offset': offset, 'requested_limit': limit,
              'rows': [], 'row_count': 0, 'returned_range': None, 'has_more': False, 'next_offset': None,
              'missing_exact_date': start if start == end and start and not rows and body.get('status') in ('ok', 'empty') else None,
              'missing_fields': [], 'truncated': False,
              'coverage_note': '只返回已保存的实际行；不以最近日期替代确切日期。缺失交易记录不补零。'}
    candle_fields = [field for field in fields if field in CANDLE_METHODS]
    if candle_fields:
        output.update(derived_field_methods={key: CANDLE_METHODS[key] for key in candle_fields},
                      derived_field_unit='source OHLC price unit',
                      derived_field_basis='same saved row; no adjustment conversion or long/short classification')
    budget = min(14000, max(2200, int(max_chars)))
    def budget_error(reason):
        # A page that cannot return one row must not point back to itself.
        # Keep a small error envelope even when provider metadata is oversized.
        return {'material_id': material['id'], 'status': 'response_budget_exceeded',
                'access': 'structured_rows', 'error_kind': reason, 'requested_range': {'start': start, 'end': end},
                'order': order, 'fields': fields, 'offset': offset, 'requested_limit': limit,
                'rows': [], 'row_count': 0, 'returned_range': None, 'has_more': False, 'next_offset': None,
                'truncated': True, 'remaining_stored_rows': max(0, len(rows)-offset),
                'next_action': '缩小 fields（如仅 time/close）后重试本页；若单字段或元数据仍过大，使用 read_material_range 按字符分段读取同一材料。原始已存数据未删除。'}
    if len(json.dumps(output, ensure_ascii=False)) > budget - 400:
        return budget_error('metadata_exceeds_response_budget')
    for row in rows[offset:offset + limit]:
        value = {k: row.get(k) for k in fields}
        if candle_fields:
            candle = _candle(row)
            value.update({key: candle[key] for key in [*candle_fields, 'candle_status', 'candle_reason']})
        # Leave space for the final range, counters and missing-field annotation.
        if len(json.dumps({**output, 'rows': [*output['rows'], value]}, ensure_ascii=False)) > budget - 400:
            break
        output['rows'].append(value)
    n = len(output['rows'])
    if not n and offset < len(rows):
        return budget_error('row_exceeds_response_budget')
    output.update(row_count=n, has_more=offset+n < len(rows), next_offset=offset+n if offset+n < len(rows) else None,
                  truncated=offset > 0 or offset+n < len(rows) or meta['source_truncated'],
                  missing_fields=[f for f in fields if any(row.get(f) is None for row in output['rows'])])
    if n:
        stamps = [r['time'] for r in output['rows']]
        output['returned_range'] = {'start': min(stamps), 'end': max(stamps)}
    if not rows:
        if body.get('status') in ('ok', 'empty'):
            output['status'] = 'missing' if start == end and start else 'empty'
        else:
            output['reason'] = 'source_unavailable' if body.get('status') == 'unavailable' else 'source_state_unverified'
            output['next_action'] = '查看 error 和 source_status；可按原证券、日期及口径重新调用 history，或读取 material_id 的原始失败记录。来源失败不能证明指定日期没有交易记录。'
    return output


def indicators(material, *, start=None, end=None, window=20, benchmark=None, max_chars=6000):
    body = data_from_material(material)
    rows = selected_rows(body, start, end)
    base = {**provenance(material, body), 'access': 'calculation', 'requested_range': {'start': start, 'end': end},
            'status': 'insufficient_data', 'classification': 'calculation',
            'methods': {'return': 'last_close/first_close-1 across return_period', 'moving_average': 'mean(last window closes)',
                        'volatility': 'sample standard deviation of consecutive close returns; not annualized',
                        'relative_return': 'asset return minus benchmark return on common trading dates',
                        'tail_close_streak': 'tail count of adjacent comparable close changes',
                        'close_to_ma_change_pct': '(last_close/moving_average-1)*100'}}
    window = int(window)
    if not 2 <= window <= 252:
        raise ValueError('均线窗口为2–252行')
    if body.get('status') not in (None, 'ok', 'empty', 'unknown'):
        return {**base, 'status': body['status'], 'reason': 'source_unavailable', 'valid_samples': 0}
    if body.get('interval') != 'daily':
        return {**base, 'reason': 'daily_bars_required'}
    if len(rows) > 252:
        return {**base, 'reason': 'range_too_large', 'matched_rows': len(rows), 'note': '请缩小日期区间至252行以内；完整材料仍可分页读取'}
    def values(source, strict=True):
        seen, vals = set(), []
        for r in source:
            d = r['time'][:10]
            if d in seen:
                raise ValueError('同一日期多行，不能用数组位置对齐')
            seen.add(d)
            close = _price(r.get('close'))
            if strict and close is None:
                raise ValueError('价格缺失或无效；不补零')
            vals.append((d, close))
        return vals
    data = values(rows, strict=False)
    base['tail_close_streak'] = _tail_close_streak(rows, data, body.get('adjustment'))
    base['close_to_ma_change_pct'] = None
    def bounded(result):
        if len(json.dumps(result, ensure_ascii=False)) > min(14000, max(2200, int(max_chars))):
            return {**base, 'reason': 'input_exceeds_response_budget', 'matched_rows': len(rows),
                    'note': '请缩小计算日期范围；计算输入不能被静默截断。完整行情可用query_market_rows分页读取。'}
        return result
    invalid_dates = [day for day, close in data if close is None]
    inconsistent_dates = [row['time'][:10] for row in rows
                          if row.get('adjustment', body.get('adjustment')) != body.get('adjustment')]
    if invalid_dates or inconsistent_dates:
        return bounded({**base, 'valid_samples': len(data)-len(invalid_dates),
                'reason': 'missing_or_invalid_close' if invalid_dates else 'incompatible_row_adjustment',
                'invalid_close_dates': invalid_dates, 'incompatible_adjustment_dates': inconsistent_dates,
                'input_closes': [[day, str(close) if close is not None else None] for day, close in data]})
    if len(data) < 2:
        return {**base, 'valid_samples': len(data), 'reason': 'at_least_two_actual_closes_required'}
    def ret(v):
        return v[-1][1] / v[0][1] - 1
    returns = [data[i][1]/data[i-1][1]-1 for i in range(1,len(data))]
    result = {**base, 'status': 'ok', 'input_closes': [[d,str(c)] for d,c in data], 'valid_samples': len(data),
              'returned_range': {'start': data[0][0], 'end': data[-1][0]}, 'return': str(ret(data)),
              'return_period': {'start': data[0][0], 'end': data[-1][0],
                                'return_intervals': len(data)-1},
              'window': window, 'moving_average': str(sum(c for _,c in data[-window:])/window) if len(data) >= window else None,
              'moving_average_period': {'start': data[-window][0] if len(data) >= window else None,
                                        'end': data[-1][0], 'window': window},
              'moving_average_gap': None if len(data) >= window else 'insufficient_window_rows',
              'volatility': statistics.stdev([float(x) for x in returns]) if len(returns) >= 2 else None,
              'return_samples': len(returns)}
    if result['moving_average'] is not None:
        result['close_to_ma_change_pct'] = str((data[-1][1]/Decimal(result['moving_average'])-1)*100)
    if benchmark:
        other = data_from_material(benchmark)
        result['benchmark'] = provenance(benchmark, other)
        adjustment = body.get('adjustment')
        if other.get('status') not in (None, 'ok', 'empty', 'unknown'):
            result.update(relative_return=None, comparison_status='benchmark_source_unavailable')
        elif other.get('interval') != 'daily' or not adjustment or adjustment == 'unknown' or (other.get('adjustment') not in (adjustment, 'index')):
            result.update(relative_return=None, comparison_status='incompatible_adjustment_or_interval')
        else:
            bvalues = values(selected_rows(other, start, end))
            left, right = dict(data), dict(bvalues)
            common = sorted(left.keys() & right.keys())
            same_snapshot = None
            try:
                left_fetch = datetime.fromisoformat(body.get('fetched_at') or '')
                right_fetch = datetime.fromisoformat(other.get('fetched_at') or '')
                if left_fetch.tzinfo is not None and right_fetch.tzinfo is not None:
                    same_snapshot = left_fetch == right_fetch
            except (TypeError, ValueError):
                pass
            result.update(alignment='intersection_of_actual_trading_dates' , aligned_samples=len(common),
                          excluded_asset_dates=sorted(left.keys()-right.keys()), excluded_benchmark_dates=sorted(right.keys()-left.keys()),
                          same_fetch_snapshot=same_snapshot,
                          fetch_snapshot_comparison_basis=('timestamp_equality_not_atomic_snapshot_guarantee'
                              if same_snapshot is not None else 'missing_or_invalid_fetch_timestamps'),
                          comparison_input_closes=[[d,str(left[d]),str(right[d])] for d in common])
            if len(common) >= 2:
                asset_return=left[common[-1]]/left[common[0]]-1
                benchmark_return=right[common[-1]]/right[common[0]]-1
                result.update(comparison_status='ok', relative_return=str(asset_return-benchmark_return),
                              aligned_asset_return=str(asset_return), aligned_benchmark_return=str(benchmark_return))
            else:
                result.update(comparison_status='insufficient_common_dates', relative_return=None)
    return bounded(result)
