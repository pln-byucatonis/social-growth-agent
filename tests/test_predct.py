"""PREDCT curation queue: footage found, a person picks the moment, the team cuts and drafts under PREDCT's rules."""
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from pln_agent import predct, writers_room
from tests.helpers import make

HAS_FFMPEG = bool(shutil.which("ffmpeg"))
REC = "marketzone-host-video-975050328168"
NOTE = "Marcus can't believe the Chiefs are only 58% to win at home; Jordan says the market has it right."


def lister(bucket, prefix):
    if prefix == "encoder-copy/":
        return [{"key": f"encoder-copy/engine-mz/20261013-145315/20261013-1453{m:02d}.ts", "size": 1, "modified": "2026-10-13T14:53:00+00:00"}
                for m in (16, 27)] + [{"key": "encoder-copy/engine-mz/20261013-143146/20261013-143146.ts", "size": 1,
                                       "modified": "2026-10-13T14:31:00+00:00"}, {"key": "encoder-copy/junk.txt", "size": 1, "modified": ""},
                                      {"key": "encoder-copy/copy-test/20261012-140200/20261012-140200.ts", "size": 1, "modified": ""}]
    if prefix == "films/five-hole/":
        return [{"key": "films/five-hole/ep1/v1.webm", "size": 1, "modified": "2026-10-20T00:00:00+00:00"}]
    return []


def add_predct_account(db):
    row = dict(db.get("accounts", "account_id", "pln.facebook.pro_league_network"))
    row.update({"account_id": "pln_picks.facebook.pln_picks", "brand_id": "pln_picks", "canonical_brand_id": "pln_picks",
                "username_original": "PLN Picks", "username_normalized": "pln_picks", "source_brand_name": "PLN Picks"})
    db.insert("accounts", row)


def _clip(seconds=40):
    out = Path(tempfile.mkdtemp()) / "src.ts"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=size=1280x720:rate=25:duration={seconds}",
                    "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}", "-c:v", "libx264", "-c:a", "aac", str(out)], check=True)
    return out


class ScanPickTests(unittest.TestCase):
    def test_recording_sessions_and_files(self):
        db, _ = make(packs=False)
        r = predct.scan(db, lister)
        self.assertEqual(len(r["new"]), 3)  # two recording sessions + one film
        rows = {c["cand_id"]: c for c in db.all("predct_candidates")}
        sess = [c for c in rows.values() if c["kind"] == "recording" and len(c["sources"]) == 2][0]
        self.assertEqual(sess["show"], "marketzone_live")
        self.assertEqual(sess["duration_s"], 120.0)
        self.assertTrue(sess["sources"][0] < sess["sources"][1])
        self.assertIn("2026-10-13 14:53", sess["title"])
        film = [c for c in rows.values() if c["kind"] == "file"][0]
        self.assertEqual(film["show"], "five_hole")
        self.assertEqual(predct.scan(db, lister)["new"], [])  # idempotent
        self.assertTrue(predct.pending(db))  # previews to make

    def test_pick_rules(self):
        db, _ = make(packs=False)
        cid = predct.scan(db, lister)["new"][0]
        with self.assertRaises(predct.CurationError):
            predct.pick(db, cid, 10, 30, "too short", "c")
        with self.assertRaises(predct.CurationError):
            predct.pick(db, cid, 10, 30, "Jordan says this is a lock of the night, bet now on the Chiefs", "c")
        with self.assertRaises(predct.CurationError):
            predct.pick(db, cid, 10, 12, NOTE, "c")  # under 5 s
        with self.assertRaises(predct.CurationError):
            predct.pick(db, cid, 10, 30, NOTE, "c", payoff_at=40)
        r = predct.pick(db, cid, 10, 30, NOTE, "curator", payoff_at=22)
        c = db.get("predct_candidates", "cand_id", cid)
        self.assertEqual(c["status"], "picked")
        self.assertEqual(c["picks"][0]["pick_id"], r["pick_id"])
        self.assertTrue(predct.pending(db))

    def test_never_said(self):
        self.assertEqual(predct.never_said("Hammer the over? No. Sign-up bonus? No."), ["hammer"])
        self.assertEqual(predct.never_said("Tail this pick, it's a LOCK OF THE week"), ["lock of the", "a lock", "tail this"])
        self.assertEqual(predct.never_said("The market thinks it's close."), [])


@unittest.skipUnless(HAS_FFMPEG, "ffmpeg not installed")
class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.src = _clip()
        self._fetch, self._put = predct._fetch, predct._put
        self.stored = {}
        predct._fetch = lambda paths, work: self.src
        predct._put = lambda f, bucket, key, ctype: (self.stored.__setitem__(key, Path(f).stat().st_size), f"s3://{bucket}/{key}")[1]

    def tearDown(self):
        predct._fetch, predct._put = self._fetch, self._put

    def test_preview_then_pick_makes_predct_drafts_only(self):
        db, s = make(packs=False)
        add_predct_account(db)
        cid = predct.scan(db, lister)["new"][0]
        predct.run_pending(db, s, "renders", limit=1)
        c = db.get("predct_candidates", "cand_id", cid)
        self.assertEqual(c["preview_status"], "ready")
        self.assertAlmostEqual(c["duration_s"], 40, delta=1)
        self.assertTrue(c["preview_path"].endswith("preview.mp4"))
        predct.pick(db, cid, 10, 30, NOTE, "curator", payoff_at=22)
        out = predct.run_pending(db, s, "renders", limit=0)
        made = [o for o in out if o.get("asset_id")][0]
        a = db.get("assets", "asset_id", made["asset_id"])
        self.assertEqual(a["brand_id"], "pln_picks")
        self.assertTrue(a["flags"]["gambling"] and a["flags"]["ai_generated_person"])
        self.assertAlmostEqual(a["duration_s"], 20, delta=0.6)
        self.assertEqual(a["facts"][0]["text"], NOTE)
        self.assertTrue(made["variants"], made.get("notes"))
        self.assertTrue(all(v.startswith(a["asset_id"] + ":pln_picks.") for v in made["variants"]), made["variants"])  # no PLN amplification
        v = db.get("variants", "variant_id", made["variants"][0])
        self.assertFalse(v["writers_status"])  # focus disabled in tests; in production curated accounts go to the room
        self.assertIn("gambling", v["risk_flags"]["review_flags"])
        c = db.get("predct_candidates", "cand_id", cid)
        self.assertEqual(c["picks"][0]["status"], "done")
        self.assertFalse(predct.pending(db))


class RulesTests(unittest.TestCase):
    def _asset(self, db):
        c = {"cand_id": "pc-x", "show": "marketzone_live", "title": "MarketZone live", "recorded_at": "2026-10-13T14:53:00+00:00"}
        p = {"pick_id": "x-1", "start": 0, "end": 20, "note": NOTE, "payoff_at": 12, "by": "c"}
        row = predct.asset_row(c, p, "s3://b/m.mp4", {"duration": 20.0, "width": 1280, "height": 720, "has_audio": True})
        db.insert("assets", row)
        return row

    def test_wagering_language_fails_validation(self):
        from pln_agent.strategists.base import common_violations, CreativeContext
        db, _ = make(packs=False)
        a = self._asset(db)
        ctx = CreativeContext(asset=a, account={}, brand={}, platform="facebook", route_role="canonical", display_brand="PREDCT")
        self.assertTrue(any("wagering" in e for e in common_violations("Bet now — this is free money", ctx)))
        self.assertFalse(any("wagering" in e for e in common_violations("The market thinks it's a coin flip.", ctx)))

    def test_writers_room_predct_rules(self):
        db, _ = make(packs=False)
        add_predct_account(db)
        a = self._asset(db)
        from pln_agent.editorial import generate_pack
        vid = generate_pack(db, a["asset_id"], make(packs=False)[1])["variants"][0]["variant_id"]
        hooks = ["THAT LOW AT HOME?", "THE MARKET SAYS CLOSE", "HAMMER THE CHIEFS", "A COIN FLIP AT HOME", "WHO'S RIGHT HERE?"]

        def call(system, messages, max_tokens=0):
            if system.startswith("You score"):
                return json.dumps({"scores": [{"i": i, "hook": 9, "specific": 9, "accurate": 9, "payoff": 9} for i in range(5)]})
            self.assertIn("Never use any of", messages[0]["content"])  # PREDCT voice and never-say list reach the writers
            caps = ["Only 58% at home? Who's right?", "The market has the Chiefs at 61% — is it?", "Easy.", "Close one. Thoughts?", "Who wins?"]
            return json.dumps({"drafts": [{"angle": "x", "hook": h, "caption": c, "cta": "Tell us"} for h, c in zip(hooks, caps)]})
        room = writers_room.write(db, vid, llm=call)
        sc = [d["score"] for d in room["drafts"]]
        self.assertGreater(sc[0], 0)  # 58 is in the curator's note
        self.assertEqual(sc[1], 0.0)  # 61 is not
        self.assertEqual(sc[2], 0.0)  # tout language
        self.assertIn("hammer", room["drafts"][2]["note"])


class PageTests(unittest.TestCase):
    def test_page_and_pick(self):
        from fastapi.testclient import TestClient
        from pln_agent.web import app as web
        db, s = make(packs=False)
        web._db, web.settings = db, s
        cid = predct.scan(db, lister)["new"][0]
        db.update("predct_candidates", "cand_id", cid, {"preview_status": "ready", "preview_path": "s3://b/p.mp4", "duration_s": 60})
        c = TestClient(web.app)
        r = c.get("/predct")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Pick this moment", r.text)
        self.assertIn("MarketZone live", r.text)
        r = c.post(f"/predct/{cid}/pick", data={"start": "5", "end": "25", "note": NOTE, "payoff_at": ""}, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertIn("Picked", r.headers["location"].replace("+", " "))
        r = c.post(f"/predct/{cid}/pick", data={"start": "5", "end": "25", "note": "bet now on this one, free money"}, follow_redirects=False)
        self.assertIn("err=", r.headers["location"])
        r = c.post("/predct/add", data={"show": "bottle_jobs", "link": "s3://bucket/films/bottle-jobs/ep.mp4"}, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertEqual(len(db.all("predct_candidates", "show='bottle_jobs'")), 1)


if __name__ == "__main__":
    unittest.main()
