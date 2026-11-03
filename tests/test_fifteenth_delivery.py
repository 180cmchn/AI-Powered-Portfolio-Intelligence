"""Production ModelClient HTTP -> Agent -> Service -> SQLite -> API delivery checks.

All model text is a controlled fixture, never real content acceptance evidence.
"""
import json

import httpx
import pytest

from app.domain import AccountInput
from app.service import ResearchTools
from tests.test_fourteenth_chain import setup_app, execute, final, call, response
from tests.delivery_helpers import final_result

A, B = 'SZ:159599', 'SZ:002354'


def suggestion(sid=A, **extra):
    return {'security_id': sid, 'action': 'watch', 'basis': '受控依据，待条件满足后复核。', **extra}


def forecast(sid=A, **extra):
    return {'security_id': sid, 'horizon': 5, 'direction': 'abstain', 'reason': '受控输入不足以判断方向。', **extra}


def limited(**extra):
    return final('受控报告：维持观察；这里只验证交付流程。', completion='limited', scope=[A],
                 limitations=['B 未研究，不补造观点。'], **extra)


def securities_app(tmp_path, monkeypatch, handler, frozen_clock, *, batch=True):
    # Outside verified calendar coverage, the real Service cannot create an
    # exploratory batch; this is the legitimate no-batch production path.
    frozen_clock.set('2026-11-02T17:00:00+08:00' if batch else '2035-11-02T17:00:00+08:00')
    app, store, _ = setup_app(tmp_path, monkeypatch, handler)
    account = store.save_account(AccountInput(name='双证券受控账户', cash='1000', holdings=[
        {'symbol': A, 'quantity': '100', 'cost_price': '1'},
        {'symbol': B, 'quantity': '100', 'cost_price': '2'}]))
    if batch:
        row = store.create_forecast_batch(account['id'], kind='formal', at=frozen_clock.instant, symbols=[A, B])
        store.bind_forecast_job(row['id'], account['latest_job_id'])
    return app, store, account


@pytest.mark.asyncio
@pytest.mark.parametrize(('bad', 'path', 'batch'), [
    ({'valid_until': 'bad'}, 'valid_until', True),
    ({'valid_until': 42}, 'valid_until', True),
    ({'forecasts': [forecast(B)]}, 'forecasts[0].security_id', True),
    ({'forecasts': [forecast(horizon=7)]}, 'forecasts[0].horizon', True),
    ({'forecasts': [forecast(), forecast(direction='down')]}, 'forecasts[1]', True),
    ({'forecasts': [forecast()]}, 'forecasts[0]', False),
    ({'forecasts': [forecast(revises_id=['bad'])]}, 'forecasts[0].revises_id', True),
    ({'suggestions': [{'action': 'watch', 'basis': '有依据'}]}, 'suggestions[0].security_id', True),
    ({'suggestions': [suggestion(price_conditions=[{'operator': 'gte', 'value': 'NaN'}])]}, 'suggestions[0].price_conditions[0]', True),
    ({'suggestions': [suggestion(agent_conditions='bad')]}, 'suggestions[0].agent_conditions', True),
    ({'suggestions': [suggestion(valid_until='bad')]}, 'suggestions[0].valid_until', True),
    ({'suggestions': [suggestion(basis=' ')]}, 'suggestions[0].basis', True),
    ({'suggestions': [suggestion(B)]}, 'suggestions[0].security_id', True),
    ({'suggestions': [suggestion(id='foreign-or-missing')]}, 'suggestions[0].id', True),
    ({'suggestions': [suggestion(), suggestion(action='hold')]}, 'suggestions[1]', True),
    ({'suggestions': [suggestion()] * 101}, 'suggestions', True),
    ({'revoked_suggestion_ids': ['foreign-or-missing']}, 'revoked_suggestion_ids[0]', True),
])
async def test_fixable_errors_return_before_acceptance_and_commit_once(tmp_path, monkeypatch, frozen_clock, bad, path, batch):
    requests = []
    def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) == 1:
            return response(call('finish_analysis', limited(**bad), 'invalid'))
        # Feedback is available to the same manager, and preflight wrote nothing.
        assert path in json.dumps(payload, ensure_ascii=False)
        with store.connect() as db:
            assert db.execute('SELECT count(*) FROM analyses').fetchone()[0] == 0
            assert db.execute('SELECT count(*) FROM recommendations').fetchone()[0] == 0
            assert db.execute('SELECT count(*) FROM forecasts').fetchone()[0] == 0
        good = {'suggestions': [suggestion()]}
        if batch:
            good['forecasts'] = [forecast()]
        return response(call('finish_analysis', limited(**good), 'corrected'))
    app, store, account = securities_app(tmp_path, monkeypatch, handler, frozen_clock, batch=batch)
    saved, reports = await execute(app, store, account)
    assert saved['state'] == 'completed', saved.get('error')
    assert len(requests) == 2 and len(reports) == 1
    assert saved['submission_outcome'] == 'recovered' and saved['submission_corrections'] == 1
    receipt = saved['delivery_application']
    assert receipt['suggestions'][0]['status'] == 'applied'
    assert store.recommendations_page(account['id'])['items'][0]['security_id'] == A
    if batch:
        assert receipt['forecasts'][0]['status'] == 'applied'
        assert saved['forecast_coverage']['abstained'] == 1
        assert saved['forecast_coverage']['uncovered'] == 5
    result = reports[0]['result']
    assert store.publish(saved['id'], result, None)
    store.recover(config_ready=True)
    with store.connect() as db:
        assert db.execute('SELECT count(*) FROM analyses').fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM recommendations').fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM forecasts').fetchone()[0] == int(batch)


@pytest.mark.asyncio
async def test_missing_baseline_is_application_receipt_not_protocol_error(tmp_path, monkeypatch, frozen_clock):
    requests = []
    def handler(request):
        requests.append(request)
        return response(call('finish_analysis', limited(forecasts=[forecast(direction='down')], suggestions=[suggestion()])))
    app, store, account = securities_app(tmp_path, monkeypatch, handler, frozen_clock)
    saved, reports = await execute(app, store, account)
    assert saved['state'] == 'completed' and len(requests) == len(reports) == 1
    assert saved['delivery_application']['forecasts'][0]['status'] == 'waiting_data'
    assert saved['delivery_application']['suggestions'][0]['status'] == 'applied'
    assert saved['forecast_coverage']['waiting_data'] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['account_version', 'formal_cutoff'])
async def test_real_state_change_after_preflight_cannot_apply_and_retains_candidate(tmp_path, monkeypatch, frozen_clock, change):
    requests = []
    def handler(request):
        requests.append(request)
        return response(call('finish_analysis', limited(forecasts=[forecast()], suggestions=[suggestion()])))
    app, store, account = securities_app(tmp_path, monkeypatch, handler, frozen_clock)
    original = ResearchTools.resolve_references
    async def change_after_preflight(self, result):
        assert result['_delivery_preflight_job_id'] == self.job['id']
        resolved = await original(self, result)
        if change == 'formal_cutoff':
            frozen_clock.set('2026-11-02T18:00:01+08:00')
        else:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as api:
                payload = {
                    'operation_id': 'fifteenth-cas-change', 'expected_version': account['version'],
                    'reason': '受控回归：预检后明确校正现金', 'mode': 'snapshot', 'cash': '999',
                    'holdings': [{'symbol': A, 'quantity': '100', 'cost_price': '1'},
                                 {'symbol': B, 'quantity': '100', 'cost_price': '2'}]}
                base = '/api/accounts/' + account['id'] + '/corrections'
                preview = await api.post(base + '/preview', json=payload)
                assert preview.status_code == 200, preview.text
                changed = await api.post(base, json={**payload, 'preview_token': preview.json()['preview_token']})
                assert changed.status_code == 201, changed.text
        return resolved
    monkeypatch.setattr(ResearchTools, 'resolve_references', change_after_preflight)
    saved, reports = await execute(app, store, account)
    assert len(requests) == 1, [e.get('payload',{}).get('error') for e in store.job(saved['id'], events=True)['events'] if e['type'] == 'terminal_submission_rejected']
    assert saved['state'] == ('failed' if change == 'formal_cutoff' else 'superseded')
    assert saved['result']['report'].startswith('受控报告')
    for group in ('suggestions', 'forecasts'):
        assert saved['delivery_application'][group][0]['status'] == 'not_applied'
    if change == 'formal_cutoff':
        assert reports == [] and saved['error_code'] == 'forecast_deadline_exceeded'
    else:
        assert len(reports) == 1 and reports[0]['status'] == 'stale'
    with store.connect() as db:
        assert db.execute('SELECT count(*) FROM recommendations').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM forecasts').fetchone()[0] == 0


def test_account_scope_does_not_grant_arbitrary_security_and_preflight_is_readonly(tmp_path, monkeypatch, frozen_clock):
    app, store, account = securities_app(tmp_path, monkeypatch, lambda request: response(), frozen_clock)
    job = store.claim_jobs(account_id=account['id'])[0]
    from app.delivery import DeliveryContractError
    with pytest.raises(DeliveryContractError, match=r'suggestions\[0\].security_id'):
        store.preflight_delivery(job, final_result(final(scope=['account'], suggestions=[suggestion('SH:600519')])))
    before = store.job(job['id'])
    checked = store.preflight_delivery(job, final_result(limited(suggestions=[suggestion()])))
    assert checked['_delivery_preflight_job_id'] == job['id']
    assert store.job(job['id']) == before
    assert store.analyses(account['id']) == []
    assert store.recommendations_page(account['id'])['items'] == []


@pytest.mark.asyncio
async def test_commit_rechecks_formal_cutoff_after_service_guard(tmp_path, monkeypatch, frozen_clock):
    app, store, account = securities_app(tmp_path, monkeypatch,
        lambda request: response(call('finish_analysis', limited(forecasts=[forecast()], suggestions=[suggestion()]))), frozen_clock)
    publish = store.publish
    def cross_at_commit(job_id, result, data_as_of):
        assert result['_delivery_preflight_job_id'] == job_id
        frozen_clock.set('2026-11-02T18:00:01+08:00')
        return publish(job_id, result, data_as_of)
    monkeypatch.setattr(store, 'publish', cross_at_commit)
    saved, reports = await execute(app, store, account)
    assert saved['state'] == 'failed' and saved['error_code'] == 'forecast_deadline_exceeded'
    assert reports == [] and saved['result']['report'].startswith('受控报告')
    assert saved['delivery_application']['forecasts'][0]['status'] == 'not_applied'
    with store.connect() as db:
        assert db.execute('SELECT count(*) FROM recommendations').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM forecasts').fetchone()[0] == 0


@pytest.mark.asyncio
async def test_recommendation_expiring_after_preflight_is_conflict_and_preserves_old_report(tmp_path, monkeypatch, frozen_clock):
    requests = []
    def handler(request):
        requests.append(request)
        return response(call('finish_analysis', limited(suggestions=[suggestion(id=identifier)])))
    app, store, account = securities_app(tmp_path, monkeypatch, handler, frozen_clock)
    initial = store.claim_jobs(account_id=account['id'])[0]
    assert store.publish(initial['id'], final_result(limited(suggestions=[
        suggestion(valid_until='2026-11-02T17:00:01+08:00')])), None)
    identifier = store.recommendations_page(account['id'])['items'][0]['id']
    old_id = store.analyses(account['id'])[0]['id']
    store.enqueue(account['id'], 'manual')
    original = ResearchTools.resolve_references
    async def expire_after_check(self, result):
        assert result['_delivery_preflight_job_id'] == self.job['id']
        resolved = await original(self, result)
        frozen_clock.advance(seconds=2)
        store.review_recommendations(stamp=frozen_clock.instant)
        return resolved
    monkeypatch.setattr(ResearchTools, 'resolve_references', expire_after_check)
    saved, reports = await execute(app, store, account)
    assert len(requests) == 1 and saved['state'] == 'failed'
    assert saved['error_code'] == 'delivery_state_conflict'
    assert saved['delivery_application']['suggestions'][0]['status'] == 'not_applied'
    assert len(reports) == 1 and reports[0]['id'] == old_id and reports[0]['status'] == 'current'
    assert store.recommendation(account['id'], identifier)['state'] == 'expired'


@pytest.mark.asyncio
@pytest.mark.parametrize('malformed', [
    {'suggestions': [None, 'invalid'], 'forecasts': [7]},
    {'suggestions': None, 'forecasts': 'invalid', 'maintained_suggestion_ids': 4},
])
async def test_invalid_runner_candidate_keeps_receipt_without_masking_contract_failure(tmp_path, monkeypatch, frozen_clock, malformed):
    app, store, account = securities_app(tmp_path, monkeypatch, lambda request: response(), frozen_clock)
    async def runner(context, config, tools, emit):
        return final_result(limited(**malformed))
    app.state.service.runner = runner
    saved, reports = await execute(app, store, account)
    assert saved['state'] == 'failed' and saved['error_code'] == 'delivery_contract_failed'
    assert reports == [] and saved['result']['suggestions'] == malformed['suggestions']
    receipt = saved['delivery_application']
    for field in ('suggestions', 'forecasts'):
        assert receipt[field] and all(row['status'] == 'not_applied' for row in receipt[field])
        assert all(field in row['reason'] for row in receipt[field])


@pytest.mark.asyncio
@pytest.mark.parametrize('action_field', ['maintained_suggestion_ids', 'review_suggestion_ids'])
async def test_replacement_cannot_keep_or_review_replaced_id_and_explicit_revoke_is_valid(tmp_path, monkeypatch, frozen_clock, action_field):
    requests = []
    def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) == 1:
            return response(call('finish_analysis', limited(suggestions=[suggestion()], **{action_field: [identifier]})))
        feedback = json.dumps(payload, ensure_ascii=False)
        assert 'suggestions[0].security_id' in feedback and action_field in feedback
        # Rejected contradictory submission had no persistence side effects.
        assert store.recommendation(account['id'], identifier)['state'] == 'watching'
        assert len(store.analyses(account['id'])) == 1
        return response(call('finish_analysis', limited(suggestions=[suggestion()], revoked_suggestion_ids=[identifier])))
    app, store, account = securities_app(tmp_path, monkeypatch, handler, frozen_clock)
    initial = store.claim_jobs(account_id=account['id'])[0]
    assert store.publish(initial['id'], final_result(limited(suggestions=[suggestion()])), None)
    identifier = store.recommendations_page(account['id'])['items'][0]['id']
    store.enqueue(account['id'], 'manual')
    saved, reports = await execute(app, store, account)
    assert saved['state'] == 'completed', saved.get('error')
    assert len(requests) == 2 and saved['submission_corrections'] == 1
    receipt = saved['delivery_application']
    assert receipt['suggestions'][0]['status'] == 'applied'
    new_id = receipt['suggestions'][0]['recommendation_id']
    assert new_id != identifier
    assert receipt['suggestion_actions'] == [{'field': 'revoked_suggestion_ids', 'id': identifier,
        'status': 'applied', 'reason': '主 Agent 明确撤销'}]
    assert store.recommendation(account['id'], identifier)['state'] == 'revoked'
    assert store.recommendation(account['id'], new_id)['replaces_ids'] == [identifier]
    assert len(reports) == 2 and sum(row['status'] == 'current' for row in reports) == 1


@pytest.mark.asyncio
async def test_transaction_crossing_formal_cutoff_rolls_back_all_applied_writes(tmp_path, monkeypatch, frozen_clock):
    app, store, account = securities_app(tmp_path, monkeypatch,
        lambda request: response(call('finish_analysis', limited(forecasts=[forecast()], suggestions=[suggestion()]))), frozen_clock)
    save_suggestions = store._save_suggestions
    reached = []
    def cross_after_writes(db, job, result):
        save_suggestions(db, job, result)
        # Prove the transaction actually applied all three kinds before the
        # final boundary check, so zero rows afterwards demonstrates rollback.
        reached.append(tuple(db.execute(f'SELECT count(*) FROM {table}').fetchone()[0]
                             for table in ('analyses', 'forecasts', 'recommendations')))
        frozen_clock.set('2026-11-02T18:00:01+08:00')
    monkeypatch.setattr(store, '_save_suggestions', cross_after_writes)
    saved, reports = await execute(app, store, account)
    assert reached == [(1, 1, 1)]
    assert saved['state'] == 'failed' and saved['error_code'] == 'forecast_deadline_exceeded'
    assert reports == [] and saved['result']['report'].startswith('受控报告')
    for field in ('forecasts', 'suggestions'):
        assert saved['delivery_application'][field][0]['status'] == 'not_applied'
    with store.connect() as db:
        for table in ('analyses', 'forecasts', 'recommendations'):
            assert db.execute(f'SELECT count(*) FROM {table}').fetchone()[0] == 0


def research_records(count):
    return [{'kind': 'sector', 'sector_id': f'controlled-sector-{index}',
             'title': f'受控研究记录 {index}', 'reason': '仅核验完整持久化，不代表投资内容验收。'}
            for index in range(count)]


@pytest.mark.asyncio
@pytest.mark.parametrize('submitted_count', [1, 100, 101])
async def test_research_record_limit_corrects_before_acceptance_and_persists_every_record(
        tmp_path, monkeypatch, submitted_count):
    requests = []
    expected = research_records(min(submitted_count, 100))
    def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) == 1:
            return response(call('finish_analysis', final(research_records=research_records(submitted_count)), 'first'))
        assert 'research_records[100]' in json.dumps(payload, ensure_ascii=False)
        with store.connect() as db:
            assert db.execute('SELECT count(*) FROM analyses').fetchone()[0] == 0
            assert db.execute('SELECT count(*) FROM research_records').fetchone()[0] == 0
        return response(call('finish_analysis', final(research_records=expected), 'corrected'))
    app, store, account = setup_app(tmp_path, monkeypatch, handler)
    saved, reports = await execute(app, store, account)
    assert saved['state'] == 'completed', saved.get('error')
    assert len(requests) == (2 if submitted_count == 101 else 1)
    assert len(reports) == 1
    assert saved['submission_corrections'] == int(submitted_count == 101)
    assert reports[0]['result']['research_records'] == expected
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as api:
        stored = (await api.get('/api/research', params={'account_id': account['id'], 'limit': 100})).json()
    assert {row['sector_id'] for row in stored} == {row['sector_id'] for row in expected}
    assert len(stored) == len(expected)
    # Re-publication and recovery reuse the completed receipt, with no row loss
    # or duplicate append after the first valid result.
    assert store.publish(saved['id'], reports[0]['result'], None)
    store.recover(config_ready=True)
    with store.connect() as db:
        assert db.execute('SELECT count(*) FROM analyses').fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM research_records').fetchone()[0] == len(expected)


def test_research_record_persistence_rejects_oversize_without_mutating_existing_rows(tmp_path):
    from app.delivery import DeliveryContractError
    from app.store import Store
    store = Store(tmp_path / 'record-limit.sqlite3')
    original = research_records(1)
    store.save_research_records(original, 'historical-controlled')
    with store.connect() as db:
        before = [tuple(row) for row in db.execute('SELECT * FROM research_records')]
    with pytest.raises(DeliveryContractError, match=r'research_records\[100\]'):
        store.save_research_records(research_records(101), 'oversize-controlled')
    with store.connect() as db:
        assert [tuple(row) for row in db.execute('SELECT * FROM research_records')] == before
    assert store.research_records()[0]['title'] == original[0]['title']
