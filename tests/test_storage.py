"""Storage behavior for separating TCP reachability from real validation."""
import tempfile
import unittest
from pathlib import Path

from proxy_hub.storage import Storage


class TestRealValidationStorage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.storage = Storage(str(Path(self.tmp.name) / "proxy_hub.db"))
        self.proxy = {"proxy_type": "trojan", "host": "node.example", "port": 443}
        self.assertTrue(self.storage.add_proxy(self.proxy))
        self.proxy_id = self.storage.get_proxies_by_type("trojan")[0]["id"]

    def tearDown(self):
        Storage._cache.pop(self.storage.db_path, None)
        Storage._locks.pop(self.storage.db_path, None)
        self.tmp.cleanup()

    def test_tcp_alive_is_not_subscription_eligible(self):
        self.storage.update_proxy_status(self.proxy_id, True, 18.0)
        self.assertEqual(len(self.storage.get_alive_proxies()), 1)
        self.assertEqual(self.storage.get_real_alive_proxies(), [])
        self.assertEqual(self.storage.stats()["real_alive"], 0)

    def test_real_validation_adds_and_tcp_recheck_clears_eligibility(self):
        self.storage.update_proxy_status(self.proxy_id, True, 18.0)
        self.storage.update_real_proxy_status(self.proxy_id, True, 42.0)
        verified = self.storage.get_real_alive_proxies()
        self.assertEqual([p["id"] for p in verified], [self.proxy_id])
        self.assertEqual(verified[0]["latency_ms"], 42.0)
        self.storage.update_proxy_status(self.proxy_id, True, 19.0)
        self.assertEqual(self.storage.get_real_alive_proxies(), [])

    def test_reparsed_proxy_requires_fresh_real_validation(self):
        self.storage.update_real_proxy_status(self.proxy_id, True, 42.0)
        self.assertEqual(len(self.storage.get_real_alive_proxies()), 1)
        self.storage.add_proxy({**self.proxy, "name": "updated"})
        self.assertEqual(self.storage.get_real_alive_proxies(), [])


if __name__ == "__main__":
    unittest.main()
