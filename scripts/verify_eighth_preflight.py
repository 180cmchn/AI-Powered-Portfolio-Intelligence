"""Read only current configured provider catalog + bounded actual public data probes."""
import asyncio
import json
import os
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx
from app.config import defaults, db_path
from app.providers import MarketProvider, read_url
from app.calendar import market_state
from scripts.verify_live_chain import configuration, public_config

async def main():
    cfg, origin = configuration(db_path('live'))
    market = MarketProvider(cfg)
    async def directory():
        async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
            response = await client.get(cfg['model_base_url'].rstrip('/')+'/models', headers={'Authorization':'Bearer '+os.getenv(cfg['model_key_env'],'')})
            response.raise_for_status()
            models = [item['id'] for item in response.json().get('data', [])]
            return {'count': len(models), 'authorized_model': 'grok-4.7', 'authorized_model_present': 'grok-4.7' in models}
    tasks = {'directory': directory(), 'security': market.security('SZ:159278'),
             'history': market.history('SZ:159278', start='2026-10-01', end='2026-10-29', limit=30),
             'pdf': read_url('https://static.cninfo.com.cn/finalpage/2026-08-15/1225475868.PDF', cfg, page=1, limit=1500)}
    values = await asyncio.gather(*tasks.values(), return_exceptions=True)
    out = {'kind':'real_public_preflight', 'configuration':public_config(cfg,origin), 'market_state':market_state()}
    for name, result in zip(tasks, values):
        out[name] = {'error_type': type(result).__name__} if isinstance(result,BaseException) else result
    path=Path('test-results/iteration-8-public-preflight.json')
    path.write_text(json.dumps(out,ensure_ascii=False,indent=2)); path.chmod(0o600)
    print(json.dumps({k:({a:b for a,b in v.items() if a not in ('rows','text','pages')} if isinstance(v,dict) else v) for k,v in out.items()},ensure_ascii=False))

if __name__ == '__main__':
    asyncio.run(main())
