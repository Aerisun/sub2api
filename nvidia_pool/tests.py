from concurrent.futures import ThreadPoolExecutor
import json
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
import unittest
from unittest.mock import patch

import httpx

from nvidia_pool.server import NvidiaPool, NvidiaServer, prepare_request, sanitized
from qiniu_pool.server import Unavailable
from qiniu_pool.tests import RelayTests


class SchedulingTests(unittest.TestCase):
    def config(self, **changes):
        return dict({'capacity':20,'per_key':2,'model_capacity':20,'key_rpm':40,
                     'global_rpm':0,'model_rpm':0,'queue_seconds':0},**changes)

    def test_verified_new_exit_releases_only_its_proven_model_circuit_and_preserves_quotas(self):
        with TemporaryDirectory() as directory:
            route=Path(directory)/'route.json';route.write_text(json.dumps({'generation':'a'*64}))
            p=NvidiaPool([str(i) for i in range(10)],self.config(proxy_route_state_file=str(route)))
            for _ in range(4):
                lease=p.acquire('model');p.error(lease,'model',502);p.release(lease,'model',False)
            p.transient_circuits['unproven']={'until':p.clock()+30,'backoff':30,'probe':False,'avoid':[]}
            p.cooldowns['*']=p.clock()+60
            starts={k:list(v) for k,v in p.starts.items()};cooldowns=dict(p.cooldowns)
            route.write_text(json.dumps({'generation':'b'*64,'verified_models':['model'],'verified_at':time.time()}))
            p.adopt_egress()
            self.assertNotIn('model',p.transient_circuits)
            self.assertIn('unproven',p.transient_circuits)
            self.assertEqual(p.cooldowns,cooldowns)
            self.assertEqual({k:list(v) for k,v in p.starts.items()},starts)
            with self.assertRaises(Unavailable):p.acquire('model')

    def test_late_failures_from_retired_exit_cannot_poison_new_exit(self):
        with TemporaryDirectory() as directory:
            route=Path(directory)/'route.json';route.write_text(json.dumps({'generation':'a'*64}))
            p=NvidiaPool([str(i) for i in range(10)],self.config(proxy_route_state_file=str(route)))
            old=[p.acquire('model') for _ in range(4)]
            route.write_text(json.dumps({'generation':'b'*64,'verified_models':['model'],'verified_at':time.time()}))
            new=p.acquire('model')
            for lease in old:p.error(lease,'model',502);p.release(lease,'model',False)
            self.assertEqual(p.egress_failures,0)
            self.assertNotIn('model',p.transient_circuits)
            self.assertEqual(p.inflight,1)
            p.error(new,'model',502);p.release(new,'model',False)
            self.assertEqual(p.egress_failures,1)
            self.assertEqual(p.inflight,0)

    def test_stale_or_missing_model_proof_cannot_clear_a_circuit(self):
        with TemporaryDirectory() as directory:
            route=Path(directory)/'route.json';route.write_text(json.dumps({'generation':'a'*64}))
            p=NvidiaPool([str(i) for i in range(10)],self.config(proxy_route_state_file=str(route)))
            p.adopt_egress()
            p.transient_circuits['model']={'until':p.clock()+30,'backoff':30,'probe':False,'avoid':[]}
            for generation,proof in [('b',{}),('c',{'verified_models':['model'],'verified_at':time.time()-1000})]:
                route.write_text(json.dumps({'generation':generation*64,**proof}));p.adopt_egress()
                self.assertIn('model',p.transient_circuits)

    def test_key_rate_limit_from_previous_exit_is_still_authoritative(self):
        with TemporaryDirectory() as directory:
            route=Path(directory)/'route.json';route.write_text(json.dumps({'generation':'a'*64}))
            p=NvidiaPool([str(i) for i in range(10)],self.config(proxy_route_state_file=str(route)))
            old=p.acquire('model')
            route.write_text(json.dumps({'generation':'b'*64,'verified_models':['model'],'verified_at':time.time()}))
            p.adopt_egress();p.error(old,'model',429,60);p.release(old,'model',False)
            self.assertGreater(p.cooldowns['member:'+old.member.label],p.clock()+59)

    def test_exactly_40_requests_per_key_across_models_and_next_window(self):
        now = [100.]
        pool = NvidiaPool([str(i) for i in range(10)], self.config(), clock=lambda:now[0])
        counts = [0]*10
        for i in range(400):
            model = 'a' if i%2 else 'b'
            lease = pool.acquire(model)
            counts[lease.index] += 1
            pool.release(lease,model,True)
        self.assertEqual(counts,[40]*10)
        with self.assertRaises(Unavailable) as error:
            pool.acquire('third')
        self.assertEqual(error.exception.retry_after,60)
        now[0] += 60.01
        pool.release(pool.acquire('a'),'a',True)

    def test_48_vision_requests_leave_16_chat_slots(self):
        pool = NvidiaPool([str(i) for i in range(50)], self.config(capacity=64, model_capacity=64,
                                                                  per_key=3, vision_capacity=48))
        images = [pool.acquire('flash', workload='vision') for _ in range(48)]
        with self.assertRaises(Unavailable):
            pool.acquire('flash', workload='vision')
        chat = [pool.acquire('other') for _ in range(16)]
        self.assertEqual(pool.inflight, 64)
        for lease in images + chat:
            pool.release(lease, 'flash' if lease in images else 'other', True)
        self.assertEqual(pool.inflight, 0)

    def test_64_real_vision_leases_fit_without_lowering_per_key_safety(self):
        pool = NvidiaPool([str(i) for i in range(50)], self.config(
            capacity=64, model_capacity=64, per_key=3))
        images = [pool.acquire('vision', workload='vision') for _ in range(64)]
        self.assertEqual(pool.workload_inflight['vision'], 64)
        self.assertLessEqual(max(member.inflight for member in pool.members), 3)
        with self.assertRaises(Unavailable):
            pool.acquire('vision', workload='vision')
        pool.release(images.pop(), 'vision', False)
        replacement = pool.acquire('vision', workload='vision')
        for lease in images + [replacement]:
            pool.release(lease, 'vision', True)
        self.assertEqual(pool.inflight, 0)
        self.assertEqual(pool.workload_inflight['vision'], 0)

    def test_parallel_turns_spill_to_idle_keys_and_preserve_cache_home(self):
        pool = NvidiaPool(['one','two','three'], self.config(per_key=3))
        first = pool.acquire('model', 'same-conversation')
        second = pool.acquire('model', 'same-conversation')
        self.assertNotEqual(first.index, second.index)
        pool.release(first, 'model', True)
        pool.release(second, 'model', True)
        again = pool.acquire('model', 'same-conversation')
        self.assertEqual(again.index, first.index)
        pool.release(again, 'model', True)

    def test_one_rate_limited_key_does_not_block_other_keys(self):
        pool = NvidiaPool(['one','two','three','four'], self.config())
        bad = pool.acquire('model')
        pool.error(bad,'model',429,30)
        pool.release(bad,'model',False)
        good = pool.acquire('model')
        self.assertNotEqual(good.index,bad.index)
        pool.release(good,'model',True)

    def test_cache_affinity_spills_only_for_exhausted_or_busy_key(self):
        p = NvidiaPool(['one','two'],self.config(key_rpm=2))
        for _ in range(2):
            lease = p.acquire('model','same')
            self.assertEqual(lease.index,0)
            p.release(lease,'model',True)
        lease = p.acquire('model','same')
        self.assertEqual(lease.index,1)
        p.release(lease,'model',True)

    def test_restart_retains_key_window_and_affinity(self):
        with TemporaryDirectory() as directory:
            config = self.config(key_rpm=1,state_path=str(Path(directory)/'state.json'))
            p = NvidiaPool(['one','two'],config)
            body = {'model':'model','conversation_id':'private-session'}
            fp = p.fingerprint(body,{})
            first = p.acquire('model',fp)
            p.release(first,'model',True)
            second = NvidiaPool(['one','two'],config)
            self.assertEqual(second.fingerprint(body,{}),fp)
            lease = second.acquire('other',fp)
            self.assertNotEqual(lease.index,first.index)
            second.release(lease,'other',True)
            with self.assertRaises(Unavailable):
                second.acquire('model')
            self.assertNotIn('private-session',Path(config['state_path']).read_text())

    def test_temporary_spill_does_not_replace_the_warm_home_key(self):
        p=NvidiaPool(['one','two'],self.config(per_key=1))
        home=p.acquire('model','conversation')
        overflow=p.acquire('model','conversation')
        self.assertNotEqual(home.index,overflow.index)
        p.release(home,'model',True);p.release(overflow,'model',True)
        again=p.acquire('model','conversation')
        self.assertEqual(home.index,again.index)
        p.release(again,'model',True)

    def test_shared_account_denials_back_off_without_sweeping_all_keys(self):
        p = NvidiaPool(['one','two','three','four','five'],self.config())
        first = p.acquire('model')
        p.error(first,'model',429,30)
        p.release(first,'model',False)
        second = p.acquire('model')
        self.assertNotEqual(second.index,first.index)
        p.error(second,'model',429,30)
        p.release(second,'model',False)
        for _ in range(2):
            extra = p.acquire('model')
            p.error(extra,'model',429,30)
            p.release(extra,'model',False)
        with self.assertRaises(Unavailable):
            p.acquire('model')
        third = p.acquire('different-model')
        p.error(third,'different-model',429,30)
        p.release(third,'different-model',False)
        with self.assertRaises(Unavailable):
            p.acquire('different-model')

    def test_adding_credentials_preserves_old_conversation_cache_binding(self):
        with TemporaryDirectory() as directory:
            config = self.config(state_path=str(Path(directory)/'state.json'))
            before = NvidiaPool(['old-one','old-two'],config)
            body = {'model':'model','conversation_id':'cache-session'}
            fp = before.fingerprint(body,{})
            first = before.acquire('model',fp)
            before.release(first,'model',True)
            expanded = NvidiaPool(['old-one','old-two','new-three'],config)
            self.assertEqual(fp,expanded.fingerprint(body,{}))
            again = expanded.acquire('model',fp)
            self.assertEqual(again.index,first.index)
            expanded.release(again,'model',True)

    def test_parallel_requests_do_not_exceed_twenty_or_two_per_key(self):
        p = NvidiaPool([str(i) for i in range(10)],self.config())
        barrier = threading.Barrier(41)
        def run(_):
            barrier.wait()
            try:
                lease = p.acquire('model')
            except Unavailable:
                return False
            time.sleep(.1)
            p.release(lease,'model',True)
            return True
        with ThreadPoolExecutor(max_workers=40) as ex:
            futures = [ex.submit(run,i) for i in range(40)]
            barrier.wait()
            admitted = sum(f.result() for f in futures)
        self.assertEqual(admitted,20)
        self.assertTrue(all(m.peak <= 2 for m in p.members))
        self.assertEqual(p.inflight,0)

    def test_live_expansion_preserves_active_lease_and_warm_home(self):
        p = NvidiaPool(['one','two'],self.config(per_key=1))
        active = p.acquire('model','warm-session')
        member = active.member
        p.error(active,'model',429,30)
        self.assertEqual(p.expand(['one','two','three']),1)
        self.assertIs(active.member,member)
        self.assertEqual(p.inflight,1)
        self.assertEqual(p.affinity['warm-session'][0],active.index)
        spill = p.acquire('model','warm-session')
        self.assertNotEqual(spill.index,active.index)
        p.release(spill,'model',True)
        p.release(active,'model',False)
        self.assertEqual(p.inflight,0)
        for keys in (['two','one','three'],['one','two','three','three']):
            with self.assertRaises(ValueError):
                p.expand(keys)
        self.assertEqual(len(p.members),3)

    def test_expansion_rolls_back_when_state_cannot_be_saved(self):
        p = NvidiaPool(['one','two'],self.config())
        with patch.object(p,'_persist',side_effect=OSError):
            with self.assertRaises(OSError):
                p.expand(['one','two','three'])
        self.assertEqual(len(p.members),2)

    def test_restart_preserves_invalid_keys_and_transient_cooldowns(self):
        with TemporaryDirectory() as directory:
            config = self.config(state_path=str(Path(directory)/'state.json'))
            before = NvidiaPool(['one','two'],config)
            expired = before.acquire('model')
            before.error(expired,'model',401)
            before.release(expired,'model',False)
            transient = before.acquire('model')
            before.error(transient,'model',503,30)
            before.release(transient,'model',False)
            after = NvidiaPool(['one','two','three'],config)
            self.assertTrue(after.members[expired.index].disabled)
            self.assertGreater(after.members[transient.index].cooldowns['model'],after.clock())
            lease = after.acquire('model')
            self.assertEqual(lease.index,2)
            after.release(lease,'model',True)

    def test_fifty_keys_balance_new_sessions_and_enforce_global_capacity(self):
        p = NvidiaPool([str(i) for i in range(50)],self.config(capacity=64,per_key=3,model_capacity=64))
        for i in range(500):
            lease = p.acquire('model',f'session-{i}')
            p.release(lease,'model',True)
        self.assertEqual([m.requests for m in p.members],[10]*50)
        leases = [p.acquire('model') for _ in range(64)]
        self.assertEqual(len({lease.index for lease in leases}),50)
        with self.assertRaises(Unavailable):
            p.acquire('model')
        self.assertTrue(all(m.inflight <= 3 for m in p.members))
        for lease in leases:
            p.release(lease,'model',True)
        self.assertEqual(p.inflight,0)

    def test_vision_limit_leaves_capacity_for_chat_and_release_is_idempotent(self):
        p=NvidiaPool([str(i) for i in range(50)],self.config(capacity=64,per_key=3,model_capacity=64,vision_capacity=32))
        images=[p.acquire('same-model',workload='vision') for _ in range(32)]
        with self.assertRaises(Unavailable):p.acquire('same-model',workload='vision')
        chat=[p.acquire('same-model',workload='chat') for _ in range(32)]
        self.assertEqual(p.inflight,64)
        self.assertEqual(p.workload_inflight,{'chat':32,'vision':32})
        for lease in images+chat:
            p.release(lease,'same-model',True)
            p.release(lease,'same-model',True)
        self.assertEqual(p.workload_inflight,{'chat':0,'vision':0})
        self.assertEqual(p.inflight,0)

    def test_per_model_capacity_queues_bursts_and_does_not_reduce_other_models(self):
        p=NvidiaPool([str(i) for i in range(10)],self.config(capacity=4,model_capacity=4,
            models=['flash','other'],model_capacities={'flash':1},queue_seconds=.3))
        first=p.acquire('flash')
        with ThreadPoolExecutor(max_workers=1) as executor:
            waiting=executor.submit(p.acquire,'flash')
            time.sleep(.04)
            self.assertFalse(waiting.done())
            other=p.acquire('other')
            p.release(other,'other',True)
            p.release(first,'flash',True)
            second=waiting.result(timeout=1)
            p.release(second,'flash',True)
        self.assertEqual(p.model_peaks['flash'],1)
        self.assertEqual(p.inflight,0)

    def test_transient_outage_cools_model_then_allows_exactly_one_recovery_probe(self):
        now=[100.]
        p=NvidiaPool([str(i) for i in range(10)],self.config(),clock=lambda:now[0])
        for _ in range(4):
            lease=p.acquire('model');p.error(lease,'model',503);p.release(lease,'model',False)
        with self.assertRaises(Unavailable) as error:p.acquire('model')
        self.assertEqual(error.exception.status,503)
        healthy=p.acquire('healthy');p.release(healthy,'healthy',True)
        now[0]+=3.01
        probe=p.acquire('model')
        with self.assertRaises(Unavailable):p.acquire('model')
        p.error(probe,'model',502);p.release(probe,'model',False)
        self.assertEqual(p.transient_circuits['model']['backoff'],6)
        now[0]+=6.01
        recovered=p.acquire('model');p.release(recovered,'model',True)
        self.assertNotIn('model',p.transient_circuits)
        leases=[p.acquire('model') for _ in range(3)]
        for lease in leases:p.release(lease,'model',True)

    def test_old_failure_cannot_admit_a_second_probe_or_shorten_retry_after(self):
        now=[100.]
        p=NvidiaPool([str(i) for i in range(10)],self.config(),clock=lambda:now[0])
        old=p.acquire('model')
        for _ in range(4):
            lease=p.acquire('model');p.error(lease,'model',503);p.release(lease,'model',False)
        now[0]+=3.01
        probe=p.acquire('model')
        p.error(old,'model',503,20);p.release(old,'model',False)
        self.assertTrue(p.transient_circuits['model']['probe'])
        p.error(probe,'model',503,1);p.release(probe,'model',False)
        self.assertGreaterEqual(p.transient_circuits['model']['until'],now[0]+20)
        lease=p.acquire('healthy');p.error(lease,'healthy',429,60);p.error(lease,'healthy',429,1)
        self.assertEqual(p.cooldowns['member:'+lease.member.label],now[0]+60)
        p.release(lease,'healthy',False)

    def test_restart_retains_transient_circuit_and_recovers_without_resetting_quota(self):
        with TemporaryDirectory() as directory:
            config=self.config(state_path=str(Path(directory)/'state.json'))
            p=NvidiaPool([str(i) for i in range(10)],config)
            for _ in range(4):
                lease=p.acquire('model');p.error(lease,'model',502,30);p.release(lease,'model',False)
            after=NvidiaPool([str(i) for i in range(10)],config)
            with self.assertRaises(Unavailable):after.acquire('model')
            self.assertEqual(sum(len(starts) for starts in after.starts.values()),4)

    def test_sparse_failures_amid_successes_do_not_trip_the_model_circuit(self):
        p=NvidiaPool([str(i) for i in range(20)],self.config())
        for _ in range(10):
            failed=p.acquire('model');p.error(failed,'model',502);p.release(failed,'model',False)
            healthy=p.acquire('model');p.release(healthy,'model',True)
        self.assertNotIn('model',p.transient_circuits)

    def test_four_slow_tails_after_a_successful_64_burst_do_not_trip_every_key(self):
        now=[100.]
        p=NvidiaPool([str(i) for i in range(50)],self.config(capacity=64,model_capacity=64,per_key=3),clock=lambda:now[0])
        leases=[p.acquire('vision',workload='vision') for _ in range(64)]
        for lease in leases[:60]:p.release(lease,'vision',True)
        now[0]+=30
        for lease in leases[60:]:
            p.error(lease,'vision',502);p.release(lease,'vision',False)
        self.assertNotIn('vision',p.transient_circuits)
        self.assertEqual(p.inflight,0)
        self.assertEqual(p.members[leases[-1].index].cooldowns['vision'],160)
        healthy=p.acquire('vision',workload='vision');p.release(healthy,'vision',True)

    def test_recovery_queue_waits_for_successful_probe_instead_of_rejecting_burst(self):
        p=NvidiaPool([str(i) for i in range(10)],self.config(queue_seconds=.5))
        p.transient_circuits['model']={'until':p.clock()+.04,'backoff':3,'probe':False,'avoid':[]}
        probe=p.acquire('model')
        self.assertTrue(probe.recovery_probe)
        with ThreadPoolExecutor(max_workers=1) as executor:
            waiting=executor.submit(p.acquire,'model')
            time.sleep(.04);self.assertFalse(waiting.done())
            p.release(probe,'model',True)
            resumed=waiting.result(timeout=1)
            p.release(resumed,'model',True)
        self.assertEqual(p.inflight,0)
        self.assertNotIn('model',p.transient_circuits)

    def test_provider_500_burst_after_success_pauses_without_exhausting_every_key(self):
        now=[100.]
        p=NvidiaPool([str(i) for i in range(50)],self.config(capacity=64,model_capacity=64,per_key=3),clock=lambda:now[0])
        successful=[p.acquire('vision',workload='vision') for _ in range(64)]
        for lease in successful:p.release(lease,'vision',True)
        for _ in range(4):
            failed=p.acquire('vision',workload='vision')
            p.error(failed,'vision',500);p.release(failed,'vision',False)
        self.assertIn('vision',p.transient_circuits)
        with self.assertRaises(Unavailable):p.acquire('vision',workload='vision')
        self.assertLessEqual(sum(m.cooldowns.get('vision',0)>now[0] for m in p.members),4)
        now[0]+=3.01
        recovery=p.acquire('vision',workload='vision');p.release(recovery,'vision',True)
        self.assertNotIn('vision',p.transient_circuits)
        with ThreadPoolExecutor(max_workers=16) as executor:
            resumed=list(executor.map(lambda _:p.acquire('vision',workload='vision'),range(16)))
        self.assertEqual(len(resumed),16)
        for lease in resumed:p.release(lease,'vision',True)
        self.assertEqual(p.inflight,0)

    def test_recovery_avoids_failed_keys_and_promotes_a_healthy_home(self):
        now=[100.]
        p=NvidiaPool([str(i) for i in range(10)],self.config(),clock=lambda:now[0])
        failed=[]
        for i in range(4):
            lease=p.acquire('model','warm' if i==0 else None);failed.append(lease.index)
            p.error(lease,'model',503);p.release(lease,'model',False)
        now[0]+=3.01
        probe=p.acquire('model','warm')
        self.assertNotIn(probe.index,failed)
        self.assertEqual(p.affinity['warm'][0],probe.index)
        p.release(probe,'model',True)

    def test_failed_affinity_home_is_not_retried_on_each_later_turn(self):
        now=[100.]
        p=NvidiaPool(['bad','healthy','other'],self.config(),clock=lambda:now[0])
        failed=p.acquire('model','conversation')
        p.error(failed,'model',502);p.release(failed,'model',False)
        replacement=p.acquire('model','conversation',exclude=[failed.index])
        p.release(replacement,'model',True)
        now[0]+=4
        next_turn=p.acquire('model','conversation')
        self.assertEqual(next_turn.index,replacement.index)
        self.assertNotEqual(next_turn.index,failed.index)
        p.release(next_turn,'model',True)


class ControlTests(unittest.TestCase):
    def test_native_cache_identifiers_are_consumed_locally_but_history_remains(self):
        body = {'model':'stepfun-ai/step-5-preview','prompt_cache_key':'private',
                'conversation_id':'private','metadata':{'session_id':'private'},
                'prompt_cache_options':{'mode':'implicit','ttl':'1h'},
                'reasoning_effort':'max','messages':[{'role':'assistant','reasoning_content':'keep'}]}
        out = prepare_request(body)
        self.assertNotIn('prompt_cache_key',out)
        self.assertNotIn('prompt_cache_options',out)
        self.assertEqual(out['messages'],body['messages'])
        self.assertEqual(out['reasoning_effort'],'max')

    def test_low_budget_and_explicit_off_are_honored(self):
        model = 'nvidia/nemotron-3.5-lightning-30b-a3b'
        low = prepare_request({'model':model,'reasoning_effort':'low'})
        self.assertEqual(low['reasoning_budget'],1024)
        off = prepare_request({'model':model,'thinking':{'type':'disabled'},
            'reasoning_effort':'max','chat_template_kwargs':{'enable_thinking':True}})
        self.assertFalse(off['chat_template_kwargs']['enable_thinking'])
        self.assertNotIn('reasoning_budget',off)
        glm = prepare_request({'model':'z-ai/glm-5.3-flash','reasoning_effort':'xhigh'})
        self.assertEqual(glm['reasoning_effort'],'max')
        self.assertFalse(glm['chat_template_kwargs']['clear_thinking'])

    def test_provider_key_is_redacted(self):
        self.assertNotIn('super-secret',sanitized('nvapi-super-secret-0123456789'))


class NvidiaRelayTests(RelayTests):
    def setUp(self):
        super().setUp()
        self.relay.prepare_request = lambda body:body
        self.relay.require_semantic_first_event = True
        self.relay.retry_transport_errors = NvidiaServer.retry_transport_errors
        self.relay.may_retry_transport = NvidiaServer.may_retry_transport.__get__(self.relay)
        self.relay.record_upstream_exception = NvidiaServer.record_upstream_exception.__get__(self.relay)
        self.relay.stream_terminal = NvidiaServer.stream_terminal.__get__(self.relay)
        self.relay.completed_drains=threading.BoundedSemaphore(4)
        self.relay.drain_completed=NvidiaServer.drain_completed.__get__(self.relay)

    def enable_nvidia_policy(self):
        self.relay.pool = NvidiaPool(['one','two','three','four','five'], {'capacity':8,'per_key':2,
            'model_capacity':8,'key_rpm':40,'global_rpm':0,'model_rpm':0,'queue_seconds':0})
        self.relay.max_attempts = 3
        self.relay.pre_output_timeout = 1
        self.relay.response_timeout = 3
        self.relay.read_timeout = .25
        self.relay.finish_error_stream = True
        for name in ('retry_status','record_response_error','read_response','upstream_options'):
            setattr(self.relay,name,getattr(NvidiaServer,name).__get__(self.relay))

    def test_provider_errors_rotate_to_a_different_key_without_waiting(self):
        for code, message in [(401,'invalid key'),(403,'invalid api key'),(408,'timeout'),
                              (429,'rate limit'),(500,'failure'),(502,'failure'),(503,'failure'),(504,'failure')]:
            with self.subTest(status=code):
                self.enable_nvidia_policy()
                self.received.clear()
                type(self).upstream_sequence = [(code,json.dumps({'error':{'message':message}}).encode()),None]
                started = time.monotonic()
                self.assertEqual(self.send(b'{"model":"model"}')[0],200)
                self.assertLess(time.monotonic()-started,.5)
                self.assertEqual(len(self.received),2)
                self.assertNotEqual(self.received[0]['authorization'],self.received[1]['authorization'])
                self.assertEqual(self.relay.pool.inflight,0)

    def test_policy_denial_does_not_rotate_or_poison_the_key(self):
        self.enable_nvidia_policy()
        type(self).upstream_error = (403,b'{"error":{"message":"Safety policy rejected request"}}')
        self.assertEqual(self.send(b'{"model":"model"}')[0],403)
        self.assertEqual(len(self.received),1)
        self.assertFalse(self.relay.pool.cooldowns)
        self.assertTrue(all(not key.cooldowns for key in self.relay.pool.members))

    def test_failed_requests_stop_after_three_distinct_keys(self):
        self.enable_nvidia_policy()
        type(self).upstream_error = (503,b'{"error":{"message":"temporary"}}')
        self.assertEqual(self.send(b'{"model":"model"}')[0],503)
        self.assertEqual(len(self.received),3)
        self.assertEqual(len({row['authorization'] for row in self.received}),3)
        self.assertEqual(self.relay.pool.inflight,0)

    def test_empty_success_response_rotates_then_returns_real_answer(self):
        self.enable_nvidia_policy()
        type(self).upstream_sequence = [(200,b''),None]
        status,body = self.send(b'{"model":"model","stream":true}')
        self.assertEqual(status,200)
        self.assertIn(b'data: [DONE]',body)
        self.assertEqual(len(self.received),2)

    def test_comment_trickle_cannot_keep_first_output_wait_alive(self):
        self.enable_nvidia_policy()
        self.relay.pre_output_timeout = .04
        class Heartbeat(httpx.SyncByteStream):
            def __iter__(self):
                for _ in range(20):
                    time.sleep(.01)
                    yield b': heartbeat\n\n'
        @contextmanager
        def stream(*_args,**_kwargs):
            response = httpx.Response(200,headers={'Content-Type':'text/event-stream'},stream=Heartbeat())
            try:
                yield response
            finally:
                response.close()
        started = time.monotonic()
        with patch.object(self.relay.client,'stream',side_effect=stream) as calls:
            self.assertEqual(self.send(b'{"model":"model","stream":true}')[0],504)
            self.assertEqual(calls.call_count,1)
        self.assertLess(time.monotonic()-started,.3)
        self.assertEqual(self.relay.pool.inflight,0)

    def test_mid_stream_timeout_ends_with_error_without_replay(self):
        self.enable_nvidia_policy()
        class Interrupted(httpx.SyncByteStream):
            def __iter__(self):
                yield b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
                raise httpx.ReadTimeout('fixture')
        @contextmanager
        def stream(*_args,**_kwargs):
            response = httpx.Response(200,headers={'Content-Type':'text/event-stream'},stream=Interrupted())
            try:
                yield response
            finally:
                response.close()
        with patch.object(self.relay.client,'stream',side_effect=stream) as calls:
            status,body = self.send(b'{"model":"model","stream":true}')
        self.assertEqual(status,200)
        self.assertIn(b'partial',body)
        self.assertIn(b'"error"',body)
        self.assertEqual(calls.call_count,1)
        self.assertEqual(self.relay.pool.inflight,0)

    def test_connection_failure_retries_once_with_a_different_key(self):
        native_stream = self.relay.client.stream
        attempts = []
        def flaky_stream(*args,**kwargs):
            attempts.append(kwargs['headers']['Authorization'])
            if len(attempts) == 1:
                raise httpx.ConnectError('temporary connection failure')
            return native_stream(*args,**kwargs)
        with patch.object(self.relay.client,'stream',side_effect=flaky_stream):
            status,body = self.send(b'{"model":"model","stream":true}')
        self.assertEqual(status,200)
        self.assertIn(b'data: [DONE]',body)
        self.assertEqual(len(attempts),2)
        self.assertNotEqual(attempts[0],attempts[1])
        self.assertEqual(self.relay.pool.inflight,0)

    def test_connection_failover_is_bounded_to_two_attempts(self):
        with patch.object(self.relay.client,'stream',side_effect=httpx.ConnectError('temporary')) as stream:
            self.assertEqual(self.send(b'{"model":"model","stream":true}')[0],502)
            self.assertEqual(stream.call_count,2)
        self.assertEqual(self.relay.pool.inflight,0)

    def test_pre_output_read_and_write_errors_rotate_keys_with_bounded_attempts(self):
        for failure in (httpx.ReadTimeout('ambiguous'),httpx.WriteError('ambiguous'),httpx.ReadError('ambiguous')):
            with self.subTest(failure=type(failure).__name__):
                self.relay.pool=NvidiaPool(['one','two'],{'capacity':4,'per_key':2,
                    'key_rpm':40,'global_rpm':0,'model_rpm':0,'queue_seconds':0})
                with patch.object(self.relay.client,'stream',side_effect=failure) as stream:
                    self.assertEqual(self.send(b'{"model":"model","stream":true}')[0],502)
                    self.assertEqual(stream.call_count,2)
                self.assertEqual(self.relay.pool.inflight,0)
                self.assertEqual(self.relay.pool.transport_error_counts['model'][type(failure).__name__],2)

    def test_native_image_parts_receive_the_vision_slot_but_text_urls_do_not(self):
        self.relay.pool=NvidiaPool(['one','two'],{'capacity':4,'per_key':2,'key_rpm':40,
            'global_rpm':0,'model_rpm':0,'vision_capacity':1,'queue_seconds':0})
        self.relay.acquire_request=NvidiaServer.acquire_request.__get__(self.relay)
        lease=self.relay.pool.acquire('model',workload='vision')
        body={'model':'model','messages':[{'role':'user','content':[{'type':'image_url','image_url':{'url':'data:image/png;base64,AAAA'}}]}]}
        self.assertEqual(self.send(json.dumps(body).encode())[0],429)
        text={**body,'messages':[{'role':'user','content':'https://image.example/picture.png'}]}
        self.assertEqual(self.send(json.dumps(text).encode())[0],200)
        self.relay.pool.release(lease,'model',True)
        self.assertEqual(self.relay.pool.workload_inflight,{'chat':0,'vision':0})

    def test_role_only_stream_is_not_a_successful_answer(self):
        type(self).upstream_error = (200,b'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\ndata: [DONE]\n\n')
        self.assertEqual(self.send(b'{"model":"model","stream":true}')[0],502)

    def test_unavailable_model_fails_fast_without_blocking_healthy_models(self):
        self.relay.pool = NvidiaPool(['one'],{'capacity':2,'per_key':1,'key_rpm':40,
            'global_rpm':0,'model_rpm':0,'queue_seconds':0,'unavailable_models':{'model':'verified upstream outage'}})
        self.assertEqual(self.send(b'{"model":"model","stream":true}')[0],503)
        self.assertFalse(self.received)


if __name__=='__main__':
    unittest.main()
