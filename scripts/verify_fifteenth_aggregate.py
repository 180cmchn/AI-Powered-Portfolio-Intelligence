#!/usr/bin/env python3
"""Prepare a private historical aggregate; send only with fresh bounded approval.

This is historical final-only synthesis, not fresh market research. Preparation
is offline. Execution uses the real Agent/ModelClient/preflight/publication path,
with at most one initial submission and one correction, and no HTTP retries.
"""
from __future__ import annotations
import argparse
import asyncio
import copy
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.context import compact_calculation_metadata
from scripts.verify_fourteenth_evidence import digest
from scripts.verify_fourteenth_live import comparison_seed, configure_isolated


def write_private(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    path.chmod(0o600)


def compact_seed(package):
    """Lossless calculation metadata factoring, keeping both reports verbatim."""
    seed = copy.deepcopy(comparison_seed(package))
    summaries, contexts = compact_calculation_metadata([item['summary'] for item in seed['calculations']])
    for calculation, summary in zip(seed['calculations'], summaries):
        calculation['summary'] = summary
        # Exact duplicate outer identity is redundant, but divergent attribution
        # remains visible instead of silently choosing one version.
        for key in ('calculation_id', 'tool', 'agent_id', 'source_task_id', 'input_version'):
            if calculation.get(key) == summary.get(key):
                calculation.pop(key, None)
    seed['calculation_contexts'] = contexts
    seed['input_organization'] = 'fifteenth-exact-calculation-metadata-v1'
    seed['retrieval_boundary'] = ('最终提交阶段没有资料取回工具。calculation_contexts 是本请求内的共同说明，'
        '每条 summary.context_ref 引用其中对象；缺少证券/期间/来源的算式仍只是对应输入的算术。'
        'full_inputs.record_id 属于历史会话，不能在本会话调用；原完整输入、工具输出和访问回执保存在私有源包。'
        '两份子报告全文和算式参数实际可见，来源列表仅为索引，不等于本轮亲读原文。')
    return seed


def expanded_calculations(seed):
    result = []
    for record in seed['calculations']:
        summary = copy.deepcopy(record['summary'])
        context = seed.get('calculation_contexts', {}).get(summary.pop('context_ref', None), {})
        result.append({**context, **summary})
    return result


def sizes(seed):
    return {key: len(json.dumps(value, ensure_ascii=False)) for key, value in seed.items()}


def source_identity(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def execution_policy(cfg):
    """Non-secret policy actually used; the authorized attempt overrides are explicit."""
    keys = ('model_protocol', 'model_responses_stream', 'model_native_search',
        'model_first_event_timeout_seconds', 'model_stream_idle_timeout_seconds',
        'model_request_hard_timeout_seconds', 'synthesis_request_hard_timeout_seconds',
        'model_request_timeout_seconds', 'synthesis_request_timeout_seconds',
        'analysis_timeout_seconds', 'agent_timeout_seconds', 'max_context_chars',
        'max_tool_result_chars', 'max_output_tokens', 'chat_use_max_completion_tokens',
        'reasoning', 'synthesis_max_calls', 'manager_final_reserved_calls')
    return {key:copy.deepcopy(cfg.get(key)) for key in keys}


def isolated_overrides(args):
    """Only explicit isolated experiment options are applied."""
    from app.config import REASONING_EFFORTS
    effort = getattr(args, 'manager_reasoning_effort', None)
    if effort is not None and effort not in REASONING_EFFORTS:
        raise ValueError('Invalid isolated manager reasoning effort')
    overrides = {'manager_reasoning_effort': effort} if effort is not None else {}
    output_limit = getattr(args, 'max_output_tokens', None)
    if output_limit is not None:
        if type(output_limit) is not int or output_limit <= 0:
            raise ValueError('Isolated max_output_tokens must be a positive integer')
        overrides['max_output_tokens'] = output_limit
    required_only = getattr(args, 'required_fields_only', False)
    if type(required_only) is not bool:
        raise ValueError('required_fields_only must be an explicit boolean option')
    if required_only:
        overrides['required_fields_only'] = True
    route = getattr(args, 'network_route', 'environment')
    if route not in ('environment', 'direct'):
        raise ValueError('network_route must be environment or direct')
    # Preserve the existing default override identity. Direct is an explicit,
    # isolated experiment option and must be repeated at execution.
    if route == 'direct':
        overrides['network_route'] = route
    return overrides


def route_description(route):
    return ('existing environment; TLS verification unchanged' if route == 'environment' else
            'explicit AsyncHTTPTransport; no environment proxy mounts; TLS verification and environment CA retained')


async def configure_network_route(client, route):
    """Change only this unused isolated HTTP client, never global proxy/CA state."""
    if route == 'environment':
        return
    if route != 'direct':
        raise ValueError('Unknown isolated network route')
    import httpx
    previous = client.http
    # As in verify_fourteenth_transport: explicit transport disables AsyncClient
    # automatic proxy mounts. Transport verify/trust_env remain their True defaults,
    # so SSL_CERT_FILE/SSL_CERT_DIR continue to select trusted certificate roots.
    transport = httpx.AsyncHTTPTransport()
    try:
        replacement = httpx.AsyncClient(base_url=previous.base_url, headers=previous.headers,
            timeout=previous.timeout, follow_redirects=previous.follow_redirects,
            transport=transport)
    except BaseException:
        await transport.aclose()
        raise
    try:
        await previous.aclose()
    except BaseException:
        await replacement.aclose()
        raise
    client.http = replacement


def task_instruction(overrides):
    reason = ('历史隔离三证券账户证据汇总；沿包内价格时点、版本和成本/费用口径，复核比较的主语、方向与分母。不是今天行情或日常持仓。'
        '回答现金与敞口、每只证券倾向和账户动作、比较合理的替代方案、依据、重要反向因素及改变看法条件。'
        '正文明确 runtime_status.time_context 中的历史判断时点 decision_as_of 和实际执行时间 execution_time；'
        '下一交易日等相对时间须注明以历史判断时点为准，不将当时的未来说成本次执行时仍未发生。'
        '具体说明未完成专题限制哪部分；只汇总可见证据，不新增搜索。本任务没有预测批次，forecasts应省略或为空。')
    if overrides.get('required_fields_only'):
        reason += (' 本次历史汇总只填写 report、completion、scope、decision_summary、limitations 五个必填字段。'
            'report 保留完整三证券问题、依据、反向因素、改变看法条件；关键来源就近附输入已有 URL 或明确材料ID，'
            '关键计算给出已有 calculation_id 或可核对的算式与分母，保持摘录、元数据和未读原文的访问限制；'
            '其他可选附录本次省略，不逐条复制材料索引。')
    return reason


def apply_isolated_overrides(store, cfg, overrides):
    if 'manager_reasoning_effort' in overrides:
        store.update_agent_definition('manager', {
            'default_binding': {'reasoning_effort': overrides['manager_reasoning_effort']}})
    if 'max_output_tokens' in overrides:
        cfg['max_output_tokens'] = overrides['max_output_tokens']
        store.set_meta('settings', cfg)
    return store.freeze_agent_team()


def visible_terminal_output(data):
    """Whitelist visible terminal fields; never retain opaque reasoning or deltas.

    Called only after the production _post credential/envelope checks. This is
    diagnostic evidence before contract validation, never an accepted delivery.
    """
    if not isinstance(data, dict) or data.get('status') not in {'completed', 'incomplete', 'failed', 'cancelled'}:
        return None
    output = data.get('output')
    if not isinstance(output, list):
        return None
    visible = []
    for item in output:
        if not isinstance(item, dict):
            continue
        if item.get('type') == 'message' and isinstance(item.get('content'), list):
            content = [{'type': part['type'], 'text': part['text']} for part in item['content']
                if isinstance(part, dict) and part.get('type') in {'output_text', 'text'}
                and isinstance(part.get('text'), str)]
            if content:
                visible.append({'type': 'message', 'content': content})
        elif (item.get('type') == 'function_call' and item.get('status', 'completed') == 'completed'
              and all(isinstance(item.get(key), str) for key in ('name', 'call_id', 'arguments'))):
            visible.append({key: item[key] for key in ('type', 'name', 'call_id', 'arguments')})
    return {'kind': 'private_visible_terminal_output', 'status': data['status'],
        'response_id': data.get('id') if isinstance(data.get('id'), str) else None,
        'not_accepted': True, 'boundary': 'Captured before contract validation; no execution or argument repair',
        'output': visible}


def prepare(args):
    from app.agents import MANAGER_TOOLS
    from app.config import model_config_summary
    from app.runtime_version import source_digest, env_file_stamp
    overrides = isolated_overrides(args)
    route = overrides.get('network_route', 'environment')
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    output.chmod(0o700)
    package = json.loads(args.evidence.read_text())
    if package.get('kind') not in ('real_historical_visible_evidence', 'controlled_fixture'):
        raise ValueError('Explicit historical/controlled provenance required')
    seed = compact_seed(package)
    if expanded_calculations(seed) != [item['summary'] for item in package['calculations']]:
        raise ValueError('Calculation projection is not lossless')
    store, cfg = configure_isolated(args.source, output/'prepared-config.sqlite3', args.reference_account)
    baseline_team = store.freeze_agent_team()
    baseline_policy = execution_policy(cfg)
    team = apply_isolated_overrides(store, cfg, overrides)
    cfg = {**store.runtime_role_config(team, 'manager', cfg), '_agent_team': team}
    schema = next(tool for tool in MANAGER_TOOLS if tool['name'] == 'finish_analysis')
    prompt = team['roles']['manager']['system_prompt']
    instruction_sha256 = hashlib.sha256(task_instruction(overrides).encode()).hexdigest()
    old = comparison_seed(package)
    # This manifest is safe to copy into public docs: no keys, proxy addresses,
    # complete visible reports or provider response bodies.
    manifest = {'kind': 'historical_three_security_final_preparation',
        'input_organization': seed['input_organization'], 'source_sha256': source_digest(),
        'head': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
        'package_sha256': digest(package), 'input_sha256': digest(seed),
        'package_kind': package['kind'], 'historical_version': package['input_version'],
        'data_as_of': package.get('data_as_of'), 'before_input_chars': len(json.dumps(old, ensure_ascii=False)),
        'input_chars': len(json.dumps(seed, ensure_ascii=False)),
        'before_components': sizes(old), 'components': sizes(seed),
        'manager_prompt_chars': len(prompt), 'manager_prompt_sha256': hashlib.sha256(prompt.encode()).hexdigest(),
        'production_final_schema_chars': len(json.dumps(schema, ensure_ascii=False)),
        'production_final_schema_sha256': digest(schema), 'full_reports_count': len(seed['reports']),
        'full_reports_unchanged': seed['reports'] == package['reports'],
        'reports': [{'report_id': item.get('report_id'), 'sha256': digest(item)} for item in seed['reports']],
        'calculation_count': len(seed['calculations']), 'all_calculation_summaries_lossless': True,
        'all_calculation_arguments_unchanged': [c.get('arguments') for c in seed['calculations']] == [c.get('arguments') for c in package['calculations']],
        'material_index_count': len(seed['materials']), 'source_indexes_unchanged': seed['materials'] == package['materials'],
        'account_metrics_gaps_unchanged': all(seed.get(k) == package.get(k) for k in ('account','metrics','open_questions','unresolved_tasks','data_as_of','input_version')),
        'requested_configuration': model_config_summary(cfg),
        'isolated_overrides': overrides,
        'task_instruction_sha256': instruction_sha256,
        'baseline_execution_policy': baseline_policy,
        'execution_policy': execution_policy(cfg),
        'role_matrix': [{k:r[k] for k in ('role_id','resolved','config_error')} for r in store.list_agent_roles()],
        'request_policy': {'maximum_http_attempts': 1 if route == 'direct' else 2, 'first_submission': 1,
            'contract_correction_maximum': 0 if route == 'direct' else 1,
            'automatic_http_retries': 0, 'new_search_or_children': False, 'new_authorization_required': True,
            'max_output_tokens': cfg['max_output_tokens'], 'analysis_timeout_seconds': cfg['analysis_timeout_seconds'],
            'synthesis_request_hard_timeout_seconds': cfg['synthesis_request_hard_timeout_seconds'],
            'network_route': route, 'route': route_description(route),
            'stop': ['first accepted explicit submission', 'unknown-outcome transport failure',
                     'budget/deadline exhaustion', 'first invalid submission' if route == 'direct' else 'second invalid submission',
                     'source/config/evidence changes'],
            'spend_cap_supported': False, 'fee_amount': None,
            'fee_note': 'No verified endpoint currency/spend ceiling; request/output/time caps do not guarantee a monetary maximum.'},
        'paid_requests': 0, 'content_assessment': 'not_evaluated', 'account_report_obtained': False}
    write_private(output/'input.json', seed)
    write_private(output/'preparation.json', manifest)
    write_private(output/'private-source.json', {'configuration_source': str(args.source.resolve()),
        'reference_account': args.reference_account, 'historical_source': str(args.historical_source.resolve()),
        'evidence_source': str(args.evidence.resolve()), 'historical_source_sha256': source_identity(args.historical_source),
        'environment_stamp': env_file_stamp(), 'historical_task_id': package['historical_task_id'],
        # Reuse the application's effective-team identity. Fresh isolated exact
        # prompt migrations update observation timestamps independently; those
        # timestamps are not changes to prompts/tools/bindings/credentials.
        'baseline_effective_team_id': baseline_team['team_id'],
        'baseline_execution_policy': baseline_policy,
        'isolated_overrides': overrides, 'task_instruction_sha256': instruction_sha256,
        'effective_team_id': team['team_id']})
    if args.manifest:
        args.manifest.parent.mkdir(parents=True, exist_ok=True)
        args.manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2)+'\n')
    return manifest


async def run_prepared(args):
    from app.agents import ResearchRuntime
    from app.domain import now
    from app.runtime_version import source_digest, env_file_stamp
    from app.service import ResearchTools, _execution_forecast_batches
    from scripts.verify_thirteenth_live import AttemptBudget
    from scripts.verify_fourteenth_budget import SharedAttemptBudget
    root = args.prepared.resolve()
    def reject(message):
        # Setup guards run before any ModelClient is created. Record the reason
        # separately so a failed check cannot overwrite an earlier execution.
        directory = root/'preflight-rejections'
        directory.mkdir(exist_ok=True)
        directory.chmod(0o700)
        write_private(directory/(uuid.uuid4().hex+'.json'), {
            'kind':'aggregate_preflight_rejection', 'at':now(), 'error':message,
            'http_attempts_reserved':0, 'model_client_created':False, 'paid_requests_sent':0})
        raise ValueError(message)
    manifest = json.loads((root/'preparation.json').read_text())
    private = json.loads((root/'private-source.json').read_text())
    seed = json.loads((root/'input.json').read_text())
    overrides = isolated_overrides(args)
    route = overrides.get('network_route', 'environment')
    if overrides != manifest.get('isolated_overrides', {}) or overrides != private.get('isolated_overrides', {}):
        reject('Isolated override differs from preparation; repeat the explicit reviewed options')
    if (manifest['request_policy'].get('network_route', 'environment') != route
            or manifest['request_policy']['route'] != route_description(route)):
        reject('Network route policy differs from preparation')
    instruction = task_instruction(overrides)
    instruction_sha256 = hashlib.sha256(instruction.encode()).hexdigest()
    if instruction_sha256 != manifest.get('task_instruction_sha256') or instruction_sha256 != private.get('task_instruction_sha256'):
        reject('Historical task instruction differs from preparation')
    if not args.authorization_note or args.authorized_http_attempts not in (1, 2):
        raise ValueError('New explicit authorization for 1 or 2 attempts required; old 4/2/40 ledgers are not reused')
    if route == 'direct' and args.authorized_http_attempts != 1:
        reject('This isolated direct comparison allows exactly one authorized HTTP attempt; no correction or retry')
    if not getattr(args, 'global_budget', None):
        reject('Existing fifteenth aggregate authorization ledger required')
    global_budget = SharedAttemptBudget(args.global_budget)
    authorization = global_budget.status()
    if authorization['maximum'] != 10 or {key:value['maximum'] for key,value in authorization['stages'].items()} != {'final':10,'full':0}:
        reject('This stage requires the separately authorized ten-attempt aggregate ledger; no previous allowance transfer')
    if source_digest() != manifest['source_sha256'] or env_file_stamp() != private['environment_stamp']:
        reject('Source/environment changed after preparation; prepare and review again')
    if digest(seed) != manifest['input_sha256'] or digest(json.loads(Path(private['evidence_source']).read_text())) != manifest['package_sha256']:
        reject('Frozen input/evidence changed')
    historical = Path(private['historical_source'])
    if source_identity(historical) != private['historical_source_sha256']:
        reject('Historical database changed after preparation')
    output = root/'execution'
    output.mkdir(exist_ok=False)
    output.chmod(0o700)
    target = output/'evaluation.sqlite3'
    # Only the historical isolated database is copied; the daily DB is never
    # opened writable. No web/quote fetch or normal worker is started.
    with sqlite3.connect(historical.resolve().as_uri()+'?mode=ro', uri=True) as origin, sqlite3.connect(target) as dest:
        origin.backup(dest)
    if source_identity(historical) != private['historical_source_sha256']:
        reject('Historical database changed during copying; no request sent')
    target.chmod(0o600)
    store, cfg = configure_isolated(Path(private['configuration_source']), target, private['reference_account'])
    baseline_team = store.freeze_agent_team()
    if baseline_team['team_id'] != private.get('baseline_effective_team_id', private['effective_team_id']):
        reject('Role definitions or effective bindings changed after preparation')
    baseline_policy = private.get('baseline_execution_policy')
    if baseline_policy is not None and (execution_policy(cfg) != baseline_policy
            or manifest.get('baseline_execution_policy') != baseline_policy):
        reject('Execution policy changed after preparation; source baseline must remain unchanged before isolated overrides')
    team = apply_isolated_overrides(store, cfg, overrides)
    if team['team_id'] != private['effective_team_id']:
        reject('Isolated effective team differs from preparation')
    with store.connect() as db:
        row = db.execute('SELECT account_id FROM synthesis_checkpoints WHERE job_id=?', (private['historical_task_id'],)).fetchone()
        if row is None: reject('Historical account is absent; never synthesize a replacement')
        account_id = row[0]
        # Current effective source-account bindings were expanded into isolated
        # defaults. Historical copy overrides must not shadow those bindings.
        db.execute('DELETE FROM account_agent_overrides')
    cfg.update(max_model_calls=args.authorized_http_attempts, max_agent_turns=args.authorized_http_attempts,
               model_retries=0)
    store.set_meta('settings', cfg)
    store.enqueue(account_id, reason='fifteenth_historical_final_aggregate')
    job = store.claim_jobs(account_id=account_id)[0]
    if _execution_forecast_batches(store, job):
        reject('Historical copy associated a forecast batch with the aggregate task; no clock or forecast mutation is allowed')
    cfg = job['config_snapshot']
    cfg = {**store.runtime_role_config(cfg['_agent_team'], 'manager', cfg), '_agent_team': cfg['_agent_team']}
    from app.config import model_config_summary
    current = model_config_summary(cfg)
    expected = manifest['requested_configuration']
    for field in ('model_protocol','model_base_url','model_name','reasoning_effort'):
        if current.get(field) != expected.get(field): reject('Model binding changed after preparation: '+field)
    if cfg['max_output_tokens'] != manifest['request_policy']['max_output_tokens']:
        reject('Output limit changed after preparation')
    if execution_policy(cfg) != manifest['execution_policy']:
        reject('Execution policy changed after preparation; time/context/protocol limits must be reviewed again')
    account = store.snapshot(account_id, job['input_version'])
    if any(account.get(k) != seed['account'].get(k) for k in ('cash','holdings','preferences','version')):
        reject('Historical account snapshot differs from evidence package')
    scope = store.delivery_scope(job)
    context = {'job_id':job['id'], 'task_id':job['id'], 'account':account,
        'input_version':job['input_version'], 'metrics':seed['metrics'], 'data_as_of':seed['data_as_of'],
        'allowed_delivery_scope':scope['allowed'], 'required_delivery_scope':scope['required'],
        'scope':'account', 'historical_evidence': {k:v for k,v in seed.items() if k not in ('account','metrics')},
        'reason':instruction,
        'recommendations':store.recommendation_context(account_id, job['id'])}
    tools = ResearchTools(store, job, cfg, context=context)
    budget = AttemptBudget(output/'attempt-budget.sqlite3', args.authorized_http_attempts)
    summary = {'kind':'real_historical_three_security_aggregate', 'started_at':now(),
        'source_sha256':manifest['source_sha256'], 'input_sha256':manifest['input_sha256'],
        'authorization_note':args.authorization_note, 'authorized_http_attempts':args.authorized_http_attempts,
        'job_id':job['id'], 'account_id':account_id, 'input_version':job['input_version'],
        'data_as_of':seed['data_as_of'], 'request_inputs':[], 'content_assessment':'not_evaluated',
        'isolated_overrides':overrides, 'task_instruction_sha256':instruction_sha256, 'visible_terminal_files':[],
        'network_route':route, 'routing':route_description(route),
        'fee_amount':None, 'daily_database_writes':0,
        'normal_business_matrix_executed':False, 'published':False}
    def save():
        summary['attempt_budget'] = budget.status()
        summary['shared_attempt_budget'] = global_budget.status()
        write_private(output/'summary.json', summary)
    async def emit(event): store.event(job['id'], event)
    runtime = ResearchRuntime(context, cfg, tools, emit)
    try:
        await configure_network_route(runtime.client, route)
    except Exception as exc:
        await runtime.close()
        summary.update(error_code=type(exc).__name__, error=str(exc), completed_at=now(), requests=[])
        store.set_job_state(job['id'], 'failed', str(exc), error_code=summary['error_code'])
        save()
        return 2
    original_post = runtime.client._post
    async def retain_visible_terminal(endpoint, payload, session=None):
        data = await original_post(endpoint, payload, session)
        visible = visible_terminal_output(data) if runtime.client.protocol == 'responses' else None
        if visible is not None:
            filename = f'visible-terminal-{len(summary["visible_terminal_files"]) + 1:02d}.json'
            write_private(output/filename, visible)
            summary['visible_terminal_files'].append({'file':filename, 'status':visible['status'],
                'not_accepted':True, 'sha256':source_identity(output/filename)})
            save()
        return data
    runtime.client._post = retain_visible_terminal
    original_budget = runtime.client.before_request
    async def reserve():
        if source_digest() != manifest['source_sha256'] or env_file_stamp() != private['environment_stamp']:
            raise ValueError('Source/environment changed during authorized run')
        if original_budget: await original_budget()
        if budget.status()['reserved_attempts'] >= args.authorized_http_attempts:
            from app.model import ModelBudgetExceeded
            raise ModelBudgetExceeded('This aggregate run exhausted its one/two-attempt cap')
        global_budget.reserve('final')
        budget.reserve()
        save()
    runtime.client.before_request = reserve
    async def inspect_request(request):
        payload = json.loads(request.content)
        messages = payload.get('input', payload.get('messages', []))
        actual = json.loads(next(m['content'] for m in messages if m.get('role') == 'user'))
        checked = (actual.get('historical_evidence') == context['historical_evidence']
                   and actual.get('account') == account and actual.get('metrics') == seed['metrics'])
        actual_instruction = actual.get('reason')
        actual_instruction_sha256 = hashlib.sha256(actual_instruction.encode()).hexdigest() if isinstance(actual_instruction, str) else None
        instruction_matches = actual_instruction == instruction and actual_instruction_sha256 == manifest['task_instruction_sha256']
        schema = payload['tools'][0] if cfg['model_protocol'] == 'responses' else payload['tools'][0]['function']
        schema = {k:v for k,v in schema.items() if k not in ('type','strict')}
        schema_matches = len(payload['tools']) == 1 and digest(schema) == manifest['production_final_schema_sha256']
        actual_effort = (payload.get('reasoning') or {}).get('effort') if cfg['model_protocol'] == 'responses' else payload.get('reasoning_effort')
        effort_matches = actual_effort == expected['reasoning_effort']
        actual_output_limit = payload.get('max_output_tokens', payload.get('max_completion_tokens', payload.get('max_tokens')))
        output_limit_matches = actual_output_limit == manifest['request_policy']['max_output_tokens']
        summary['request_inputs'].append({'payload_sha256':digest(payload), 'payload_bytes':len(request.content),
            'network_route':route,
            'full_historical_evidence_visible':checked, 'report_count':len(seed['reports']),
            'calculation_count':len(seed['calculations']), 'actual_schema_sha256':digest(schema),
            'production_schema_matches':schema_matches,
            'reasoning_effort_sent':actual_effort, 'reasoning_effort_matches':effort_matches,
            'max_output_tokens_sent':actual_output_limit, 'output_limit_matches':output_limit_matches,
            'time_context':copy.deepcopy((actual.get('runtime_status') or {}).get('time_context')),
            'task_instruction_sha256':actual_instruction_sha256, 'task_instruction_matches':instruction_matches,
            'transport_not_entered_reason': None if checked and schema_matches and effort_matches and output_limit_matches and instruction_matches else
                'Actual request differs from reviewed evidence, task instruction, production schema, effort or output limit; reserved attempt retained'})
        save()
        if not instruction_matches: raise ValueError('Actual request task instruction differs from reviewed policy; not sent')
        if not output_limit_matches: raise ValueError('Actual request output limit differs from reviewed policy; not sent')
        if not effort_matches: raise ValueError('Actual request effort differs from reviewed manager binding; not sent')
        if not schema_matches: raise ValueError('Actual final schema differs from reviewed production schema; not sent')
        if not checked: raise ValueError('Actual request lost historical facts/reports/calculations; not sent')
        store.event(job['id'], {'type':'historical_evidence_delivery', 'at':now(),
            'payload':{'input_sha256':manifest['input_sha256'], 'reports':manifest['reports'],
                'calculation_count':len(seed['calculations']), 'full_reports_visible':True,
                'source_access':'Historical delegated reports and calculation arguments; source index is not full text reading'}})
    runtime.client.http.event_hooks['request'].append(inspect_request)
    save()
    result = None
    try:
        result = await runtime.run()
        result['historical_evidence_access'] = {'input_sha256':manifest['input_sha256'],
            'reports':manifest['reports'], 'calculation_count':len(seed['calculations']),
            'full_visible_input_verified':all(r['full_historical_evidence_visible'] for r in summary['request_inputs']),
            'source_access':'Delivered historical reports; no new source-document reading or current child execution'}
        write_private(output/'model-result.json', result)
        summary['contract_accepted'] = True
        summary['published'] = store.publish(job['id'], result, seed['data_as_of'])
        summary['report_sha256'] = hashlib.sha256(result['report'].encode()).hexdigest()
        summary['submission_outcome'] = result.get('submission_outcome')
    except Exception as exc:
        summary.update(error_code=getattr(exc,'code',type(exc).__name__), error=str(exc))
        if result is not None:
            store.retain_delivery_candidate(job['id'], result, str(exc))
        store.set_job_state(job['id'], 'failed', str(exc), error_code=summary['error_code'])
    finally:
        await runtime.close()
        summary.update(completed_at=now(), requests=runtime.client.requests,
            source_unchanged=source_digest()==manifest['source_sha256'],
            environment_unchanged=env_file_stamp()==private['environment_stamp'])
        with store.connect() as db:
            rows = db.execute('SELECT kind,count(*) FROM forecasts WHERE job_id=? GROUP BY kind', (job['id'],)).fetchall()
        summary['forecast_rows_by_kind'] = dict(rows)
        summary['formal_predictions'] = dict(rows).get('formal', 0)
        save()
    return 0 if summary['published'] else 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path)
    parser.add_argument('--reference-account')
    parser.add_argument('--historical-source', type=Path)
    parser.add_argument('--evidence', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--manifest', type=Path)
    parser.add_argument('--manager-reasoning-effort', choices=('low','medium','high','xhigh','max'),
        help='Explicit isolated manager binding override; repeat the same option for preparation and execution')
    parser.add_argument('--max-output-tokens', type=int,
        help='Explicit isolated output limit override; repeat the same option for preparation and execution')
    parser.add_argument('--required-fields-only', action='store_true',
        help='Historical aggregate instruction to omit optional appendices; full production schema remains unchanged; repeat for execution')
    parser.add_argument('--network-route', choices=('environment','direct'), default='environment',
        help='Isolated route only; direct disables environment proxy mounts while retaining TLS verification/environment CA; repeat for execution')
    parser.add_argument('--prepared', type=Path)
    parser.add_argument('--run-real', action='store_true')
    parser.add_argument('--authorization-note')
    parser.add_argument('--authorized-http-attempts', type=int, default=0)
    parser.add_argument('--global-budget', type=Path, help='Existing separately authorized ten-attempt aggregate ledger')
    args = parser.parse_args()
    if args.run_real:
        if not args.prepared or not args.authorization_note or args.authorized_http_attempts not in (1,2) or not args.global_budget:
            parser.error('Explicit new approval, --prepared, --global-budget and --authorized-http-attempts 1 or 2 required')
        raise SystemExit(asyncio.run(run_prepared(args)))
    if args.authorized_http_attempts or args.authorization_note:
        parser.error('Preparation does not consume or grant an authorization')
    if not all((args.source,args.reference_account,args.historical_source,args.evidence,args.output)):
        parser.error('Offline preparation needs configuration source/account, historical source, evidence and NEW output')
    manifest = prepare(args)
    print(json.dumps({k:manifest[k] for k in ('input_sha256','before_input_chars','input_chars','full_reports_count','calculation_count','paid_requests')}))

if __name__ == '__main__': main()
