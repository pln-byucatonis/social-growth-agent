"""Phase 1: Audience Intelligence, Creative Intelligence Database, Knowledge Engine, adaptive cadence."""
import unittest
from datetime import date

from pln_agent import audience, cadence, knowledge
from pln_agent.creative_db import evidence_class, permutation_p, upsert_agent_post, what_works
from pln_agent.db import DB

from tests import fixtures_social
from tests.helpers import CJ, make, test_db_url

CJ_IG = "carjitsu.instagram.carjitsu_championship"
PLN_IG = "pln.instagram.proleaguenetwork"


class Base(unittest.TestCase):
    def setUp(self):
        self.db, self.s = make(packs=True)
        self.ops = DB(test_db_url(), init=False)
        self.n_posts = fixtures_social.build(self.ops, self.db)
        self.report = audience.import_ops_social(self.db, self.ops, self.s.timezone)


class AudienceIntelligenceTests(Base):
    def test_import_is_idempotent_and_skips_unreported_fields(self):
        n1 = self.db.one("SELECT COUNT(*) AS n FROM metrics")["n"]
        again = audience.import_ops_social(self.db, self.ops, self.s.timezone)
        self.assertEqual(self.db.one("SELECT COUNT(*) AS n FROM metrics")["n"], n1)
        self.assertEqual(again["posts"], self.n_posts)
        self.assertEqual(self.db.one("SELECT COUNT(*) AS n FROM posts")["n"], self.n_posts)
        # Instagram reports impressions as 0 everywhere → "not reported", never imported as measured zeros
        self.assertIsNone(self.db.one("SELECT id FROM metrics WHERE canonical_metric='impressions'"))
        self.assertIn(f"{CJ_IG}: impressions", self.report["not_reported"])

    def test_kpis_label_measured_derived_and_unavailable(self):
        k = audience.account_kpis(self.db, CJ_IG, days=28)
        self.assertEqual(k["kpis"]["shares_per_1k_views"]["basis"], "measured")
        self.assertEqual(k["kpis"]["follows_per_1k_views"]["basis"], "derived")
        self.assertIn("not per-post attribution", k["kpis"]["follows_per_1k_views"]["how"])
        self.assertEqual(k["kpis"]["non_follower_reach"]["basis"], "unavailable")
        self.assertIsNone(k["kpis"]["non_follower_reach"]["value"])  # never estimated
        self.assertEqual(k["kpis"]["returning_viewers"]["basis"], "unavailable")
        self.assertGreater(k["net_follower_change"], 0)
        self.assertGreater(k["kpis"]["follows_per_1k_views"]["value"], 0)

    def test_breakdowns_by_brand_and_content_type(self):
        by_brand = {r["group"]: r for r in audience.breakdown(self.db, "brand_id")}
        self.assertEqual(set(by_brand), {"carjitsu", "pln"})
        by_type = {r["group"]: r for r in audience.breakdown(self.db, "content_type", [CJ_IG])}
        self.assertGreater(by_type["submission_highlight"]["median_shares_per_1k_views"],
                           by_type["announcement"]["median_shares_per_1k_views"] * 2)

    def test_overlap_questions_are_explicitly_not_measurable(self):
        self.assertTrue(all(q["measurable"] is not True for q in audience.OVERLAP_QUESTIONS))


class CreativeIntelligenceTests(Base):
    def test_ingested_posts_only_claim_what_the_caption_shows(self):
        p = self.db.all("posts", "account_id=?", [CJ_IG])[0]
        self.assertEqual(p["origin"], "ops_ingested")
        self.assertEqual(p["provenance"]["content_type"], "inferred_from_caption")
        self.assertEqual(p["creative"]["opening_style"], "unknown")
        self.assertEqual(p["provenance"]["opening_style"], "unknown")
        self.assertIn(p["creative"]["posting_hour_bucket"], ("afternoon", "evening"))

    def test_what_works_finds_the_planted_effects_with_a_stated_rule(self):
        r = what_works(self.db, "carjitsu", "instagram", "shares_per_1k_views")
        sub = next(f for f in r["findings"] if f["variable"] == "content_type" and f["value"] == "submission_highlight")
        self.assertEqual(sub["evidence"], "conclusive")
        self.assertGreater(sub["lift"], 1.0)
        q = what_works(self.db, "carjitsu", "instagram", "comments_per_1k_views")
        ask = next(f for f in q["findings"] if f["variable"] == "asks_question" and f["value"] == "yes")
        self.assertEqual(ask["evidence"], "conclusive")
        self.assertIn("insufficient", r["rule"])

    def test_small_samples_are_insufficient_not_guessed(self):
        self.assertEqual(evidence_class(3, 40, 0.001), "insufficient")
        self.assertEqual(evidence_class(6, 6, 0.1), "suggestive")
        self.assertAlmostEqual(permutation_p([1, 2, 3], [1, 2, 3]), 1.0, places=2)

    def test_agent_published_post_records_its_creative_decisions(self):
        v = self.db.get("variants", "variant_id", f"{CJ}:carjitsu.tiktok.carjitsuchampionship")
        rec = upsert_agent_post(self.db, v, "7300000000000000001", "https://www.tiktok.com/@x/video/1",
                                "2026-10-21T23:30:00+00:00", self.s.timezone)
        self.assertEqual(rec["origin"], "agent_published")
        self.assertEqual(rec["provenance"]["hook_style"], "agent_decision")
        self.assertEqual(rec["creative"]["posting_hour_bucket"], "evening")
        self.assertTrue(rec["source_footage"]["segments"])
        self.assertIsNotNone(rec["duration_s"])


class KnowledgeEngineTests(Base):
    def setUp(self):
        super().setUp()
        knowledge.seed(self.db)

    def test_seed_is_attributed_dated_and_classified(self):
        rows = self.db.all("knowledge")
        self.assertTrue(rows)
        for e in rows:
            self.assertIn(e["level"], knowledge.LEVELS)
            self.assertTrue(e["source_date"])
            if e["level"] in ("platform_rule", "platform_guidance"):
                self.assertTrue(e["source_url"].startswith("https://"), e["entry_id"])
                self.assertTrue(e["quote"], e["entry_id"])
        self.assertFalse([e for e in rows if e["level"] == "pln_evidence"])  # never seeded: only measured

    def _fake(self, pages):
        def fetch(url):
            body = pages.get(url)
            return (200, body) if body is not None else (0, "")
        return fetch

    def test_review_flags_missing_quotes_and_unreachable_pages_only(self):
        entries = {e["entry_id"]: e for e in self.db.all("knowledge")}
        yt = entries["yt-shorts-no-minimum-cadence"]
        fb = entries["meta-facebook-originality-2026"]
        pages = {}
        for e in entries.values():
            if e["source_url"]:
                pages[e["source_url"]] = pages.get(e["source_url"], "<html><body>") + f"<p>{e['quote']}</p>"
        r = knowledge.review_sources(self.db, self._fake(pages))
        self.assertFalse(r["quote_missing"] or r["unreachable"])
        pages[yt["source_url"]] = "<p>Totally new advice: post 9 times a day</p>"
        pages.pop(fb["source_url"])
        skipped = knowledge.review_sources(self.db, self._fake(pages))
        self.assertEqual(skipped["checked"], 0)  # reviewed this week already
        r = knowledge.review_sources(self.db, self._fake(pages), force=True)
        self.assertIn(yt["entry_id"], r["quote_missing"])
        self.assertIn(fb["source_url"], r["unreachable"])
        self.assertEqual(self.db.get("knowledge", "entry_id", yt["entry_id"])["status"], "needs_review")
        self.assertEqual(self.db.get("knowledge", "entry_id", fb["entry_id"])["status"], "needs_review")
        self.assertEqual(self.db.get("knowledge", "entry_id", "linkedin-pages-weekly-posting")["status"], "current")
        # the statement itself is never rewritten by the engine
        self.assertEqual(self.db.get("knowledge", "entry_id", yt["entry_id"])["statement"], yt["statement"])

    def test_manual_entries_need_sources_and_cannot_fake_pln_evidence(self):
        with self.assertRaises(ValueError):
            knowledge.add_entry(self.db, {"level": "platform_guidance", "statement": "Post 3x a day"}, "a")
        with self.assertRaises(ValueError):
            knowledge.add_entry(self.db, {"level": "pln_evidence", "statement": "x", "publisher": "me"}, "a")
        e = knowledge.add_entry(self.db, {"level": "external_research", "statement": "Short clips do well",
                                          "source_url": "https://example.org/study", "source_date": "2026-09-01",
                                          "publisher": "Example Research"}, "by@pln")
        self.assertEqual(e["status"], "current")

    def test_pln_evidence_is_derived_dated_and_superseded(self):
        out = knowledge.derive_pln_evidence(self.db, [{"brand_id": "carjitsu", "platform": "instagram",
                                                        "outcome": "shares_per_1k_views"}])
        self.assertTrue(any("content-type-submission-highlight" in e for e in out["written"]))
        e = self.db.get("knowledge", "entry_id", [x for x in out["written"] if "submission-highlight" in x][0])
        self.assertEqual(e["level"], "pln_evidence")
        self.assertEqual(e["evidence"]["evidence"], "conclusive")
        self.assertEqual(e["source_date"], e["retrieved_at"])
        # evidence disappears (posts re-labelled) → superseded, not deleted
        self.db.execute("DELETE FROM posts WHERE account_id=?", [CJ_IG])
        out2 = knowledge.derive_pln_evidence(self.db, [{"brand_id": "carjitsu", "platform": "instagram",
                                                         "outcome": "shares_per_1k_views"}])
        self.assertIn(e["entry_id"], out2["superseded"])
        self.assertEqual(self.db.get("knowledge", "entry_id", e["entry_id"])["status"], "superseded")

    def test_guidance_precedence(self):
        knowledge.derive_pln_evidence(self.db, [{"brand_id": "carjitsu", "platform": "instagram", "outcome": "shares_per_1k_views"}])
        g = knowledge.guidance(self.db, platform="instagram", brand_id="carjitsu")
        levels = [e["level"] for e in g]
        self.assertEqual(levels, sorted(levels, key=knowledge.LEVELS.index))
        self.assertEqual(levels[0], "platform_rule")


class CadenceTests(Base):
    def test_starting_cadence_is_configurable_and_labelled_a_hypothesis(self):
        acc = self.db.get("accounts", "account_id", "carjitsu.tiktok.carjitsuchampionship")
        lo, hi, src = cadence.starting(acc)
        self.assertEqual((lo, hi), (14.0, 21.0))
        r = cadence.recommend(self.db, acc["account_id"])
        self.assertEqual(r["action"], "collect_baseline")
        self.assertIn("hypothesis", r["rationale"])

    def test_increase_where_extra_posts_add_followers(self):
        for i in range(20):  # give it enough drafts that supply doesn't cap the recommendation
            self.db.insert("variants", {"variant_id": f"extra-{i}", "asset_id": "a", "account_id": CJ_IG,
                                        "platform": "instagram", "brand_id": "carjitsu", "state": "in_review"})
        r = cadence.recommend(self.db, CJ_IG)
        self.assertEqual(r["action"], "increase", r["rationale"])
        self.assertGreater(r["evidence"]["followers_per_extra_post"], 25)
        self.assertLessEqual(r["recommended_per_week"], r["start_max"] * 1.5)

    def test_no_gain_and_dilution_means_dont_add_volume(self):
        for i in range(20):
            self.db.insert("variants", {"variant_id": f"pln-{i}", "asset_id": "a", "account_id": PLN_IG,
                                        "platform": "instagram", "brand_id": "pln", "state": "in_review"})
        r = cadence.recommend(self.db, PLN_IG)
        self.assertIn(r["action"], ("decrease", "hold"), r["rationale"])
        self.assertGreater(r["evidence"]["engagement_dilution"], 0.2)

    def test_supply_caps_the_recommendation(self):
        r = cadence.recommend(self.db, CJ_IG)
        drafts = r["evidence"]["drafts_on_hand"]
        if drafts < 9:
            self.assertEqual(r["action"], "production_limited")
            self.assertEqual(r["recommended_per_week"], float(drafts))

    def test_planner_respects_the_weekly_target(self):
        from pln_agent.calendar_planner import plan_week
        acc = "carjitsu.tiktok.carjitsuchampionship"
        self.db.insert("cadence", {"account_id": acc, "recommended_per_week": 0, "action": "decrease"},
                       upsert_key="account_id")
        plan = plan_week(self.db, self.s, date(2026, 10, 21), persist=False)
        self.assertFalse([p for p in plan["placed"] if p["account_id"] == acc])
        self.assertTrue(any("cadence target" in u["reason"] for u in plan["unplaced"]))


class DashboardTests(Base):
    def test_intelligence_pages_render(self):
        from fastapi.testclient import TestClient
        from pln_agent.web import app as web
        knowledge.seed(self.db)
        cadence.recommend_all(self.db)
        web._db = self.db
        c = TestClient(web.app)
        for path, needle in (("/intelligence", "Followers gained per 1,000 views"), ("/creative", "evidence rule"),
                             ("/knowledge", "Platform rule"), ("/cadence", "starting")):
            r = c.get(path)
            self.assertEqual(r.status_code, 200, path)
            self.assertIn(needle, r.text, path)
        r = c.get("/creative?brand=carjitsu&platform=instagram&outcome=shares_per_1k_views")
        self.assertIn("conclusive", r.text)


if __name__ == "__main__":
    unittest.main()
