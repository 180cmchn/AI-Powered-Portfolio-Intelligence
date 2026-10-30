"""Real HTTP/SQLite submission identity, frozen scope and delivery semantics."""
from ledger_helpers import snapshot_payload
import json
import sqlite3
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app.domain import AccountInput
from app.main import create_app
from app.service import Service
from app.store import Store
from app.domain import TZ


@pytest.fixture
def app(tmp_path):
    app = create_app(tmp_path / 'tasks.sqlite3', worker=False)
    app.state.store.set_meta('settings', {'quote_provider': 'manual', 'debounce_seconds': 0,
        'scheduled_research_enabled': False, 'market_refresh_enabled': False,
        'forecast_evaluation_enabled': False})
    return app


def account(client, name='A', holdings=True):
    response = client.post('/api/accounts', json={'name': name, 'cash': '10000', 'holdings':
        [{'symbol': 'SZ:159278', 'quantity': '10000'}] if holdings else []})
    assert response.status_code == 201, response.text
    return response.json()


def test_submit_receipt_survives_response_loss_restart_and_account_switch(app):
    with TestClient(app) as c:
        a, b = account(c), account(c, 'B')
        body = {'operation_id': 'intent-receipt-1', 'origin': {'page': 'overview', 'action': 'analyze'}}
        first = c.post(f"/api/accounts/{a['id']}/analyze", json=body).json()
        assert first['state'] == 'queued' and first['merged']
        assert first['id'] == a['latest_job_id']
        assert first['target']['account_name'] == 'A' and first['target']['holdings'][0]['quantity'] == '10000'
        assert first['stage'] == 'waiting' and first['started_at'] is None
        # Duplicate POST is safe even after the first response was never seen.
        assert c.post(f"/api/accounts/{a['id']}/analyze", json=body).json()['id'] == first['id']
        assert len(c.get(f"/api/accounts/{a['id']}/jobs").json()) == 1
        assert c.get('/api/task-operations/intent-receipt-1', params={'account_id': b['id']}).status_code == 404
        altered = {**body, 'symbol': 'SH:518880'}
        assert c.post(f"/api/accounts/{a['id']}/analyze", json=altered).status_code == 409
        restarted = Store(app.state.store.path)
        assert restarted.task_operation(a['id'], body['operation_id'])['id'] == first['id']


def test_scope_freezes_at_submission_and_retry_uses_current_version(app):
    with TestClient(app) as c:
        a = account(c)
        first = c.post(f"/api/accounts/{a['id']}/analyze", json={'symbol': 'SH:518880', 'operation_id':'security-1'}).json()
        assert first['task_kind'] == 'security_research'
        app.state.store.set_meta('universe', {'watchlist':['SH:688825'], 'candidates':['SH:562500']})
        job = c.get('/api/jobs/' + first['id']).json()
        assert job['universe']['candidates'] == []
        changed = c.put(f"/api/accounts/{a['id']}", json=snapshot_payload({'name':'A','cash':'10000', 'expected_version':1,
                     'holdings':[{'symbol':'SZ:159278','quantity':'20000'}]})).json()
        retried = c.post('/api/jobs/' + first['id'] + '/retry', json={'operation_id':'retry-current-1'}).json()
        assert retried['parent_job_id'] == first['id'] and retried['task_kind'] == 'manual_retry'
        assert retried['input_version'] == 2 and retried['target']['holdings'][0]['quantity'] == '20000'
        assert changed['cash'] == '10000.00' and retried['target']['candidates'] == ['SH:562500']


def test_empty_scope_requires_explicit_choice_and_does_not_run(app):
    with TestClient(app) as c:
        a = account(c, holdings=False)
        assert a['analysis_state'] == 'waiting_scope' and not app.state.store.claim_jobs()
        implicit = c.post(f"/api/accounts/{a['id']}/analyze", json={'operation_id':'empty-intent'}).json()
        assert implicit['state'] == 'waiting_scope'
        explicit = c.post(f"/api/accounts/{a['id']}/analyze", json={'operation_id':'cash-intent','research_scope':'cash_only'}).json()
        assert explicit['state'] == 'queued' and explicit['target']['symbols'] == []
        assert len(app.state.store.claim_jobs()) == 1


def test_report_delivery_and_checkpoint_are_independent(app):
    with TestClient(app) as c:
        a = account(c)
        job = app.state.store.claim_jobs()[0]
        with pytest.raises(ValueError, match='最终报告为空'):
            app.state.store.publish(job['id'], {'agents':[{'status':'completed'}]}, None)
        assert app.state.store.job(job['id'])['state'] == 'running'
        app.state.store.publish(job['id'], {'report':'模型原报告：维持，条件为……', 'agents':[{'status':'failed'}]}, None)
        viewed = c.get('/api/jobs/' + job['id']).json()
        assert viewed['state'] == 'completed' and viewed['report_status'] == 'current'
        assert viewed['report_id'] and viewed['started_at'] and viewed['completed_at']
        assert c.get('/api/analyses/' + viewed['report_id']).json()['report'] == '模型原报告：维持，条件为……'


def test_configuration_waits_do_not_consume_restart_recovery_budget(app):
    s = app.state.store
    a = s.save_account(AccountInput(name='A', holdings=[{'symbol':'SZ:159278','quantity':'1'}]))
    for _ in range(4):
        job = s.claim_jobs()[0]
        s.set_job_state(job['id'], 'waiting_config', '配置缺失')
        with s.connect() as db:
            db.execute("UPDATE jobs SET state='queued' WHERE id=?", (job['id'],))
    assert s.job(a['latest_job_id'])['attempts'] == 4
    assert s.job(a['latest_job_id'])['recovery_attempts'] == 0
    s.claim_jobs()
    s.recover()
    assert s.job(a['latest_job_id'])['recovery_attempts'] == 1


def test_legacy_origin_unknown_and_v9_backup_preserved(app):
    s = app.state.store
    a = s.save_account(AccountInput(name='legacy'))
    with s.connect() as db:
        db.execute('UPDATE jobs SET task_metadata=NULL WHERE id=?', (a['latest_job_id'],))
        db.execute("UPDATE meta SET value='9' WHERE key='schema_version'")
    upgraded = Store(s.path)
    job = upgraded.job(a['latest_job_id'])
    assert job['trigger_source'] == 'unknown' and job['origin']['action'] == '历史来源未知'
    assert job['target']['candidates'] is None and job['target']['scope_unknown']
    with sqlite3.connect(str(s.path) + '.before-v10.bak') as db:
        assert db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == '9'
    assert Store(s.path).meta('schema_version') == 11


def test_account_create_receipt_double_click_and_response_loss(app):
    payload = {'name': '保存意图 A', 'cash': '10000',
               'holdings': [{'symbol': 'SZ:159278', 'quantity': '10000'}]}
    headers = {'X-Operation-ID': 'account-create-intent-1'}
    with TestClient(app) as c:
        with ThreadPoolExecutor(max_workers=2) as pool:
            replies = list(pool.map(lambda _: c.post('/api/accounts', json=payload, headers=headers), range(2)))
        assert all(reply.status_code == 201 for reply in replies)
        a, repeated = [reply.json() for reply in replies]
        assert a['id'] == repeated['id'] and a['version'] == repeated['version'] == 1
        assert a['latest_job_id'] == repeated['latest_job_id']
        assert len(c.get('/api/accounts').json()) == 1
        assert len(c.get(f"/api/accounts/{a['id']}/jobs").json()) == 1
        receipt = c.get('/api/account-operations/account-create-intent-1').json()
        assert receipt['account_id'] == a['id']
        assert receipt['input_version'] == 1 and receipt['job_id'] == a['latest_job_id']
        assert receipt['job']['target']['holdings'][0]['quantity'] == '10000'
        assert c.post('/api/accounts', json={**payload, 'cash': '0'}, headers=headers).status_code == 409
        assert c.put(f"/api/accounts/{a['id']}", json=snapshot_payload(payload), headers=headers).status_code == 409
        reopened = Store(app.state.store.path)
        assert reopened.account_operation('account-create-intent-1')['job_id'] == a['latest_job_id']


def test_account_update_receipt_keeps_original_version_and_job_after_newer_save(app):
    with TestClient(app) as c:
        a, b = account(c), account(c, 'B')
        payload = {'name': 'A', 'cash': '10000', 'expected_version': 1,
                   'holdings': [{'symbol': 'SZ:159278', 'quantity': '20000'}]}
        headers = {'X-Operation-ID': 'account-update-intent-1'}
        first = c.put(f"/api/accounts/{a['id']}", json=snapshot_payload(payload), headers=headers)
        assert first.status_code == 200
        saved = first.json()
        assert saved['version'] == 2 and saved['cash'] == '10000.00'
        repeated = c.put(f"/api/accounts/{a['id']}", json=snapshot_payload(payload), headers=headers)
        assert repeated.status_code == 200 and repeated.json()['version'] == 2
        assert repeated.json()['latest_job_id'] == saved['latest_job_id']
        assert c.put(f"/api/accounts/{b['id']}", json=snapshot_payload(payload), headers=headers).status_code == 409
        assert c.put(f"/api/accounts/{a['id']}", json=snapshot_payload({**payload, 'cash': '0'}), headers=headers).status_code == 409
        assert c.get(f"/api/accounts/{b['id']}").json()['version'] == 1
        newer = c.put(f"/api/accounts/{a['id']}", json=snapshot_payload({**payload, 'expected_version': 2,
            'holdings': [{'symbol': 'SZ:159278', 'quantity': '30000'}]}),
            headers={'X-Operation-ID': 'account-update-intent-2'}).json()
        assert newer['version'] == 3
        receipt = c.get('/api/account-operations/account-update-intent-1').json()
        assert receipt['input_version'] == 2 and receipt['job_id'] == saved['latest_job_id']
        assert receipt['job']['input_version'] == 2
        assert receipt['job']['target']['holdings'][0]['quantity'] == '20000'
        assert receipt['account']['version'] == 3, 'current account is separate from the original save receipt'
        assert receipt['job']['id'] != receipt['account']['latest_job_id']
        before_count = len(c.get(f"/api/accounts/{a['id']}/jobs").json())
        assert c.put(f"/api/accounts/{a['id']}", json=snapshot_payload(payload), headers=headers).status_code == 200
        assert c.get(f"/api/accounts/{a['id']}").json()['version'] == 3
        assert len(c.get(f"/api/accounts/{a['id']}/jobs").json()) == before_count


def test_restart_preserves_original_schedule_first_start_and_separate_attempt_start(app):
    s = app.state.store
    with TestClient(app) as c:
        a = account(c)
        original = c.get('/api/jobs/' + a['latest_job_id']).json()
        planned = original['scheduled_at']
        assert planned == original['current_due_at'] and original['started_at'] is None
        first = s.claim_jobs(account_id=a['id'])[0]
        assert first['started_at'] and first['attempt_started_at']
        assert first['attempt_started_at'] >= first['started_at']
        s.task_stage(first['id'], 'researching')
        s.recover()
        recovered = c.get('/api/jobs/' + first['id']).json()
        assert recovered['state'] == 'queued' and recovered['stage'] == 'waiting'
        assert recovered['scheduled_at'] == planned
        assert recovered['current_due_at'] > original['current_due_at']
        assert recovered['started_at'] == first['started_at']
        assert recovered['attempt_started_at'] == first['attempt_started_at']
        second = s.claim_jobs(account_id=a['id'])[0]
        assert second['started_at'] == first['started_at']
        assert second['attempt_started_at'] > first['attempt_started_at']
        assert second['scheduled_at'] == planned and second['recovery_attempts'] == 1
        assert second['attempts'] == first['attempts'] + 1 and second['completed_at'] is None
        assert any(event['type'] == 'service_recovered' for event in s.job(first['id'], events=True)['events'])
        s.publish(second['id'], {'report': '受控恢复后报告'}, None)
        done = c.get('/api/jobs/' + second['id']).json()
        assert done['completed_at'] >= second['attempt_started_at']
        assert done['started_at'] == first['started_at'] and done['scheduled_at'] == planned


def test_configuration_resume_changes_queue_stage_without_rewriting_schedule_or_recovery_count(app):
    from tests.helpers_sixth import configure_test_team
    s = app.state.store
    configure_test_team(s, {'model_name': 'tenth-config-fixture', 'reasoning_effort': 'max'})
    with TestClient(app) as c:
        a = account(c)
        first = s.claim_jobs(account_id=a['id'])[0]
        s.set_job_state(first['id'], 'waiting_config', '受控上游配置拒绝')
        assert c.get('/api/jobs/' + first['id']).json()['stage'] == 'waiting_config'
        s.update_agent_definition('manager', {'default_binding': {'reasoning_effort': 'high'}})
        app.state.service.resume_waiting_config()
        resumed = c.get('/api/jobs/' + first['id']).json()
        assert resumed['state'] == 'queued' and resumed['stage'] == 'waiting'
        assert resumed['scheduled_at'] == first['scheduled_at']
        assert resumed['started_at'] == first['started_at']
        assert resumed['recovery_attempts'] == 0
        second = s.claim_jobs(account_id=a['id'])[0]
        assert second['started_at'] == first['started_at']
        assert second['attempt_started_at'] > first['attempt_started_at']
        assert second['config_snapshot']['reasoning_effort'] == 'high'
        assert second['recovery_attempts'] == 0


def test_merged_business_demands_keep_distinct_sources_and_planned_trading_day(app):
    s = app.state.store
    with TestClient(app) as c:
        a, b = account(c), account(c, 'B')
        before = c.get('/api/jobs/' + a['latest_job_id']).json()
        keys = {'close_summary': f"close:{a['id']}:2026-10-30",
                'price_change': f"price:{a['id']}:SZ:159278:controlled-window",
                'public_information': f"material:{a['id']}:controlled-document"}
        for kind, event_key in keys.items():
            s.request_analysis(a['id'], kind, event_key, reason=kind)
        merged = c.post(f"/api/accounts/{a['id']}/analyze", json={
            'operation_id': 'merged-sources-intent', 'origin': {'page': 'overview', 'action': 'analyze'}}).json()
        assert merged['merged'] and merged['id'] == before['id']
        assert merged['created_at'] == before['created_at']
        assert merged['scheduled_at'] == before['scheduled_at']
        assert {demand['kind']: demand['event_key'] for demand in merged['demands']} == keys
        assert merged['trading_day'] == '2026-10-30'
        close = next(demand for demand in merged['demands'] if demand['kind'] == 'close_summary')
        assert close['trading_day'] == '2026-10-30'
        assert all('trading_day' not in demand for demand in merged['demands'] if demand['kind'] != 'close_summary')
        assert {source['trigger_source'] for source in merged['sources']} == {'save_holdings', 'user_click'}
        assert any(source.get('origin') == {'page': 'overview', 'action': 'analyze'} for source in merged['sources'])
        assert c.get('/api/jobs/' + b['latest_job_id']).json()['demands'] == []


def test_retry_with_operation_body_inherits_explicit_cash_scope(app):
    s = app.state.store
    with TestClient(app) as c:
        a = c.post('/api/accounts', json={'name': '现金重试', 'cash': '10000',
            'research_scope': 'cash_only'}).json()
        original = s.claim_jobs(account_id=a['id'])[0]
        assert original['research_scope'] == 'cash_only'
        s.set_job_state(original['id'], 'failed', '受控汇总失败')
        response = c.post('/api/jobs/' + original['id'] + '/retry', json={
            'operation_id': 'cash-only-retry-body', 'origin': {'page': 'agents', 'action': 'retry-job'}})
        assert response.status_code == 200
        retried = response.json()
        assert retried['research_scope'] == 'cash_only' and retried['state'] == 'queued'
        assert retried['parent_job_id'] == original['id']
        assert retried['target']['symbols'] == []
        assert len(s.claim_jobs(account_id=a['id'])) == 1


def test_retry_merged_into_newer_task_keeps_original_parent_in_source(app):
    s = app.state.store
    with TestClient(app) as c:
        a = account(c)
        original = s.claim_jobs(account_id=a['id'])[0]
        s.set_job_state(original['id'], 'failed', '受控旧任务失败')
        changed = c.put(f"/api/accounts/{a['id']}", json=snapshot_payload({'name': 'A', 'cash': '10000',
            'expected_version': 1, 'holdings': [{'symbol': 'SZ:159278', 'quantity': '20000'}]})).json()
        response = c.post('/api/jobs/' + original['id'] + '/retry', json={
            'operation_id': 'merged-parent-retry-body', 'origin': {'page': 'agents', 'action': 'retry-job'}})
        assert response.status_code == 200
        merged = response.json()
        assert merged['merged'] and merged['id'] == changed['latest_job_id']
        assert merged['input_version'] == 2 and merged['task_kind'] == 'holdings_update'
        assert merged['parent_job_id'] is None, 'the queued task keeps its original creation identity'
        retry_sources = [source for source in merged['sources'] if source.get('parent_job_id') == original['id']]
        assert len(retry_sources) == 1
        assert retry_sources[0]['origin'] == {'page': 'agents', 'action': 'retry-job'}
        assert c.get('/api/task-operations/merged-parent-retry-body', params={'account_id': a['id']}).json()['sources'] == merged['sources']


@pytest.mark.parametrize('with_holding', [True, False])
def test_single_security_api_worker_publishes_target_abstention_without_losing_report(app, monkeypatch, with_holding):
    """Real API/worker/ledger; only model output and forecast clock are fixtures."""
    from app import forecasts
    at = datetime(2026, 10, 30, 16, tzinfo=TZ)
    original_local = forecasts._local
    monkeypatch.setattr(forecasts, '_local', lambda value=None: original_local(at if value is None else value))
    observed = []
    async def runner(context, cfg, tools, emit):
        batch = await tools.list_forecast_targets()
        observed.append(batch)
        assert batch['kind'] == 'exploratory'
        assert {item['symbol'] for item in batch['items']} == {'SH:518880'}
        return {'report': '受控协议替身：本次研究明确目标黄金ETF，证据不足时保留观察并弃权。',
                'forecasts': [{'security_id': 'SH:518880', 'horizon': horizon,
                    'direction': 'abstain', 'reason': '受控测试缺少方向证据；不是实际投资判断'} for horizon in (1, 5, 20)]}
    app.state.service.runner = runner
    with TestClient(app) as c:
        a = account(c, holdings=with_holding)
        submitted = c.post(f"/api/accounts/{a['id']}/analyze", json={
            'symbol': 'SH:518880', 'operation_id': 'single-target-forecast-intent'}).json()
        job = app.state.store.claim_jobs(account_id=a['id'])[0]
        assert job['id'] == submitted['id']
        asyncio.run(app.state.service.execute(job))
        delivered = c.get('/api/jobs/' + job['id']).json()
        assert delivered['state'] == 'completed', delivered.get('error')
        assert delivered['report_id'] and delivered['report_status'] == 'current'
        report = c.get('/api/analyses/' + delivered['report_id']).json()
        assert report['report'].startswith('受控协议替身')
        records = app.state.store.forecasts_page(kind='exploratory', account_id=a['id'])['items']
        assert len(records) == 3
        assert {record['symbol'] for record in records} == {'SH:518880'}
        assert all(record['forecast_id'] and record['direction'] == 'abstain' for record in records)
        assert app.state.store.forecast_statistics(kind='formal')['expected'] == 0
        assert observed and app.state.store.account(a['id'])['cash'] == '10000'


def test_exploratory_account_scope_uses_frozen_candidates_and_ignores_watchlist(app):
    s = app.state.store
    s.set_meta('universe', {'watchlist': ['SH:688825'], 'candidates': ['SH:562500']})
    with TestClient(app) as c:
        a = account(c)
        submitted = c.post(f"/api/accounts/{a['id']}/analyze", json={'operation_id': 'frozen-exploratory-intent'}).json()
        assert submitted['target']['candidates'] == ['SH:562500']
        # Mutate only the live global value to prove preparation reads the job's
        # frozen input; the public save path additionally supersedes old jobs.
        s.set_meta('universe', {'watchlist': ['SZ:000001'], 'candidates': ['SH:518880']})
        batch = s.prepare_exploratory_forecast_job(submitted['id'], at=datetime(2026, 10, 30, 16, tzinfo=TZ))
        assert {item['symbol'] for item in batch['items']} == {'SZ:159278', 'SH:562500'}
        assert batch['input_version'] == submitted['input_version']
        assert batch['kind'] == 'exploratory' and batch['expected_count'] == 6


def test_superseded_input_cannot_prepare_exploration_from_new_account(app):
    s = app.state.store
    with TestClient(app) as c:
        a = account(c)
        old = s.claim_jobs(account_id=a['id'])[0]
        c.put(f"/api/accounts/{a['id']}", json=snapshot_payload({'name': 'A', 'cash': '10000', 'expected_version': 1,
            'holdings': [{'symbol': 'SH:518880', 'quantity': '100'}]}))
        assert s.job(old['id'])['state'] == 'superseded'
        assert s.prepare_exploratory_forecast_job(old['id'], at=datetime(2026, 10, 30, 16, tzinfo=TZ)) is None
        assert s.forecast_batches(account_id=a['id']) == []
