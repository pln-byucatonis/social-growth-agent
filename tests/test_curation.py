"""Curation queue (pln_agent/curation.py): a person picks the moment; the editor and critic don't overrule it."""
import json
import unittest

import tests.fixtures_social as fx
from pln_agent import curation, editor, recut, team
from pln_agent.db import DB
from tests.helpers import make, test_db_url
from tests.test_recut import CUT, GOOD, Model


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.db, self.s = make(packs=False)
        self.ops = DB(test_db_url(), init=False)
        fx.build_clips(self.ops)

    def test_fill_takes_the_best_per_recording_without_overlaps(self):
        r = curation.fill(self.db, self.ops, "carjitsu", per_recording=5)
        self.assertEqual((len(r["added"]), r["recordings"]), (5, 1))
        ids = [c["clip_id"] for c in self.db.all("curation_clips")]
        self.assertFalse({"CJ:0", "CJ:0b"} <= set(ids))
        self.assertEqual(curation.fill(self.db, self.ops, "carjitsu", per_recording=5)["added"], [])  # idempotent
        self.assertTrue(curation.pending(self.db))  # previews to make

    def test_pick_rules(self):
        curation.fill(self.db, self.ops, "carjitsu", per_recording=3)
        c = self.db.all("curation_clips")[0]
        s0 = c["start_s"]
        for args in ((s0, s0 + 1, s0 + 0.5, "too short a pick"), (s0 - 30, s0 + 5, s0, "outside the preview"),
                     (s0, s0 + 8, s0 + 9, "payoff outside the pick"), (s0, s0 + 8, s0 + 4, "short")):
            with self.assertRaises(curation.CurationError):
                curation.pick(self.db, c["clip_id"], *args, actor="mike")
        p = curation.pick(self.db, c["clip_id"], s0, s0 + 8, s0 + 5, "He slips the choke and ends up on top", "mike")
        self.assertEqual(self.db.get("curation_clips", "clip_id", c["clip_id"])["status"], "picked")
        self.assertEqual(p["status"], "pending")

    def test_pick_becomes_an_asset_with_the_persons_range_and_note(self):
        curation.fill(self.db, self.ops, "carjitsu", per_recording=3)
        c = self.db.all("curation_clips")[0]
        s0 = c["start_s"]
        curation.pick(self.db, c["clip_id"], s0 + 1, s0 + 9, s0 + 6, "He slips the choke and ends up on top", "mike")
        out = curation.run_pending(self.db, self.ops, self.s, "bucket", limit=0)
        self.assertEqual(out[0]["pick"], "done")
        a = self.db.get("assets", "asset_id", out[0]["asset_id"])
        self.assertEqual(a["probe"]["curated"]["note"], "He slips the choke and ends up on top")
        self.assertEqual(a["description"], "He slips the choke and ends up on top")
        self.assertEqual((a["moments"][0]["start"], a["moments"][-1]["end"]), (s0 + 1, s0 + 9))
        self.assertTrue(out[0]["variants"])


class OverruleTests(unittest.TestCase):
    def setUp(self):
        self.old = team._llm

    def tearDown(self):
        team._llm = self.old

    def _curated(self):
        db, s = make()
        from tests.helpers import CJ
        a = db.get("assets", "asset_id", CJ)
        db.update("assets", "asset_id", CJ, {"probe": {**(a.get("probe") or {}), "curated": {
            "by": "mike", "at": "x", "note": "He flips it from the bottom", "start": 9.0, "end": 16.0, "payoff_at": 11.5}}})
        recut.pull(db, "test")
        return db, s, f"{CJ}:carjitsu.instagram.carjitsu_championship"

    def test_the_editor_cannot_drop_a_persons_pick(self):
        db, s, vid = self._curated()
        m = team._llm = Model([{"readable": False, "why": "dark car"}], [{"scores": GOOD, "verdict": "pass"}])
        r = recut.process(db, vid, s)
        self.assertIn("picked this moment by hand", m.calls[0]["text"])
        self.assertEqual(r["outcome"], "passed")
        ed = db.get("variants", "variant_id", vid)["treatment"]["editor"]
        self.assertEqual([(x["in"], x["out"]) for x in ed["shots"]], [(9.0, 16.0)])

    def test_a_critic_objection_goes_to_approvals_as_a_note(self):
        db, s, vid = self._curated()
        kill = {"scores": {**GOOD, "payoff_visible": 1}, "verdict": "kill", "cut_problems": ["the tap is hidden"]}
        team._llm = Model([CUT], [kill])
        r = recut.process(db, vid, s)
        v = db.get("variants", "variant_id", vid)
        self.assertEqual(r["outcome"], "passed")
        self.assertIn(v["state"], ("in_review", "needs_fix"))
        self.assertIn("the tap is hidden", v["treatment"]["recut"]["critic_note"])


class PageTests(unittest.TestCase):
    def test_page(self):
        from fastapi.testclient import TestClient
        from pln_agent.web import app as web
        db, s = make(packs=False)
        ops = DB(test_db_url(), init=False)
        fx.build_clips(ops)
        curation.fill(db, ops, "carjitsu", per_recording=2)
        c = db.all("curation_clips")[0]
        db.update("curation_clips", "clip_id", c["clip_id"], {"preview_path": "/tmp/p.mp4", "preview_status": "ready", "preview_from": 0})
        web._db, web.settings = db, s
        t = TestClient(web.app)
        page = t.get("/curation?brand=carjitsu").text
        self.assertIn("Pick this moment", page)
        r = t.post(f"/curation/{c['clip_id']}/pick", data={"start": c["start_s"], "end": c["start_s"] + 6, "payoff_at": c["start_s"] + 3,
                                                           "note": "He escapes out of the door", "brand": "carjitsu"}, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertEqual(db.get("curation_clips", "clip_id", c["clip_id"])["status"], "picked")


if __name__ == "__main__":
    unittest.main()
