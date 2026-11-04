"""Server identity and exact exports through production HTTP/worker publication.

These model responses and dates are controlled engineering fixtures.
"""
import hashlib
import json

import httpx
import pytest

from app import agents
from app.domain import AccountInput
from app.model import ModelClient
from app.report_metadata import attach_report_metadata
from tests.test_fourteenth_chain import setup_app, execute, final, call, response
from tests.test_ninth_calculation_context import config, setup, output, seed, source


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['formal', 'exploratory', 'test'])
async def test_server_kind_survives_wrong_prose_and_exact_exports(tmp_path, monkeypatch, frozen_clock, kind):
    frozen_clock.set('2026-11-02T16:00:00+08:00')
    original = '\n正式预测（故意错误的模型措辞）\r\n保留观察；资料不足，弃权。  \n'
    observed = []
    def handler(request):
        payload = json.loads(request.content)
        identity = seed(payload)['prediction_context']
        assert identity['kind'] == kind
        assert identity['base_date'] == '2026-11-02'
        assert identity['publication_time'] is None
        assert identity['information_cutoff'] is None
        targets = seed(payload)['forecast_batch']['items']
        assert {item['horizon'] for item in targets} == {1, 5, 20}
        assert all(item['target_date'] for item in targets)
        assert identity['target_page_count'] == len(targets)
        assert 'targets' not in identity
        observed.append(identity)
        return response(call('finish_analysis', final(original, scope=['account'],
            forecasts=[{'security_id':'SZ:159599','horizon':1,'direction':'abstain','reason':'资料不足'}])))
    app, store, _ = setup_app(tmp_path, monkeypatch, handler)
    account = store.save_account(AccountInput(name='语义夹具', cash='100',
        holdings=[{'symbol':'SZ:159599','quantity':'100','cost_price':'1'}]))
    batch = store.create_forecast_batch(account['id'], kind=kind, at=frozen_clock.instant)
    store.bind_forecast_job(batch['id'], account['latest_job_id'])
    job, reports = await execute(app, store, account)
    assert job['state'] == 'completed', job.get('error')
    assert len(observed) == 1 and len(reports) == 1
    report = reports[0]
    assert report['report'] == original
    metadata = report['system_metadata']
    assert metadata['kinds'] == [kind]
    targets = metadata['batches'][0]['targets']
    published = [item for item in targets if item['forecast_id']]
    assert len(published) == 1 and published[0]['kind'] == kind
    assert published[0]['published_at'] == published[0]['information_cutoff']
    assert report['result']['delivery']['content_assessment'] == 'not_evaluated'
    assert store.forecast_statistics(kind='formal')['expected'] == (3 if kind == 'formal' else 0)
    with store.connect() as db:
        before = tuple(db.execute('SELECT report,result FROM analyses WHERE id=?', (report['id'],)).fetchone())
        assert db.execute('SELECT kind FROM forecasts WHERE report_id=?', (report['id'],)).fetchone()[0] == kind
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as api:
        detail = (await api.get('/api/analyses/'+report['id'])).json()
        assert detail['system_metadata'] == metadata
        raw = await api.get('/api/analyses/'+report['id']+'/export?format=original')
        reading = await api.get('/api/analyses/'+report['id']+'/export?format=reading')
        assert raw.content == original.encode()
        assert reading.content.endswith(raw.content) and reading.content != raw.content
        assert kind in reading.text and '系统标识' in reading.text
        assert '-original.md' in raw.headers['content-disposition']
        assert '-reading.md' in reading.headers['content-disposition']
        for export in (raw, reading):
            assert export.headers['x-content-sha256'] == hashlib.sha256(export.content).hexdigest()
            assert export.headers['x-original-report-sha256'] == hashlib.sha256(raw.content).hexdigest()
        assert (await api.get('/api/analyses/'+report['id']+'/export?format=bogus')).status_code == 422
        assert (await api.get('/api/analyses/missing/export')).status_code == 404
    with store.connect() as db:
        assert tuple(db.execute('SELECT report,result FROM analyses WHERE id=?', (report['id'],)).fetchone()) == before
    assert store.forecasts_page(kind=kind)['items'][0]['information_cutoff'] == published[0]['information_cutoff']


@pytest.mark.asyncio
@pytest.mark.parametrize('protocol', ['responses', 'chat_completions'])
async def test_twenty_observations_and_twenty_intervals_reach_final_http(tmp_path, monkeypatch, protocol):
    cfg = config(monkeypatch, model_protocol=protocol)
    store, tools, context = setup(tmp_path, cfg)
    material = source(store, 'SZ:159278', 0, count=21)
    queries = [{'material_id':material['id'],'start':'2026-01-02','window':20},
               {'material_id':material['id'],'start':'2026-01-01','window':20}]
    expected = [await tools.market_indicators(**args) for args in queries]
    payloads = []
    def handler(request):
        payload = json.loads(request.content); payloads.append(payload)
        if len(payloads) == 1:
            return output(protocol, [('market_indicators', args) for args in queries])
        if len(payloads) == 2:
            return output(protocol, [('calculate', {'operation':'sum','values':[1,2]})])
        summaries = [item for item in seed(payload)['calculation_summaries'] if item['tool'] == 'market_indicators']
        assert len(summaries) == 2
        for summary, original in zip(summaries, expected):
            for field in ('return','return_period','moving_average','moving_average_period','valid_samples',
                          'return_samples','adjustment','source_truncated','requested_range','returned_range'):
                assert summary[field] == original[field]
        assert summaries[0]['valid_samples'] == 20
        assert summaries[0]['return_period']['return_intervals'] == 19
        assert summaries[1]['valid_samples'] == 21
        assert summaries[1]['return_period']['return_intervals'] == 20
        assert summaries[0]['moving_average_period'] == summaries[1]['moving_average_period']
        return output(protocol, [('finish_analysis', final('受控计算口径检查。'))])
    monkeypatch.setattr(agents, 'ModelClient', lambda cfg, before_request=None:
        ModelClient(cfg, before_request, httpx.MockTransport(handler)))
    async def emit(event): pass
    await agents.run_research(context, cfg, tools, emit)
    assert len(payloads) == 3


def test_demo_and_missing_history_never_inferred_from_report_prose(tmp_path):
    from app.store import Store
    store = Store(tmp_path/'labels.sqlite3')
    account = store.save_account({'name':'label fixture','cash':'100','holdings':[]})
    report = {'id':'historical','job_id':account['latest_job_id'],'account_id':account['id'],
        'input_version':account['version'],'mode':'live','created_at':'2026-11-02T16:00:00+08:00',
        'report':'正式预测','result':{'forecasts':[{'kind':'formal'}]}}
    assert attach_report_metadata(store, report)['system_metadata']['kinds'] == ['none']
    assert attach_report_metadata(store, {**report,'mode':'demo'})['system_metadata']['kinds'] == ['demo']


@pytest.mark.asyncio
@pytest.mark.parametrize('protocol', ['responses', 'chat_completions'])
async def test_large_forecast_page_does_not_repeat_targets_or_displace_final_calculation(tmp_path, monkeypatch, protocol):
    cfg = config(monkeypatch, model_protocol=protocol, max_context_chars=10000,
                 max_model_calls=1, max_agent_turns=1)
    store, tools, context = setup(tmp_path, cfg)
    # A large server page is already bounded and archived by ModelClient.session.
    # Re-pinning its 100 targets would re-expand it beyond the reserved budget.
    context['account'] = {key:context['account'][key] for key in ('id','version','cash','holdings')}
    context['forecast_batch'] = {'id':'b'*32,'kind':'formal','base_date':'2026-11-02',
        'deadline':'2026-11-02T18:00:00+08:00','rule_version':'c'*64,
        'has_more':True,'next_offset':100,'expected_count':300,
        'items':[{'symbol':f'SZ:{100000+i//3}','horizon':[1,5,20][i%3],
            'target_date':'2026-12-02','baseline':{'status':'ready','base_close':'100','source':'controlled'}}
            for i in range(100)]}
    material = source(store, 'SZ:159278', 0, count=21)
    expected = await tools.market_indicators(material['id'], window=20)
    requests = []
    def handler(request):
        payload = json.loads(request.content); requests.append(payload)
        visible = seed(payload)
        identity = visible['prediction_context']
        assert identity['kind'] == 'formal'
        assert identity['target_page_count'] == 100 and identity['targets_has_more'] is True
        assert 'targets' not in identity and 'items' not in identity
        assert len(json.dumps(identity, ensure_ascii=False)) < 800
        assert 'forecast_batch.items' in identity['targets_location']
        assert '种子截断' in identity['targets_location']
        batch = visible['forecast_batch']
        assert batch['truncated_for_context'] and batch['record_id']
        assert batch['items'] == [] and batch['items_omitted_for_context'] == 100
        assert batch['expected_count'] == 300 and batch['next_offset'] == 100
        assert '未读取或未研究目标保持未覆盖' in batch['note']
        summaries = visible['calculation_summaries']
        assert len(summaries) == 1, json.dumps({
            'seed_chars':len(json.dumps(visible, ensure_ascii=False)),
            'summary_chars':len(json.dumps(runtime.calculations[0]['summary'], ensure_ascii=False)),
            'equal_model_config': visible['model_config'] == runtime.calculations[0]['summary']['model_config'],
            'parts':{key:len(json.dumps(value, ensure_ascii=False)) for key,value in visible.items()}})
        for key in ('return','return_period','moving_average','moving_average_period','valid_samples','return_samples','adjustment'):
            assert summaries[0][key] == expected[key]
        assert summaries[0]['model_config_ref'] == 'seed.model_config'
        assert visible['model_config'] == runtime.calculations[0]['summary']['model_config']
        assert visible['calculation_indexes']['omitted_summary_count'] == 0
        history = payload.get('input', payload.get('messages'))
        # Chat's system prompt is not part of the configured visible history budget.
        history = [item for item in history if item.get('role') != 'system']
        assert len(json.dumps(history, ensure_ascii=False)) <= cfg['max_context_chars']
        return output(protocol, [('finish_analysis', final('受控大范围上下文口径检查。'))])
    monkeypatch.setattr(agents, 'ModelClient', lambda cfg, before_request=None:
        ModelClient(cfg, before_request, httpx.MockTransport(handler)))
    async def emit(event): pass
    runtime = agents.ResearchRuntime(context, cfg, tools, emit)
    try:
        await runtime._dispatch('manager', 'market_indicators', {'material_id':material['id'], 'window':20})
        original_seed = json.loads(runtime.manager_session.history[0]['content'])
        archived = next(item for item in runtime.manager_session.archive if item['record_id'] == original_seed['record_id'])
        assert len(json.loads(archived['payload'])['forecast_batch']['items']) == 100
        await runtime.run()
        assert runtime.context['forecast_batch'] == context['forecast_batch']
        target_archive = next(item for item in runtime.manager_session.archive if item['kind'] == 'forecast_target_page')
        assert json.loads(target_archive['payload']) == context['forecast_batch']
        assert runtime.synthesis_checkpoint['model_config'] == runtime.model_config
    finally:
        await runtime.close()
    assert len(requests) == 1
