#!/usr/bin/env python3
"""One isolated full research after an explicitly reviewed final-only comparison.

Shares one durable total/stage HTTP-attempt ledger across all worker processes,
retries, child agents and automatic jobs. No daily process is started or changed.
"""
from __future__ import annotations
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import verify_twelfth_live as base
from scripts.verify_fourteenth_budget import SharedAttemptBudget, install_budget
from scripts.verify_fourteenth_live import configure_isolated
from app.runtime_version import source_digest


def require_approved_final(summary_path, review_path):
    summary = json.loads(summary_path.read_text())
    review = json.loads(review_path.read_text())
    named = next((item for item in summary.get('variants', []) if item['variant'] == 'named_finish_analysis'), {})
    if not (named.get('delivery_contract_satisfied') is True and named.get('tool_choice_status') == 'observed'
            and summary.get('final_source_unchanged') and summary.get('final_environment_unchanged')):
        raise ValueError('Named final-only submission must satisfy the contract on unchanged source first')
    if summary.get('source_sha256') != source_digest():
        raise ValueError('Final-only evidence does not match current final source')
    calls = named.get('tool_calls', [])
    report = calls[0].get('arguments', {}).get('report', '') if len(calls) == 1 else ''
    required = ('account_facts', 'answers_scope', 'basis_and_counter_factors', 'conditions', 'limitations')
    if not (review.get('kind') in ('manual_content_review', 'evidence_content_review') and review.get('reviewer') and review.get('reviewed_at')
            and review.get('report_sha256') == hashlib.sha256(report.encode()).hexdigest()
            and report and all(review.get('checks', {}).get(key) in ('pass', 'not_applicable') for key in required)):
        raise ValueError('Actual final report needs a body-bound manual review of facts, judgments, basis, conditions and gaps')
    return {'summary_sha256': hashlib.sha256(summary_path.read_bytes()).hexdigest(),
            'report_sha256': review['report_sha256'], 'reviewer': review['reviewer'], 'reviewed_at': review['reviewed_at']}


class Instance(base.Instance):
    child_script = Path(__file__).resolve()


async def run(args):
    approved = require_approved_final(args.approved_final_summary, args.content_review)
    budget = SharedAttemptBudget(args.global_budget)
    state = budget.status()
    if state['maximum'] > 44 or state['stages']['final']['maximum'] > 4 or state['stages']['full']['maximum'] > 40:
        raise ValueError('Shared ledger exceeds the explicit 4+40 / total44 authorization')
    if state['stages']['full']['reserved_attempts']:
        raise ValueError('Full-run stage already attempted; preserve original attempt, no automatic second business run')
    args.iteration_label, args.operation_prefix = '第十四轮', 'fourteenth'
    previous_configure, previous_instance = base.configure, base.Instance
    previous_path = os.environ.get('FOURTEENTH_GLOBAL_BUDGET')
    base.configure, base.Instance = configure_isolated, Instance
    os.environ['FOURTEENTH_GLOBAL_BUDGET'] = str(args.global_budget.resolve())
    try:
        code = await base.run(args)
    finally:
        base.configure, base.Instance = previous_configure, previous_instance
        if previous_path is None:
            os.environ.pop('FOURTEENTH_GLOBAL_BUDGET', None)
        else:
            os.environ['FOURTEENTH_GLOBAL_BUDGET'] = previous_path
        if (args.output/'summary.json').exists():
            path = args.output/'summary.json'
            summary = json.loads(path.read_text())
            summary.update(kind='fourteenth_isolated_full_research_after_reviewed_final',
                aggregate_http_budget=budget.status(), approved_final=approved,
                authorization_note=args.authorization_note,
                accounting_input_boundary='Historical amounts reused as simulated arithmetic only; fresh quotes have their own actual source/time')
            path.write_text(json.dumps(summary, ensure_ascii=False, indent=2)+'\n')
            path.chmod(0o600)
    return code


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path)
    parser.add_argument('--reference-account')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--run-real', action='store_true')
    parser.add_argument('--authorization-note')
    parser.add_argument('--global-budget', type=Path)
    parser.add_argument('--approved-final-summary', type=Path)
    parser.add_argument('--content-review', type=Path)
    parser.add_argument('--serve-child', type=Path)
    parser.add_argument('--listen-fd', type=int)
    parser.add_argument('--worker', action='store_true')
    args = parser.parse_args()
    if args.serve_child:
        path = os.environ.get('FOURTEENTH_GLOBAL_BUDGET')
        if args.listen_fd is None or not path:
            parser.error('Reserved listener and existing global authorization ledger required')
        install_budget(path, 'full')
        base.serve_child(args.serve_child, args.listen_fd, args.worker)
    else:
        if not all((args.run_real, args.authorization_note, args.source, args.reference_account,
                    args.output, args.global_budget, args.approved_final_summary, args.content_review)):
            parser.error('Explicit real authorization, source, account, new output, shared budget and approved final report required')
        raise SystemExit(asyncio.run(run(args)))
