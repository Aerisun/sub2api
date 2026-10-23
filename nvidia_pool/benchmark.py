"""Bounded real-load check. Reports only labels, status and latency, never secrets."""
from concurrent.futures import ThreadPoolExecutor
import argparse
from datetime import datetime
import json
from pathlib import Path
import statistics
import threading
import time
from uuid import uuid4
import httpx

ROOT = Path(__file__).resolve().parents[1]
MODELS = ['nvidia/nemotron-3.5-lightning-30b-a3b','z-ai/glm-5.3-flash']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model',choices=MODELS)
    parser.add_argument('--thinking-off',action='store_true')
    parser.add_argument('--output',type=Path)
    args = parser.parse_args()
    key = (ROOT/'secrets/nvidia/internal_key.txt').read_text().strip()
    with httpx.Client(trust_env=False,timeout=40,limits=httpx.Limits(max_connections=70)) as client:
        health=client.get('http://127.0.0.1:8096/health').json()
        key_count = len(health['members'])
        assert health['capacity']==64 and health['per_key']==3 and health['available_keys']==key_count
        before_requests = {m['label']:m['requests'] for m in health['members']}
        run_id = uuid4().hex
        gate=threading.Barrier(64)
        def one(i):
            model=args.model or MODELS[i%len(MODELS)]
            body={'model':model,'messages':[{'role':'user','content':'Reply with OK only.'}],
                  'stream':True,'max_tokens':1024,'reasoning_effort':'none' if args.thinking_off else 'low',
                  'conversation_id':f'benchmark-{run_id}-{i}'}
            gate.wait(); start=time.monotonic()
            row={'model':model,'index':i}; answer=''; done=False
            try:
                with client.stream('POST','http://127.0.0.1:8096/v1/chat/completions',
                       headers={'Authorization':'Bearer '+key},json=body) as response:
                    row['http_status']=response.status_code
                    for line in response.iter_lines():
                        if not line.startswith('data:'):continue
                        raw=line[5:].strip()
                        if raw=='[DONE]':done=True;continue
                        event=json.loads(raw)
                        for c in event.get('choices',[]):
                            d=c.get('delta') or {}
                            if d.get('content') or d.get('reasoning_content'):
                                row.setdefault('first_output_ms',round((time.monotonic()-start)*1000))
                            answer+=d.get('content') or ''
                row['ok']=bool(row['http_status']==200 and done and answer.strip().upper().rstrip('.!')=='OK')
            except (httpx.HTTPError,ValueError) as error:
                row.update(ok=False,error=type(error).__name__)
            row['total_ms']=round((time.monotonic()-start)*1000)
            return row
        with ThreadPoolExecutor(max_workers=64) as executor:rows=list(executor.map(one,range(64)))
        after=client.get('http://127.0.0.1:8096/health').json()
    good=sorted(r['first_output_ms'] for r in rows if r['ok'])
    report={'keys':key_count,'concurrency':64,'peak_inflight':after['peak'],
            'ok':sum(r['ok'] for r in rows),'requests':64,
            'first_output_p50_ms':round(statistics.median(good)) if good else None,
            'first_output_p95_ms':good[min(len(good)-1,int(len(good)*.95))] if good else None,
            'checks':rows,'per_key_peak':{m['label']:m['peak'] for m in after['members']},
            'per_key_requests':{m['label']:m['requests']-before_requests[m['label']] for m in after['members']}}
    output = args.output or ROOT/f'var/reports/nvidia-{key_count}key-64-concurrency-{datetime.now():%Y%m%d}.json'
    output.write_text(json.dumps(report,indent=2)+'\n')
    output.chmod(0o600)
    print(json.dumps({k:v for k,v in report.items() if k not in ('checks','per_key_peak')}),flush=True)


if __name__=='__main__':main()
