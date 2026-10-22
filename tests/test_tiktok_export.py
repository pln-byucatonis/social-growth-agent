"""TikTok Studio export upload: tolerant parsing of Overview / Followers / Content files, idempotent save."""
import io
import unittest
import zipfile
from datetime import date

from pln_agent import tiktok_export as tx
from tests.helpers import make

TODAY = date(2026, 10, 22)
OVERVIEW = b"\xef\xbb\xbfDate,Video Views,Profile Views,Likes,Comments,Shares\nOctober 20,\"12,400\",310,880,41,57\nOctober 21,9.1K,250,700,30,44\n"
FOLLOWERS = b"Date,Followers,Difference in followers from previous day\n2026-10-20,15210,34\n2026-10-21,15260,50\n"
CONTENT = (b"Time,Video title,Video link,Post time,Total likes,Total comments,Total shares,Total views\n"
           b"2026-10-21,Seat belt escape,https://www.tiktok.com/@carjitsu/video/7429912345678901234,2026-10-19,1200,80,95,40100\n"
           b"2026-10-21,broken row,,2026-10-18,1,1,1,1\n")


def _xlsx(rows):
    """A minimal .xlsx with inline strings (what spreadsheet apps write is read the same way)."""
    def cell(ci, ri, v):
        ref = chr(65 + ci) + str(ri)
        return f'<c r="{ref}"><v>{v}</v></c>' if isinstance(v, (int, float)) else f'<c r="{ref}" t="inlineStr"><is><t>{v}</t></is></c>'
    body = "".join(f'<row r="{ri}">' + "".join(cell(ci, ri, v) for ci, v in enumerate(r)) + "</row>" for ri, r in enumerate(rows, 1))
    sheet = ('<?xml version="1.0"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
             + body + "</sheetData></worksheet>")
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("xl/worksheets/sheet1.xml", sheet)
    return b.getvalue()


class ParseTests(unittest.TestCase):
    def test_overview_daily(self):
        p = tx.parse("Overview.csv", OVERVIEW, TODAY)
        self.assertEqual(p["kind"], "daily")
        self.assertEqual(p["rows"][0], {"day": "2026-10-20", "metrics": {"video_views": 12400.0, "profile_views": 310.0, "likes": 880.0,
                                                                        "comments": 41.0, "shares": 57.0}})
        self.assertEqual(p["rows"][1]["metrics"]["video_views"], 9100.0)

    def test_followers_net_change_is_not_followers(self):
        p = tx.parse("FollowerHistory.csv", FOLLOWERS, TODAY)
        self.assertEqual(p["rows"][1]["metrics"], {"follower_count": 15260.0, "new_followers": 50.0})

    def test_content_per_video(self):
        p = tx.parse("Content.csv", CONTENT, TODAY)
        self.assertEqual(p["kind"], "videos")
        self.assertEqual(len(p["rows"]), 1)
        r = p["rows"][0]
        self.assertEqual((r["video_id"], r["posted"]), ("7429912345678901234", "2026-10-19"))
        self.assertEqual(r["metrics"]["video_views"], 40100.0)
        self.assertEqual(len(p["skipped"]), 1)

    def test_xlsx(self):
        data = _xlsx([["Date", "Video Views", "Likes"], ["October 21", 5000, 300]])
        p = tx.parse("Overview.xlsx", data, TODAY)
        self.assertEqual(p["rows"], [{"day": "2026-10-21", "metrics": {"video_views": 5000.0, "likes": 300.0}}])

    def test_year_less_date_in_the_future_means_last_year(self):
        p = tx.parse("Overview.csv", b"Date,Video Views\nDecember 30,10\n", TODAY)
        self.assertEqual(p["rows"][0]["day"], "2025-12-30")

    def test_unrecognised_file(self):
        with self.assertRaises(tx.ExportError):
            tx.parse("x.csv", b"foo,bar\n1,2\n", TODAY)


class SaveTests(unittest.TestCase):
    def test_save_is_idempotent_and_makes_posts(self):
        db, _ = make(packs=False)
        acct = db.all("accounts", "platform='tiktok'")[0]["account_id"]
        p = tx.parse("Content.csv", CONTENT, TODAY)
        tx.save(db, acct, p, "t", TODAY)
        tx.save(db, acct, p, "t", TODAY)
        rows = db.all("metrics", "source=?", [tx.SOURCE])
        self.assertEqual(len(rows), 4)
        self.assertEqual({r["canonical_metric"] for r in rows}, {"views", "likes", "comments", "shares"})
        post = db.get("posts", "post_id", "tiktok:7429912345678901234")
        self.assertEqual(post["account_id"], acct)
        self.assertIn("/video/", post["permalink"])
        tx.save(db, acct, tx.parse("Overview.csv", OVERVIEW, TODAY), "t", TODAY)
        self.assertEqual(len(db.all("metrics", "source=? AND platform_post_id IS NULL", [tx.SOURCE])), 10)

    def test_only_tiktok_accounts(self):
        db, _ = make(packs=False)
        ig = db.all("accounts", "platform='instagram'")[0]["account_id"]
        with self.assertRaises(tx.ExportError):
            tx.save(db, ig, tx.parse("Overview.csv", OVERVIEW, TODAY), "t", TODAY)


class PageTests(unittest.TestCase):
    def test_read_then_save(self):
        from fastapi.testclient import TestClient
        from pln_agent.web import app as web
        db, s = make(packs=False)
        web._db = db
        web.settings = s
        c = TestClient(web.app)
        acct = db.all("accounts", "platform='tiktok'")[0]["account_id"]
        self.assertEqual(c.get("/accounts/tiktok-export").status_code, 200)
        r = c.post("/accounts/tiktok-export", data={"account_id": acct}, files={"file": ("Overview.csv", OVERVIEW, "text/csv")})
        self.assertEqual(r.status_code, 200)
        self.assertIn("video_views", r.text)
        import re
        b64 = re.search(r'name="data" value="([^"]+)"', r.text).group(1)
        r = c.post("/accounts/tiktok-export/save", data={"account_id": acct, "filename": "Overview.csv", "data": b64}, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertIn("Saved", r.headers["location"].replace("+", " "))
        self.assertEqual(len(db.all("metrics", "source=?", [tx.SOURCE])), 10)


if __name__ == "__main__":
    unittest.main()
