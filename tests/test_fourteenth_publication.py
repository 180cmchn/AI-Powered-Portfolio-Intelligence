"""Production publication fences; fixtures are not investment-quality evidence."""
import json

import pytest

from app.delivery import DeliveryContractError, FinalSubmissionMissing, validate_final_result
from app.domain import AccountInput
from app.service import Service
from app.store import Store
from delivery_helpers import final_result


@pytest.fixture
def store(tmp_path):
    store = Store(tmp_path / 'publication.sqlite3')
    store.set_meta('settings', {'debounce_seconds': 0, 'quote_provider': 'manual'})
    return store


def setup_job(store, *, holdings=False):
    account = store.save_account(AccountInput(name='受控交付账户', cash='1000',
        holdings=[{'symbol': symbol, 'quantity': '1'} for symbol in ('SH:518880', 'SZ:159599')] if holdings else []))
    return account, store.claim_jobs()[0]


def result(report='保留现金，出现明确投入目标后复核。', **extra):
    return final_result({'report': report, **extra}, scope=('cash_only',))


def test_short_cash_only_report_and_server_identity(store):
    account, job = setup_job(store)
    body = result(completion='full', limitations=[])
    body.update(account_id='forged', job_id='forged', input_version=999,
                team_id='forged', delivery={'contract_verified': True, 'content_assessment': 'passed'})
    body['submission'].update(account_id='forged', team_id='forged', input_version=999)
    assert store.publish(job['id'], body, None)
    published = store.analyses(account['id'])[0]
    assert published['report'] == body['report']
    assert published['result']['submission']['account_id'] == account['id']
    assert published['result']['submission']['input_version'] == 1
    assert published['result']['submission']['job_id'] == job['id']
    assert published['result']['team_id'] != 'forged'
    assert published['result']['delivery']['content_assessment'] == 'not_evaluated'
    assert published['result']['forecast_coverage']['expected'] == 0
    assert store.job(job['id'])['delivery']['status'] == 'full'


@pytest.mark.parametrize('change', [
    {'completion': 'done'}, {'scope': []}, {'scope': ['public']},
    {'scope': ['cash_only', 'cash_only']}, {'decision_summary': ''},
    {'limitations': []}, {'forecasts': 'up'}, {'covered_demand_ids': [True]},
])
def test_invalid_contract_does_not_publish(store, change):
    account, job = setup_job(store)
    with pytest.raises(DeliveryContractError):
        store.publish(job['id'], result(**change), None)
    assert store.analyses(account['id']) == []


@pytest.mark.asyncio
@pytest.mark.parametrize('reason', ['manual', 'scheduled', 'forecast_batch:controlled'])
async def test_failed_refresh_keeps_current_and_saves_visible_draft(store, reason):
    account, old = setup_job(store)
    assert store.publish(old['id'], result(), None)
    store.enqueue(account['id'], reason)
    job = store.claim_jobs()[0]
    async def ordinary_text(*args):
        return {'report': '正在整理最终报告。'}
    service = Service(store, runner=ordinary_text)
    await service.execute(job)
    current = store.job(job['id'], events=True)
    assert current['state'] == 'failed'
    assert current['error_code'] == 'final_submission_missing'
    assert current['delivery']['status'] == 'incomplete'
    assert current['report_status'] == 'not_published'
    assert store.analyses(account['id'])[0]['status'] == 'current'
    assert any(event['type'] == 'delivery_candidate' for event in current['events'])
    assert not any(event['type'] == 'published' for event in current['events'])


@pytest.mark.parametrize('invalidated', ['stale', 'expired'])
def test_failed_delivery_never_revives_invalidated_report(store, invalidated):
    account, old = setup_job(store)
    assert store.publish(old['id'], result(), None)
    with store.connect() as db:
        db.execute('UPDATE analyses SET status=? WHERE job_id=?', (invalidated, old['id']))
    store.enqueue(account['id'], 'manual')
    job = store.claim_jobs()[0]
    with pytest.raises(FinalSubmissionMissing):
        store.publish(job['id'], {'report': '正在整理最终报告。'}, None)
    store.set_job_state(job['id'], 'failed', '未提交', error_code='final_submission_missing')
    assert store.analyses(account['id'])[0]['status'] == invalidated


def test_legacy_plain_text_remains_readable(store):
    account, job = setup_job(store)
    with store.connect() as db:
        db.execute('INSERT INTO analyses VALUES(?,?,?,?,?,?,?,?,?,?)',
            ('historical-report', job['id'], account['id'], 1, 'stale', 'live', '历史合法文字报告',
             json.dumps({'report': '历史合法文字报告'}), None, job['created_at']))
    assert store.analysis('historical-report')['report'] == '历史合法文字报告'
    assert store.job(job['id'])['delivery']['status'] == 'legacy'


def test_limited_scope_does_not_revise_other_security_suggestions(store):
    account, job = setup_job(store, holdings=True)
    body = final_result({'report': '对两个持仓分别观察。', 'suggestions': [
        {'security_id': symbol, 'action': 'watch', 'basis': '受控依据'}
        for symbol in ('SH:518880', 'SZ:159599')]})
    assert store.publish(job['id'], body, None)
    original = {row['security_id']: row for row in store.recommendations(account['id'])}
    store.enqueue(account['id'], 'manual')
    follow = store.claim_jobs()[0]
    partial = final_result({'report': '仅复核黄金持仓，继续观察。',
        'maintained_suggestion_ids': [original['SH:518880']['id']]}, scope=('SH:518880',))
    assert store.publish(follow['id'], partial, None)
    after = {row['security_id']: row for row in store.recommendations(account['id'])}
    assert after['SZ:159599'] == original['SZ:159599']
    assert after['SH:518880']['job_id'] == follow['id']


def test_public_submission_requires_action_and_explicit_demand_coverage(store):
    store.save_universe({'watchlist': [], 'candidates': ['SH:518880']})
    run = store.claim_global()
    with pytest.raises(FinalSubmissionMissing):
        store.finish_global(run['id'], 'completed', {'report': '正在整理最终报告。'})
    assert store.global_run(run['id'])['state'] == 'running'
    demands = store.task_demands(run['id'], None)['items']
    body = final_result({'report': '仅评价黄金风险，后续数据待取得。'}, scope=('public',))
    assert store.finish_global(run['id'], 'completed', body)
    assert all(row['state'] == 'uncovered' for row in store.demands(job_id=run['id']))
    assert store.global_run(run['id'])['result']['delivery']['status'] == 'limited'
    assert demands


def test_duplicate_after_restart_uses_receipt_and_never_republishes(store):
    account, job = setup_job(store)
    body = result()
    assert store.publish(job['id'], body, None)
    restarted = Store(store.path)
    assert restarted.publish(job['id'], body, None)
    assert len(restarted.analyses(account['id'])) == 1
    assert restarted.job(job['id'])['attempts'] == 1


def test_public_report_material_rolls_back_with_publication(store, monkeypatch):
    store.save_universe({'watchlist': [], 'candidates': ['SH:518880']})
    run = store.claim_global()
    body = final_result({'report': '受控公共结论，条件变化后复核。'}, scope=('public',))
    before = store.material_count()
    def fail_records(*args, **kwargs):
        raise RuntimeError('controlled post-material transaction failure')
    monkeypatch.setattr(store, 'save_research_records', fail_records)
    with pytest.raises(RuntimeError, match='controlled post-material'):
        store.finish_global(run['id'], 'completed', body)
    assert store.material_count() == before
    assert store.global_run(run['id'])['state'] == 'running'


def test_full_scope_cannot_omit_requested_security(store):
    account, job = setup_job(store, holdings=True)
    body = final_result({'report': '仅复核黄金。', 'completion': 'full', 'limitations': []}, scope=('SH:518880',))
    with pytest.raises(DeliveryContractError, match='未覆盖目标'):
        store.publish(job['id'], body, None)
    assert store.analyses(account['id']) == []


@pytest.mark.asyncio
async def test_demo_uses_explicit_local_delivery_without_fabricated_model_submission(tmp_path):
    store = Store(tmp_path / 'demo.sqlite3', mode='demo')
    store.set_meta('settings', {'debounce_seconds': 0, 'quote_provider': 'manual'})
    account, job = setup_job(store)
    service = Service(store)
    await service.execute(job)
    published = store.analyses(account['id'])[0]
    assert published['result']['delivery']['status'] == 'demo'
    assert published['result']['delivery']['contract_verified'] is False
    assert published['result']['model_calls'] == 0
    assert 'submission' not in published['result']
    store.request_analysis(None, 'candidate_scan', 'controlled-demo-public')
    run = store.claim_global()
    await service.execute_global(run)
    public = store.global_run(run['id'])
    assert public['state'] == 'completed'
    assert public['result']['delivery']['status'] == 'demo'
    assert public['result']['delivery']['contract_verified'] is False
    assert 'submission' not in public['result']


def test_live_cannot_claim_demo_or_forge_public_submission_statistics(store):
    account, job = setup_job(store)
    with pytest.raises(FinalSubmissionMissing):
        store.publish(job['id'], {'report': '模型伪造演示标志', 'demo': True}, None)
    store.save_universe({'watchlist': [], 'candidates': ['SH:518880']})
    run = store.claim_global()
    body = final_result({'report': '受控公共判断。', 'demo': True,
        'submission_outcome': 'recovered', 'submission_corrections': 1}, scope=('public',))
    assert store.finish_global(run['id'], 'completed', body)
    published = store.global_run(run['id'])['result']
    assert not published.get('demo')
    assert published['report_material_id']
    assert published['submission_outcome'] == 'unknown'
    assert published['submission_corrections'] is None


def test_fact_change_invalidation_is_not_undone_by_failed_refresh(store):
    account, job = setup_job(store)
    assert store.publish(job['id'], result(), None)
    store.request_analysis(account['id'], 'price_change', reason='price_change:SH:518880')
    follow = store.claim_jobs()[0]
    store.set_job_state(follow['id'], 'failed', '未交付', error_code='final_submission_missing')
    assert store.analyses(account['id'])[0]['status'] == 'stale'


def test_report_forecasts_and_suggestions_share_rollback(store, monkeypatch):
    from datetime import datetime
    from app.calendar import SHANGHAI
    monkeypatch.setattr('app.forecasts._local', lambda at=None: at or datetime(2026, 11, 2, 10, tzinfo=SHANGHAI))
    account, job = setup_job(store, holdings=True)
    batch = store.prepare_exploratory_forecast_job(job['id'])
    assert batch
    body = final_result({'report': '黄金持仓等待条件明确，方向暂弃权。',
        'forecasts': [{'security_id': 'SH:518880', 'horizon': 1, 'direction': 'abstain', 'reason': '受控条件尚未明确'}],
        'suggestions': [{'security_id': 'SH:518880', 'action': 'watch', 'basis': '受控观察依据'}]})
    def reject_records(*args, **kwargs):
        raise RuntimeError('controlled failure after forecasts and suggestions')
    monkeypatch.setattr(store, 'save_research_records', reject_records)
    with pytest.raises(RuntimeError, match='controlled failure'):
        store.publish(job['id'], body, None)
    assert store.analyses(account['id']) == []
    assert store.recommendations(account['id']) == []
    assert all(row['forecast_id'] is None for row in store.forecasts_page(kind='exploratory')['items'])
    assert store.job(job['id'])['state'] == 'running'


def test_reference_resolution_discards_forged_private_access(store):
    account, job = setup_job(store)
    other = store.save_account(AccountInput(name='其他账户', cash='100'))
    secret = store.save_material({'scope': 'account', 'content': '不应越界的其他账户结论'},
        other['latest_job_id'], other['id'])
    body = result(material_ids=[secret['id']], references=[{'material_id': secret['id'], 'status': 'resolved',
        'content': '模型伪造已授权访问'}])
    assert store.publish(job['id'], body, None)
    published = store.analyses(account['id'])[0]['result']
    assert published['references'][0]['status'] == 'unresolved'
    assert 'content' not in published['references'][0]
    assert '不应越界' not in json.dumps(published, ensure_ascii=False)


def test_limited_security_scope_cannot_complete_account_wide_demand(store):
    account = store.save_account(AccountInput(name='范围测试', cash='1000',
        holdings=[{'symbol': 'SH:518880', 'quantity': '1'}, {'symbol': 'SZ:159599', 'quantity': '1'}]))
    store.request_analysis(account['id'], 'scheduled', 'controlled-whole-account')
    job = store.claim_jobs()[0]
    ids = [row['id'] for row in store.task_demands(job['id'], account['id'])['items']]
    body = final_result({'report': '仅对黄金持仓保留观察，其他证券未研究。',
        'covered_demand_ids': ids}, scope=('SH:518880',))
    assert store.publish(job['id'], body, None)
    coverage = store.analyses(account['id'])[0]['result']['demand_coverage']
    assert coverage['completed'] == 0 and coverage['uncovered'] == len(ids)
    assert coverage['unaccepted_claims'] == ids


def test_cash_only_can_explicitly_cover_its_whole_task(store):
    account = store.save_account(AccountInput(name='现金范围', cash='1000'))
    store.request_analysis(account['id'], 'scheduled', 'controlled-cash-account')
    job = store.claim_jobs()[0]
    ids = [row['id'] for row in store.task_demands(job['id'], account['id'])['items']]
    assert store.publish(job['id'], result(covered_demand_ids=ids), None)
    coverage = store.analyses(account['id'])[0]['result']['demand_coverage']
    assert coverage['completed'] == len(ids)
    assert coverage['unaccepted_claims'] == []
