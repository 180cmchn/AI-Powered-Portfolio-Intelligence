"""Production delivery of original sign evidence and non-half-sale cost terms.

Model completions below are explicit controlled fixtures. They prove what the
real loop delivers, not that an external model's eventual prose is factual.
"""
import copy
import hashlib
import json
from pathlib import Path

import httpx
import pytest

from app import agents
from app.agent_config import PROMPTS, SEVENTEENTH_PREVIOUS_PROMPTS
from app.materials import material_text, normalize_financial_metrics
from app.model import ModelClient
from app.service import ResearchTools
from app.store import Store, dumps, loads
from tests.context_helpers import effective_context
from tests.delivery_helpers import delivery_arguments
from tests.test_eleventh_ledger import store, account, trade
from tests.test_ninth_calculation_context import config, output, setup


FIXTURE = Path(__file__).with_name('fixtures') / 'seventeenth-financial-signs.json'


def financial_fixture():
    evidence = json.loads(FIXTURE.read_text())
    assert hashlib.sha256(evidence['excerpt'].encode()).hexdigest() == evidence['excerpt_sha256']
    return evidence


def raw_material(store, evidence):
    return store.save_material({'scope': 'public', 'access': 'excerpt', 'source_type': 'external_original',
        'document_id': evidence['source']['document_bytes_sha256'], 'url': evidence['source']['url'],
        'content': evidence['excerpt'], 'source_title': evidence['source']['title'],
        'returned_range': {'start_page': 2, 'end_page': 2}, 'truncated': True})


def extraction(evidence, source_id):
    return {'claim': '累计营收同比下降，归母和扣非同比上升；三个指标分别陈述。',
            'category': 'fact', 'material_ids': [source_id], 'url': evidence['source']['url'],
            'financial_metrics': copy.deepcopy(evidence['expected_financial_metrics'])}


def test_optional_financial_metrics_keep_amount_and_comparison_signs_distinct_and_unknown():
    metrics = normalize_financial_metrics(financial_fixture()['expected_financial_metrics'])
    assert [item['value_sign'] for item in metrics] == ['positive'] * 3
    assert [item['comparison']['value_sign'] for item in metrics] == ['negative', 'positive', 'positive']
    unknown, invalid = normalize_financial_metrics([{'metric': 'unknown basis', 'value': '0'},
        {'metric': 'unread percentage', 'value': 'positive-looking prose', 'comparison': {'value': '-2.04%'}}])
    assert unknown['value_sign'] == 'zero'
    assert unknown['subject'] is None and unknown['report_period'] is None
    assert all(value is None for value in unknown['comparison'].values())
    assert invalid['value'] == 'positive-looking prose' and invalid['value_sign'] is None
    assert invalid['comparison']['value'] == '-2.04%' and invalid['comparison']['value_sign'] is None


async def test_metric_correction_is_new_readable_extraction_and_preserves_account_access(tmp_path, monkeypatch):
    cfg = config(monkeypatch)
    db, tools, context = setup(tmp_path, cfg)
    evidence = financial_fixture()
    source = raw_material(db, evidence)
    body = extraction(evidence, source['id'])
    wrong = copy.deepcopy(body)
    wrong['financial_metrics'][0]['comparison']['value'] = '2.04'
    previous = await tools.save_material(wrong)
    corrected = await tools.save_material(body)
    assert previous['id'] != corrected['id']
    assert db.material(previous['id'])['financial_metrics'][0]['comparison']['value'] == '2.04'
    saved = db.material(corrected['id'])
    assert saved['representation'] == 'agent_extraction' and saved['source_type'] == 'model_extraction'
    assert saved['financial_metrics'][0]['comparison']['value_sign'] == 'negative'
    visible = await tools.get_material(corrected['id'])
    assert visible['financial_metrics_count'] == 3 and visible['financial_metrics_origin'] == 'agent_extraction'
    assert '"value": "-2.04"' in visible['text']
    assert '非独立核验的原始事实' in visible['text']
    assert material_text(saved).count('[Agent 提取的财务指标') == 1
    # Keeping only an unknown metric remains allowed, without a fact gate.
    unknown = await tools.save_material({'claim': '基期未知', 'financial_metrics': [{'metric': 'revenue'}]})
    assert db.material(unknown['id'])['financial_metrics'][0]['comparison']['baseline_period'] is None
    second = db.save_account({'name': 'isolated second account', 'cash': '100', 'holdings': []})
    other = ResearchTools(db, db.job(second['latest_job_id']), cfg)
    with pytest.raises(KeyError):
        await other.get_material(corrected['id'])
    assert (await other.get_material(source['id']))['text'] == evidence['excerpt']


@pytest.mark.parametrize('protocol', ['responses', 'chat_completions'])
async def test_original_signs_and_correction_reach_actual_manager_synthesis_payload(tmp_path, monkeypatch, protocol):
    cfg = config(monkeypatch, model_protocol=protocol, max_model_calls=None, max_agent_turns=None,
                 synthesis_max_calls=None, max_tool_result_chars=12000)
    db, tools, context = setup(tmp_path, cfg)
    evidence = financial_fixture()
    source = raw_material(db, evidence)
    context['previous_model_conclusion'] = '营收、归母、扣非同比都改善（旧模型结论，待根据原文纠正）'
    requests, events = [], []
    saved_id = None
    async def handler(request):
        nonlocal saved_id
        payload = json.loads(request.content)
        requests.append(payload)
        wire = json.dumps(payload, ensure_ascii=False)
        if len(requests) == 1:
            assert '金额为正不等于同比增长' in wire
            return output(protocol, [('get_material', {'material_id': source['id']})])
        if len(requests) == 2:
            assert '-2.04%' in wire and '19.11%' in wire and '17.57%' in wire
            return output(protocol, [('save_material', {'material': extraction(evidence, source['id'])})])
        if len(requests) == 3:
            messages = payload.get('input', payload.get('messages', []))
            delivered = [json.loads(message.get('output', message.get('content'))) for message in messages
                         if message.get('type') == 'function_call_output' or message.get('role') == 'tool']
            saved_id = delivered[-1]['id']
            assert delivered[-1]['financial_metrics_count'] == 3
            return output(protocol, [('get_material', {'material_id': saved_id})])
        if len(requests) == 4:
            messages = payload.get('input', payload.get('messages', []))
            delivered = [json.loads(message.get('output', message.get('content'))) for message in messages
                         if message.get('type') == 'function_call_output' or message.get('role') == 'tool']
            assert '"value_sign": "negative"' in delivered[-1]['text']
            return output(protocol, [('prepare_synthesis', {'summary': '已读原表并分项提取，营收下降而两项利润增长。',
                'material_ids': [source['id'], saved_id]})])
        # The final epoch projects only delivered material, retaining the exact
        # original signs and the account-scoped structured correction.
        assert len(requests) in (5, 6)
        tool_names = [tool.get('name', (tool.get('function') or {}).get('name')) for tool in payload['tools']]
        assert not ({'read', 'spawn_research', 'web_search', 'fundamentals'} & set(tool_names))
        assert '-2.04%' in wire and '19.11%' in wire and '17.57%' in wire
        messages = payload.get('input', payload.get('messages', []))
        delivered = [json.loads(item['content']) for item in messages if item.get('role') == 'user']
        projected = next(item['delivered_research_evidence'] for item in delivered if 'delivered_research_evidence' in item)
        corrected = next(item['result'] for item in projected if isinstance(item.get('result'), dict) and
                         item['result'].get('id') == saved_id and 'text' in item['result'])
        assert '"value_sign": "negative"' in corrected['text']
        assert saved_id in wire and source['id'] in wire
        if len(requests) == 5:
            return output(protocol, [('finish_analysis', {'report': '缺少发布契约字段的受控候选；营收同比下降2.04%。'})])
        assert tool_names == ['finish_analysis']
        return output(protocol, [('finish_analysis', delivery_arguments(
            '营收同比下降2.04%，归母和扣非分别增长19.11%、17.57%；利润改善不能概括为营收增长。当前保留现金，待营收恢复并结合价格与估值复核。',
            scope=('account',), material_ids=[source['id'], saved_id]))])
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(agents, 'ModelClient', lambda c, before_request=None: ModelClient(c, before_request, transport))
    async def emit(value):
        events.append(value)
    runtime = agents.ResearchRuntime(context, cfg, tools, emit)
    try:
        result = await runtime.run()
        assert '营收同比下降2.04%' in result['report']
        assert runtime.manager_session.cache_epoch_reason == 'synthesis'
        assert len(requests) == 6
    finally:
        await runtime.close()


async def test_non_half_sale_precise_costs_reach_production_manager_input(store, monkeypatch):
    a = account(store)
    store.record_trade(a['id'], trade(store, a))
    sale = store.record_trade(a['id'], trade(store, a, operation_id='sell-forty-percent',
        side='sell', quantity='400', price='11', traded_at='2026-10-27T10:00:00+08:00'))
    current = store.account(a['id'])
    ledger = current['ledger']
    entry = next(row for row in ledger['recent_entries'] if row.get('side') == 'sell')
    assert entry['quantity_before'] == '1000' and entry['quantity_after'] == '600'
    assert entry['cost_relieved'] == '4002.04' and entry['total_cost_after'] == '6003.06'
    assert entry['net_sale_proceeds'] == '4392.76' and entry['realized_pnl_effect'] == '390.72'
    assert '卖出份额结转成本' in ledger['cost_terms']['realized_pnl']
    cfg = config(monkeypatch, max_model_calls=None, max_agent_turns=None, synthesis_max_calls=None)
    job = store.job(sale['research']['job_id'])
    tools = ResearchTools(store, job, cfg)
    context = {'job_id': job['id'], 'account': current, 'input_version': current['version']}
    payloads = []
    async def handler(request):
        payload = json.loads(request.content)
        payloads.append(payload)
        visible = effective_context(payload)['account']['ledger']['recent_entries']
        sold = next(row for row in visible if row.get('side') == 'sell')
        for key in ('cost_relieved', 'total_cost_after', 'net_sale_proceeds', 'realized_pnl_effect'):
            assert sold[key] == entry[key]
        return output('responses', [('finish_analysis', delivery_arguments(
            '卖出400股的净所得4392.76元，减结转成本4002.04元，已实现390.72元；600股剩余成本6003.06元。保留现金，结合下一期经营与集中度复核。'))])
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(agents, 'ModelClient', lambda c, before_request=None: ModelClient(c, before_request, transport))
    async def emit(value):
        pass
    runtime = agents.ResearchRuntime(context, cfg, tools, emit)
    try:
        result = await runtime.run()
        assert '结转成本4002.04' in result['report'] and len(payloads) == 1
        assert store.ledger(a['id'])['summary']['known_realized_pnl'] == '390.72'
    finally:
        await runtime.close()


def test_seventeenth_prompt_migration_is_exact_and_preserves_custom_and_binding(tmp_path):
    db = Store(tmp_path / 'prompt-migration.sqlite3')
    old = Path(__file__).with_name('fixtures').joinpath('sixteenth-manager-prompt.txt').read_text()
    assert hashlib.sha256(old.encode()).hexdigest() == SEVENTEENTH_PREVIOUS_PROMPTS['manager']
    with db.connect() as connection:
        manager = loads(connection.execute("SELECT body FROM agent_definitions WHERE role_id='manager'").fetchone()[0])
        original_version, original_binding = manager['prompt_version'], copy.deepcopy(manager['default_binding'])
        manager['system_prompt'] = old
        connection.execute("UPDATE agent_definitions SET body=? WHERE role_id='manager'", (dumps(manager),))
        child = loads(connection.execute("SELECT body FROM agent_definitions WHERE role_id='filing_researcher'").fetchone()[0])
        child['system_prompt'] += '\n用户保存的自定义说明，必须逐字保留。'
        connection.execute("UPDATE agent_definitions SET body=? WHERE role_id='filing_researcher'", (dumps(child),))
        connection.execute("DELETE FROM meta WHERE key='seventeenth_content_prompt_migration'")
    reopened = Store(db.path)
    roles = {role['role_id']: role for role in reopened.list_agent_roles()}
    assert roles['manager']['system_prompt'] == (PROMPTS / 'manager.txt').read_text()
    assert roles['manager']['prompt_version'] == original_version + 1
    assert roles['manager']['default_binding'] == original_binding
    assert roles['filing_researcher']['system_prompt'] == child['system_prompt']
    assert roles['filing_researcher']['prompt_version'] == child['prompt_version']
    assert Store(db.path).list_agent_roles() == reopened.list_agent_roles()
