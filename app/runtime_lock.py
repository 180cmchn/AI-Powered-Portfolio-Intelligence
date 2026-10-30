"""Ownership fence acquired before opening or migrating a worker database."""
import fcntl
from pathlib import Path


class DatabaseLease:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = open(str(self.path) + '.lock', 'a+')
        try:
            fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise RuntimeError('该数据库已有后台服务运行；未打开或迁移数据库，请先安全备份并停止已确认的实例') from None

    def close(self):
        if not self.file.closed:
            fcntl.flock(self.file, fcntl.LOCK_UN)
            self.file.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
