"""Chinese prose boundaries through ModelClient, publication and API reads."""
import json

import httpx
import pytest

from app.domain import AccountInput
from tests.test_fourteenth_chain import setup_app,execute,final,call,response


@pytest.mark.asyncio
@pytest.mark.parametrize('tail',[
    '）后续正文', '，后续正文', '。下一句', '；后续正文', '】后续正文',
    '》后续正文', '」后续正文', '”后续正文', '’后续正文', '！下一句', '？下一句',
])
async def test_bare_url_stops_at_chinese_prose_delimiter_in_published_api(tmp_path,monkeypatch,tail):
    url='https://fixture.invalid/annual.pdf?year=2026&view=full#page3'
    report='受控结论。来源（'+url+tail+'；仅为链接，未读取原文。'
    sent=[]
    def handler(request):
        sent.append(request)
        return response(call('finish_analysis',final(report)))
    app,store,account=setup_app(tmp_path,monkeypatch,handler,max_model_calls=1,max_agent_turns=1)
    job,reports=await execute(app,store,account)
    assert job['state']=='completed' and len(sent)==len(reports)==1
    result=reports[0]['result']
    assert result['report']==report
    assert result['references']==[{'url':url,'status':'source_link','reason':'not_located_in_saved_material'}]
    assert result['reference_summary']=={'resolved':0,'source_link':1,'unresolved':0}
    with store.connect() as db:
        saved=db.execute('SELECT report,result FROM analyses WHERE job_id=?',(job['id'],)).fetchone()
        assert saved['report']==report and json.loads(saved['result'])['references']==result['references']
        assert db.execute('SELECT count(*) FROM research_material_deliveries').fetchone()[0]==0


@pytest.mark.asyncio
@pytest.mark.parametrize('url',[
    'https://fixture.invalid/基金/季度报告.pdf?类别=股票&日期=2026-10-30#第三页',
    'https://fixture.invalid/%E5%9F%BA%E9%87%91/%EF%BC%89?q=%EF%BC%8C%EF%BC%9B&literal=%23#%E7%AC%AC%E4%B8%89%E9%A1%B5',
    'https://fixture.invalid/基金/报告.pdf?filter=a;b&sign=a%2Bb%2F%3D#section_3',
])
async def test_bare_url_preserves_unicode_query_fragment_and_percent_encoding(tmp_path,monkeypatch,url):
    report='来源（'+url+'），受控判断不代表已读原文。'
    app,store,account=setup_app(tmp_path,monkeypatch,lambda request:response(call('finish_analysis',final(report))),
        max_model_calls=1,max_agent_turns=1)
    job,reports=await execute(app,store,account)
    assert job['state']=='completed'
    assert reports[0]['report']==report
    assert reports[0]['result']['references'][0]['url']==url


@pytest.mark.asyncio
async def test_structured_url_is_not_trimmed_by_bare_prose_boundaries(tmp_path,monkeypatch):
    url='https://fixture.invalid/目录/条款（附录）?说明=甲，乙#段落。'
    app,store,account=setup_app(tmp_path,monkeypatch,lambda request:response(call('finish_analysis',final(
        '受控显式来源字段。',findings=[{'source_url':url}]))),max_model_calls=1,max_agent_turns=1)
    job,reports=await execute(app,store,account)
    assert job['state']=='completed'
    item=reports[0]['result']['findings'][0]
    assert item['source_url']==item['references'][0]['url']==url
    assert item['references'][0]['status']=='source_link'


@pytest.mark.asyncio
async def test_fixed_bare_url_retains_saved_excerpt_and_cross_account_boundaries(tmp_path,monkeypatch):
    public_url='https://fixture.invalid/公开材料?kind=摘要#资料'
    private_url='https://fixture.invalid/private-source'
    report=f'受控引用（{public_url}），还列来源（{private_url}）；不声称阅读全文。'
    app,store,account=setup_app(tmp_path,monkeypatch,lambda request:response(call('finish_analysis',final(report))),
        max_model_calls=1,max_agent_turns=1)
    other=store.save_account(AccountInput(name='Other isolated account',cash='0',holdings=[],research_scope='cash_only'))
    public=store.save_material({'scope':'public','representation':'native_search_source','access':'search_excerpt',
        'url':public_url,'title':'Controlled public excerpt','text':'Controlled excerpt only.'})
    private=store.save_material({'scope':'account','representation':'native_search_source','access':'search_excerpt',
        'url':private_url,'title':'PRIVATE_TITLE_MARKER','text':'PRIVATE_BODY_MARKER'},account_id=other['id'])
    job,reports=await execute(app,store,account)
    assert job['state']=='completed'
    refs=reports[0]['result']['references']
    assert len(refs)==2
    assert refs[0]['material_id']==public['id'] and refs[0]['status']=='resolved'
    assert refs[0]['access']=='search_excerpt'
    assert refs[1]=={'url':private_url,'status':'source_link','reason':'not_located_in_saved_material'}
    assert private['id'] not in json.dumps(refs) and 'PRIVATE_' not in json.dumps(refs)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://testserver') as api:
        assert (await api.get(refs[0]['locator'])).status_code==200
        assert (await api.get(f'/api/materials/{private["id"]}/range',params={'account_id':account['id']})).status_code==404
    with store.connect() as db:
        assert db.execute('SELECT count(*) FROM research_material_deliveries').fetchone()[0]==0


@pytest.mark.asyncio
async def test_existing_stored_bare_url_references_are_not_rewritten_by_api_read(tmp_path,monkeypatch):
    url='https://fixture.invalid/old'
    report=f'历史报告（{url}）后续正文。'
    app,store,account=setup_app(tmp_path,monkeypatch,lambda request:response(call('finish_analysis',final(report))),
        max_model_calls=1,max_agent_turns=1)
    job,reports=await execute(app,store,account)
    aid=reports[0]['id']
    with store.connect() as db:
        row=db.execute('SELECT result FROM analyses WHERE id=?',(aid,)).fetchone()
        legacy=json.loads(row['result'])
        legacy['references']=[{'url':url+'）后续正文','status':'source_link','reason':'not_located_in_saved_material'}]
        raw=json.dumps(legacy,ensure_ascii=False)
        db.execute('UPDATE analyses SET result=? WHERE id=?',(raw,aid))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://testserver') as api:
        for path in (f'/api/analyses/{aid}',f'/api/accounts/{account["id"]}/analyses'):
            result=(await api.get(path)).json();result=result[0] if isinstance(result,list) else result
            assert result['report']==report
            assert result['result']['references']==legacy['references']
    with store.connect() as db:
        assert db.execute('SELECT result FROM analyses WHERE id=?',(aid,)).fetchone()[0]==raw
