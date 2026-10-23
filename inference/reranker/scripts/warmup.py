#!/usr/bin/env python3
"""Initialize immutable weight caches before normal customer traffic."""
import argparse
import json
from pathlib import Path
import time
from urllib.request import ProxyHandler, Request, build_opener
p=argparse.ArgumentParser();p.add_argument('--port',type=int,required=True);args=p.parse_args()
root=Path(__file__).resolve().parent.parent
key=(root/'secrets/api_key.txt').read_text().strip()
opener=build_opener(ProxyHandler({}));base=f'http://127.0.0.1:{args.port}'
for attempt in range(360):
    try:
        with opener.open(base+'/health',timeout=1) as r:
            if r.status==200:break
    except OSError:
        time.sleep(.5)
else:
    raise RuntimeError('Reranker did not become healthy')
body={'model':'qwen3-reranker-4b','query':'这份文档记录了哪些归档信息？',
      'documents':['这是一份文档整理记录，包含文件编号、页码及目录信息。'*4],'top_n':1}
req=Request(base+'/v1/rerank',data=json.dumps(body,ensure_ascii=False).encode(),headers={'Content-Type':'application/json','Authorization':'Bearer '+key})
with opener.open(req,timeout=180) as r:data=json.load(r)
assert data['results'][0]['index']==0 and data['usage']['prompt_tokens']>64
print(f"Worker {args.port} warmed; original Qwen3-Reranker-4B Q8_0; full input.")
