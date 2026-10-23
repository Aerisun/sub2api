from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from image_hub.egress_control import route_lock


class RouteLockTests(unittest.TestCase):
    def test_independent_supervisors_cannot_mutate_one_group_concurrently(self):
        with tempfile.TemporaryDirectory() as directory, patch('image_hub.egress_control.ROOT',Path(directory)):
            role={'mihomo_config':str(Path(directory)/'mihomo.yaml'),'group':'same'}
            def attempt():
                with route_lock(dict(role),timeout=.08):return True
            with ThreadPoolExecutor(max_workers=1) as executor:
                with route_lock(role):
                    with self.assertRaises(TimeoutError):executor.submit(attempt).result(timeout=1)
                self.assertTrue(executor.submit(attempt).result(timeout=1))

    def test_different_proxy_groups_do_not_block_each_other(self):
        with tempfile.TemporaryDirectory() as directory, patch('image_hub.egress_control.ROOT',Path(directory)):
            role={'mihomo_config':str(Path(directory)/'mihomo.yaml'),'group':'one'}
            with route_lock(role),route_lock({**role,'group':'two'},timeout=.01):pass
