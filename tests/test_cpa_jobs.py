from __future__ import annotations

import unittest

from cpa_inspector.models import JobResult
from cpa_inspector.services.jobs import JobManager


class JobManagerTest(unittest.TestCase):
    def test_create_update_finish(self) -> None:
        mgr = JobManager()
        job = mgr.create("health-check", total=2)
        self.assertEqual(job.status, "queued")
        mgr.update(job.job_id, current=1, message="1/2", results=[JobResult("a", "成功")])
        got = mgr.get(job.job_id)
        self.assertEqual(got.current, 1)
        self.assertTrue(mgr.has_running())
        mgr.finish(job.job_id, status="success")
        self.assertFalse(mgr.has_running())
        self.assertEqual(mgr.get(job.job_id).status, "success")
