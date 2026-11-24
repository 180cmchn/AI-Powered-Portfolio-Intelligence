#!/usr/bin/env python3
"""Opt-in real comparison of request organization, with useful fixed evidence.

Two independent sessions use the selected filing role's unchanged model/effort,
network route and deterministic evidence/calculator tools. Both arms have stable
cache identities: this isolates organization, not every historical client change.
No artificial request-count ceiling, padding, unknown-result replay or transport
injection. This controlled exercise is not the normal-service business matrix.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import json
import math
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.agents import CHILD_TOOLS, RESEARCH_TOOLS
from app.config import timeout_policy
from app.domain import now
from app.model import ModelClient, ModelError, ModelProtocolError, encode
from app.model_network import safe_exception_details
from app.runtime_version import source_digest, env_file_stamp
from app.service import ResearchTools
from scripts.verify_seventeenth_live import configure_isolated, preservation, write


TASK = ('核对天娱数科2026年前三季度营收、归母和扣非的金额与同比方向，分别说明主体、期间、单位、基期，'
        '不要把三项合并成一个正向标签。对同一模拟账户给出的买入和非半仓卖出记录，计算买后成本、'
        '卖出成本结转、剩余总成本、净卖出所得、已实现盈亏和期末现金，准确区分这些术语。'
        '对单独标为合成的20个收盘观察点说明均线、收益区间、价格/均线比与变化率，'
        '并区分两个收益率之差的百分点。最后解释该有限证据能支持什么判断，不能支持什么，'
        '给出模拟账户动作与改变判断所需条件；不强制买卖，不把合成价格当真实行情。')
BOUNDARY = ('这是固定获准证据的调查会话，当前仅可用read_evidence、calculate和submit_report。'
            'read_evidence返回保存原文或明确标注的模拟事实；不联网、不调用未提供工具。'
            '按任务需要自主调查和计算，不为缓存重复阅读或填充文字。已有足够事实就提交，'
            '在summary和findings保留可复算数字、来源及具体限制。未阅读部分不得声称已读。'
            '状态新消息按顺序生效，null预算表示不按次数封顶，完成/截止/重复无进展仍会结束。')


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def evidence_packets(path):
    fixture = json.loads(path.read_text())
    if fixture.get('kind') != 'public_original_pdf_excerpt_fixture':
        raise ValueError('A saved public original-PDF excerpt fixture is required')
    excerpt = fixture['excerpt']
    if hashlib.sha256(excerpt.encode()).hexdigest() != fixture['excerpt_sha256']:
        raise ValueError('Original excerpt hash does not match the fixture')
    financial = {'kind': fixture['kind'], 'source': fixture['source'], 'excerpt': excerpt,
                 'excerpt_sha256': fixture['excerpt_sha256']}
    ledger = {'kind': 'explicitly_synthetic_account_ledger', 'currency': 'CNY',
        'account': {'identity': 'cache-comparison-simulated-account', 'opening_cash': '40000.00', 'opening_holdings': []},
        'events': [
            {'side': 'buy', 'symbol': 'SZ:002354', 'quantity': 1200, 'price': '11.00', 'total_fees': '6.00'},
            {'side': 'sell', 'symbol': 'SZ:002354', 'quantity': 300, 'price': '12.50', 'total_fees': '5.00'}],
        'accounting_method': 'moving average cost; purchase fees enter cost; sale fees reduce sale proceeds',
        'boundary': 'Simulated quantities, cash, prices and fees only; no broker or current market-price assertion.'}
    periods = {'kind': 'explicitly_synthetic_price_observations', 'unit': 'CNY',
        'close_observations': [f'{100 + i * 2}.{i % 2}0' for i in range(20)],
        'observation_order': 'oldest to newest, 20 consecutive trading-session closes, no intraday high/low provided',
        'comparison_returns': {'asset_percent': '3.00', 'benchmark_percent': '1.25'},
        'boundary': 'Separate arithmetic example; not the security current market data and not forecast performance.'}
    return {'financial_excerpt': financial, 'simulated_ledger': ledger, 'period_and_percent': periods}


def tool_definitions():
    read = {'name': 'read_evidence', 'description': '读取本任务固定证据包的一项；实际内容与版本两臂一致，未读取不视为已知。',
        'parameters': {'type': 'object', 'properties': {'packet_id': {'type': 'string'}},
                       'required': ['packet_id'], 'additionalProperties': False}}
    return [read, copy.deepcopy(next(t for t in RESEARCH_TOOLS if t['name'] == 'calculate')),
            copy.deepcopy(next(t for t in CHILD_TOOLS if t['name'] == 'submit_report'))]


def apply_status(session, variant, status):
    if variant == 'legacy_seed_rewrite':
        seed = json.loads(session.history[0]['content'])
        seed['runtime_status'] = copy.deepcopy(status)
        session.history[0]['content'] = encode(seed)
    elif not session.requests:
        seed = json.loads(session.history[0]['content'])
        seed['runtime_status'] = copy.deepcopy(status)
        session.history[0]['content'] = encode(seed)
    else:
        session.history.append({'role': 'user', 'content': encode({'runtime_status': status})})


async def run_variant(record, *, config, system, initial, packets, store, deadline,
                      frozen_source, frozen_environment, save):
    client = ModelClient(config)  # Actual configured transport; never injected.
    session = client.session(system, copy.deepcopy(initial))
    session.agent_id = 'filing-comparison-instance-' + record['variant']
    session.native_search = False
    session.deadlines = {'analysis_budget_exhausted': deadline}
    tools = tool_definitions()
    calculator = ResearchTools(store, {}, config)
    read_ids, previous_progress, repeats, final_required = set(), None, 0, False
    useful_tool_rounds = 0
    seen_tool_outputs = set()
    record.update(session_id=session.session_id, status='running', calls_unlimited=True,
                  requests=client.requests, rounds=[], report=None, content_review='pending')

    async def before_request():
        if source_digest() != frozen_source or env_file_stamp() != frozen_environment:
            raise ModelError('固定源码或环境文件发生变化，停止比较', code='comparison_identity_changed')
    client.before_request = before_request

    async def observe_wire(_request):
        # Request bodies/headers are deliberately not copied to the journal.
        record['wire_attempts'] = record.get('wire_attempts', 0) + 1
        save()
    async def accepted(observation):
        record['last_accepted_request_id'] = observation['local_request_id']
        save()
    client.http.event_hooks['request'] = [observe_wire]
    session.on_request_accepted = accepted
    started = time.monotonic()
    try:
        while True:
            remaining = deadline - time.monotonic()
            reserve = min(timeout_policy(config, 'synthesis')['request_hard_seconds'],
                          float(config.get('analysis_timeout_seconds', 3600)) * .25)
            final_required = final_required or remaining <= reserve
            session.phase = 'synthesis' if final_required else 'research'
            status = {'model_calls_used': len(client.requests), 'remaining_shared_model_calls': None,
                'remaining_agent_turns': None, 'evidence_packets_read': sorted(read_ids),
                'final_submission_required': final_required}
            apply_status(session, record['variant'], status)
            selected = [tool for tool in tools if tool['name'] == 'submit_report'] if final_required else tools
            turn = await client.complete(session, selected, tool_choice='submit_report' if final_required else None)
            item = {'round': len(record['rounds']) + 1, 'visible_text': turn.text,
                'calls': [{'name': call.name, 'arguments': call.arguments, 'error': call.error} for call in turn.calls]}
            record['rounds'].append(item)
            submit = [call for call in turn.calls if call.name == 'submit_report']
            if submit:
                if len(turn.calls) != 1 or submit[0].error or not isinstance(submit[0].arguments.get('summary'), str) or not submit[0].arguments['summary'].strip():
                    raise ModelProtocolError('固定证据比较未获得唯一完整的submit_report，保留实际尝试')
                record.update(report=copy.deepcopy(submit[0].arguments), status='completed',
                    delivery_contract='single_nonempty_submit_report',
                    unrequested_packets=sorted(set(packets) - read_ids))
                client.add_results(session, [(submit[0], {'accepted': True})])
                break
            if not turn.calls:
                if final_required:
                    raise ModelProtocolError('比较会话终止阶段未提交报告')
                final_required = True
                client.add_user(session, {'submission_feedback': '普通文本不是提交；已有结论请通过submit_report结束，并写明缺口。'})
                save()
                continue
            results = []
            available_tools = {tool['name'] for tool in selected}
            for call in turn.calls:
                if call.error:
                    result = {'error': call.error}
                elif call.name not in available_tools:
                    result = {'error': 'Tool not available in this stage; submit the existing evidence'}
                elif call.name == 'read_evidence':
                    key = call.arguments.get('packet_id')
                    result = copy.deepcopy(packets.get(key, {'error': 'Unknown packet_id'}))
                    if key in packets:
                        read_ids.add(key)
                elif call.name == 'calculate':
                    try:
                        result = await calculator.calculate(**call.arguments)
                    except TypeError:
                        result = {'error': 'calculate requires operation and values only'}
                else:
                    result = {'error': 'Tool not available in this fixed-evidence scope'}
                results.append((call, result))
            client.add_results(session, results)
            item['results'] = [{'tool': call.name, 'result': result} for call, result in results]
            outputs = {digest([call.name, call.arguments, result]) for call, result in results if not result.get('error')}
            if outputs - seen_tool_outputs:
                useful_tool_rounds += 1
            seen_tool_outputs.update(outputs)
            progress = digest(item['results'])
            repeats = repeats + 1 if progress == previous_progress else 0
            previous_progress = progress
            if repeats >= 2:
                final_required = True
                client.add_user(session, {'progress_boundary': '相同工具输入/结果连续没有新增事实，依据已得证据提交并说明缺口。'})
            save()
    except BaseException as exc:
        record.update(status='cancelled' if isinstance(exc, asyncio.CancelledError) else 'failed',
            error_code=getattr(exc, 'code', 'comparison_exception'),
            exception_chain=safe_exception_details(exc, secret=client.key))
        raise
    finally:
        record.update(duration_seconds=round(time.monotonic() - started, 3),
            natural_continuations=max(0, len(record['rounds']) - 1),
            model_usage=client.usage_summary(client.requests),
            useful_tool_rounds=useful_tool_rounds,
            three_useful_continuations_observed=useful_tool_rounds >= 3 and len(record['rounds']) >= 4,
            cache_field_forwarding='unconfirmed', cost_amount=None, cost_unit=None)
        save()
        await client.aclose(source='configuration_probe')


async def compare(args):
    if not args.run_real:
        raise ValueError('Real paid execution requires --run-real under the existing iteration authorization')
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    output.chmod(0o700)
    packets = evidence_packets(args.evidence)
    store, settings = configure_isolated(args.source, output / 'isolated-config.sqlite3', args.reference_account,
        route=getattr(args, 'route', None))
    team = store.freeze_agent_team()
    config = store.runtime_role_config(team, args.role, settings)
    config.update(max_model_calls=None, max_agent_turns=None, synthesis_max_calls=None)
    system = team['roles'][args.role]['system_prompt'] + '\n\n' + BOUNDARY
    initial = {'kind': 'fixed_evidence_organization_comparison', 'task': TASK,
        'packets': [{'packet_id': key, 'kind': value['kind']} for key, value in packets.items()],
        'boundary': 'Public saved excerpt plus independent simulated account; no live order or daily account copied.'}
    summary = {'kind': initial['kind'], 'started_at': now(), 'status': 'running',
        'source_sha256': source_digest(), 'environment_stamp': env_file_stamp(),
        'commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
        'requested_configuration': {key: config.get(key) for key in ('model_base_url', 'model_name', 'model_protocol',
            'reasoning_effort', 'max_output_tokens', 'model_network_route', 'model_retries', 'analysis_timeout_seconds')},
        'role': args.role, 'system_sha256': digest(system), 'initial_sha256': digest(initial),
        'route_selection': getattr(args, 'route', None) or 'preserve_source',
        'tool_schema_sha256': digest(tool_definitions()), 'evidence_sha256': digest(packets),
        'counts': {'max_model_calls': None, 'max_agent_turns': None, 'synthesis_max_calls': None},
        'comparison_limits': ['Sequential independent sessions, not randomized causal attribution',
            'Model outputs/tool choices may differ; factual tool results and target are fixed',
            'Both arms have stable session cache hints; legacy arm emulates first-message rewriting only',
            'HTTP acceptance does not prove cache-hint forwarding; actual usage is the cache evidence',
            'Normal production-service research and financial content review are separate acceptance gates'],
        'variants': [], 'before_preservation': preservation({'source': args.source,
            'source_wal': Path(str(args.source) + '-wal'), 'source_shm': Path(str(args.source) + '-shm'), 'env': ROOT / '.env'})}
    def save():
        write(output / 'summary.json', summary)
    write(output / 'fixed-evidence.json', packets)
    save()
    duration = args.seconds_per_session or float(config.get('analysis_timeout_seconds', 3600))
    try:
        for variant in ('legacy_seed_rewrite', 'append_only'):
            record = {'variant': variant, 'started_at': now()}
            summary['variants'].append(record)
            await run_variant(record, config=config, system=system, initial=initial, packets=packets,
                store=store, deadline=time.monotonic() + duration, frozen_source=summary['source_sha256'],
                frozen_environment=summary['environment_stamp'], save=save)
        summary['status'] = 'completed_pending_content_review'
    except BaseException as exc:
        summary.update(status='cancelled' if isinstance(exc, asyncio.CancelledError) else 'stopped_after_failure',
            remaining_variants_not_run=[item for item in ('legacy_seed_rewrite', 'append_only')
                if item not in {v['variant'] for v in summary['variants']}])
        # Any failure stops this experiment; never overlap an unknown request
        # with the other arm or repeat it to improve the success denominator.
        if isinstance(exc, (KeyboardInterrupt, asyncio.CancelledError)):
            raise
    finally:
        summary['finished_at'] = now()
        summary['after_preservation'] = preservation({'source': args.source,
            'source_wal': Path(str(args.source) + '-wal'), 'source_shm': Path(str(args.source) + '-shm'), 'env': ROOT / '.env'})
        summary['all_model_usage'] = ModelClient.usage_summary([
            request for variant in summary['variants'] for request in variant.get('requests', [])])
        save()
    return 0 if summary['status'] == 'completed_pending_content_review' else 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-real', action='store_true')
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--reference-account', required=True)
    parser.add_argument('--role', default='filing_researcher')
    parser.add_argument('--route', choices=('environment', 'direct'),
        help='Optional model-only route for both arms; default preserves saved source route')
    parser.add_argument('--evidence', type=Path, default=ROOT / 'tests/fixtures/seventeenth-financial-signs.json')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--seconds-per-session', type=float)
    args = parser.parse_args()
    if args.seconds_per_session is not None and (not math.isfinite(args.seconds_per_session) or args.seconds_per_session <= 0):
        parser.error('--seconds-per-session must be positive')
    return asyncio.run(compare(args))


if __name__ == '__main__':
    raise SystemExit(main())
