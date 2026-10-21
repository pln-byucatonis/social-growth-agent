"""Designed on-screen text: knockout / pill / outline hooks, kinetic captions, payoff stamp, countdown, editing."""
import shutil
import tempfile
import unittest
from pathlib import Path

from pln_agent import overlays

from tests.helpers import CJ, PUTT, make


class OverlayDrawingTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def test_brand_styles(self):
        self.assertEqual(overlays.style_for("carjitsu")["hook"], "knockout")
        self.assertEqual(overlays.style_for("putt_tour")["stamp"], "ACE.")
        self.assertTrue(Path(overlays.style_for("pln")["font_path"]).exists())
        self.assertEqual(overlays.style_for("carjitsu", {"hook": "pill"})["hook"], "pill")  # a post can override
        self.assertEqual(overlays.style_id(overlays.style_for("putt_tour")), "pill+kinetic+stamp+countdown")

    def test_knockout_letters_are_transparent_holes(self):
        from PIL import Image
        s = overlays.style_for("carjitsu")
        bottom = overlays.render_hook("TWO SECONDS LEFT", s, 1080, 1920, self.dir / "k.png")
        img = Image.open(self.dir / "k.png")
        top = int(1920 * (overlays.SAFE_TOP + 0.03))
        band = [img.getpixel((x, y))[3] for y in range(top, bottom - 10, 6) for x in range(100, 980, 6)]
        self.assertGreater(sum(a > 200 for a in band), len(band) * 0.4)  # mostly solid band…
        self.assertGreater(sum(a < 40 for a in band), len(band) * 0.05)  # …with letter-shaped holes in it
        self.assertLess(bottom, 1920 * 0.5)

    def test_kinetic_phrases_time_each_word(self):
        ph = overlays.phrases([{"start": 1.0, "end": 3.0, "text": "one grip changes everything"}])
        self.assertEqual([p["words"] for p in ph], [["one", "grip", "changes"], ["everything"]])
        self.assertAlmostEqual(ph[0]["times"][1], 1.5)

    def test_build_layers_and_graph(self):
        v = {"brand_id": "putt_tour", "onscreen_text": [{"start": 0, "end": 3, "text": "WAIT FOR IT", "position": "top"},
                                                         {"start": 3, "end": 12, "text": "Putt Tour", "position": "lower_left_small"}]}
        b = overlays.build(v, [{"start": 0.5, "end": 2.0, "text": "here it comes"}], 12.0, 1080, 1920, self.dir, payoff_at=6.0)
        self.assertEqual(b["style_id"], "pill+kinetic+stamp+countdown")
        self.assertTrue(any("stamp" in str(L["png"]) for L in b["layers"]))
        self.assertIsNotNone(b["countdown"])
        ins, parts, final = overlays.ffmpeg_graph("base", 1, b["layers"], b["countdown"], 1080, 1920)
        self.assertEqual(final, "olcd")
        self.assertTrue(parts[0].startswith("[base][1:v]overlay"))


@unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg")
class OverlayRenderTests(unittest.TestCase):
    def test_render_records_style_and_edit_rerenders(self):
        from fastapi.testclient import TestClient
        from pln_agent.media import render_variant
        from pln_agent.web import app as web
        db, s = make(packs=True)
        v = [x for x in db.all("variants", "asset_id=?", [CJ]) if x["platform"] == "instagram"][0]
        r = render_variant(db, v["variant_id"], s)
        self.assertEqual(r["status"], "rendered")
        self.assertEqual(r["qc"]["overlay_style"], "knockout+kinetic+stamp")
        web._db, web.BACKGROUND = db, False
        self.addCleanup(setattr, web, "BACKGROUND", True)
        self.addCleanup(setattr, web, "settings", web.settings)
        web.settings = s
        c = TestClient(web.app)
        self.assertIn("On-screen text", c.get(f"/variant/{v['variant_id']}").text)
        res = c.post(f"/variant/{v['variant_id']}/overlay", data={"hook_text": "2 SECONDS LEFT.", "hook": "pill", "captions": "classic",
                                                                  "stamp": "", "actor": "bill"}, follow_redirects=False)
        self.assertEqual(res.status_code, 303)
        after = db.get("variants", "variant_id", v["variant_id"])
        self.assertEqual(after["onscreen_text"][0]["text"], "2 SECONDS LEFT.")
        self.assertEqual(after["render_status"], "rendered")
        self.assertEqual(after["render_qc"]["overlay_style"], "pill+classic")


class RenderQueueTests(unittest.TestCase):
    def test_queued_render_needs_the_runner(self):
        from pln_agent import team_tasks
        db, s = make(packs=True)
        self.assertFalse(team_tasks.needs_compute(db))
        v = db.all("variants", "asset_id=?", [PUTT])[0]
        db.update("variants", "variant_id", v["variant_id"], {"render_status": "queued"})
        self.assertTrue(team_tasks.needs_compute(db))


if __name__ == "__main__":
    unittest.main()
