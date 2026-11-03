#!/usr/bin/env python3
"""Prepare offline; run one independently authorized isolated business stage.

The preparation path opens no HTTP client and starts no service. Real execution
uses the production ledger API, Service worker, Agent and ModelClient. Every
stage has a fresh durable budget; aggregate/old-fourteenth ledgers are not inputs.
"""
from __future__ import annotations
import argparse
import asyncio
import hashlib
import json
import math
import os
from contextlib import contextmanager, closing
from pathlib import Path
import sqlite3
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.domain import now
from app.store import Store
from app.runtime_version import source_digest, env_file_stamp
from scripts.verify_fourteenth_live import configure_isolated
from scripts.verify_fifteenth_aggregate import (execution_policy, apply_isolated_overrides,
    configure_network_route, route_description)
from scripts.verify_thirteenth_live import AttemptBudget
from scripts import verify_twelfth_live as base

EVALUATION_ROOT = ROOT / 'data/evaluations'
STAGES = ('a_initial', 'a_after_buy', 'a_after_sell', 'b_cash')
DEFAULT_CAPS = {'a_initial': 16, 'a_after_buy': 8, 'a_after_sell': 8, 'b_cash': 8}
BUSINESS_PORT = 18765
OBSERVATION_SECONDS = 10
BUDGET_KIND = 'fifteenth_independent_business_stage_v1'
FOLLOWUP_PARENT = {'a_after_sell': 'a_initial', 'b_cash': 'a_after_sell'}
SALE = {'symbol': 'SH:518880', 'quantity': '100', 'price': '10.000', 'side': 'sell'}
TIMEOUT_OVERRIDE_FIELDS = (
    'model_first_event_timeout_seconds', 'model_stream_idle_timeout_seconds',
    'model_request_hard_timeout_seconds', 'synthesis_request_hard_timeout_seconds',
    'agent_timeout_seconds', 'analysis_timeout_seconds',
)


def timeout_overrides(args):
    """Explicit per-job limits; never change source settings or old snapshots."""
    result = {}
    for key in TIMEOUT_OVERRIDE_FIELDS:
        value = getattr(args, key, None)
        if value is not None:
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ValueError(key + ' must be a finite positive number of seconds')
            result[key] = value
    return result


def retry_policy(config):
    """Reviewable projection of the unchanged production request retry rules."""
    retries = max(0, min(int(config.get('model_retries', 1)), 3))
    return {'model_retries_source': 'source_settings' if 'model_retries' in config else 'ModelClient_default',
        'configured_model_retries': config.get('model_retries'), 'effective_model_retries': retries,
        'maximum_http_attempts_per_model_call': retries + 1,
        'retryable_http_statuses': [429, 500, 502, 503, 504],
        'accepted_unknown_response_retry': False,
        'max_job_attempts': config.get('max_job_attempts')}


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def file_digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str)+'\n')
    Path(path).chmod(0o600)


def db_digest(path):
    """Hash the logical read-only snapshot, including committed WAL changes."""
    result = hashlib.sha256()
    with sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro', uri=True) as db:
        for line in db.iterdump():
            result.update(line.encode())
    return result.hexdigest()


def copy_database(source, target):
    with sqlite3.connect(Path(source).resolve().as_uri()+'?mode=ro', uri=True) as origin, sqlite3.connect(target) as dest:
        origin.backup(dest)
    Path(target).chmod(0o600)


def configuration_digest(source, account_id):
    """Read only model configuration, not changing account/quote business state."""
    with sqlite3.connect(Path(source).resolve().as_uri()+'?mode=ro', uri=True) as db:
        db.execute('BEGIN')
        values = {table: db.execute('SELECT * FROM '+table+' ORDER BY 1').fetchall()
                  for table in ('model_providers','provider_connections','provider_catalogs','agent_definitions')}
        values['overrides'] = db.execute('SELECT * FROM account_agent_overrides WHERE account_id=? ORDER BY role_id', (account_id,)).fetchall()
        values['meta'] = db.execute("SELECT key,value FROM meta WHERE key='settings' OR key LIKE 'revoked_provider_credentials:%' ORDER BY key").fetchall()
        return digest(values)


def approved_report(summary_path, review_path):
    """A qualified/unevaluated report cannot silently pass this stage gate."""
    summary_path, review_path = Path(summary_path), Path(review_path)
    summary, review = json.loads(summary_path.read_text()), json.loads(review_path.read_text())
    if not (summary.get('published') is True and summary.get('contract_accepted') is True):
        raise ValueError('A real contract-accepted published report is required')
    if not (review.get('accepted') is True and review.get('reviewer') and review.get('reviewed_at')):
        raise ValueError('Explicit accepted=true body-bound content review required; qualified is not an approval')
    body = json.loads((summary_path.parent/'model-result.json').read_text()).get('report')
    if not isinstance(body, str) or not body.strip():
        raise ValueError('Actual model report body is missing')
    body_hash = hashlib.sha256(body.encode()).hexdigest()
    identity = review.get('identity') or review
    if identity.get('report_sha256') != body_hash:
        raise ValueError('Content review belongs to another report')
    job_id = summary['job_id']
    if identity.get('job_id', job_id) != job_id:
        raise ValueError('Content review belongs to another job')
    with sqlite3.connect((summary_path.parent/'evaluation.sqlite3').resolve().as_uri()+'?mode=ro', uri=True) as db:
        rows = db.execute('SELECT a.report,j.state FROM analyses a JOIN jobs j ON j.id=a.job_id WHERE a.job_id=?', (job_id,)).fetchall()
    if len(rows) != 1 or rows[0] != (body, 'completed'):
        raise ValueError('Approved report is not the unique completed persisted publication')
    return {'summary_sha256': file_digest(summary_path), 'review_sha256': file_digest(review_path),
            'report_sha256': body_hash, 'job_id': job_id,
            'reviewer': review['reviewer'], 'reviewed_at': review['reviewed_at']}


def previous_stage_identity(summary_path, review_path, stage):
    """Bind a successful, independently reviewed actual predecessor, read only."""
    summary_path = Path(summary_path).resolve()
    approved = approved_report(summary_path, review_path)
    summary = json.loads(summary_path.read_text())
    if (summary.get('kind') != 'fifteenth_business_stage'
            or summary.get('stage') != FOLLOWUP_PARENT[stage]
            or summary.get('mechanical_passed') is not True
            or summary.get('execution_stopped') is not True):
        raise ValueError('A successful stopped preceding business stage and its accepted review are required')
    database = summary_path.parent/'evaluation.sqlite3'
    with sqlite3.connect(database.as_uri()+'?mode=ro', uri=True) as db:
        row = db.execute('SELECT j.account_id,j.input_version,a.version,r.status,r.input_version '
            'FROM jobs j JOIN accounts a ON a.id=j.account_id JOIN analyses r ON r.job_id=j.id WHERE j.id=?',
            (summary['job_id'],)).fetchone()
    if (not row or row[0] != summary.get('account_id') or row[1] != row[2]
            or row[3] != 'current' or row[4] != row[2]):
        raise ValueError('Preceding report no longer matches its current account version')
    return {'stage': summary['stage'], 'account_id': row[0], 'input_version': row[1],
        'job_id': summary['job_id'], 'database_sha256': db_digest(database), 'approved_report': approved}


def check_previous_stage(frozen):
    parent = frozen.get('previous_stage')
    if parent and previous_stage_identity(parent['summary'], parent['review'], frozen['stage']) != parent['identity']:
        raise ValueError('Preceding stage evidence changed after preparation')


class ReadOnlyStore(Store):
    """Inspect an existing failed execution without startup or migrations."""
    def __init__(self, path):
        self.path, self.mode = Path(path), 'live'

    @contextmanager
    def connect(self):
        with closing(sqlite3.connect(self.path.resolve().as_uri()+'?mode=ro', uri=True)) as db:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA query_only=ON')
            yield db


def retry_parent_identity(execution, stage, previous, _seen=None):
    """Bind the failed post-sale task and its already committed single sale."""
    execution = Path(execution).resolve()
    seen = set(_seen or ())
    if execution in seen:
        raise ValueError('Retry parent lineage contains a cycle')
    seen.add(execution)
    summary = json.loads((execution/'summary.json').read_text())
    if (stage != 'a_after_sell' or summary.get('kind') != 'fifteenth_business_stage'
            or summary.get('stage') != stage or summary.get('execution_stopped') is not True
            or summary.get('published') is not False or summary.get('contract_accepted') is not False
            or summary.get('previous_stage') != previous):
        raise ValueError('Retry requires a stopped unpublished failed sale with the same approved predecessor')
    store = ReadOnlyStore(execution/'evaluation.sqlite3')
    job = store.job(summary['job_id'])
    account = store.account(job['account_id'])
    if (job['state'] != 'failed' or job['account_id'] != previous['account_id']
            or summary.get('account_id') != account['id'] or summary.get('account_a_id') != account['id']
            or account.get('latest_job_id') != job['id']
            or job['input_version'] != 5 or account['version'] != 5
            or previous['input_version'] != 4):
        raise ValueError('Retry requires the same failed A account and unchanged post-sale version five')
    trades = summary.get('trades') or []
    ancestor = None
    origin_summary = execution/'summary.json'
    origin_job_id = job['id']
    if not trades and isinstance(summary.get('retry_parent'),dict):
        ancestor = retry_parent_identity(summary['retry_parent']['execution'],stage,previous,seen)
        retry = summary.get('retry_receipt') or {}
        if (ancestor != summary['retry_parent'] or summary.get('original_sale') != ancestor['original_sale']
                or job.get('parent_job_id') != ancestor['job_id'] or job['id'] == ancestor['job_id']
                or (retry.get('job_id') or retry.get('id')) != job['id'] or retry.get('merged') is not False
                or retry.get('account_id') != account['id']
                or retry.get('parent_job_id') != ancestor['job_id'] or retry.get('input_version') != 5):
            raise ValueError('Failed retry lineage or retry API receipt differs from its frozen parent')
        origin_summary = Path(ancestor['original_sale']['source_summary'])
        original = json.loads(origin_summary.read_text())
        trades = original.get('trades') or []
        origin_job_id = original['job_id']
    elif summary.get('retry_parent'):
        raise ValueError('A retry must not submit another sale')
    if len(trades) != 1:
        raise ValueError('Failed sale execution must retain its one original sale receipt')
    trade = trades[0]; submitted = trade.get('input') or {}; receipt = trade.get('receipt') or {}
    if ({key:submitted.get(key) for key in SALE} != SALE or submitted.get('mode') != 'simulated'
            or submitted.get('expected_version') != 4 or receipt.get('input_version') != 5
            or receipt.get('account_id') != account['id'] or receipt.get('operation_id') != submitted.get('operation_id')
            or (receipt.get('research') or {}).get('job_id') != origin_job_id):
        raise ValueError('Failed execution sale input/receipt does not match its task and ledger version')
    ledger = store.ledger(account['id'])
    sales = [entry for entry in ledger['entries'] if entry.get('kind') == 'trade' and entry.get('side') == 'sell']
    with store.connect() as db:
        operation = db.execute('SELECT receipt FROM ledger_operations WHERE account_id=? AND operation_id=?',
            (account['id'], submitted['operation_id'])).fetchone()
        published = db.execute('SELECT 1 FROM analyses WHERE job_id=?', (job['id'],)).fetchone()
        checkpoint = db.execute('SELECT body,created_at,resume_count FROM synthesis_checkpoints WHERE job_id=?', (job['id'],)).fetchone()
    if (published or len(sales) != 1 or sales[0].get('id') != receipt.get('entry_id')
            or sales[0] != receipt.get('entry') or not operation or json.loads(operation[0]) != receipt
            or ledger_facts(ledger) != ledger_facts(summary.get('prepared_ledger') or {})
            or ledger['cash'] != '87479.90'):
        raise ValueError('Failed sale receipt, publication state or committed ledger differs')
    with sqlite3.connect((execution/'business-attempts.sqlite3').as_uri()+'?mode=ro', uri=True) as db:
        authorization = json.loads(db.execute('SELECT body FROM business_authorization').fetchone()[0])
        used = db.execute('SELECT count(*) FROM attempts').fetchone()[0]
    if (authorization.get('kind') != BUDGET_KIND or authorization.get('stage') != stage
            or (authorization.get('frozen') or {}).get('source_sha256') != summary.get('source_sha256')
            or used != (summary.get('budget') or {}).get('reserved_attempts')):
        raise ValueError('Failed sale execution request ledger identity differs')
    body = json.loads(checkpoint['body']) if checkpoint else {}
    return {'execution':str(execution),'summary_sha256':file_digest(execution/'summary.json'),
        'database_sha256':db_digest(store.path),'attempt_ledger_sha256':db_digest(execution/'business-attempts.sqlite3'),
        'job_id':job['id'],'account_id':account['id'],'input_version':5,'prior_http_attempts':used,
        'execution_policy':summary['execution_policy'],'retry_policy':summary['retry_policy'],
        'effective_team_id':job['config_snapshot']['_agent_team']['team_id'],
        'ledger_facts_sha256':digest(ledger_facts(ledger)),
        'original_sale':{'input':submitted,'entry_id':receipt['entry_id'],'operation_id':receipt['operation_id'],
            'receipt_sha256':digest(receipt),'source_summary':str(origin_summary)},
        **({'retry_parent':ancestor} if ancestor else {}),
        'checkpoint':({'created_at':checkpoint['created_at'],'resume_count':checkpoint['resume_count'],
            'body_sha256':digest(body),'report_count':len(body.get('reports') or []),
            'calculation_count':len(body.get('calculations') or [])} if checkpoint else None)}


def check_retry_parent(frozen):
    expected = frozen.get('retry_parent')
    if expected and retry_parent_identity(expected['execution'], frozen['stage'], frozen['previous_stage']['identity']) != expected:
        raise ValueError('Failed retry-parent evidence changed after preparation')


class StageBudget:
    """Reuse atomic AttemptBudget reservations with an independent stage seal."""
    def __init__(self, path):
        self.path = Path(path).resolve()
        if not self.path.is_file():
            raise ValueError('Existing independent business-stage ledger required')
        with sqlite3.connect(self.path) as db:
            try:
                self.metadata = json.loads(db.execute('SELECT body FROM business_authorization').fetchone()[0])
            except (sqlite3.Error, TypeError, ValueError) as exc:
                raise ValueError('Aggregate or old research ledgers cannot authorize a business stage') from exc
        if self.metadata.get('kind') != BUDGET_KIND:
            raise ValueError('Wrong authorization ledger kind')
        self.budget = AttemptBudget(self.path)

    @classmethod
    def create(cls, path, stage, maximum, note, preparation_hash, frozen=None):
        if stage not in STAGES or type(maximum) is not int or not 1 <= maximum <= 40 or not note.strip():
            raise ValueError('Explicit stage, positive bounded HTTP cap and authorization note required')
        path = Path(path)
        path.touch(exist_ok=False, mode=0o600)
        AttemptBudget(path, maximum)
        metadata = {'kind': BUDGET_KIND, 'stage': stage, 'authorization_note': note,
                    'preparation_sha256': preparation_hash, 'authorized_at': now(), 'frozen': frozen}
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE business_authorization(body TEXT NOT NULL)')
            db.execute('INSERT INTO business_authorization VALUES(?)', (json.dumps(metadata),))
            db.execute('CREATE TABLE admitted_jobs(job_id TEXT PRIMARY KEY)')
            db.execute('CREATE TABLE stops(reason TEXT NOT NULL,at TEXT NOT NULL)')
            db.execute('CREATE TABLE denied_jobs(job_id TEXT,at TEXT NOT NULL)')
        return cls(path)

    def admit(self, job_id):
        with sqlite3.connect(self.path) as db:
            db.execute('INSERT INTO admitted_jobs VALUES(?)', (job_id,))

    def check_job(self, job_id):
        from app.model import ModelBudgetExceeded
        with sqlite3.connect(self.path) as db:
            allowed = db.execute('SELECT 1 FROM admitted_jobs WHERE job_id=?', (job_id,)).fetchone()
            stopped = db.execute('SELECT 1 FROM stops').fetchone()
            if not allowed:
                db.execute('INSERT INTO denied_jobs VALUES(?,?)', (job_id, now()))
        if not allowed or stopped:
            raise ModelBudgetExceeded('This isolated stage has no authorization for another task or an unknown-outcome continuation')

    def halt(self, reason):
        with sqlite3.connect(self.path) as db:
            db.execute('INSERT INTO stops VALUES(?,?)', (reason, now()))

    def check_frozen(self):
        from app.model import ModelBudgetExceeded
        frozen = self.metadata.get('frozen')
        if frozen is None:
            raise ModelBudgetExceeded('Production HTTP requires a frozen preparation identity')
        try:
            check_previous_stage(frozen)
        except (ValueError, OSError, sqlite3.Error) as exc:
            self.halt('previous_stage_evidence_changed')
            raise ModelBudgetExceeded('Preceding stage evidence changed before HTTP') from exc
        try:
            check_retry_parent(frozen)
        except (ValueError, OSError, sqlite3.Error, KeyError) as exc:
            self.halt('retry_parent_evidence_changed')
            raise ModelBudgetExceeded('Failed retry-parent evidence changed before HTTP') from exc
        if (source_digest()!=frozen['source_sha256'] or env_file_stamp()!=frozen['environment_stamp']
                or configuration_digest(frozen['configuration_source'], frozen['reference_account'])!=frozen['configuration_sha256']):
            self.halt('source_environment_or_configuration_changed')
            raise ModelBudgetExceeded('Source, environment or source model configuration changed before HTTP')

    def reserve(self):
        from app.model import ModelBudgetExceeded
        with sqlite3.connect(self.path, timeout=15) as db:
            db.execute('BEGIN IMMEDIATE')
            maximum=db.execute('SELECT maximum FROM budget').fetchone()[0]
            count=db.execute('SELECT count(*) FROM attempts').fetchone()[0]
            if db.execute('SELECT 1 FROM stops').fetchone() or count>=maximum:
                raise ModelBudgetExceeded('Independent business-stage HTTP ceiling or stop latch reached')
            db.execute('INSERT INTO attempts(reserved_at) VALUES(?)',(now(),))

    def check_timeouts(self, config):
        frozen = self.metadata.get('frozen') or {}
        expected = frozen.get('timeout_policy')
        # Older sealed ledgers did not record a separate timeout projection.
        # New runs always bind all six fields, including unchanged source values.
        if expected is not None and {key:config.get(key) for key in TIMEOUT_OVERRIDE_FIELDS} != expected:
            self.halt('actual_http_timeout_policy_differs_from_preparation')
            raise ValueError('Actual HTTP timeout policy differs from frozen preparation')

    def check_retries(self, config):
        expected = (self.metadata.get('frozen') or {}).get('retry_policy')
        # New preparations seal this policy. Do not rewrite historical ledgers.
        if expected is not None and retry_policy(config) != expected:
            self.halt('actual_http_retry_policy_differs_from_preparation')
            raise ValueError('Actual HTTP retry policy differs from frozen preparation')

    def status(self):
        with sqlite3.connect(self.path) as db:
            return {**self.budget.status(), 'kind': BUDGET_KIND, 'stage': self.metadata['stage'],
                'stops': [dict(zip(('reason','at'), r)) for r in db.execute('SELECT reason,at FROM stops')],
                'denied_job_ids': [r[0] for r in db.execute('SELECT DISTINCT job_id FROM denied_jobs')]}


def install_budget(path):
    """Child-process-only hooks; production request/response handling stays intact."""
    from app.model import ModelClient
    original_init, original_post = ModelClient.__init__, ModelClient._post
    budget = StageBudget(path)
    route = (budget.metadata.get('frozen') or {}).get('network_route','environment')
    if route not in ('environment','direct'):
        raise ValueError('Unknown frozen model network route')
    def transport_state(client):
        transport = client.http._transport
        pool = getattr(transport,'_pool',None)
        tls = getattr(pool,'_ssl_context',None)
        return (client.http,transport,tuple(client.http._mounts.items()),pool,
            getattr(tls,'verify_mode',None),getattr(tls,'check_hostname',None),client.http.trust_env)
    def check_route(client):
        if getattr(client,'_business_network_route',None) != route or getattr(client,'_business_transport_state',None) != transport_state(client):
            budget.halt('actual_http_network_route_differs_from_preparation')
            raise ValueError('Actual model network route differs from frozen preparation')
        if route=='direct':
            import ssl
            state = transport_state(client)
            if client.http._mounts or state[4:] != (ssl.CERT_REQUIRED,True,True):
                budget.halt('actual_http_network_route_differs_from_preparation')
                raise ValueError('Direct model route must retain TLS verification and environment CA without proxy mounts')
    def initialize(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        original_guard = self.before_request
        async def reserve():
            if original_guard:
                await original_guard()
            budget.check_frozen()
            budget.check_timeouts(self.config)
            budget.check_retries(self.config)
            check_route(self)
            budget.reserve()
        self.before_request = reserve
    async def post(self, endpoint, payload, session=None):
        budget.check_job(session.task_id if session else None)
        budget.check_timeouts(self.config)
        budget.check_retries(self.config)
        frozen = budget.metadata.get('frozen')
        if not frozen:
            raise ValueError('No frozen effective matrix')
        if not hasattr(self,'_business_network_route'):
            await configure_network_route(self,route)
            self._business_network_route = route
            self._business_transport_state = transport_state(self)
        check_route(self)
        role = self.config.get('role_id')
        expected = frozen['roles'].get(role)
        actual = {'model':self.model,'base_url':self.base_url,'protocol':self.protocol,'reasoning_effort':self.reasoning_effort}
        sent_effort = (payload.get('reasoning') or {}).get('effort') if self.protocol=='responses' else payload.get('reasoning_effort')
        sent_limits = {key:payload[key] for key in ('max_output_tokens','max_completion_tokens','max_tokens') if key in payload}
        limit_field = ('max_output_tokens' if self.protocol=='responses' else
            'max_completion_tokens' if self.config.get('chat_use_max_completion_tokens',False) else 'max_tokens')
        expected_limits = {} if frozen['max_output_tokens'] is None else {limit_field:frozen['max_output_tokens']}
        limit_matches = sent_limits == expected_limits and all(type(value) is int for value in sent_limits.values())
        if (actual!=expected or sent_effort!=expected['reasoning_effort']
                or not limit_matches or payload.get('model')!=expected['model']):
            budget.halt('actual_http_configuration_differs_from_preparation')
            raise ValueError('Actual HTTP model/effort/output configuration differs from reviewed matrix')
        request_count = len(self.requests)
        try:
            return await original_post(self, endpoint, payload, session)
        except BaseException:
            if len(self.requests) > request_count:
                item = self.requests[-1]
                known_terminal = item.get('stream_terminal') in {
                    'response.completed', 'response.incomplete', 'response.failed', 'response.cancelled',
                } and item.get('error') != 'invalid_stream_terminal'
                origin_sent = any(event.get('scope') == 'origin' and any(boundary in event.get('event', '')
                    for boundary in ('send_request_headers.', 'send_request_body.', 'receive_response_headers.'))
                    for event in item.get('transport_events', []))
                uncertain = (item.get('request_accepted') is True or origin_sent
                    or item.get('retry_suppressed') in {'accepted_or_sent_request_outcome_unknown','upstream_execution_may_continue'})
                # Cancellation/EOF/parser rejection do not prove an accepted
                # generation ended. A validated terminal or explicit HTTP
                # rejection does, even if local cleanup is later cancelled.
                # A failed child ends its own Runtime loop without replaying
                # that request. The manager can still independently summarize
                # completed evidence; only its own unknown outcome stops the job.
                if role == 'manager' and uncertain and item.get('request_accepted') is not False and not known_terminal:
                    budget.halt('accepted_or_sent_request_outcome_unknown')
            raise
        finally:
            for item in self.requests[request_count:]:
                item.update(isolated_network_route=route,isolated_network_route_verified=True)
    ModelClient.__init__, ModelClient._post = initialize, post
    def restore():
        ModelClient.__init__, ModelClient._post = original_init, original_post
    return restore


class Instance(base.Instance):
    child_script = Path(__file__).resolve()

    async def start(self):
        # Preserve the existing Instance HTTP/stop methods, but atomically bind
        # the exact reviewed port. No release/rebind race or random fallback.
        import socket
        import subprocess
        import httpx
        self.log=(self.output/(self.name+'.log')).open('w')
        Path(self.log.name).chmod(0o600)
        with socket.socket() as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(('127.0.0.1',BUSINESS_PORT))
            listener.listen(128)
            args=[sys.executable,str(self.child_script),'--serve-child',str(self.database),
                  '--listen-fd',str(listener.fileno())]
            if self.worker:args.append('--worker')
            self.process=subprocess.Popen(args,cwd=ROOT,pass_fds=(listener.fileno(),),
                env={**os.environ,'APP_DB_PATH':str(self.database),'APP_MODE':'live',
                     'APP_HOST':'127.0.0.1','APP_PORT':str(BUSINESS_PORT)},stdout=self.log,stderr=self.log)
        self.base=f'http://127.0.0.1:{BUSINESS_PORT}'
        self.api=httpx.AsyncClient(base_url=self.base,timeout=45,trust_env=False)
        for _ in range(100):
            if self.process.poll() is not None:raise RuntimeError('Isolated child exited before readiness')
            try:
                health=await self.request('GET','/api/health')
                if health['ready']:
                    return {'name':self.name,'url':self.base,'pid':self.process.pid,
                            'worker':self.worker,'health':health}
            except httpx.HTTPError:pass
            await asyncio.sleep(.2)
        raise RuntimeError('Isolated child did not become ready')


def prepare(args):
    """Offline configuration/manifest preparation; not a business execution."""
    stage = args.stage
    if stage not in ('a_initial', *FOLLOWUP_PARENT):
        raise ValueError('a_after_buy is not implemented; the A change stage is the explicit gold sale')
    retry_execution = getattr(args, 'retry_execution', None)
    if retry_execution and stage != 'a_after_sell':
        raise ValueError('retry-execution currently supports only the same failed a_after_sell stage')
    cap = args.max_http_attempts or (40 if retry_execution else DEFAULT_CAPS[stage])
    if type(cap) is not int or not 2 <= cap <= 40:
        raise ValueError('HTTP cap must be an integer from 2 to 40 including the two final reserves')
    explicit_timeouts = timeout_overrides(args)
    route = getattr(args,'network_route','environment')
    if route not in ('environment','direct'):
        raise ValueError('network_route must be environment or direct')
    omit_output_limit = getattr(args, 'omit_output_limit', False)
    if omit_output_limit and getattr(args, 'max_output_tokens', None) is not None:
        raise ValueError('omit-output-limit and max-output-tokens are mutually exclusive')
    approved = approved_report(args.approved_summary, args.content_review)
    output = args.output.resolve()
    if not output.is_relative_to(EVALUATION_ROOT.resolve()) or output == EVALUATION_ROOT.resolve():
        raise ValueError('Use a new isolated data/evaluations child directory')
    previous = None
    if stage == 'a_initial':
        if getattr(args, 'previous_stage', None):
            raise ValueError('a_initial requires a fresh isolated database')
    else:
        if not getattr(args, 'previous_stage', None) or Path(args.previous_stage).resolve() != Path(args.approved_summary).resolve():
            raise ValueError('Follow-up requires --previous-stage to identify the approved preceding summary')
        previous = previous_stage_identity(args.previous_stage, args.content_review, stage)
    retry_parent = retry_parent_identity(retry_execution, stage, previous) if retry_execution else None
    output.mkdir(parents=True, exist_ok=False)
    output.chmod(0o700)
    database = output/'prepared.sqlite3'
    config_identity = configuration_digest(args.source,args.reference_account)
    store, cfg = configure_isolated(args.source, database if previous is None else output/'configuration.sqlite3', args.reference_account)
    if configuration_digest(args.source,args.reference_account)!=config_identity:
        raise ValueError('Source configuration changed during offline preparation')
    baseline = {'team_id': store.freeze_agent_team()['team_id'], 'policy': execution_policy(cfg)}
    overrides = {}
    for attr in ('manager_reasoning_effort','max_output_tokens'):
        value = getattr(args, attr, None)
        if value is not None:
            overrides[attr] = value
    if omit_output_limit:
        overrides['max_output_tokens'] = None
    if route=='direct':
        overrides['network_route'] = route
    if 'manager_reasoning_effort' in overrides:
        from app.config import REASONING_EFFORTS
        if overrides['manager_reasoning_effort'] not in REASONING_EFFORTS:
            raise ValueError('Invalid explicit manager effort')
    if overrides.get('max_output_tokens') is not None and (type(overrides['max_output_tokens']) is not int or overrides['max_output_tokens'] <= 0):
        raise ValueError('Output limit must be an explicit positive integer')
    overrides.update(explicit_timeouts)
    cfg.update(explicit_timeouts)
    team = apply_isolated_overrides(store, cfg, overrides)
    if previous:
        # Copy only into the new isolated stage. Original reports/materials stay
        # in their original account; B does not acquire A's report or permissions.
        from app.store import Store
        configuration = store.path
        copy_database((Path(retry_parent['execution']) if retry_parent else Path(args.previous_stage).resolve().parent)/'evaluation.sqlite3', database)
        store = Store(database, 'live')
        with sqlite3.connect(configuration) as origin, store.connect() as dest:
            for table in ('model_providers','provider_connections','provider_catalogs','agent_definitions'):
                dest.execute('DELETE FROM '+table)
                for row in origin.execute('SELECT * FROM '+table):
                    dest.execute('INSERT INTO '+table+' VALUES('+','.join('?' for _ in row)+')', tuple(row))
            dest.execute("DELETE FROM meta WHERE key LIKE 'revoked_provider_credentials:%'")
            for row in origin.execute("SELECT key,value FROM meta WHERE key LIKE 'revoked_provider_credentials:%'"):
                dest.execute('INSERT INTO meta VALUES(?,?)', tuple(row))
            # Isolated roles already contain the reviewed effective bindings.
            dest.execute('DELETE FROM account_agent_overrides')
        if store.freeze_agent_team()['team_id'] != team['team_id']:
            raise ValueError('Copied stage does not retain the reviewed effective model matrix')
        if previous_stage_identity(args.previous_stage, args.content_review, stage) != previous:
            raise ValueError('Preceding business snapshot changed while preparing')
    cfg.update(max_model_calls=cap, final_submission_reserved_calls=2, max_job_attempts=1,
               debounce_seconds=600, scheduled_research_enabled=False,
               market_refresh_enabled=False, forecast_evaluation_enabled=False, news_queries=[])
    if retry_parent and (execution_policy(cfg) != retry_parent['execution_policy']
            or retry_policy(cfg) != retry_parent['retry_policy'] or team['team_id'] != retry_parent['effective_team_id']):
        raise ValueError('Retry must preserve the failed execution model matrix and generation/timeout/retry policies')
    store.set_meta('settings', cfg)
    from app.agents import MANAGER_TOOLS
    final_schema=next(t for t in MANAGER_TOOLS if t['name']=='finish_analysis')
    manifest = {'kind':'fifteenth_business_preparation','stage':stage,'prepared_at':now(),
        'source_sha256':source_digest(),'environment_stamp':env_file_stamp(),
        'prepared_database_sha256':db_digest(database),'approved_report':approved,
        'baseline_configuration':baseline,'effective_team_id':team['team_id'],
        'source_configuration_sha256':config_identity,
        'isolated_overrides':overrides,'execution_policy':execution_policy(cfg),'retry_policy':retry_policy(cfg),
        'network_route':route,'network_route_description':route_description(route),
        'production_final_schema_sha256':digest(final_schema),'production_final_schema':final_schema,
        'provider_connections':[{k:p.get(k) for k in ('id','base_url','protocol','revision','credential_revision')} for p in team['connections'].values()],
        'initial_cash':'100000','simulated_trades':base.INPUTS,
        'simulated_fees_by_trade':['5.00','5.10','5.00'],
        'simulated_fee_plan':__import__('app.ledger',fromlist=['default_plan']).default_plan(),'expected_cash':'86484.90','expected_total_cost':'13515.10','expected_fees':'15.10',
        'role_matrix':[{k:r[k] for k in ('role_id','resolved','config_error')} for r in store.list_agent_roles()],
        'maximum_http_attempts':cap,'http_counting_boundary':'Model-provider HTTP attempts across manager, children and corrections; quote/material HTTP is separate and may incur unknown fees',
        'final_submission_reserved_calls':min(cap,max(2,min(4,int(cfg.get('final_submission_reserved_calls',2))))),
        'network_started':False,'model_requests':0,
        'database':str(output/'execution/evaluation.sqlite3'),
        'listener':f'http://127.0.0.1:{BUSINESS_PORT}','listener_policy':'Parent atomically reserves this exact port during authorized run and passes the descriptor; occupied port rejects, no fallback; daily8765 untouched',
        'stages_covered_by_this_execution':[stage],'other_stages_not_run':[s for s in STAGES if s != stage],
        'required_matrix_stages':['a_initial','a_after_sell','b_cash'],
        'unselected_alternative_stages':['a_after_buy'],
        'observation_seconds_each':OBSERVATION_SECONDS,'input_prices':'Declared simulated accounting inputs, never current quotes',
        'fee_boundary':'No verified monetary ceiling; model, search/tool and interrupted-request fees may be unknown',
        'budget_boundary':'A fresh per-job configured engineering budget counts manager, child and correction HTTP attempts; it is not a cumulative user authorization pool across stages. Historical ledgers and stop latches remain unchanged and are not transferred.',
        'automatic_followups':'New event tasks remain visible; this single-job stage does not authorize their model calls',
        'previous_stage':previous,'account_a_id':previous['account_id'] if previous else None,
        'retry_parent':retry_parent,
        'operation_prefix':'fifteenth-business-'+uuid.uuid4().hex[:12]}
    if previous:
        manifest.update(initial_cash=None, simulated_trades=[SALE] if stage=='a_after_sell' else [],
            simulated_fees_by_trade=['5.00'] if stage=='a_after_sell' else [],
            expected_cash='87479.90' if stage=='a_after_sell' else '100000',
            expected_total_cost='12510.10' if stage=='a_after_sell' else '0.00',
            expected_fees='20.10' if stage=='a_after_sell' else '0.00',
            stage_input={'sale':SALE, 'trade_clock':'Actual current time; production T+0/calendar/sellable checks'} if stage=='a_after_sell' else
                {'cash':'100000','holdings':[], 'candidates':['SZ:159278'],
                 'material_boundary':'Same isolated source store; production account permissions apply. A reports remain A reports and are never B evidence by copying conclusions.'})
    if retry_parent:
        manifest.update(simulated_trades=[],simulated_fees_by_trade=[],
            stage_input={'retry_job_id':retry_parent['job_id'],'original_sale':retry_parent['original_sale'],
                'trade_boundary':'The sale was already committed in the failed execution; this retry performs no trade.',
                'recovery_boundary':'Normal retry API and Service decide continue_research, synthesis_only or fresh research from current checkpoint validity; no TTL extension or forced recovery.'})
        check_retry_parent({'stage':stage,'previous_stage':{'identity':previous},'retry_parent':retry_parent})
    write(output/'preparation.json', manifest)
    write(output/'private-source.json', {'configuration_source':str(args.source.resolve()),
        'preparation_sha256':file_digest(output/'preparation.json'),
        'network_route':route,
        'reference_account':args.reference_account,'approved_summary':str(Path(args.approved_summary).resolve()),
        'content_review':str(Path(args.content_review).resolve())})
    return manifest


async def trade(instance, account_id, values, operation):
    base_url = '/api/accounts/'+account_id
    account = await instance.request('GET',base_url)
    fees = await instance.request('GET',base_url+'/fee-plan')
    payload = {**values,'operation_id':operation,'expected_version':account['version'],
        'fee_plan_version':fees['version'],'mode':'simulated','traded_at':now(),
        'note':'Explicit isolated simulated arithmetic input; not market price or broker execution'}
    preview = await instance.request('POST',base_url+'/trades/preview',json=payload)
    receipt = await instance.request('POST',base_url+'/trades',json=payload)
    if preview['cash_after'] != receipt['cash_after']:
        raise ValueError('Ledger preview and committed cash differ')
    return {'input':payload,'preview':preview,'receipt':receipt}


def ledger_facts(ledger):
    # Sellable quantities can naturally change at a trading-day boundary;
    # comparison is of committed accounting, not a frozen display clock.
    return {key:ledger.get(key) for key in ('cash','entries','cash_entries','summary')} | {
        'positions':[{key:row.get(key) for key in ('symbol','quantity','total_cost','cost_price','lots','fees_included_in_cost')}
                     for row in ledger.get('positions',[])]}


def persisted_view(store, job_id):
    job = store.job(job_id)
    account_id = job['account_id']
    with store.connect() as db:
        reports = [dict(r) for r in db.execute('SELECT id,report,input_version,status FROM analyses WHERE job_id=?',(job_id,))]
        forecasts = [dict(r) for r in db.execute('SELECT id FROM forecasts WHERE job_id=?',(job_id,))]
    return {'job_state':job['state'],'input_version':job['input_version'],
        'account_version':store.account(account_id)['version'],
        'reports':[{'id':r['id'],'report_sha256':hashlib.sha256(r['report'].encode()).hexdigest(),
                    'input_version':r['input_version'],'status':r['status']} for r in reports],
        'forecast_ids':[r['id'] for r in forecasts],
        'recommendation_ids':[r['id'] for r in store.recommendations_page(account_id,view='all',limit=100)['items']]}


def report_bodies(store):
    with store.connect() as db:
        return {r['id']: {'account_id':r['account_id'],'job_id':r['job_id'],'input_version':r['input_version'],
            'report_sha256':hashlib.sha256(r['report'].encode()).hexdigest()}
            for r in db.execute('SELECT id,account_id,job_id,input_version,report FROM analyses')}


def forecast_bodies(store):
    with store.connect() as db:
        return {row['id']:digest(dict(row)) for row in db.execute('SELECT * FROM forecasts')}


def record_retry_mode(summary, store):
    if not summary.get('retry_parent') or not summary.get('job_id'):
        return
    job = store.job(summary['job_id'], events=True)
    events = job.get('events') or []
    resumed = [event for event in events if event.get('type') in {'research_resuming','synthesis_resuming'}]
    entered = any(event.get('type') == 'agent_started' for event in events)
    summary['retry_execution'] = {'parent_job_id':summary['retry_parent']['job_id'], 'job_id':job['id'],
        'actual_recovery_mode':resumed[-1]['recovery_mode'] if resumed else 'fresh_research' if entered else 'not_started',
        'checkpoint_job_id':resumed[-1].get('checkpoint_job_id') if resumed else None,
        'service_events':resumed,
        'note':'Only actual Service recovery events establish checkpoint reuse. Without one, saved research is not claimed as restored.'}


async def run_prepared(args):
    from app.store import Store
    from app.calendar import market_state
    root = args.prepared.resolve()
    manifest = json.loads((root/'preparation.json').read_text())
    private = json.loads((root/'private-source.json').read_text())
    if file_digest(root/'preparation.json') != private['preparation_sha256']:
        raise ValueError('Reviewed preparation changed')
    route = getattr(args,'network_route','environment')
    if (route not in ('environment','direct') or route != manifest.get('network_route','environment')
            or route != private.get('network_route','environment')
            or manifest.get('isolated_overrides',{}).get('network_route','environment') != route
            or manifest.get('network_route_description',route_description(route)) != route_description(route)):
        raise ValueError('Network route differs from frozen preparation; repeat the explicit route')
    if not (getattr(args,'run_real',False) and args.authorization_note and args.authorization_note.strip()
            and args.authorize_stage == manifest['stage'] and args.authorized_http_attempts == manifest['maximum_http_attempts']):
        raise ValueError('Explicit matching stage, configured per-job HTTP cap and authorization note are required')
    for key, value in timeout_overrides(args).items():
        if value != manifest['execution_policy'].get(key):
            raise ValueError('Run timeout override differs from frozen preparation: ' + key)
    if source_digest()!=manifest['source_sha256'] or env_file_stamp()!=manifest['environment_stamp']:
        raise ValueError('Source/environment changed after preparation')
    if db_digest(root/'prepared.sqlite3') != manifest['prepared_database_sha256']:
        raise ValueError('Prepared isolated database changed')
    if configuration_digest(private['configuration_source'],private['reference_account']) != manifest['source_configuration_sha256']:
        raise ValueError('Source model configuration changed after preparation')
    if approved_report(private['approved_summary'],private['content_review']) != manifest['approved_report']:
        raise ValueError('Reviewed report evidence changed after preparation')
    parent_guard = {'stage':manifest['stage'], 'previous_stage': None}
    if manifest.get('previous_stage'):
        parent_guard['previous_stage'] = {'summary':private['approved_summary'], 'review':private['content_review'],
            'identity':manifest['previous_stage']}
    if manifest.get('retry_parent'):
        parent_guard['retry_parent'] = manifest['retry_parent']
    check_previous_stage(parent_guard)
    check_retry_parent(parent_guard)
    output = root/'execution';output.mkdir(exist_ok=False);output.chmod(0o700)
    check_store, check_cfg = configure_isolated(Path(private['configuration_source']),output/'configuration-check.sqlite3',private['reference_account'])
    if {'team_id':check_store.freeze_agent_team()['team_id'],'policy':execution_policy(check_cfg)} != manifest['baseline_configuration']:
        raise ValueError('Source model matrix or execution policy changed before isolated overrides')
    copy_database(root/'prepared.sqlite3',output/'evaluation.sqlite3')
    store = Store(output/'evaluation.sqlite3','live')
    if store.freeze_agent_team()['team_id'] != manifest['effective_team_id'] or execution_policy(store.settings()) != manifest['execution_policy']:
        raise ValueError('Frozen effective settings differ')
    if retry_policy(check_cfg) != manifest['retry_policy'] or retry_policy(store.settings()) != manifest['retry_policy']:
        raise ValueError('Frozen request retry policy differs')
    budget = StageBudget.create(output/'business-attempts.sqlite3',manifest['stage'],args.authorized_http_attempts,
        args.authorization_note,file_digest(root/'preparation.json'), frozen={
            'source_sha256':manifest['source_sha256'],'environment_stamp':manifest['environment_stamp'],
            'configuration_source':private['configuration_source'],'reference_account':private['reference_account'],
            'configuration_sha256':manifest['source_configuration_sha256'],
            **parent_guard,
            'max_output_tokens':manifest['execution_policy']['max_output_tokens'],
            'timeout_policy':{key:manifest['execution_policy'].get(key) for key in TIMEOUT_OVERRIDE_FIELDS},
            'retry_policy':manifest['retry_policy'],
            'network_route':route,
            'roles':{r['role_id']:{'model':r['resolved']['model_id'],
                'base_url':store.freeze_agent_team()['connections'][r['resolved']['provider_id']]['base_url'],
                'protocol':store.freeze_agent_team()['connections'][r['resolved']['provider_id']]['protocol'],
                'reasoning_effort':r['resolved']['reasoning_effort']} for r in manifest['role_matrix']}})
    summary = {'kind':'fifteenth_business_stage','stage':manifest['stage'],'started_at':now(),
        'source_sha256':manifest['source_sha256'],'market_state':market_state(),
        'authorization_note':args.authorization_note,'published':False,'contract_accepted':False,
        'mechanical_passed':False,'content_assessment':'not_evaluated','overall_business_acceptance_passed':False,
        'instances':[],'trades':[],'observations':[],'approved_report':manifest['approved_report'],
        'daily_database_writes':0,'fees_known':False,'account_a_id':manifest.get('account_a_id'),
        'uncovered_stages':manifest['other_stages_not_run'],
        'required_matrix_stages':manifest.get('required_matrix_stages',['a_initial','a_after_sell','b_cash']),
        'unselected_alternative_stages':['a_after_buy'],
        'final_submission_reserved_calls':manifest['final_submission_reserved_calls']}
    summary['execution_policy'] = manifest['execution_policy']
    summary['retry_policy'] = manifest['retry_policy']
    summary['network_route'] = route
    summary['network_route_description'] = route_description(route)
    summary['budget_boundary'] = manifest['budget_boundary']
    inherited_reports = report_bodies(store)
    inherited_forecasts = forecast_bodies(store)
    inherited_jobs = {item['task']['id'] for item in base.task_audit(store)}
    summary['previous_stage'] = manifest.get('previous_stage')
    summary['retry_parent'] = manifest.get('retry_parent')
    if summary['retry_parent']:
        summary['original_sale'] = summary['retry_parent']['original_sale']
    summary['inherited_task_ids'] = sorted(inherited_jobs)
    instance = None
    previous_env = os.environ.get('FIFTEENTH_BUSINESS_BUDGET')
    os.environ['FIFTEENTH_BUSINESS_BUDGET'] = str(budget.path)
    def save():
        summary['budget'] = budget.status();write(output/'summary.json',summary)
    def frozen():
        if source_digest()!=manifest['source_sha256'] or env_file_stamp()!=manifest['environment_stamp']:
            budget.halt('source_or_environment_changed');raise ValueError('Source/environment changed during stage')
        check_previous_stage(parent_guard)
        check_retry_parent(parent_guard)
    async def start(name, worker):
        nonlocal instance
        frozen();instance=Instance(store.path,output,name,worker)
        state=await instance.start();summary['instances'].append(state)
        if state['health']['runtime_version']['source_sha256_at_start'] != manifest['source_sha256']:
            raise ValueError('Isolated process started another source identity')
        save()
    try:
        save();await start('prepare-api-no-worker',False)
        stage,prefix=manifest['stage'],manifest['operation_prefix']
        symbols=['SZ:159278'] if stage=='b_cash' else ['SH:518880','SZ:002354','SZ:159599']
        resolution=await instance.request('POST','/api/securities/resolve',json={'symbols':symbols,'context':'stock_etf'})
        if not resolution['ok']:
            raise ValueError('Real security directory did not resolve the stage; no fabricated fallback')
        if manifest.get('retry_parent'):
            retry_parent = manifest['retry_parent']
            account=await instance.request('GET','/api/accounts/'+retry_parent['account_id'])
            ledger=await instance.request('GET',f"/api/accounts/{account['id']}/ledger")
            if (account['version']!=retry_parent['input_version'] or account.get('latest_job_id')!=retry_parent['job_id']
                    or digest(ledger_facts(ledger))!=retry_parent['ledger_facts_sha256']):
                raise ValueError('Copied failed post-sale accounting state changed before retry')
            receipt=await instance.request('POST','/api/jobs/'+retry_parent['job_id']+'/retry',
                json={'operation_id':prefix+'-retry-sale-research'})
            latest=receipt.get('job_id') or receipt.get('id')
            if not latest or latest==retry_parent['job_id'] or receipt.get('merged') is not False:
                raise ValueError('Retry API must create a new child job for the failed sale research')
            child=store.job(latest)
            if (child.get('parent_job_id')!=retry_parent['job_id'] or child['account_id']!=account['id']
                    or child['input_version']!=retry_parent['input_version']):
                raise ValueError('Retry API child task identity differs from the failed sale')
            summary['retry_receipt']=receipt
        elif stage=='a_initial':
            account=await instance.request('POST','/api/accounts',headers={'X-Operation-ID':prefix+'-account-a'},json={
                'name':'第十五轮正常业务A（隔离模拟记账）','cash':'100000','holdings':[],
                'preferences':{'risk':'balanced','horizon':'研究三证券趋势、现金与集中度、含费成本、反向因素及改变判断的条件。成交价格只是模拟记账输入；当前行情另看实际时点。按需要派发研究，不强制角色数量。'}})
            summary['account_a_id']=account['id']
            for i,values in enumerate(base.INPUTS):
                summary['trades'].append(await trade(instance,account['id'],{**values,'side':'buy'},prefix+f'-buy-{i}'))
            ledger=await instance.request('GET',f"/api/accounts/{account['id']}/ledger")
            if ledger['cash']!='86484.90' or {x['symbol']:x['quantity'] for x in ledger['positions']}!={x['symbol']:x['quantity'] for x in base.INPUTS}:
                raise ValueError('A three-buy ledger does not reconcile')
            from decimal import Decimal
            if sum(Decimal(x['total_cost']) for x in ledger['positions'])!=Decimal('13515.10') or Decimal(ledger['summary']['total_fees'])!=Decimal('15.10'):
                raise ValueError('A fees and precise position costs do not reconcile')
            latest=summary['trades'][-1]['receipt']['research']['job_id']
        elif stage=='a_after_sell':
            account=await instance.request('GET','/api/accounts/'+manifest['account_a_id'])
            before=await instance.request('GET',f"/api/accounts/{account['id']}/ledger")
            summary['before_change_ledger']=before
            if (account['version']!=manifest['previous_stage']['input_version'] or before['cash']!='86484.90'
                    or {p['symbol']:p['quantity'] for p in before['positions']}!={p['symbol']:p['quantity'] for p in base.INPUTS}):
                raise ValueError('A sale requires the exact approved three-buy accounting state')
            summary['trades'].append(await trade(instance,account['id'],SALE,prefix+'-sell-gold'))
            receipt=summary['trades'][0]['receipt'];latest=receipt['research']['job_id']
            after=await instance.request('GET',f"/api/accounts/{account['id']}/ledger")
            if (after['input_version']!=before['input_version']+1 or after['cash']!='87479.90'
                    or {p['symbol']:p['quantity'] for p in after['positions']}!={'SH:518880':'0','SZ:002354':'1000','SZ:159599':'1000'}):
                raise ValueError('A sale version/cash/quantity does not reconcile')
            from decimal import Decimal
            if (sum(Decimal(p['total_cost']) for p in after['positions'])!=Decimal('12510.10')
                    or Decimal(after['summary']['total_fees'])!=Decimal('20.10')):
                raise ValueError('A sale fees and remaining cost do not reconcile')
        elif stage=='b_cash':
            summary['universe_change']=await instance.request('PUT','/api/universe',json={'watchlist':[], 'candidates':['SZ:159278']})
            account=await instance.request('POST','/api/accounts',headers={'X-Operation-ID':prefix+'-account-b'},json={
                'name':'第十五轮正常业务B（独立现金与候选范围）','cash':'100000','holdings':[],
                'preferences':{'risk':'balanced','horizon':'独立评价现金账户与候选159278，说明买入或继续持有现金的依据、反向因素及重新评估条件。共享原始材料须沿实际可见来源使用，不把A账户报告当作本账户结论。'}})
            latest=account['save_receipt']['job_id'];summary['account_b_id']=account['id']
            if account['id']==manifest['account_a_id'] or account['holdings'] or account['cash']!='100000':
                raise ValueError('B must be an independent cash account')
            with store.connect() as db:
                if db.execute('SELECT 1 FROM analyses WHERE account_id=?',(account['id'],)).fetchone():
                    raise ValueError('B cannot inherit a report from A')
            if store.job(latest)['target']['symbols']!=['SZ:159278']:
                raise ValueError('B candidate is not frozen into the actual job scope')
        summary.update(account_id=account['id'],job_id=latest)
        summary['prepared_ledger']=await instance.request('GET',f"/api/accounts/{account['id']}/ledger")
        with store.connect() as db:
            if db.execute('SELECT attempts FROM jobs WHERE id=?',(latest,)).fetchone()[0]:
                raise ValueError('Unexpected model execution during worker-free input preparation')
        await instance.stop();instance=None
        cfg=store.settings();cfg['debounce_seconds']=4;store.set_meta('settings',cfg)
        with store.connect() as db:
            db.execute("UPDATE jobs SET due_at=? WHERE id=? AND state='queued'",(now(),latest))
        budget.admit(latest);await start('research-worker',True)
        async with asyncio.timeout(float(cfg['analysis_timeout_seconds'])+120):
            while True:
                frozen();job=await instance.request('GET','/api/jobs/'+latest)
                if job['state'] not in base.ACTIVE:break
                await asyncio.sleep(.25)
        summary['job']=job
        record_retry_mode(summary,store)
        result=job.get('result') or {}
        summary['published']=job['state']=='completed' and bool(job.get('report_id')) and bool(result.get('report'))
        summary['contract_accepted']=bool(result.get('delivery',{}).get('contract_verified'))
        if not summary['published'] or not summary['contract_accepted']:
            raise ValueError('Business research did not deliver; preserve the first failure and stop')
        write(output/'model-result.json',result)
        summary['report_sha256']=hashlib.sha256(result['report'].encode()).hexdigest()
        budget.halt('first_valid_publication_stop')
        baseline=persisted_view(store,latest);summary['after_publish_baseline']=baseline
        if len(baseline['reports'])!=1 or baseline['reports'][0]['input_version']!=baseline['input_version']:
            raise ValueError('Publication is not unique and version-bound')
        for phase in ('after_publish','after_restart'):
            if phase=='after_restart':
                await instance.stop();instance=None;await start('restart-observer-worker',True)
            for index in range(2):
                await asyncio.sleep(manifest['observation_seconds_each']/2)
                frozen();await instance.request('GET','/api/status')
                view=persisted_view(store,latest)
                if [(r['id'],r['report_sha256'],r['input_version']) for r in view['reports']]!=[(r['id'],r['report_sha256'],r['input_version']) for r in baseline['reports']]:
                    raise ValueError('Published report changed or duplicated after publish/restart')
                if view['forecast_ids']!=baseline['forecast_ids'] or view['recommendation_ids']!=baseline['recommendation_ids']:
                    raise ValueError('Structured delivery duplicated after publish/restart')
                summary['observations'].append({'phase':phase,'index':index+1,'at':now(),'view':view});save()
        summary['final_ledger']=await instance.request('GET',f"/api/accounts/{account['id']}/ledger")
        if ledger_facts(summary['prepared_ledger'])!=ledger_facts(summary['final_ledger']):
            raise ValueError('Model/restart changed simulated ledger')
        current_reports=report_bodies(store)
        if any(current_reports.get(key)!=value for key,value in inherited_reports.items()):
            raise ValueError('A prior report was overwritten instead of preserving its versioned body')
        summary['inherited_reports_preserved']=True
        current_forecasts=forecast_bodies(store)
        if any(current_forecasts.get(key)!=value for key,value in inherited_forecasts.items()):
            raise ValueError('A prior forecast was changed instead of preserving its immutable body')
        summary['inherited_forecasts_preserved']=True
        summary['prior_report_status_note']='New account/universe versions may legitimately make old reports stale; their bodies and identities must remain unchanged.'
        summary['mechanical_passed']=True
    except Exception as exc:
        summary['error']={'type':type(exc).__name__,'message':str(exc)}
    finally:
        if instance:await instance.stop()
        if previous_env is None:os.environ.pop('FIFTEENTH_BUSINESS_BUDGET',None)
        else:os.environ['FIFTEENTH_BUSINESS_BUDGET']=previous_env
        audits=[item for item in base.task_audit(store) if item['task']['id'] not in inherited_jobs]
        record_retry_mode(summary,store)
        if manifest.get('retry_parent'):
            try:
                check_retry_parent(parent_guard)
                summary['retry_parent_unchanged']=True
            except (ValueError,OSError,sqlite3.Error,KeyError):
                summary['retry_parent_unchanged']=False;summary['mechanical_passed']=False
        summary['all_attempts_after_shutdown']=audits
        summary['additional_tasks']=[item['task'] for item in audits if item['task']['id']!=summary.get('job_id') and
            (item['requests'] or item['task']['state'] in base.ACTIVE or item['task'].get('attempts',0))]
        summary['additional_task_interpretation']='New input/material/price events may be legitimate. Their model execution is not authorized by this single-job stage; inspect provenance before calling an event a self-trigger loop.'
        summary['source_unchanged']=source_digest()==manifest['source_sha256']
        summary['environment_unchanged']=env_file_stamp()==manifest['environment_stamp']
        summary['completed_at']=now();summary['execution_stopped']=True
        summary['overall_business_acceptance_passed']=False
        summary['acceptance_boundary']='Mechanical/API/worker/restart evidence only. Each actual report still needs independent accepted=true content review before the next stage. No matrix/continuous-stability claim.'
        if not summary['source_unchanged'] or not summary['environment_unchanged']:summary['mechanical_passed']=False
        save()
    return 0 if summary['mechanical_passed'] else 2


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path);parser.add_argument('--reference-account')
    parser.add_argument('--output',type=Path);parser.add_argument('--stage',choices=STAGES,default='a_initial')
    parser.add_argument('--approved-summary',type=Path);parser.add_argument('--content-review',type=Path)
    parser.add_argument('--previous-stage',type=Path,help='For A sale/B, the exact approved predecessor execution/summary.json; no inherited authorization')
    parser.add_argument('--retry-execution',type=Path,help='Stopped failed a_after_sell execution to retry through the normal API; copies its committed sale without another trade')
    parser.add_argument('--max-http-attempts',type=int)
    parser.add_argument('--network-route',choices=('environment','direct'),default='environment',
        help='ModelClient-only isolated route; direct retains TLS/environment CA and must be repeated at execution')
    parser.add_argument('--manager-reasoning-effort',choices=('low','medium','high','xhigh','max'))
    output_limit = parser.add_mutually_exclusive_group()
    output_limit.add_argument('--max-output-tokens',type=int)
    output_limit.add_argument('--omit-output-limit',action='store_true',help='Omit all model output-limit fields in this isolated stage; preserve source configuration')
    for key in TIMEOUT_OVERRIDE_FIELDS:
        parser.add_argument('--'+key.replace('_','-'),type=float,
            help='Explicit isolated per-job timeout in seconds; run uses the frozen prepared value')
    parser.add_argument('--prepared',type=Path);parser.add_argument('--run-real',action='store_true')
    parser.add_argument('--authorize-stage',choices=STAGES);parser.add_argument('--authorized-http-attempts',type=int)
    parser.add_argument('--authorization-note')
    parser.add_argument('--serve-child',type=Path);parser.add_argument('--listen-fd',type=int);parser.add_argument('--worker',action='store_true')
    args=parser.parse_args()
    if args.serve_child:
        ledger=os.environ.get('FIFTEENTH_BUSINESS_BUDGET')
        if args.listen_fd is None or not ledger or Path(ledger).resolve()!=args.serve_child.resolve().parent/'business-attempts.sqlite3':
            parser.error('Child requires the reserved socket and this stage authorization ledger')
        install_budget(ledger);base.serve_child(args.serve_child,args.listen_fd,args.worker)
    elif args.run_real:
        if not args.prepared:parser.error('--run-real requires --prepared')
        raise SystemExit(asyncio.run(run_prepared(args)))
    else:
        if not all((args.source,args.reference_account,args.output,args.approved_summary,args.content_review)):
            parser.error('Offline prepare needs source, account, new output and an accepted body-bound report review')
        print(json.dumps(prepare(args),ensure_ascii=False,indent=2))


if __name__=='__main__':main()
