#!/usr/bin/env python3
"""One isolated real round, with a durable aggregate HTTP-attempt cap.

Uses the prior verifier's production HTTP/accounting/worker/observation path.
Every ModelClient in this isolated process reserves an attempt, including retries,
children, automatic account jobs and global work. No daily process is patched.
"""
from __future__ import annotations
import argparse
import asyncio
import json
import os
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import verify_twelfth_live as base
from app.domain import now

class AttemptBudget:
    def __init__(self, path, limit=None):
        self.path = Path(path)
        if limit is not None:
            with sqlite3.connect(self.path) as db:
                db.execute('CREATE TABLE budget (maximum INTEGER NOT NULL)')
                db.execute('INSERT INTO budget VALUES (?)', (limit,))
                db.execute('CREATE TABLE attempts (id INTEGER PRIMARY KEY, reserved_at TEXT NOT NULL)')
            self.path.chmod(0o600)

    def reserve(self):
        from app.model import ModelBudgetExceeded
        with sqlite3.connect(self.path, timeout=10) as db:
            db.execute('BEGIN IMMEDIATE')
            used = db.execute('SELECT count(*) FROM attempts').fetchone()[0]
            limit = db.execute('SELECT maximum FROM budget').fetchone()[0]
            if used >= limit:
                raise ModelBudgetExceeded('隔离验收全局 HTTP 尝试预算已耗尽；包含所有任务与重试')
            db.execute('INSERT INTO attempts (reserved_at) VALUES (?)', (now(),))

    def status(self):
        with sqlite3.connect(self.path) as db:
            return {'maximum': db.execute('SELECT maximum FROM budget').fetchone()[0],
                    'reserved_attempts': db.execute('SELECT count(*) FROM attempts').fetchone()[0]}


def install_budget(path):
    from app.model import ModelClient
    original = ModelClient.__init__
    budget = AttemptBudget(path)
    def initialize(self, *args, **kwargs):
        original(self, *args, **kwargs)
        before = self.before_request
        async def guard():
            if before:
                await before()
            budget.reserve()
        self.before_request = guard
    ModelClient.__init__ = initialize


class Instance(base.Instance):
    child_script = Path(__file__).resolve()


async def run(args):
    args.iteration_label = "第十三轮"
    args.operation_prefix = "thirteenth"
    original_configure = base.configure
    def configure(source, target, account):
        store, cfg = original_configure(source, target, account)
        limit = min(40, int(cfg.get('max_model_calls', 40)))
        AttemptBudget(args.output / 'attempt-budget.sqlite3', limit)
        return store, cfg
    base.configure = configure
    base.Instance = Instance
    code = await base.run(args)
    path = args.output / 'summary.json'
    summary = json.loads(path.read_text())
    summary['kind'] = 'thirteenth_single_real_research_with_isolated_simulated_ledger'
    budget_path = args.output / 'attempt-budget.sqlite3'
    summary['aggregate_http_budget'] = AttemptBudget(budget_path).status() if budget_path.exists() else None
    summary['previous_failure_denominator'] = {'iteration': 12, 'tasks': 1, 'http_requests': 39, 'complete_terminals': 37, 'failed_requests': 2, 'reports': 0}
    summary['automatic_manual_recovery'] = 'none; original failed attempt retained, no retry to manufacture success'
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2)+'\n')
    path.chmod(0o600)
    return code

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=ROOT/'data/live.sqlite3')
    parser.add_argument('--reference-account')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--run-real', action='store_true')
    parser.add_argument('--serve-child', type=Path)
    parser.add_argument('--listen-fd', type=int)
    parser.add_argument('--worker', action='store_true')
    args = parser.parse_args()
    if args.serve_child:
        if args.listen_fd is None:
            parser.error('Reserved listener required')
        install_budget(args.serve_child.parent/'attempt-budget.sqlite3')
        base.serve_child(args.serve_child, args.listen_fd, args.worker)
    else:
        if not args.run_real or not args.reference_account or not args.output:
            parser.error('Explicit --run-real --reference-account ID --output NEW required')
        raise SystemExit(asyncio.run(run(args)))
