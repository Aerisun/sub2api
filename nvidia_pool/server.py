"""One proxied NVIDIA channel; rolling per-key limits and stable cache affinity."""
from collections import deque
import json
import os
from pathlib import Path
import re
import signal
import sys
import threading
import time

import httpx

from qiniu_pool.server import Pool, Lease, Member, Server, Unavailable, MAX_BODY

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT/'deployment/private/config/nvidia-pool.json'
PRIVATE_VISION_MODELS = frozenset(('nvidia/nemotron-3.5-super-vl-120b-a12b','nvidia/nemotron-3-nano-omni-30b-a3b-reasoning'))


class ModelUnavailable(Unavailable):
    status = 503


class IncompleteGeneration(ValueError):
    def __init__(self, semantic_started):
        super().__init__('Incomplete upstream generation')
        self.semantic_started=semantic_started


def sanitized(value):
    text=re.sub(r'((?:/_vision-assets/|/v1/vision-assets/))[A-Za-z0-9_-]{43}',r'\1[redacted]',str(value))
    return re.sub(r'(?:nvapi-|sk-)[A-Za-z0-9_-]{10,}', '[redacted]', text)[:500]


class NvidiaPool(Pool):
    def __init__(self, keys, config, **kwargs):
        super().__init__(keys, config, **kwargs)
        self.expansion_lock = threading.Lock()
        self.key_rpm = int(config.get('key_rpm', 40))
        if not 1 <= self.key_rpm <= 40:
            raise ValueError('Per-key request limit must not exceed 40 RPM')
        for i, member in enumerate(self.members):
            member.label = f'N{i+1:02d}'
        self.rate_errors = deque()
        self.vision_capacity = int(config.get('vision_capacity', self.capacity))
        if not 1 <= self.vision_capacity <= self.capacity:
            raise ValueError('Vision concurrency must fit within total capacity')
        self.queue_seconds = float(config.get('queue_seconds',.15))
        if not 0 <= self.queue_seconds <= 5:
            raise ValueError('NVIDIA queue budget must be between zero and five seconds')
        self.model_capacities = dict(config.get('model_capacities',{}))
        if any(model not in config.get('models',[]) or type(limit) is not int or not 1 <= limit <= self.capacity
                for model,limit in self.model_capacities.items()):
            raise ValueError('Invalid per-model NVIDIA capacity')
        self.workload_inflight = {'chat':0,'vision':0}
        self.workload_peaks = {'chat':0,'vision':0}
        self.transient_errors = {}
        self.recent_successes = {}
        self.transient_circuits = {}
        self.transport_error_counts = {}
        self.failure_status_counts = {}
        self.egress_generation = None
        self.egress_failures = 0
        self.egress_route_file = config.get('proxy_route_state_file')
        self.egress_marker = None
        self.model_availability = {model:{'available':False,'reason':reason,'checked_at':time.time()}
            for model,reason in config.get('unavailable_models',{}).items()}
        if self.state_path and self.state_path.exists():
            state = json.loads(self.state_path.read_text())
            self.egress_generation = state.get('egress_generation')
            self.model_availability.update(state.get('model_availability',{}))
            offset = time.time()-self.clock()
            for model, saved in state.get('transient_circuits',{}).items():
                self.transient_circuits[model] = {'until':saved['resume_at']-offset,
                    'backoff':saved['backoff'],'probe':False,'avoid':saved.get('avoid',[])}
            for member, digest in zip(self.members, self._member_hashes()):
                saved = state.get('key_health',{}).get(digest,{})
                member.disabled = saved.get('disabled',False)
                member.cooldowns = {model:expiry-offset for model,expiry in saved.get('cooldowns',{}).items()
                                    if expiry > time.time()}

    def persistent_extra(self):
        offset = time.time()-self.clock()
        return {'egress_generation':getattr(self,'egress_generation',None),
                'model_availability':{model:dict(value) for model,value in getattr(self,'model_availability',{}).items()},
                'transient_circuits':{model:{'resume_at':value['until']+offset,'backoff':value['backoff'],'avoid':value.get('avoid',[])}
                    for model,value in getattr(self,'transient_circuits',{}).items()},
                'key_health':{digest:{'disabled':member.disabled,
                    'cooldowns':{model:expiry+offset for model,expiry in member.cooldowns.items()}}
                    for member,digest in zip(self.members,self._member_hashes())}}

    def expand(self, keys):
        """Append credentials without replacing active leases or warm bindings."""
        with self.expansion_lock, self.condition:
            current = [member.secret for member in self.members]
            if (not keys or any(not isinstance(key,str) or not key.strip() for key in keys)
                    or len(set(keys)) != len(keys) or keys[:len(current)] != current):
                raise ValueError('Credential reload must preserve the existing unique prefix')
            added = [Member(secret=key,label=f'N{i+1:02d}',pending=True)
                     for i,key in enumerate(keys) if i >= len(current)]
            self.members.extend(added)
            try:
                # New keys stay ineligible until their commit is acknowledged.
                # Waiting releases the scheduler lock, including during reload.
                self._persist()
            except OSError:
                del self.members[len(current):]
                raise
            for member in added:
                member.pending = False
            self.condition.notify_all()
            return len(added)

    def mark_availability(self, model, available, reason):
        with self.condition:
            self.model_availability[model] = {'available':available,'reason':reason,'checked_at':time.time()}
            self._persist()

    def adopt_egress(self):
        if not self.egress_route_file:
            return
        try:
            path=Path(self.egress_route_file);stat=path.stat();marker=(stat.st_ino,stat.st_mtime_ns,stat.st_size)
            if marker==self.egress_marker:
                return
            route=json.loads(path.read_text())
            if not isinstance(route,dict):return
            generation=route.get('generation')
            if not isinstance(generation,str) or len(generation)!=64:
                return
        except (OSError,ValueError):
            return
        with self.condition:
            self.egress_marker=marker
            if generation==self.egress_generation:
                return
            self.egress_generation=generation;self.egress_failures=0
            # Only a fresh complete generation on this exact selected exit
            # can release a circuit inherited from the previous exit. Quotas,
            # key authentication holds and active leases are never reset.
            stamp=route.get('verified_at')
            proof=route.get('verified_models',[])
            verified=proof if (type(stamp) in (int,float) and 0<=time.time()-stamp<=900
                and isinstance(proof,list) and all(isinstance(model,str) for model in proof)) else []
            for model in verified:
                self.transient_circuits.pop(model,None)
                self.transient_errors.pop(model,None)
                self.recent_successes.pop(model,None)
            self._persist();self.condition.notify_all()

    def acquire(self, model, affinity=None, exclude=(), health_probe=False, workload='chat'):
        if workload not in self.workload_inflight:
            raise ValueError('Unknown NVIDIA workload')
        self.adopt_egress()
        deadline = self.clock()+self.queue_seconds
        with self.condition:
            readiness = self.model_availability.get(model)
            if readiness and not readiness['available'] and not health_probe:
                raise ModelUnavailable('NVIDIA model is temporarily unavailable; healthy routes remain preferred', 300)
            while True:
                now = self.clock()
                circuit = self.transient_circuits.get(model)
                if circuit and (circuit['until'] > now or circuit['probe']):
                    if now<deadline:
                        pause=circuit['until']-now if circuit['until']>now else deadline-now
                        self.condition.wait(min(deadline-now,pause))
                        continue
                    self.local_rejections += 1
                    raise ModelUnavailable('NVIDIA model is recovering from upstream failures',
                        max(1,circuit['until']-now))
                delay = max(self.cooldowns.get('*', 0), self.cooldowns.get(model, 0))-now
                if delay > 0:
                    self.local_rejections += 1
                    raise Unavailable('Provider shared quota is cooling down', delay)
                waits, candidates = [], []
                for i, member in enumerate(self.members):
                    if member.disabled or member.pending or i in exclude:
                        continue
                    wait = max(self._quota_wait(member.label, self.key_rpm, now),
                               self.cooldowns.get('member:'+member.label, 0)-now,
                               member.cooldowns.get(model, 0)-now)
                    if wait > 0:
                        waits.append(wait)
                    elif member.inflight < self.per_key:
                        candidates.append(i)
                workload_ready = workload != 'vision' or self.workload_inflight['vision'] < self.vision_capacity
                model_limit = self.model_capacities.get(model,self.model_capacity)
                if candidates and self.inflight < self.capacity and self.model_inflight.get(model,0) < model_limit and workload_ready:
                    if circuit:
                        fresh = [index for index in candidates if index not in circuit.get('avoid',[])]
                        candidates = fresh or candidates
                    preferred = self.affinity.get(affinity) if affinity else None
                    minimum_load = min(self.members[index].inflight for index in candidates)
                    if (preferred and preferred[1]+self.affinity_ttl > now and preferred[0] in candidates
                            and self.members[preferred[0]].inflight == minimum_load):
                        index = preferred[0]
                        self.affinity_hits += 1
                    else:
                        index = min(candidates, key=lambda i:(self.members[i].inflight,
                            len(self.starts.get(self.members[i].label, ())), self.members[i].requests,
                            (i-self.cursor)%len(self.members)))
                    self.cursor = (index+1)%len(self.members)
                    if affinity:
                        # Busy healthy keys keep their cache home. A failed key
                        # must lose it, otherwise every later turn returns to the
                        # same timeout after its short cooldown expires.
                        healthy_home = (preferred and preferred[1]+self.affinity_ttl > now
                            and not self.members[preferred[0]].disabled and preferred[0] not in exclude
                            and preferred[0] not in (circuit.get('avoid',[]) if circuit else ())
                            and self.members[preferred[0]].cooldowns.get(model,0) <= now
                            and self.cooldowns.get('member:'+self.members[preferred[0]].label,0) <= now)
                        home = preferred[0] if healthy_home else index
                        self.affinity[affinity] = (home, now)
                        self.affinity.move_to_end(affinity)
                        while len(self.affinity)>8192:
                            self.affinity.popitem(last=False)
                    member = self.members[index]
                    self.starts.setdefault(member.label, deque()).append(now)
                    self.inflight += 1
                    self.peak = max(self.peak, self.inflight)
                    self.model_inflight[model] = self.model_inflight.get(model,0)+1
                    self.model_peaks[model] = max(self.model_peaks.get(model,0),self.model_inflight[model])
                    member.inflight += 1
                    member.peak = max(member.peak,member.inflight)
                    member.requests += 1
                    lease = Lease(self,index)
                    lease.workload = workload
                    lease.egress_generation = self.egress_generation
                    lease.recovery_probe = bool(circuit)
                    if circuit:
                        circuit['probe'] = True
                    self.workload_inflight[workload] += 1
                    self.workload_peaks[workload] = max(self.workload_peaks[workload],self.workload_inflight[workload])
                    try:
                        self._persist()
                    except OSError:
                        member.inflight -= 1
                        self.inflight -= 1
                        self.model_inflight[model] -= 1
                        self.workload_inflight[workload] -= 1
                        if circuit:
                            current = self.transient_circuits.get(model)
                            if current:
                                current['probe'] = False
                        self.condition.notify_all()
                        raise Unavailable('Request-limit state could not be saved')
                    return lease
                if now >= deadline or not any(not m.disabled for m in self.members):
                    self.local_rejections += 1
                    raise Unavailable('NVIDIA pool is busy; retry shortly', min(waits) if waits and not candidates else 1)
                # Lease release and key expansion notify this condition. Wake
                # on those events or the next quota expiry, without polling.
                self.condition.wait(min(deadline-now, min(waits) if waits else deadline-now))

    def release(self, lease, model, success):
        with self.condition:
            if lease.closed:
                return
            workload = getattr(lease,'workload','chat')
            self.workload_inflight[workload] -= 1
            super().release(lease,model,success)
            if getattr(lease,'egress_generation',self.egress_generation)!=self.egress_generation:
                return
            circuit = self.transient_circuits.get(model)
            if success:
                recent=self.recent_successes.setdefault(model,deque(maxlen=256))
                now=self.clock()
                while recent and recent[0]<=now-60:recent.popleft()
                recent.append(now)
            if success and not circuit:
                # Isolated failures amid successful work are not an outage.
                self.transient_errors.pop(model,None)
            if circuit and getattr(lease,'recovery_probe',False):
                if success:
                    del self.transient_circuits[model]
                    self.transient_errors.pop(model,None)
                else:
                    circuit['probe'] = False
                    circuit['until'] = max(circuit['until'],self.clock()+circuit['backoff'])
                self._persist()

    def transient_error(self, lease, model, retry_after, shared_failure=False):
        with self.condition:
            if getattr(lease,'egress_generation',self.egress_generation)!=self.egress_generation:
                return
            now = self.clock()
            # Provider-wide 500/503 bursts are not evidence that each API key
            # is broken. Pause the model briefly rather than poisoning all
            # 50 keys for 30s while sweeping them with more retries.
            key_backoff=30 if getattr(lease,'workload','chat')=='vision' and not shared_failure else 3
            lease.member.cooldowns[model] = max(lease.member.cooldowns.get(model,0),now+max(key_backoff,retry_after or 0))
            errors = self.transient_errors.setdefault(model,deque(maxlen=256))
            while errors and errors[0][0] <= now-10:
                errors.popleft()
            errors.append((now,lease.index))
            recent=self.recent_successes.setdefault(model,deque(maxlen=256))
            while recent and recent[0]<=now-60:recent.popleft()
            circuit = self.transient_circuits.get(model)
            # Slow tails of a mostly successful burst are individual failures,
            # not proof that every key/model is down. Keep key-level backoff.
            if circuit or (len({index for _,index in errors}) >= 4 and (shared_failure or len(errors)>=len(recent))):
                backoff = min(30,circuit['backoff']*2) if circuit and getattr(lease,'recovery_probe',False) else circuit['backoff'] if circuit else 3
                self.transient_circuits[model] = {'until':max(circuit['until'] if circuit else 0,now+max(backoff,retry_after or 0)),
                    'backoff':backoff,'probe':bool(circuit and circuit['probe'] and not getattr(lease,'recovery_probe',False)),
                    'avoid':sorted(set(circuit.get('avoid',[]) if circuit else [])|{index for _,index in errors})}
            self._persist()

    def error(self, lease, model, status, retry_after=None):
        self.adopt_egress()
        with self.condition:
            self.failure_status_counts[str(status)] = self.failure_status_counts.get(str(status),0)+1
            if (status==408 or 500<=status<=599) and getattr(lease,'egress_generation',self.egress_generation)==self.egress_generation:
                self.egress_failures += 1
        if status == 408 or 500 <= status <= 599:
            return self.transient_error(lease,model,retry_after,
                shared_failure=status in (500,503) and getattr(lease,'workload','chat')=='vision')
        if status == 403:
            with self.condition:
                lease.member.cooldowns[model] = max(lease.member.cooldowns.get(model,0),self.clock()+max(60,retry_after or 0))
                self._persist()
            return
        if status != 429:
            return super().error(lease,model,status,retry_after)
        with self.condition:
            now = self.clock()
            delay = max(1,retry_after or 60)
            bucket = 'member:'+lease.member.label
            self.cooldowns[bucket] = max(self.cooldowns.get(bucket,0),now+delay)
            while self.rate_errors and self.rate_errors[0][0] <= now-60:
                self.rate_errors.popleft()
            self.rate_errors.append((now,lease.index,model))
            # Keys can share an account quota: repeated denials across keys
            # trigger shared backoff rather than sweeping every credential.
            threshold = min(4, len(self.members))
            if len({index for _,index,target in self.rate_errors if target==model}) >= threshold:
                self.cooldowns[model] = max(self.cooldowns.get(model,0),now+delay)
            if len({index for _,index,_ in self.rate_errors}) >= threshold and len({target for _,_,target in self.rate_errors}) >= 2:
                self.cooldowns['*'] = max(self.cooldowns.get('*',0),now+delay)
            self._persist()

    def snapshot(self):
        with self.condition:
            result = super().snapshot()
            now = self.clock()
            result.pop('remaining_rpm',None)
            result.update(service='nvidia-pool',key_rpm=self.key_rpm,
                vision_capacity=self.vision_capacity,workload_inflight=dict(self.workload_inflight),
                model_capacities=dict(self.model_capacities),queue_seconds=self.queue_seconds,
                model_inflight=dict(self.model_inflight),
                workload_peaks=dict(self.workload_peaks),
                transport_error_counts={model:dict(counts) for model,counts in self.transport_error_counts.items()},
                failure_status_counts=dict(self.failure_status_counts),
                egress_generation=self.egress_generation,egress_failures=self.egress_failures,
                transient_circuits={model:{'cooldown_seconds':max(0,round(value['until']-now,2)),
                    'backoff_seconds':value['backoff'],'recovery_probe_inflight':value['probe']}
                    for model,value in self.transient_circuits.items()},
                configured_max_rpm=self.key_rpm*len(self.members),
                model_availability={model:dict(value) for model,value in self.model_availability.items()},
                per_key_remaining_rpm={m.label:max(0,self.key_rpm-sum(t>now-60 for t in self.starts.get(m.label,())))
                                       for m in self.members})
        return result


def prepare_request(body):
    out = dict(body)
    for field in ('thinking','reasoning','chat_template_kwargs'):
        if out.get(field) is not None and not isinstance(out[field],dict):
            raise ValueError(field+' must be an object')
    name = out['model'].rsplit('/',1)[-1]
    thinking = out.get('thinking') or {}
    effort = out.get('reasoning_effort') or (out.get('reasoning') or {}).get('effort') or (out.get('chat_template_kwargs') or {}).get('reasoning_effort') or 'low'
    effort = str(effort).lower().replace('-','')
    disabled = (thinking.get('type')=='disabled' or out.get('enable_thinking') is False or effort in ('off','none'))
    if effort not in ('auto','low','minimal','medium','high','xhigh','max','none','off'):
        raise ValueError('Unsupported effort')
    for field in ('prompt_cache_key','conversation_id','metadata','prompt_cache_options','reasoning','thinking','enable_thinking'):
        out.pop(field,None)
    if 'max_completion_tokens' in out:
        out.setdefault('max_tokens',out.pop('max_completion_tokens'))
    kwargs = dict(out.get('chat_template_kwargs') or {})
    if name.startswith('nemotron-3'):
        out.pop('reasoning_effort',None)
        kwargs.setdefault('enable_thinking',not disabled)
        if disabled:
            kwargs['enable_thinking'] = False
            out.pop('reasoning_budget',None)
        if name=='nemotron-3.5-lightning-30b-a3b':kwargs.setdefault('force_nonempty_content',True)
        budget = thinking.get('budget_tokens') or {'auto':1024,'minimal':512,'low':1024,'medium':4096,'high':8192,'xhigh':16384,'max':32768,'off':0,'none':0}[effort]
        if not disabled:
            out.setdefault('reasoning_budget',min(int(budget),int(out.get('max_tokens',32768))))
    elif name in ('glm-5.3','glm-5.3-flash'):
        out['reasoning_effort'] = 'low' if disabled or effort in ('auto','minimal','low') else 'high' if effort in ('medium','high') else 'max'
        # TensorRT-LLM reads GLM-5.3 controls from the chat template. A root-only
        # hint can silently use its default max effort, delaying vision output.
        kwargs['reasoning_effort'] = out['reasoning_effort']
        # Preserve full thinking history for agent/tool turns.
        kwargs.setdefault('clear_thinking',False)
    elif name.startswith('deepseek-v4.1-flash'):
        if disabled:
            out.pop('reasoning_effort',None)
            out['thinking'] = {'type':'disabled'}
        else:
            out['reasoning_effort'] = {'auto':1,'minimal':1,'low':1,'medium':25,'high':50,'xhigh':75,'max':100}[effort]
    elif name.startswith('step-5'):
        # This NVIDIA checkpoint always reasons. Disabling its template switch
        # leaks reasoning into content instead of speeding generation up.
        out['reasoning_effort'] = 'low' if disabled or effort in ('auto','minimal') else effort
        kwargs['enable_thinking'] = True
        kwargs.setdefault('preserve_thinking',True)
    else:
        raise ValueError('Model has no verified NVIDIA controls')
    out['chat_template_kwargs'] = kwargs
    from nvidia_pool.images import normalize_images
    return normalize_images(out)


def has_images(body):
    return any(isinstance(part,dict) and part.get('type') in ('image_url','image','input_image')
        for message in body.get('messages',[]) if isinstance(message,dict)
        for part in (message.get('content') if isinstance(message.get('content'),list) else []))


class NvidiaServer(Server):
    pool_class = NvidiaPool
    max_request_body = 96*1024*1024
    retry_transport_errors = True
    finish_error_stream = True
    sanitize = staticmethod(sanitized)
    def stream_terminal(self,body):
        from image_hub.sse_terminal import SSETerminal
        return SSETerminal(complete_on_usage=has_images(body) and body.get('n',1)==1)

    def first_event_ready(self,event,body):
        metadata=body.get('metadata')
        if (body.get('model') in PRIVATE_VISION_MODELS and has_images(body) and
                isinstance(metadata,dict) and metadata.get('aerisun_vision_helper') is True):
            # Verification reasoning is private to the vision helper. Buffer
            # it until real recognition text starts, so a reasoning-only stall
            # remains eligible for the existing safe pre-output recovery.
            return any(bool((choice.get('delta') or {}).get('content')) for choice in event.get('choices',[]))
        return Server.first_event_ready(self,event,body)

    def drain_completed(self,response,iterator,deadline):
        # The transport reserves four spare connections. Finish the caller
        # first, then reuse a healthy upstream socket without keeping its
        # inference slot occupied by a lingering provider tail.
        if not self.completed_drains.acquire(blocking=False):
            response.close();return
        try:
            until=min(deadline,time.monotonic()+.1) if deadline else time.monotonic()+.1
            for _ in iterator:
                if time.monotonic()>=until:break
        except (httpx.HTTPError,OSError,RuntimeError,ValueError):
            pass  # The original finish and final usage were already delivered.
        finally:
            try:response.close()
            except (httpx.HTTPError,OSError,RuntimeError,ValueError):pass
            self.completed_drains.release()
    def prepare_request(self, body):
        if body.get("model") in PRIVATE_VISION_MODELS and not has_images(body):
            raise ValueError("Private vision models require an image")
        out=prepare_request(body)
        if body['model'].rsplit('/',1)[-1]=='step-5-preview' and has_images(body):
            from nvidia_pool.image_fetch import inline_remote_images
            from nvidia_pool.images import normalize_images
            out=normalize_images(inline_remote_images(out))
        if has_images(body) and not body.get('stream') and body.get('n',1)==1 and not body.get('logprobs'):
            # Read real provider deltas even for JSON clients. Waiting for an
            # entire reasoning answer inside a first-output timeout causes
            # healthy long generations to be repeatedly cancelled. Explicit
            # multi-choice/logprob responses keep their native JSON shape.
            out={**out,'stream':True,'stream_options':{**(out.get('stream_options') or {}),'include_usage':True}}
        return out

    def upload_context(self, body):
        assets=getattr(self,'image_assets',None)
        # GLM's large inline upload stalls were reproduced with exact pixels.
        # Step's remote-image fetch instead emits provider SSE 504s; its native
        # inline path succeeds. Only the verified GLM route uses capabilities.
        glm=body.get('model','').rsplit('/',1)[-1]=='glm-5.3-flash'
        return assets.request(body) if assets and glm else super().upload_context(body)

    def public_asset(self, handler, head=False):
        assets=getattr(self,'image_assets',None)
        return assets.serve(handler,head) if assets else False

    def local_request_error(self, error):
        from nvidia_pool.assets import AssetCapacityError
        from nvidia_pool.image_fetch import ImageFetchError
        if isinstance(error,AssetCapacityError):return 429,str(error)
        if isinstance(error,ImageFetchError):return error.status,str(error)
        return None

    def acquire_request(self, model, affinity, exclude, body):
        transport=getattr(getattr(self,'client',None),'_transport',None)
        if callable(getattr(transport,'refresh',None)):
            transport.refresh()
        return self.pool.acquire(model,affinity,exclude,workload='vision' if has_images(body) else 'chat')

    def request_policy(self, body):
        policy=super().request_policy(body)
        if has_images(body):
            policy.update(pre_output_timeout=self.vision_pre_output_timeout,
                          first_event_seconds=self.vision_first_event_seconds)
            metadata=body.get('metadata')
            if body.get('model') in PRIVATE_VISION_MODELS and isinstance(metadata,dict) and metadata.get('aerisun_vision_helper') is True:
                # The tool already owns bounded recovery across gateways.
                # Let one full existing first-event budget expire here,
                # rather than consuming another silent attempt inside the
                # same request and starving the tool's remaining recovery.
                policy['pre_output_timeout']=min(self.vision_pre_output_timeout,self.vision_first_event_seconds)
        return policy

    def request_options(self, pre_deadline, body):
        if not has_images(body):
            return self.upstream_options(pre_deadline)
        remaining=max(.01,pre_deadline-time.monotonic())
        # Image upload, image encoding/prefill and streamed decoding have
        # different latency distributions from a one-word text health check.
        return {'timeout':httpx.Timeout(min(self.vision_read_timeout,remaining),
            connect=min(8,remaining),write=min(30,remaining),pool=min(1,remaining))}

    def may_retry_transport(self, error):
        # The caller authorized key failover. The handler only retries before
        # forwarding model output; partial streamed output is never replayed.
        return not getattr(error,'semantic_started',False) and isinstance(error,(httpx.HTTPError,ValueError,OSError))

    def read_nonstream(self, response, pre_deadline, deadline):
        if 'text/event-stream' not in response.headers.get('Content-Type',''):
            return self.read_response(response,pre_deadline)
        from image_hub.agent_bridge import Stream
        from image_hub.server import HubError
        semantic=False
        from image_hub.sse_terminal import SSETerminal
        terminal=SSETerminal(complete_on_usage=True)
        def events(_event,value):
            nonlocal semantic
            if isinstance(value,dict):
                semantic|=any(any((choice.get('delta') or {}).get(field) for field in
                    ('content','reasoning_content','reasoning','thinking','tool_calls','function_call'))
                    for choice in value.get('choices',[]) if isinstance(choice,dict))
        def lines():
            for line in response.iter_lines():
                now=time.monotonic()
                if (deadline and now>=deadline) or (not semantic and pre_deadline and now>=pre_deadline):
                    raise TimeoutError('Upstream response deadline exceeded')
                if len(line)>2*1024*1024:raise ValueError('Oversized upstream event')
                raw=line.encode('utf-8')+b'\n'
                end=terminal.feed(raw)
                yield raw if end is None else raw[:end]
                if end is not None:return
        try:
            value=Stream('chat',events,internal_names=()).read(lines())
            choices=value.get('choices') or []
            if not choices or choices[0].get('finish_reason') not in ('stop','length','tool_calls','function_call'):
                raise ValueError('Incomplete upstream generation')
            return json.dumps(value,ensure_ascii=False,separators=(',',':')).encode()
        except (HubError,httpx.HTTPError,ValueError,OSError,TypeError,KeyError,UnicodeError):
            raise IncompleteGeneration(semantic) from None

    def retry_status(self, status, has_fallback, message=None):
        if status == 403:
            text = str(message or '').lower()
            return (not re.search(r'policy|safety|moderation|content.filter',text) and
                    any(part in text for part in ('invalid api key','invalid token','authentication','invalid credential',
                                                'permission_denied','permission denied','not authorized')))
        return status in (401,408,429) or 500 <= status <= 599

    def record_response_error(self, lease, model, status, retry_after, message=None):
        if status != 403 or self.retry_status(status,False,message):
            self.pool.error(lease,model,status,retry_after)

    def runtime_policy(self):
        value={'retry_policy':{'max_attempts':self.max_attempts,'pre_output_timeout':self.pre_output_timeout,
                'response_timeout':self.response_timeout,'idle_timeout':self.read_timeout,
                'replay_after_output':False}}
        value['retry_policy']['vision']={'pre_output_timeout':self.vision_pre_output_timeout,
            'first_event_seconds':self.vision_first_event_seconds,'idle_timeout':self.vision_read_timeout,
            'tool_first_attempt_seconds':min(self.vision_pre_output_timeout,self.vision_first_event_seconds)}
        transport=getattr(getattr(self,'client',None),'_transport',None)
        if callable(getattr(transport,'snapshot',None)):value['upstream_transport']=transport.snapshot()
        if getattr(self,'image_assets',None):value['image_transfer']=self.image_assets.snapshot()
        path=getattr(self,'egress_route_file',None)
        if path:
            try:
                route=json.loads(Path(path).read_text())
                value['dedicated_egress']={'proxy':'http://127.0.0.1:17896','selected':route.get('selected'),
                    'automatic':True,'adopts_new_routes_without_interrupting_streams':True}
            except (OSError,ValueError):pass
        return value

    def upstream_options(self, pre_deadline):
        remaining = max(.01, pre_deadline-time.monotonic()) if pre_deadline else self.read_timeout
        return {'timeout':httpx.Timeout(min(self.read_timeout,remaining),connect=min(4,remaining),
                                       write=min(10,remaining),pool=min(1,remaining))}

    def read_response(self, response, deadline):
        chunks, size = [], 0
        for chunk in response.iter_bytes():
            if deadline and time.monotonic() >= deadline:
                raise TimeoutError('Upstream response deadline exceeded')
            size += len(chunk)
            if size > MAX_BODY:
                raise ValueError('Empty or oversized upstream response')
            chunks.append(chunk)
        return b''.join(chunks)

    def record_upstream_exception(self, model, error):
        if not isinstance(self.pool,NvidiaPool):
            return
        with self.pool.condition:
            counts = self.pool.transport_error_counts.setdefault(model,{})
            name = type(error).__name__
            if isinstance(error,ValueError):
                known = {'Upstream exceeded first-output deadline':'first_output_deadline',
                    'Empty upstream stream':'empty_stream','Upstream returned no model events':'no_model_events',
                    'Upstream stream failed before content':'provider_stream_error',
                    'Missing first upstream event':'oversized_stream_preamble',
                    'Upstream did not return an event stream':'unexpected_content_type',
                    'Malformed upstream response':'malformed_response',
                    'Empty or oversized upstream response':'empty_or_oversized_response'}
                name = known.get(str(error),'invalid_json_or_stream')
            counts[name] = counts.get(name,0)+1

    def __init__(self, *args, **kwargs):
        self.completed_drains=threading.BoundedSemaphore(4)
        super().__init__(*args, **kwargs)
        config = args[1] if len(args) > 1 else kwargs['config']
        self.image_assets=None
        if config.get('image_asset_origin'):
            from nvidia_pool.assets import ImageAssets
            self.image_assets=ImageAssets(config['image_asset_origin'])
            # Large requests can fetch several image URLs concurrently while
            # provider POSTs are waiting. GETs must not exhaust relay workers.
            self.workers=threading.BoundedSemaphore(max(16,self.pool.capacity*8))
        self.egress_route_file=config.get('proxy_route_state_file')
        if config.get('proxy_route_state_file'):
            from nvidia_pool.egress_transport import EgressTransport
            old=self.client
            self.client=httpx.Client(trust_env=False,timeout=old.timeout,
                transport=EgressTransport(config['proxy_route_state_file'],trust_env=False,proxy=config['proxy'],
                    limits=httpx.Limits(max_connections=self.pool.capacity+4,
                        max_keepalive_connections=self.pool.capacity+4,keepalive_expiry=300)))
            old.close()
        self.max_attempts = int(config.get('max_attempts',3))
        self.pre_output_timeout = float(config.get('pre_output_timeout',40))
        self.response_timeout = float(config.get('response_timeout',300))
        self.read_timeout = float(config.get('read_timeout',12))
        self.vision_pre_output_timeout=float(config.get('vision_pre_output_timeout',55))
        self.vision_first_event_seconds=float(config.get('vision_first_event_seconds',30))
        self.vision_read_timeout=float(config.get('vision_read_timeout',30))
        if (not 1 <= self.max_attempts <= 5 or not 1 <= self.pre_output_timeout <= 120
                or not self.pre_output_timeout <= self.response_timeout <= 1800
                or not .01 <= self.read_timeout <= self.pre_output_timeout
                or not 1 <= self.vision_first_event_seconds <= self.vision_pre_output_timeout <= min(120,self.response_timeout)
                or not .01 <= self.vision_read_timeout <= self.vision_pre_output_timeout):
            super().server_close()
            raise ValueError('Invalid NVIDIA retry/deadline configuration')
        self.probe_stop = threading.Event()
        self.prober = threading.Thread(target=self.probe_unavailable,daemon=True)
        self.prober.start()

    def probe_unavailable(self):
        while not self.probe_stop.wait(300):
            with self.pool.condition:
                models = [model for model,state in self.pool.model_availability.items() if not state['available']]
            for model in models:
                if self.probe_stop.is_set():
                    return
                try:
                    lease = self.pool.acquire(model,health_probe=True)
                except Unavailable:
                    continue
                success, reason = False, 'Provider did not return a complete answer within the latency budget'
                try:
                    body = prepare_request({'model':model,'stream':True,'max_tokens':1024,'reasoning_effort':'low',
                                            'messages':[{'role':'user','content':'Reply with OK only.'}]})
                    start = time.monotonic()
                    with self.client.stream('POST',self.upstream+'/v1/chat/completions',
                            headers={'Authorization':'Bearer '+lease.member.secret,'Content-Type':'application/json'},json=body,
                            timeout=httpx.Timeout(min(12,self.first_event_seconds),connect=4,write=20,pool=1)) as response:
                        text, done = '', False
                        reason = 'HTTP '+str(response.status_code)
                        if response.status_code==200:
                            for line in response.iter_lines():
                                if time.monotonic()-start>min(12,self.first_event_seconds):
                                    break
                                if not line.startswith('data:'):
                                    continue
                                payload = line[5:].strip()
                                if payload=='[DONE]':
                                    done = True
                                    break
                                event = json.loads(payload)
                                for choice in event.get('choices',[]):
                                    text += (choice.get('delta') or {}).get('content') or ''
                            success = done and text.strip().upper().rstrip('.!')=='OK'
                            reason = 'Successful proxied generation' if success else 'No complete answer within the latency budget'
                except Exception as error:
                    reason = type(error).__name__
                finally:
                    self.pool.release(lease,model,success)
                self.pool.mark_availability(model,success,reason)

    def server_close(self):
        self.probe_stop.set()
        self.prober.join(timeout=10)
        super().server_close()


def main():
    config = json.loads(CONFIG.read_text())
    config.setdefault('runtime_state_path', f'/run/user/{os.getuid()}/sub2api-pool-state/nvidia.json')
    if config.get('proxy') not in ('http://127.0.0.1:17890','http://127.0.0.1:17896'):
        raise ValueError('NVIDIA channel requires the configured proxy')
    keys = json.loads((ROOT/'secrets/nvidia/api_keys.json').read_text())['keys']
    internal_key = (ROOT/'secrets/nvidia/internal_key.txt').read_text().strip()
    server = NvidiaServer(('127.0.0.1',int(config.get('port',8096))),config,keys,internal_key,
                          upstream='https://integrate.api.nvidia.com')
    def stop(*_):
        threading.Thread(target=server.shutdown,daemon=True).start()
    signal.signal(signal.SIGTERM,stop)
    signal.signal(signal.SIGINT,stop)
    def reload_keys():
        try:
            keys = json.loads((ROOT/'secrets/nvidia/api_keys.json').read_text())['keys']
            added = server.pool.expand(keys)
            print(json.dumps({'event':'credential_reload','added':added,
                              'keys':len(server.pool.members)}),flush=True)
        except Exception as error:
            print(json.dumps({'event':'credential_reload_failed','error':type(error).__name__}),
                  file=sys.stderr,flush=True)
    signal.signal(signal.SIGHUP,lambda *_:threading.Thread(target=reload_keys,daemon=True).start())
    try:
        server.serve_forever(poll_interval=.2)
    finally:
        server.server_close()


if __name__=='__main__':
    main()
