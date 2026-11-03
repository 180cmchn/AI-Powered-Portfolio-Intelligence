"""Small final-submission contract, not an investment-content quality judge.

The model authors the decision and its scope. Runtime submission provenance and
publication identity are server-owned. Historical reports are never migrated by
this validator; it applies only to new publication attempts.
"""
from __future__ import annotations

import copy
from .domain import symbol
from .model import ModelError


class DeliveryContractError(ValueError, ModelError):
    code = 'delivery_contract_failed'


class DeliveryStateConflict(DeliveryContractError):
    code = 'delivery_state_conflict'


class FinalSubmissionMissing(DeliveryContractError):
    code = 'final_submission_missing'


MAX_RESEARCH_RECORDS = 100


def validate_research_records(records):
    """Validate new authored records before acceptance and persistence alike."""
    if not isinstance(records, list):
        raise DeliveryContractError('research_records: 需要对象数组')
    if len(records) > MAX_RESEARCH_RECORDS:
        raise DeliveryContractError(f'research_records[{MAX_RESEARCH_RECORDS}]: 单次最多 {MAX_RESEARCH_RECORDS} 条；请明确合并或限定本次提交范围')
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise DeliveryContractError(f'research_records[{index}]: 需要对象')
        if record.get('kind') not in ('market', 'sector', 'candidate'):
            raise DeliveryContractError(f'research_records[{index}].kind: 不在允许枚举中')


def _validate_expiry(value, path):
    """Only authored dates are checked; legacy persisted reports stay readable."""
    if value is None:
        return
    from .recommendations import parse_time
    try:
        if not isinstance(value, str) or not value.strip():
            raise ValueError()
        parse_time(value)
    except (ValueError, TypeError):
        raise DeliveryContractError(f'{path}: 需要有效日期文本或 null') from None


def validate_final_result(result, *, allowed_scope=None, required_scope=None, contract_context=None):
    if not isinstance(result, dict):
        raise DeliveryContractError('最终提交需要对象')
    if not isinstance(result.get('report'), str) or not result['report'].strip():
        raise DeliveryContractError('最终报告为空；子研究或成功请求不能代替最终报告发布')
    submission = result.get('submission')
    if not isinstance(submission, dict) or submission.get('action') != 'finish_analysis':
        raise FinalSubmissionMissing('未收到明确的 finish_analysis 最终提交；普通文字仅保存为进度或候选草稿')
    if (type(submission.get('contract_version')) is not int or submission['contract_version'] != 1 or
            not isinstance(submission.get('call_id'), str) or not submission['call_id'].strip()):
        raise DeliveryContractError('最终提交凭据不完整')
    if result.get('completion') not in ('full', 'limited'):
        raise DeliveryContractError('completion 需要 full 或 limited')
    if not isinstance(result.get('decision_summary'), str) or not result['decision_summary'].strip():
        raise DeliveryContractError('decision_summary 需要回答本轮问题的判断摘要')
    for field in ('scope', 'limitations'):
        if (not isinstance(result.get(field), list) or
                any(not isinstance(value, str) or not value.strip() for value in result[field])):
            raise DeliveryContractError(f'{field} 需要非空字符串组成的数组')
    if not result['scope']:
        raise DeliveryContractError('scope 需要明确实际完成的研究范围')
    if len(set(result['scope'])) != len(result['scope']):
        raise DeliveryContractError('scope 不能重复')
    if result['completion'] == 'limited' and not result['limitations']:
        raise DeliveryContractError('有限报告需要 limitations 说明具体未完成内容及影响')
    if allowed_scope is not None and set(result['scope']) - set(allowed_scope):
        raise DeliveryContractError('scope 超出本次服务端冻结的研究范围')
    if (required_scope is not None and result['completion'] == 'full' and
            not {'account', 'public'} & set(result['scope']) and set(required_scope) - set(result['scope'])):
        raise DeliveryContractError('完整范围报告仍有未覆盖目标；请限定 scope 并提交 limited 及具体缺口')
    for field in ('findings', 'suggestions', 'forecasts', 'information_impacts'):
        if field in result and (not isinstance(result[field], list) or
                               any(not isinstance(value, dict) for value in result[field])):
            raise DeliveryContractError(f'{field} 需要对象数组')
    for field in ('material_ids', 'uncertainties', 'maintained_suggestion_ids', 'review_suggestion_ids', 'revoked_suggestion_ids'):
        if field in result and (not isinstance(result[field], list) or
                               any(not isinstance(value, str) or not value.strip() for value in result[field])):
            raise DeliveryContractError(f'{field} 需要非空字符串组成的数组')
    if 'covered_demand_ids' in result and (not isinstance(result['covered_demand_ids'], list) or
            any(type(value) is not int or value < 1 for value in result['covered_demand_ids'])):
        raise DeliveryContractError('covered_demand_ids 需要正整数数组')
    if 'valid_until' in result:
        _validate_expiry(result['valid_until'], 'valid_until')
    if len(result.get('suggestions', [])) > 100:
        raise DeliveryContractError('单次 suggestions 最多 100 条')
    validate_research_records(result.get('research_records', []))
    for item in result.get('information_impacts', []):
        if item.get('status') not in ('relevant', 'irrelevant', 'uncertain'):
            raise DeliveryContractError('材料关联 status 不在允许枚举中')
    validate_structured_delivery(result, allowed_scope=allowed_scope, contract_context=contract_context)
    return copy.deepcopy(result)



def validate_structured_delivery(result, *, allowed_scope=None, contract_context=None):
    """One read-only rule source for acceptance and transactional publication.

    Context contains only authorized, server-owned indexes. It deliberately does
    not promise that account versions, deadlines or baseline availability will
    remain unchanged after the preflight.
    """
    context = contract_context
    scope = set(result.get('scope', []))
    allowed = set(allowed_scope) if allowed_scope is not None else None
    lifecycle_fields = ('maintained_suggestion_ids', 'review_suggestion_ids', 'revoked_suggestion_ids')
    if context is not None and not context['account_id']:
        for field in ('suggestions', 'forecasts', *lifecycle_fields):
            if result.get(field):
                raise DeliveryContractError(f'{field}: 公共研究不能发布账户预测或修改账户建议')

    def security(item, path):
        raw = item.get('security_id', item.get('symbol'))
        try:
            sid = symbol(raw) if isinstance(raw, str) else ''
        except ValueError:
            sid = ''
        if not sid or ':' not in sid or sid != raw:
            raise DeliveryContractError(f'{path}.security_id: 需要规范证券身份，例如 SZ:159599')
        if 'security_id' in item and 'symbol' in item and item['symbol'] != sid:
            raise DeliveryContractError(f'{path}.symbol: 与 security_id 冲突')
        if allowed is not None and sid not in allowed:
            raise DeliveryContractError(f'{path}.security_id: 超出本次服务端冻结的研究范围')
        if not ({'account', 'public'} & scope) and sid not in scope:
            raise DeliveryContractError(f'{path}.security_id: 与本次明确交付的 scope 不一致')
        return sid

    recommendations = context.get('recommendations', {}) if context is not None else None
    handled, suggestion_keys = {}, set()
    for field in lifecycle_fields:
        for index, identifier in enumerate(result.get(field, [])):
            path = f'{field}[{index}]'
            if identifier in handled:
                raise DeliveryContractError(f'{path}: 同一建议 ID 重复或与 {handled[identifier]} 冲突')
            handled[identifier] = field
            if recommendations is not None:
                prior = recommendations.get(identifier)
                if not prior:
                    raise DeliveryContractError(f'{path}: 不是当前账户可处理的有效建议 ID')
                security({'security_id': prior['security_id']}, path)
    for index, item in enumerate(result.get('suggestions', [])):
        path = f'suggestions[{index}]'
        sid = security(item, path)
        if item.get('action') not in ('watch', 'buy_candidate', 'add_candidate', 'hold', 'reduce_candidate', 'exit_candidate'):
            raise DeliveryContractError(f'{path}.action: 不在允许枚举中')
        if not isinstance(item.get('basis'), str) or not item['basis'].strip():
            raise DeliveryContractError(f'{path}.basis: 需要非空依据文本')
        for field in ('material_ids', 'agent_conditions'):
            if field in item and (not isinstance(item[field], list) or
                    any(not isinstance(value, str) or not value.strip() for value in item[field])):
                raise DeliveryContractError(f'{path}.{field}: 需要非空字符串数组')
        from .recommendations import price_condition
        if 'valid_until' in item:
            _validate_expiry(item['valid_until'], f'{path}.valid_until')
        if 'price_conditions' in item:
            conditions = item['price_conditions']
            if not isinstance(conditions, list):
                raise DeliveryContractError(f'{path}.price_conditions: 需要价格条件对象数组')
            for condition_index, condition in enumerate(conditions):
                if not isinstance(condition, dict) or price_condition(condition, 0) is None:
                    raise DeliveryContractError(f'{path}.price_conditions[{condition_index}]: 价格条件格式或数值无效；定性条件请放 agent_conditions')
        identifier = item.get('id')
        if 'id' in item and (not isinstance(identifier, str) or not identifier.strip()):
            raise DeliveryContractError(f'{path}.id: 需要当前账户的非空建议 ID；新建请省略')
        key = sid
        if key in suggestion_keys:
            raise DeliveryContractError(f'{path}: 同一建议或证券重复提交；请合并为一个明确动作')
        suggestion_keys.add(key)
        if identifier and identifier in handled:
            raise DeliveryContractError(f'{path}.id: 与 {handled[identifier]} 冲突')
        if not identifier and recommendations is not None:
            for handled_id, field in handled.items():
                # A new suggestion replaces older suggestions for this security.
                # Explicit revoke + replacement is consistent; keeping/reviewing
                # the same old suggestion while replacing it is not.
                if (field != 'revoked_suggestion_ids' and
                        recommendations[handled_id]['security_id'] == sid):
                    raise DeliveryContractError(f'{path}.security_id: 新建议会替代同证券旧建议，与 {field} 冲突')
        if identifier and recommendations is not None:
            prior = recommendations.get(identifier)
            if not prior or prior['security_id'] != sid:
                raise DeliveryContractError(f'{path}.id: 必须属于当前账户的同一证券有效建议')
    forecasts = result.get('forecasts', [])
    if len(forecasts) > 10000:
        raise DeliveryContractError('forecasts: 单次最多 10000 条')
    seen = set()
    for index, item in enumerate(forecasts):
        path = f'forecasts[{index}]'
        sid = security(item, path)
        if set(item) - {'security_id', 'symbol', 'horizon', 'direction', 'reason', 'material_ids', 'citations', 'references', 'revises_id'}:
            raise DeliveryContractError(f'{path}: 身份和时间由服务端确定，含不支持字段')
        if 'material_ids' in item and (not isinstance(item['material_ids'], list) or
                any(not isinstance(value, str) or not value.strip() for value in item['material_ids'])):
            raise DeliveryContractError(f'{path}.material_ids: 需要非空字符串数组')
        horizon = item.get('horizon')
        if type(horizon) is not int or horizon < 1:
            raise DeliveryContractError(f'{path}.horizon: 需要正整数期限')
        if item.get('direction') not in ('up', 'sideways', 'down', 'abstain'):
            raise DeliveryContractError(f'{path}.direction: 不在允许枚举中')
        if not isinstance(item.get('reason'), str) or not item['reason'].strip() or len(item['reason']) > 16000:
            raise DeliveryContractError(f'{path}.reason: 需要非空理由，最多 16000 字符')
        key = (sid, horizon)
        if key in seen:
            raise DeliveryContractError(f'{path}: 同一证券/期限重复或冲突')
        seen.add(key)
        if context is not None:
            if not context.get('forecast_batch_id'):
                raise DeliveryContractError(f'{path}: 当前任务没有服务端冻结的预测批次')
            if key not in context['forecast_targets']:
                raise DeliveryContractError(f'{path}.horizon: 预测不在服务端冻结的批次证券/期限范围内')
            if context.get('restricted_targets') is not None and key not in context['restricted_targets']:
                raise DeliveryContractError(f'{path}: 预测不在本次执行分配的证券/期限子范围内')
            revises = item.get('revises_id')
            if 'revises_id' in item and (not isinstance(revises, str) or not revises.strip()):
                raise DeliveryContractError(f'{path}.revises_id: 需要非空预测 ID；没有修订请省略')
            if revises and context.get('revisable_forecasts', {}).get(revises) != key:
                raise DeliveryContractError(f'{path}.revises_id: 必须为本账户同证券同期限的已有预测')

def bind_delivery(result, *, job_id, account_id, input_version, universe_version, team_id):
    """Called only after validation, using values from the frozen server task."""
    result = copy.deepcopy(result)
    result.pop('demo', None)
    submission = result['submission']
    result['submission'] = {key: submission[key] for key in ('action', 'contract_version', 'call_id')}
    identity = dict(job_id=job_id, account_id=account_id, input_version=input_version,
                    universe_version=universe_version, team_id=team_id)
    result['submission'].update(identity)
    result.update(identity)
    result['delivery'] = {'status': result['completion'], 'contract_verified': True,
                          'content_assessment': 'not_evaluated',
                          **{key: result[key] for key in ('scope', 'decision_summary', 'limitations')}}
    return result


def incomplete_delivery(error_code):
    return {'status': 'incomplete', 'contract_verified': False,
            'content_assessment': 'not_evaluated', 'error_code': error_code}


def bind_submission_observation(result, events):
    """Derive attempt labels from server-recorded events, never model flags."""
    manager = [event for event in events if event.get('agent') == 'manager']
    observed = any(event.get('type') == 'tool_started' and
                   (event.get('payload') or {}).get('tool') == 'finish_analysis' for event in manager)
    protocol_corrections = sum(event.get('type') == 'terminal_submission_rejected' for event in manager)
    reference_corrections = sum(event.get('type') == 'tool_result' and
        (event.get('payload') or {}).get('tool') == 'finish_analysis' and
        isinstance((event.get('payload') or {}).get('result'), dict) and
        (event['payload']['result'].get('accepted') is False and bool(event['payload']['result'].get('reference_feedback')))
        for event in manager)
    corrections = protocol_corrections + reference_corrections if observed else None
    outcome = ('recovered' if corrections else 'first_attempt') if observed else 'unknown'
    result.update(submission_outcome=outcome, submission_corrections=corrections)
    result['submission_feedback_counts'] = {'protocol': protocol_corrections, 'references': reference_corrections} if observed else None
    if isinstance(result.get('delivery'), dict):
        result['delivery'].update(submission_outcome=outcome, submission_corrections=corrections)
    return result


def unapplied_delivery(result, reason):
    """Retain each submitted intent, including malformed failed candidates."""
    def entries(field):
        values = result.get(field, [])
        if isinstance(values, list):
            return list(enumerate(values))
        # Keep a visible receipt for an invalid whole field. The original value
        # remains in the candidate; error handling must not fail or omit it.
        return [(None, values)]

    def items(field):
        output = []
        for index, item in entries(field):
            valid_shape = index is not None and isinstance(item, dict)
            row = {'index': index, 'security_id': item.get('security_id', item.get('symbol')) if valid_shape else None,
                   'status': 'not_applied', 'reason': reason}
            if not valid_shape:
                row['reason'] = f'{reason}；{field}' + (f'[{index}] 需要对象' if index is not None else ' 需要对象数组')
            if field == 'forecasts':
                row['horizon'] = item.get('horizon') if valid_shape else None
            output.append(row)
        return output

    actions = []
    for field in ('maintained_suggestion_ids', 'review_suggestion_ids', 'revoked_suggestion_ids'):
        for index, identifier in entries(field):
            valid_shape = index is not None and isinstance(identifier, str)
            actions.append({'field': field, 'id': identifier if valid_shape else None,
                'status': 'not_applied', 'reason': reason if valid_shape else f'{reason}；{field} 需要非空字符串数组'})
    return {'suggestions': items('suggestions'), 'suggestion_actions': actions, 'forecasts': items('forecasts')}
