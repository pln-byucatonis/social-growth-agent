"""Replay, punch-in and sound design on Reels (pln_agent/edit_fx.py)."""
import shutil
import unittest

from pln_agent import edit_fx
from tests.helpers import CJ, make

HAS_FFMPEG = bool(shutil.which("ffmpeg"))
SEGS = [{"source_in": 2.0, "source_out": 8.0}, {"source_in": 12.0, "source_out": 18.0}]


class PlanTests(unittest.TestCase):
    def fx(self, **own):
        return edit_fx.settings_for({"brand_id": "carjitsu", "treatment": {"edit_fx": own}})

    def test_replay_window_around_the_payoff_in_source_time(self):
        r = edit_fx.replay_plan(SEGS, 8.0, self.fx(), 60.0, 90)  # output 8.0 s = source 14.0 (second segment)
        self.assertEqual((r["src_in"], r["src_out"]), (12.8, 14.8))
        self.assertEqual(r["out_len"], 4.0)
        self.assertEqual(r["starts_at"], 12.0)

    def test_replay_skipped_when_off_too_long_or_no_payoff(self):
        self.assertIsNone(edit_fx.replay_plan(SEGS, 8.0, self.fx(replay=False), 60.0, 90))
        self.assertIsNone(edit_fx.replay_plan(SEGS, 8.0, self.fx(), 60.0, 14))  # 12 s edit + 4 s replay > 14 s limit
        self.assertIsNone(edit_fx.replay_plan(SEGS, None, self.fx(), 60.0, 90))

    def test_post_switches_and_licensed_music_only(self):
        f = self.fx(sfx=False, punch_in=False)
        self.assertFalse(f["sfx"]["enabled"])
        self.assertEqual(edit_fx.punch_in_filter(5.0, f, 1080, 1920), "")
        self.assertIn("zoompan", edit_fx.punch_in_filter(5.0, self.fx(), 1080, 1920))
        fx = self.fx()
        fx["music"]["beds"] = [{"path": "s3://b/no-license.mp3"}, {"path": "s3://b/ok.mp3", "license": "Library X, account 1", "brands": ["carjitsu"]}]
        self.assertEqual(edit_fx.music_bed("carjitsu", fx)["path"], "s3://b/ok.mp3")
        self.assertIsNone(edit_fx.music_bed("pln", fx))


@unittest.skipUnless(HAS_FFMPEG, "ffmpeg not installed")
class RenderTests(unittest.TestCase):
    VID = f"{CJ}:carjitsu.instagram.carjitsu_championship"

    def test_reel_gets_replay_punch_in_and_sound(self):
        from pln_agent.media import probe_output, render_variant
        db, s = make()
        v = db.get("variants", "variant_id", self.VID)
        edit = sum(x["source_out"] - x["source_in"] for x in v["edl"]["segments"])
        r = render_variant(db, self.VID, s)
        self.assertEqual(r["status"], "rendered", r["detail"])
        for bit in ("punch-in on the payoff", "replay", "impact sound"):
            self.assertIn(bit, r["detail"])
        p = probe_output(r["path"])
        self.assertAlmostEqual(p["duration_s"], edit + 4.0, delta=0.4)
        self.assertTrue(p["has_audio"])
        self.assertEqual((p["width"], p["height"]), (1080, 1920))

    def test_switched_off_renders_the_plain_edit(self):
        from pln_agent.media import probe_output, render_variant
        db, s = make()
        v = db.get("variants", "variant_id", self.VID)
        t = dict(v.get("treatment") or {})
        t["edit_fx"] = {"replay": False, "punch_in": False, "sfx": False, "music": False}
        db.update("variants", "variant_id", self.VID, {"treatment": t})
        edit = sum(x["source_out"] - x["source_in"] for x in v["edl"]["segments"])
        r = render_variant(db, self.VID, s)
        self.assertEqual(r["status"], "rendered", r["detail"])
        self.assertNotIn("replay", r["detail"])
        self.assertAlmostEqual(probe_output(r["path"])["duration_s"], edit, delta=0.4)

    def test_stacked_layout_replay(self):
        from pln_agent.media import probe_output, render_variant
        from unittest import mock
        db, s = make()
        with mock.patch("pln_agent.art_director.crop_cuts_graphics", lambda *a, **k: (True, "score bug")):
            r = render_variant(db, self.VID, s)
        self.assertEqual(r["status"], "rendered", r["detail"])
        self.assertIn("stacked", r["detail"])
        self.assertIn("replay", r["detail"])
        self.assertEqual((probe_output(r["path"])["width"],), (1080,))


class PageTests(unittest.TestCase):
    def test_switches_on_the_post_page(self):
        from fastapi.testclient import TestClient
        from pln_agent.web import app as web
        db, s = make()
        web._db, web.settings = db, s
        vid = f"{CJ}:carjitsu.instagram.carjitsu_championship"
        c = TestClient(web.app)
        self.assertIn("Replay &amp; sound", c.get(f"/variant/{vid}").text)
        r = c.post(f"/variant/{vid}/fx", data={"replay": "on", "sfx": "on"}, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        v = db.get("variants", "variant_id", vid)
        self.assertEqual(v["treatment"]["edit_fx"], {"replay": True, "punch_in": False, "sfx": True, "music": False})
        self.assertEqual(v["render_status"], "queued")


if __name__ == "__main__":
    unittest.main()
