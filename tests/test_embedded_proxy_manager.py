# tests/test_embedded_proxy_manager.py
import time
import unittest

from embedded_proxy_manager import EmbeddedProxyManager, NodeSlot


class LeaseTests(unittest.TestCase):
    def _mgr(self, n=3):
        m = EmbeddedProxyManager.__new__(EmbeddedProxyManager)
        m._lock = __import__("threading").RLock()
        m._nodes = {}
        for i in range(n):
            slot = NodeSlot(
                id=f"n{i}",
                name=f"node-{i}",
                server=f"{i}.example",
                port=443,
                protocol="vless",
                local_http=f"http://127.0.0.1:{28000+i}",
                healthy=True,
            )
            m._nodes[slot.id] = slot
        m._running = True
        return m

    def test_prefer_idle_then_reuse_lowest_ref(self):
        m = self._mgr(2)
        a = m.acquire()
        self.assertEqual(a.ref_count, 1)
        b = m.acquire()
        self.assertNotEqual(a.id, b.id)
        c = m.acquire()  # reuse
        self.assertIn(c.id, {a.id, b.id})
        self.assertEqual(m._nodes[c.id].ref_count, 2)

    def test_exclude_and_unhealthy_not_selected(self):
        m = self._mgr(2)
        m._nodes["n0"].healthy = False
        got = m.acquire(exclude_ids={"n1"})
        self.assertIsNone(got)

    def test_release_failed_marks_unhealthy_or_cooldown(self):
        m = self._mgr(1)
        n = m.acquire()
        m.release(n.id, failed=True)
        self.assertEqual(m._nodes[n.id].ref_count, 0)
        self.assertTrue(
            (not m._nodes[n.id].healthy)
            or m._nodes[n.id].cooldown_until > time.time()
        )
