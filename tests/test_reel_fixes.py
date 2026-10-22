"""Creative review fixes: stacked layout when a crop would cut broadcast graphics, hooks checked against the clock."""
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from pln_agent import art_director, overlays

HAS_FFMPEG = bool(shutil.which("ffmpeg"))


def _broadcast_clip(bar: bool) -> str:
    out = Path(tempfile.mkdtemp()) / "bc.mp4"
    vf = "drawbox=x=0:y=ih*0.82:w=iw:h=ih*0.12:color=white@1:t=fill,drawgrid=w=40:h=ih*0.12:y=ih*0.82:color=black:t=3" if bar else "null"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    "testsrc2=size=1920x1080:rate=25:duration=6", "-vf", vf, "-pix_fmt", "yuv420p", str(out)], check=True)
    return str(out)


@unittest.skipUnless(HAS_FFMPEG, "ffmpeg not installed")
class StackTests(unittest.TestCase):
    def test_lower_third_across_the_frame_means_stack(self):
        segs = [{"source_in": 0.2, "source_out": 5.8}]
        no_llm = lambda *a: json.dumps({"cut_graphics": False})  # noqa: E731
        self.assertTrue(art_director.crop_cuts_graphics(_broadcast_clip(True), segs, llm=no_llm)[0])

    def test_vision_can_ask_for_stack(self):
        segs = [{"source_in": 0.2, "source_out": 5.8}]
        yes = lambda *a: json.dumps({"cut_graphics": True, "what": "HOLE IN ONE card"})  # noqa: E731
        ok, why = art_director.crop_cuts_graphics(_broadcast_clip(False), segs, llm=yes)
        self.assertTrue(ok)
        self.assertIn("HOLE IN ONE", why)


class StackedOverlayTests(unittest.TestCase):
    def test_words_go_in_the_band_under_the_frame(self):
        v = {"variant_id": "x", "brand_id": "carjitsu", "onscreen_text": [{"text": "WATCH THE SEATBELT", "start": 0, "end": 2}]}
        layout = {"mode": "stack", "y0": 576, "y1": 1184}
        r = overlays.build(v, [], 12.0, 1080, 1920, Path(tempfile.mkdtemp()), 6.0,
                           [{"kind": "payoff", "t": 6.0, "intensity": 1}, {"kind": "reaction", "t": 8.0, "intensity": 1}],
                           None, layout=layout)
        zones = {p["what"].split(" ")[0]: p["zone"] for p in r["plan"]}
        self.assertEqual(zones["layout"], "stacked")
        self.assertEqual(zones["hook"], "top")
        self.assertEqual(zones["payoff"], "lower")


class HookCheckTests(unittest.TestCase):
    def test_numbers_are_checked_and_replaced(self):
        from PIL import Image
        a = {"ok": True, "frames": {0.2: Image.new("RGB", (270, 480)), 1.0: Image.new("RGB", (270, 480))}}
        bad = lambda *a: json.dumps({"contradicts": True, "why": "the clock reads 0:11"})  # noqa: E731
        self.assertEqual(art_director.hook_contradicts(a, "2 SECONDS LEFT.", 0, 2, llm=bad), (True, "the clock reads 0:11"))
        self.assertEqual(art_director.hook_contradicts(a, "WATCH THE SEATBELT", 0, 2, llm=bad), (False, ""))  # no number: not asked


if __name__ == "__main__":
    unittest.main()
