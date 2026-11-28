"""Pipeline health: the checks catch a pipeline that has stopped producing, and every page says so."""
import json
import unittest
from datetime import datetime, timedelta, timezone

from pln_agent import health
from pln_agent.db import now_iso
from tests.helpers import make


def by_key(res):
    return {c["key"]: c for c in res}


class HealthTests(unittest.TestCase):
    def setUp(self):
        self.db, self.s = make()

    def test_quiet_pipeline_is_flagged(self):
        later = datetime.now(timezone.utc) + timedelta(days=5)  # five days on, nothing new has happened
        c = by_key(health.checks(self.db, self.s, later))
        self.assertFalse(c["new_drafts"]["ok"])
        self.assertEqual(c["new_drafts"]["level"], "bad")
        self.assertFalse(c["published"]["ok"])
        self.assertTrue(c["drafting_loop"]["ok"])

    def test_fresh_drafts_count(self):
        c = by_key(health.checks(self.db, self.s))
        self.assertTrue(c["new_drafts"]["ok"], c["new_drafts"])  # the fixture's drafts were just made

    def test_drafting_loop_is_flagged(self):
        self.db.execute("DELETE FROM variants")
        for i in range(health.LOOP_PER_HOUR):
            self.db.insert("editorial_packs", {"pack_id": f"p{i}", "asset_id": "a", "created_at": now_iso(), "summary": {}})
        c = by_key(health.checks(self.db, self.s))
        self.assertFalse(c["drafting_loop"]["ok"])

    def test_posts_waiting_with_nothing_approved(self):
        old = (datetime.now(timezone.utc) - timedelta(days=4)).replace(microsecond=0).isoformat()
        self.db.execute("UPDATE variants SET updated_at=? WHERE state='in_review'", [old])
        self.db.execute("DELETE FROM approvals")
        c = by_key(health.checks(self.db, self.s))
        self.assertFalse(c["waiting_approval"]["ok"], c["waiting_approval"])
        self.assertIn("none approved since never", c["waiting_approval"]["title"])

    def test_run_saves_and_pages_show_the_banner(self):
        from fastapi.testclient import TestClient
        from pln_agent.web import app as web
        web._db = self.db
        res = health.run(self.db, self.s, datetime.now(timezone.utc) + timedelta(days=5))
        self.assertEqual(json.loads(self.db.kv_get(health.KEY))["at"], res["at"])
        self.assertEqual(health.run_due(self.db, self.s), "not due")  # hourly, not every tick
        c = TestClient(web.app)
        page = c.get("/runsheet").text
        self.assertIn("Pipeline:", page)
        self.assertIn("No new drafts in 24 hours", page)
        h = c.get("/health")
        self.assertEqual(h.status_code, 200)
        self.assertIn("Pipeline health", h.text)
        self.assertNotIn("Pipeline:</b>", h.text)  # no banner on the health page itself
        self.assertEqual(c.post("/health/run", follow_redirects=False).status_code, 303)


if __name__ == "__main__":
    unittest.main()
