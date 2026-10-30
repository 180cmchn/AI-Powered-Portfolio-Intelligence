"""Local API boundaries for provider connections, shared roles and predictions."""
from fastapi import HTTPException, Query, Header
from pydantic import BaseModel, ConfigDict, Field
from typing import Literal


class ProviderInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    base_url: str = Field(max_length=2000)
    protocol: Literal['responses', 'chat_completions']
    api_key: str | None = Field(default=None, max_length=8192, repr=False)


class BindingInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    provider_id: str | None = None
    model_id: str | None = None
    reasoning_effort: Literal['low', 'medium', 'high', 'xhigh', 'max'] | None = None


class RoleInput(BindingInput):
    name: str | None = Field(default=None, max_length=120)
    responsibility: str | None = Field(default=None, max_length=4000)
    system_prompt: str | None = Field(default=None, max_length=30000)
    tools: list[str] | None = None
    enabled: bool | None = None


class EnvironmentImportInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    preview_id: str = Field(min_length=32, max_length=32)


def _forecast_filters(kind='formal', **values):
    """Empty browser filters are absent; empty kind keeps the formal-only default."""
    kind = kind.strip() if isinstance(kind, str) else 'formal'
    kind = kind or 'formal'
    if kind not in ('formal', 'exploratory', 'test'):
        raise ValueError('预测记录类型须为 formal、exploratory 或 test')
    result = {'kind': kind}
    for key, value in values.items():
        if isinstance(value, str):
            value = value.strip()
        if value not in (None, ''):
            result[key] = value
    return result


def register_sixth_routes(app, store, service):
    @app.get('/api/environment-connection')
    async def environment_connection():
        return store.environment_connection()

    @app.post('/api/environment-connection/import')
    async def import_environment_connection(data: EnvironmentImportInput):
        result = await store.import_environment_connection(data.preview_id)
        service.resume_waiting_config()
        return result

    @app.get('/api/provider-operations/{operation_id}')
    async def provider_operation(operation_id: str):
        return store.provider_operation(operation_id)

    @app.get('/api/providers')
    async def providers():
        return store.list_providers()

    @app.post('/api/providers', status_code=201)
    async def create_provider(data: ProviderInput, x_operation_id: str | None = Header(default=None, max_length=36)):
        return await store.save_provider(data.model_dump(exclude_unset=True), operation_id=x_operation_id)

    @app.put('/api/providers/{provider_id}')
    async def update_provider(provider_id: str, data: ProviderInput, x_operation_id: str | None = Header(default=None, max_length=36)):
        result = await store.save_provider(data.model_dump(exclude_unset=True), provider_id=provider_id, operation_id=x_operation_id)
        service.resume_waiting_config()
        return result

    @app.get('/api/providers/{provider_id}/models')
    async def models(provider_id: str):
        return await store.discover_models(provider_id)

    @app.post('/api/providers/{provider_id}/models/refresh')
    async def refresh_models(provider_id: str):
        return await store.discover_models(provider_id, force=True)

    @app.post('/api/providers/{provider_id}/models/continue')
    async def continue_models(provider_id: str):
        return await store.discover_models(provider_id, continue_page=True)

    @app.delete('/api/providers/{provider_id}/credential')
    async def clear_credential(provider_id: str):
        return await store.clear_provider_credential(provider_id)

    @app.post('/api/providers/{provider_id}/archive')
    async def archive_provider(provider_id: str):
        return store.archive_provider(provider_id)

    @app.get('/api/agent-roles')
    async def roles():
        return store.list_agent_roles(include_archived=True)

    @app.put('/api/agent-roles/{role_id}')
    async def update_role(role_id: str, data: RoleInput):
        payload = data.model_dump(exclude_unset=True)
        binding = {key: payload.pop(key) for key in ('provider_id', 'model_id', 'reasoning_effort') if key in payload}
        if binding:
            payload['default_binding'] = binding
        result = store.update_agent_definition(role_id, payload)
        service.resume_waiting_config()
        return result

    @app.get('/api/accounts/{account_id}/agent-models')
    async def account_roles(account_id: str):
        store.account(account_id)
        return store.list_agent_roles(account_id)

    @app.put('/api/accounts/{account_id}/agent-models/{role_id}')
    async def update_override(account_id: str, role_id: str, data: BindingInput):
        store.account(account_id)
        result = store.set_account_agent_override(account_id, role_id, data.model_dump(exclude_unset=True))
        service.resume_waiting_config()
        return result

    @app.delete('/api/accounts/{account_id}/agent-models/{role_id}')
    async def clear_override(account_id: str, role_id: str):
        store.account(account_id)
        result = store.clear_account_agent_override(account_id, role_id)
        service.resume_waiting_config()
        return result

    @app.get('/api/accounts/{account_id}/forecast-settings')
    async def forecast_settings(account_id: str):
        store.account(account_id)
        return store.forecast_settings(account_id)

    @app.put('/api/accounts/{account_id}/forecast-settings')
    async def save_forecast_settings(account_id: str, data: dict):
        store.account(account_id)
        return store.save_forecast_settings(account_id, data)

    @app.get('/api/forecast-batches')
    async def forecast_batches(account_id: str | None = None, limit: int = Query(50, ge=1, le=200)):
        return store.forecast_batches(account_id.strip() or None if account_id is not None else None, limit=limit, summary=True)

    @app.get('/api/forecast-window-runs')
    async def forecast_windows(account_id: str | None = None, start: str | None = None, end: str | None = None,
                               limit: int = Query(30, ge=1, le=100), offset: int = Query(0, ge=0)):
        aid = account_id.strip() or None if account_id is not None else None
        return {'page': store.forecast_window_runs(aid, limit=limit, offset=offset, start_date=start, end_date=end),
                'statistics': store.forecast_window_statistics(aid, start_date=start, end_date=end)}

    @app.get('/api/forecast-attempts')
    async def forecast_attempts(batch_id: str | None = None, job_id: str | None = None):
        return store.forecast_attempts(batch_id=batch_id, job_id=job_id)

    @app.get('/api/forecast-batches/{batch_id}')
    async def forecast_batch_detail(batch_id: str):
        with store.connect() as db:
            return store._forecast_batch(db, batch_id, include_attribution=True)

    @app.get('/api/forecasts')
    async def forecasts(account_id: str | None = None, kind: str = 'formal', security_id: str | None = None,
                        team_id: str | None = None, model_id: str | None = None, reasoning_effort: str | None = None,
                        rule_version: str | None = None, start: str | None = None, end: str | None = None,
                        limit: int = Query(30, ge=1, le=100), offset: int = Query(0, ge=0)):
        filters = _forecast_filters(account_id=account_id, kind=kind, symbol=security_id, team_id=team_id,
                       manager_model=model_id, manager_effort=reasoning_effort, rule_version=rule_version, start_date=start, end_date=end)
        return {'page': store.forecasts_page(**filters, limit=limit, offset=offset),
                'statistics': store.forecast_statistics(**filters)}

    @app.get('/api/forecasts/compare')
    async def compare_forecasts(account_ids: list[str] = Query(..., min_length=2, max_length=10),
                        kind: str = 'formal', security_id: str | None = None,
                        rule_version: str | None = None, start: str | None = None, end: str | None = None):
        filters = _forecast_filters(kind=kind, symbol=security_id, rule_version=rule_version,
                                    start_date=start, end_date=end)
        selected = [account_id.strip() for account_id in account_ids]
        if any(not account_id for account_id in selected):
            raise ValueError('比较账户不能为空')
        return store.forecast_comparison(selected, **filters)

    @app.get('/api/forecasts/{forecast_id}')
    async def forecast_detail(forecast_id: str):
        return store.forecast_detail(forecast_id)

    @app.post('/api/forecasts/evaluate')
    async def evaluate():
        # This action cannot generate a model prediction or select a historical winner.
        result = await store.forecast_tick(store.settings(), create_batches=False)
        return {**result, 'enabled': store.settings().get('forecast_evaluation_enabled', True)}

    @app.post('/api/forecasts/{forecast_id}/recheck')
    async def recheck_forecast(forecast_id: str):
        return store.request_forecast_recheck(forecast_id)
