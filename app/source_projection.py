"""Lossless visible-source projection, never retrieval of hidden material."""
from __future__ import annotations

import copy
import hashlib
import json


_KIND = 'shared_row_raw_v1'
_RULE = ('Every result.rows[i].raw inherits result_projection.common_fields exactly; '
         'no original field or value is omitted.')


def _serialized(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _same(left, right):
    """JSON equality including number/string/bool types, unlike Python ==."""
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(_same(value, right[key]) for key, value in left.items())
    if isinstance(left, list):
        return len(left) == len(right) and all(_same(a, b) for a, b in zip(left, right))
    if isinstance(left, float):
        return left == right and repr(left) == repr(right)  # Preserve -0.0 versus 0.0.
    return left == right


def project_source_item(item):
    """Share only exact row.raw fields, keeping the entire result reconstructible.

    Stored results, original archive records and observation metadata are never
    changed. Different period values, types and row-specific metadata stay put.
    """
    if not isinstance(item, dict) or 'result_projection' in item:
        return item
    result = item.get('result')
    rows = result.get('rows') if isinstance(result, dict) else None
    if not isinstance(rows, list) or len(rows) < 2:
        return item
    raws = [row.get('raw') if isinstance(row, dict) else None for row in rows]
    if not all(isinstance(raw, dict) for raw in raws):
        return item
    common = {key: copy.deepcopy(value) for key, value in raws[0].items()
        if all(key in raw and _same(raw[key], value) for raw in raws)}
    if not common:
        return item
    try:
        original = _serialized(result)
        projected = copy.deepcopy(item)
        for row in projected['result']['rows']:
            row['raw'] = {key: value for key, value in row['raw'].items() if key not in common}
        projected['result_projection'] = {'kind': _KIND, 'common_fields': common,
            'source_result_sha256': hashlib.sha256(original.encode()).hexdigest(), 'rule': _RULE}
        if len(_serialized(projected)) >= len(_serialized(item)):
            return item
    except (TypeError, ValueError):
        return item
    return projected


def projected_result_matches(item, original):
    """A receipt needs the exact original result, not a partial projected view."""
    if not isinstance(item, dict) or 'result' not in item or item.get('truncated_for_synthesis'):
        return False
    projection = item.get('result_projection')
    if projection is None:
        return 'result_projection' not in item and _same(item['result'], original)
    if (not isinstance(projection, dict) or set(projection) != {
            'kind', 'common_fields', 'source_result_sha256', 'rule'}
            or projection.get('kind') != _KIND or projection.get('rule') != _RULE
            or not isinstance(projection.get('common_fields'), dict)
            or not projection['common_fields']):
        return False
    result = item['result']
    rows = result.get('rows') if isinstance(result, dict) else None
    if not isinstance(rows, list) or len(rows) < 2:
        return False
    common = projection['common_fields']
    if not all(isinstance(row, dict) and isinstance(row.get('raw'), dict)
               and not common.keys() & row['raw'].keys() for row in rows):
        return False
    expanded = copy.deepcopy(result)
    for row in expanded['rows']:
        row['raw'] = {**copy.deepcopy(common), **row['raw']}
    try:
        digest = hashlib.sha256(_serialized(expanded).encode()).hexdigest()
    except (TypeError, ValueError):
        return False
    return digest == projection['source_result_sha256'] and _same(expanded, original)


def diverse_source_order(candidates, discovery_tools, *, balance_repeated=False):
    """Latest representative per tool first, then remaining order, within class.

    Final submission can balance repeated tools by their serialized size:
    one large result must not receive the same repeated allowance as one short
    original excerpt. Only ordering changes; values and omissions stay exact.
    This does not promise every result fits. Original session records remain
    retrievable under the existing permissions, when recall is still allowed.
    """
    recent = sorted(enumerate(candidates), key=lambda pair: -pair[0])
    ordered = []
    for discovery in (False, True):
        first, repeated, represented = [], [], set()
        for pair in recent:
            kind, item = pair[1]
            if (item.get('tool') in discovery_tools) != discovery:
                continue
            identity = ('tool', item.get('tool')) if kind == 'tool' else ('kind', kind)
            if identity in represented:
                repeated.append(pair)
            else:
                represented.add(identity)
                first.append(pair)
        if not balance_repeated:
            ordered.extend(first + repeated)
            continue
        groups = {}
        for pair in first + repeated:
            kind, item = pair[1]
            identity = ('tool', item.get('tool')) if kind == 'tool' else ('kind', kind)
            groups.setdefault(identity, []).append(pair)
        positions = dict.fromkeys(groups, 0)
        charged = dict.fromkeys(groups, 0)
        while positions:
            # Insertion order resolves ties by the latest representative. The
            # next value within a tool remains the latest unselected result.
            identity = min(positions, key=lambda key: charged[key])
            pair = groups[identity][positions[identity]]
            ordered.append(pair)
            charged[identity] += len(json.dumps(pair[1][1], ensure_ascii=False, default=str))
            positions[identity] += 1
            if positions[identity] == len(groups[identity]):
                del positions[identity]
    return ordered
