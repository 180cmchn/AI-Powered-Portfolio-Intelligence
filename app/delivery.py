"""Small final-submission contract, not an investment-content quality judge.

The model authors the decision and its scope. Runtime submission provenance and
publication identity are server-owned. Historical reports are never migrated by
this validator; it applies only to new publication attempts.
"""
from __future__ import annotations

import copy
from .model import ModelError


class DeliveryContractError(ValueError, ModelError):
    code = 'delivery_contract_failed'


class FinalSubmissionMissing(DeliveryContractError):
    code = 'final_submission_missing'


def validate_final_result(result, *, allowed_scope=None, required_scope=None):
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
    for field in ('findings', 'suggestions', 'research_records', 'forecasts', 'information_impacts'):
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
    if len(result.get('suggestions', [])) > 100:
        raise DeliveryContractError('单次 suggestions 最多 100 条')
    for item in result.get('suggestions', []):
        if item.get('action') not in ('watch', 'buy_candidate', 'add_candidate', 'hold', 'reduce_candidate', 'exit_candidate'):
            raise DeliveryContractError('建议 action 不在允许枚举中')
    for item in result.get('research_records', []):
        if item.get('kind') not in ('market', 'sector', 'candidate'):
            raise DeliveryContractError('研究记录 kind 不在允许枚举中')
    for item in result.get('information_impacts', []):
        if item.get('status') not in ('relevant', 'irrelevant', 'uncertain'):
            raise DeliveryContractError('材料关联 status 不在允许枚举中')
    for item in result.get('forecasts', []):
        if (not isinstance(item.get('security_id', item.get('symbol')), str) or
                type(item.get('horizon')) is not int or item['horizon'] < 1 or
                item.get('direction') not in ('up', 'sideways', 'down', 'abstain') or
                not isinstance(item.get('reason'), str) or not item['reason'].strip()):
            raise DeliveryContractError('预测需要证券、正整数期限、有效方向与理由')
    return copy.deepcopy(result)


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
