#!/usr/bin/env python3
"""Opt-in, at most two final-only transport diagnostics; never a research run.

Environment proxy routing and an explicit direct transport receive identical
controlled cash-only input, prompt and minimal finish_analysis schema. Direct
transport keeps HTTPX TLS verification/environment certificate settings and
changes no global environment. No old 44-attempt authorization is reused.
"""
from __future__ import annotations
import argparse
import asyncio
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import httpx
from app.agents import MANAGER_TOOLS
from app.delivery import validate_final_result
from app.domain import now
from app.model import ModelClient, ModelError
from app.runtime_version import env_file_stamp, source_digest
from scripts.verify_fourteenth_budget import SharedAttemptBudget
from scripts.verify_fourteenth_live import configure_isolated

INPUT = {'kind': 'controlled_cash_only_transport_probe', 'research_scope': 'cash_only',
         'account': {'cash': '100.00', 'holdings': []},
         'boundary': 'Explicit simulated facts for transport diagnosis, not a real account or current investment research.'}
PROMPT = ('这是提交协议与传输诊断，受控事实只有现金100元、无持仓，不是用户实盘账户。'
          '不要联网、创建专题、预测或交易。只调用一次finish_analysis，用几句中文给出适用于这笔模拟现金的判断及重新评估条件；'
          '不假定资金用途。scope填写cash_only，completion选择full或limited，limited时说明具体缺口。')
_ENV_NAMES = ('HTTPS_PROXY', 'HTTP_PROXY', 'ALL_PROXY', 'NO_PROXY', 'https_proxy',
              'http_proxy', 'all_proxy', 'no_proxy', 'SSL_CERT_FILE', 'SSL_CERT_DIR')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':')).encode()).hexdigest()


def terminal_tool():
    # Deliberately the existing business contract's five basic fields. This
    # minimal capability probe does not certify the complete research tool set.
    tool = copy.deepcopy(next(t for t in MANAGER_TOOLS if t['name'] == 'finish_analysis'))
    fields = ('report', 'completion', 'scope', 'decision_summary', 'limitations')
    tool['parameters'] = {'type': 'object', 'properties': {
        key: tool['parameters']['properties'][key] for key in fields},
        'required': list(fields), 'additionalProperties': False}
    return tool


def check_authorization(args):
    if (not getattr(args, 'run_real', False) or
            type(getattr(args, 'authorized_http_attempts', None)) is not int or
            not 1 <= args.authorized_http_attempts <= 2 or
            not str(getattr(args, 'authorization_note', '') or '').strip() or
            not getattr(args, 'source', None) or not getattr(args, 'reference_account', None) or
            not getattr(args, 'output', None)):
        raise ValueError('Default allowance is 0; existing explicit authorization, 1-2 HTTP attempts, source, account and NEW output are required')
    if not args.source.is_file():
        raise ValueError('Read-only source database must already exist')


async def probe(args):
    check_authorization(args)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    output.chmod(0o700)
    budget = SharedAttemptBudget.initialize(output/'probe-attempts.sqlite3',
        maximum=args.authorized_http_attempts, final_maximum=args.authorized_http_attempts, full_maximum=0)
    environment = {key: os.environ.get(key) for key in _ENV_NAMES}  # Values never serialized.
    summary = {'kind': 'controlled_cash_only_transport_probe', 'started_at': now(),
        'authorization_note': args.authorization_note, 'authorized_http_attempts': args.authorized_http_attempts,
        'source_sha256': source_digest(), 'environment_stamp': env_file_stamp(),
        'commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
        'environment_variable_presence': {key: bool(value) for key, value in environment.items()},
        'httpx_version': httpx.__version__, 'input': INPUT, 'input_sha256': digest(INPUT),
        'prompt_sha256': digest(PROMPT), 'tools_sha256': digest([terminal_tool()]),
        'prior_44_attempt_ledger_touched': False, 'daily_database_access': 'mode=ro via configure_isolated',
        'daily_database_writes': 0, 'daily_service_restarted': False,
        'reports_published': 0, 'predictions_published': 0, 'content_assessment': 'not_evaluated',
        'fee_amount': None, 'variants': [],
        'scope_note': 'Two independent transport observations only. HTTP/terminal/contract are separate; no complete account research or semantic content acceptance is claimed.'}
    def save():
        summary['attempt_budget'] = budget.status()
        path = output/'summary.json'
        path.write_text(json.dumps(summary, ensure_ascii=False, indent=2)+'\n')
        path.chmod(0o600)
    save()
    try:
        store, config = configure_isolated(args.source, output/'isolated-config.sqlite3', args.reference_account)
        team = store.freeze_agent_team()
        cfg = store.runtime_role_config(team, 'manager', config)
        cfg = {**cfg, 'model_retries': 0}  # One HTTP attempt per route, never blind replay.
        summary['requested_configuration'] = {key: cfg.get(key) for key in (
            'model_base_url', 'model_protocol', 'model_name', 'reasoning_effort', 'max_output_tokens',
            'model_responses_stream', 'analysis_timeout_seconds', 'synthesis_request_hard_timeout_seconds')}
        summary['model_retries'] = 0
        summary['isolated_prompt_migration'] = store.meta('fourteenth_role_migration')
        deadline = time.monotonic() + float(cfg['analysis_timeout_seconds'])
        for route in ('environment', 'direct'):
            if budget.status()['reserved_attempts'] >= args.authorized_http_attempts:
                summary['not_run'] = {'variant': route, 'reason': 'authorized_http_attempts_exhausted'}
                break
            if source_digest() != summary['source_sha256'] or env_file_stamp() != summary['environment_stamp']:
                summary['not_run'] = {'variant': route, 'reason': 'source_or_environment_changed'}
                break
            record = {'variant': route, 'started_at': now(), 'requests': [],
                'routing': 'HTTPX environment proxy routing' if route == 'environment' else
                           'Explicit AsyncHTTPTransport: no environment proxy routing; TLS verify/environment certificates retained',
                'delivery_contract_satisfied': False, 'content_assessment': 'not_evaluated'}
            summary['variants'].append(record)
            save()
            client = session = transport = None
            started = time.monotonic()
            async def reserve():
                budget.reserve('final')
            try:
                # Supplying transport disables only AsyncClient's environment
                # proxy mounts. AsyncHTTPTransport defaults verify/trust_env=True.
                transport = httpx.AsyncHTTPTransport() if route == 'direct' else None
                client = ModelClient(cfg, before_request=reserve, transport=transport)
                session = client.session(PROMPT, copy.deepcopy(INPUT))
                session.native_search, session.phase, session.agent_id = False, 'synthesis', 'manager'
                session.deadlines = {'analysis_budget_exhausted': deadline}
                async def observe_request(request):
                    payload = json.loads(request.content)
                    input_items = payload.get('input') if cfg['model_protocol'] == 'responses' else payload['messages'][1:]
                    record['actual_request'] = {'payload_sha256': digest(payload),
                        'input_sha256': digest(json.loads(input_items[0]['content'])),
                        'tool_choice': payload.get('tool_choice'), 'model': payload.get('model'),
                        'effort': (payload.get('reasoning') or {}).get('effort') if cfg['model_protocol'] == 'responses' else payload.get('reasoning_effort'),
                        'output_limit': payload.get('max_output_tokens', payload.get('max_completion_tokens', payload.get('max_tokens')))}
                    if record['actual_request']['input_sha256'] != summary['input_sha256']:
                        raise ValueError('Controlled input changed before HTTP')
                client.http.event_hooks['request'].append(observe_request)
                turn = await client.complete(session, [terminal_tool()], tool_choice='finish_analysis')
                record.update(visible_text=turn.text, tool_choice_status=turn.tool_choice_status,
                    tool_calls=[{'id': c.id, 'name': c.name, 'arguments': c.arguments, 'error': c.error} for c in turn.calls])
                if len(turn.calls) == 1 and turn.calls[0].name == 'finish_analysis' and not turn.calls[0].error:
                    call = turn.calls[0]
                    candidate = {**call.arguments, 'submission': {'action': 'finish_analysis', 'contract_version': 1, 'call_id': call.id}}
                    validate_final_result(candidate, allowed_scope={'cash_only'})
                    record['delivery_contract_satisfied'] = True
            except Exception as exc:
                record.update(error_code=getattr(exc, 'code', type(exc).__name__), error_type=type(exc).__name__,
                              error=str(exc) if isinstance(exc, ModelError) else 'Probe did not produce a complete validated result')
            finally:
                record['requests'] = session.requests if session else (client.requests if client else [])
                if client:
                    await client.aclose()
                elif transport:
                    await transport.aclose()
                record['elapsed_seconds'] = round(time.monotonic()-started, 3)
                save()
        summary['probe_sequence_finished'] = True
    except Exception as exc:
        summary.update(error_code=getattr(exc, 'code', type(exc).__name__), error_type=type(exc).__name__,
                       error=str(exc) if isinstance(exc, ModelError) else 'Probe configuration or setup failed; no automatic retry')
    finally:
        summary.update(completed_at=now(), final_source_unchanged=source_digest()==summary['source_sha256'],
            final_environment_unchanged=env_file_stamp()==summary['environment_stamp'],
            proxy_and_certificate_environment_unchanged=environment=={key: os.environ.get(key) for key in _ENV_NAMES})
        summary['observed_http_attempts'] = sum(len(v['requests']) for v in summary['variants'])
        summary['reserved_minus_observed'] = budget.status()['reserved_attempts']-summary['observed_http_attempts']
        save()
    # Zero means both authorized routes delivered structurally, not content
    # approval. Partial execution or failure remains an explicit diagnostic 2.
    return 0 if (len(summary['variants']) == 2 and all(v['delivery_contract_satisfied'] for v in summary['variants'])
                 and summary['final_source_unchanged'] and summary['final_environment_unchanged']
                 and summary['proxy_and_certificate_environment_unchanged']) else 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-real', action='store_true')
    parser.add_argument('--authorized-http-attempts', type=int, default=0)
    parser.add_argument('--authorization-note')
    parser.add_argument('--source', type=Path)
    parser.add_argument('--reference-account')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    try:
        check_authorization(args)
    except ValueError as exc:
        parser.error(str(exc))
    raise SystemExit(asyncio.run(probe(args)))

if __name__ == '__main__':
    main()
