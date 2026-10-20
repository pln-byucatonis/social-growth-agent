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
  source_bucket text, source_key text, vertical_key text, vertical_mode text, status text, superseded_by text);
CREATE TABLE clip_tags (clip_id text, tag text, source text);
CREATE TABLE people (id bigint PRIMARY KEY, name text);
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
INSERT INTO people VALUES (7,'Athlete Seven');
INSERT INTO clip_talent VALUES ('CJ.mp4:10-24','ATHLETE SEVEN',7,'face');
"""


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
        self.assertEqual(a["people"], [{"name": "Athlete Seven", "verified": False, "detected_by": "face"}])
        self.assertTrue(a["flags"]["combat"])
        ev = evaluate(a)
        self.assertIn("media rights not verified", ev.blockers)
        master = self.db.get("assets", "asset_id", clip_asset_id("CJ.mp4:40-52"))
        self.assertTrue(master["media_path"].startswith("s3://pln-vod-library/"))
        self.assertEqual(master["moments"][0]["start"], 40.0)

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
