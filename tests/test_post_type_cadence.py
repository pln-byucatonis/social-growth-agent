"""Post-type tests (carousel vs Reel, Story alongside), peer-informed cadence and timing tests."""
import unittest
from datetime import date, datetime, timedelta, timezone

from pln_agent import growth_lab as lab
from pln_agent import peer_cadence
from tests.helpers import make

NOW = datetime(2026, 10, 22, 20, 0, tzinfo=timezone.utc)


def ig_account(db):
    from collections import Counter
    vs = [v for v in db.all("variants", "state='in_review'") if v["platform"] == "instagram" and not v.get("post_type")]
    return Counter(v["account_id"] for v in vs).most_common(1)[0][0]  # the Instagram account with the most Reel drafts


def snapshot(db, handle, followers, at, hours, ppw=40, compare_to=("pln",)):
    import uuid
    db.insert("competitor_snapshots", {"snapshot_id": f"cs-{uuid.uuid4().hex[:8]}", "handle": handle, "platform": "instagram", "name": handle,
                                       "captured_at": at.isoformat(), "followers": followers, "media_count": 100,
                                       "profile": {"posts_per_week": ppw, "common_hours_utc": hours, "reels_share": 0.8},
                                       "compare_to": list(compare_to)})


class PostTypeTests(unittest.TestCase):
    def test_carousel_arm_posts_the_carousel_and_the_reel_steps_aside(self):
        db, _ = make()
        acct = ig_account(db)
        e = lab.create(db, acct, "post_type", dict(lab.POST_TYPES), "carousels get shared", "t", outcome="shares_per_1k_views")
        arms = db.all("experiment_arms", "experiment_id=?", [e["experiment_id"]])
        b = [a for a in arms if a["arm"] == "B"]
        a = [x for x in arms if x["arm"] == "A"]
        self.assertTrue(b and a)
        car = db.get("variants", "variant_id", b[0]["variant_id"])
        self.assertEqual(car["post_type"], "carousel")
        reel = db.get("variants", "variant_id", b[0]["applied"]["reel"])
        self.assertEqual(reel["state"], "archived")
        self.assertFalse(db.get("variants", "variant_id", a[0]["variant_id"]).get("post_type"))

    def test_story_arm_measures_the_reel(self):
        db, _ = make()
        acct = ig_account(db)
        e = lab.create(db, acct, "story_companion", dict(lab.STORY), "stories lift reach", "t", outcome="reach")
        b = [a for a in db.all("experiment_arms", "experiment_id=?", [e["experiment_id"]]) if a["arm"] == "B"][0]
        reel = db.get("variants", "variant_id", b["variant_id"])
        self.assertFalse(reel.get("post_type"))
        self.assertEqual(reel["state"], "in_review")
        self.assertEqual(db.get("variants", "variant_id", b["applied"]["story"])["post_type"], "story")

    def test_two_tests_share_an_accounts_drafts(self):
        db, _ = make()
        acct = ig_account(db)
        template = dict(db.all("variants", "account_id=? AND state='in_review'", [acct])[0])
        e1 = lab.create(db, acct, "story_companion", dict(lab.STORY), "x", "t", outcome="reach")  # takes today's drafts
        e2 = lab.create(db, acct, "post_time", {"A": "18:30", "B": "21:30"}, "y", "t", outcome="reach")
        for k in range(6):  # drafts that arrive while both tests run
            db.insert("variants", {**template, "variant_id": f"extra-{k}", "post_type": ""})
        lab.refresh(db)
        n1 = len(db.all("experiment_arms", "experiment_id=?", [e1["experiment_id"]]))
        n2 = len(db.all("experiment_arms", "experiment_id=?", [e2["experiment_id"]]))
        self.assertGreater(n2, 0)  # the second test got drafts too
        ids1 = {a["variant_id"] for a in db.all("experiment_arms", "experiment_id=?", [e1["experiment_id"]])}
        ids2 = {a["variant_id"] for a in db.all("experiment_arms", "experiment_id=?", [e2["experiment_id"]])}
        self.assertFalse(ids1 & ids2)
        self.assertGreater(n1, 0)

    def test_planner_puts_a_timing_arm_at_its_time_only(self):
        from pln_agent.calendar_planner import plan_week
        db, s = make()
        acct = ig_account(db)
        e = lab.create(db, acct, "post_time", {"A": "18:30", "B": "21:30"}, "evening", "t", outcome="reach")
        arms = {a["variant_id"]: a["applied"]["post_time"] for a in db.all("experiment_arms", "experiment_id=?", [e["experiment_id"]])}
        plan = plan_week(db, s, date(2026, 10, 26), days=7, states=("in_review", "approved"), persist=False)
        placed = {p["variant_id"]: p["slot"] for p in plan["placed"] if p["account_id"] == acct}
        for vid, hhmm in arms.items():
            if vid in placed:
                self.assertEqual(placed[vid].strftime("%H:%M"), hhmm)
        others = [t for vid, t in placed.items() if vid not in arms]
        self.assertFalse(any(t.strftime("%H:%M") == "21:30" for t in others))  # the test's time is reserved for it


class PeerCadenceTests(unittest.TestCase):
    def _peers(self, db):
        for h, f0, f1, hours in (("fast", 100000, 103000, [1, 2, 23]), ("slow", 500000, 499000, [16, 17, 15]),
                                 ("mid", 200000, 202000, [1, 0, 12])):
            snapshot(db, h, f0, NOW - timedelta(days=10), hours)
            snapshot(db, h, f1, NOW, hours)

    def test_growth_over_a_week_and_local_hours(self):
        db, _ = make(packs=False)
        self._peers(db)
        ps = {p["handle"]: p for p in peer_cadence.peers(db, NOW)}
        self.assertAlmostEqual(ps["fast"]["growth_pct_week"], 2.1, places=1)
        self.assertLess(ps["slow"]["growth_pct_week"], 0)
        self.assertEqual(ps["fast"]["hours_local"], [21, 22, 19])  # UTC → New York (EDT in October)

    def test_young_history_is_said_not_guessed(self):
        db, _ = make(packs=False)
        snapshot(db, "new", 1000, NOW - timedelta(days=2), [23])
        snapshot(db, "new", 1100, NOW, [23])
        acct = db.all("accounts", "platform='instagram'")[0]["account_id"]
        r = peer_cadence.for_account(db, acct, NOW)
        self.assertIn("not measured yet", r["basis"])
        self.assertIsNone(peer_cadence.peers(db, NOW)[0]["growth_pct_week"])

    def test_plan_tests_once(self):
        db, _ = make()
        self._peers(db)
        acct = ig_account(db)
        r = peer_cadence.for_account(db, acct, NOW)
        self.assertEqual(set(r["growing"]), {"fast", "mid"})
        self.assertTrue(r["timing"])
        b = int(r["timing"]["B"][:2])
        self.assertTrue(all(min(abs(b - int(s[:2])), 24 - abs(b - int(s[:2]))) >= 2 for s in r["slots"]))
        out = peer_cadence.plan_tests(db, acct, "t", NOW)
        self.assertTrue(out["timing"])
        self.assertEqual(len(out["frequency"]), 3)
        again = peer_cadence.plan_tests(db, acct, "t", NOW)
        self.assertIsNone(again["timing"])
        self.assertIsNone(again["frequency"])

    def test_experiments_page_shows_the_card(self):
        from fastapi.testclient import TestClient
        from pln_agent.web import app as web
        db, s = make()
        self._peers(db)
        web._db, web.settings = db, s
        r = TestClient(web.app).get("/experiments")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Cadence &amp; timing from the benchmarks", r.text)
        self.assertIn("Post type (Reel vs carousel)", r.text)  # suggested


if __name__ == "__main__":
    unittest.main()
