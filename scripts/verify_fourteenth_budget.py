"""One durable HTTP-attempt cap shared by final-only and full-run processes."""
from __future__ import annotations
from pathlib import Path
import sqlite3
from app.domain import now
from app.model import ModelBudgetExceeded


class SharedAttemptBudget:
    def __init__(self, path):
        self.path = Path(path).resolve()
        if not self.path.is_file():
            raise ValueError('Shared authorized budget must already exist; never recreate it on restart')

    @classmethod
    def initialize(cls, path, *, maximum=44, final_maximum=4, full_maximum=40):
        path = Path(path).resolve()
        if maximum < 1 or min(final_maximum, full_maximum) < 0 or final_maximum + full_maximum > maximum:
            raise ValueError('Invalid authorized total/stage caps')
        path.parent.mkdir(parents=True, exist_ok=True)
        # Exclusive create preserves every prior attempt across crashes/restarts.
        path.touch(exist_ok=False, mode=0o600)
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE budget(maximum INTEGER NOT NULL)')
            db.execute('INSERT INTO budget VALUES(?)', (maximum,))
            db.execute('CREATE TABLE stages(name TEXT PRIMARY KEY,maximum INTEGER NOT NULL)')
            db.executemany('INSERT INTO stages VALUES(?,?)', [('final', final_maximum), ('full', full_maximum)])
            db.execute('CREATE TABLE attempts(id INTEGER PRIMARY KEY,stage TEXT NOT NULL,reserved_at TEXT NOT NULL)')
        return cls(path)

    def reserve(self, stage):
        with sqlite3.connect(self.path, timeout=15) as db:
            db.execute('BEGIN IMMEDIATE')
            limit = db.execute('SELECT maximum FROM budget').fetchone()[0]
            cap = db.execute('SELECT maximum FROM stages WHERE name=?', (stage,)).fetchone()
            if cap is None:
                raise ValueError('Unknown authorized stage')
            total = db.execute('SELECT count(*) FROM attempts').fetchone()[0]
            used = db.execute('SELECT count(*) FROM attempts WHERE stage=?', (stage,)).fetchone()[0]
            if total >= limit or used >= cap[0]:
                raise ModelBudgetExceeded('第十四轮授权 HTTP 尝试额度耗尽；包含所有阶段、任务及重试')
            db.execute('INSERT INTO attempts(stage,reserved_at) VALUES(?,?)', (stage, now()))

    def status(self):
        with sqlite3.connect(self.path) as db:
            return {'maximum': db.execute('SELECT maximum FROM budget').fetchone()[0],
                'reserved_attempts': db.execute('SELECT count(*) FROM attempts').fetchone()[0],
                'stages': {name: {'maximum': limit, 'reserved_attempts': db.execute(
                    'SELECT count(*) FROM attempts WHERE stage=?', (name,)).fetchone()[0]}
                    for name, limit in db.execute('SELECT name,maximum FROM stages')}}


def install_budget(path, stage):
    from app.model import ModelClient
    original = ModelClient.__init__
    budget = SharedAttemptBudget(path)
    def initialize(self, *args, **kwargs):
        original(self, *args, **kwargs)
        before = self.before_request
        async def reserve():
            if before:
                await before()
            budget.reserve(stage)
        self.before_request = reserve
    ModelClient.__init__ = initialize
