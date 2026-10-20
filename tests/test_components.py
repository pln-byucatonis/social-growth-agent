"""Component tests: metrics/baselines, calendar coordination, media rendering, schema, experiments, dashboard."""
import shutil
import unittest
from pathlib import Path

from pln_agent.calendar_planner import plan_week
from pln_agent.db import render_ddl
from pln_agent.experiments import plan_experiments, recommendations
from pln_agent.media import probe_output, render_variant
from pln_agent.metrics import baseline, checkpoint_for, import_csv

from tests.helpers import CJ, PUTT, SLAP, TODAY, make

FIX = Path(__file__).parent / "fixtures" / "synthetic_metrics.csv"


class MetricsTests(unittest.TestCase):
    def setUp(self):
        self.db, self.s = make(packs=False)

    def test_unknown_stays_unknown(self):
        b = baseline(self.db, "carjitsu.tiktok.carjitsuchampionship")
        for k in ("followers", "posts_per_week", "median_reach_per_post", "median_watch_time_s", "profile_to_follow_conversion"):
            self.assertIsNone(b[k], k)

    def test_import_and_baseline(self):
        r = import_csv(self.db, FIX)
        self.assertEqual(r["imported"], 12)
        self.assertTrue(any("unknown" in s for s in r["skipped"]))  # empty value not imported as 0
        b = baseline(self.db, "world_putting_league.instagram.worldputtingleague")
        self.assertEqual(b["followers"], 12000)
        self.assertEqual(b["median_reach_per_post"], 5000)  # p2 uses 7d (6000) not 2h (100)
        self.assertEqual(b["profile_to_follow_conversion"], 0.1)
        self.assertEqual(b["posts_per_week"], 3.0)
        self.assertIsNone(baseline(self.db, "putt_tour.instagram.putttour")["median_saves_per_post"])
        rows = self.db.all("metrics", "metric='saved'")
        self.assertEqual(rows, [])

    def test_native_metric_names_preserved(self):
        import_csv(self.db, FIX)
        m = self.db.all("metrics", "metric='followers_count'")
        self.assertTrue(m and m[0]["canonical_metric"] == "followers")

    def test_checkpoints(self):
        from datetime import datetime, timedelta
        t = datetime(2026, 10, 1)
        self.assertEqual(checkpoint_for(t, t + timedelta(hours=3)), "2h")
        self.assertEqual(checkpoint_for(t, t + timedelta(hours=30)), "24h")
        self.assertEqual(checkpoint_for(t, t + timedelta(days=3)), "72h")
        self.assertEqual(checkpoint_for(t, t + timedelta(days=8)), "7d")


class CalendarTests(unittest.TestCase):
    def setUp(self):
        self.db, self.s = make()
        from datetime import date
        self.plan = plan_week(self.db, self.s, date(2026, 10, 21))

    def test_coordination_rules(self):
        placed = self.plan["placed"]
        self.assertTrue(placed)
        by_asset = {}
        for p in placed:
            by_asset.setdefault(p["asset_id"], []).append(p)
        for items in by_asset.values():
            canon = [p["slot"] for p in items if p["route_role"] in ("canonical", "legacy_inplace")]
            for p in items:
                if p["route_role"] == "pln_amplification" and canon:
                    self.assertGreaterEqual((p["slot"] - min(canon)).total_seconds(), 24 * 3600)
            for i, a in enumerate(items):
                for b in items[i + 1:]:
                    self.assertGreaterEqual(abs((a["slot"] - b["slot"]).total_seconds()), 6 * 3600)
                    self.assertFalse(a["platform"] == b["platform"] and a["slot"].date() == b["slot"].date())
        for p in placed:
            if p["platform"] == "linkedin":
                self.assertLess(p["slot"].weekday(), 5)
        self.assertLessEqual(sum(p["route_role"] == "legacy_bridge" for p in placed), 2)

    def test_only_ready_variants_and_supply_gap_reported(self):
        ids = {p["variant_id"] for p in self.plan["placed"]}
        self.assertFalse(any(v.startswith(SLAP) for v in ids))  # rights-blocked never planned
        self.assertIn("supply", self.plan["supply_note"].lower() + "supply")
        self.assertGreater(self.plan["open_slots"], 0)


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg not installed")
class MediaTests(unittest.TestCase):
    def setUp(self):
        self.db, self.s = make()

    def test_vertical_render_and_thumbnail(self):
        r = render_variant(self.db, f"{CJ}:carjitsu.tiktok.carjitsuchampionship", self.s)
        self.assertEqual(r["status"], "rendered", r["detail"])
        p = probe_output(r["path"])
        self.assertEqual((p["width"], p["height"]), (1080, 1920))
        self.assertTrue((Path(r["dir"]) / "thumbnail.jpg").exists())
        self.assertTrue((Path(r["dir"]) / "edl.json").exists())

    def test_crop_follows_the_action_not_the_frame_centre(self):
        import subprocess
        import tempfile
        from pln_agent.media import _reframe, subject_x
        src = str(Path(tempfile.mkdtemp()) / "left_action.mp4")
        # 16:9 master; a box bounces around the left fifth of the frame, the rest is static
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                        "color=c=gray:s=640x360:d=3:r=25", "-f", "lavfi", "-i", "color=c=white:s=60x60:d=3:r=25",
                        "-filter_complex", "[0][1]overlay=x='40+60*abs(sin(t*6))':y='120+80*abs(cos(t*5))'",
                        "-pix_fmt", "yuv420p", src], check=True)
        fx, why = subject_x(src, [{"source_in": 0.0, "source_out": 3.0}])
        self.assertLess(fx, 0.3, why)
        self.assertIn("motion", why)
        self.assertIn("in_w*", _reframe({"w": 1080, "h": 1920}, 640, 360, fx))
        self.assertIn("(in_w-1080)/2", _reframe({"w": 1080, "h": 1920}, 640, 360))  # default stays centred
        # a static shot falls back to the centre
        still = str(Path(tempfile.mkdtemp()) / "still.mp4")
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                        "color=c=gray:s=640x360:d=2:r=25", "-pix_fmt", "yuv420p", still], check=True)
        self.assertEqual(subject_x(still, [{"source_in": 0.0, "source_out": 2.0}])[0], 0.5)

    def test_missing_media_never_fakes_a_video(self):
        r = render_variant(self.db, f"{SLAP}:slapfight.tiktok.slapfightchampionshipusa", self.s)
        self.assertEqual(r["status"], "edl_only")
        self.assertFalse((Path(r["dir"]) / "video.mp4").exists())


class OpsSignalScoringTests(unittest.TestCase):
    def test_ops_energy_is_blended_into_source_strength(self):
        from pln_agent.scoring import _source
        seg = {"segments": [{"moment": "Submission", "source_timecode": "00:06.00"}]}
        asset = {"media_present": True, "moments": [{"label": "Submission", "intensity": 0.8}]}
        base, _ = _source(asset, {"edl": seg})
        hot, why = _source({**asset, "probe": {"ops_clip": {"energy_score": 1.0}}}, {"edl": seg})
        self.assertEqual(base, 0.8)
        self.assertAlmostEqual(hot, 0.84)
        self.assertIn("energy", why)


class MiscTests(unittest.TestCase):
    def test_postgres_ddl(self):
        ddl = render_ddl("postgres")
        self.assertIn("JSONB", ddl)
        self.assertIn("BIGSERIAL PRIMARY KEY", ddl)
        self.assertIn("idempotency_key TEXT NOT NULL UNIQUE", ddl)
        committed = (Path(__file__).parents[1] / "migrations" / "postgres" / "001_init.sql").read_text()
        self.assertEqual(committed.strip(), ddl.strip(), "regenerate: python -m pln_agent schema --dialect postgres")

    def test_experiments_max_two_per_platform_with_control(self):
        db, s = make()
        exps = plan_experiments(db, TODAY)
        by_p = {}
        for e in exps:
            if e.get("status") == "planned":
                by_p[e["platform"]] = by_p.get(e["platform"], 0) + 1
                self.assertTrue(e["control"] and e["treatment"])
        self.assertTrue(all(n <= 2 for n in by_p.values()))

    def test_recommendations_cite_evidence(self):
        db, s = make()
        recs = recommendations(db)
        self.assertTrue(any(r["type"] == "baseline" for r in recs))
        self.assertTrue(any(r["type"] == "rights_blocker" for r in recs))
        self.assertTrue(all(r["evidence"] for r in recs))


try:
    import fastapi  # noqa: F401
    import httpx  # noqa: F401
    HAVE_WEB = True
except ImportError:
    HAVE_WEB = False


@unittest.skipUnless(HAVE_WEB, "fastapi/httpx not installed")
class DashboardTests(unittest.TestCase):
    def test_pages_and_approval_flow(self):
        from fastapi.testclient import TestClient
        from pln_agent.web import app as webapp
        db, s = make()
        webapp._db = db
        webapp.settings = s
        c = TestClient(webapp.app)
        for u in ["/", "/queue", "/calendar?start=2026-10-21", "/accounts", "/migration", "/audit", "/healthz",
                  f"/variant/{CJ}:carjitsu.tiktok.carjitsuchampionship"]:
            self.assertEqual(c.get(u).status_code, 200, u)
        vid = f"{PUTT}:putt_tour.instagram.putttour"
        r = c.post(f"/variant/{vid}/approve", data={"reviewer": "a"}, follow_redirects=False)
        self.assertIn("1%2F2", r.headers["location"])
        c.post(f"/variant/{vid}/approve", data={"reviewer": "b"})
        self.assertEqual(db.get("variants", "variant_id", vid)["state"], "approved")
        r = c.post(f"/variant/{SLAP}:slapfight.x.slapfightusa/approve", data={"reviewer": "a"}, follow_redirects=False)
        self.assertIn("err=", r.headers["location"])
        c.post("/kill", data={"state": "on", "actor": "ops", "reason": "test"})
        self.assertIn("EMERGENCY STOP ON", c.get("/").text)


if __name__ == "__main__":
    unittest.main()
