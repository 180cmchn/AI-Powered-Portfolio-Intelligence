"""Small request-boundary cache diagnostics. No bodies or opaque reasoning logs."""
from __future__ import annotations

import hashlib
import json


def serialized(value):
    # Match httpx's JSON wire encoder, including insertion order of opaque items.
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(serialized(value).encode('utf-8')).hexdigest()


def canonical(value):
    """Only for newly constructed application input/schema, never old outputs."""
    if isinstance(value, dict):
        return {key: canonical(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [canonical(item) for item in value]
    return value


def frozen_tools(session, tools):
    normalized = sorted((canonical(tool) for tool in tools), key=lambda item: item.get('name', ''))
    fingerprint = digest(normalized)
    if session._tool_schema_hash != fingerprint:
        session._tool_schema_hash, session._tool_schema = fingerprint, normalized
    return session._tool_schema


def observe_payload(session, payload, request_id):
    input_key = 'input' if 'input' in payload else 'messages'
    history = payload.get(input_key, [])
    wire = serialized(payload)
    hashes = [digest(item) for item in history]
    # Hash known first-level fields so a changed path needs no retained body.
    allowed = {'role', 'type', 'content', 'arguments', 'output', 'call_id', 'tool_call_id',
               'tool_calls', 'id', 'name', 'status', 'encrypted_content', 'reasoning_content'}
    item_fields = [{key: digest(value) for key, value in item.items() if key in allowed}
                   if isinstance(item, dict) else {} for item in history]
    current = {'request_id': request_id, 'epoch': session.cache_epoch, 'history': hashes,
        'item_fields': item_fields, 'system': digest(payload.get('instructions', history[:1] if input_key == 'messages' else None)),
        'tools': digest(payload.get('tools', [])), 'model': digest(payload.get('model')),
        'effort': digest(payload.get('reasoning', payload.get('reasoning_effort'))),
        'tool_choice': digest(payload.get('tool_choice'))}
    previous = session._previous_payload
    is_prefix, first_path, history_path, reason, changed = None, None, None, 'first', False
    if previous:
        changed = previous['epoch'] != current['epoch']
        is_prefix = hashes[:len(previous['history'])] == previous['history']
        reason = session.cache_epoch_reason if changed else 'append_only'
        for key in ('model', 'effort', 'system', 'tools', 'tool_choice'):
            if previous[key] != current[key]:
                first_path = {'system': 'instructions' if input_key == 'input' else 'messages[0]',
                              'effort': 'reasoning'}.get(key, key)
                if not changed:
                    reason = {'system': 'system_changed', 'tools': 'tools_changed',
                              'model': 'model_changed', 'effort': 'effort_changed',
                              'tool_choice': 'tool_choice_changed'}[key]
                break
        if not is_prefix:
            index = next((i for i, old in enumerate(previous['history'])
                          if i >= len(hashes) or hashes[i] != old), 0)
            path = f'{input_key}[{index}]'
            if index < len(hashes):
                old, new = previous['item_fields'][index], item_fields[index]
                field = next((key for key in sorted(old.keys() | new.keys()) if old.get(key) != new.get(key)), None)
                if field:
                    path += '.' + field
            history_path = path
            first_path = first_path or path
            if reason == 'append_only':
                reason = 'history_rewritten'
    session._previous_payload = current
    return {'session_id': session.session_id, 'epoch': session.cache_epoch,
        'previous_request_id': previous['request_id'] if previous else None,
        'epoch_changed': changed, 'change_reason': reason, 'first_changed_path': first_path,
        'history_first_changed_path': history_path,
        'history_is_prefix': is_prefix, 'previous_input_items': len(previous['history']) if previous else 0,
        'input_items': len(history), 'history_item_hashes': hashes, 'system_hash': current['system'],
        'tools_hash': current['tools'], 'payload_hash': digest(payload),
        'payload_chars': len(wire), 'payload_bytes': len(wire.encode('utf-8')),
        'input_chars': len(serialized(history)), 'input_bytes': len(serialized(history).encode('utf-8')),
        'token_estimate': None, 'token_estimate_status': 'no_applicable_tokenizer'}
