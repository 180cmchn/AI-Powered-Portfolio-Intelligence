"""Explicit synthetic submissions for pre-contract persistence fixtures.

These helpers create controlled test evidence only. They do not judge report
semantics, patch production validation, or convert ordinary model messages.
"""
from __future__ import annotations

import uuid


def delivery_arguments(report, *, scope=('account',), completion='limited',
                       decision_summary=None,
                       limitations=('受控工程夹具，不代表真实投资内容验收。',), **extra):
    return {'report': report, 'scope': list(scope), 'completion': completion,
            'decision_summary': decision_summary if decision_summary is not None else report,
            'limitations': list(limitations), **extra}


def final_result(result, *, scope=('account',)):
    """Synthetic runtime receipt for direct Store / Service runner fixtures."""
    result = dict(result)
    report = result.pop('report', None)
    return {**delivery_arguments(report, scope=scope), **result,
            'submission': {'action': 'finish_analysis', 'contract_version': 1,
                           'call_id': 'controlled-' + uuid.uuid4().hex}}


def runner_result(result, context):
    """Service test-double return that explicitly models a completed tool call."""
    target = (context.get('target_security') or {}).get('symbol')
    scope = ('public',) if context.get('scope') == 'global' else (target,) if target else ('account',)
    return final_result(result, scope=scope)


def publish_fixture(store, job_id, result, data_as_of):
    job = store.job(job_id)
    target = job.get('target_security')
    return store.publish(job_id, final_result(result, scope=(target,) if target else ('account',)), data_as_of)


def finish_global_fixture(store, run_id, state, result=None, error=None):
    if state == 'completed':
        result = final_result(result or {}, scope=('public',))
    return store.finish_global(run_id, state, result, error)
