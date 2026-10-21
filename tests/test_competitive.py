"""Competitive Intelligence against a fake Business Discovery endpoint."""
import unittest
import urllib.parse
from datetime import datetime, timedelta, timezone

from pln_agent import competitive as ci
from pln_agent import oauth
from pln_agent.adapters.base import HttpResponse

from tests.helpers import make

IG = "carjitsu.instagram.carjitsu_championship"
NOW = datetime.now(timezone.utc)
T = lambda days: (NOW - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S+0000")


def media(n, likes, every_days=0.5):
    return [{"id": f"m{i}", "caption": ("Who wins this? 👀\nmore" if i == 0 else f"post {i}"), "like_count": likes * (5 if i == 0 else 1),
             "comments_count": 10, "timestamp": T(i * every_days + 0.1), "media_type": "VIDEO", "media_product_type": "REELS",
             "permalink": f"https://www.instagram.com/p/m{i}/"} for i in range(n)]


class FakeDiscovery:
    def __init__(self, followers=1_000_000):
        self.followers = followers

    def __call__(self, req):
        q = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlparse(req.url).query).items()}
        assert q["access_token"] == "PAGE-TOKEN" and "/1784?" in req.url
        h = q["fields"].split("username(")[1].split(")")[0]
        if h == "nitrocircus":
            return HttpResponse(400, {"error": {"message": "Invalid user id"}})
        return HttpResponse(200, {"business_discovery": {"username": h, "name": h.title(), "followers_count": self.followers,
                                                         "media_count": 5000, "media": {"data": media(20, 20000)}}})


class CompetitiveTests(unittest.TestCase):
    def setUp(self):
        self.db, self.s = make(packs=False)
        store = oauth.MemorySecretStore()
        oauth.set_store(store)
        self.addCleanup(oauth.set_store, None)
        store.put("pln/growth-agent/tokens/x", {"provider": "meta", "access_token": "PAGE-TOKEN"})
        self.db.update("accounts", "account_id", IG, {"verification_status": "oauth_connected", "credential_ref": "vault:pln/growth-agent/tokens/x",
                                                      "stable_platform_id": "1784"})

    def test_profile_is_the_same_calculation_for_anyone(self):
        p = ci.profile(media(20, 20000), 1_000_000, NOW)
        self.assertAlmostEqual(p["posts_per_week"], 14.6)  # 20 posts over 9.6 days
        self.assertEqual(p["reels_share"], 1.0)
        self.assertAlmostEqual(p["median_er"], 0.02001)
        self.assertEqual(p["standouts"][0]["hook"], "Who wins this?")
        self.assertGreater(p["standouts"][0]["x_median"], 4)

    def test_snapshot_writes_benchmark_knowledge_growth_and_gaps(self):
        r = ci.snapshot(self.db, FakeDiscovery(), NOW)
        self.assertIn("nitrocircus", r["errors"])
        self.assertEqual(len(r["captured"]), len(ci.config()["benchmarks"]) - 1)
        k = self.db.get("knowledge", "entry_id", "benchmark-instagram-powerslap")
        self.assertEqual((k["level"], k["topic"]), ("external_research", "benchmark"))
        self.assertIn("not evidence for ours", k["statement"])
        ci.snapshot(self.db, FakeDiscovery(1_007_000), NOW + timedelta(days=7))
        pow_ = next(x for x in ci.latest(self.db) if x["handle"] == "powerslap")
        self.assertEqual(pow_["profile"]["growth"]["per_week"], 7000)
        # our CarJitsu IG posts ~2/week of feed posts → volume and Reels gaps
        from pln_agent.creative_db import upsert_ingested_post
        acc = self.db.get("accounts", "account_id", IG)
        for i in range(8):
            upsert_ingested_post(self.db, acc, {"platform_post_id": f"o{i}", "published_at": T(i * 3.5 + 1), "text": "x", "post_type": "feed"}, self.s.timezone)
        g = {x["account_id"]: x for x in ci.gaps(self.db, NOW + timedelta(days=7))}
        self.assertIn(IG, g)
        self.assertTrue(any("posts" in n and "/week" in n for n in g[IG]["notes"]))
        self.assertTrue(any("Reels" in n for n in g[IG]["notes"]))

    def test_knowledge_review_does_not_fetch_benchmark_profiles(self):
        from pln_agent.knowledge import review_sources
        ci.snapshot(self.db, FakeDiscovery(), NOW)
        fetched = []
        review_sources(self.db, fetch=lambda u: (fetched.append(u), (200, "x"))[1], force=True)
        self.assertFalse(any("instagram.com" in u for u in fetched))

    def test_page_renders(self):
        from fastapi.testclient import TestClient
        from pln_agent.web import app as web
        ci.snapshot(self.db, FakeDiscovery(), NOW)
        web._db = self.db
        r = TestClient(web.app).get("/competitors?brand=carjitsu")
        self.assertEqual(r.status_code, 200)
        self.assertIn("@powerslap", r.text)


if __name__ == "__main__":
    unittest.main()
