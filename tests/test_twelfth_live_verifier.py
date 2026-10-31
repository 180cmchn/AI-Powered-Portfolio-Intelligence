"""Cost-free verifier integration: production API/SQLite, explicit runner/vault.

Never runs --run-real, never opens the daily database or project credentials,
and never contacts a model or public data provider.
"""
from pathlib import Path
from decimal import Decimal
import json
import shutil
from types import SimpleNamespace
import uuid

import httpx
import pytest

from app.domain import AccountInput, now
from app.main import create_app
from app.store import Store
from scripts import verify_twelfth_live as verifier
from tests.helpers_sixth import configure_test_team


@pytest.mark.asyncio
async def test_single_round_verifier_contract_with_explicit_runner(tmp_path, monkeypatch):
    source = Store(tmp_path/'controlled-source.sqlite3', 'live')
    cfg = {**source.settings(), 'quote_provider':'manual', 'reasoning_effort':'max',
           'model_name':'twelfth-cost-free-verifier-fixture'}
    source.set_meta('settings', cfg)
    configure_test_team(source,cfg)
    account = source.save_account(AccountInput(name='explicit verifier fixture source',cash='100000',holdings=[]))
    vault = source._credential_store
    origin_configure = verifier.configure
    calls = []

    def configure_controlled(origin, target, account_id):
        assert origin == source.path
        store, settings = origin_configure(origin,target,account_id)
        store._credential_store = vault
        for symbol,kind in [('SH:518880','etf'),('SZ:002354','stock'),('SZ:159599','etf')]:
            store.put_cache('securities',symbol,{'symbol':symbol,'name':symbol+' fixture',
                'kind':kind,'status':'ok','identity_status':'confirmed','fetched_at':now(),
                'source':'explicit-verifier-fixture'})
            store.put_cache('quotes',symbol,{'symbol':symbol,'price':'10.000','status':'closed',
                'fresh_for_triggers':False,'as_of':now(),'source':'explicit-verifier-fixture'})
        return store,settings

    async def controlled_runner(context, config, tools, emit):
        calls.append(context['job_id'])
        assert config['model_name']=='twelfth-cost-free-verifier-fixture'
        assert config['reasoning_effort']=='max'
        # Explicit synthetic diagnostics exercise the verifier's aggregation;
        # they are never represented as a provider or billing observation.
        await emit({'type':'model_usage','agent_id':'manager','payload':{'requests':[{
            'role_id':'manager','provider_id':config['provider_id'],'model':config['model_name'],
            'reasoning_effort_sent':'max','stream_terminal':'response.completed',
            'error':None,'usage':{'input_tokens':1,'output_tokens':1},'test_double':True}]}})
        return {'report':'Explicit controlled verifier report; zero real model/network calls.',
                'suggestions':[{'security_id':'SH:518880','action':'watch','basis':'explicit fixture',
                    'price_conditions':[{'operator':'gte','field':'price','value':'12'}]}]}

    class ControlledASGIInstance:
        """Same production lifespan/API contracts, no sockets or external I/O."""
        def __init__(self,database,output,name,worker):
            self.database,self.output,self.name,self.worker=database,output,name,worker
            self.api=None

        async def start(self):
            self.application=create_app(self.database,mode='live',worker=self.worker,runner=controlled_runner)
            self.application.state.store._credential_store=vault

            async def cached_quotes(symbols=None,force=False):
                return list(self.application.state.store.cache('quotes').values())
            self.application.state.service.refresh_quotes=cached_quotes
            self.lifespan=self.application.router.lifespan_context(self.application)
            await self.lifespan.__aenter__()
            self.base='http://testserver'
            self.api=httpx.AsyncClient(transport=httpx.ASGITransport(app=self.application),base_url=self.base)
            return {'name':self.name,'url':self.base,'worker':self.worker,'pid':None,
                    'test_double':'ASGI transport + fixture runner/vault',
                    'health':await self.request('GET','/api/health')}

        async def request(self,method,path,**kwargs):
            response=await self.api.request(method,path,**kwargs)
            response.raise_for_status()
            return response.json()

        async def stop(self):
            if self.api:
                await self.api.aclose()
                self.api=None
                await self.lifespan.__aexit__(None,None,None)

    monkeypatch.setattr(verifier,'configure',configure_controlled)
    monkeypatch.setattr(verifier,'Instance',ControlledASGIInstance)
    original_scrubbed=verifier.scrubbed
    monkeypatch.setattr(verifier,'scrubbed',lambda value,config:original_scrubbed(
        {**value,'controlled_verifier_check':True,'actual_real_model_calls':0},config))
    output=verifier.ROOT/'data/evaluations'/('twelfth-controlled-verifier-check-'+uuid.uuid4().hex)
    try:
        code=await verifier.run(SimpleNamespace(output=output,source=source.path,reference_account=account['id']))
        summary=json.loads((output/'summary.json').read_text())
        assert code==0, summary.get('error') or summary
        assert len(calls)==1
        assert summary['actual_real_model_calls']==0
        assert summary['prepared_ledger']['cash']=='86484.90'
        assert len(summary['trades'])==3
        assert len(summary['instances'])==3
        assert len(summary['observations'])==20
        assert summary['final_report_current'] is True
        assert summary['old_quote_baseline_observed'] is True
        assert summary['automatic_tasks']==[]
        assert summary['binding_mismatches']==[]
        assert summary['total_http_attempts']==1  # explicitly synthetic above
    finally:
        shutil.rmtree(output,ignore_errors=True)


@pytest.mark.asyncio
async def test_actual_reserved_socket_preparation_instance(tmp_path):
    """Actual isolated subprocess startup/HTTP/stop, worker disabled throughout."""
    database=tmp_path/'empty-no-worker.sqlite3'
    instance=verifier.Instance(database,tmp_path,'cost-free-prepare',False)
    try:
        result=await instance.start()
        assert result['worker'] is False
        assert result['health']['worker_running'] is False
        assert not result['url'].endswith(':8765')
        account=await instance.request('POST','/api/accounts',
            headers={'X-Operation-ID':'controlled-no-worker-account'},
            json={'name':'cost-free subprocess fixture','cash':'100000','holdings':[]})
        ledger=await instance.request('GET',f"/api/accounts/{account['id']}/ledger")
        assert Decimal(ledger['cash'])==Decimal('100000.00')
        with Store(database,'live').connect() as db:
            assert db.execute('SELECT count(*) FROM jobs WHERE attempts>0').fetchone()[0]==0
    finally:
        await instance.stop()
