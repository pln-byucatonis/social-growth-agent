"""Trend Scout: signals from each source → trend cards (formats + topics) → applying one to a real clip."""
import json
import unittest
from unittest import mock

from pln_agent import trend_scout
from pln_agent.db import now_iso

from tests.helpers import CJ, make

REDDIT = {"data": {"children": [
    {"data": {"title": "Wait for it... the craziest finish of the year", "permalink": "/r/bjj/1", "ups": 5400}},
    {"data": {"title": "POV: you thought you had the back", "permalink": "/r/bjj/2", "ups": 2100}},
    {"data": {"title": "Rate this submission 1-10", "permalink": "/r/bjj/3", "ups": 900}},
    {"data": {"title": "nsfw thing", "permalink": "/r/bjj/4", "ups": 99999, "over_18": True}},
]}}
RSS = b"""<?xml version="1.0"?><rss xmlns:ht="https://trends.google.com/trending/rss"><channel>
<item><title>ufc 310</title><ht:approx_traffic>200,000+</ht:approx_traffic><link>https://trends.google.com/x</link></item>
<item><title>golf hole in one record</title><ht:approx_traffic>50,000+</ht:approx_traffic></item>
<item><title>election results</title><ht:approx_traffic>2,000,000+</ht:approx_traffic></item>
</channel></rss>"""


def fake_fetch(url, timeout=15):
    if "reddit.com" in url:
        return json.dumps(REDDIT).encode()
    if "trends.google.com" in url:
        return RSS
    raise OSError("offline")


class TrendScoutTests(unittest.TestCase):
    def setUp(self):
        self.db, self.s = make(packs=True)
        self.db.insert("competitor_snapshots", {"snapshot_id": "c1", "handle": "@rival", "platform": "instagram", "captured_at": now_iso(),
                                                "profile": {"standouts": [{"hook": "Wait till the end 😳", "er": 0.08}]}})

    def test_collect_and_build_cards(self):
        st = trend_scout.collect(self.db, fake_fetch)
        self.assertTrue(st["reddit"]["ok"] and st["google_trends"]["ok"])
        self.assertFalse(st["youtube"]["ok"])  # no API key: reported, not fatal
        self.assertTrue(st["competitors"]["ok"])
        texts = [s["text"] for s in self.db.all("trend_signals")]
        self.assertNotIn("nsfw thing", texts)
        cards = {c["card_id"]: c for c in trend_scout.build_cards(self.db)}
        self.assertIn("format:wait_for_it", cards)  # reddit + competitor
        self.assertEqual(cards["format:wait_for_it"]["sightings"], len([t for t in texts if "wait" in t.lower()]))
        self.assertIn("format:pov", cards)
        self.assertIn("format:rate_it", cards)
        fit = cards["format:wait_for_it"]["fits"]["putt_tour"]
        self.assertGreater(fit["score"], 0.9)
        self.assertTrue(fit["hook"])
        self.assertTrue(any(r.startswith("Use licensed") for r in cards["format:pov"]["risk"]))
        topics = [c for c in cards.values() if c["kind"] == "topic"]
        self.assertTrue(any("ufc" in c["title"].lower() for c in topics))
        self.assertFalse(any("election" in json.dumps(c["signals"]).lower() for c in topics))  # blocked topic

    def test_llm_sharpens_hooks_but_never_names(self):
        trend_scout.collect(self.db, fake_fetch)

        def llm(system, messages, max_tokens):
            return json.dumps({"cards": [{"card_id": "format:pov", "why_now": "everywhere this week",
                                          "fits": {"carjitsu": {"score": 0.95, "hook": "pov: the seatbelt is locked"},
                                                   "putt_tour": {"score": 0.4, "hook": "Putt Tour pov"}}}]})
        cards = {c["card_id"]: c for c in trend_scout.build_cards(self.db, llm=llm)}
        pov = cards["format:pov"]
        self.assertEqual(pov["fits"]["carjitsu"]["hook"], "POV: THE SEATBELT IS LOCKED")
        self.assertNotIn("PUTT TOUR", pov["fits"]["putt_tour"]["hook"])  # brand name rejected, rule-based hook kept
        self.assertEqual(pov["fits"]["putt_tour"]["score"], 0.4)
        self.assertEqual(pov["why_now"], "everywhere this week")

    def test_apply_puts_the_hook_on_a_clip_and_queues_render(self):
        trend_scout.collect(self.db, fake_fetch)
        trend_scout.build_cards(self.db)
        self.db.insert("scout_moments", {"clip_id": "cj.mp4:1-9", "source_id": "cj.mp4", "brand_id": "carjitsu", "caption": "a choke",
                                         "potential": 90, "status": "candidate", "start_sec": 0, "end_sec": 8, "categories": []})
        with mock.patch("pln_agent.ops_sources.clip_asset_id", lambda cid: CJ):
            r = trend_scout.apply(self.db, "format:rate_it", "carjitsu", "bill", None, self.s)
        v = self.db.get("variants", "variant_id", r["variant_id"])
        self.assertEqual(v["onscreen_text"][0]["text"], r["hook"])
        self.assertEqual(v["render_status"], "queued")
        self.assertEqual(v["treatment"]["trend"], "format:rate_it")
        self.assertTrue(v["treatment"]["overlay"]["hook_custom"])
        self.assertIn("Drop your rating", v["caption"])
        card = self.db.get("trend_cards", "card_id", "format:rate_it")
        self.assertEqual(card["status"], "applied")
        self.assertEqual(card["applied"][0]["variant_id"], r["variant_id"])

    def test_manual_lines_and_page(self):
        from fastapi.testclient import TestClient
        from pln_agent.web import app as web
        n = trend_scout.add_manual(self.db, "#povchallenge\nwait for the end\nx", "bill")
        self.assertEqual(n, 2)
        cards = trend_scout.build_cards(self.db)
        self.assertTrue(any(c["card_id"] == "format:wait_for_it" for c in cards))
        web._db = self.db
        c = TestClient(web.app)
        r = c.get("/trends")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Wait for it", r.text)
        self.assertIn("Apply to a clip", r.text)


if __name__ == "__main__":
    unittest.main()
