"""The primary can recover without pausing ongoing customer image tools."""
from contextlib import nullcontext,redirect_stdout
import io
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock,patch

from internal_tools import vision_monitor as monitor
from internal_tools.vision_routing import PRIMARY,OMNI
from image_hub.concurrency import SlotLimitReached
from internal_tools import test_vision_quality as quality_fixture


class VisionMonitorTests(unittest.TestCase):
    def check(self,ready):
        good=quality_fixture.VisionQualityTests();good.setUp()
        router=SimpleNamespace(active_count=lambda:1,snapshot=lambda:[{'id':PRIMARY,'ready':ready}],probe=Mock())
        calls=[]
        def recognition(url,key,body,**kwargs):
            calls.append((body,kwargs));kwargs['on_text']('fixture output')
            return 200,{'choices':[{'message':{'content':json.dumps(good.good)},'finish_reason':'stop'}],
                        'usage':{'total_tokens':12}}
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            for path,data in [('secrets/nvidia/internal_key.txt',b'fixture-key'),('nvidia_pool/fixtures/vision-quality.png',b'fixture-image')]:
                p=root/path;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(data)
            with patch.object(monitor,'ROOT',root),patch.object(monitor,'VisionRouter',return_value=router), \
                 patch.object(monitor,'recognition_request',side_effect=recognition), \
                 patch.object(monitor,'wait_ready'), \
                 patch.object(monitor,'RECOGNITION_UPSTREAM_SLOTS',SimpleNamespace(acquire=lambda **_:nullcontext())),redirect_stdout(io.StringIO()):
                monitor.main()
        return calls,router

    def test_unavailable_primary_is_reverified_while_customers_use_backup(self):
        calls,router=self.check(False)
        self.assertEqual(len(calls),1);self.assertEqual(calls[0][0]['model'],PRIMARY)
        self.assertEqual(calls[0][1]['timeout'],30)
        self.assertEqual(router.probe.call_args.args[:2],(PRIMARY,True))

    def test_healthy_routes_do_not_add_probe_load_during_customer_work(self):
        calls,router=self.check(True)
        self.assertEqual(calls,[]);router.probe.assert_not_called()

    def test_slow_probe_does_not_overrun_service_deadline_or_mark_skipped_backup_failed(self):
        now=[100.0]
        router=SimpleNamespace(active_count=lambda:0,snapshot=lambda:[{'id':PRIMARY,'ready':True,'updated':1},
            {'id':OMNI,'ready':True,'updated':2}],probe=Mock())
        def timeout(*args,**kwargs):
            now[0]+=kwargs['timeout']
            raise TimeoutError('fixture provider stalled')
        with patch.object(monitor,'recognition_request',side_effect=timeout) as request, \
             patch.object(monitor,'RECOGNITION_UPSTREAM_SLOTS',SimpleNamespace(acquire=lambda **_:nullcontext())):
            rows=monitor.probe_routes(router,'fixture','fixture',clock=lambda:now[0])
        self.assertEqual(request.call_count,1)
        self.assertEqual(rows[1]['reason'],'probe_budget_exhausted')
        self.assertIsNone(rows[1]['ready'])
        self.assertEqual(router.probe.call_count,1)
        self.assertLessEqual(now[0],135)

    def test_older_backup_observation_is_checked_before_a_recent_slow_primary(self):
        router=SimpleNamespace(active_count=lambda:0,snapshot=lambda:[{'id':PRIMARY,'ready':True,'updated':20},
            {'id':OMNI,'ready':True,'updated':10}],probe=Mock())
        models=[]
        def request(url,key,body,**kwargs):
            models.append(body['model']);raise HubError(503,'fixture busy')
        from image_hub.server import HubError
        with patch.object(monitor,'recognition_request',side_effect=request), \
             patch.object(monitor,'RECOGNITION_UPSTREAM_SLOTS',SimpleNamespace(acquire=lambda **_:nullcontext())):
            monitor.probe_routes(router,'fixture','fixture')
        self.assertEqual(models,[OMNI,PRIMARY])
        router.probe.assert_not_called()

    def test_recovery_probe_cannot_exceed_customer_execution_capacity(self):
        router=SimpleNamespace(active_count=lambda:16,snapshot=lambda:[{'id':PRIMARY,'ready':False}],probe=Mock())
        limiter=Mock();limiter.acquire.side_effect=SlotLimitReached()
        with patch.object(monitor,'RECOGNITION_UPSTREAM_SLOTS',limiter),patch.object(monitor,'recognition_request') as request:
            rows=monitor.probe_routes(router,'fixture','fixture')
        self.assertEqual(rows[0]['reason'],'customer_capacity_in_use')
        self.assertIsNone(rows[0]['ready'])
        request.assert_not_called();router.probe.assert_not_called()
