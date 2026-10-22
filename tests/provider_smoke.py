"""Opt-in real network probe. Run: PYTHONPATH=. .venv/bin/python tests/provider_smoke.py

Prints observed successes AND failures; this is not a deterministic test suite or
proof of provider reliability. It does not send accounts, holdings, or secrets.
"""
import asyncio, json
from app import providers

async def main():
    cfg={"network_timeout_seconds": 12, "read_timeout_seconds": 30, "read_max_bytes": 15000000}
    market=providers.MarketProvider(cfg)
    output={"tested_at":providers.now_iso(),"test_kind":"real_network_application_adapters","results":{}}
    jobs={
       "history":lambda:market.history("SH:600519","2026-10-01","2026-10-22",limit=20),
       "industry":lambda:market.sectors("industry",limit=5),
       "concept":lambda:market.sectors("concept",limit=5),
       "members":lambda:market.sector_members("BK0420",limit=5),
       "sector_history":lambda:market.sector_history("BK0420","2026-10-01","2026-10-22",limit=20),
       "benchmark":lambda:market.benchmark_history("CSI300","2026-10-01","2026-10-22",limit=20),
       "fundamentals":lambda:market.fundamentals("SH:600519",periods=8),
    }
    for name,call in jobs.items():
        result=await call()
        summary={k:result.get(k) for k in ("status","error","source","as_of","fetched_at","truncated","total","amount_unit","volume_unit")}
        summary["row_count"]=len(result.get("rows",[]))
        summary["first_identity"]={k:result.get("rows",[{}])[0].get(k) for k in ("sector_id","symbol","time","report_period","published_at") if result.get("rows") and k in result["rows"][0]}
        output["results"][name]=summary
        print(name,json.dumps(summary,ensure_ascii=False),flush=True)
    pdf="https://static.cninfo.com.cn/finalpage/2026-08-15/1225475868.PDF"
    metadata=await providers.pdf_metadata(pdf,cfg)
    summary={k:metadata.get(k) for k in ("access","error","page_count","byte_length","document_id","fetched_at")}
    output["results"]["pdf_metadata"]=summary
    print("pdf_metadata",json.dumps(summary,ensure_ascii=False),flush=True)
    if metadata["access"]=="metadata":
        located=await providers.search_pdf(pdf,"归属于母公司",cfg,start_page=21,end_page=80,max_results=10)
        summary={k:located.get(k) for k in ("access","error","searched_range","search_complete","truncated","cache_hit")}
        summary["match_pages"]=[item["page"] for item in located.get("matches",[])]
        output["results"]["pdf_search"]=summary
        print("pdf_search",json.dumps(summary,ensure_ascii=False),flush=True)
        page=summary["match_pages"][0] if summary["match_pages"] else 29
        read=await providers.read_url(pdf,cfg,page=page,limit=10000)
        summary={k:read.get(k) for k in ("access","error","returned_range","truncated","returned_chars","cache_hit","document_id")}
        summary["contains_search_term"]="归属于母公司" in (read.get("text") or "")
        output["results"]["pdf_page"]=summary
        print("pdf_page",json.dumps(summary,ensure_ascii=False),flush=True)
    print(json.dumps(output, ensure_ascii=False, indent=2))

if __name__ == '__main__':
    asyncio.run(main())
