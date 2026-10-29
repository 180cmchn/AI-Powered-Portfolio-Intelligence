#!/usr/bin/env python3
"""Controlled SQLite pagination benchmark. No network or user database access."""
from __future__ import annotations
import argparse
from datetime import datetime
import json
from pathlib import Path
import platform
import sqlite3
import statistics
import sys
import tempfile
import time
import tracemalloc

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.calendar import SHANGHAI
from app.store import Store


def measure(call, repeats=3):
    durations, peaks, counts = [], [], []
    for _ in range(repeats):
        tracemalloc.start()
        started = time.perf_counter()
        count = call()
        durations.append(round((time.perf_counter()-started)*1000, 3))
        peaks.append(tracemalloc.get_traced_memory()[1])
        tracemalloc.stop()
        counts.append(count)
    return {'median_ms': statistics.median(durations), 'runs_ms': durations,
            'peak_python_bytes': max(peaks), 'returned_items': counts}


def benchmark(batches=5000):
    with tempfile.TemporaryDirectory(prefix='guanlan-query-benchmark-') as directory:
        store = Store(Path(directory)/'fixture.sqlite3')
        account=store.save_account({'name':'synthetic benchmark', 'holdings':[{'symbol':'SZ:159278','quantity':'1'}]})
        original=store.create_forecast_batch(account['id'],kind='exploratory',at=datetime(2026,10,28,16,tzinfo=SHANGHAI))
        with store.connect() as db:
            batch = list(db.execute('SELECT * FROM forecast_batches WHERE id=?',(original['id'],)).fetchone())
            sample_rows = [list(row) for row in db.execute('SELECT * FROM forecast_samples WHERE batch_id=?',(original['id'],))]
            projection = list(db.execute('SELECT * FROM forecast_rule_projections WHERE batch_id=?',(original['id'],)).fetchone())
            for i in range(1,batches):
                bid='bench-batch-%06d'%i
                body=list(batch);body[0]=bid
                db.execute('INSERT INTO forecast_batches VALUES('+','.join('?'*len(body))+')',body)
                body=list(projection);body[0]=bid
                db.execute('INSERT INTO forecast_rule_projections VALUES('+','.join('?'*len(body))+')',body)
                for j,row in enumerate(sample_rows):
                    body=list(row);body[0]=bid+'-sample-'+str(j);body[1]=bid
                    db.execute('INSERT INTO forecast_samples VALUES('+','.join('?'*len(body))+')',body)
        def old_full_then_slice():
            with store.connect() as db:
                rows=store._forecast_rows(db, {'kind':'exploratory'})
            return len(rows[60:90])
        def sql_page():
            page=store.forecasts_page(kind='exploratory',limit=30,offset=60)
            assert page['total']==batches*3
            return len(page['items'])
        old_full_then_slice();sql_page()
        return {'scope':'Synthetic SQLite, no network/model; legacy algorithm full materialization versus SQL COUNT/LIMIT/OFFSET, same current joins and fixture',
            'batches':batches,'expected_samples':batches*3,'page_size':30,'offset':60,'repeats':3,
            'hardware':{'machine':platform.machine(),'system':platform.platform(),'processor':platform.processor(),
                'python':platform.python_version(),'sqlite':sqlite3.sqlite_version},
            'before_full_materialization':measure(old_full_then_slice), 'after_sql_pagination':measure(sql_page),
            'limitations':'Local controlled microbenchmark; Python allocations exclude SQLite native memory and OS caches. Not a production SLA.'}

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--batches',type=int,default=5000);parser.add_argument('--output',type=Path)
    args=parser.parse_args();result=benchmark(args.batches)
    value=json.dumps(result,ensure_ascii=False,indent=2)
    if args.output: args.output.write_text(value+'\n')
    print(value)
