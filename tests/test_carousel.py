"""Carousels and Stories: made from a Reel, slides drawn and checked, public links per slide, Instagram's flows."""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from pln_agent import carousel
from pln_agent.adapters import get_adapter
from pln_agent.adapters.base import HttpResponse
from pln_agent.public_media import publish_render
from pln_agent.workflow import approve

from tests.helpers import CJ, PUTT, make

REEL = f"{PUTT}:putt_tour.instagram.putttour"
HAS_FFMPEG = bool(shutil.which("ffmpeg"))


def _clip(w=1920, h=1080, secs=4) -> str:
    out = Path(tempfile.mkdtemp(prefix="pln-car-")) / "src.mp4"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    f"testsrc2=size={w}x{h}:rate=25:duration={secs}", "-pix_fmt", "yuv420p", str(out)], check=True)
    return str(out)


class MakeTests(unittest.TestCase):
    def setUp(self):
        self.db, self.s = make(packs=True)

    def test_make_carousel_from_a_reel_once(self):
        r = carousel.make(self.db, REEL, "carousel", "bill")
        self.assertTrue(r["created"])
        v = self.db.get("variants", "variant_id", r["variant_id"])
        self.assertEqual(v["variant_id"], REEL + "~carousel")
        self.assertEqual(v["post_type"], "carousel")
        self.assertEqual(v["render_status"], "queued")
        self.assertIn("Swipe for the finish.", v["caption"])
        self.assertEqual(v["treatment"]["parent"], REEL)
        self.assertIn(v["state"], ("in_review", "needs_fix"))
        self.assertFalse(carousel.make(self.db, REEL, "carousel", "bill")["created"])  # idempotent
        with self.assertRaises(carousel.CarouselError):
            carousel.make(self.db, r["variant_id"], "story", "bill")  # not from another carousel

    def test_instagram_only_for_now(self):
        with self.assertRaises(carousel.CarouselError):
            carousel.make(self.db, f"{CJ}:carjitsu.tiktok.carjitsuchampionship", "carousel", "bill")

    def test_carousel_skips_video_length_checks_and_approves(self):
        vid = carousel.make(self.db, REEL, "carousel", "bill")["variant_id"]
        v = self.db.get("variants", "variant_id", vid)
        self.assertFalse([e for e in v["validation"]["errors"] if "cut length" in e or "EDL" in e])
        if v["state"] == "in_review":
            self.assertEqual(approve(self.db, vid, "bill")["state"], "approved")


@unittest.skipUnless(HAS_FFMPEG, "ffmpeg not installed")
class SlideTests(unittest.TestCase):
    def setUp(self):
        self.db, self.s = make(packs=True)
        reel = self.db.get("variants", "variant_id", REEL)
        self.db.update("assets", "asset_id", reel["asset_id"], {"media_path": _clip(), "media_present": True,
                                                               "width": 1920, "height": 1080, "duration_s": 4.0})
        self.db.update("variants", "variant_id", REEL, {"thumbnail": {"source_time": 2, "carousel_frames": [0.5, 2.0, 3.5]}})

    def test_carousel_slides_are_4x5_jpegs_with_a_cta_card(self):
        from PIL import Image
        vid = carousel.make(self.db, REEL, "carousel", "bill")["variant_id"]
        r = carousel.render_slides(self.db, vid, self.s)
        self.assertEqual(r["status"], "rendered", r)
        self.assertTrue(r["qc"]["passed"], r["qc"])
        self.assertEqual([s["role"] for s in r["slides"]], ["setup", "payoff", "reaction", "cta"])
        for s in r["slides"]:
            with Image.open(s["file"]) as im:
                self.assertEqual((im.size, im.format), ((1080, 1350), "JPEG"))
        v = self.db.get("variants", "variant_id", vid)
        self.assertEqual(v["render_path"], r["slides"][0]["file"])

    def test_landscape_frames_are_stacked_not_cropped(self):
        from PIL import Image
        f = Path(tempfile.mkdtemp()) / "f.png"
        Image.new("RGB", (1920, 1080), (200, 30, 30)).save(f)
        img, (y0, y1) = carousel.compose(f, 1080, 1350)
        self.assertEqual(img.size, (1080, 1350))
        self.assertEqual(y1 - y0, 607)  # the whole 16:9 frame across the slide
        self.assertGreater(y0, 300)  # room above for the hook

    def test_words_avoid_graphics_the_picture_already_has(self):
        import json
        portrait = _clip(1080, 1920)
        v = {**self.db.get("variants", "variant_id", REEL), "variant_id": "x"}

        def llm(system, messages, max_tokens):  # slide 1 (payoff): graphic low; slide 2 (reaction): graphics everywhere
            return json.dumps({"frames": [{"t": 1, "zones": ["lower"]}, {"t": 2, "zones": ["top", "middle", "lower"]}]})
        out = Path(tempfile.mkdtemp())
        slides = carousel.build_slides(v, portrait, "carousel", out, llm=llm)
        by = {s["role"]: s for s in slides}
        self.assertTrue(by["payoff"]["placed"].startswith("middle"))
        self.assertEqual(by["reaction"]["text"], "")
        self.assertTrue(by["reaction"]["placed"].startswith("dropped"))
        self.assertEqual(by["setup"]["placed"], "top")

    def test_story_uses_the_reels_video_when_rendered(self):
        self.db.update("variants", "variant_id", REEL, {"render_status": "rendered", "render_path": "s3://b/r/video.mp4"})
        vid = carousel.make(self.db, REEL, "story", "bill")["variant_id"]
        r = carousel.render_slides(self.db, vid, self.s)
        self.assertEqual([(s["kind"], s["file"]) for s in r["slides"]], [("video", "s3://b/r/video.mp4")])

    def test_story_still_without_a_reel_render(self):
        vid = carousel.make(self.db, REEL, "story", "bill")["variant_id"]
        r = carousel.render_slides(self.db, vid, self.s)
        self.assertTrue(r["qc"]["passed"], r["qc"])
        self.assertEqual(len(r["slides"]), 1)

    def test_render_variant_dispatches_slides(self):
        from pln_agent.media import render_variant
        vid = carousel.make(self.db, REEL, "carousel", "bill")["variant_id"]
        self.assertEqual(render_variant(self.db, vid, self.s)["status"], "rendered")


class PublishTests(unittest.TestCase):
    def setUp(self):
        self.db, self.s = make(packs=True)
        self.vid = carousel.make(self.db, REEL, "carousel", "bill")["variant_id"]
        slides = [{"kind": "image", "file": f"s3://pln-dam/r/slide_{i}.jpg", "role": r, "text": "", "t": None}
                  for i, r in enumerate(("setup", "payoff", "reaction", "cta"), 1)]
        self.db.update("variants", "variant_id", self.vid, {"slides": slides, "render_status": "rendered",
                                                           "render_path": slides[0]["file"], "render_qc": {"passed": True}})
        self.acc = self.db.get("accounts", "account_id", "putt_tour.instagram.putttour")

    def test_public_links_for_every_slide(self):
        os.environ["PLN_MEDIA_PUBLIC_BUCKET"] = "pub"
        os.environ["PLN_MEDIA_PUBLIC_BASE_URL"] = "https://media.example.invalid"
        self.addCleanup(os.environ.pop, "PLN_MEDIA_PUBLIC_BUCKET", None)
        self.addCleanup(os.environ.pop, "PLN_MEDIA_PUBLIC_BASE_URL", None)
        v = self.db.get("variants", "variant_id", self.vid)
        if v["state"] != "approved":
            self.db.update("variants", "variant_id", self.vid, {"state": "approved"})
        copies = []
        url = publish_render(self.db, self.vid, "t", copier=lambda *a: copies.append(a))
        self.assertEqual(len(copies), 4)
        self.assertTrue(all(c[4] == "image/jpeg" for c in copies))
        slides = self.db.get("variants", "variant_id", self.vid)["slides"]
        self.assertEqual(url, slides[0]["public_url"])
        self.assertRegex(slides[3]["public_url"], r"/v/[A-Za-z0-9_-]{24}/putt-tour-instagram-carousel-4\.jpg$")

    def test_simulated_plan_shows_items_then_carousel(self):
        v = self.db.get("variants", "variant_id", self.vid)
        sent = get_adapter("instagram", settings=self.s).simulate(v, self.acc, "k" * 32).raw["would_send"]
        bodies = [r["json"] if isinstance(r, dict) and "json" in r else r for r in sent]
        text = str(bodies)
        self.assertEqual(text.count("is_carousel_item"), 4)
        self.assertIn("CAROUSEL", text)

    def test_publish_creates_children_then_parent(self):
        os.environ["PLN_TOKEN_OVERRIDE"] = "x"
        sent = []

        def transport(req):
            sent.append(req)
            if req.method == "GET" and "status_code" in req.url:
                return HttpResponse(200, {"status_code": "FINISHED"})
            if req.method == "GET":
                return HttpResponse(200, {"permalink": "https://www.instagram.com/p/abc/"})
            if req.url.endswith("/media_publish"):
                return HttpResponse(200, {"id": "media-1"})
            return HttpResponse(200, {"id": f"c{len(sent)}"})
        v = self.db.get("variants", "variant_id", self.vid)
        v["slides"] = [{**s, "public_url": f"https://m/{i}.jpg"} for i, s in enumerate(v["slides"])]
        from unittest import mock
        with mock.patch("pln_agent.adapters.providers.resolve_token", lambda ref: "tok"):
            res = get_adapter("instagram", transport=transport, settings=self.s).publish(
                {**v, "public_media_url": "https://m/0.jpg"}, self.acc, "k")
        posts = [r for r in sent if r.method == "POST"]
        self.assertEqual([bool(r.json_body.get("is_carousel_item")) for r in posts[:4]], [True] * 4)
        self.assertEqual(posts[4].json_body["media_type"], "CAROUSEL")
        self.assertEqual(posts[4].json_body["children"], "c1,c2,c3,c4")
        self.assertEqual(res.platform_post_id, "media-1")

    def test_publish_refuses_without_public_slide_links(self):
        from pln_agent.adapters.base import AdapterError
        from unittest import mock
        v = self.db.get("variants", "variant_id", self.vid)
        with mock.patch("pln_agent.adapters.providers.resolve_token", lambda ref: "tok"), self.assertRaises(AdapterError):
            get_adapter("instagram", transport=lambda r: HttpResponse(200, {"id": "1"}), settings=self.s).publish(
                {**v, "public_media_url": "https://m/0.jpg"}, self.acc, "k")

    def test_story_plan(self):
        sid = carousel.make(self.db, REEL, "story", "bill")["variant_id"]
        self.db.update("variants", "variant_id", sid, {"slides": [{"kind": "video", "file": "s3://b/v.mp4", "role": "story"}]})
        v = self.db.get("variants", "variant_id", sid)
        reqs = get_adapter("instagram", settings=self.s).plan_requests(v, self.acc, "https://m/x.mp4")
        self.assertEqual(reqs[0].json_body["media_type"], "STORIES")
        self.assertIn("video_url", reqs[0].json_body)
        self.assertNotIn("caption", reqs[0].json_body)


class PageTests(unittest.TestCase):
    def test_variant_page_offers_carousel_and_story(self):
        from fastapi.testclient import TestClient
        from pln_agent.web import app as web
        db, s = make(packs=True)
        web._db = db
        web.settings = s
        c = TestClient(web.app)
        r = c.get(f"/variant/{REEL}")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Make carousel", r.text)
        self.assertIn("Make story", r.text)
        r = c.post(f"/variant/{REEL}/make/carousel", follow_redirects=False)
        self.assertIn("~carousel", r.headers["location"])
        r = c.get(f"/variant/{REEL}~carousel")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Open Reel", r.text)


if __name__ == "__main__":
    unittest.main()
