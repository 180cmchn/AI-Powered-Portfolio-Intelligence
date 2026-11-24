"""Read the effective application context after ordered tail updates.

This helper inspects actual captured HTTP messages; it does not mutate payloads
or model a result that was not sent. Prefix assertions must use the raw payload.
"""
import copy
import json


def effective_context(payload):
    messages = payload.get('input', payload.get('messages', []))
    result = {}
    children, calculations, indexes, contexts = {}, {}, {}, {}
    first = True
    for message in messages:
        if message.get('role') != 'user':
            continue
        try:
            value = json.loads(message.get('content', '{}'))
        except (ValueError, TypeError):
            continue
        if not isinstance(value, dict) or value.get('truncated_for_context'):
            continue
        if first:
            result = copy.deepcopy(value)
            first = False
        if 'runtime_status' in value:
            status = copy.deepcopy(value['runtime_status'])
            if not status.get('children_update'):
                children = {}
            for item in status.get('children', []):
                children[item['agent_id']] = {**children.get(item['agent_id'], {}), **item}
                for key in item.get('removed_fields', []):
                    children[item['agent_id']].pop(key, None)
            status['children'] = list(children.values())
            result['runtime_status'] = status
        if 'prediction_context' in value:
            result['prediction_context'] = copy.deepcopy(value['prediction_context'])
        if 'calculation_summaries' in value:
            for item in value['calculation_summaries']:
                calculations[item['calculation_id']] = copy.deepcopy(item)
            result['calculation_summaries'] = list(calculations.values())
            contexts.update(copy.deepcopy(value.get('calculation_contexts', {})))
            if contexts:
                result['calculation_contexts'] = contexts
        if 'calculation_indexes' in value:
            for item in value['calculation_indexes'].get('items', []):
                indexes[item['calculation_id']] = copy.deepcopy(item)
            result['calculation_indexes'] = {**copy.deepcopy(value['calculation_indexes']), 'items': list(indexes.values())}
        if 'calculation_access' in value:
            result['calculation_access'] = value['calculation_access']
    if calculations:
        resolved = []
        for item in calculations.values():
            reference = item.get('context_ref')
            if reference:
                assert reference in contexts, f'missing calculation context: {reference}'
                item = {**copy.deepcopy(contexts[reference]), **item}
            if item.get('model_config_ref') == 'seed.model_config':
                assert isinstance(result.get('model_config'), dict), 'missing referenced seed model_config'
                item = {'model_config': copy.deepcopy(result['model_config']), **item}
            resolved.append(item)
        result['calculation_summaries'] = resolved
    return result
