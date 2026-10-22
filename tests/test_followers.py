"""Daily follower snapshots for connected priority accounts."""
import unittest
from datetime import date
from unittest import mock

from pln_agent import insights
from pln_agent.adapters.base import HttpResponse

from tests.helpers import make


class FollowerTests(unittest.TestCase):
    def setUp(self):
        self.db, _ = make(packs=False)
        self.db.update("accounts", "account_id", "carjitsu.instagram.carjitsu_championship",
                       {"verification_status": "oauth_connected", "credential_ref": "vault:x", "stable_platform_id": "1789"})

    def test_snapshot_once_a_day(self):
        sent = []

        def transport(req):
            sent.append(req.url)
            return HttpResponse(200, {"followers_count": 12345})
        with mock.patch("pln_agent.oauth.access_token", lambda name: "tok"):
            r = insights.snapshot_followers(self.db, transport=transport, today=date(2026, 10, 22))
            insights.snapshot_followers(self.db, transport=transport, today=date(2026, 10, 22))  # same day: upsert, no dup
        self.assertEqual(r["saved"]["carjitsu.instagram.carjitsu_championship"], 12345)
        rows = self.db.all("metrics", "canonical_metric='followers' AND account_id='carjitsu.instagram.carjitsu_championship'")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["period_start"][:10], "2026-10-22")
        self.assertIn("fields=followers_count", sent[0])

    def test_due_runs_once(self):
        with mock.patch.object(insights, "snapshot_followers", return_value={"saved": {"a": 1}, "errors": {}}) as snap:
            insights.snapshot_followers_due(self.db)
            self.assertEqual(insights.snapshot_followers_due(self.db), "done today")
            self.assertEqual(snap.call_count, 1)


if __name__ == "__main__":
    unittest.main()
