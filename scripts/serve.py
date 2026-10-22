"""One local process owns the API and durable background worker."""
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv
load_dotenv(ROOT / ".env")
import uvicorn

if __name__ == "__main__":
    host = os.environ.get("APP_HOST", "127.0.0.1")
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise SystemExit("首版本地应用只允许绑定回环地址")
    uvicorn.run("app.main:app", host=host, port=int(os.getenv("APP_PORT", "8765")), workers=1, access_log=False)
