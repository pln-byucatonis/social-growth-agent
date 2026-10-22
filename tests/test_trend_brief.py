"""Monday brief: YouTube breakouts against the channel's own median, market movers with a 7-day move, the weekly build."""
import json
import os
import unittest
from datetime import date, datetime, timedelta, timezone

from pln_agent import trend_brief as tb
from tests.helpers import make

NOW = datetime(2026, 10, 26, 13, 0, tzinfo=timezone.utc)  # a Monday, after 12:00 UTC


def feed(views, start=NOW):
    entries = "".join(
        f"<entry><yt:videoId>v{i}</yt:videoId><title>Video {i}</title><link rel='alternate' href='https://www.youtube.com/shorts/v{i}'/>"
        f"<published>{(start - timedelta(days=i + 2)).isoformat()}</published><media:group><media:community>"
        f"<media:statistics views='{v}'/></media:community></media:group></entry>" for i, v in enumerate(views))
    return ("<feed xmlns='http://www.w3.org/2005/Atom' xmlns:yt='http://www.youtube.com/xml/schemas/2015' "
            f"xmlns:media='http://search.yahoo.com/mrss/'>{entries}</feed>").encode()


POLY = [{"id": "1", "question": "Will the Fed cut rates in December?", "outcomePrices": "[\"0.62\", \"0.38\"]", "oneDayPriceChange": 0.03,
         "oneWeekPriceChange": 0.15, "volume1wk": 900000, "events": [{"slug": "fed-dec"}], "endDate": "2026-12-10T00:00:00Z"},
        {"id": "2", "question": "Will the Fed hold rates in December?", "outcomePrices": "[\"0.35\"]", "oneWeekPriceChange": -0.14,
         "volume1wk": 800000, "events": [{"slug": "fed-dec"}]},
        {"id": "3", "question": "Will Trump win the 2028 election?", "outcomePrices": "[\"0.40\"]", "oneWeekPriceChange": 0.10,
         "volume1wk": 500000, "events": [{"slug": "pres-2028"}]},
        {"id": "4", "question": "Small market", "outcomePrices": "[\"0.50\"]", "oneWeekPriceChange": 0.40, "volume1wk": 100,
         "events": [{"slug": "tiny"}]},
        {"id": "6", "question": "Spread: Bears (-3.5)", "outcomePrices": "[\"0.44\"]", "oneWeekPriceChange": 0.20, "volume1wk": 400000,
         "events": [{"slug": "nfl-ne-chi"}]},
        {"id": "5", "question": "Will the Maple Leafs win the Stanley Cup?", "outcomePrices": "[\"0.20\"]", "oneWeekPriceChange": 0.02,
         "volume1wk": 300000, "events": [{"slug": "cup"}]}]


def kalshi(price):
    return {"markets": [{"ticker": "KXNHLGAME-26OCT26TORBOS-TOR", "event_ticker": "KXNHLGAME-26OCT26TORBOS", "title": "Toronto vs Boston",
                         "yes_sub_title": "Toronto", "last_price_dollars": str(price), "previous_price_dollars": str(price - 0.02),
                         "volume_24h_fp": "200000"},
                        {"ticker": "KXMVECOMBO-1", "title": "combo", "last_price_dollars": "0.5", "volume_24h_fp": "9999999"}], "cursor": ""}


def fetcher(price=0.55, views=None):
    views = views or [100, 110, 90, 105, 95, 98, 102, 4000]

    def fetch(url, timeout=15):
        if "youtube.com/feeds" in url:
            return feed(views)
        if "polymarket" in url:
            return json.dumps(POLY).encode()
        if "kalshi" in url:
            return json.dumps(kalshi(price)).encode()
        raise AssertionError(url)
    return fetch


class BriefTests(unittest.TestCase):
    def test_breakouts_against_the_channels_median(self):
        db, _ = make(packs=False)
        tb.collect_youtube(db, fetcher(views=[100, 110, 4000, 105, 95, 98, 4100, 102]))
        out = tb.breakouts(db, NOW)
        self.assertTrue(out)
        self.assertTrue(all(o["title"] == "Video 2" for o in out))  # Video 6 is as big, but older than the last 7 days
        o = out[0]
        self.assertGreaterEqual(o["x"], 3)
        self.assertTrue(o["short"])
        self.assertTrue(o["brands"])

    def test_movers_seven_day_and_dedupe_and_sensitive(self):
        db, _ = make(packs=False)
        tb.snapshot_markets(db, fetcher(price=0.40), date(2026, 10, 19))
        tb.snapshot_markets(db, fetcher(price=0.55), date(2026, 10, 26))
        ms = tb.movers(db, date(2026, 10, 26))
        titles = [m["title"] for m in ms]
        self.assertIn("Will the Fed cut rates in December?", titles)
        self.assertNotIn("Will the Fed hold rates in December?", titles)  # same event: one storyline
        self.assertNotIn("Small market", titles)  # not enough money in it
        self.assertNotIn("Spread: Bears (-3.5)", titles)  # a betting line, not a storyline
        self.assertNotIn("Will the Maple Leafs win the Stanley Cup?", titles)  # didn't move
        tor = next(m for m in ms if m["source"] == "kalshi")
        self.assertEqual((tor["move"], tor["window"], tor["category"], tor["show"]), (15, "7d", "hockey", "Five Hole"))
        self.assertFalse(any(m["title"] == "combo" for m in ms))
        pres = next(m for m in ms if "Trump" in m["title"])
        self.assertTrue(pres["politics"])  # kept, marked, last
        self.assertEqual(ms[-1]["title"], pres["title"])

    def test_kalshi_falls_back_to_24h_without_history(self):
        db, _ = make(packs=False)
        os.environ  # noqa: B018
        tb.snapshot_markets(db, fetcher(price=0.55), date(2026, 10, 26))
        db.execute("UPDATE market_snapshots SET change_1d=0.10 WHERE source='kalshi'")
        tor = [m for m in tb.movers(db, date(2026, 10, 26)) if m["source"] == "kalshi"][0]
        self.assertEqual(tor["window"], "24h")

    def test_kalshi_falls_back_to_events_when_the_list_is_all_combos(self):
        def fetch(url, timeout=15):
            if "/events" in url:
                return json.dumps({"events": [{"event_ticker": "KXNHLGAME-X", "title": "Toronto vs Boston",
                                               "markets": [{"ticker": "KXNHLGAME-X-TOR", "last_price_dollars": "0.6",
                                                            "previous_price_dollars": "0.5", "volume_24h_fp": "1000"}]}]}).encode()
            return json.dumps({"markets": [{"ticker": "KXMVECOMBO-1"}], "cursor": ""}).encode()
        rows = tb.from_kalshi(fetch)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["hint"], rows[0]["title"]), ("hockey", "Toronto vs Boston"))
        self.assertAlmostEqual(rows[0]["change_1d"], 0.1)

    def test_run_due_builds_on_monday_once(self):
        db, _ = make(packs=False)
        os.environ["PLN_TREND_BRIEF_DISABLED"] = "0"
        try:
            sunday = NOW - timedelta(days=1)
            r = tb.run_due(db, fetcher(), now=sunday.replace(hour=13))
            self.assertIn("brief", r)  # Sunday is the end of last week's Monday-week: it gets its brief
            monday_early = NOW.replace(hour=9)
            r = tb.run_due(db, fetcher(), now=monday_early)
            self.assertNotIn("brief", r)  # before 8 am ET on Monday
            r = tb.run_due(db, fetcher(), now=NOW)
            self.assertEqual(r.get("brief"), "2026-10-26")
            self.assertNotIn("brief", tb.run_due(db, fetcher(), now=NOW + timedelta(hours=2)))
            b = tb.latest(db)
            self.assertEqual(b["week_start"], "2026-10-26")
            self.assertTrue(b["items"]["clip"])
        finally:
            os.environ["PLN_TREND_BRIEF_DISABLED"] = "1"

    def test_llm_lines_and_page(self):
        from fastapi.testclient import TestClient
        from pln_agent.web import app as web
        db, s = make(packs=False)
        tb.collect(db, fetcher(views=[100, 110, 4000, 105, 95, 98, 90, 102]), NOW.date())
        llm = lambda *a, **k: json.dumps({"lines": ["**Copy** the reveal-then-react structure"]})  # noqa: E731
        b = tb.build(db, NOW, llm=llm)
        self.assertEqual(b["make"][0]["idea"], "Copy the reveal-then-react structure")
        web._db, web.settings = db, s
        r = TestClient(web.app).get("/trends")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Monday brief", r.text)
        self.assertIn("Will the Fed cut rates in December?", r.text)
        self.assertIn("reveal-then-react", r.text)


if __name__ == "__main__":
    unittest.main()
