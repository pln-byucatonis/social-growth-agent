"""YouTube collector against a fake Data API v3 + Analytics API v2 (no network, no credentials)."""
import time
import unittest
import urllib.parse
from datetime import datetime, timedelta, timezone

from pln_agent import audience, oauth
from pln_agent.adapters.base import HttpResponse
from pln_agent.insights import collect_youtube, iso_duration_s

from tests.helpers import make

ACC = "carjitsu.youtube.carjitsuchampionship6540"
NOW = datetime.now(timezone.utc)
D = lambda days: (NOW - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


class FakeYouTube:
    def __init__(self, forbidden=()):
        self.calls, self.forbidden = [], set(forbidden)

    def __call__(self, req):
        u = urllib.parse.urlparse(req.url)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        self.calls.append((u.path, q))
        assert req.headers["Authorization"] == "Bearer YT-TOKEN"
        if q.get("id") in self.forbidden:
            return HttpResponse(403, {"error": {"message": "forbidden"}})
        if u.path.endswith("/channels"):
            return HttpResponse(200, {"items": [{"id": q["id"], "statistics": {"subscriberCount": "1500", "viewCount": "900000",
                                                                              "videoCount": "120", "hiddenSubscriberCount": False},
                                                 "contentDetails": {"relatedPlaylists": {"uploads": "UU123"}}}]})
        if u.path.endswith("/playlistItems"):
            return HttpResponse(200, {"items": [{"contentDetails": {"videoId": "short1", "videoPublishedAt": D(10)}},
                                                {"contentDetails": {"videoId": "long1", "videoPublishedAt": D(1)}},
                                                {"contentDetails": {"videoId": "old1", "videoPublishedAt": D(400)}}]})
        if u.path.endswith("/videos"):
            assert "old1" not in q["id"]  # outside the lookback window
            return HttpResponse(200, {"items": [
                {"id": "short1", "snippet": {"publishedAt": D(10), "title": "Seatbelt choke with 2 seconds left?"},
                 "contentDetails": {"duration": "PT45S"}, "statistics": {"viewCount": "20000"}},
                {"id": "long1", "snippet": {"publishedAt": D(1), "title": "Full card: CarJitsu fall event"},
                 "contentDetails": {"duration": "PT12M3S"}, "statistics": {"viewCount": "800", "likeCount": "40", "commentCount": "5"}}]})
        if u.path.endswith("/reports"):
            dims, metrics = q["dimensions"], q["metrics"].split(",")
            if dims == "day":
                return HttpResponse(200, {"columnHeaders": [{"name": "day"}] + [{"name": m} for m in metrics],
                                          "rows": [[(NOW - timedelta(days=i)).date().isoformat()] + [10 * (i + 1)] * len(metrics)
                                                   for i in range(3)]})
            if dims == "subscribedStatus":
                return HttpResponse(200, {"columnHeaders": [{"name": "subscribedStatus"}, {"name": "views"}],
                                          "rows": [["SUBSCRIBED", 400], ["UNSUBSCRIBED", 1600]]})
            if dims == "video":  # analytics lags: only the older Short has a row yet
                vals = {"views": 20000, "estimatedMinutesWatched": 250, "averageViewDuration": 31, "averageViewPercentage": 69,
                        "subscribersGained": 60, "shares": 120, "likes": 900, "comments": 40}
                return HttpResponse(200, {"columnHeaders": [{"name": "video"}] + [{"name": m} for m in metrics],
                                          "rows": [["short1"] + [vals[m] for m in metrics]]})
        return HttpResponse(404, {"error": {"message": "unexpected " + req.url}})


class YouTubeCollectorTests(unittest.TestCase):
    def setUp(self):
        self.db, self.s = make(packs=False)
        self.store = oauth.MemorySecretStore()
        oauth.set_store(self.store)
        self.addCleanup(oauth.set_store, None)
        name = f"pln/growth-agent/tokens/{ACC}"
        self.store.put(name, {"provider": "google", "access_token": "YT-TOKEN", "refresh_token": "R",
                              "expires_at": int(time.time()) + 3600})
        self.db.update("accounts", "account_id", ACC, {"verification_status": "oauth_connected", "credential_ref": f"vault:{name}",
                                                       "stable_platform_id": "UC123"})

    def test_iso_durations(self):
        self.assertEqual(iso_duration_s("PT45S"), 45)
        self.assertEqual(iso_duration_s("PT12M3S"), 723)
        self.assertEqual(iso_duration_s("PT1H"), 3600)
        self.assertIsNone(iso_duration_s(""))

    def test_collects_channel_videos_and_audience_split_idempotently(self):
        fake = FakeYouTube()
        r = collect_youtube(self.db, self.s.timezone, transport=fake)
        self.assertEqual(r["errors"], {})
        self.assertEqual(r["collected"][0]["videos"], 2)
        n = self.db.one("SELECT COUNT(*) AS n FROM metrics WHERE source='platform_api'")["n"]
        collect_youtube(self.db, self.s.timezone, transport=fake)
        self.assertEqual(self.db.one("SELECT COUNT(*) AS n FROM metrics WHERE source='platform_api'")["n"], n)
        posts = {p["platform_post_id"]: p for p in self.db.all("posts", "account_id=?", [ACC])}
        self.assertEqual(posts["short1"]["post_type"], "short")
        self.assertEqual(posts["long1"]["post_type"], "video")
        self.assertEqual(posts["short1"]["origin"], "platform_api")
        self.assertEqual(posts["short1"]["creative"]["duration_bucket"], "31–60s")
        self.assertEqual(posts["short1"]["creative"]["asks_question"], "yes")
        self.assertTrue(posts["short1"]["permalink"].startswith("https://www.youtube.com/shorts/"))
        # the long upload hasn't reached Analytics yet: public counters only, no invented watch time
        lm = {r["canonical_metric"] for r in self.db.all("metrics", "platform_post_id='long1'")}
        self.assertEqual(lm, {"views", "likes", "comments"})

    def test_kpis_become_measured(self):
        collect_youtube(self.db, self.s.timezone, transport=FakeYouTube())
        k = audience.account_kpis(self.db, ACC, days=28, end=NOW.date())["kpis"]
        self.assertEqual(k["non_follower_reach"]["basis"], "measured")
        self.assertAlmostEqual(k["non_follower_reach"]["value"], 0.8)
        self.assertEqual(k["follows_per_1k_views"]["basis"], "measured")
        self.assertAlmostEqual(k["follows_per_1k_views"]["value"], 3.0)  # 60 subs / 20,000 views
        self.assertEqual(k["avg_watch_time_s"]["basis"], "measured")
        self.assertEqual(k["avg_watch_time_s"]["value"], 31)

    def test_one_failing_channel_does_not_stop_the_rest(self):
        other = "pln.youtube.proleaguenetwork"
        name = f"pln/growth-agent/tokens/{other}"
        self.store.put(name, {"provider": "google", "access_token": "YT-TOKEN", "expires_at": int(time.time()) + 3600})
        self.db.update("accounts", "account_id", other, {"verification_status": "oauth_connected",
                                                         "credential_ref": f"vault:{name}", "stable_platform_id": "UCX"})
        r = collect_youtube(self.db, self.s.timezone, transport=FakeYouTube(forbidden={"UCX"}))
        self.assertIn(other, r["errors"])
        self.assertEqual([c["account_id"] for c in r["collected"]], [ACC])

    def test_unconnected_channels_are_skipped(self):
        self.db.update("accounts", "account_id", ACC, {"verification_status": "ownership_confirmed"})
        r = collect_youtube(self.db, self.s.timezone, transport=FakeYouTube())
        self.assertEqual(r["collected"], [])


if __name__ == "__main__":
    unittest.main()
