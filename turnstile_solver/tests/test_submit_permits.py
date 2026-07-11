from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.submit_permits import SubmitPermitError, SubmitPermitPool


class SubmitPermitPoolTests(unittest.TestCase):
    def test_timeout_and_release_are_fail_closed(self):
        pool = SubmitPermitPool(1)
        permit = pool.acquire(0)
        with self.assertRaises(SubmitPermitError):
            pool.acquire(0)
        result = pool.release(permit.permit_id)
        self.assertTrue(result["ok"])
        with self.assertRaises(SubmitPermitError):
            pool.release(permit.permit_id)

    def test_shutdown_invalidates_active_permits(self):
        pool = SubmitPermitPool(1)
        permit = pool.acquire(0)
        pool.close()
        with self.assertRaises(SubmitPermitError):
            pool.release(permit.permit_id)
        with self.assertRaises(SubmitPermitError):
            pool.acquire(0)

    def test_expired_permit_is_reaped_and_slot_can_be_reacquired(self):
        pool = SubmitPermitPool(1, lease_sec=30)
        leaked = pool.acquire(0, lease_sec=1)
        self.assertEqual(pool.reap_expired(leaked.expires_at_ms), 1)
        replacement = pool.acquire(0, lease_sec=9)
        self.assertEqual(replacement.lease_sec, 9)
        with self.assertRaises(SubmitPermitError):
            pool.release(leaked.permit_id)
        stats = pool.stats()
        self.assertEqual(stats["expired"], 1)
        self.assertEqual(stats["reaped"], 1)
        pool.release(replacement.permit_id)


if __name__ == "__main__":
    unittest.main()
