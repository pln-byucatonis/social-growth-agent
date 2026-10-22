"""Stacked vs cropped: a growth_lab `layout` experiment assigns arms; renders honour them unless a crop would cut graphics."""
import unittest

from pln_agent import growth_lab as lab
from pln_agent import media
from tests.helpers import make


def _landscape(db):
    for a in db.all("assets"):
        db.update("assets", "asset_id", a["asset_id"], {"width": 1920, "height": 1080})


class LayoutTests(unittest.TestCase):
    def _acct(self, db):
        vs = [v for v in db.all("variants", "state='in_review'") if v["platform"] == "instagram" and not v.get("post_type")]
        return vs[0]["account_id"]

    def test_assign_alternates_and_marks_the_render(self):
        db, _ = make()
        _landscape(db)
        acct = self._acct(db)
        e = lab.create(db, acct, "layout", dict(lab.LAYOUTS), "stacked holds longer", "t", outcome="watch_pct")
        arms = db.all("experiment_arms", "experiment_id=?", [e["experiment_id"]])
        self.assertTrue(arms)
        got = {db.get("variants", "variant_id", a["variant_id"])["treatment"]["layout_arm"] for a in arms}
        self.assertEqual(got, {"crop", "stack"} if len(arms) > 1 else got)
        self.assertEqual(e["result"]["outcome"], "watch_pct")

    def test_portrait_masters_are_not_in_the_test(self):
        db, _ = make()
        for a in db.all("assets"):
            db.update("assets", "asset_id", a["asset_id"], {"width": 1080, "height": 1920})
        e = lab.create(db, self._acct(db), "layout", dict(lab.LAYOUTS), "x", "t", outcome="watch_pct")
        self.assertEqual(db.all("experiment_arms", "experiment_id=?", [e["experiment_id"]]), [])

    def test_crop_arm_that_would_cut_graphics_leaves_the_test(self):
        db, _ = make()
        _landscape(db)
        e = lab.create(db, self._acct(db), "layout", dict(lab.LAYOUTS), "x", "t", outcome="watch_pct")
        crop = next(a for a in db.all("experiment_arms", "experiment_id=?", [e["experiment_id"]])
                    if db.get("variants", "variant_id", a["variant_id"])["treatment"]["layout_arm"] == "crop")
        media._leave_layout_test(db, crop["variant_id"], "score bug across the bottom")
        self.assertIsNone(db.one("SELECT 1 AS x FROM experiment_arms WHERE variant_id=?", [crop["variant_id"]]))
        t = db.get("variants", "variant_id", crop["variant_id"])["treatment"]
        self.assertIsNone(t["layout_arm"])
        self.assertIn("score bug", t["layout_forced"])

    def test_proposed_with_watch_outcome(self):
        db, _ = make()
        _landscape(db)
        ps = [p for p in lab.propose(db) if p["variable"] == "layout"]
        self.assertTrue(ps)
        self.assertEqual(ps[0]["outcome"], "watch_pct")

    def test_watch_pct_outcome(self):
        from pln_agent.audience import post_outcomes
        db, _ = make(packs=False)
        acct = db.all("accounts", "platform='instagram'")[0]["account_id"]
        db.insert("posts", {"post_id": "instagram:1", "account_id": acct, "platform": "instagram", "platform_post_id": "1",
                            "published_at": "2026-10-01T00:00:00+00:00", "duration_s": 20.0})
        db.insert("metrics", {"account_id": acct, "platform": "instagram", "platform_post_id": "1", "metric": "ig_reels_avg_watch_time",
                              "canonical_metric": "avg_watch_time_s", "value": 7.0, "source": "t", "captured_at": "2026-10-08T00:00:00+00:00"})
        p = post_outcomes(db, [acct])[0]
        self.assertAlmostEqual(p["outcomes"]["watch_pct"], 35.0)


if __name__ == "__main__":
    unittest.main()
