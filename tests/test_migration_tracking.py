"""Audience Migration tracking from measured follower series and bridge posts."""
import unittest
from datetime import date, timedelta

from pln_agent import migration_tracking as mt

from tests.helpers import make

TODAY = date(2026, 10, 20)
WPL = "world_putting_league.instagram.worldputtingleague"
PUTT = "putt_tour.instagram.putttour"


def follower_series(db, acc, start, daily, days=40, bumps=()):
    v = start
    for i in range(days):
        d = TODAY - timedelta(days=days - 1 - i)
        v += daily + (30 if d in bumps else 0)
        db.insert("metrics", {"account_id": acc, "platform": "instagram", "metric": "followers", "canonical_metric": "followers",
                              "value": float(v), "checkpoint": "snapshot", "period_start": d.isoformat(), "period_end": d.isoformat(),
                              "source": "test", "captured_at": d.isoformat() + "T12:00:00+00:00", "metric_key": f"f:{acc}:{d}"})


class MigrationTrackingTests(unittest.TestCase):
    def setUp(self):
        self.db, self.s = make(packs=False)

    def _bridge(self, d, n):
        self.db.insert("posts", {"post_id": f"instagram:b{n}", "account_id": WPL, "brand_id": "world_putting_league",
                                 "platform": "instagram", "platform_post_id": f"b{n}", "published_at": d.isoformat() + "T15:00:00+00:00",
                                 "caption": "We're Putt Tour now — follow @putttour", "creative": {}})

    def test_decline_alert_bridge_association_and_recommendation(self):
        bridge_days = [TODAY - timedelta(days=k) for k in (33, 26, 19, 12, 5)]
        for i, d in enumerate(bridge_days):
            self._bridge(d, i)
        follower_series(self.db, WPL, 5000, -5)            # WPL shrinking ~140 / 28d (≈3%)
        follower_series(self.db, PUTT, 1000, 1, bumps={d + timedelta(days=1) for d in bridge_days})
        r = mt.track(self.db, TODAY)
        ig = next(p for p in r["platforms"] if p["platform"] == "instagram")
        self.assertEqual(ig["legacy"]["change_28d"], -140)
        self.assertTrue(any("WPL instagram down" in a["text"] for a in r["alerts"]))
        self.assertEqual(ig["bridge"]["bridge_posts"], 5)
        self.assertGreater(ig["bridge"]["median_gain_after_bridge"], ig["bridge"]["median_gain_other_days"])
        self.assertIn("association only", ig["bridge"]["note"])
        self.assertEqual(ig["recommendation"]["home"], "putt_tour")  # WPL is 4.3× but shrinking
        self.assertEqual(ig["recommendation"]["confidence"], "measured")
        self.assertTrue(any(a["text"].startswith("Putt Tour instagram has no posts") for a in r["alerts"]))  # Putt Tour has no posts

    def test_big_healthy_legacy_flags_the_configured_option(self):
        follower_series(self.db, WPL, 50000, 2)
        follower_series(self.db, PUTT, 1000, 1)
        r = mt.track(self.db, TODAY)
        ig = next(p for p in r["platforms"] if p["platform"] == "instagram")
        self.assertTrue(ig["recommendation"]["home"].startswith("legacy"))
        self.assertTrue(any("revisit the decision" in a["text"] for a in r["alerts"]))

    def test_no_data_is_audit_required_and_pages_render(self):
        r = mt.track(self.db, TODAY)
        self.assertTrue(all(p["recommendation"]["confidence"] == "audit required" for p in r["platforms"]))
        from fastapi.testclient import TestClient
        from pln_agent.web import app as web
        web._db = self.db
        page = TestClient(web.app).get("/migration")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Tracking (measured)", page.text)


if __name__ == "__main__":
    unittest.main()
