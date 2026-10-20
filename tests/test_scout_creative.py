"""Phase 2: Viral Content Scout and Creative Director."""
import unittest

from pln_agent import scout
from pln_agent.creative_director import RECIPES, use_cut
from pln_agent.db import DB, now_iso
from pln_agent.workflow import approve

from tests import fixtures_social as fx
from tests.helpers import make, test_db_url


class ScoutTests(unittest.TestCase):
    def setUp(self):
        self.db, self.s = make(packs=False)
        self.ops = DB(test_db_url(), init=False)
        fx.build_clips(self.ops)
        self.r = scout.scan(self.db, self.ops)

    def m(self, cid):
        return self.db.get("scout_moments", "clip_id", cid)

    def test_every_clip_scored_with_explainable_parts(self):
        self.assertEqual(self.r["scored"], 39)
        rev = self.m("CJ:0")
        self.assertIn("unexpected_reversal", rev["categories"])
        self.assertEqual(set(rev["detail"]["parts"]), set(scout.taxonomy()["weights"]))
        self.assertIn("revers", rev["detail"]["parts"]["category"]["why"].lower())
        self.assertIn("neutral", rev["detail"]["parts"]["learned"]["why"])
        # same ops highlight score, but a stop-the-scroll category beats a reset in the car
        self.assertGreater(rev["potential"], self.m("CJ:3")["potential"])
        self.assertEqual(self.m("CJ:3")["categories"], [])
        # Putt Tour uses its own taxonomy
        self.assertIn("impossible_putt", self.m("PT:0")["categories"])
        self.assertIn("missed_short_putt", self.m("PT:1")["categories"])
        self.assertEqual(scout.scan(self.db, self.ops)["scored"], 0)  # incremental

    def test_injury_adjacent_moments_are_ranked_down_and_flagged(self):
        base = {"start_sec": 0, "end_sec": 15, "highlight_score": 80, "energy_score": 0.7, "transcript": ""}
        ko = scout.score_clip(self.db, {**base, "caption": "Big slap, he drops hard, out cold"}, "slapfight", [], [], False)
        react = scout.score_clip(self.db, {**base, "caption": "Big slap, the crowd reacts in shock"}, "slapfight", [], [], False)
        self.assertLess(ko["potential"], react["potential"])
        self.assertLess(ko["pln_potential"], react["pln_potential"])
        self.assertIn("never the hook", ko["detail"]["safety"])
        self.assertEqual(react["detail"]["safety"], "")

    def test_top_20_of_an_event_are_non_overlapping_and_diverse(self):
        top = scout.top_moments(self.db, fx.CJ_MASTER, n=20)
        self.assertLessEqual(len(top), 20)
        ids = {t["clip_id"] for t in top}
        self.assertFalse({"CJ:0", "CJ:0b"} <= ids)  # overlapping near-duplicates
        from collections import Counter
        cats = Counter((t["categories"] or ["uncategorised"])[0] for t in top)
        self.assertLessEqual(max(cats.values()), 4)
        self.assertEqual(top, sorted(top, key=lambda t: -t["potential"]))

    def test_learned_lift_comes_only_from_pln_evidence(self):
        base, why = scout.learned_lift(self.db, "carjitsu", "submission_highlight")
        self.assertEqual(base, 0.5)
        self.db.insert("knowledge", {"entry_id": "pln-x", "level": "pln_evidence", "platform": "instagram",
                                     "brand_id": "carjitsu", "topic": "content_type", "statement": "s", "status": "current",
                                     "evidence": {"outcome": "shares_per_1k_views", "value": "submission_highlight",
                                                  "lift": 1.4, "evidence": "conclusive", "n": 40}})
        lift, why = scout.learned_lift(self.db, "carjitsu", "submission_highlight")
        self.assertEqual(lift, 1.0)  # capped
        self.assertIn("pln-x", why)

    def test_promote_creates_assets_packages_and_keeps_human_decisions(self):
        out = scout.promote(self.db, self.ops, ["CJ:0", "PT:0"], self.s, "by@pln")
        self.assertEqual(len(out["assets"]), 2)
        self.assertEqual(self.m("CJ:0")["status"], "promoted")
        pkg = self.db.all("creative_packages")
        self.assertEqual(len(pkg), 2)
        scout.dismiss(self.db, "CJ:2", "by@pln", "weak")
        self.assertNotIn("CJ:2", {t["clip_id"] for t in scout.top_moments(self.db, fx.CJ_MASTER)})
        self.assertNotIn("CJ:0", {t["clip_id"] for t in scout.top_moments(self.db, fx.CJ_MASTER)})


class CreativeDirectorTests(unittest.TestCase):
    def setUp(self):
        import os
        self._policy = os.environ.get("PLN_IDENTITY_POLICY")
        os.environ["PLN_IDENTITY_POLICY"] = "withhold_names"  # production policy
        self.addCleanup(lambda: os.environ.pop("PLN_IDENTITY_POLICY", None) if self._policy is None
                        else os.environ.__setitem__("PLN_IDENTITY_POLICY", self._policy))
        self.db, self.s = make(packs=False)
        self.ops = DB(test_db_url(), init=False)
        fx.build_clips(self.ops)
        scout.scan(self.db, self.ops)
        from pln_agent.ops_sources import set_rights_default
        set_rights_default(self.db, "by@pln", license="PLN-owned production", owner="Pro League Network",
                           territory=["WORLDWIDE"], rights_verified=True, music_cleared=True)
        out = scout.promote(self.db, self.ops, ["CJ:1"], self.s, "by@pln")  # no detected person → not identity-blocked
        self.aid = out["assets"][0]
        self.all = self.db.all("variants", "asset_id=?", [self.aid])
        self.vs = {v["platform"]: v for v in self.all if v["route_role"] != "pln_amplification"}

    def test_one_moment_distinct_executions_per_platform(self):
        self.assertTrue({"tiktok", "instagram", "youtube"} <= set(self.vs))
        openings = {p: v["treatment"]["hook"]["opening_beat"] for p, v in self.vs.items() if v.get("treatment")}
        self.assertEqual(len(set(openings.values())), len(openings))  # no two destinations open the same way
        for v in self.vs.values():
            t = v["treatment"]
            for part in ("hook", "edit", "context", "payoff", "conversion"):
                self.assertIn(part, t)
            self.assertTrue(t["knowledge_refs"])
        if "linkedin" in self.vs:
            self.assertIn("Business treatment", self.vs["linkedin"]["treatment"]["note"])
        pkg = self.db.all("creative_packages", "asset_id=?", [self.aid])[0]
        self.assertEqual(set(pkg["treatments"]), {v["account_id"] for v in self.all if v.get("treatment")})

    def test_duration_variants_and_honest_availability(self):
        t = self.vs["tiktok"]["treatment"]
        labels = {x["label"]: x for x in t["duration_variants"]}
        self.assertEqual(set(labels), {"A", "B", "C"})
        # clip came from the master (no vertical rendition) → longer cuts can reach into the event footage
        self.assertTrue(labels["C"]["available"])
        self.assertAlmostEqual(labels["A"]["actual_s"], 12.0, places=1)
        self.assertEqual(t["recommended_variant"], RECIPES["tiktok"]["default"])
        self.assertIn("hypothesis", t["recommended_why"])

    def test_pln_duration_evidence_overrides_the_recipe(self):
        self.db.insert("knowledge", {"entry_id": "pln-dur", "level": "pln_evidence", "platform": "tiktok",
                                     "brand_id": "carjitsu", "topic": "duration_bucket", "statement": "s", "status": "current",
                                     "evidence": {"outcome": "shares_per_1k_views", "value": "31–60s", "lift": 0.4,
                                                  "evidence": "suggestive", "n": 9}})
        from pln_agent.editorial import generate_pack
        generate_pack(self.db, self.aid, self.s, actor="t")
        v = self.db.get("variants", "variant_id", self.vs["tiktok"]["variant_id"])
        self.assertEqual(v["treatment"]["recommended_variant"], "C")
        self.assertIn("pln-dur", v["treatment"]["knowledge_refs"])

    def test_switching_cut_invalidates_review_and_render(self):
        vid = self.vs["tiktok"]["variant_id"]
        self.assertEqual(self.db.get("variants", "variant_id", vid)["state"], "in_review",
                         self.db.get("variants", "variant_id", vid)["validation"])
        approve(self.db, vid, "editor")
        approve(self.db, vid, "second-editor")  # combat → two-person signoff
        self.assertEqual(self.db.get("variants", "variant_id", vid)["state"], "approved")
        self.db.update("variants", "variant_id", vid, {"render_status": "rendered", "render_path": "s3://b/k.mp4"})
        v = use_cut(self.db, vid, "B", "editor")
        self.assertEqual(v["state"], "in_review")
        self.assertEqual(v["edl"]["variant_label"], "B")
        self.assertEqual(v["render_status"], "not_rendered")
        self.assertTrue(v["edl"]["editor_cut"])
        from pln_agent.workflow import _current_approvers
        self.assertEqual(_current_approvers(self.db, v), set())
        with self.assertRaises(ValueError):
            use_cut(self.db, vid, "Z", "editor")

    def test_scout_and_variant_pages_render(self):
        from fastapi.testclient import TestClient
        from pln_agent.web import app as web
        web._db = self.db
        c = TestClient(web.app)
        r = c.get("/scout?brand=carjitsu")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Viral Content Scout", r.text)
        r = c.get(f"/variant/{self.vs['tiktok']['variant_id']}")
        self.assertIn("Creative treatment", r.text)
        self.assertIn("Use cut", r.text)


if __name__ == "__main__":
    unittest.main()
