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
from internal_tools.vision_routing import PRIMARY
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
                 patch('urllib.request.urlopen',return_value=nullcontext(SimpleNamespace(status=200))),redirect_stdout(io.StringIO()):
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
