import tempfile
import concurrent.futures
import threading
import unittest
from pathlib import Path
from internal_tools.vision_routing import VisionRouter,PRIMARY,OMNI

class VisionRoutingTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.now=1000
  self.router=VisionRouter(Path(self.tmp.name)/'routes.db',clock=lambda:self.now)
 def test_parallel_workers_preserve_primary_and_release_without_exposing_images(self):
  first=self.router.acquire();second=VisionRouter(self.router.path,clock=lambda:self.now).acquire()
  self.assertEqual(first[1],PRIMARY);self.assertEqual(second[1],PRIMARY)
  more=[self.router.acquire() for _ in range(62)];self.assertTrue(all(x[1]==PRIMARY for x in more))
  self.assertIsNone(self.router.acquire())
  self.router.finish(first,True,1200);self.router.finish(second,True,1500)
  self.assertEqual(sum(r['successes'] for r in self.router.snapshot()),2)
 def test_transient_failure_skips_model_until_cooldown_and_recovers(self):
  first=self.router.acquire();self.router.finish(first,False,transient=True)
  for _ in range(2):
   failed=self.router.acquire();self.router.finish(failed,False,transient=True)
  second=self.router.acquire();self.assertEqual(second[1],OMNI);self.router.finish(second,True,1500)
  self.now+=11;self.router.probe(PRIMARY,True,1000);self.assertEqual(self.router.acquire()[1],PRIMARY)
 def test_isolated_request_failure_keeps_healthy_model_available_to_other_customers(self):
  first=self.router.acquire();self.router.finish(first,False,transient=True,partial_failure=True)
  second=self.router.acquire();self.assertEqual(second[1],PRIMARY)
  self.router.finish(second,True,1200)
  state=next(r for r in self.router.snapshot() if r['id']==PRIMARY)
  self.assertTrue(state['ready']);self.assertEqual(state['failures'],0)
 def test_fast_small_model_cannot_displace_healthy_quality_primary(self):
  with self.router.db() as c:
   c.execute('UPDATE models SET latency=6000 WHERE id=?',(PRIMARY,))
   c.execute('UPDATE models SET latency=50 WHERE id=?',(OMNI,))
  self.assertEqual(self.router.acquire()[1],PRIMARY)

 def test_slow_failed_tails_do_not_close_a_recently_successful_primary(self):
  leases=[self.router.acquire() for _ in range(64)]
  for lease in leases[:60]:self.router.finish(lease,True,3500)
  self.now+=30
  for lease in leases[60:]:self.router.finish(lease,False,transient=True,partial_failure=True)
  self.assertEqual(self.router.acquire()[1],PRIMARY)

 def test_fresh_verified_quality_probe_restores_route_during_old_cooldown(self):
  self.router.probe(PRIMARY,False)
  self.assertEqual(self.router.acquire()[1],OMNI)
  self.router.probe(PRIMARY,True,4500)
  state=next(r for r in self.router.snapshot() if r['id']==PRIMARY)
  self.assertTrue(state['ready']);self.assertEqual(state['cooldown'],0)
  self.assertEqual(self.router.acquire()[1],PRIMARY)
  self.router.probe(PRIMARY,False)
  self.assertEqual(self.router.acquire()[1],OMNI)
 def test_health_probe_reenables_only_verified_completed_models(self):
  self.router.probe(PRIMARY,False);self.assertEqual(self.router.acquire()[1],OMNI)
  self.now+=31;self.router.probe(PRIMARY,True,500);self.assertTrue(next(r for r in self.router.snapshot() if r['id']==PRIMARY)['ready'])

 def test_probe_timeouts_do_not_evict_a_verified_backup_on_first_failure(self):
  self.router.probe(OMNI,True,3000)
  self.now+=31;self.router.probe(OMNI,False,transient=True)
  self.assertEqual(self.router.acquire(exclude={PRIMARY})[1],OMNI)
  self.now+=31;self.router.probe(OMNI,False,transient=True)
  self.assertEqual(self.router.acquire(exclude={PRIMARY})[1],OMNI)
  self.now+=31;self.router.probe(OMNI,False,transient=True)
  self.assertIsNone(self.router.acquire(exclude={PRIMARY}))
  self.router.probe(OMNI,True,2800)
  self.assertEqual(self.router.acquire(exclude={PRIMARY})[1],OMNI)

 def test_completed_quality_failure_still_immediately_stops_backup(self):
  self.router.probe(OMNI,True,3000);self.router.probe(OMNI,False)
  self.assertIsNone(self.router.acquire(exclude={PRIMARY}))
 def test_expired_worker_lease_does_not_leak_capacity_and_standbys_remain_cold(self):
  leases=[self.router.acquire() for _ in range(64)];self.assertTrue(all(leases));self.assertIsNone(self.router.acquire())
  self.now+=121;self.assertIsNotNone(self.router.acquire())
 def test_different_customers_do_not_share_response_data(self):
  a=self.router.acquire();self.router.finish(a,True,2500)
  with self.router.db() as c:
   tables={row[0] for row in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
   self.assertEqual(tables,{'models','leases'})

 def test_64_simultaneous_connections_share_one_limit_and_release_all_leases(self):
  gate=threading.Barrier(64)
  def acquire(_):
   router=VisionRouter(self.router.path,clock=lambda:self.now)
   gate.wait(timeout=10)
   return router.acquire()
  with concurrent.futures.ThreadPoolExecutor(max_workers=64) as pool:
   leases=list(pool.map(acquire,range(64)))
  self.assertEqual(len({lease[0] for lease in leases}),64)
  self.assertTrue(all(lease[1]==PRIMARY for lease in leases))
  self.assertIsNone(self.router.acquire())
  for lease in leases:self.router.finish(lease,True,1200)
  with self.router.db() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM leases').fetchone()[0],0)
  self.assertEqual(self.router.acquire()[1],PRIMARY)

 def test_retry_cannot_bypass_shared_capacity_using_backup(self):
  router=VisionRouter(self.router.path,clock=lambda:self.now,capacity=2)
  primary=router.acquire();backup=router.acquire(exclude={PRIMARY})
  self.assertEqual(backup[1],OMNI)
  self.assertIsNone(router.acquire())
  router.finish(primary,False)
  self.assertEqual(router.acquire()[1],PRIMARY)

 def test_ready_check_respects_cooldowns_exclusions_and_retired_models(self):
  self.assertTrue(self.router.has_ready())
  self.router.probe(OMNI,False)
  self.assertFalse(self.router.has_ready(exclude={PRIMARY}))
  self.router.probe(PRIMARY,False)
  with self.router.db() as c:
   c.execute('INSERT INTO models(id,latency,ready) VALUES(?,?,?)',('retired-model',1,1))
  self.assertFalse(self.router.has_ready())
  self.router.probe(PRIMARY,True,1000)
  self.assertTrue(self.router.has_ready())
  self.assertFalse(self.router.has_ready(exclude={PRIMARY}))
