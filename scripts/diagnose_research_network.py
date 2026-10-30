#!/usr/bin/env python3
"""Read-only public DNS/document/history diagnosis. No model calls or system changes."""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta
import ipaddress
import json
from pathlib import Path
import socket
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.providers import MarketProvider, SHANGHAI, read_url, now_iso


async def run(args):
    config = {'network_timeout_seconds': 6, 'read_timeout_seconds': 18, 'read_max_chars': 3000,
              '_research_failure_memory': {}, 'document_public_dns_fallback': not args.no_public_dns_fallback}
    result = {'started_at': now_iso(), 'scope': 'public network reads only; no model calls, private DB, credentials, proxy or DNS configuration changes',
              'public_dns_fallback': config['document_public_dns_fallback'], 'dns': [], 'documents': [], 'histories': []}
    hosts = list(dict.fromkeys([*args.host, *[urlsplit(u).hostname for u in args.url]]))
    for host in hosts:
        row = {'host': host}
        try:
            entries = await asyncio.wait_for(asyncio.get_running_loop().getaddrinfo(host, 443, type=socket.SOCK_STREAM), 6)
            addresses = list(dict.fromkeys(r[4][0] for r in entries))
            row.update(addresses=addresses, all_public=all(ipaddress.ip_address(a).is_global for a in addresses),
                synthetic_198_18=bool(addresses) and all(ipaddress.ip_address(a) in ipaddress.ip_network('198.18.0.0/15') for a in addresses))
        except Exception as exc:
            row.update(error_type=type(exc).__name__, stage='system_dns')
        result['dns'].append(row)
    for url in args.url:
        body = await read_url(url, config, page=1 if urlsplit(url).path.lower().endswith('.pdf') else None)
        result['documents'].append({k: body.get(k) for k in ('url','final_url','title','access','fetched_at','document_id','page_count',
            'returned_range','returned_chars','error','error_kind','failure_stage','next_action','network_diagnostics') if k in body})
        print(json.dumps({'document': url, 'access': body.get('access'), 'stage': body.get('failure_stage')}, ensure_ascii=False), flush=True)
    market = MarketProvider(config)
    end = datetime.now(SHANGHAI).date(); start = end - timedelta(days=60)
    for symbol in args.symbol:
        body = await market.history(symbol,start=start.isoformat(),end=end.isoformat(),limit=100)
        result['histories'].append({k:body.get(k) for k in ('symbol','status','source','fetched_at','as_of','adjustment','requested_range','fallback_from','primary_failure','error','error_kind','next_action')})
        result['histories'][-1].update(row_count=len(body.get('rows',[])), first_row=(body.get('rows') or [None])[0], last_row=(body.get('rows') or [None])[-1])
        print(json.dumps({'symbol': symbol,'status':body.get('status'),'rows':len(body.get('rows',[]))},ensure_ascii=False),flush=True)
    if args.benchmark:
        body=await market.benchmark_history(args.benchmark,start=start.isoformat(),end=end.isoformat(),limit=100)
        result['benchmark']={k:body.get(k) for k in ('benchmark','status','source','fetched_at','as_of','adjustment','fallback_from','primary_failure','error','error_kind')}
        result['benchmark']['row_count']=len(body.get('rows',[]))
    result['finished_at']=now_iso()
    if args.output:
        args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    else:
        print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host',action='append',default=[])
    parser.add_argument('--url',action='append',default=[])
    parser.add_argument('--symbol',action='append',default=[])
    parser.add_argument('--benchmark',choices=['CSI300','SSE50','CSI500'])
    parser.add_argument('--no-public-dns-fallback',action='store_true')
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    if not (args.host or args.url or args.symbol or args.benchmark):
        args.host=['static.cninfo.com.cn','pdf.dfcfw.com','www.phfund.com.cn','web.ifzq.gtimg.cn']
    asyncio.run(run(args))
