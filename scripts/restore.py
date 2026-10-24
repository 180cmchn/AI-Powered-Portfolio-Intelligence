"""Restore a compatible SQLite backup while the service is stopped."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import sys
from datetime import datetime

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app.config import db_path


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("backup", type=Path)
    p.add_argument("--mode", choices=["live", "demo"], required=True)
    args = p.parse_args()
    target = db_path(args.mode)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.resolve() == args.backup.resolve():
        raise SystemExit("备份和目标不能是同一文件")
    with open(str(target) + ".lock", "a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise SystemExit("请先停止服务，再恢复数据库")
        with sqlite3.connect(f"{args.backup.resolve().as_uri()}?mode=ro", uri=True) as source:
            if source.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise SystemExit("备份数据库完整性检查失败")
            mode = source.execute("SELECT value FROM meta WHERE key='mode'").fetchone()
            schema = source.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            if not mode or json.loads(mode[0]) != args.mode or not schema or json.loads(schema[0]) not in (1, 2, 3):
                raise SystemExit("备份模式或数据库版本不匹配")
            if target.exists():
                prior = ROOT / "data" / "backups" / f"before-restore-{datetime.now().strftime('%Y%m%d%H%M%S')}.sqlite3"
                prior.parent.mkdir(exist_ok=True)
                with sqlite3.connect(target) as current, sqlite3.connect(prior) as saved:
                    current.backup(saved)
                prior.chmod(0o600)
                print(f"恢复前快照：{prior}")
            with sqlite3.connect(target) as dest:
                source.backup(dest)
        target.chmod(0o600)
        print(f"已恢复 {target}。启动服务后仅恢复最新任务，旧任务不会覆盖新输入。")


if __name__ == "__main__":
    main()
