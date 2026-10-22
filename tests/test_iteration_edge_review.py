"""Independent edge-case regression review: deterministic local state only."""
from datetime import datetime, timedelta

from app.domain import AccountInput, TZ
from app.store import Store


def test_candidate_suggestion_is_monitored_until_expiry(tmp_path):
    store = Store(tmp_path / 'candidate.sqlite3')
    store.set_meta('settings', {'debounce_seconds': 0, 'quote_provider': 'manual'})
    account = store.save_account(AccountInput(name='新候选', holdings=[]))
    job = store.claim_jobs()[0]
    stamp = datetime.now(TZ)
    store.publish(job['id'], {'report': '在板块研究中发现的新证券', 'suggestions': [{
        'security_id': 'SZ:000002', 'action': 'buy_candidate',
        'valid_until': (stamp + timedelta(hours=1)).isoformat(),
        'price_conditions': [{'operator': 'lte', 'value': '10'}],
        'agent_conditions': ['最新公告与研究依据仍一致'],
    }]}, None)
    assert 'SZ:000002' in store.monitored(), 'active suggestions need quotes even outside holdings and universe'
    store.review_recommendations(stamp=stamp + timedelta(hours=2))
    assert store.recommendations(account['id'])[0]['state'] == 'expired'
    assert 'SZ:000002' not in store.monitored(), 'expired suggestions should not retain obsolete quote subscriptions'


def test_material_aliases_survive_index_and_range(tmp_path):
    store = Store(tmp_path / 'material-alias.sqlite3')
    material = store.save_material({
        'claim': '仅从摘要观察到的披露数字', 'category': 'fact', 'scope': 'public',
        'access_scope': 'excerpt', 'security_ids': ['SH:600519'],
        'publisher': '测试发布者', 'unit': '亿元', 'uncertainties': '未读取完整来源',
    })
    index = store.material_index()[0]
    fragment = store.material_range(material['id'])
    for item in (index, fragment):
        assert item['access'] == 'excerpt'
        assert item['symbols'] == ['SH:600519']
        assert item['publisher'] == '测试发布者'
        assert item['unit'] == '亿元'
        assert item['uncertainties'] == '未读取完整来源'


def test_optional_or_targeted_suggestions_do_not_implicitly_revoke_other_symbols(tmp_path):
    store = Store(tmp_path / 'suggestion-preservation.sqlite3')
    store.set_meta('settings', {'debounce_seconds': 0, 'quote_provider': 'manual'})
    account = store.save_account(AccountInput(name='独立建议', holdings=[]))
    first_job = store.claim_jobs()[0]
    store.publish(first_job['id'], {'report': '证券 A 建议', 'suggestions': [{
        'security_id': 'SH:600519', 'action': 'watch', 'basis': '持续观察的依据',
        'price_conditions': [{'operator': 'lte', 'value': '10'}],
    }]}, None)
    first = store.recommendations(account['id'])[0]
    store.enqueue(account['id'], 'manual')
    followup = store.claim_jobs()[0]
    store.publish(followup['id'], {'report': '普通自然语言补充报告，没有撤销原建议'}, None)
    carried = next(item for item in store.recommendations(account['id']) if item['id'] == first['id'])
    assert carried['state'] in ('watching', 'triggered', 'needs_review')
    store.enqueue(account['id'], 'manual_security:SZ:000002')
    targeted = store.claim_jobs()[0]
    store.publish(targeted['id'], {'report': '只补充证券 B 的观察', 'suggestions': [{
        'security_id': 'SZ:000002', 'action': 'watch', 'basis': '证券 B 的独立依据',
    }]}, None)
    recommendations = store.recommendations(account['id'])
    retained = next(item for item in recommendations if item['id'] == first['id'])
    assert retained['state'] in ('watching', 'triggered', 'needs_review')
    assert any(item['security_id'] == 'SZ:000002' and item['state'] == 'watching' for item in recommendations)


def test_already_expired_outputs_do_not_create_an_immediate_reanalysis_loop(tmp_path):
    store = Store(tmp_path / 'expired-output.sqlite3')
    store.set_meta('settings', {'debounce_seconds': 0, 'quote_provider': 'manual'})
    account = store.save_account(AccountInput(name='过期输出', holdings=[]))
    executed = 0
    for _ in range(4):
        jobs = store.claim_jobs()
        if not jobs:
            break
        executed += 1
        store.publish(jobs[0]['id'], {'report': '模型重复返回已过期的适用日期', 'valid_until': '2020-01-01T00:00:00+08:00'}, None)
        store.review_recommendations()
    assert executed <= 2, 'unchanged already-expired output must not consume unbounded immediate model calls'
    assert not store.claim_jobs(), 'expiry should not create an endless replacement job chain'
    assert store.analyses(account['id'])[0]['status'] != 'current'
