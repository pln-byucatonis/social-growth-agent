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
        self.assertEqual(overlays.style_for("carjitsu")["hook"], "pill")  # knockout is unreadable over busy in-car footage
        self.assertIn("ACE.", overlays.words_for(overlays.style_for("putt_tour"))["payoff"])
        self.assertTrue(Path(overlays.style_for("pln")["font_path"]).exists())
        self.assertEqual(overlays.style_for("carjitsu", {"hook": "pill"})["hook"], "pill")  # a post can override
        self.assertEqual(overlays.style_id(overlays.style_for("putt_tour")), "pill+kinetic+stamp+reaction+countdown")

    def test_knockout_letters_are_transparent_holes(self):
        from PIL import Image
        s = overlays.style_for("carjitsu", {"hook": "knockout"})
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
        v = {"variant_id": "x:1", "brand_id": "putt_tour",
             "onscreen_text": [{"start": 0, "end": 3, "text": "Putt Tour: Putt drops", "position": "top"},
                               {"start": 3, "end": 12, "text": "Putt Tour", "position": "lower_left_small"}]}
        beats = [{"kind": "payoff", "t": 6.0, "intensity": 0.9}, {"kind": "reaction", "t": 8.0, "intensity": 0.5}]
        b = overlays.build(v, [{"start": 0.5, "end": 2.0, "text": "here it comes"}], 12.0, 1080, 1920, self.dir, 6.0, beats)
        self.assertEqual(b["style_id"], "pill+kinetic+stamp+reaction+countdown")
        bank = overlays.words_for(b["style"])
        self.assertIn(b["words"]["hook"], bank["hook"])  # brand-name filler replaced by a bold hook
        self.assertIn(b["words"]["payoff"], bank["payoff"])
        self.assertIn(b["words"]["reaction"], bank["reaction"])
        self.assertFalse(any("tag" in str(L["png"]) for L in b["layers"]))  # no brand chip
        self.assertTrue(any("stamp" in str(L["png"]) for L in b["layers"]))
        self.assertTrue(any("reaction" in str(L["png"]) for L in b["layers"]))
        # a hook someone typed is kept as written; "none" turns the payoff word off
        v2 = {**v, "treatment": {"overlay": {"hook_custom": True, "stamp": "none"}},
              "onscreen_text": [{"start": 0, "end": 3, "text": "Putt Tour's best ace", "position": "top"}]}
        b2 = overlays.build(v2, [], 12.0, 1080, 1920, self.dir, 6.0, beats)
        self.assertEqual(b2["words"]["hook"], "Putt Tour's best ace")
        self.assertNotIn("payoff", b2["words"])
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
        self.assertEqual(r["qc"]["overlay_style"], "pill+kinetic+stamp+reaction")
        self.assertTrue(r["qc"]["overlay_words"].get("payoff"))
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
        self.assertEqual(after["render_qc"]["overlay_style"], "pill+classic+stamp+reaction")
        self.assertEqual(after["render_qc"]["overlay_words"]["hook"], "2 SECONDS LEFT.")


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


@unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg")
class ArtDirectorTests(unittest.TestCase):
    def test_text_moves_off_a_score_bug(self):
        import subprocess
        from pln_agent import art_director
        d = Path(tempfile.mkdtemp())
        clip = d / "bug.mp4"
        # moving footage with a static "score bug" across the top zone
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "mandelbrot=size=360x640:rate=30",
                        "-t", "6", "-vf", "drawbox=x=0:y=110:w=360:h=90:color=black:t=fill,"
                        "drawgrid=x=0:y=110:w=24:h=18:t=3:c=white", str(clip)], check=True)
        a = art_director.analyze(str(clip), 6.0, [3.0], vision=False)
        self.assertTrue(a["ok"])
        self.assertGreaterEqual(art_director.zone_report(a, "top")["graphic"], art_director.GRAPHIC_COVER)
        zone, why = art_director.choose(a, ["top", "lower"], 1.0)
        self.assertEqual(zone, "lower")
        self.assertIn("graphic", why)
        v = {"variant_id": "x", "brand_id": "putt_tour", "onscreen_text": [{"start": 0, "end": 2, "text": "WAIT FOR IT", "position": "top"}]}
        b = overlays.build(v, [], 6.0, 1080, 1920, d, 3.0, [{"kind": "payoff", "t": 3.0, "intensity": 1}], a)
        self.assertEqual(next(p for p in b["plan"] if p["what"].startswith("hook"))["zone"], "lower")

    def test_vision_spots_a_title_card_the_measurements_miss(self):
        import json as _json
        import subprocess
        from pln_agent import art_director
        d = Path(tempfile.mkdtemp())
        clip = d / "busy.mp4"
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "mandelbrot=size=360x640:rate=30",
                        "-t", "6", str(clip)], check=True)
        calls = []

        def llm(system, messages, max_tokens):
            calls.append(messages)
            return _json.dumps({"frames": [{"t": 3.0, "zones": ["middle"], "what": "HOLE IN ONE title card"},
                                           {"t": 3.65, "zones": ["middle"], "what": "title card"}]})
        a = art_director.analyze(str(clip), 6.0, [3.0, 3.65, 4.3], llm=llm)
        self.assertTrue(calls and any(c.get("type") == "image" for c in calls[0][0]["content"]))
        zone, why = art_director.choose(a, ["middle", "top"], (3.0, 4.3))
        self.assertEqual(zone, "top")
        self.assertIn("frames show a graphic", why)
        # the payoff word is dropped rather than competing with the broadcast's own title card
        v = {"variant_id": "z", "brand_id": "putt_tour", "onscreen_text": [{"start": 0, "end": 2, "text": "WAIT FOR IT", "position": "top"}]}
        built = overlays.build(v, [], 6.0, 1080, 1920, d, 3.0, [{"kind": "payoff", "t": 3.0, "intensity": 1}], a)
        self.assertNotIn("payoff", built["words"])
        self.assertTrue(any("already has its own graphic" in p["why"] for p in built["plan"]))
        # no model access: falls back to measurements without failing
        b = art_director.analyze(str(clip), 6.0, [3.0], llm=lambda *x: (_ for _ in ()).throw(RuntimeError("no access")))
        self.assertEqual(b["seen"], {})

    def test_locked_off_camera_is_not_a_graphic(self):
        import subprocess
        from pln_agent import art_director
        d = Path(tempfile.mkdtemp())
        clip = d / "still.mp4"
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=360x640:rate=30",
                        "-t", "4", "-vf", "drawbox=x=0:y=0:w=360:h=30:color=black:t=fill", str(clip)], check=True)
        a = art_director.analyze(str(clip), 4.0, vision=False)
        self.assertEqual(art_director.choose(a, ["top", "lower"], 1.0)[0], "top")

    def test_animation_and_timing_rules(self):
        d = Path(tempfile.mkdtemp())
        v = {"variant_id": "y", "brand_id": "carjitsu", "onscreen_text": [{"start": 0, "end": 2, "text": "NO WAY OUT", "position": "top"}]}
        # payoff 0.3 s before the end: too late for a word
        b = overlays.build(v, [], 10.0, 1080, 1920, d, 9.7, [{"kind": "finish", "t": 9.7, "intensity": 1}])
        self.assertNotIn("payoff", b["words"])
        self.assertTrue(any("too close to the end" in p["why"] for p in b["plan"]))
        # entrance frames precede the held layer, and a fade closes it
        b = overlays.build(v, [], 10.0, 1080, 1920, d, 5.0, [{"kind": "finish", "t": 5.0, "intensity": 1}])
        names = [L["png"].name for L in b["layers"]]
        self.assertTrue(any("_in0" in n for n in names) and any("_out1" in n for n in names))
        # captions step aside while the payoff word is up
        cues = [{"start": 4.5, "end": 6.5, "text": "one grip changes everything right here"}]
        b = overlays.build(v, cues, 10.0, 1080, 1920, d, 5.0, [{"kind": "finish", "t": 5.0, "intensity": 1}])
        caps = [L for L in b["layers"] if "ov_cap_" in L["png"].name]
        self.assertTrue(all(L["end"] <= 5.0 or L["start"] >= 6.3 for L in caps))
