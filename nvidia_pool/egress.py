"""Dedicated NVIDIA egress: qualify subscription updates without touching live traffic."""
import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
import fcntl
import hashlib
import json
import logging
from pathlib import Path
import signal
import secrets
import threading
import time
from urllib.parse import quote,urlencode

import httpx
import yaml

from image_hub.proxy_qualifier import (atomic_write,controller,eligible_nodes,fingerprint,
                                      install_pool,isolated_proxy,read_yaml,stable_pool)
from nvidia_pool.server import prepare_request
from image_hub.egress_control import route_lock

ROOT=Path(__file__).resolve().parents[1]
TARGET='https://integrate.api.nvidia.com/v1/models'
MODELS=('nvidia/nemotron-3.5-lightning-30b-a3b',)
VISION_MODEL='nvidia/nemotron-3.5-super-vl-120b-a12b'
LOG=logging.getLogger('nvidia-egress')
QUALIFICATION_VERSION=3


def delay(client,node,timeout=3000):
    query=urlencode({'url':TARGET,'expected':'200','timeout':timeout})
    try:
        # File-provider members are not exposed by the global /proxies API.
        # Probe them through their provider, including installed spare nodes.
        providers=client.request('/providers/proxies').get('providers',{})
        owner=next((name for name,provider in providers.items()
                    if any(member.get('name')==node for member in provider.get('proxies',[]))),None)
        path=('/providers/proxies/'+quote(owner,safe='')+'/'+quote(node,safe='')+'/healthcheck?'
              if owner is not None else '/proxies/'+quote(node,safe='')+'/delay?')
        value=client.request(path+query).get('delay')
        return value if type(value) is int and value>0 else None
    except Exception:return None


def generation(proxy,key,model,vision=False):
    started=time.monotonic();result={'ok':False,'model':model,'workload':'vision' if vision else 'chat'}
    content='Reply with OK only.'
    if vision:
        image=base64.b64encode((ROOT/'nvidia_pool/fixtures/vision-health.png').read_bytes()).decode()
        content=[{'type':'text','text':'Read the printed number and name the color and shape of each object. Answer briefly in English.'},
                 {'type':'image_url','image_url':{'url':'data:image/png;base64,'+image}}]
    body=prepare_request({'model':model,'stream':True,'max_tokens':1024,'reasoning_effort':'low',
                          'thinking':{'type':'disabled'},'messages':[{'role':'user','content':content}]})
    try:
        with httpx.Client(proxy=proxy,trust_env=False,
                          timeout=httpx.Timeout(30 if vision else 12,connect=8 if vision else 4,write=30),http2=False) as client:
            with client.stream('POST','https://integrate.api.nvidia.com/v1/chat/completions',
                               headers={'Authorization':'Bearer '+key},json=body) as response:
                result['http_status']=response.status_code
                if response.status_code==200:
                    text='';done=False
                    for line in response.iter_lines():
                        if time.monotonic()-started>(40 if vision else 15):break
                        if not line.startswith('data:'):continue
                        raw=line[5:].strip()
                        if raw=='[DONE]':done=True;break
                        event=json.loads(raw)
                        for choice in event.get('choices',[]):
                            content=(choice.get('delta') or {}).get('content')
                            if content:
                                result.setdefault('first_text_ms',round((time.monotonic()-started)*1000))
                                text+=content
                    result['ok']=done and (all(word in text.lower() for word in
                        ('657952','red','triangle','blue','circle','green','square')) if vision
                        else text.strip().upper().rstrip('.!')=='OK')
    except Exception as error:result['error_type']=type(error).__name__
    result['total_ms']=round((time.monotonic()-started)*1000)
    return result


def qualified(rows,limit=3):
    good=[r for r in rows if r.get('ok') and all(c.get('ok') for c in r.get('checks',[]))]
    return sorted(good,key=lambda r:(r['score_ms'],r['network_ms']))[:limit]


def candidate_names(rank,limit):
    # A nearby relay may reach a different NVIDIA POP. Reserve regional tests
    # before filling by network latency, then rank actual model responses.
    chosen=[]
    for prefix in ('NA-UnitedStates-','ASIA-Japan-','ASIA-Singapore-'):
        chosen.extend([name for name in rank if name.startswith(prefix)][:1])
    return (chosen+[name for name in rank if name not in chosen])[:limit]


def publish_route(role,selected,verified_models=()):
    if not role.get('route_state_file'):return
    nodes=read_yaml(role['provider_file']).get('proxies',[])
    node=next((n for n in nodes if n['name']==selected),None)
    if not node:return
    node_fingerprint=fingerprint([node])
    value={'selected':selected,'node_fingerprint':node_fingerprint,
           'generation':hashlib.sha256(node_fingerprint.encode()+secrets.token_bytes(32)).hexdigest()}
    path=Path(role['route_state_file'])
    if path.exists():
        try:
            previous=json.loads(path.read_text())
            if previous.get('node_fingerprint')==node_fingerprint and previous.get('selected')==selected:
                if not verified_models:return
                value['generation']=previous['generation']
        except (ValueError,AttributeError,KeyError):pass
    if verified_models:
        value.update(verified_models=sorted(set(verified_models)),verified_at=time.time())
    atomic_write(path,(json.dumps(value)+'\n').encode())


def evaluate(config,nodes):
    keys=json.loads((ROOT/'secrets/nvidia/api_keys.json').read_text())['keys']
    with isolated_proxy(nodes,config) as (client,proxy):
        names={n['name']:n for n in nodes}
        with ThreadPoolExecutor(max_workers=6) as workers:
            delays=dict(workers.map(lambda name:(name,delay(client,name)),names))
        rank=sorted([name for name,value in delays.items() if value],key=lambda name:delays[name])
        rows=[]
        for index,name in enumerate(candidate_names(rank,config.get('max_candidates',6))):
            client.request('/proxies/Qualification','PUT',{'name':name})
            row={'node':name,'network_ms':delays[name],'config':names[name],'ok':False}
            row['checks']=[generation(proxy,keys[(index*len(MODELS)+offset)%len(keys)],model)
                           for offset,model in enumerate(MODELS)]
            row['checks'].append(generation(proxy,keys[(index*len(MODELS)+len(MODELS))%len(keys)],VISION_MODEL,vision=True))
            row['ok']=all(check['ok'] for check in row['checks'])
            if row['ok']:row['score_ms']=max(c['first_text_ms'] for c in row['checks'])
            rows.append(row)
            LOG.info('candidate_checked node=%s network_ms=%s model_ok=%s',name,row['network_ms'],row['ok'])
        return rows


def reconcile(config,validate=None,source=None):
    nodes=eligible_nodes(read_yaml(config['subscription_file'])) if source is None else source
    if not nodes:return {'state':'retained','reason':'empty_subscription','checked_at':time.time()}
    rows=(validate or evaluate)(config,nodes)
    winners=qualified(rows,config.get('pool_size',3))
    result={'state':'retained','checked_at':time.time(),'source_fingerprint':fingerprint(nodes),
            'qualification_version':QUALIFICATION_VERSION,
            'results':[{k:v for k,v in row.items() if k!='config'} for row in rows]}
    if winners:
        role=config['role'];provider=Path(role['provider_file'])
        if provider.exists() and role.get('mihomo_config'):
            selected=controller(role).request('/proxies/'+quote(role['group'],safe=''))['now']
            winners=stable_pool(winners,selected)
        if not provider.exists():
            atomic_write(provider,yaml.safe_dump({'proxies':[w['config'] for w in winners]},sort_keys=False).encode())
            result['state']='prepared'
        else:
            install_pool(role,[w['config'] for w in winners]);result['state']='applied'
        result.update(primary=winners[0]['node'],pool=[w['node'] for w in winners])
        publish_route(role,winners[0]['node'],[c['model'] for c in winners[0].get('checks',[]) if c.get('ok') and c.get('model')])
    return result


def failover(config,client=None,model_failure=False):
    """Probe NVIDIA itself; failed probes never restart or close model streams."""
    role=config['role'];client=client or controller(role)
    group='/proxies/'+quote(role['group'],safe='')
    selected=client.request(group)
    if not model_failure and delay(client,selected['now']):
        publish_route(role,selected['now'])
        return False
    names=[name for name in selected.get('all',[]) if name!=selected['now']]
    with ThreadPoolExecutor(max_workers=3) as workers:
        candidates=list(workers.map(lambda name:(name,delay(client,name)),names))
    healthy=sorted([(name,value) for name,value in candidates if value],key=lambda v:v[1])
    if not healthy:return False
    keys=None
    for name,latency in healthy:
        if model_failure:
            if keys is None:keys=json.loads((ROOT/'secrets/nvidia/api_keys.json').read_text())['keys']
            nodes=read_yaml(role['provider_file'])['proxies']
            candidate=[node for node in nodes if node['name']==name]
            # Test privately first. A /models GET can succeed while image
            # uploads or VLM inference through the same exit are broken.
            probe_config={**config,'probe_port':config.get('failover_probe_port',config.get('probe_port',17996)+1),
                'probe_controller_port':config.get('failover_probe_controller_port',config.get('probe_controller_port',19196)+1)}
            with isolated_proxy(candidate,probe_config) as (_,proxy):
                if not generation(proxy,keys[0],VISION_MODEL,vision=True)['ok']:continue
        with route_lock(role):
            current=client.request(group)
            if current['now']!=selected['now']:
                return False
            if name not in current.get('all',[]):
                continue
            client.request(group,'PUT',{'name':name})
            applied=client.request(group)
            if applied['now']!=name or delay(client,name) is None:
                if selected['now'] in applied.get('all',[]):
                    client.request(group,'PUT',{'name':selected['now']})
                    publish_route(role,selected['now'])
                continue
            proof=[VISION_MODEL] if model_failure else []
            state_path=Path(config['state_file']) if config.get('state_file') else None
            if state_path and state_path.exists():
                state=json.loads(state_path.read_text())
                if time.time()-state.get('checked_at',0)<=900:
                    proof += [c['model'] for row in state.get('results',[]) if row.get('node')==name and row.get('ok')
                              for c in row.get('checks',[]) if c.get('ok') and c.get('model')]
            if proof:publish_route(role,name,proof)
            else:publish_route(role,name)
        LOG.info('qualified_backup_selected node=%s latency_ms=%s',name,latency)
        return True
    return False


def failure_count():
    with httpx.Client(trust_env=False,timeout=3) as client:
        health=client.get('http://127.0.0.1:8096/health').raise_for_status().json()
    return (health.get('egress_generation'),health.get('egress_failures',
            sum(sum(counts.values()) for counts in health.get('transport_error_counts',{}).values())))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,default=ROOT/'deployment/private/config/nvidia-egress.json')
    parser.add_argument('--once',action='store_true')
    args=parser.parse_args();config=json.loads(args.config.read_text())
    state_path=Path(config['state_file']);lock=state_path.with_suffix('.lock').open('a')
    fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    stop=threading.Event();promotion=threading.Lock()
    signal.signal(signal.SIGTERM,lambda *_:stop.set());signal.signal(signal.SIGINT,lambda *_:stop.set())
    def watchdog():
        previous=None;last_model_check=0
        while not stop.wait(config.get('health_seconds',10)):
            try:
                epoch,count=failure_count()
                failures=0 if previous is None or previous[0]!=epoch or count<previous[1] else count-previous[1]
                previous=(epoch,count)
                model_failure=failures>=3 and time.monotonic()-last_model_check>=60
                if model_failure:
                    last_model_check=time.monotonic()
                    keys=json.loads((ROOT/'secrets/nvidia/api_keys.json').read_text())['keys']
                    model_failure=not generation('http://127.0.0.1:17896',keys[0],VISION_MODEL,vision=True)['ok']
                with promotion:failover(config,model_failure=model_failure)
            except Exception as error:LOG.warning('health_check_retained error_type=%s',type(error).__name__)
    worker=threading.Thread(target=watchdog,daemon=True)
    if not args.once:worker.start()
    state=json.loads(state_path.read_text()) if state_path.exists() else {}
    seen=None;since=time.monotonic();retry_at=0
    while not stop.is_set():
        try:
            nodes=eligible_nodes(read_yaml(config['subscription_file']))
            signature=fingerprint(nodes)
            if signature!=seen:seen=signature;since=time.monotonic()
            ready=time.monotonic()-since>=config.get('debounce_seconds',5)
            due=time.time()>=max(retry_at,state.get('checked_at',0)+config.get('refresh_seconds',900))
            outdated=state.get('qualification_version')!=QUALIFICATION_VERSION
            if args.once or (ready and (signature!=state.get('source_fingerprint') or outdated) and time.time()>=retry_at) or due:
                # Candidate tests run through an isolated proxy, never the live group.
                rows=evaluate(config,nodes) if nodes else []
                with promotion:result=reconcile(config,validate=lambda *_:rows,source=nodes)
                state=result
                retry_at=time.time()+300 if result['state']=='retained' else 0
                atomic_write(state_path,(json.dumps(result,ensure_ascii=False,indent=2)+'\n').encode())
                LOG.info('qualification_complete state=%s primary=%s',result['state'],result.get('primary'))
                if args.once:print(json.dumps(result,ensure_ascii=False));return 0
        except Exception as error:
            LOG.error('qualification_failed_existing_route_retained error_type=%s',type(error).__name__)
            retry_at=time.time()+300
            if args.once:raise
        stop.wait(config.get('poll_seconds',5))
    if worker.is_alive():worker.join(timeout=15)
    return 0


if __name__=='__main__':raise SystemExit(main())
