"""Independent review reproductions for cross-module state ownership."""
from datetime import datetime

from app.domain import AccountInput, TZ
from app.store import Store


def test_fresh_universe_does_not_inherit_old_scope_retry_exhaustion(tmp_path):
    store = Store(tmp_path / 'review-scope-retry.sqlite3')
    store.set_meta('settings', {'debounce_seconds': 0, 'quote_provider': 'manual', 'max_job_attempts': 2})
    store.save_universe({'watchlist': [], 'candidates': ['SH:600519']})
    first = store.claim_global()
    assert first['attempts'] == 1
    store.recover()
    second = store.claim_global()
    assert second['attempts'] == 2
    store.save_universe({'watchlist': [], 'candidates': ['SZ:000001']})
    store.recover()
    fresh = store.claim_global()
    assert fresh is not None, '新候选范围有独立待执行需求，不能继承旧范围的中断耗尽'
    assert fresh['universe_version'] == 3 and fresh['attempts'] == 1


def test_targeted_research_does_not_complete_unrelated_recommendation_demand(tmp_path):
    store = Store(tmp_path / 'review-target.sqlite3')
    store.set_meta('settings', {'debounce_seconds': 0, 'quote_provider': 'manual'})
    account = store.save_account(AccountInput(name='受控单证券研究', holdings=[]))
    initial = store.claim_jobs()[0]
    store.publish(initial['id'], {'report': '受控初始建议', 'suggestions': [
        {'security_id': 'SH:600519', 'action': 'watch', 'basis': 'A 受控依据',
         'price_conditions': [{'operator': 'gte', 'value': 110}]},
        {'security_id': 'SZ:000001', 'action': 'watch', 'basis': 'B 受控依据'}]}, None)
    stamp = datetime.now(TZ)
    store.review_recommendations({'SH:600519': {'price': '120', 'status': 'ok', 'as_of': stamp.isoformat()}}, stamp)
    demand = next(d for d in store.demands(account['id']) if d['kind'] == 'recommendation_review')
    store.enqueue(account['id'], 'manual_security:SZ:000001')
    local = store.claim_jobs()[0]
    store.publish(local['id'], {'report': '只研究 B 的补充报告'}, None)
    after = next(d for d in store.demands(account['id']) if d['id'] == demand['id'])
    assert after['state'] != 'completed', 'B 的局部研究没有处理 A，不能完成 A 的复核事项'
    store.dispatch_demands()
    jobs = store.claim_jobs()
    assert jobs and jobs[0]['id'] != local['id']


def test_local_completion_retains_only_in_scope_demands_and_full_followup_is_bounded(tmp_path):
    store = Store(tmp_path / 'review-target-wide.sqlite3')
    store.set_meta('settings', {'debounce_seconds': 0, 'quote_provider': 'manual'})
    account = store.save_account(AccountInput(name='受控多轮范围', holdings=[]))
    initial = store.claim_jobs()[0]
    store.publish(initial['id'], {'report': '受控初始建议', 'suggestions': [
        {'security_id': sid, 'action': 'watch', 'basis': '受控依据',
         'price_conditions': [{'operator': 'gte', 'value': 110}]}
        for sid in ('SH:600519', 'SZ:000001')]}, None)
    ids = {row['security_id']: row['id'] for row in store.recommendations(account['id'])}
    stamp = datetime.now(TZ)
    store.review_recommendations({sid: {'price': '120', 'status': 'ok', 'as_of': stamp.isoformat()} for sid in ids}, stamp)
    for kind in ('public_information', 'scheduled', 'configuration_changed', 'report_expired'):
        store.request_analysis(account['id'], kind, f'controlled-wide:{kind}')
    store.enqueue(account['id'], 'manual_security:SZ:000001')
    local = store.claim_jobs()[0]
    store.publish(local['id'], {'report': '仅复核 B 并明确维持', 'maintained_suggestion_ids': [ids['SZ:000001']]}, None)
    demands = store.demands(account['id'])
    assert next(d for d in demands if ids['SZ:000001'] in d['event_key'])['state'] == 'completed'
    assert next(d for d in demands if ids['SH:600519'] in d['event_key'])['state'] == 'pending'
    assert all(d['state'] == 'pending' for d in demands if d['event_key'].startswith('controlled-wide:'))
    store.dispatch_demands()
    follow = store.claim_jobs()[0]
    assert 'manual_security:' not in follow['reason']
    covered = [row['id'] for row in store.task_demands(follow['id'], account['id'])['items']]
    store.publish(follow['id'], {'report': '全账户复核已完成', 'maintained_suggestion_ids': list(ids.values()),
                                'covered_demand_ids': covered}, None)
    for _ in range(3):
        store.dispatch_demands()
        store.review_recommendations()
        assert not store.claim_jobs()
    assert all(d['state'] == 'completed' for d in store.demands(account['id']))
