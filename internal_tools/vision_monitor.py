"""Validate private NVIDIA image routes periodically without customer credentials."""
import base64
import json
import time

from internal_tools.recognition_transport import recognition_request
from internal_tools.vision_routing import VisionRouter,DEFAULTS,ROOT
from internal_tools.image_tools import VISION_SYSTEM_PROMPT,RECOGNITION_UPSTREAM_SLOTS
from internal_tools.vision_quality import QUALITY_PROMPT,accepts,controls
from image_hub.concurrency import SlotLimitReached
from image_hub.server import HubError


def health_result(text, first, complete):
    # Latency is an observed target, not evidence that a correct answer is
    # unavailable. Closing every slow-but-working route causes avoidable 503s.
    quality=accepts(text)
    ok=bool(first) and complete and quality
    reason=('verified' if first[0]<=7000 else 'verified_slow') if ok else 'incomplete' if not complete else 'quality_check_failed'
    return ok,reason


def wait_ready(deadline,clock=time.monotonic):
    # Process order is not readiness; keep this phase inside the service budget.
    import urllib.request
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    while clock()<deadline:
        try:
            with opener.open('http://127.0.0.1:8096/health',timeout=min(1,max(.01,deadline-clock()))) as response:
                value=json.load(response)
                if response.status==200 and isinstance(value,dict) and value.get('status')=='ok':return
        except (OSError,ValueError):pass
        time.sleep(min(.1,max(0,deadline-clock())))
    raise TimeoutError('NVIDIA pool did not become ready within the probe budget')


def probe_routes(router,key,image,deadline=None,clock=time.monotonic):
    deadline=clock()+32 if deadline is None else deadline
    states={row['id']:row for row in router.snapshot()}
    def one(model):
        remaining=deadline-clock()
        if remaining<8:
            return {'model':model,'ready':None,'reason':'probe_budget_exhausted','first_text_ms':None,'total_ms':0}
        state=next((row for row in router.snapshot() if row['id']==model),{})
        # Healthy routes need no extra probe during customer work. An
        # unavailable primary must still be able to recover while customers
        # use the configured backup; otherwise continuous load can pin it off.
        recover_primary=model==DEFAULTS[0]['id'] and not state.get('ready')
        if router.active_count() and not recover_primary:
            return {'model':model,'ready':None,'reason':'customer_requests_active','first_text_ms':None,'total_ms':0}
        start=clock();first=[];ok=False;reason='upstream_unavailable'
        body={'model':model,'max_tokens':768,**controls(model,768),'temperature':.6,'top_p':.95,'metadata':{'aerisun_vision_helper':True},'messages':[{'role':'system','content':VISION_SYSTEM_PROMPT},{'role':'user','content':[{'type':'text','text':QUALITY_PROMPT},{'type':'image_url','image_url':{'url':image}}]}]}
        try:
            # Probes share the same provider capacity as accepted customer work.
            # Recovery may wait briefly for a slot, never create a 17th request.
            with RECOGNITION_UPSTREAM_SLOTS.acquire(timeout=min(1,max(0,remaining-8)) if recover_primary else 0):
                remaining=deadline-clock()
                if remaining<8:
                    return {'model':model,'ready':None,'reason':'probe_budget_exhausted','first_text_ms':None,'total_ms':round((clock()-start)*1000)}
                _,value=recognition_request('http://127.0.0.1:8096/v1/chat/completions',key,body,timeout=min(30,remaining),on_text=lambda _:first.append((clock()-start)*1000) if not first else None)
            text=value['choices'][0]['message']['content']
            complete=value['choices'][0].get('finish_reason')=='stop' and bool(value.get('usage'))
            ok,reason=health_result(text,first,complete)
        except SlotLimitReached:
            return {'model':model,'ready':None,'reason':'customer_capacity_in_use','first_text_ms':None,'total_ms':round((clock()-start)*1000)}
        except HubError as error:
            if error.status in (429,503):
                return {'model':model,'ready':None,'reason':'provider_busy','first_text_ms':None,'total_ms':round((clock()-start)*1000)}
        except Exception:pass
        router.probe(model,ok,first[0] if first else None,
                     transient=not ok and reason in ('upstream_unavailable','incomplete'))
        return {'model':model,'ready':ok,'reason':reason,'latency_target_met':bool(first) and first[0]<=7000,'first_text_ms':round(first[0]) if first else None,'total_ms':round((clock()-start)*1000)}
    # Oldest observations first: one slow route cannot indefinitely starve the
    # backup's next check. This only schedules health work, not customer routes.
    probes=sorted(enumerate(DEFAULTS),key=lambda item:(states.get(item[1]['id'],{}).get('updated',0),item[0]))
    return [one(model['id']) for _,model in probes]


def main():
    start=time.monotonic();deadline=start+32
    wait_ready(min(deadline,start+5))
    router=VisionRouter();key=(ROOT/'secrets/nvidia/internal_key.txt').read_text().strip()
    image='data:image/png;base64,'+base64.b64encode((ROOT/'nvidia_pool/fixtures/vision-quality.png').read_bytes()).decode()
    rows=probe_routes(router,key,image,deadline)
    print(json.dumps({'event':'nvidia_vision_health','checks':rows},ensure_ascii=False),flush=True)

if __name__=='__main__':main()
