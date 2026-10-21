"""Growth Lab: arm assignment on unapproved drafts, reading with the evidence rule, write-back to PLN evidence."""
import unittest
from datetime import date, datetime, timedelta, timezone

from pln_agent import growth_lab as lab

from tests.helpers import make

ACC = "pln.instagram.proleaguenetwork"
NOW = datetime.now(timezone.utc)


def _post(db, vid, i, eng, views=10000.0):
    pub = (NOW - timedelta(days=5, hours=i)).replace(microsecond=0).isoformat()
    pid = f"p-{vid}-{i}"
    db.insert("posts", {"post_id": f"instagram:{pid}", "account_id": ACC, "brand_id": "pln", "platform": "instagram",
                        "platform_post_id": pid, "published_at": pub, "origin": "agent_published", "variant_id": vid,
                        "creative": {"commentary": "yes" if eng > 30 else "no"}})
    for m, v in (("views", views), ("engagements", eng * views / 1000)):
        db.insert("metrics", {"account_id": ACC, "platform_post_id": pid, "platform": "instagram", "metric": m,
                              "canonical_metric": m, "value": v, "checkpoint": "7d", "source": "test",
                              "captured_at": NOW.replace(microsecond=0).isoformat(), "metric_key": f"t:{pid}:{m}"})


class GrowthLabTests(unittest.TestCase):
    def setUp(self):
        self.db, self.s = make(packs=True)

    def test_assignment_alternates_and_only_touches_unapproved_drafts(self):
        vs = self.db.all("variants", "account_id=? AND state='in_review'", [ACC])
        for v in vs:  # give each draft two available cuts
            segs = (v.get("edl") or {}).get("segments") or []
            t = dict(v.get("treatment") or {})
            t["duration_variants"] = [{"label": "A", "available": True, "segments": segs[:1]},
                                      {"label": "B", "available": True, "segments": segs}]
            self.db.update("variants", "variant_id", v["variant_id"], {"treatment": t})
        e = lab.create(self.db, ACC, "cut", {"A": "short", "B": "medium"}, "short beats medium", "bill")
        arms = self.db.all("experiment_arms", "experiment_id=?", [e["experiment_id"]])
        n = {x: sum(1 for a in arms if a["arm"] == x) for x in "AB"}
        self.assertGreaterEqual(min(n.values()), 1)
        self.assertLessEqual(abs(n["A"] - n["B"]), 1)  # balanced
        self.assertTrue(all(self.db.get("variants", "variant_id", a["variant_id"])["state"] in ("in_review", "needs_fix") for a in arms))
        for a in arms:
            self.assertEqual(self.db.get("variants", "variant_id", a["variant_id"])["edl"]["variant_label"], a["arm"])
        self.assertEqual(lab.assign(self.db, e["experiment_id"]), [])  # nothing new to assign
        with self.assertRaises(ValueError):
            lab.create(self.db, ACC, "cut", {"A": "x", "B": "y"}, "h", "bill", outcome="views")  # raw views never decide

    def test_hook_arm_rewrites_hook_and_caption_and_revalidates(self):
        v = self.db.all("variants", "account_id=? AND state='in_review'", [ACC])[0]
        styles = [h["style"] for h in v["hook_candidates"]]
        e = lab.create(self.db, ACC, "hook_style", {"A": styles[0], "B": styles[1]}, "h", "bill")
        rows = {r["variant_id"]: r for r in self.db.all("experiment_arms", "experiment_id=?", [e["experiment_id"]])}
        v2 = self.db.get("variants", "variant_id", v["variant_id"])
        want = next(h["text"] for h in v["hook_candidates"] if h["style"] == e["result"]["arms"][rows[v["variant_id"]]["arm"]])
        self.assertEqual(v2["hook"], want)
        self.assertIn(want, v2["caption"])

    def test_reading_needs_sample_then_concludes_and_writes_evidence(self):
        e = lab.create(self.db, ACC, "cut", {"A": "short", "B": "medium"}, "h", "bill",
                       start=(NOW - timedelta(days=20)).date(), mode="assigned")
        eid = e["experiment_id"]

        def arm(vid, a):
            self.db.insert("experiment_arms", {"experiment_id": eid, "variant_id": vid, "arm": a, "assigned_at": NOW.isoformat(), "applied": {}})
        for i in range(3):
            arm(f"va{i}", "A"); _post(self.db, f"va{i}", i, 40 + i)
            arm(f"vb{i}", "B"); _post(self.db, f"vb{i}", i, 20 + i)
        r = lab.read(self.db)[0]
        self.assertEqual(r["rows"][0]["evidence"], "insufficient")
        self.assertIn("collecting", r["verdict"])
        for i in range(3, 10):
            arm(f"va{i}", "A"); _post(self.db, f"va{i}", i, 40 + i)
            arm(f"vb{i}", "B"); _post(self.db, f"vb{i}", i, 20 + i)
        r = lab.read(self.db)[0]
        self.assertEqual(r["status"], "concluded")
        self.assertEqual(r["rows"][0]["evidence"], "conclusive")
        self.assertIn("A beats B", r["verdict"])
        k = self.db.get("knowledge", "entry_id", f"pln-exp-{eid}")
        self.assertEqual(k["level"], "pln_evidence")
        self.assertTrue(k["evidence"]["controlled"])
        self.assertEqual(self.db.get("experiments", "experiment_id", eid)["status"], "concluded")

    def test_observed_variable_and_timeout(self):
        e = lab.create(self.db, ACC, "commentary", {"off": "no", "on": "yes"}, "commentary helps", "bill", mode="observed",
                       start=(NOW - timedelta(days=60)).date())
        r = lab.read(self.db, today=NOW.date())[0]
        self.assertEqual(r["status"], "inconclusive")  # 0 posts after > 6 weeks
        self.assertEqual(self.db.get("experiments", "experiment_id", e["experiment_id"])["status"], "inconclusive")

    def test_page_renders_and_creates(self):
        from fastapi.testclient import TestClient
        from pln_agent.web import app as web
        web._db = self.db
        c = TestClient(web.app)
        r = c.post("/experiments/new", data={"account_id": ACC, "variable": "commentary", "mode": "observed",
                                             "arm_a": "no", "arm_b": "yes", "hypothesis": "commentary helps",
                                             "outcome": "shares_per_1k_views", "actor": "bill"}, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        r = c.get("/experiments")
        self.assertEqual(r.status_code, 200)
        self.assertIn("commentary helps", r.text)


if __name__ == "__main__":
    unittest.main()
