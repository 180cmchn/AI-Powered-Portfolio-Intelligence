"""Optional output limit through real settings API and persisted job snapshots."""
from copy import deepcopy

import httpx
import pytest

from app.domain import AccountInput
from app.main import create_app


def api_for(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1')


@pytest.mark.asyncio
async def test_new_settings_default_to_supplier_output_limit_without_writing_a_numeric_default(tmp_path):
    app=create_app(tmp_path/'new.sqlite3',mode='live',worker=False)
    before=deepcopy(app.state.store.meta('settings',{}))
    async with api_for(app) as api:
        settings=(await api.get('/api/settings')).json()
        assert settings['max_output_tokens'] is None
        assert app.state.store.meta('settings',{})==before
        response=await api.put('/api/settings',json={'max_agent_turns':13})
        assert response.status_code==200 and response.json()['max_output_tokens'] is None
        assert 'max_output_tokens' not in app.state.store.meta('settings',{})


@pytest.mark.asyncio
async def test_omitted_field_preserves_explicit_numeric_limit_and_null_is_an_explicit_choice(tmp_path):
    database=tmp_path/'choices.sqlite3'
    app=create_app(database,mode='live',worker=False)
    app.state.store.set_meta('settings',{'max_output_tokens':4000,'quote_provider':'manual'})
    async with api_for(app) as api:
        assert (await api.get('/api/settings')).json()['max_output_tokens']==4000
        partial=await api.put('/api/settings',json={'max_agent_turns':13})
        assert partial.status_code==200 and partial.json()['max_output_tokens']==4000
        assert app.state.store.meta('settings')['max_output_tokens']==4000
        cleared=await api.put('/api/settings',json={'max_output_tokens':None})
        assert cleared.status_code==200 and cleared.json()['max_output_tokens'] is None
        assert app.state.store.meta('settings')['max_output_tokens'] is None
        assert (await api.put('/api/settings',json={})).json()['max_output_tokens'] is None
    restarted=create_app(database,mode='live',worker=False)
    async with api_for(restarted) as api:
        assert (await api.get('/api/settings')).json()['max_output_tokens'] is None
        numeric=await api.put('/api/settings',json={'max_output_tokens':6000})
        assert numeric.status_code==200 and numeric.json()['max_output_tokens']==6000
    restarted_again=create_app(database,mode='live',worker=False)
    assert restarted_again.state.store.settings()['max_output_tokens']==6000


@pytest.mark.asyncio
async def test_clearing_limit_only_changes_new_job_snapshot(tmp_path):
    app=create_app(tmp_path/'snapshots.sqlite3',mode='live',worker=False)
    store=app.state.store
    store.set_meta('settings',{'max_output_tokens':4000,'debounce_seconds':0,'quote_provider':'manual'})
    account=store.save_account(AccountInput(name='Snapshot fixture',cash='100000',holdings=[],research_scope='cash_only'))
    running=store.claim_jobs(account_id=account['id'])[0]
    old_snapshot=deepcopy(running['config_snapshot'])
    assert old_snapshot['max_output_tokens']==4000
    async with api_for(app) as api:
        response=await api.put('/api/settings',json={'max_output_tokens':None})
        assert response.status_code==200
        created=await api.post('/api/accounts',json={'name':'New snapshot fixture','cash':'100000','holdings':[],
            'research_scope':'cash_only'})
        assert created.status_code==201
    assert store.job(running['id'])['config_snapshot']==old_snapshot
    assert store.job(running['id'])['state']=='running'
    next_job=store.claim_jobs(account_id=created.json()['id'])[0]
    assert next_job['id']!=running['id'] and next_job['config_snapshot']['max_output_tokens'] is None
    assert running['config_snapshot']['max_output_tokens']==4000
    assert store.account(account['id'])['version']==account['version']


@pytest.mark.asyncio
@pytest.mark.parametrize('value',[0,-1,255,32001,True,1.5,''])
async def test_invalid_output_limit_does_not_mutate_saved_choice(tmp_path,value):
    app=create_app(tmp_path/'invalid.sqlite3',mode='live',worker=False)
    app.state.store.set_meta('settings',{'max_output_tokens':4000,'quote_provider':'manual'})
    before=deepcopy(app.state.store.meta('settings'))
    async with api_for(app) as api:
        response=await api.put('/api/settings',json={'max_output_tokens':value})
        assert response.status_code==422
        assert (await api.get('/api/settings')).json()['max_output_tokens']==4000
    assert app.state.store.meta('settings')==before
