#!/usr/bin/env python3
"""Layered delivery review and bounded final-only endpoint comparison.

Default allowance is zero. A comparison requires explicit --run-real,
--authorized-http-attempts and --authorization-note; those flags record an
existing user authorization, they do not grant one. Never writes daily reports
or predictions. Full research must wait until final-only evidence is reviewed.
"""
from __future__ import annotations
import argparse
import asyncio
from collections import Counter
import hashlib
import json
from pathlib import Path
import subprocess
import sqlite3
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def configure_isolated(source, target, account_id):
    """Copy only effective model bindings, then migrate exact old built-ins locally."""
    from scripts.verify_eleventh_live import configure
    store, cfg = configure(source, target, account_id)
    with sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True) as origin, store.connect() as dest:
        for key, value in origin.execute("SELECT key,value FROM meta WHERE key LIKE 'revoked_provider_credentials:%'"):
            dest.execute('INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)', (key, value))
        # Store initialized an empty target before the source roles were copied.
        # Re-run only this exact-hash migration on the copied isolated roles;
        # customized prompts and all bindings remain unchanged.
        dest.execute("DELETE FROM meta WHERE key='fourteenth_role_migration'")
        store.migrate_fourteenth_roles(dest)
    return store, cfg


def comparison_seed(package):
    """Explicit equal-input projection; originals stay in the private package."""
    from scripts.verify_fourteenth_evidence import selected
    seed = {key: package[key] for key in ('kind', 'historical_task_id', 'historical_checkpoint_at',
        'data_as_of', 'input_version', 'account', 'metrics', 'reports', 'materials',
        'open_questions', 'unresolved_tasks', 'boundary') if key in package}
    seed['calculations'] = [selected(item, ('calculation_id', 'tool', 'arguments', 'summary',
        'agent_id', 'source_task_id', 'input_version')) for item in package.get('calculations', [])]
    seed['source_access_note'] = ('报告全文和成功计算摘要在本次输入中可见。材料列表仅为来源及访问类型索引，'
        '历史逐次材料访问回执完整保存在私有包中，没有将其原始输出再次装入本请求；'
        '主 Agent 可沿实际收到的子报告来源链综合，不得声称本次亲读全部原文。')
    return seed


def acceptance_layers(summary, content_review=None):
    """Structure and human content review are distinct, including unknown states."""
    reports = summary.get('final_reports', [])
    report = next((item for item in reports if item.get('job_id') == summary.get('released_job_id')), None)
    result = (report or {}).get('result') or {}
    delivery = result.get('delivery') or {}
    requests = [request for attempt in summary.get('all_attempts_after_shutdown', [])
                for request in attempt.get('requests', [])]
    complete = sum(not r.get('error') and (r.get('stream_terminal') in ('response.completed', '[DONE]')
                   or r.get('response_status') == 'completed'
                   or r.get('protocol_terminal') in ('responses.completed', 'chat.stop', 'chat.tool_calls')) for r in requests)
    content = {'status': 'not_evaluated', 'accepted': None}
    if content_review is not None:
        # A reviewer must bind the actual body, record a reviewer and inspect
        # each semantic item. No structural check is relabelled human review.
        expected = hashlib.sha256(result.get('report', '').encode()).hexdigest()
        required = ('account_facts', 'answers_scope', 'basis_and_counter_factors',
                    'conditions', 'limitations')
        if (content_review.get('kind') in ('manual_content_review', 'evidence_content_review')
                and content_review.get('report_sha256') == expected
                and content_review.get('reviewer') and content_review.get('reviewed_at')
                and all(content_review.get('checks', {}).get(key) in ('pass', 'fail', 'not_applicable')
                        for key in required)):
            content = {**content_review, 'status': 'evaluated', 'accepted':
                       all(content_review['checks'][key] != 'fail' for key in required)}
        else:
            content['invalid_review'] = 'Missing body identity, reviewer, time or explicit item results'
    contract = delivery.get('contract_verified') is True
    published = bool(report and report.get('status') == 'current' and summary.get('report_published'))
    coverage = summary.get('forecast_coverage')
    mechanical = summary.get('mechanical_single_report_acceptance_passed',
                             summary.get('single_report_acceptance_passed', False))
    http_complete = sum(r.get('http_body_complete') is True for r in requests)
    http_unknown = sum('http_body_complete' not in r for r in requests)
    return {'transport': {'http_attempts': len(requests), 'complete_terminals': complete,
                         'all_complete': bool(requests) and complete == len(requests),
                         'http_bodies_complete': http_complete,
                         'http_body_completion_unknown': http_unknown,
                         'all_http_bodies_complete': bool(requests) and http_complete == len(requests)},
            'delivery_contract': {'satisfied': contract, 'status': delivery.get('status', 'legacy_or_missing')},
            'publication': {'published': published, 'mechanical_checks_passed': bool(mechanical)},
            'content': content,
            'forecast_coverage': coverage if coverage is not None else {'status': 'not_evaluated'},
            'forecast_effect': {'status': 'not_evaluated', 'note': 'Requires naturally matured recorded forecasts'},
            'business_tasks': {'attempted': summary.get('business_jobs_with_http', 0),
                               'content_accepted': int(contract and published and content['accepted'] is True)},
            'overall_live_acceptance_passed': bool(contract and published and mechanical and content['accepted'] is True)}


async def compare(args):
    from app.agents import MANAGER_TOOLS
    from app.domain import now
    from app.model import ModelClient
    from app.runtime_version import source_digest, env_file_stamp
    from scripts.verify_thirteenth_live import AttemptBudget
    from scripts.verify_fourteenth_evidence import digest
    package = json.loads(args.evidence.read_text())
    if package.get('kind') not in ('real_historical_visible_evidence', 'controlled_fixture'):
        raise ValueError('Explicit evidence provenance is required')
    seed = comparison_seed(package)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    output.chmod(0o700)
    store, cfg = configure_isolated(args.source, output / 'isolated-config.sqlite3', args.reference_account)
    team = store.freeze_agent_team()
    cfg = {**store.runtime_role_config(team, 'manager', cfg), '_agent_team': team}
    allowed = min(args.authorized_http_attempts, int(cfg.get('max_model_calls', 40)))
    budget = AttemptBudget(output / 'attempt-budget.sqlite3', allowed)
    global_budget = None
    if getattr(args, 'global_budget', None):
        from scripts.verify_fourteenth_budget import SharedAttemptBudget
        global_budget = SharedAttemptBudget(args.global_budget)
    async def reserve():
        # The stage cap is checked before reserving the shared total. The total
        # and stage limits themselves are atomic in the shared SQLite.
        if budget.status()['reserved_attempts'] >= allowed:
            from app.model import ModelBudgetExceeded
            raise ModelBudgetExceeded('固定证据对照本次授权额度耗尽')
        if global_budget:
            global_budget.reserve('final')
        budget.reserve()
    client = ModelClient(cfg, before_request=reserve)
    tools = [tool for tool in MANAGER_TOOLS if tool['name'] == 'finish_analysis']
    # Both branches receive exactly the same data and instructions. The only
    # treatment variable is explicit final tool selection.
    prompt = ('根据固定历史证据包交付账户判断。按原时点分析，不称为当日实盘。'
              '回答现金与敞口、逐项动作、依据、主要反向因素及改变判断条件；缺口局部说明。'
              '不新增研究，不编造价格或预测。用 finish_analysis 提交当前证据支持的完整或有限报告。')
    summary = {'kind': 'final_only_fixed_evidence_comparison', 'started_at': now(),
        'authorization_note': args.authorization_note, 'authorized_http_attempts': allowed,
        'package_sha256': digest(package), 'input_sha256': digest(seed), 'input_characters': len(json.dumps(seed, ensure_ascii=False)), 'package_kind': package['kind'],
        'source_sha256': source_digest(), 'environment_stamp': env_file_stamp(),
        'commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(),
        'protocol': cfg['model_protocol'], 'model_requested': cfg['model_name'],
        'effort_requested': cfg['reasoning_effort'], 'variants': [],
        'formal_predictions_written': 0, 'daily_database_writes': 0,
        'content_assessment': 'not_evaluated', 'fee_amount': None}
    def save():
        summary['attempt_budget'] = budget.status()
        if global_budget:
            summary['global_attempt_budget'] = global_budget.status()
        (output / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n')
        (output / 'summary.json').chmod(0o600)
    save()
    observed_record = None
    async def observe_actual_request(request):
        payload = json.loads(request.content)
        actual = payload.get('input') if cfg['model_protocol'] == 'responses' else payload.get('messages', [])[1:]
        actual_seed = json.loads(actual[0]['content'])
        evidence = {'request_payload_sha256': digest(payload), 'actual_input_sha256': digest(actual_seed),
            'identical_fixed_input': actual_seed == seed,
            'full_reports_visible': all(report == seen for report, seen in zip(seed.get('reports', []), actual_seed.get('reports', []))) and len(actual_seed.get('reports', [])) == len(seed.get('reports', [])),
            'calculation_summaries_visible': actual_seed.get('calculations') == seed.get('calculations')}
        if observed_record is not None:
            observed_record.setdefault('actual_request_inputs', []).append(evidence)
        if not evidence['identical_fixed_input']:
            raise ValueError('Actual final input differs from the fixed evidence; do not send a truncated comparison')
    client.http.event_hooks['request'].append(observe_actual_request)
    comparison_deadline = time.monotonic() + float(cfg['analysis_timeout_seconds'])
    try:
        for mode in ('single_tool_unforced', 'named_finish_analysis'):
            if budget.status()['reserved_attempts'] >= allowed:
                summary['not_run'] = mode + ': authorized HTTP attempts exhausted'
                break
            # The final-only harness sends the reviewed bounded projection as
            # one explicit seed. session()'s general one-third seed compaction
            # would otherwise silently retain only a prefix without recall tools.
            session = client.session(prompt, {})
            session.history[:] = [{'role': 'user', 'content': json.dumps(seed, ensure_ascii=False)}]
            session.native_search = False
            session.phase = 'synthesis'
            session.deadlines = {'analysis_budget_exhausted': comparison_deadline}
            started = time.monotonic()
            record = {'variant': mode, 'input_sha256': digest(seed), 'started_at': now()}
            summary['variants'].append(record)
            observed_record = record
            try:
                turn = await client.complete(session, tools, tool_choice='finish_analysis' if mode == 'named_finish_analysis' else None)
                record.update(visible_text=turn.text, tool_calls=[{'name': c.name, 'arguments': c.arguments,
                    'error': c.error} for c in turn.calls], tool_choice_status=turn.tool_choice_status,
                    terminal_submission_present=len(turn.calls) == 1 and turn.calls[0].name == 'finish_analysis' and not turn.calls[0].error)
                if record['terminal_submission_present']:
                    from app.delivery import validate_final_result
                    candidate = {**turn.calls[0].arguments, 'submission': {'action': 'finish_analysis', 'contract_version': 1, 'call_id': turn.calls[0].id}}
                    try:
                        validate_final_result(candidate, allowed_scope={'account', *[h['symbol'] for h in package.get('account', {}).get('holdings', [])]})
                        record['delivery_contract_satisfied'] = True
                    except Exception as exc:
                        record.update(delivery_contract_satisfied=False, contract_error_code=getattr(exc, 'code', type(exc).__name__))
                else:
                    record['delivery_contract_satisfied'] = False
            except Exception as exc:
                record.update(error_code=getattr(exc, 'code', type(exc).__name__), error=str(exc))
            finally:
                record.update(elapsed_seconds=round(time.monotonic()-started, 3), requests=session.requests)
                save()
            # Never spend a further full research round after a failed minimal
            # named submission. This CLI intentionally has no full-research mode.
    finally:
        await client.aclose()
        summary.update(completed_at=now(), final_source_unchanged=source_digest()==summary['source_sha256'],
                       final_environment_unchanged=env_file_stamp()==summary['environment_stamp'])
        save()
    named = next((v for v in summary['variants'] if v['variant'] == 'named_finish_analysis'), {})
    return 0 if (named.get('delivery_contract_satisfied') is True and named.get('tool_choice_status') == 'observed'
                 and summary['final_source_unchanged'] and summary['final_environment_unchanged']) else 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--summary', type=Path, help='Review an existing run without model calls')
    parser.add_argument('--content-review', type=Path)
    parser.add_argument('--run-real', action='store_true')
    parser.add_argument('--authorized-http-attempts', type=int, default=0)
    parser.add_argument('--authorization-note')
    parser.add_argument('--source', type=Path)
    parser.add_argument('--reference-account')
    parser.add_argument('--evidence', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--global-budget', type=Path, help='Existing shared 44-attempt authorization ledger')
    args = parser.parse_args()
    if args.summary:
        review = json.loads(args.content_review.read_text()) if args.content_review else None
        print(json.dumps(acceptance_layers(json.loads(args.summary.read_text()), review), ensure_ascii=False, indent=2))
        return
    if not (args.run_real and args.authorized_http_attempts > 0 and args.authorization_note
            and args.source and args.reference_account and args.evidence and args.output):
        parser.error('Default allowance is 0. An existing explicit authorization, positive cap, source account, evidence and NEW output are required')
    raise SystemExit(asyncio.run(compare(args)))

if __name__ == '__main__':
    main()
