"""Video Production: QC on renders, A/B/C cut renders, ops subject-tracked vertical hand-off."""
import subprocess
import tempfile
import unittest
from pathlib import Path

from pln_agent.media import qc_render

from tests.helpers import CJ, make


def _clip(path, dur=3, black_head=0.0, audio=True, size="1080x1920"):
    vf = f"color=c=gray:s={size}:d={dur}:r=25"
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", vf]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={dur}"]
    filt = f"drawbox=c=black:t=fill:enable='lt(t,{black_head})'" if black_head else "null"
    cmd += ["-vf", filt, "-pix_fmt", "yuv420p", "-shortest", str(path)]
    subprocess.run(cmd, check=True)
    return str(path)


class QualityControlTests(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())

    def test_clean_render_passes_with_measures(self):
        qc = qc_render(_clip(self.d / "ok.mp4"), {"w": 1080, "h": 1920}, {"min_s": 2, "max_s": 60}, "tiktok")
        self.assertTrue(qc["passed"], qc)
        self.assertEqual(qc["measures"]["size"], "1080x1920")
        self.assertIn("loudness_lufs", qc["measures"])

    def test_black_opening_wrong_size_and_overlong_fail(self):
        qc = qc_render(_clip(self.d / "bad.mp4", dur=4, black_head=1.0, size="1280x720"), {"w": 1080, "h": 1920},
                       {"max_s": 3}, "tiktok")
        self.assertFalse(qc["passed"])
        text = " ".join(qc["errors"])
        self.assertIn("opens on black", text)
        self.assertIn("expected 1080x1920", text)
        self.assertIn("longer than", text)

    def test_silent_render_is_flagged(self):
        qc = qc_render(_clip(self.d / "mute.mp4", audio=False), {"w": 1080, "h": 1920}, {}, "instagram")
        self.assertIn("no audio track", qc["warnings"])


class OpsVerticalTests(unittest.TestCase):
    def setUp(self):
        self.db, self.s = make(packs=True)
        self.vid = f"{CJ}:carjitsu.tiktok.carjitsuchampionship"
        # make the sample asset look like an S3 master with a CD treatment
        self.db.update("assets", "asset_id", CJ, {"media_path": "s3://pln-vod-library/PLNstudio/CJ.mp4"})
        t = {"duration_variants": [
            {"label": "A", "available": True, "actual_s": 12.0, "payoff_at_s": 1.0,
             "segments": [{"source_in": 100.0, "source_out": 112.0, "purpose": "variant A", "moment": "x", "source_timecode": ""}]},
            {"label": "C", "available": False, "why": "short"}]}
        self.db.update("variants", "variant_id", self.vid, {"treatment": t})

    def test_request_then_poll_marks_ready(self):
        from pln_agent.ops_vertical import label_for, poll, request_cuts
        sent = []
        r = request_cuts(self.db, self.vid, "by@pln", invoke=sent.append)
        self.assertEqual(r["requested"], ["A"])
        self.assertEqual(sent[0], {"source_bucket": "pln-vod-library", "source_key": "PLNstudio/CJ.mp4", "start_sec": 100.0,
                                   "end_sec": 112.0, "label": label_for(self.vid, "A")})
        self.assertEqual(request_cuts(self.db, self.vid, "by@pln", invoke=sent.append)["requested"], [])  # idempotent
        none_yet = poll(self.db, "assets", lister=lambda prefix: [], render=False)
        self.assertEqual(none_yet["ready"], [])
        key = f"verticals/{label_for(self.vid, 'A')}/CJ_100-112_9x16.mp4"
        done = poll(self.db, "assets", lister=lambda prefix: [key, key.replace("9x16", "16x9")], render=False)
        self.assertEqual(done["ready"], [(self.vid, "A")])
        cut = self.db.get("variants", "variant_id", self.vid)["treatment"]["duration_variants"][0]
        self.assertEqual(cut["ops_vertical"]["status"], "ready")
        self.assertEqual(cut["ops_vertical"]["key"], key)

    def test_vertical_renditions_are_not_re_requested(self):
        from pln_agent.ops_vertical import request_cuts
        self.db.update("assets", "asset_id", CJ, {"media_path": "s3://assets/verticals/CJ-10-24_9x16.mp4"})
        r = request_cuts(self.db, self.vid, "by@pln", invoke=lambda p: self.fail("should not invoke"))
        self.assertEqual(r["requested"], [])


class CutRenderTests(unittest.TestCase):
    def test_render_cuts_stores_per_cut_results_and_qc(self):
        from pln_agent.media import render_cuts
        db, s = make(packs=True)
        vid = f"{CJ}:carjitsu.tiktok.carjitsuchampionship"
        src = _clip(Path(tempfile.mkdtemp()) / "master.mp4", dur=8, size="1920x1080")
        db.update("assets", "asset_id", CJ, {"media_path": src, "media_present": True, "width": 1920, "height": 1080})
        t = {"duration_variants": [{"label": "A", "available": True, "actual_s": 4.0, "payoff_at_s": 1.0,
                                    "segments": [{"source_in": 1.0, "source_out": 5.0, "purpose": "variant A",
                                                  "moment": "x", "source_timecode": ""}]}]}
        db.update("variants", "variant_id", vid, {"treatment": t})
        out = render_cuts(db, vid, s, "t")
        self.assertEqual(out[0]["status"], "rendered", out)
        cut = db.get("variants", "variant_id", vid)["treatment"]["duration_variants"][0]
        self.assertEqual(cut["render"]["status"], "rendered")
        self.assertIn("passed", cut["render"]["qc"])
        self.assertEqual(cut["render"]["qc"]["measures"]["size"], "1080x1920")
        # the post's own render is untouched
        self.assertNotEqual(db.get("variants", "variant_id", vid)["render_status"], "rendered")


if __name__ == "__main__":
    unittest.main()
