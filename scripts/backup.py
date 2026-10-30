#!/usr/bin/env python3
"""Consistent SQLite backup without Store, migration or a running webpage."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.config import db_path
from app.calendar import SHANGHAI


def backup(source, destination, mode):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if source == destination or destination.exists():
        raise ValueError('备份目标必须是一个新的独立文件')
    destination.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(source.as_uri() + '?mode=ro', uri=True) as current:
        row = current.execute("SELECT value FROM meta WHERE key='mode'").fetchone()
        if not row or json.loads(row[0]) != mode:
            raise ValueError('源数据库模式不匹配')
        # Reserve a private target before SQLite opens it; never copy live WAL.
        with destination.open('xb'):
            destination.chmod(0o600)
        try:
            with sqlite3.connect(destination) as saved:
                current.backup(saved)
                if saved.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise ValueError('备份完整性检查失败')
        except BaseException:
            destination.unlink(missing_ok=True)
            raise
    return destination


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--mode', choices=['live', 'demo'], required=True)
    p.add_argument('--database', type=Path)
    p.add_argument('--output', type=Path)
    args = p.parse_args()
    source = args.database or db_path(args.mode)
    destination = args.output or ROOT / 'data/backups' / f'{args.mode}-{datetime.now(SHANGHAI):%Y%m%d-%H%M%S-%f}.sqlite3'
    print(f'一致性备份已完成：{backup(source, destination, args.mode)}')


if __name__ == '__main__':
    main()
