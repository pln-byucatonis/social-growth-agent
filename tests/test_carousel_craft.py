"""Carousels the editor makes (pln_agent/carousel_craft.py): format, frames, crops, lines, sharpness and the critic."""
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from pln_agent import carousel, carousel_craft as craft, team
from tests.helpers import make
from tests.test_carousel import REEL, _clip

HAS_FFMPEG = bool(shutil.which("ffmpeg"))
GOOD = {k: 4 for k in ("cover_stops", "swipe_pull", "sharp", "text_reads", "fresh", "format_fit")}


def fbf(n=6, cover="Stuck Under The Dash"):
    return {"format": "frame_by_frame", "why": "a reversal in clear steps",
            "slides": [{"t": 0.2 + 0.6 * i, "focus": {"x": 0.6, "y": 0.4}, "text": cover if i == 0 else f"step {'abcdefg'[i]} happens",
                        "role": "cover" if i == 0 else "step"} for i in range(n)], "end_card": ""}


class Model:
    def __init__(self, plans, verdicts):
        self.plans, self.verdicts, self.prompts = list(plans), list(verdicts), []

    def __call__(self, system, messages, max_tokens):
        text = " ".join(c.get("text", "") for c in messages[0]["content"] if c.get("type") == "text")
        self.prompts.append(text)
        if "Choose ONE format" in text or "couldn't be used" in text:
            return json.dumps(self.plans.pop(0))
        if "finished slides" in text:
            return json.dumps(self.verdicts.pop(0))
        return '{"frames": []}'


class CheckTests(unittest.TestCase):
    def test_plan_rules(self):
        p = craft.check(fbf(), 0, 4, set())
        self.assertEqual((p["format"], len(p["slides"])), ("frame_by_frame", 6))
        with self.assertRaises(craft.CraftError):
            craft.check(fbf(3), 0, 4, set())  # frame by frame needs 5+
        with self.assertRaises(craft.CraftError):
            craft.check(fbf(cover="Down In 3 Seconds"), 0, 4, set())  # a number on the cover
        with self.assertRaises(craft.CraftError):
            craft.check(fbf(cover="No Way Out"), 0, 4, {"no way out"})  # a line the account used lately
        q = {"format": "question_first", "slides": fbf(3)["slides"], "end_card": ""}
        with self.assertRaises(craft.CraftError):
            craft.check(q, 0, 4, set())  # question first needs its closing question
        dup = fbf(6)
        dup["slides"][2]["t"] = dup["slides"][1]["t"] + 0.1
        self.assertEqual(len(craft.check(dup, 0, 4, set())["slides"]), 5)  # the same frame twice: dropped
        placed = fbf(6)
        placed["slides"][0]["text_at"] = "bottom"
        placed["slides"][2]["text_at"] = "top"
        got = craft.check(placed, 0, 4, set())["slides"]
        self.assertEqual([x["text_at"] for x in got], ["top", "bottom", "top", "bottom", "bottom", "bottom"])  # cover always top
        self.assertEqual(craft.check({"format": "explainer", "slides": fbf(4)["slides"], "end_card": ""}, 0, 4, set())["end_card"],
                         "Follow for the next one")

    def test_critic_bar(self):
        self.assertEqual(craft.judge({"scores": GOOD, "verdict": "pass"})["verdict"], "pass")
        self.assertEqual(craft.judge({"scores": {**GOOD, "cover_stops": 3}, "verdict": "pass"})["verdict"], "redo")
        self.assertEqual(craft.judge({"scores": {**GOOD, "sharp": 2}, "verdict": "pass"})["verdict"], "redo")

    def test_crop_is_full_bleed_on_the_action(self):
        from PIL import Image
        f = Path(tempfile.mkdtemp()) / "f.png"
        im = Image.new("RGB", (1920, 1080), (0, 0, 0))
        im.paste((255, 0, 0), (1500, 0, 1920, 1080))  # the action on the right
        im.save(f)
        out = craft.crop45(f, {"x": 0.9, "y": 0.5})
        self.assertEqual(out.size, (1080, 1350))
        self.assertGreater(out.getpixel((1000, 600))[0], 200)  # cropped toward the action, no blurred fill

    def test_dark_frames_lifted_and_duplicates_spotted(self):
        from PIL import Image, ImageStat
        dark = Image.effect_noise((1080, 1350), 20).convert("RGB").point(lambda v: v // 4)
        self.assertGreater(ImageStat.Stat(craft.lift(dark).convert("L")).mean[0], ImageStat.Stat(dark.convert("L")).mean[0] + 15)
        bright = Image.new("RGB", (1080, 1350), (200, 200, 200))
        self.assertEqual(craft.lift(bright).getpixel((5, 5)), (200, 200, 200))
        self.assertTrue(craft.too_alike(bright, bright))
        self.assertFalse(craft.too_alike(bright, Image.new("RGB", (1080, 1350), (20, 20, 20))))

    def test_bottom_text_stays_on_the_slide(self):
        from PIL import Image
        from pln_agent import overlays
        out = Path(tempfile.mkdtemp()) / "t.png"
        overlays.render_hook("a line long enough to wrap onto two lines here", {**overlays.style_for("carjitsu"), "hook": "outline"},
                             1080, 1350, out, "bottom")
        box = Image.open(out).getbbox()
        self.assertLessEqual(box[3], int(1350 * overlays.BOTTOM_EDGE) + 20)  # ends above the edge, not off it

    def test_sharpest_frame_wins(self):
        from PIL import Image, ImageFilter
        d = Path(tempfile.mkdtemp())
        sharp = Image.effect_noise((640, 360), 90).convert("RGB")

        def grab(src, t, out):
            img = sharp if abs(t - 1.25) < 0.01 else sharp.filter(ImageFilter.GaussianBlur(6))
            img.save(out)
            return out
        t, _ = craft.sharpest("x", 1.0, d, grab)
        self.assertEqual(t, 1.25)


@unittest.skipUnless(HAS_FFMPEG, "ffmpeg not installed")
class FlowTests(unittest.TestCase):
    def setUp(self):
        self.old = team._llm
        self.db, self.s = make(packs=True)
        reel = self.db.get("variants", "variant_id", REEL)
        self.db.update("assets", "asset_id", reel["asset_id"], {"media_path": _clip(), "media_present": True,
                                                               "width": 1920, "height": 1080, "duration_s": 4.0})
        self.db.update("variants", "variant_id", REEL, {"treatment": {**(reel.get("treatment") or {}), "editor": {
            "what_happens": "he flips it from the bottom", "shots": [{"in": 0.0, "out": 4.0, "role": "payoff"}]}}})
        self.vid = carousel.make(self.db, REEL, "carousel", "bill")["variant_id"]

    def tearDown(self):
        team._llm = self.old

    def test_passes_with_full_bleed_slides_and_the_plan_on_the_post(self):
        from PIL import Image
        m = team._llm = Model([fbf()], [{"scores": GOOD, "verdict": "pass"}])
        r = carousel.render_slides(self.db, self.vid, self.s)
        self.assertEqual(r["status"], "rendered", r)
        self.assertIn(len(r["slides"]), (5, 6))  # a near-duplicate frame may be dropped (min 5)
        for sl in r["slides"]:
            with Image.open(sl["file"]) as im:
                self.assertEqual((im.size, im.format), ((1080, 1350), "JPEG"))
        v = self.db.get("variants", "variant_id", self.vid)
        self.assertEqual(v["treatment"]["carousel"]["format"], "frame_by_frame")
        self.assertEqual(v["state"], "in_review")
        self.assertIn("he flips it from the bottom", m.prompts[0])
        self.assertIn("TAP.", m.prompts[0])  # stock words are off limits

    def test_redo_then_needs_fix_with_the_reasons(self):
        bad = {"scores": {**GOOD, "sharp": 1}, "verdict": "redo", "problems": ["slide 3 is motion blur"], "fix": "use 1.4s"}
        m = team._llm = Model([fbf(), fbf()], [bad, bad])
        carousel.render_slides(self.db, self.vid, self.s)
        self.assertTrue(any("slide 3 is motion blur" in p for p in m.prompts[2:]))  # the redo heard the critic
        v = self.db.get("variants", "variant_id", self.vid)
        self.assertEqual(v["state"], "needs_fix")
        self.assertTrue(any("motion blur" in e for e in v["validation"]["errors"]))
        self.assertEqual(len(v["treatment"]["carousel"]["history"]), 2)

    def test_out_of_allowance_waits(self):
        from pln_agent.editor import Throttled

        def down(*a, **k):
            raise Throttled("Too many tokens per day")
        team._llm = down
        r = carousel.render_slides(self.db, self.vid, self.s)
        self.assertEqual(r["status"], "deferred")
        self.assertEqual(self.db.get("variants", "variant_id", self.vid)["render_status"], "queued")


if __name__ == "__main__":
    unittest.main()
