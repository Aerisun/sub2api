"""Validate private NVIDIA image routes periodically without customer credentials."""
import base64
import concurrent.futures
import json
from pathlib import Path
import time

from internal_tools.recognition_transport import recognition_request
from internal_tools.vision_routing import VisionRouter,DEFAULTS,ROOT
from internal_tools.image_tools import VISION_SYSTEM_PROMPT
from internal_tools.vision_quality import QUALITY_PROMPT,accepts,controls
from image_hub.server import HubError


def health_result(text, first, complete):
    # Latency is an observed target, not evidence that a correct answer is
    # unavailable. Closing every slow-but-working route causes avoidable 503s.
    quality=accepts(text)
    ok=bool(first) and complete and quality
    reason=('verified' if first[0]<=7000 else 'verified_slow') if ok else 'incomplete' if not complete else 'quality_check_failed'
    return ok,reason


def main():
    # systemd After= orders processes, not readiness of their listening socket.
    import urllib.request
    for attempt in range(50):
        try:
            with urllib.request.urlopen('http://127.0.0.1:8096/health',timeout=1) as response:
                if response.status==200:break
        except OSError:
            if attempt==49:raise
            time.sleep(.1)
    router=VisionRouter();key=(ROOT/'secrets/nvidia/internal_key.txt').read_text().strip()
    image='data:image/png;base64,'+base64.b64encode((ROOT/'nvidia_pool/fixtures/vision-quality.png').read_bytes()).decode()
    def one(model):
        state=next((row for row in router.snapshot() if row['id']==model),{})
        # Healthy routes need no extra probe during customer work. An
        # unavailable primary must still be able to recover while customers
        # use the configured backup; otherwise continuous load can pin it off.
        recover_primary=model==DEFAULTS[0]['id'] and not state.get('ready')
        if router.active_count() and not recover_primary:
            return {'model':model,'ready':None,'reason':'customer_requests_active','first_text_ms':None,'total_ms':0}
        start=time.monotonic();first=[];ok=False;reason='upstream_unavailable'
        body={'model':model,'max_tokens':768,**controls(model,768),'temperature':.6,'top_p':.95,'metadata':{'aerisun_vision_helper':True},'messages':[{'role':'system','content':VISION_SYSTEM_PROMPT},{'role':'user','content':[{'type':'text','text':QUALITY_PROMPT},{'type':'image_url','image_url':{'url':image}}]}]}
        try:
            _,value=recognition_request('http://127.0.0.1:8096/v1/chat/completions',key,body,timeout=30,on_text=lambda _:first.append((time.monotonic()-start)*1000) if not first else None)
            text=value['choices'][0]['message']['content']
            complete=value['choices'][0].get('finish_reason')=='stop' and bool(value.get('usage'))
            ok,reason=health_result(text,first,complete)
        except HubError as error:
            if error.status in (429,503):
                return {'model':model,'ready':None,'reason':'provider_busy','first_text_ms':None,'total_ms':round((time.monotonic()-start)*1000)}
        except Exception:pass
        router.probe(model,ok,first[0] if first else None,
                     transient=not ok and reason in ('upstream_unavailable','incomplete'))
        return {'model':model,'ready':ok,'reason':reason,'latency_target_met':bool(first) and first[0]<=7000,'first_text_ms':round(first[0]) if first else None,'total_ms':round((time.monotonic()-start)*1000)}
    # Probe the configured primary and backup before a never-verified route;
    # a long timeout on that route must not delay backup recovery.
    probes=sorted(DEFAULTS,key=lambda model:not model['ready'])
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:rows=list(pool.map(one,[m['id'] for m in probes]))
    print(json.dumps({'event':'nvidia_vision_health','checks':rows},ensure_ascii=False),flush=True)

if __name__=='__main__':main()
