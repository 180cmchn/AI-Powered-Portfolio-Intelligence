"""Resume verifier boundaries against isolated production Store/ledger records."""
import copy
import json
import sqlite3
import itertools
from types import SimpleNamespace

import httpx
from pathlib import Path

import pytest

from app.domain import AccountInput, now
from app.store import Store
from scripts import verify_seventeenth_live as verifier
from tests.helpers_sixth import configure_test_team


@pytest.fixture
def matrix(tmp_path, monkeypatch, frozen_clock):
    monkeypatch.setattr(verifier, 'ROOT', tmp_path)
    source = Store(tmp_path / 'data/live.sqlite3')
    directory = tmp_path / 'data/evaluations/prior'
    directory.mkdir(parents=True)
    store = Store(directory / 'evaluation.sqlite3')
    cfg = {**store.settings(), **dict.fromkeys(verifier.COUNT_FIELDS),
           'reasoning_effort': 'max', 'model_name': 'selected-model', 'model_network_route': 'direct',
           'max_output_tokens': None}
    store.set_meta('settings', cfg)
    configure_test_team(store, cfg)
    account = store.save_account_operation(AccountInput(name='Matrix A', cash='100000', holdings=[]),
                                           operation_id='seventeenth-create-a')
    for i, (symbol, quantity, price) in enumerate(verifier.INITIAL_BUYS):
        store.put_cache('securities', symbol, {'symbol': symbol, 'kind': 'stock' if symbol == 'SZ:002354' else 'etf',
            'name': '黄金ETF' if symbol == 'SH:518880' else '受控证券', 'status': 'ok', 'identity_status': 'confirmed'})
        store.record_trade(account['id'], {'operation_id': f'seventeenth-buy-{i}', 'symbol': symbol,
            'side': 'buy', 'quantity': quantity, 'price': price, 'mode': 'simulated', 'traded_at': now(),
            'expected_version': store.account(account['id'])['version'],
            'fee_plan_version': store.fee_plan(account['id'])['version']})
    origin = directory / 'summary.json'
    original = {'kind': 'seventeenth_normal_start_real_matrix', 'source_sha256': 'old-source',
        'a_id': account['id'], 'started_at': now(), 'finished_at': now(), 'execution_result': 'cancelled',
        'observations': {'requests': []}}
    origin.write_text(json.dumps(original))
    return source, store, account, origin


def resume(matrix, **changes):
    source, store, account, origin = matrix
    return verifier.resume_configuration(changes.get('database', store.path), changes.get('origin', origin),
        changes.get('source', source.path), changes.get('alias', 'A'), route=changes.get('route'),
        manager_effort=changes.get('manager_effort'))


def record_proven_sale(matrix):
    source, store, account, origin = matrix
    saved = store.record_trade(account['id'], {'symbol': 'SH:518880', 'side': 'sell',
        'quantity': '100', 'price': '11', 'operation_id': 'seventeenth-sell-once',
        'mode': 'simulated', 'traded_at': now(), 'expected_version': 4,
        'fee_plan_version': store.fee_plan(account['id'])['version']})
    prior = json.loads(origin.read_text())
    publication = {'status': 'fail', 'current_version': 4, 'current_report_ids': [],
                   'content_assessment': 'not_evaluated'}
    prior.update(trades=[{'receipt': saved}], scenario_outcomes=[{'scenario': 'a_initial',
        'account_id': account['id'], 'first_publication': publication, 'final_publication': publication,
        'delivered_by': 'first_task', 'last_stage': 'a_initial'}])
    origin.write_text(json.dumps(prior))
    return saved


@pytest.mark.parametrize('account_override', [False, True])
def test_explicit_manager_xhigh_preserves_source_other_roles_and_binding_identity(matrix, account_override):
    source, store, account, origin = matrix
    if account_override:
        store.set_account_agent_override(account['id'], 'manager', {'reasoning_effort': 'high'})
    source_before, origin_before = source.path.read_bytes(), origin.read_bytes()
    roles_before = {r['role_id']: r for r in store.list_agent_roles(include_archived=True)}
    ledger_before = store.ledger(account['id'])
    restored, config, proof, _ = resume(matrix, manager_effort='xhigh')
    roles_after = {r['role_id']: r for r in restored.list_agent_roles(include_archived=True)}
    assert all(roles_after[k] == roles_before[k] for k in roles_before if k != 'manager')
    assert roles_after['manager']['default_binding'] == {
        **roles_before['manager']['default_binding'], 'reasoning_effort': 'xhigh'}
    effective = next(r for r in restored.list_agent_roles(account['id']) if r['role_id'] == 'manager')
    assert effective['resolved']['reasoning_effort'] == 'xhigh'
    assert config == store.settings() and all(config[k] is None for k in verifier.COUNT_FIELDS)
    assert restored.ledger(account['id']) == ledger_before
    assert source.path.read_bytes() == source_before and origin.read_bytes() == origin_before
    assert proof['manager_effort_override']['source'] == 'explicit_cli_user_override'
    assert proof['manager_effort_override']['account_effort_override_changed'] is account_override


def test_proven_post_sale_resume_keeps_failed_v4_evidence_and_skips_trade(matrix):
    saved = record_proven_sale(matrix)
    source, store, account, origin = matrix
    before, source_before, origin_before = store.ledger(account['id']), source.path.read_bytes(), origin.read_bytes()
    restored, _, proof, _ = resume(matrix)
    assert proof['resume_stage'] == 'after_sell' and proof['sale_receipt'] == saved
    assert proof['inherited_initial_scenario']['final_publication']['status'] == 'fail'
    assert proof['inherited_initial_scenario']['final_publication']['current_version'] == 4
    assert proof['inherited_initial_scenario']['inherited_from']['summary'] == str(origin)
    assert proof['restart_input_version'] == 5
    assert restored.ledger(account['id']) == before
    assert source.path.read_bytes() == source_before and origin.read_bytes() == origin_before


def test_manager_override_is_bound_by_normal_next_claim_without_rewriting_prior_snapshot(matrix, frozen_clock):
    source, store, account, origin = matrix
    frozen_clock.advance(seconds=store.settings()['debounce_seconds'] + 1)
    prior = store.claim_jobs(account_id=account['id'])[0]
    with store.connect() as db:
        frozen_before = db.execute('SELECT config_snapshot FROM jobs WHERE id=?', (prior['id'],)).fetchone()[0]
    restored, _, _, _ = resume(matrix, manager_effort='xhigh')
    with restored.connect() as db:
        assert db.execute('SELECT config_snapshot FROM jobs WHERE id=?', (prior['id'],)).fetchone()[0] == frozen_before
    restored.recover()
    current = restored.claim_jobs(account_id=account['id'])[0]
    roles = current['config_snapshot']['_agent_team']['roles']
    assert roles['manager']['resolved']['reasoning_effort'] == 'xhigh'
    assert all(r['resolved']['reasoning_effort'] == 'max' for k, r in roles.items() if k != 'manager')
    assert json.loads(frozen_before)['_agent_team']['roles']['manager']['resolved']['reasoning_effort'] == 'max'


@pytest.mark.parametrize('missing', ['sale', 'initial', 'initial_version', 'extra_trade'])
def test_post_sale_resume_rejects_missing_or_changed_origin_and_ledger_evidence(matrix, missing):
    record_proven_sale(matrix)
    source, store, account, origin = matrix
    prior = json.loads(origin.read_text())
    if missing == 'sale':
        prior['trades'] = []
    elif missing == 'initial':
        prior['scenario_outcomes'] = []
    elif missing == 'initial_version':
        prior['scenario_outcomes'][0]['final_publication']['current_version'] = 5
    else:
        store.record_trade(account['id'], {'symbol': 'SH:518880', 'side': 'sell', 'quantity': '100', 'price': '11',
            'operation_id': 'unrelated-sale', 'mode': 'simulated', 'traded_at': now(), 'expected_version': 5,
            'fee_plan_version': store.fee_plan(account['id'])['version']})
    origin.write_text(json.dumps(prior))
    with pytest.raises(ValueError, match='Post-sale resume'):
        resume(matrix)


@pytest.mark.parametrize('change', ['valid', 'new_http', 'unknown_http', 'parent', 'account_version',
                                   'latest_job', 'owner_running', 'fatal', 'new_task', 'not_exhausted'])
def test_fresh_resume_only_for_unexecuted_interruption_guard_on_current_original_task(change):
    parent = {'id': 'original', 'state': 'failed', 'recovery_attempts': 2,
              'error': '服务中断后重试次数已达上限，请手动重试'}
    account = {'version': 5, 'latest_job_id': 'original'}
    baseline = {'requests': [{'local_request_id': 'old', 'job_id': 'original', 'error': 'cancelled'}]}
    snapshot = {'jobs': [{'id': 'original', 'state': 'failed', 'input_version': 5}],
                'requests': copy.deepcopy(baseline['requests'])}
    proof = {'restart_job_id': 'original', 'restart_input_version': 5, 'interruption_recovery_limit': 2}
    if change in ('new_http', 'unknown_http'):
        snapshot['requests'].append({'job_id': 'original',
            'local_request_id': 'new' if change == 'new_http' else None, 'error': 'cancelled'})
    elif change in ('parent', 'new_task'):
        parent['id'] = 'a-new-task'
    elif change == 'account_version':
        account['version'] = 6
    elif change == 'latest_job':
        account['latest_job_id'] = 'replacement'
    elif change == 'owner_running':
        snapshot['jobs'][0]['state'] = 'running'
    elif change == 'fatal':
        parent['error'] = 'Unsupported model configuration'
    elif change == 'not_exhausted':
        parent['recovery_attempts'] = 1
    assert verifier.fresh_resume_allowed(parent, account, snapshot, baseline, proof) is (change == 'valid')


def test_resume_keeps_existing_ledger_choices_and_previous_evidence(matrix):
    source, store, account, origin = matrix
    source_before, old_summary = source.path.read_bytes(), origin.read_bytes()
    before = store.ledger(account['id'])
    restored, config, proof, original = resume(matrix)
    assert restored.path == store.path
    assert all(key in config and config[key] is None for key in verifier.COUNT_FIELDS)
    assert config['reasoning_effort'] == 'max' and config['max_output_tokens'] is None
    assert config['model_network_route'] == 'direct'
    assert restored.ledger(account['id']) == before
    assert restored.account(account['id'])['cash'] == '84484.90'
    assert restored.account(account['id'])['version'] == 4
    assert len(proof['initial_buy_receipts']) == 3
    assert proof['a_id'] == account['id'] and proof['b_id'] is None
    assert source.path.read_bytes() == source_before and origin.read_bytes() == old_summary
    assert original['execution_result'] == 'cancelled'
    assert proof['standard_store_initialization']['selected_bindings_unchanged'] is True


@pytest.mark.parametrize('kind', ['daily_direct', 'daily_hardlink', 'outside_evaluations', 'wrong_origin', 'nonterminal_origin', 'unknown_account'])
def test_resume_rejects_unproven_or_daily_database_identity(matrix, tmp_path, kind):
    source, store, account, origin = matrix
    args = {}
    if kind == 'daily_direct':
        args['source'] = store.path
    elif kind == 'daily_hardlink':
        alias = store.path.parent / 'source-hardlink.sqlite3'
        alias.hardlink_to(source.path)
        args['database'] = alias
    elif kind == 'outside_evaluations':
        args['database'] = source.path
    else:
        body = json.loads(origin.read_text())
        if kind == 'wrong_origin':
            body['database_path'] = str(source.path)
        elif kind == 'nonterminal_origin':
            body.pop('finished_at')
        else:
            body['a_id'] = 'never-guess-a-daily-account'
        origin.write_text(json.dumps(body))
    with pytest.raises(ValueError):
        resume(matrix, **args)


def test_resume_refuses_route_or_count_setting_changes(matrix):
    with pytest.raises(ValueError, match='route conflicts'):
        resume(matrix, route='environment')
    source, store, account, origin = matrix
    store.set_meta('settings', {**store.settings(), 'max_agent_turns': 12})
    with pytest.raises(ValueError, match='explicit null'):
        resume(matrix)
    assert store.settings()['max_agent_turns'] == 12


def test_saved_receipt_requires_matching_committed_ledger_evidence():
    # A malformed imported database must not make the verifier skip a real trade.
    with sqlite3.connect(':memory:') as db:
        db.execute('CREATE TABLE ledger_operations(account_id,operation_id,receipt)')
        db.execute('CREATE TABLE ledger_entries(id,account_id,kind,body)')
        expected = {'symbol': 'SH:518880', 'side': 'buy', 'quantity': '300', 'price': '10'}
        receipt = {'entry_id': 'missing-entry', 'entry': {**expected, 'mode': 'simulated'}}
        db.execute('INSERT INTO ledger_operations VALUES(?,?,?)', ('A', 'buy', json.dumps(receipt)))
        with pytest.raises(ValueError, match='committed ledger evidence'):
            verifier.saved_trade_receipt(db, 'A', 'buy', expected)


def test_later_sell_and_b_are_rejected_without_relabeling_v5_as_initial_v4(matrix):
    source, store, account, origin = matrix
    expected = {'symbol': 'SH:518880', 'side': 'sell', 'quantity': '100', 'price': '11'}
    saved = store.record_trade(account['id'], {**expected, 'operation_id': 'seventeenth-sell-once',
        'mode': 'simulated', 'traded_at': now(), 'expected_version': store.account(account['id'])['version'],
        'fee_plan_version': store.fee_plan(account['id'])['version']})
    b = store.save_account_operation(AccountInput(name='Matrix B', cash='100000', holdings=[]),
                                    operation_id='seventeenth-create-b')
    # An older summary may precede B, but the original matrix operation proves identity.
    with pytest.raises(ValueError, match='original three-buy A version'):
        resume(matrix)
    with store.connect() as db:
        assert verifier.saved_trade_receipt(db, account['id'], 'seventeenth-sell-once', expected) == saved
        with pytest.raises(ValueError, match='declared matrix trade'):
            verifier.saved_trade_receipt(db, account['id'], 'seventeenth-sell-once', {**expected, 'quantity': '200'})
        assert db.execute("SELECT count(*) FROM ledger_entries WHERE kind='trade'").fetchone()[0] == 4
    assert store.account(account['id'])['cash'] == '85579.90'


def test_second_resume_uses_explicit_origin_and_keeps_request_source_lineage(matrix):
    source, store, account, old_origin = matrix
    directory = old_origin.parent.parent / 'second'
    directory.mkdir()
    prior = json.loads(old_origin.read_text())
    prior.update(database_path=str(store.path), source_sha256='source-two')
    older = {'local_request_id': 'old-failed', 'error': 'RemoteProtocolError',
             'request_source_sha256': 'source-one', 'request_source_run': 'run-one'}
    second = {'local_request_id': 'second-success', 'usage': {'input_tokens': 100, 'input_tokens_details': {'cached_tokens': 50}}}
    prior['observations']['requests'] = [older, second]
    origin = directory / 'summary.json'
    origin.write_text(json.dumps(prior))
    resume(matrix, origin=origin)
    baseline = {'requests': [older, second, {'local_request_id': 'origin-not-captured', 'error': 'cancelled'}]}
    current = copy.deepcopy(baseline)
    current['requests'].append({'local_request_id': 'new-failed', 'error': 'ReadError'})
    result = verifier.request_accounting(current, baseline, 'source-three', prior, 'run-three')
    assert result['prior_baseline']['requests'] == 3 and result['prior_baseline']['failed_requests'] == 2
    assert result['current_run']['requests'] == result['current_run']['failed_requests'] == 1
    assert result['cumulative']['requests'] == 4 and result['cumulative']['failed_requests'] == 3
    assert [r['request_source_sha256'] for r in current['requests']] == ['source-one', 'source-two', None, 'source-three']
    assert result['prior_source_unknown_requests'] == 1


def test_recovery_events_read_service_top_level_and_runtime_payload():
    events = [{'type': 'research_resuming', 'recovery_mode': 'continue_research', 'checkpoint_job_id': 'parent'},
              {'type': 'research_resumed', 'payload': {'recovery_mode': 'continue_research', 'source_task_id': 'parent'}}]
    found = verifier.recovery_events(events)
    assert all(r['recovery_mode'] == 'continue_research' for r in found)
    assert found[0]['checkpoint_job_id'] == found[1]['source_task_id'] == 'parent'
    synthesis = verifier.recovery_events([
        {'type': 'synthesis_resuming', 'recovery_mode': 'synthesis_only'},
        {'type': 'synthesis_resumed', 'payload': {'source_task_id': 'parent'}}])
    assert synthesis[-1]['recovery_mode'] == 'synthesis_only'


def test_preservation_reports_metadata_difference_without_changing_raw_result():
    before = {'source_shm': {'exists': True, 'size': 12, 'sha256': 'same', 'mtime_ns': 1}}
    after = copy.deepcopy(before)
    after['source_shm']['mtime_ns'] = 2
    result = verifier.preservation_comparison(before, after)
    assert result['raw_equal'] is False and result['content_unchanged'] is True
    assert result['metadata_unchanged'] is False
    assert result['differences']['source_shm']['mtime_ns'] == {'before': 1, 'after': 2}


@pytest.mark.parametrize('error,code', [('RemoteProtocolError', 'upstream_transport_error'),
    ('ReadError', 'upstream_transport_error'), ('stream_missing_terminal', 'stream_missing_terminal')])
def test_diagnostic_stop_ignores_old_errors_and_waits_for_new_owner_to_settle(error, code):
    old = {'local_request_id': 'old-243', 'job_id': 'old', 'error': error, 'error_code': code}
    baseline = {'requests': [old]}
    snapshot = {'requests': [old], 'jobs': [{'id': 'old', 'state': 'failed'}], 'global_runs': []}
    assert verifier.diagnostic_stream_stop(snapshot, baseline, 'resumed') is None
    snapshot['requests'].append({'local_request_id': 'new-one', 'job_id': 'new', 'error': error, 'error_code': code})
    snapshot['jobs'].append({'id': 'new', 'state': 'running'})
    assert verifier.diagnostic_stream_stop(snapshot, baseline, 'resumed') is None
    snapshot['jobs'][-1]['state'] = 'failed'
    stop = verifier.diagnostic_stream_stop(snapshot, baseline, 'resumed')
    assert [row['local_request_id'] for row in stop['failures']] == ['new-one']
    assert stop['automatic_retry_started'] is False and stop['business_acceptance'] is False


def test_diagnostic_stop_does_not_treat_cancel_or_successful_tail_drain_as_stream_failure():
    snapshot = {'jobs': [{'id': 'job', 'state': 'completed'}], 'global_runs': [], 'requests': [
        {'local_request_id': 'cancelled', 'job_id': 'job', 'error': 'cancelled', 'cancelled_by': 'parent_failed'},
        {'local_request_id': 'valid-terminal', 'job_id': 'job', 'error': None,
         'stream_terminal': 'response.completed', 'stream_close_reason': 'terminal_tail_transport_error'}]}
    assert verifier.diagnostic_stream_stop(snapshot, {'requests': []}, 'complete') is None


@pytest.mark.asyncio
@pytest.mark.parametrize('code', ['invalid_stream_json', 'stream_sequence_invalid',
    'stream_response_identity_changed', 'stream_event_type_mismatch', 'stream_error'])
async def test_actual_model_post_sse_rejections_trigger_diagnostic_stop(code):
    from app.model import ModelClient, ModelError
    from tests.test_seventh_streaming import cfg, frame, response, sse
    wires = {
        'invalid_stream_json': b'data: {bad json}\n\n',
        'stream_sequence_invalid': frame('response.created', {'id': 'response-fixture'}, sequence=1)
            + frame('response.completed', response(), sequence=1),
        'stream_response_identity_changed': frame('response.created', {'id': 'one'})
            + frame('response.completed', response(id='two')),
        'stream_event_type_mismatch': frame('response.completed', response(), declared='response.failed'),
        'stream_error': frame('error', message='controlled SSE error'),
    }
    reply, _ = sse([wires[code]])
    client = ModelClient(cfg(), transport=httpx.MockTransport(lambda request: reply))
    try:
        with pytest.raises(ModelError):
            await client.complete(client.session('controlled', {}), [])
        recorded = client.requests[-1]
        assert recorded['request_accepted'] is True and recorded['error'] == code
        assert recorded['stream_close_reason'] == 'local_protocol_rejection'
        snapshot = {'jobs': [{'id': 'owner', 'state': 'failed'}], 'global_runs': [],
                    'requests': [{**recorded, 'job_id': 'owner'}]}
        stop = verifier.diagnostic_stream_stop(snapshot, {'requests': []}, 'local_sse_rejection')
        assert stop['failures'][0]['error_code'] == code
    finally:
        await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('boundary', ['initial_failure', 'other_work', 'sse_rejection',
                                    'pre_retry_failure', 'pre_retry_wall', 'pre_retry_source'])
async def test_resume_orchestrator_saves_diagnostic_stop_before_retry_without_rebuy(matrix, monkeypatch, boundary):
    """Controlled local API responses exercise the actual verifier run boundary."""
    source, store, account, origin = matrix
    old_summary = origin.read_bytes()
    before_ledger = store.ledger(account['id'])
    old = {'local_request_id': 'prior-failure', 'error': 'RemoteProtocolError', 'error_code': 'upstream_transport_error'}
    store.event(account['latest_job_id'], {'type': 'model_error', 'payload': {'requests': [old]}})
    actions, calls = [], []
    target_job_id = store.account(account['id'])['latest_job_id']
    late_boundary = False
    delayed = boundary.startswith('pre_retry_')
    stream_error = {'error': 'stream_missing_terminal', 'error_code': 'stream_missing_terminal'}
    if boundary == 'sse_rejection':
        from app.model import ModelClient, ModelProtocolError
        from tests.test_seventh_streaming import cfg, sse
        response, _ = sse([b'data: {bad json}\n\n'])
        client = ModelClient(cfg(), transport=httpx.MockTransport(lambda request: response))
        try:
            with pytest.raises(ModelProtocolError):
                await client.complete(client.session('controlled', {}), [])
            stream_error = copy.deepcopy(client.requests[-1])
            assert stream_error['error'] == 'invalid_stream_json'
            assert stream_error['stream_close_reason'] == 'local_protocol_rejection'
        finally:
            await client.aclose()
    if delayed:
        with store.connect() as db:
            db.execute('INSERT INTO synthesis_checkpoints(job_id,account_id,input_version,universe_version,body,guard,created_at) VALUES(?,?,?,?,?,?,?)',
                (target_job_id, account['id'], 4, 'controlled', json.dumps({'calculations': [{'result': 3}]}), 'controlled', now()))
        monkeypatch.setattr(Store, 'checkpoint_recovery_diagnostic', lambda self, parent: {'state': 'needs_research'})
    async def fake_process(*args, **kwargs):
        actions.append(args[2])
        class Process:
            returncode = 0
            async def communicate(self):
                return b'controlled lifecycle; no service process started', None
        return Process()
    monkeypatch.setattr(verifier.asyncio, 'create_subprocess_exec', fake_process)
    async def no_delay(*args):
        pass
    monkeypatch.setattr(verifier.asyncio, 'sleep', no_delay)
    ticks = itertools.count(0, 10)
    monkeypatch.setattr(verifier, 'time', SimpleNamespace(monotonic=lambda:
        10**9 if boundary == 'pre_retry_wall' and late_boundary else next(ticks)))
    monkeypatch.setattr(verifier, 'source_digest', lambda:
        'changed-source' if boundary == 'pre_retry_source' and late_boundary else 'new-source')
    monkeypatch.setattr(verifier.subprocess, 'check_output', lambda *args, **kwargs: 'controlled-commit')
    class ReservedFixturePort:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def bind(self, address): assert address == ('127.0.0.1', 0)
        def getsockname(self): return ('127.0.0.1', 18765)
    monkeypatch.setattr(verifier, 'socket', SimpleNamespace(socket=ReservedFixturePort))
    actual_observations = verifier.observations
    def controlled_observations(current_store):
        result = actual_observations(current_store)
        if actions:
            # The owning task has converged before the diagnostic boundary.
            for row in result['jobs']:
                if row['state'] in ('running', 'queued', 'waiting_scope'):
                    row['state'] = 'failed'
            if not delayed or late_boundary and boundary == 'pre_retry_failure':
                result['requests'].append({**stream_error, 'local_request_id': 'new-stream-failure', 'job_id': target_job_id})
            if boundary == 'other_work':
                result['global_runs'].append({'id': 'other-global-task', 'state': 'running', 'reason': 'legitimate later work'})
                result['demands'].append({'id': 'later-demand', 'account_id': None, 'state': 'pending', 'kind': 'public_update'})
        return result
    monkeypatch.setattr(verifier, 'observations', controlled_observations)
    monkeypatch.setattr(verifier, 'formal_window_plan', lambda *args: {'status': 'not_covered', 'reason': 'controlled_clock'})
    def handler(request):
        nonlocal late_boundary
        method, path = request.method, request.url.path
        calls.append((method, path))
        if path == '/api/health':
            value = {'worker_running': True, 'runtime_version': {'source_sha256_at_start': 'new-source'}}
        elif path == '/api/securities/resolve':
            value = {'ok': True}
        elif path == '/api/settings':
            value = store.settings()
            if method == 'PUT':
                value.update(json.loads(request.content))
                store.set_meta('settings', value)
        elif path == '/api/accounts/' + account['id']:
            value = store.account(account['id'])
        elif path.endswith('/forecast-settings'):
            from app.forecasts import DEFAULT_RULE
            value = DEFAULT_RULE
        elif path.endswith('/ledger'):
            value = store.ledger(account['id'])
        elif path.endswith('/analyses'):
            value = []  # No invented report or success is needed to stop diagnosis.
        elif path == '/api/jobs/' + target_job_id and delayed:
            value = {**store.job(target_job_id), 'state': 'failed',
                     'synthesis_checkpoint': {'completed_research_available': True}}
            late_boundary = True  # State changes during awaited GET, after settle's snapshot.
        else:
            raise AssertionError('Unexpected API mutation/read: ' + method + ' ' + path)
        return httpx.Response(200, json=value)
    actual_client = httpx.AsyncClient
    monkeypatch.setattr(verifier.httpx, 'AsyncClient', lambda **kwargs:
        actual_client(**kwargs, transport=httpx.MockTransport(handler)))
    output = origin.parent.parent / 'resumed'
    args = SimpleNamespace(source=source.path, reference_account=None, output=output, route=None,
        resume_database=store.path, resume_origin=origin, resume_account='A', wall_seconds=14400,
        observation_timeout_seconds=300, stop_on_new_stream_failure=True)
    if boundary in ('pre_retry_wall', 'pre_retry_source'):
        with pytest.raises(RuntimeError, match='wall deadline|Source changed'):
            await verifier.run(args)
    else:
        assert await verifier.run(args) == 3
    summary = json.loads((output / 'summary.json').read_text())
    assert summary['execution_result'] == ('stopped_with_failure' if boundary in ('pre_retry_wall', 'pre_retry_source')
                                           else 'diagnostic_stopped_after_stream_failure')
    assert summary['finished_at'] and summary['recoveries'] == []
    assert summary['request_accounting']['prior_baseline']['failed_requests'] == 1
    if boundary not in ('pre_retry_wall', 'pre_retry_source'):
        assert [r['local_request_id'] for r in summary['diagnostic_stop']['failures']] == ['new-stream-failure']
        assert summary['request_accounting']['current_run']['failed_requests'] == 1
        if delayed:
            assert summary['diagnostic_stop']['stage'] == 'retry_preflight'
    if boundary == 'other_work':
        assert summary['diagnostic_stop']['remaining_work']['busy'][0]['id'] == 'other-global-task'
        assert summary['diagnostic_stop']['remaining_work']['pending']
        assert summary['stages'][0]['settled_status'] == 'owner_settled_other_work_interrupted_by_diagnostic_shutdown'
        assert calls.count(('GET', '/api/health')) == 1  # Startup only; no wait for unrelated work.
    assert actions == ['start', 'stop']
    assert not any(method == 'POST' and (path == '/api/accounts' or '/trades' in path or path.endswith('/retry'))
                   for method, path in calls)
    assert store.ledger(account['id']) == before_ledger and origin.read_bytes() == old_summary


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['normal_start', 'guard_fresh', 'stream_before_fresh'])
async def test_post_sale_run_uses_current_v5_without_initial_or_trade_replay(matrix, monkeypatch, mode):
    """Exercise actual orchestration with local API substitutes and real task operations."""
    record_proven_sale(matrix)
    source, store, account, origin = matrix
    old_summary, source_before = origin.read_bytes(), source.path.read_bytes()
    ledger_before = store.ledger(account['id'])
    target = store.account(account['id'])['latest_job_id']
    actions, calls, fresh_jobs = [], [], []
    late_failure = False
    async def fake_process(*args, **kwargs):
        actions.append(args[2])
        if args[2] == 'start':
            manager = next(r for r in store.list_agent_roles() if r['role_id'] == 'manager')
            assert manager['resolved']['reasoning_effort'] == 'xhigh'
            with store.connect() as db:
                db.execute('UPDATE jobs SET state=?,recovery_attempts=?,error=? WHERE id=?',
                    ('completed' if mode == 'normal_start' else 'failed', 2,
                     '服务中断后重试次数已达上限，请手动重试', target))
        class Process:
            returncode = 0
            async def communicate(self):
                return b'controlled lifecycle only; no process started', None
        return Process()
    monkeypatch.setattr(verifier.asyncio, 'create_subprocess_exec', fake_process)
    async def no_delay(*args):
        pass
    monkeypatch.setattr(verifier.asyncio, 'sleep', no_delay)
    ticks = itertools.count(0, 10)
    monkeypatch.setattr(verifier, 'time', SimpleNamespace(monotonic=lambda: next(ticks)))
    monkeypatch.setattr(verifier, 'source_digest', lambda: 'xhigh-fixture-source')
    monkeypatch.setattr(verifier.subprocess, 'check_output', lambda *args, **kwargs: 'controlled-commit')
    class ReservedFixturePort:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def bind(self, address): assert address == ('127.0.0.1', 0)
        def getsockname(self): return ('127.0.0.1', 18765)
    monkeypatch.setattr(verifier, 'socket', SimpleNamespace(socket=ReservedFixturePort))
    monkeypatch.setattr(verifier, 'formal_window_plan', lambda *args: {'status': 'not_covered', 'reason': 'controlled_clock'})
    actual_observations = verifier.observations
    def controlled_observations(current_store):
        snap = actual_observations(current_store)
        for row in snap['jobs']:
            if row['id'] in fresh_jobs:
                row['state'] = 'completed'  # Local API publication fixture, not a real research result.
        if late_failure:
            snap['requests'].append({'local_request_id': 'new-stream-failure', 'job_id': target,
                'error': 'stream_missing_terminal', 'error_code': 'stream_missing_terminal'})
        return snap
    monkeypatch.setattr(verifier, 'observations', controlled_observations)
    def handler(request):
        nonlocal late_failure
        method, path = request.method, request.url.path
        calls.append((method, path))
        if path == '/api/health':
            value = {'worker_running': True, 'runtime_version': {'source_sha256_at_start': 'xhigh-fixture-source'}}
        elif path == '/api/securities/resolve':
            value = {'ok': True}
        elif path == '/api/settings':
            value = store.settings()
            if method == 'PUT':
                value.update(json.loads(request.content)); store.set_meta('settings', value)
        elif path == '/api/accounts/' + account['id']:
            value = store.account(account['id'])
        elif path.endswith('/forecast-settings'):
            from app.forecasts import DEFAULT_RULE
            value = DEFAULT_RULE
        elif path.endswith('/ledger'):
            value = store.ledger(account['id'])
        elif path.endswith('/analyses'):
            value = [{'id': 'controlled-v5-report', 'status': 'current', 'input_version': 5,
                      'report': 'Controlled v5 API response, not a supplier result.',
                      'result': {'delivery': {'contract_verified': True}}}] if fresh_jobs or mode == 'normal_start' else []
        elif path == '/api/jobs/' + target:
            value = store.job(target)
            late_failure = mode == 'stream_before_fresh'
        elif path == '/api/accounts/' + account['id'] + '/analyze':
            payload = json.loads(request.content)
            value = store.submit_task(account['id'], operation_id=payload['operation_id'], origin=payload['origin'])
            repeated = store.submit_task(account['id'], operation_id=payload['operation_id'], origin=payload['origin'])
            assert repeated['id'] == value['id'] and repeated['input_version'] == 5
            assert value['id'] != target and value['recovery_attempts'] == 0
            fresh_jobs.append(value['id'])
        elif path.startswith('/api/jobs/') and path.rsplit('/', 1)[-1] in fresh_jobs:
            value = {**store.job(path.rsplit('/', 1)[-1]), 'state': 'completed'}
        elif path == '/api/accounts' and method == 'POST':
            assert json.loads(request.content)['name'] == '第十七轮独立现金B'
            raise RuntimeError('Controlled boundary before B; no additional scenario executed')
        else:
            raise AssertionError('Unexpected API operation: ' + method + ' ' + path)
        return httpx.Response(200, json=value)
    actual_client = httpx.AsyncClient
    monkeypatch.setattr(verifier.httpx, 'AsyncClient', lambda **kwargs:
        actual_client(**kwargs, transport=httpx.MockTransport(handler)))
    output = origin.parent.parent / 'after-sale-resume'
    args = SimpleNamespace(source=source.path, reference_account=None, output=output, route=None,
        resume_database=store.path, resume_origin=origin, resume_account='A', manager_effort='xhigh',
        wall_seconds=14400, observation_timeout_seconds=300, stop_on_new_stream_failure=True)
    if mode == 'stream_before_fresh':
        assert await verifier.run(args) == 3
    else:
        with pytest.raises(RuntimeError, match='Controlled boundary before B'):
            await verifier.run(args)
    result = json.loads((output / 'summary.json').read_text())
    assert result['resume']['resume_stage'] == 'after_sell'
    assert all(stage['label'] != 'a_initial' for stage in result['stages'])
    initial = result['scenario_outcomes'][0]
    assert initial['final_publication']['status'] == 'fail' and initial['final_publication']['current_version'] == 4
    assert initial['inherited_from']['summary'] == str(origin)
    assert result['trades'][0]['action'] == 'verified_existing_trade_without_post'
    assert not any('/trades' in path for method, path in calls)
    assert len(fresh_jobs) == (1 if mode == 'guard_fresh' else 0)
    if mode == 'guard_fresh':
        assert result['recoveries'][0]['action'] == 'independent_production_fresh'
        assert result['recoveries'][0]['actual_recovery_mode'] == 'fresh'
        assert result['recoveries'][0]['actual_parent_job_id'] is None
        assert result['scenario_outcomes'][1]['delivered_by'] == 'independent_fresh_task'
    if mode == 'stream_before_fresh':
        assert result['diagnostic_stop']['stage'] == 'retry_preflight'
        assert not any(method == 'POST' and (path.endswith('/analyze') or path.endswith('/retry') or path == '/api/accounts') for method,path in calls)
    assert actions == ['start', 'stop']
    assert store.ledger(account['id']) == ledger_before and origin.read_bytes() == old_summary
    assert source.path.read_bytes() == source_before
