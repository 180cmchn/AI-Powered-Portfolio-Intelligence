#!/usr/bin/env python3
"""Uncapped-count real business matrix through manage.py and ordinary HTTP APIs.

Only model configuration is copied from the read-only reference database. No
transport injection, worker patch, task allowlist or time override is used.
All legitimate account/global jobs remain eligible under production policy.
Private reports and credentials references stay in ignored data/evaluations.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
from datetime import datetime, time as clock_time
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import httpx
from app.calendar import market_state, SHANGHAI
from app.config import defaults, migrate_timeout_policy
from app.domain import now
from app.runtime_version import source_digest
from app.store import Store
from app.model import ModelClient
from scripts.verify_live_chain import all_events

COUNT_FIELDS = ('max_model_calls', 'max_agent_turns', 'synthesis_max_calls')
INITIAL_BUYS = (('SH:518880', '300', '10'), ('SZ:002354', '1000', '10'), ('SZ:159599', '1000', '2.5'))


def configure_isolated(source, target, account_id, *, route=None):
    """Read only the chosen effective team; never copy account/business tables."""
    store = Store(target, 'live')
    with sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True) as origin, store.connect() as dest:
        origin.row_factory = sqlite3.Row
        origin.execute('PRAGMA query_only=ON')
        if not origin.execute('SELECT 1 FROM accounts WHERE id=?', (account_id,)).fetchone():
            raise ValueError('Reference account does not exist; configuration cannot be inferred')
        saved = origin.execute("SELECT value FROM meta WHERE key='settings'").fetchone()
        config = {**defaults(), **migrate_timeout_policy(json.loads(saved[0]) if saved else {})}
        overrides = {row['role_id']: json.loads(row['body']) for row in origin.execute(
            'SELECT role_id,body FROM account_agent_overrides WHERE account_id=?', (account_id,))}
        roles, providers = [], set()
        for row in origin.execute('SELECT role_id,version,body FROM agent_definitions ORDER BY rowid'):
            role = json.loads(row['body'])
            default, override = role['default_binding'], overrides.get(row['role_id'], {})
            binding = dict(override if override.get('provider_id') is not None else default)
            binding['reasoning_effort'] = override.get('reasoning_effort', default.get('reasoning_effort', 'medium'))
            role['default_binding'] = binding
            if binding.get('provider_id'):
                providers.add(binding['provider_id'])
            roles.append((row['role_id'], row['version'], json.dumps(role, ensure_ascii=False)))
        for table in ('model_providers', 'provider_connections', 'provider_catalogs', 'agent_definitions'):
            dest.execute('DELETE FROM ' + table)
        dest.executemany('INSERT INTO agent_definitions VALUES(?,?,?)', roles)
        for provider_id in providers:
            for table, key in (('model_providers', 'id'), ('provider_connections', 'provider_id'), ('provider_catalogs', 'provider_id')):
                for row in origin.execute(f'SELECT * FROM {table} WHERE {key}=?', (provider_id,)):
                    dest.execute(f'INSERT INTO {table} VALUES(?,?,?)', tuple(row))
            for row in origin.execute("SELECT key,value FROM meta WHERE key=?",
                                      ('revoked_provider_credentials:' + provider_id,)):
                dest.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', tuple(row))
        # Run only the production exact-hash migrations on copied built-ins;
        # customized prompts and all provider/model/effort choices survive.
        # Store(target) already initialized today's marker before old roles
        # were copied. Preserve the source marker, not that temporary marker,
        # so an old contract advances once and a current contract never twice.
        for marker in ('seventeenth_market_semantics_prompt_migration',
                       'seventeenth_demand_declaration_contract_migration',
                       'seventeenth_card_basis_prompt_migration'):
            migration = origin.execute('SELECT value FROM meta WHERE key=?', (marker,)).fetchone()
            if migration:
                dest.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', (marker, migration[0]))
            else:
                dest.execute('DELETE FROM meta WHERE key=?', (marker,))
        dest.execute("DELETE FROM meta WHERE key IN ('fourteenth_role_migration','fifteenth_synthesis_prompt_migration','seventeenth_content_prompt_migration')")
        store.migrate_fourteenth_roles(dest)
    config.update({key: None for key in COUNT_FIELDS})
    config.update(scheduled_research_enabled=False, market_refresh_enabled=True,
        forecast_evaluation_enabled=True, debounce_seconds=30,
        scan_interval_minutes=240, analysis_interval_minutes=1440, news_queries=[])
    if route is not None:
        config['model_network_route'] = route
    store.set_meta('settings', config)
    return store, config


def preservation(paths):
    result = {}
    for label, path in paths.items():
        path = Path(path)
        if not path.exists():
            result[label] = {'exists': False}
            continue
        stat = path.stat()
        result[label] = {'exists': True, 'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns,
            'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
    return result


def preservation_comparison(before, after):
    fields = ('exists', 'size', 'sha256')
    differences = {key: {field: {'before': before.get(key, {}).get(field), 'after': after.get(key, {}).get(field)}
        for field in set(before.get(key, {})) | set(after.get(key, {}))
        if before.get(key, {}).get(field) != after.get(key, {}).get(field)} for key in set(before) | set(after)}
    return {'raw_equal': before == after,
        'content_unchanged': all(all(before.get(key, {}).get(field) == after.get(key, {}).get(field)
            for field in fields) for key in differences),
        'metadata_unchanged': all(before.get(key, {}).get('mtime_ns') == after.get(key, {}).get('mtime_ns') for key in differences),
        'differences': {key: value for key, value in differences.items() if value}}


def same_database(left, right):
    left, right = Path(left).resolve(), Path(right).resolve()
    return left == right or (left.exists() and right.exists() and left.samefile(right))


def saved_trade_receipt(connection, account_id, operation, expected):
    row = connection.execute('SELECT receipt FROM ledger_operations WHERE account_id=? AND operation_id=?',
                             (account_id, operation)).fetchone()
    if not row:
        return None
    receipt = json.loads(row[0])
    entry = receipt.get('entry') or {}
    recorded = connection.execute('SELECT body FROM ledger_entries WHERE id=? AND account_id=? AND kind=?',
        (receipt.get('entry_id'), account_id, 'trade')).fetchone()
    if not recorded or json.loads(recorded[0]) != entry:
        raise ValueError('Saved trade receipt lacks matching committed ledger evidence')
    try:
        quantities_match = all(Decimal(str(entry.get(key))) == Decimal(expected[key]) for key in ('quantity', 'price'))
    except (InvalidOperation, ValueError, TypeError):
        quantities_match = False
    if (any(entry.get(key) != expected[key] for key in ('symbol', 'side')) or
            not quantities_match or
            entry.get('mode') != 'simulated'):
        raise ValueError('Existing operation does not match the declared matrix trade')
    return receipt


def apply_manager_effort(store, effort, account_id=None):
    """Apply only an explicit user override through normal role configuration."""
    if effort is None:
        return None
    if effort != 'xhigh':
        raise ValueError('This validation override supports only explicitly requested manager xhigh')
    before = {r['role_id']: r for r in store.list_agent_roles(include_archived=True)}
    manager = before['manager']
    with store.connect() as db:
        row = db.execute('SELECT body FROM account_agent_overrides WHERE account_id=? AND role_id=?',
                         (account_id, 'manager')).fetchone() if account_id else None
        old_override = json.loads(row[0]) if row else None
    store.update_agent_definition('manager', {'expected_version': manager['version'],
        'default_binding': {'reasoning_effort': effort}})
    if old_override and old_override.get('reasoning_effort', effort) != effort:
        store.set_account_agent_override(account_id, 'manager', {
            **{k: old_override[k] for k in ('provider_id', 'model_id') if k in old_override},
            'reasoning_effort': effort})
    after = {r['role_id']: r for r in store.list_agent_roles(include_archived=True)}
    assert all(before[k] == after[k] for k in before if k != 'manager')
    expected = {**manager['default_binding'], 'reasoning_effort': effort}
    assert after['manager']['default_binding'] == expected
    effective = next(r for r in store.list_agent_roles(account_id) if r['role_id'] == 'manager')['resolved']
    assert effective['reasoning_effort'] == effort
    return {'source': 'explicit_cli_user_override', 'argument': '--manager-effort', 'value': effort,
        'role_id': 'manager', 'previous_binding': manager['default_binding'],
        'current_binding': after['manager']['default_binding'],
        'account_effort_override_changed': bool(old_override and old_override.get('reasoning_effort', effort) != effort),
        'non_manager_roles_unchanged': True,
        'scheduling': 'Applied before normal manage start; production startup/configuration waiters and claim-time binding remain authoritative.'}


def resume_configuration(database, origin, source, account_alias, *, route=None, manager_effort=None):
    """Validate explicit prior-run identity read-only before opening the Store."""
    evaluation_root = (ROOT / 'data/evaluations').resolve()
    database, origin, source = Path(database).resolve(), Path(origin).resolve(), Path(source).resolve()
    if not source.is_file():
        raise ValueError('An existing daily source is required for identity rejection and read-only preservation')
    if (not database.is_file() or not database.is_relative_to(evaluation_root) or
            not origin.is_file() or not origin.is_relative_to(evaluation_root)):
        raise ValueError('Resume database and origin must be existing independent data/evaluations artifacts')
    if same_database(database, source) or same_database(database, ROOT / 'data/live.sqlite3'):
        raise ValueError('Daily source database cannot be a resume target, including aliases/hard links')
    if account_alias != 'A':
        raise ValueError('Resume requires the explicit matrix account alias A')
    prior = json.loads(origin.read_text())
    prior_database = (prior.get('database_path') or (prior.get('health') or {}).get('runtime_version', {}).get('database_path')
                      or origin.parent / 'evaluation.sqlite3')
    if not same_database(database, prior_database) or prior.get('kind') != 'seventeenth_normal_start_real_matrix':
        raise ValueError('Origin summary does not identify this matrix database')
    if not prior.get('finished_at'):
        raise ValueError('Origin run must have a recorded terminal launcher state before resuming')
    a_id = prior.get('a_id')
    with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True) as db:
        db.execute('PRAGMA query_only=ON')
        config = json.loads(db.execute("SELECT value FROM meta WHERE key='settings'").fetchone()[0])
        if any(key not in config or config[key] is not None for key in COUNT_FIELDS):
            raise ValueError('Resume requires saved explicit null count limits; it never rewrites settings')
        if route is not None and route != config.get('model_network_route'):
            raise ValueError('Resume route conflicts with saved configuration')
        a_operation = db.execute('SELECT account_id FROM account_operations WHERE operation_id=?',
                                 ('seventeenth-create-a',)).fetchone()
        if not a_id or not a_operation or a_operation[0] != a_id or not db.execute('SELECT 1 FROM accounts WHERE id=?', (a_id,)).fetchone():
            raise ValueError('Explicit origin/account-operation evidence for A is missing or inconsistent')
        buys = []
        for i, (symbol, quantity, price) in enumerate(INITIAL_BUYS):
            receipt = saved_trade_receipt(db, a_id, f'seventeenth-buy-{i}',
                {'symbol': symbol, 'side': 'buy', 'quantity': quantity, 'price': price})
            if receipt is None:
                raise ValueError('Resume requires all three existing buys; it does not reconstruct missing trades')
            if (receipt.get('input_version') != i + 2 or receipt['entry'].get('account_version') != i + 2
                    or receipt['entry'].get('input_version') != i + 1):
                raise ValueError('Original three-buy receipts must prove A versions 2, 3 and 4')
            buys.append(receipt)
        b_operation = db.execute('SELECT account_id FROM account_operations WHERE operation_id=?',
                                 ('seventeenth-create-b',)).fetchone()
        b_id = b_operation[0] if b_operation else None
        if prior.get('b_id') and prior['b_id'] != b_id:
            raise ValueError('Origin B identity conflicts with persisted account-operation evidence')
        if b_id and not db.execute('SELECT 1 FROM accounts WHERE id=?', (b_id,)).fetchone():
            raise ValueError('Persisted matrix B was deleted; do not guess another account')
        current_version, restart_job_id = db.execute('SELECT version,latest_job_id FROM accounts WHERE id=?', (a_id,)).fetchone()
        sale = saved_trade_receipt(db, a_id, 'seventeenth-sell-once',
            {'symbol': 'SH:518880', 'side': 'sell', 'quantity': '100', 'price': '11'})
        resume_stage, inherited_initial = 'before_sale', None
        trade_ids = {r[0] for r in db.execute("SELECT id FROM ledger_entries WHERE account_id=? AND kind='trade'", (a_id,))}
        expected_ids = {r['entry_id'] for r in buys}
        if b_id:
            raise ValueError('Resume requires no B and a proven original three-buy A version or its one recorded sale')
        if sale:
            expected_ids.add(sale['entry_id'])
            initial = [item for item in prior.get('scenario_outcomes', [])
                if item.get('scenario') == 'a_initial' and item.get('account_id') == a_id]
            sale_proven = any(item.get('receipt') == sale for item in prior.get('trades', []))
            if (current_version != buys[-1]['input_version'] + 1 or sale.get('input_version') != current_version or
                    sale['entry'].get('input_version') != buys[-1]['input_version'] or
                    sale['entry'].get('account_version') != current_version or not sale_proven or len(initial) != 1 or
                    any(initial[0].get(k, {}).get('current_version') != buys[-1]['input_version'] or
                        initial[0].get(k, {}).get('status') not in ('pass', 'fail')
                        for k in ('first_publication', 'final_publication'))):
                raise ValueError('Post-sale resume requires matching origin sale and real initial-version scenario evidence')
            resume_stage = 'after_sell'
            inherited_initial = copy.deepcopy(initial[0])
            inherited_initial.setdefault('inherited_from', {'summary': str(origin),
                'source_sha256': prior.get('source_sha256'), 'started_at': prior.get('started_at')})
        elif current_version != buys[-1]['input_version']:
            raise ValueError('Resume requires the original three-buy A version when no sale is recorded')
        if trade_ids != expected_ids:
            raise ValueError('Resume ledger contains additional or missing matrix trades')
        bindings_before = {row[0]: json.loads(row[1])['default_binding'] for row in db.execute('SELECT role_id,body FROM agent_definitions')}
        schema_before = db.execute('PRAGMA user_version').fetchone()[0]
    # Only this already-proven independent database receives normal migrations.
    store = Store(database, 'live')
    current = store.settings()
    with store.connect() as db:
        bindings_after = {row[0]: json.loads(row[1])['default_binding'] for row in db.execute('SELECT role_id,body FROM agent_definitions')}
        schema_after = db.execute('PRAGMA user_version').fetchone()[0]
    if bindings_before != bindings_after or any(current.get(key) is not None for key in COUNT_FIELDS):
        raise ValueError('Standard Store initialization changed selected bindings or unlimited count settings')
    effort_override = apply_manager_effort(store, manager_effort, a_id)
    return store, current, {'origin_summary': str(origin), 'origin_summary_sha256': hashlib.sha256(origin.read_bytes()).hexdigest(),
        'origin_source_sha256': prior.get('source_sha256'), 'origin_started_at': prior.get('started_at'),
        'origin_finished_at': prior.get('finished_at'), 'a_id': a_id, 'b_id': b_id, 'initial_buy_receipts': buys,
        'resume_stage': resume_stage, 'sale_receipt': sale, 'inherited_initial_scenario': inherited_initial,
        'restart_job_id': restart_job_id, 'restart_input_version': current_version,
        'interruption_recovery_limit': current.get('max_job_attempts'),
        'manager_effort_override': effort_override,
        'standard_store_initialization': {'performed': True, 'schema_version_before': schema_before,
            'schema_version_after': schema_after, 'settings_unchanged': config == current, 'selected_bindings_unchanged': True}}, prior


def fresh_resume_allowed(parent, account, snapshot, baseline, resume):
    """An explicit stopped-run continuation may start fresh, never replay a new failed request."""
    if not resume or parent['id'] != resume.get('restart_job_id') or account['version'] != resume.get('restart_input_version'):
        return False
    if account.get('latest_job_id') != parent['id'] or parent.get('state') != 'failed':
        return False
    limit = resume.get('interruption_recovery_limit')
    if (not isinstance(limit, int) or limit < 1 or parent.get('recovery_attempts', 0) < limit or
            parent.get('error') != '服务中断后重试次数已达上限，请手动重试'):
        return False  # A zero-HTTP fatal/configuration failure is not this explicit restart case.
    latest = next((r for r in snapshot['jobs'] if r['id'] == parent['id']), None)
    if not latest or latest['state'] != 'failed' or latest.get('input_version') != account['version']:
        return False
    prior_ids = {r.get('local_request_id') for r in baseline['requests'] if r.get('local_request_id')}
    return not any(r.get('job_id') == parent['id'] and
        (not r.get('local_request_id') or r['local_request_id'] not in prior_ids) for r in snapshot['requests'])


def request_accounting(snapshot, baseline, source_sha256, origin=None, run_id=None):
    """Separate prior and new actual requests even when a job ID is recovered."""
    prior_ids = {r['local_request_id'] for r in baseline['requests'] if r.get('local_request_id')}
    origin_requests = {r['local_request_id']: r for r in (origin or {}).get('observations', {}).get('requests', [])
                       if r.get('local_request_id')}
    prior_rows, current_rows, unknown_rows = [], [], []
    for request in snapshot['requests']:
        identity = request.get('local_request_id')
        if not identity:
            request['run_attribution'] = 'unknown_missing_local_request_id'
            unknown_rows.append(request)
        elif identity in prior_ids:
            old = origin_requests.get(identity)
            request.update(run_attribution='prior_baseline', request_source_sha256=(
                old.get('request_source_sha256') if old and 'request_source_sha256' in old else
                (origin or {}).get('source_sha256') if old else None),
                request_source_run=old.get('request_source_run') if old and 'request_source_run' in old else
                (origin or {}).get('started_at') if old else None)
            prior_rows.append(request)
        else:
            request.update(run_attribution='current_run', request_source_sha256=source_sha256, request_source_run=run_id)
            current_rows.append(request)
    def metrics(rows):
        full = ModelClient.usage_summary(rows)
        return {'requests': len(rows), 'failed_requests': sum(bool(r.get('error')) for r in rows),
            **{key: full.get(key) for key in ('duration_ms', 'cache_summary', 'normalized_totals')}}
    return {'prior_baseline': metrics(prior_rows), 'current_run': metrics(current_rows),
        'unknown_identity': metrics(unknown_rows), 'cumulative': metrics(snapshot['requests']),
        'prior_source_unknown_requests': sum(r.get('request_source_sha256') is None for r in prior_rows),
        'policy': 'Old failures remain in prior and cumulative metrics; only new local request IDs belong to the current source.'}


def recovery_events(events):
    found = []
    for event in events:
        if event.get('type') not in ('synthesis_resuming', 'research_resuming', 'synthesis_resumed', 'research_resumed'):
            continue
        value = {**event, **(event.get('payload') or {})}
        value['recovery_mode'] = value.get('recovery_mode') or (
            'synthesis_only' if event['type'].startswith('synthesis_') else 'continue_research')
        found.append(value)
    return found


class DiagnosticStreamStop(Exception):
    """A settled new stream failure stops diagnosis without another retry."""


def diagnostic_stream_stop(snapshot, baseline, label):
    prior_ids = {r.get('local_request_id') for r in baseline['requests']}
    failures = []
    codes = {'upstream_transport_error', 'connect_timeout', 'pool_timeout', 'write_timeout',
        'network_read_timeout', 'stream_missing_terminal', 'invalid_stream_terminal',
        'first_event_timeout', 'stream_idle_timeout', 'request_hard_timeout'}
    errors = {'ReadError', 'RemoteProtocolError', 'LocalProtocolError', 'WriteError', 'ConnectError',
              'ConnectTimeout', 'ReadTimeout', 'WriteTimeout', 'PoolTimeout'}
    task_states = {row['id']: row['state'] for row in snapshot['jobs'] + snapshot['global_runs']}
    for request in snapshot['requests']:
        if not request.get('local_request_id') or request['local_request_id'] in prior_ids or not request.get('error'):
            continue
        if request.get('error') == 'cancelled' or request.get('cancelled_by') in ('parent_failed', 'service_shutdown'):
            continue
        if (request.get('stream_terminal') in ('response.completed', '[DONE]') and
                str(request.get('stream_close_reason') or '').startswith(('terminal_tail_', 'terminal_drain_'))):
            continue
        if (request.get('error_code') not in codes and request.get('error') not in codes | errors
                and request.get('stream_close_reason') not in ('transport_error_before_terminal', 'local_protocol_rejection')):
            continue
        state = task_states.get(request.get('job_id'))
        if state is None or state in ('running', 'queued'):
            continue  # Let the owning production task save and converge first.
        keys = ('local_request_id', 'job_id', 'agent_id', 'role_id', 'started_at', 'finished_at',
                'error', 'error_code', 'cancelled_by', 'request_accepted', 'http_body_complete',
                'stream_terminal', 'stream_close_reason', 'first_transport_failure')
        failures.append({**{key: request.get(key) for key in keys}, 'owner_task_state': state})
    return {'reason': 'new_stream_failure_after_production_task_settled', 'stage': label, 'at': now(),
        'failures': failures, 'automatic_retry_started': False,
        'business_acceptance': False, 'prior_failures_excluded': True} if failures else None


def formal_window_plan(market, remaining_seconds, rule):
    """Observe the real clock; never manufacture a retrospective formal window."""
    local = datetime.fromisoformat(market['as_of']).astimezone(SHANGHAI)
    start = datetime.combine(local.date(), clock_time.fromisoformat(rule['start_time']), SHANGHAI)
    deadline = datetime.combine(local.date(), clock_time.fromisoformat(rule['deadline_time']), SHANGHAI)
    reason = ('calendar_not_confirmed_trading_day' if market.get('trading_day') is not True else
        'original_window_already_started' if local >= start else
        'configured_wall_deadline_does_not_cover_full_window' if (deadline - local).total_seconds() > remaining_seconds else None)
    return {'status': 'eligible_to_enable_before_window' if reason is None else 'not_covered',
        'reason': reason, 'start': start.isoformat(), 'deadline': deadline.isoformat(),
        'observed_at': local.isoformat(), 'rule': {key: rule[key] for key in ('horizons', 'threshold', 'start_time', 'deadline_time')}}


def recovery_progress(checkpoint):
    """Compare completed evidence, never timestamps/request counts as progress."""
    keys = ('reports', 'calculations', 'pending_tool_outputs', 'verified_material_versions', 'covered_event_scope',
            'source_coverage', 'unresolved_tasks', 'open_questions', 'recovery_state')
    bookkeeping = {'at', 'created_at', 'updated_at', 'timestamp', 'task_id', 'source_task_id', 'job_id',
        'checkpoint_job_id', 'checkpoint_source_job_id', 'agent_id', 'source_agent_id', 'report_id',
        'instance_id', 'session_id', 'record_id', 'source_record_id', 'recipient_task_id', 'source_job_id',
        'calculation_id', 'material_delivery_ids', 'report_delivery_ids', 'delivery_ids', 'delivered_at',
        'research_round', 'round'}
    unordered = {'reports', 'calculations', 'pending_tool_outputs', 'open_questions', 'unresolved_tasks', 'pending_messages',
                 'material_ids', 'prior_material_ids', 'latest_material_ids', 'historical_material_ids'}
    def content(value, field=None):
        if isinstance(value, dict):
            return {k: content(v, k) for k, v in value.items() if k not in bookkeeping}
        if isinstance(value, list):
            values = [content(v) for v in value]
            if field in unordered:
                canonical = {json.dumps(v, sort_keys=True, ensure_ascii=False, default=str): v for v in values}
                return [canonical[key] for key in sorted(canonical)]
            return values  # Ordered arithmetic operands/market rows retain order.
        return value
    return hashlib.sha256(json.dumps(content({k: checkpoint.get(k) for k in keys}),
        sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + '\n')
    path.chmod(0o600)


def sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


def observations(store):
    with store.connect() as db:
        jobs = [dict(r) for r in db.execute("SELECT id,account_id,input_version,state,reason,attempts,json_extract(result,'$.delivery.error_code') AS error_code,json_extract(task_metadata,'$.cancellation.code') AS cancellation_code,due_at FROM jobs ORDER BY created_at")]
        runs = [dict(r) for r in db.execute('SELECT id,state,kind,reason,attempts,error FROM global_runs ORDER BY created_at')]
        reports = [dict(r) for r in db.execute('SELECT id,account_id,job_id,input_version,status,report,result FROM analyses ORDER BY created_at')]
        forecasts = [dict(r) for r in db.execute('SELECT id,job_id,symbol,horizon,direction,kind,published_at,target_date FROM forecasts')]
        demands = [dict(r) for r in db.execute('SELECT id,account_id,kind,state,job_id,detail FROM research_demands')]
    requests, seen_requests = [], set()
    for job in jobs + runs:
        for event in all_events(store, job['id']):
            for request in (event.get('payload') or {}).get('requests', []):
                identity = request.get('local_request_id')
                if identity and identity in seen_requests:
                    continue
                if identity:
                    seen_requests.add(identity)
                requests.append({'job_id': job['id'], 'event_seq': event['seq'], **request})
    for report in reports:
        report['report_sha256'] = sha(report.pop('report'))
        result = json.loads(report.pop('result'))
        report['delivery'] = result.get('delivery')
    return {'jobs': jobs, 'global_runs': runs, 'reports': reports, 'forecasts': forecasts,
            'demands': demands, 'requests': requests,
            'http_attempts': len(requests),
            'completed_with_eof': sum(r.get('http_body_complete') is True and
                (r.get('response_status') == 'completed' or r.get('stream_terminal') == 'response.completed') for r in requests),
            'model_usage': ModelClient.usage_summary(requests),
            'known_fee': None, 'fee_source': 'No invoice or supplier billing correlation performed'}


def pending_work(snapshot):
    """Report blocked work without failing it or swallowing ordinary new events."""
    jobs, runs = snapshot['jobs'], snapshot['global_runs']
    busy = [item for item in jobs + runs if item['state'] in ('queued', 'running')]
    blocked = [item for item in jobs + runs if item['state'] in ('waiting_config', 'waiting_scope')]
    blocked_accounts = {item['account_id'] for item in jobs if item in blocked}
    global_blocked = any(item in blocked for item in runs)
    pending, deferred = [], []
    for demand in snapshot['demands']:
        if demand['state'] != 'pending':
            continue
        target = demand.get('account_id')
        (deferred if target in blocked_accounts or (not target and global_blocked) else pending).append(demand)
    return {'busy': busy, 'pending': pending, 'blocked': blocked, 'deferred': deferred}


def event_outcomes(snapshot):
    """Account publication cannot hide failure of another legitimate event."""
    tasks = snapshot['jobs'] + snapshot['global_runs']
    failed = [item for item in tasks if item['state'] == 'failed']
    unresolved = [item for item in tasks if item['state'] not in ('completed', 'superseded', 'failed')]
    failed_demands = [item for item in snapshot['demands'] if item['state'] == 'failed']
    work = pending_work(snapshot)
    return {'status': 'fail' if failed or failed_demands else 'incomplete' if
            unresolved or work['pending'] or work['deferred'] else 'pass',
            'failed_tasks': failed, 'failed_demands': failed_demands, 'unresolved_tasks': unresolved,
            'superseded_tasks': [item for item in tasks if item['state'] == 'superseded'],
            'boundary': 'Superseded tasks remain visible with their real input/replacement reason; this is event processing, not uninterrupted request success.'}


async def run(args):
    out = args.output.resolve()
    if not out.is_relative_to((ROOT / 'data/evaluations').resolve()):
        raise ValueError('Output must be a new private data/evaluations directory')
    out.mkdir(parents=True, exist_ok=False)
    out.chmod(0o700)
    resume_database = getattr(args, 'resume_database', None)
    if resume_database and (not getattr(args, 'resume_origin', None) or getattr(args, 'resume_account', None) != 'A'):
        raise ValueError('Resume requires --resume-origin and --resume-account A')
    if not resume_database and not args.reference_account:
        raise ValueError('New matrix requires --reference-account')
    paths = {'source_db': args.source.resolve(), 'source_wal': Path(str(args.source.resolve()) + '-wal'),
        'source_shm': Path(str(args.source.resolve()) + '-shm'), 'env': ROOT / '.env'}
    if resume_database:
        paths['prior_summary'] = args.resume_origin.resolve()
    preservation_before = preservation(paths)
    resume, origin_summary = None, None
    manager_effort = getattr(args, 'manager_effort', None)
    if resume_database:
        store, config, resume, origin_summary = resume_configuration(resume_database, args.resume_origin,
            args.source, args.resume_account, route=args.route, manager_effort=manager_effort)
        effort_override = resume['manager_effort_override']
    else:
        store, config = configure_isolated(args.source.resolve(), out / 'evaluation.sqlite3', args.reference_account, route=args.route)
        effort_override = apply_manager_effort(store, manager_effort)
    baseline = observations(store)
    if resume:
        write(out / 'prior-baseline.json', baseline)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    env = {**os.environ, 'APP_DB_PATH': str(store.path), 'APP_RUNTIME_DIR': str(out),
           'APP_MODE': 'live', 'APP_HOST': '127.0.0.1', 'APP_PORT': str(port)}
    summary = {'kind': 'seventeenth_normal_start_real_matrix', 'started_at': now(),
        'database_path': str(store.path.resolve()), 'resume': resume,
        'manager_effort_override': effort_override,
        'source_sha256': source_digest(), 'baseline_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
        'market_state_at_start': market_state(), 'route': config['model_network_route'],
        'roles': [{key: role.get(key) for key in ('role_id', 'version', 'resolved', 'config_error')}
                  for role in store.list_agent_roles()],
        'count_limits': {key: config[key] for key in COUNT_FIELDS},
        'http_request_limit': None, 'task_count_limit': None,
        'stop_on_new_stream_failure': bool(getattr(args, 'stop_on_new_stream_failure', False)),
        'execution_wall_limit_seconds': args.wall_seconds,
        'planned': ['A three production simulated buys', 'A one idempotent simulated sell',
                    'B cash plus 159278 and legitimate A/global universe-change jobs'],
        'attempt_policy': 'No HTTP/task/Agent-turn quota. Explicit failed-task checkpoint recovery uses a new production POST retry task; an unchanged completed-evidence fingerprint stops further recovery. Unknown partial responses are never replayed.',
        'formal_natural_generation': {'status': 'not_executed',
            'reason': 'Real calendar/window eligibility will be checked before enabling the original full window.'},
        'natural_maturity': 'not_yet_observed',
        'stages': [], 'trades': [], 'recoveries': [], 'scenario_outcomes': [], 'port': port, 'daily_database_writes': 0,
        'preservation_before': preservation_before}
    if resume and resume['resume_stage'] == 'after_sell':
        summary['scenario_outcomes'].append(copy.deepcopy(resume['inherited_initial_scenario']))
        summary['trades'].append({'operation_id': 'seventeenth-sell-once', 'receipt': resume['sale_receipt'],
            'already_recorded': True, 'source_summary': resume['origin_summary'],
            'action': 'verified_existing_trade_without_post'})
    started = False
    deadline = time.monotonic() + args.wall_seconds

    def save():
        summary['observations'] = observations(store)
        summary['request_accounting'] = request_accounting(summary['observations'], baseline,
            summary['source_sha256'], origin_summary, summary['started_at'])
        summary['source_unchanged'] = source_digest() == summary['source_sha256']
        write(out / 'summary.json', summary)

    async def manage(action):
        proc = await asyncio.create_subprocess_exec(sys.executable, str(ROOT / 'scripts/manage.py'), action,
            '--mode', 'live', '--port', str(port), cwd=ROOT, env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        stdout, _ = await proc.communicate()
        with (out / 'lifecycle.log').open('a') as log:
            log.write(now() + ' ' + action + '\n' + stdout.decode())
        (out / 'lifecycle.log').chmod(0o600)
        if proc.returncode:
            raise RuntimeError('manage.py ' + action + ' failed; see private lifecycle.log')

    async with httpx.AsyncClient(base_url=f'http://127.0.0.1:{port}', trust_env=False, timeout=90) as api:
        async def request(method, path, **kw):
            response = await api.request(method, path, **kw)
            if response.status_code >= 400:
                try:
                    body = response.json()
                except ValueError:
                    body = response.text
                failures = summary.setdefault('api_failures', [])
                failure = {'method': method, 'path': path, 'status': response.status_code,
                    'body': body}
                failures.append(failure)
                write(out / f'api-failure-{len(failures)}.json', failure)
                raise RuntimeError(f'{method} {path}: {response.status_code}')
            return response.json()

        def check_budget():
            snap = observations(store)
            if time.monotonic() >= deadline:
                raise RuntimeError('Declared wall deadline reached; remaining work preserved by normal shutdown')
            if source_digest() != summary['source_sha256']:
                raise RuntimeError('Source changed during fixed-source run')
            return snap

        def stop_for_diagnostic(diagnostic, snap):
            diagnostic.update(remaining_work=pending_work(snap),
                shutdown_scope='Owner settled; other work is interrupted by diagnostic shutdown',
                observation_boundary='Verifier snapshot check, not an atomic transaction with the independent service; production retry guards remain authoritative')
            summary['diagnostic_stop'] = diagnostic
            write(out / 'diagnostic-stop.json', diagnostic)
            save()
            raise DiagnosticStreamStop(diagnostic['reason'])

        async def settle(label, account_id=None):
            begin = time.monotonic()
            quiet_since = None
            observation_failure_since = None
            diagnostic = None
            while True:
                snap = check_budget()
                work = pending_work(snap)
                diagnostic = diagnostic_stream_stop(snap, baseline, label) if summary['stop_on_new_stream_failure'] else None
                if diagnostic:
                    break  # Do not wait for unrelated running jobs or pending demands.
                # A queued/running DB row alone is not a live-process handle.
                # Poll the normal health endpoint without replaying any model request.
                try:
                    health = await request('GET', '/api/health')
                    observation_failure_since = None
                except httpx.HTTPError as exc:
                    observation_failure_since = observation_failure_since or time.monotonic()
                    summary.setdefault('observation_errors', []).append({'at': now(), 'type': type(exc).__name__,
                        'action': 'repoll_same_instance_without_restarting_or_replaying'})
                    save()
                    if time.monotonic() - observation_failure_since >= args.observation_timeout_seconds:
                        raise RuntimeError('Local observation deadline reached; model outcome remains unknown') from exc
                    await asyncio.sleep(2)
                    continue
                if not health.get('worker_running'):
                    raise RuntimeError('Isolated worker is not running; no unknown request was replayed')
                if not work['busy'] and not work['pending']:
                    quiet_since = quiet_since or time.monotonic()
                    if time.monotonic() - quiet_since >= 5:
                        break
                else:
                    quiet_since = None
                save()
                await asyncio.sleep(2)
            stage = {'label': label, 'at': now(), 'duration_seconds': round(time.monotonic()-begin, 3),
                     'account_id': account_id, 'observations': snap,
                     'remaining_work': work,
                     'settled_status': 'owner_settled_other_work_interrupted_by_diagnostic_shutdown' if diagnostic else
                        'blocked_or_deferred' if work['blocked'] or work['deferred'] else 'quiescent'}
            if account_id and not diagnostic:
                stage['account'] = await request('GET', '/api/accounts/' + account_id)
                stage['ledger'] = await request('GET', f'/api/accounts/{account_id}/ledger')
                stage['reports'] = await request('GET', f'/api/accounts/{account_id}/analyses')
                current = [report for report in stage['reports'] if report['status'] == 'current'
                           and report['input_version'] == stage['account']['version']]
                stage['publication'] = {'status': 'pass' if len(current) == 1 and
                    current[0]['result'].get('delivery', {}).get('contract_verified') is True else 'fail',
                    'current_version': stage['account']['version'], 'current_report_ids': [r['id'] for r in current],
                    'content_assessment': 'not_evaluated'}
                for report in stage['reports']:
                    report_path = out / (label + '-' + report['id'] + '-original.md')
                    report_path.write_bytes(report['report'].encode('utf-8'))
                    report_path.chmod(0o600)
            summary['stages'].append(stage)
            save()
            print(json.dumps({'stage': label, 'http': snap['http_attempts'], 'reports': len(snap['reports']),
                              'jobs': [(j['state'], j['reason']) for j in snap['jobs']]}, ensure_ascii=False), flush=True)
            if diagnostic:
                stop_for_diagnostic(diagnostic, snap)
            if work['blocked']:
                raise RuntimeError('Production task requires configuration or scope repair; matrix stopped without more model requests')
            return stage

        attempted_recovery_evidence = set()

        async def recover_if_useful(stage):
            """New business task from completed evidence, never a wire replay."""
            first_stage = stage
            account_id = stage.get('account_id')
            while account_id and stage.get('publication', {}).get('status') != 'pass':
                account = await request('GET', '/api/accounts/' + account_id)
                parent = await request('GET', '/api/jobs/' + account['latest_job_id'])
                if parent['state'] != 'failed':
                    break
                diagnostic = store.checkpoint_recovery_diagnostic(parent)
                with store.connect() as db:
                    row = db.execute('SELECT body FROM synthesis_checkpoints WHERE job_id=?', (parent['id'],)).fetchone()
                checkpoint = json.loads(row[0]) if row else None
                record = {'parent_job_id': parent['id'], 'account_id': account_id, 'at': now(),
                    'preflight': diagnostic, 'checkpoint_untouched': True,
                    'prior_task_terminal_state': parent['state']}
                observed = check_budget()
                new_stream_failure = diagnostic_stream_stop(observed, baseline, 'retry_preflight') if summary['stop_on_new_stream_failure'] else None
                if new_stream_failure:
                    stop_for_diagnostic(new_stream_failure, observed)
                unavailable = (not checkpoint or diagnostic['state'] in ('invalidated', 'no_checkpoint') or
                    not parent.get('synthesis_checkpoint', {}).get('completed_research_available'))
                fresh = unavailable and fresh_resume_allowed(parent, account, observed, baseline, resume)
                if unavailable and not fresh:
                    record.update(action='not_retried', reason='no_current_completed_evidence_for_safe_recovery')
                    summary['recoveries'].append(record)
                    break
                if fresh:
                    record.update(action='independent_production_fresh',
                        reason='explicit_resume_after_interruption_limit_without_reusable_checkpoint',
                        intended_input_version=account['version'], unknown_partial_response_replayed=False)
                else:
                    progress = (account_id, account['version'], recovery_progress(checkpoint))
                    if progress in attempted_recovery_evidence:
                        record.update(action='not_retried', reason='repeated_no_new_completed_evidence')
                        summary['recoveries'].append(record)
                        break
                    attempted_recovery_evidence.add(progress)
                    record.update(action='independent_production_retry', completed_evidence_sha256=progress[2])
                # The Service independently rechecks quotes/materials/version
                # and chooses fresh/continue/synthesis_only after this POST.
                # Refresh after awaited account/job reads, immediately before
                # POST. This observation boundary is not a cross-service lock.
                latest = check_budget()
                diagnostic_stop = diagnostic_stream_stop(latest, baseline, 'retry_preflight') if summary['stop_on_new_stream_failure'] else None
                if diagnostic_stop:
                    stop_for_diagnostic(diagnostic_stop, latest)
                if fresh:
                    if not fresh_resume_allowed(parent, account, latest, baseline, resume):
                        record.update(action='not_retried', reason='fresh_resume_eligibility_changed')
                        summary['recoveries'].append(record)
                        break
                    result = await request('POST', f'/api/accounts/{account_id}/analyze', json={
                        'operation_id': 'seventeenth-resume-fresh-' + parent['id'],
                        'origin': {'page': 'accounts', 'action': 'analyze'}})
                else:
                    result = await request('POST', f"/api/jobs/{parent['id']}/retry", json={
                        'operation_id': 'seventeenth-recovery-' + parent['id'],
                        'origin': {'page': 'agents', 'action': 'retry-job'}})
                record['job_id'] = result['id']
                summary['recoveries'].append(record)
                stage = await settle(('fresh_' if fresh else 'recovery_') + result['id'], account_id)
                completed = await request('GET', '/api/jobs/' + result['id'])
                observed = recovery_events(all_events(store, result['id']))
                record.update(state=completed['state'], production_recovery_events=observed,
                    actual_recovery_mode=(completed.get('result') or {}).get('recovery_mode') or
                        (observed[-1].get('recovery_mode') if observed else 'fresh'),
                    actual_parent_job_id=completed.get('parent_job_id'))
                save()
            summary['scenario_outcomes'].append({'scenario': first_stage['label'], 'account_id': account_id,
                'first_publication': first_stage.get('publication'), 'final_publication': stage.get('publication'),
                'delivered_by': 'first_task' if first_stage is stage else
                    'independent_fresh_task' if stage['label'].startswith('fresh_') else 'independent_recovery_task',
                'last_stage': stage['label']})
            save()
            return stage

        async def enable_formal_when_full_window_available(account_id):
            rule = await request('GET', f'/api/accounts/{account_id}/forecast-settings')
            plan = formal_window_plan(market_state(), deadline - time.monotonic(), rule)
            summary['formal_natural_generation'].setdefault('accounts', {})[account_id] = plan
            if plan['status'] == 'eligible_to_enable_before_window':
                saved = await request('PUT', f'/api/accounts/{account_id}/forecast-settings', json={'enabled': True})
                plan.update(status='enabled_before_original_window', saved_rule=saved)
            save()

        async def trade(account_id, values, operation):
            with store.connect() as db:
                old_receipt = saved_trade_receipt(db, account_id, operation, values)
            if old_receipt is not None:
                summary['trades'].append({'operation_id': operation, 'receipt': old_receipt,
                    'action': 'existing_identical_operation_not_replayed', 'idempotent_replay_equal': True})
                save()
                return
            base = f'/api/accounts/{account_id}'
            account = await request('GET', base)
            fees = await request('GET', base + '/fee-plan')
            payload = {**values, 'operation_id': operation, 'expected_version': account['version'],
                'fee_plan_version': fees['version'], 'mode': 'simulated', 'traded_at': now(),
                'note': '第十七轮独立验收模拟算术成交，不是市场报价或券商成交'}
            preview = await request('POST', base + '/trades/preview', json=payload)
            receipt = await request('POST', base + '/trades', json=payload)
            replay = await request('POST', base + '/trades', json=payload)
            assert replay.get('replayed') is True
            assert {k: v for k, v in receipt.items() if k != 'replayed'} == {k: v for k, v in replay.items() if k != 'replayed'}
            assert preview['cash_after'] == receipt['cash_after']
            summary['trades'].append({'input': payload, 'receipt': receipt, 'idempotent_replay_equal': True})
            save()

        try:
            await manage('start')
            started = True
            summary['health'] = await request('GET', '/api/health')
            assert summary['health']['runtime_version']['source_sha256_at_start'] == summary['source_sha256']
            summary['symbol_resolution'] = await request('POST', '/api/securities/resolve',
                json={'symbols': ['518880', '002354', '159599', '159278']})
            if not summary['symbol_resolution']['ok']:
                raise RuntimeError('Real security identity unresolved')
            # Public API choice is persisted/read back; no custom client transport.
            settings = await request('GET', '/api/settings')
            assert all(settings[key] is None for key in COUNT_FIELDS)
            summary['network_setting'] = {k: settings.get(k) for k in ('model_network_route', 'model_network_revision', 'max_output_tokens', *COUNT_FIELDS)}
            if resume:
                a = await request('GET', '/api/accounts/' + resume['a_id'])
            else:
                a = await request('POST', '/api/accounts', headers={'X-Operation-ID': 'seventeenth-create-a'}, json={
                    'name': '第十七轮独立模拟A', 'cash': '100000', 'holdings': [],
                    'preferences': {'horizon': '关注518880、002354、159599；结合真实资料分析仓位现金、逐项动作、反向因素和再评估条件。成交为独立模拟输入；建议不执行交易。'}})
            summary['a_id'] = a['id']
            await enable_formal_when_full_window_available(a['id'])
            if not resume:
                for i, (symbol, quantity, price) in enumerate(INITIAL_BUYS):
                    await trade(a['id'], {'symbol': symbol, 'side': 'buy', 'quantity': quantity, 'price': price}, f'seventeenth-buy-{i}')
            if settings.get('debounce_seconds') != 0 or settings.get('scheduled_research_enabled') is not True:
                await request('PUT', '/api/settings', json={'debounce_seconds': 0, 'scheduled_research_enabled': True})
            if not resume or resume['resume_stage'] == 'before_sale':
                await recover_if_useful(await settle('a_initial', a['id']))
                await trade(a['id'], {'symbol': 'SH:518880', 'side': 'sell', 'quantity': '100', 'price': '11'}, 'seventeenth-sell-once')
            await recover_if_useful(await settle('a_after_sell', a['id']))
            # Build B before changing the shared universe: both accounts must
            # follow ordinary input invalidation and public-material routing.
            if resume and resume.get('b_id'):
                b = await request('GET', '/api/accounts/' + resume['b_id'])
            else:
                b = await request('POST', '/api/accounts', headers={'X-Operation-ID': 'seventeenth-create-b'}, json={
                    'name': '第十七轮独立现金B', 'cash': '100000', 'holdings': [],
                    'preferences': {'horizon': '只考虑159278是否适配本账户；自主决定是否研究/派发。给出持币或投入观察条件、反向因素与再评估条件。不得借用其他账户结论。'}})
            summary['b_id'] = b['id']
            await enable_formal_when_full_window_available(b['id'])
            await request('PUT', '/api/universe', json={'watchlist': [], 'candidates': ['159278']})
            await recover_if_useful(await settle('b_cash_and_universe_events', b['id']))
            formal_plans = list(summary['formal_natural_generation'].get('accounts', {}).values())
            enabled = [plan for plan in formal_plans if plan['status'] == 'enabled_before_original_window']
            if enabled:
                until = max(datetime.fromisoformat(plan['deadline']) for plan in enabled)
                while datetime.fromisoformat(now()) < until:
                    check_budget()
                    await request('GET', '/api/health')
                    save()
                    await asyncio.sleep(min(2, max(0, (until - datetime.fromisoformat(now())).total_seconds())))
                await settle('natural_formal_window')
            summary['formal_natural_generation']['status'] = 'original_window_observed' if enabled else 'not_covered'
            summary['formal_natural_generation']['batches'] = await request('GET', '/api/forecast-batches')
            summary['formal_natural_generation']['windows'] = await request('GET', '/api/forecast-window-runs')
            # Pause new scheduled research through the same user-facing API,
            # keep due scoring enabled, then verify durable publication restart.
            await request('PUT', '/api/settings', json={'scheduled_research_enabled': False})
            # Saving runtime settings is itself a legitimate account demand.
            # Drain it before the restart baseline; never mask or reject it.
            await settle('after_pause_configuration_events')
            restart_before = observations(store)
            await manage('stop')
            started = False
            await manage('start')
            started = True
            summary['restart_health'] = await request('GET', '/api/health')
            after = await settle('after_restart')
            summary['restart_preserved_reports'] = restart_before['reports'] == after['observations']['reports']
            summary['restart_preserved_forecasts'] = restart_before['forecasts'] == after['observations']['forecasts']
            summary['continuous_events'] = event_outcomes(after['observations'])
            delivered = len(summary['scenario_outcomes']) == 3 and all(
                item['final_publication']['status'] == 'pass' for item in summary['scenario_outcomes'])
            summary['matrix_delivery_verified'] = delivered and summary['restart_preserved_reports'] and summary['restart_preserved_forecasts']
            summary['execution_result'] = ('matrix_published_content_review_pending' if
                summary['matrix_delivery_verified'] and summary['continuous_events']['status'] == 'pass' else
                'matrix_delivered_with_recorded_failures_content_review_pending' if summary['matrix_delivery_verified'] else
                'matrix_incomplete_content_review_pending')
        except DiagnosticStreamStop as exc:
            summary['execution_result'] = 'diagnostic_stopped_after_stream_failure'
            summary['failure'] = {'type': type(exc).__name__, 'message': str(exc)}
        except Exception as exc:
            summary['execution_result'] = 'stopped_with_failure'
            summary['failure'] = {'type': type(exc).__name__, 'message': str(exc)}
            raise
        except asyncio.CancelledError:
            summary['execution_result'] = 'cancelled'
            summary['failure'] = {'type': 'CancelledError', 'message': 'User or process cancellation; no unfinished generation was replayed'}
            raise
        finally:
            if started:
                try:
                    await manage('stop')
                except Exception as exc:
                    summary['shutdown_failure'] = {'type': type(exc).__name__, 'message': str(exc)}
            summary['finished_at'] = now()
            summary['preservation_after'] = preservation(paths)
            summary['daily_files_unchanged'] = summary['preservation_after'] == preservation_before
            summary['file_preservation'] = preservation_comparison(preservation_before, summary['preservation_after'])
            save()
    return 3 if summary.get('diagnostic_stop') else 0 if summary.get('matrix_delivery_verified') else 2


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source', type=Path, required=True)
    p.add_argument('--reference-account', help='Required only for a new isolated matrix')
    p.add_argument('--resume-database', type=Path, help='Existing independent data/evaluations database; never the daily source')
    p.add_argument('--resume-origin', type=Path, help='Explicit previous terminal run summary.json for this database')
    p.add_argument('--resume-account', choices=['A'], help='Explicit matrix alias; never inferred from account names')
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--route', choices=['environment', 'direct'], default=None,
        help='Omit to preserve the selected saved route; never changes system proxies or CA settings')
    p.add_argument('--manager-effort', choices=['xhigh'], default=None,
        help='Explicit user override for the independent manager only; omitted preserves all selected efforts')
    p.add_argument('--wall-seconds', type=int, default=14400)
    p.add_argument('--observation-timeout-seconds', type=int, default=300)
    p.add_argument('--stop-on-new-stream-failure', action='store_true',
        help='After a new stream/transport failure settles, stop before another production retry; prior failures do not trigger')
    p.add_argument('--run-real', action='store_true')
    args = p.parse_args()
    if not args.run_real:
        p.error('Use --run-real only with authorization for the declared paid matrix')
    if args.resume_database:
        if not args.resume_origin or args.resume_account != 'A':
            p.error('--resume-database requires --resume-origin and --resume-account A')
    elif args.resume_origin or args.resume_account or not args.reference_account:
        p.error('New matrix requires --reference-account; resume options must be provided together')
    if args.wall_seconds <= 0 or args.observation_timeout_seconds <= 0:
        p.error('Non-count deadlines must be positive')
    raise SystemExit(asyncio.run(run(args)))


if __name__ == '__main__':
    main()
