"""Bounded reuse of public raw historical snapshots, never model extractions."""
from __future__ import annotations

from datetime import datetime
import json

from .providers import _date_range, SHANGHAI


def reusable_history(store, method, arguments, config):
    if method not in ('history', 'benchmark_history'):
        return None
    identity = arguments.get('symbol') if method == 'history' else arguments.get('benchmark', 'CSI300')
    interval = arguments.get('interval', 'daily')
    adjustment = arguments.get('adjust', 'none') if method == 'history' else 'index'
    # Adjusted histories can change past prices after new corporate actions.
    if interval != 'daily' or adjustment not in ('none', 'index'):
        return None
    try:
        first, last = _date_range(arguments.get('start'), arguments.get('end'))
    except ValueError:
        return None
    with store.connect() as db:
        records = db.execute("""SELECT m.body FROM material_catalog c JOIN materials m ON m.id=c.material_id
            WHERE c.scope='public' AND json_extract(c.metadata,'$.symbol')=?
            AND json_extract(c.metadata,'$.source_title')=? ORDER BY m.created_at DESC LIMIT 50""", (identity, method)).fetchall()
    stamp = datetime.now(SHANGHAI)
    provider = config.get('research_provider', config.get('quote_provider', 'eastmoney'))
    for record in records:
        material = json.loads(record[0])
        if material.get('access') != 'structured_data' or material.get('representation') == 'agent_extraction':
            continue
        try:
            body = json.loads(material.get('content') or '{}')
            observed = datetime.fromisoformat(body['fetched_at'])
            age = (stamp - observed).total_seconds()
            scope = body['requested_range']
            source = str(body.get('source', ''))
            allowed_source = (source.startswith(('eastmoney:', 'tencent:')) if provider == 'eastmoney'
                              else source.startswith('tushare:') if provider == 'tushare' else False)
            if not (allowed_source and 0 <= age <= 7 * 86400 and observed.tzinfo and
                    body.get('status') == 'ok' and body.get('rows') and
                    (body.get('symbol') or body.get('benchmark')) == identity and
                    body.get('interval') == interval and body.get('adjustment') == adjustment and
                    scope['start'] <= first <= last <= scope['end'] and
                    observed.strftime('%Y%m%d') >= last):
                continue
            if observed.strftime('%Y%m%d') == last and observed.hour < 15:
                continue
            rows = [r for r in body['rows'] if first <= str(r.get('time', '')).replace('-', '')[:8] <= last]
            if not rows:
                continue
        except (KeyError, ValueError, TypeError):
            continue
        return material, {'cache_reused_after_failure': True, 'cache_age_seconds': int(age),
            'cache_stale': True, 'cache_fetched_at': body['fetched_at'], 'cache_requested_range': {'start': first, 'end': last},
            'cache_note': '复用7日内已保存、覆盖请求区间的同身份未复权日线/指数原始快照；抓取时点保留，未验证此后供应商更正。缺失交易日不补零。'}
    return None
