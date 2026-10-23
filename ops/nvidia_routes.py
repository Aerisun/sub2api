"""Synchronize availability on the existing NVIDIA account; never onboard models."""
import json
import re
import httpx
from ops import admin_api as api
from ops.admin_api import ROOT
STATE = ROOT/'deployment/private/config/nvidia-account.json'
MAPPING = {'glm-5.3':'z-ai/glm-5.3',
           'deepseek-v4.1-flash':'deepseek-ai/deepseek-v4.1-flash',
           'Nemotron-3.5':'nvidia/nemotron-3.5-lightning-30b-a3b'}
NEW_PRICES = {'Nemotron-3.5':'qwen3.8-flash'}

def ensure_routes(admin):
    routes = api.request('/api/v1/admin/groups/9/composite-routes',admin)
    for model in NEW_PRICES:
        if not any(r['enabled'] and r['public_model']==model and r['match_type']=='exact' for r in routes):
            api.request('/api/v1/admin/groups/9/composite-routes',admin,
                {'public_model':model,'match_type':'exact','target_platform':'openai',
                 'upstream_model':model,'endpoint':'any','priority':1,'enabled':True,
                 'notes':'Unified public alias; physical NVIDIA model and private key rotation remain internal.'},'POST')


def sync(admin):
    """Exclude unready physical routes before the outer gateway can select them."""
    state = json.loads(STATE.read_text())
    if state.get('published'):
        ensure_routes(admin)
    health = httpx.get('http://127.0.0.1:8096/health',trust_env=False,timeout=5).json()
    ready = {public:physical for public,physical in MAPPING.items()
             if health.get('model_availability',{}).get(physical,{}).get('available',True)}
    # Private vision routes are scoped by the recognition endpoint and managed
    # by the shared vision circuit breaker. Public chat discovery stays unchanged.
    from internal_tools.vision_routing import DEFAULTS
    verified_pool_models=set(health.get('models',[]))
    # The pool health schema may omit its configured model list.
    verified_pool_models.update(json.loads((ROOT/'deployment/private/config/nvidia-pool.json').read_text()).get('models',[]))
    ready.update({model['id']:model['id'] for model in DEFAULTS if model['id'] in verified_pool_models})
    if not ready:
        raise RuntimeError('No healthy NVIDIA model routes')
    account = api.request(f"/api/v1/admin/accounts/{state['account_id']}",admin)
    name = f"NVIDIA 精选模型（{len(health['members'])} Key 内部轮换·代理）"
    metadata = {}
    # Preserve the operator's explicit overseas proxy binding on route syncs.
    # Its loopback DIRECT rule reaches this host's pool; provider requests use
    # the pool's separately qualified overseas egress.
    proxy_id = json.loads((ROOT/'deployment/private/config/nvidia-pool.json').read_text()).get('gateway_proxy_id')
    if proxy_id is not None:
        if type(proxy_id) is not int or proxy_id <= 0:
            raise RuntimeError('Configured NVIDIA gateway proxy must be a positive proxy ID')
        if account.get('proxy_id') != proxy_id:
            metadata['proxy_id'] = proxy_id
    if account['name'] != name:
        metadata['name'] = name
    notes = re.sub(r'\b(?:Thirty|\d+) private keys',f"{len(health['members'])} private keys",account.get('notes') or '')
    if notes != account.get('notes'):
        metadata['notes'] = notes
    if metadata:
        api.request(f"/api/v1/admin/accounts/{state['account_id']}",admin,metadata,'PUT')
    if state.get('vision_enabled'):
        # Retired GLM Flash must not be resurrected by this periodic sync.
        from internal_tools.vision_routing import PRIMARY, OMNI
        desired_extra = {k:v for k,v in account['extra'].items()
                         if not k.startswith('glm_image_bridge_') and k!='glm_flash_native_vision'}
        desired_extra.update(nvidia_image_tool='configured',
            glm_image_bridge_account_id=state['account_id'],glm_image_bridge_model=PRIMARY,
            glm_image_bridge_reasoning_effort='low',
            glm_image_bridge_fallback_account_id=state['account_id'],glm_image_bridge_fallback_model=OMNI)
        if desired_extra != account['extra']:
            api.request(f"/api/v1/admin/accounts/{state['account_id']}",admin,{'extra':desired_extra},'PUT')
    if (account['credentials']['model_mapping'] != ready or
            account['credentials'].get('base_url') != 'http://127.0.0.1:8096' or
            account['credentials'].get('pool_manages_transient_errors') is not True):
        credentials = {**account['credentials'], 'model_mapping':ready,
            'base_url':'http://127.0.0.1:8096',
            'api_key':(ROOT/'secrets/nvidia/internal_key.txt').read_text().strip(),
            'openai_capabilities':['chat_completions'],'pool_mode':True,
            'pool_mode_retry_count':0,'pool_mode_retry_status_codes':[],
            'pool_manages_transient_errors':True}
        api.request(f"/api/v1/admin/accounts/{state['account_id']}",admin,
                    {'credentials':credentials},'PUT')
    print(json.dumps({'ready_models':list(ready),'unavailable_models':sorted(set(MAPPING)-set(ready))}))


if __name__ == '__main__':
    sync(api.login())
