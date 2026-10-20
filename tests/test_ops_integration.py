"""Ops-portal integration: Data API backend, ops registry/clip import, rights overlay, proxied dashboard."""
import os
import unittest

from pln_agent.db import DB, _RdsData
from pln_agent.ops_sources import clip_asset_id, clip_moments, import_ops_accounts, import_ops_clips, set_asset_rights
from pln_agent.rights import evaluate
from pln_agent.strategists.base import common_violations

from tests.helpers import make, test_db_url

PG = os.environ.get("PLN_TEST_DATABASE_URL")


class FakeRdsClient:
    def __init__(self):
        self.calls = []

    def execute_statement(self, **kw):
        self.calls.append(kw)
        return {"formattedRecords": '[{"n": 1}]'}


class MergePeopleTests(unittest.TestCase):
    def test_tagger_variants_collapse_to_the_ops_name(self):
        from pln_agent.ops_sources import merge_people
        raw = [{"name": "OKUMA 915", "verified": False, "detected_by": "nova"},
               {"name": "RUNT", "verified": False, "detected_by": "nova"},
               {"name": "Okuma 915", "verified": False, "detected_by": "nova"},
               {"name": "Runt", "verified": False, "detected_by": "face"},
               {"name": "person #136", "verified": False, "detected_by": "face"}]
        out = merge_people(raw, {"okuma": {"id": 1294, "name": "Okuma"}})
        self.assertEqual([p["name"] for p in out], ["Okuma", "Runt"])
        self.assertEqual(out[0]["ops_person_id"], 1294)
        self.assertEqual(out[0]["aliases"], ["Okuma 915"])  # "OKUMA 915" folds case-insensitively
        self.assertEqual(out[1]["name"], "Runt")  # face-detected mixed case wins over OCR caps
        self.assertNotIn("aliases", out[1])        # spellings differing only by case are one name
        self.assertEqual(out[1]["detected_by"], "face+nova")

    def test_every_spelling_is_still_withheld_from_copy(self):
        from types import SimpleNamespace
        asset = {"people": [{"name": "Okuma", "aliases": ["OKUMA 915", "Okuma 915"], "verified": False}],
                 "people_verified": False}
        ctx = SimpleNamespace(asset=asset, display_brand="SlapFIGHT", canonical_handle="", grounding_corpus="915")
        errs = common_violations("okuma 915 lands it", ctx)
        self.assertTrue(any("unverified person" in e for e in errs), errs)


class JunkNameTests(unittest.TestCase):
    def test_junk_detection(self):
        from pln_agent.strategists.base import is_junk_name
        for junk in ("person #2775", "Person 81", "FPS", "", "Q", "face #3"):
            self.assertTrue(is_junk_name(junk), junk)
        for real in ("Black Larry", "Rocky", "CANNON", "Q. Davidson", "Mexicutioner", "OKUMA 915"):
            self.assertFalse(is_junk_name(real), real)

    def test_house_vocabulary_and_whole_words(self):
        db, _ = make(packs=False)
        from pln_agent.strategists import CreativeContext
        a = dict(db.get("assets", "asset_id", "carjitsu-sample-001"))
        a["people"] = [{"name": "League", "verified": False}, {"name": "Ram", "verified": False},
                       {"name": "Rocky", "verified": False}, {"name": "person #12", "verified": False}]
        ctx = CreativeContext(asset=a, account={}, brand={}, platform="x", route_role="canonical", display_brand="CarJitsu")
        self.assertEqual(common_violations("New on the Pro League Network: a programmed finish.", ctx), [])
        self.assertTrue(any("Rocky" in e for e in common_violations("Rocky gets the tap.", ctx)))


class ProductionConfigTests(unittest.TestCase):
    def test_shipped_content_calendar_has_no_sample_events(self):
        from pln_agent.config import CONFIG_DIR
        rows = (CONFIG_DIR / "content_calendar.csv").read_text().strip().splitlines()
        self.assertEqual(len(rows), 1, "config/content_calendar.csv must hold only official events (header only by default)")


class RdsDataTests(unittest.TestCase):
    def test_placeholders_and_typed_params(self):
        r = object.__new__(_RdsData)
        r.client, r.cluster, r.secret, r.database = FakeRdsClient(), "arn:c", "arn:s", "growth"
        out = r.run("SELECT ? , ?, ?, ?, ?, ?", ["a", 3, 1.5, True, None, '{"x":1}'], json_params={5})
        self.assertEqual(out, [{"n": 1}])
        call = r.client.calls[0]
        self.assertEqual(call["sql"], "SELECT :p0 , :p1, :p2, :p3, :p4, :p5")
        vals = [p["value"] for p in call["parameters"]]
        self.assertEqual(vals[:5], [{"stringValue": "a"}, {"longValue": 3}, {"doubleValue": 1.5},
                                    {"booleanValue": True}, {"isNull": True}])
        self.assertEqual(call["parameters"][5]["typeHint"], "JSON")
        self.assertEqual(call["database"], "growth")
        with self.assertRaises(ValueError):
            r.run("SELECT ?", [])


class FakePagedClient:
    """Mimics the Data API's 1 MB result cap: full SELECTs fail, pages of <= 3 rows succeed."""

    def __init__(self, rows):
        self.rows, self.sql = rows, []

    def execute_statement(self, **kw):
        import json as _j
        import re as _re
        self.sql.append(kw["sql"])
        m = _re.search(r"LIMIT (\d+) OFFSET (\d+)$", kw["sql"])
        if not m:
            raise type("UnsupportedResultException", (Exception,), {})("The result exceeds the size limit 1 MB.")
        size, off = int(m.group(1)), int(m.group(2))
        if size > 3:
            raise type("UnsupportedResultException", (Exception,), {})("The result exceeds the size limit 1 MB.")
        return {"formattedRecords": _j.dumps(self.rows[off:off + size])}


class RdsPagingTests(unittest.TestCase):
    def test_oversized_select_is_reread_in_pages(self):
        r = object.__new__(_RdsData)
        rows = [{"i": i} for i in range(10)]
        r.client, r.cluster, r.secret, r.database = FakePagedClient(rows), "c", "s", "growth"
        self.assertEqual(r.run("SELECT i FROM t ORDER BY i"), rows)
        with self.assertRaises(Exception):
            r.run("UPDATE t SET i = 1")  # writes are never retried as pages


class ClipMappingTests(unittest.TestCase):
    def test_moments_split_around_decisive_point(self):
        ms = clip_moments(2026.5, 2042.9, 7.09, ["flush_slap", "knockout"], 0.92, "A flush slap lands.", relative=True)
        self.assertEqual([m["kind"] for m in ms], ["setup", "finish", "reaction"])
        self.assertEqual(ms[0]["start"], 0.0)
        self.assertAlmostEqual(ms[1]["start"], 5.59, places=2)
        self.assertAlmostEqual(ms[-1]["end"], 16.4, places=1)
        abs_ms = clip_moments(100.0, 110.0, None, [], 0.5, "", relative=False)
        self.assertEqual(abs_ms[0]["start"], 100.0)  # master timecodes when no rendition exists

    def test_injury_language_from_machine_captions_is_rejected(self):
        db, _ = make(packs=False)
        from pln_agent.strategists import CreativeContext
        a = db.get("assets", "asset_id", "carjitsu-sample-001")
        ctx = CreativeContext(asset=a, account={}, brand={}, platform="x", route_role="canonical", display_brand="")
        self.assertTrue(common_violations("Savage dropped by a devastating chin shot, out cold", ctx))


OPS_DDL = """
CREATE TABLE social_account (id serial PRIMARY KEY, brand text, platform text, platform_account_id text,
  handle text, display_name text, status text, capabilities jsonb DEFAULT '{}'::jsonb);
CREATE TABLE clip_sources (id text PRIMARY KEY, bucket text, property text, title text, aired_date date, duration_sec numeric);
CREATE TABLE clips (id text PRIMARY KEY, source_id text, property text, start_sec numeric, end_sec numeric,
  peak_offset_sec numeric, moment_offset_sec numeric, highlight_score integer, caption text, transcript text,
  source_bucket text, source_key text, vertical_key text, vertical_mode text, status text, superseded_by text,
  thumb_key text, energy_score real, vertical_subject text);
CREATE TABLE clip_tags (clip_id text, tag text, source text);
CREATE TABLE people (id bigint PRIMARY KEY, name text, aliases jsonb DEFAULT '[]', hidden boolean DEFAULT false);
CREATE TABLE clip_talent (clip_id text, name_raw text, person_id bigint, source text);
INSERT INTO social_account (brand, platform, platform_account_id, handle, display_name, status) VALUES
  ('putt-tour','instagram','17841400000000001','@putttour','Putt Tour','active'),
  ('carjitsu','facebook','100000000000001','CarJitsu Championship','CarJitsu Championship','active'),
  ('mystery','instagram','1','@x','X','active');
INSERT INTO clip_sources VALUES ('pln-vod-library/PLNstudio/CJ.mp4','pln-vod-library','CarJitsu','CJ ep 1','2026-07-01',3600);
INSERT INTO clips VALUES
 ('CJ.mp4:10-24','pln-vod-library/PLNstudio/CJ.mp4','CarJitsu',10,24,6,7,88,'Back-seat reversal into a submission.','and he taps',
  'pln-vod-library','PLNstudio/CJ.mp4','verticals/CJ-10-24_9x16.mp4','tight','tagged',NULL),
 ('CJ.mp4:40-52','pln-vod-library/PLNstudio/CJ.mp4','CarJitsu',40,52,4,5,71,'Scramble at the door.',NULL,
  'pln-vod-library','PLNstudio/CJ.mp4',NULL,NULL,'tagged',NULL),
 ('CJ.mp4:60-70','pln-vod-library/PLNstudio/CJ.mp4','CarJitsu',60,70,4,5,95,'Old cut.',NULL,
  'pln-vod-library','PLNstudio/CJ.mp4',NULL,NULL,'tagged','CJ.mp4:60-71'),
 ('X.mp4:1-9','pln-vod-library/PLNstudio/CJ.mp4','Mystery',1,9,2,2,99,'Unknown',NULL,'b','k',NULL,NULL,'tagged',NULL);
INSERT INTO clip_tags VALUES ('CJ.mp4:10-24','submission','nova');
INSERT INTO people VALUES (7,'Athlete Seven','[]',false), (8,'Okuma','[]',false), (9,NULL,'[]',true);
INSERT INTO clip_talent VALUES ('CJ.mp4:10-24','ATHLETE SEVEN',7,'face');
"""


class BulkReviewTests(unittest.TestCase):
    def setUp(self):
        self.db, self.s = make()

    def _pick(self, vid):
        return (vid, self.db.get("variants", "variant_id", vid)["caption_hash"])

    def test_bulk_approve_skips_sensitive_and_changed_copy(self):
        from pln_agent.workflow import bulk_review, edit_variant
        ok = "carjitsu-sample-001:carjitsu.tiktok.carjitsuchampionship"
        sensitive = "putt-sample-002:putt_tour.instagram.putttour"   # paid partnership → two-person
        changed = "carjitsu-sample-001:carjitsu.x.carjitsuusa"
        picks = [self._pick(ok), self._pick(sensitive), self._pick(changed)]
        v = self.db.get("variants", "variant_id", changed)
        edit_variant(self.db, changed, {"caption": v["caption"] + " Watch it again."}, "editor")
        r = bulk_review(self.db, picks, "by@pln", "approve")
        self.assertEqual(r["done"], [ok])
        self.assertIn("two-person", r["skipped"][sensitive])
        self.assertIn("copy changed", r["skipped"][changed])
        self.assertEqual(self.db.get("variants", "variant_id", ok)["state"], "approved")
        self.assertEqual(self.db.get("variants", "variant_id", sensitive)["state"], "in_review")

    def test_bulk_reject_needs_a_note_and_records_each(self):
        from pln_agent.workflow import WorkflowError, bulk_review
        vid = "wpl-archive-003:putt_tour.instagram.putttour"
        with self.assertRaises(WorkflowError):
            bulk_review(self.db, [self._pick(vid)], "by@pln", "reject")
        r = bulk_review(self.db, [self._pick(vid)], "by@pln", "reject", "off-brand")
        self.assertEqual(r["done"], [vid])
        self.assertEqual(self.db.get("variants", "variant_id", vid)["state"], "rejected")


@unittest.skipUnless(PG, "set PLN_TEST_DATABASE_URL to run the ops import against Postgres")
class OpsImportTests(unittest.TestCase):
    def setUp(self):
        self.db, self.s = make(packs=False)
        url = test_db_url()
        self.ops = DB(url, init=False)
        self.ops.execute(OPS_DDL)

    def test_registry_merge_upgrades_matches_and_adds_new_without_authorizing(self):
        r = import_ops_accounts(self.db, self.ops)
        self.assertIn("putt_tour.instagram.putttour", r["matched"])
        pt = self.db.get("accounts", "account_id", "putt_tour.instagram.putttour")
        self.assertEqual(pt["verification_status"], "ownership_confirmed")
        self.assertEqual(pt["stable_platform_id"], "17841400000000001")
        self.assertFalse(pt["publishing_authorized"])
        self.assertTrue(any(a.startswith("carjitsu.facebook.") for a in r["added"]))
        self.assertTrue(any("mystery" in s for s in r["skipped"]))
        self.assertEqual(len(self.db.all("accounts", "source='user_csv'")), 28)  # inventory untouched

    def test_clip_import_maps_library_and_stays_rights_blocked(self):
        r = import_ops_clips(self.db, self.ops, "assets-bucket", min_score=50)
        self.assertEqual(r["imported"], 2)  # superseded + unknown-property clips excluded
        self.assertTrue(any("Mystery" in s for s in r["skipped"]))
        a = self.db.get("assets", "asset_id", clip_asset_id("CJ.mp4:10-24"))
        self.assertEqual(a["media_path"], "s3://assets-bucket/verticals/CJ-10-24_9x16.mp4")
        self.assertEqual(a["brand_id"], "carjitsu")
        self.assertEqual(a["moments"][1]["kind"], "finish")
        self.assertEqual(a["people"], [{"name": "Athlete Seven", "verified": False, "detected_by": "face",
                                        "ops_person_id": 7}])
        self.assertTrue(a["flags"]["combat"])
        ev = evaluate(a)
        self.assertIn("media rights not verified", ev.blockers)
        master = self.db.get("assets", "asset_id", clip_asset_id("CJ.mp4:40-52"))
        self.assertTrue(master["media_path"].startswith("s3://pln-vod-library/"))
        self.assertEqual(master["moments"][0]["start"], 40.0)

    def test_standing_rights_default_applies_to_new_imports_but_never_identities(self):
        from pln_agent.ops_sources import set_rights_default
        set_rights_default(self.db, "by@pln", license="PLN-owned production", owner="Pro League Network",
                           territory=["WORLDWIDE"], rights_verified=True, music_cleared=True, people_verified=True)
        import_ops_clips(self.db, self.ops, "b", min_score=50)
        a = self.db.get("assets", "asset_id", clip_asset_id("CJ.mp4:10-24"))
        ev = evaluate(a)
        self.assertNotIn("media rights not verified", ev.blockers)
        self.assertNotIn("audio present but music/sound not cleared", ev.blockers)
        self.assertIn("people/athlete identities not verified", ev.blockers)

    def test_withhold_names_policy_lets_unverified_clips_post_without_names(self):
        import os
        from pln_agent.ops_sources import set_rights_default
        set_rights_default(self.db, "by@pln", license="PLN-owned production", territory=["WORLDWIDE"],
                           rights_verified=True, music_cleared=True)
        import_ops_clips(self.db, self.ops, "b", min_score=50)
        a = self.db.get("assets", "asset_id", clip_asset_id("CJ.mp4:10-24"))
        # machine caption/transcript/event title are kept for reviewers but not used for copy
        from pln_agent.ops_sources import TAG_SENTENCES
        self.assertIn(a["description"], TAG_SENTENCES["submission"])
        self.assertNotIn("CJ ep 1", a["title"])  # raw source titles never reach copy
        self.assertEqual(a["transcript"], [])
        self.assertEqual(a["probe"]["ops_clip"]["machine_caption"], "Back-seat reversal into a submission.")
        self.assertIn("people/athlete identities not verified", evaluate(a).blockers)  # default policy blocks
        os.environ["PLN_IDENTITY_POLICY"] = "withhold_names"
        try:
            ev = evaluate(a)
            self.assertTrue(ev.ok, ev.blockers)
            self.assertIn("names_withheld", ev.review_flags)
            from pln_agent.editorial import generate_pack
            p = generate_pack(self.db, a["asset_id"], self.s)
            self.assertTrue(p["variants"])
            for v in p["variants"]:
                self.assertNotIn("Athlete Seven", v["caption"] + v["hook"])
        finally:
            os.environ.pop("PLN_IDENTITY_POLICY")

    def test_import_shares_slots_across_properties_and_keeps_thin_p0_libraries(self):
        self.ops.execute(
            "INSERT INTO clip_sources VALUES ('s/PT.mp4','b','PuttTour','PT',NULL,600);"
            "INSERT INTO clips (id, source_id, property, start_sec, end_sec, highlight_score, caption, source_bucket,"
            " source_key, status) VALUES "
            "('PT:1','s/PT.mp4','PuttTour',0,10,35,'Long putt drops.','b','PT.mp4','tagged'),"
            "('PT:2','s/PT.mp4','PuttTour',10,20,20,'Course overview.','b','PT.mp4','tagged'),"
            + ",".join(f"('CJ:x{i}','pln-vod-library/PLNstudio/CJ.mp4','CarJitsu',{i},{i + 5},{60 + i},'c','b','k','tagged')"
                       for i in range(10)))
        r = import_ops_clips(self.db, self.ops, "b", limit=4, min_score=50)
        ids = {a["content_id"] for a in self.db.all("assets") if a["content_id"]}
        self.assertIn("PT:1", ids)        # P0 brand below the global floor but above the priority floor
        self.assertNotIn("PT:2", ids)     # junk-level score never imported
        self.assertEqual(r["imported"], 3)  # 4 picked: PT:1 + 2 CarJitsu (P0) per round, Mystery (score 99) skipped

    def test_rights_overlay_survives_reimport(self):
        import_ops_clips(self.db, self.ops, "b", min_score=50)
        aid = clip_asset_id("CJ.mp4:10-24")
        set_asset_rights(self.db, aid, "legal@pln", license="PLN-owned production", territory=["WORLDWIDE"],
                         rights_verified=True, people_verified=True, music_cleared=True)
        import_ops_clips(self.db, self.ops, "b", min_score=50)
        a = self.db.get("assets", "asset_id", aid)
        self.assertTrue(evaluate(a).ok, evaluate(a).blockers)


try:
    import fastapi  # noqa: F401
    import httpx  # noqa: F401
    HAVE_WEB = True
except ImportError:
    HAVE_WEB = False


@unittest.skipUnless(HAVE_WEB, "fastapi/httpx not installed")
class ProxiedDashboardTests(unittest.TestCase):
    def setUp(self):
        from dataclasses import replace
        from fastapi.testclient import TestClient
        from pln_agent.web import app as webapp
        self.db, s = make()
        webapp._db = self.db
        webapp.settings = replace(s, agent_api_key="k-test")
        self.c = TestClient(webapp.app)
        self.h = {"Authorization": "Bearer k-test", "X-PLN-User-Email": "Editor@ProLeagueNetwork.com",
                  "X-Forwarded-Prefix": "/api/social-agent"}

    def tearDown(self):
        from pln_agent.web import app as webapp
        from pln_agent.config import load_settings
        webapp.settings = load_settings()

    def test_queue_bulk_route_approves_selected_posts(self):
        vid = "carjitsu-sample-001:carjitsu.tiktok.carjitsuchampionship"
        h = self.db.get("variants", "variant_id", vid)["caption_hash"]
        page = self.c.get("/queue", headers=self.h).text
        self.assertIn(f'value="{vid}|{h}"', page)
        self.assertIn("Approve selected", page)
        admin = {**self.h, "X-PLN-User-Admin": "1"}
        r = self.c.post("/queue/bulk", headers=admin, data={"pick": [f"{vid}|{h}"], "decision": "approve"},
                        follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertIn("Approved+1", r.headers["location"])
        self.assertEqual(self.db.get("variants", "variant_id", vid)["state"], "approved")
        self.assertIn("err=", self.c.post("/queue/bulk", headers=admin, data={"decision": "approve"},
                                          follow_redirects=False).headers["location"])

    def test_requires_proxy_key_and_identity(self):
        self.assertEqual(self.c.get("/queue").status_code, 401)
        self.assertEqual(self.c.get("/queue", headers={"Authorization": "Bearer k-test"}).status_code, 401)
        self.assertEqual(self.c.get("/healthz").status_code, 200)
        r = self.c.get("/queue", headers=self.h)
        self.assertEqual(r.status_code, 200)
        self.assertIn('href="/api/social-agent/assets"', r.text)
        self.assertNotIn('name="reviewer"', self.c.get("/variant/carjitsu-sample-001:carjitsu.tiktok.carjitsuchampionship",
                                                       headers=self.h).text)

    def test_changes_need_admin_and_record_the_signed_in_user(self):
        vid = "carjitsu-sample-001:carjitsu.tiktok.carjitsuchampionship"
        r = self.c.post(f"/variant/{vid}/approve", headers=self.h, data={"reviewer": "spoofed"}, follow_redirects=False)
        self.assertEqual(r.status_code, 403)
        r = self.c.post(f"/variant/{vid}/approve", headers={**self.h, "X-PLN-User-Admin": "1"},
                        data={"reviewer": "spoofed"}, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertTrue(r.headers["location"].startswith("/api/social-agent/variant/"))
        appr = self.db.all("approvals", "variant_id=?", [vid])
        self.assertEqual(appr[-1]["reviewer"], "editor@proleaguenetwork.com")

    def test_embedded_in_portal_hides_own_nav_and_queue_has_preview_column(self):
        framed = self.c.get("/queue", headers={**self.h, "Sec-Fetch-Dest": "iframe"}).text
        self.assertNotIn('href="/api/social-agent/audit">Audit</a>', framed)
        self.assertIn('href="/api/social-agent/audit">Audit</a>', self.c.get("/queue", headers=self.h).text)
        self.assertIn("no preview", framed)  # sample assets have no S3 thumbnail

    def test_thumbnail_and_preview_urls_are_presigned_and_cued(self):
        from pln_agent.media import asset_preview, asset_thumb_url
        import os
        os.environ.setdefault("AWS_ACCESS_KEY_ID", "AKIDEXAMPLE")
        os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "example")
        a = {"media_present": True, "media_path": "s3://bkt/verticals/x_9x16.mp4",
             "probe": {"ops_clip": {"thumb": "s3://assets/frames/x_0.jpg"}}}
        self.assertIn("response-content-type=image%2Fjpeg", asset_thumb_url(a))
        p = asset_preview(a, [{"source_in": 1.5, "source_out": 3.0}, {"source_in": 4.0, "source_out": 9.25}])
        self.assertTrue(p["src"].startswith("https://bkt.s3"))
        self.assertTrue(p["src"].endswith("#t=1.50,9.25"))
        self.assertEqual(asset_preview({"media_present": False, "media_path": "s3://b/k"}), {})

    def test_bad_prefix_is_ignored(self):
        r = self.c.get("/queue", headers={**self.h, "X-Forwarded-Prefix": 'x"><script>'})
        self.assertNotIn("<script>", r.text)

    def test_rights_editor_unblocks_asset(self):
        aid = "wpl-archive-003"
        self.assertEqual(self.c.get(f"/asset/{aid}", headers=self.h).status_code, 200)
        r = self.c.post(f"/asset/{aid}/rights", headers={**self.h, "X-PLN-User-Admin": "1"}, follow_redirects=False,
                        data={"license": "Legacy broadcast licence", "territory": "US", "expires": "2027-12-31",
                              "rights_verified": "on", "people_verified": "on", "music_cleared": "on", "regenerate": "on"})
        self.assertEqual(r.status_code, 303, r.text)
        self.assertTrue(evaluate(self.db.get("assets", "asset_id", aid)).ok)
        self.assertEqual(self.db.get("asset_rights", "asset_id", aid)["updated_by"], "editor@proleaguenetwork.com")


if __name__ == "__main__":
    unittest.main()
