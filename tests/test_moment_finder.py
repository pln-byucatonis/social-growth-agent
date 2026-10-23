"""Moment finder (pln_agent/moment_finder.py): search a whole recording when every draft from it was dropped."""
import json
import unittest
from pathlib import Path

import tests.fixtures_social as fx
from pln_agent import moment_finder as mf
from pln_agent import team
from pln_agent.db import DB, now_iso
from pln_agent.ops_sources import import_ops_clips
from tests.helpers import ROOT, make, test_db_url

SAMPLE = str(ROOT / "sample_media" / "inbox" / "carjitsu_sample_001.mp4")


def sample_frames(src, c, outdir):
    """Frames from the sample clip stand in for the recording (whose candidates are hours in)."""
    from pln_agent.editor import frames
    return frames(SAMPLE, 2.0, 8.0, Path(outdir), fps=1.0, width=160)[:6]


class FinderTests(unittest.TestCase):
    def setUp(self):
        self.old = team._llm
        self.db, self.s = make(packs=False)
        self.ops = DB(test_db_url(), init=False)
        fx.build_clips(self.ops)
        import_ops_clips(self.db, self.ops, "assets-bucket", clip_ids=["CJ:1"])
        from pln_agent.editorial import generate_pack
        from pln_agent.ops_sources import clip_asset_id
        self.aid = clip_asset_id("CJ:1")
        generate_pack(self.db, self.aid, self.s)
        for v in self.db.all("variants", "asset_id=?", [self.aid]):
            self.db.update("variants", "variant_id", v["variant_id"], {
                "state": "archived", "treatment": {**(v.get("treatment") or {}), "recut": {"status": "dropped", "why": "editor: dark car"}}})

    def tearDown(self):
        team._llm = self.old

    def test_only_recordings_with_nothing_left(self):
        srcs = mf.exhausted(self.db)
        self.assertEqual([s["media_path"] for s in srcs], [f"s3://pln-vod-library/PLNstudio/CarJitsu__2026-09-12.mp4"])
        v = self.db.all("variants", "asset_id=?", [self.aid])[0]
        self.db.update("variants", "variant_id", v["variant_id"], {"state": "recut", "treatment": {"recut": {"status": "pending"}}})
        self.assertEqual(mf.exhausted(self.db), [])  # still being worked on: not yet

    def test_candidates_skip_tried_clips_and_overlaps(self):
        c = mf.candidates(self.db, self.ops, "s3://pln-vod-library/PLNstudio/CarJitsu__2026-09-12.mp4")
        ids = [x["clip_id"] for x in c]
        self.assertEqual(len(c), mf.SURVEY)
        self.assertNotIn("CJ:1", ids)  # already a draft
        self.assertFalse({"CJ:0", "CJ:0b"} <= set(ids))  # near-duplicates: only one of them

    def test_search_picks_the_readable_moments_and_makes_drafts(self):
        calls = []

        def model(system, messages, max_tokens):
            content = messages[0]["content"]
            calls.append(sum(1 for x in content if x.get("type") == "image"))
            return json.dumps({"candidates": [
                {"c": 1, "readable": False, "strength": 9, "what": "tangled"},
                {"c": 2, "readable": True, "strength": 8, "payoff_t": 610, "what": "he taps, hand clearly on the seat"},
                {"c": 3, "readable": True, "strength": 5, "what": "slow reset"},
                {"c": 4, "readable": True, "strength": 9, "payoff_t": 910, "what": "escapes out of the door"},
                {"c": 5, "readable": True, "strength": 9, "wrong_sport": True, "what": "a slap contest"}]})
        team._llm = model
        made = []
        src = mf.exhausted(self.db)[0]
        row = mf.search(self.db, self.ops, self.s, src, promote=lambda ids: made.extend(ids) or {"assets": ids}, frames_fn=sample_frames)
        self.assertEqual(len(calls), 1)  # one look for the whole recording
        self.assertEqual(calls[0], mf.SURVEY)
        c = row["candidates"]
        self.assertEqual(made, [c[3]["clip_id"], c[1]["clip_id"]])  # strongest first; unreadable, weak and wrong-sport left out
        self.assertIn("made drafts from 2", row["note"])
        saved = self.db.get("moment_searches", "media_path", src["media_path"])
        self.assertEqual(saved["picked"], made)
        self.assertEqual(mf.exhausted(self.db), [])  # searched: not again for a while

    def test_wrong_league_is_reported(self):
        team._llm = lambda *a: json.dumps({"candidates": [{"c": i, "readable": True, "strength": 8, "wrong_sport": True}
                                                          for i in range(1, 16)]})
        row = mf.search(self.db, self.ops, self.s, mf.exhausted(self.db)[0], promote=lambda ids: {"assets": ids},
                        frames_fn=sample_frames)
        self.assertIn("wrong league", row["note"])
        self.assertEqual(row["picked"], [])

    def test_runner_waits_for_the_editor_queue(self):
        team._llm = lambda *a: json.dumps({"candidates": []})
        v = self.db.get("variants", "variant_id", self.db.all("variants", "asset_id=?", [self.aid])[0]["variant_id"])
        other = dict(v, variant_id="waiting", state="recut", treatment={"recut": {"status": "pending"}}, asset_id="carjitsu-sample-001")
        self.db.insert("variants", other)
        self.assertEqual(mf.run_pending(self.db, self.ops, self.s), [])


if __name__ == "__main__":
    unittest.main()
