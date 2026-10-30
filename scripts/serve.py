"""One local process owns a reserved socket, database and durable worker."""
import os
from pathlib import Path
import socket
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app.config import db_path  # one dotenv load; preserves shell-vs-file origin
import uvicorn


def main():
    host = os.environ.get('APP_HOST', '127.0.0.1')
    if host not in ('127.0.0.1', 'localhost', '::1'):
        raise SystemExit('首版本地应用只允许绑定回环地址')
    port = int(os.getenv('APP_PORT', '8765'))
    family = socket.AF_INET6 if host == '::1' else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind((host, port))
            listener.listen(128)
        except OSError:
            raise SystemExit(f'端口 {port} 不可绑定；尚未打开或迁移数据库') from None
        config = uvicorn.Config('app.main:app', host=host, port=port, workers=1, access_log=False)
        uvicorn.Server(config).run(sockets=[listener])


if __name__ == '__main__':
    main()
