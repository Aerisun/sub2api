"""Capacity checks use temporary slots and local fixtures, never paid models."""
from contextlib import ExitStack
import multiprocessing
import concurrent.futures
import threading
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from image_hub.concurrency import FileSlotPool, SlotLimitReached, configured_limit
from image_hub.server import HubError
from internal_tools.image_tools import ImageRecognitionExecutor
from internal_tools.test_image_tools import IMAGE
from internal_tools.vision_routing import VisionRouter


def hold_slots(directory, ready, release_path):
    with ExitStack() as stack:
        pool = FileSlotPool(64, directory)
        for _ in range(16):
            stack.enter_context(pool.acquire())
        ready.put(True)
        deadline = time.monotonic() + 15
        while not Path(release_path).exists() and time.monotonic() < deadline:
            time.sleep(.01)


class ConcurrencyTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        router = patch('internal_tools.image_tools.ROUTER', VisionRouter(Path(folder.name)/'routes.db'))
        router.start()
        self.addCleanup(router.stop)
        for name, limit, directory in [('RECOGNITION_SLOTS',64,'accepted'),('RECOGNITION_UPSTREAM_SLOTS',16,'upstream')]:
            slots=patch('internal_tools.image_tools.'+name,FileSlotPool(limit,Path(folder.name)/directory))
            slots.start();self.addCleanup(slots.stop)

    def test_waiting_slot_resumes_on_release_and_timeout_never_leaks_a_slot(self):
        with tempfile.TemporaryDirectory() as folder:
            pool=FileSlotPool(1,folder)
            entered=threading.Event()
            def waiting():
                with pool.acquire(timeout=1):entered.set()
            with pool.acquire():
                thread=threading.Thread(target=waiting);thread.start()
                self.assertFalse(entered.wait(.05))
                start=time.monotonic()
                with self.assertRaises(SlotLimitReached),pool.acquire(timeout=.05):pass
                self.assertLess(time.monotonic()-start,.3)
            thread.join(2);self.assertFalse(thread.is_alive());self.assertTrue(entered.is_set())
            with pool.acquire():pass

    def test_queue_wait_does_not_consume_actual_inference_budget(self):
        for slot_name in ('RECOGNITION_SLOTS','RECOGNITION_UPSTREAM_SLOTS'):
            with self.subTest(waiting_for=slot_name),tempfile.TemporaryDirectory() as folder:
                slots=FileSlotPool(1,Path(folder)/slot_name)
                def recognize(*args,**kwargs):
                    self.assertGreater(kwargs['timeout'],.18)
                    time.sleep(.12)
                    return 200,{'choices':[{'message':{'content':'red'}}],'usage':{'total_tokens':12}}
                with patch('internal_tools.image_tools.'+slot_name,slots), \
                     patch('internal_tools.image_tools.RECOGNITION_QUEUE_SECONDS',.5), \
                     patch('internal_tools.image_tools.RECOGNITION_EXECUTION_SECONDS',.2), \
                     patch('internal_tools.image_tools.authorized_models',return_value={'image_recognition'}), \
                     patch('internal_tools.image_tools.recognition_request',side_effect=recognize), \
                     concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                    with slots.acquire():
                        result=executor.submit(ImageRecognitionExecutor('sk-fixture'),{'image':IMAGE},'queued')
                        time.sleep(.16)
                        self.assertFalse(result.done())
                    self.assertEqual(result.result(2)['status'],'completed')

    def test_64_tool_calls_finish_with_bounded_provider_load_and_unchanged_model(self):
        with tempfile.TemporaryDirectory() as folder:
            accepted=FileSlotPool(64,Path(folder)/'accepted')
            upstream=FileSlotPool(4,Path(folder)/'upstream')
            gate=threading.Barrier(64);lock=threading.Lock();load={'active':0,'peak':0,'calls':0}
            def recognize(url,key,body,**kwargs):
                self.assertEqual(body['model'],'nvidia/nemotron-3.5-super-vl-120b-a12b')
                self.assertEqual(body['reasoning_budget'],256)
                self.assertEqual(body['max_tokens'],3072)
                with lock:
                    load['active']+=1;load['peak']=max(load['peak'],load['active']);load['calls']+=1
                time.sleep(.08)
                with lock:load['active']-=1
                return 200,{'choices':[{'message':{'content':'real fixture result'}}],
                    'usage':{'prompt_tokens':40,'completion_tokens':2,'total_tokens':42}}
            def one(i):
                gate.wait(timeout=10)
                return ImageRecognitionExecutor('sk-fixture')({'image':IMAGE},str(i))
            with patch('internal_tools.image_tools.RECOGNITION_SLOTS',accepted), \
                 patch('internal_tools.image_tools.RECOGNITION_UPSTREAM_SLOTS',upstream), \
                 patch('internal_tools.image_tools.authorized_models',return_value={'image_recognition'}), \
                 patch('internal_tools.image_tools.recognition_request',side_effect=recognize), \
                 concurrent.futures.ThreadPoolExecutor(max_workers=64) as executor:
                results=list(executor.map(one,range(64)))
            self.assertEqual(load,{'active':0,'peak':4,'calls':64})
            self.assertEqual(sum(r['status']=='completed' for r in results),64)
            self.assertTrue(all(r['cached'] is False and r['usage']['total_tokens']==42 for r in results))

    def test_64_slots_shared_by_four_processes_and_recovered_after_exit(self):
        context = multiprocessing.get_context("spawn")
        with tempfile.TemporaryDirectory() as folder:
            ready = context.Queue()
            release_path = str(Path(folder).parent / (Path(folder).name + '-release'))
            workers = [context.Process(target=hold_slots, args=(folder, ready, release_path)) for _ in range(4)]
            try:
                for worker in workers:
                    worker.start()
                for _ in workers:
                    self.assertTrue(ready.get(timeout=8))
                pool = FileSlotPool(64, folder)
                with self.assertRaises(SlotLimitReached), pool.acquire():
                    pass
                workers[0].terminate()
                workers[0].join(timeout=3)
                self.assertFalse(workers[0].is_alive())
                with ExitStack() as stack:
                    for _ in range(16):
                        stack.enter_context(pool.acquire())
                    with self.assertRaises(SlotLimitReached), pool.acquire():
                        pass
                self.assertEqual(len(list(Path(folder).iterdir())), 64)
                self.assertTrue(all(path.stat().st_size == 0 for path in Path(folder).iterdir()))
            finally:
                Path(release_path).touch()
                for worker in workers:
                    if worker.pid is not None:
                        worker.join(timeout=3)
                        if worker.is_alive():
                            worker.kill()
                            worker.join(timeout=2)
                ready.close()
                ready.join_thread()
                Path(release_path).unlink(missing_ok=True)

    def test_busy_recognition_never_forwards_a_billable_request(self):
        with tempfile.TemporaryDirectory() as folder:
            pool = FileSlotPool(1, folder)
            with pool.acquire(), patch('internal_tools.image_tools.RECOGNITION_SLOTS', pool), \
                    patch('internal_tools.image_tools.RECOGNITION_QUEUE_SECONDS',.05), \
                    patch('internal_tools.image_tools.authorized_models', return_value={'image_recognition'}), \
                    patch('internal_tools.image_tools.recognition_request') as request:
                with self.assertRaises(HubError) as caught:
                    ImageRecognitionExecutor('sk-fixture')({'image': IMAGE}, 'fixture')
                self.assertEqual(caught.exception.status, 429)
                self.assertTrue(caught.exception.retryable)
                request.assert_not_called()

    def test_failed_upstream_releases_recognition_slot(self):
        with tempfile.TemporaryDirectory() as folder:
            pool = FileSlotPool(1, folder)
            with patch('internal_tools.image_tools.RECOGNITION_SLOTS', pool), \
                    patch('internal_tools.image_tools.authorized_models', return_value={'image_recognition'}), \
                    patch('internal_tools.image_tools.recognition_request', side_effect=HubError(504, 'fixture')):
                with self.assertRaises(HubError):
                    ImageRecognitionExecutor('sk-fixture')({'image': IMAGE}, 'fixture')
            with pool.acquire():
                pass

    def test_environment_limits_are_bounded(self):
        for value in ('0', '-1', '1025', 'invalid'):
            with self.subTest(value=value), patch.dict('os.environ', {'FIXTURE_TOOL_LIMIT': value}):
                with self.assertRaises(ValueError):
                    configured_limit('FIXTURE_TOOL_LIMIT', 48)
        with patch.dict('os.environ', {'FIXTURE_TOOL_LIMIT': '16'}):
            self.assertEqual(configured_limit('FIXTURE_TOOL_LIMIT', 48), 16)


if __name__ == '__main__':
    unittest.main()
