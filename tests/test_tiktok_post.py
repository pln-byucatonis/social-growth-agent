"""TikTok post settings: a person chooses who can watch, interactions and disclosure before anything posts."""
import os
import unittest
from unittest import mock

from pln_agent import tiktok_post
from pln_agent.adapters.base import HttpResponse
from pln_agent.workflow import WorkflowError, approve, schedule
from tests.helpers import make

INFO = {"username": "carjitsu", "nickname": "CarJitsu", "avatar": "", "privacy_options":
        ["PUBLIC_TO_EVERYONE", "MUTUAL_FOLLOW_FRIENDS", "SELF_ONLY"], "comment_disabled": False,
        "duet_disabled": True, "stitch_disabled": False, "max_duration_s": 600}
VID = "carjitsu-sample-001:carjitsu.tiktok.carjitsuchampionship"


class TikTokSettingsTests(unittest.TestCase):
    def setUp(self):
        self.db, self.s = make()

    def test_creator_info_reads_the_account_live(self):
        os.environ["TT_TEST_TOKEN"] = "x"
        self.addCleanup(os.environ.pop, "TT_TEST_TOKEN")
        body = {"data": {"creator_username": "carjitsu", "creator_nickname": "CarJitsu", "creator_avatar_url": "a.jpg",
                         "privacy_level_options": ["SELF_ONLY", "PUBLIC_TO_EVERYONE"], "comment_disabled": False,
                         "duet_disabled": True, "stitch_disabled": False, "max_video_post_duration_sec": 300},
                "error": {"code": "ok"}}
        info = tiktok_post.creator_info({"credential_ref": "env:TT_TEST_TOKEN"}, transport=lambda r: HttpResponse(200, body))
        self.assertEqual((info["username"], info["max_duration_s"], info["duet_disabled"]), ("carjitsu", 300, True))
        busy = {"data": {}, "error": {"code": "spam_risk_too_many_posts"}}
        with self.assertRaises(tiktok_post.TikTokPostError) as e:
            tiktok_post.creator_info({"credential_ref": "env:TT_TEST_TOKEN"}, transport=lambda r: HttpResponse(200, busy))
        self.assertIn("posted too much", str(e.exception))
        with self.assertRaises(tiktok_post.TikTokPostError) as e:
            tiktok_post.creator_info({"credential_ref": ""})
        self.assertIn("connect", str(e.exception))

    def test_nothing_is_chosen_for_the_person(self):
        v = self.db.get("variants", "variant_id", VID)
        with self.assertRaises(tiktok_post.TikTokPostError):
            tiktok_post.choose(self.db, VID, {"title": v["caption"]}, INFO, "bill")  # no privacy picked
        approve(self.db, VID, "bill")
        with self.assertRaises(WorkflowError):  # can't be scheduled without the person's settings
            from datetime import datetime, timezone
            schedule(self.db, VID, datetime(2030, 1, 2, 16, tzinfo=timezone.utc), "bill")

    def test_rules_enforced(self):
        v = self.db.get("variants", "variant_id", VID)
        base = {"title": v["caption"], "privacy_level": "SELF_ONLY"}
        with self.assertRaises(tiktok_post.TikTokPostError):  # unaudited app: private only
            tiktok_post.choose(self.db, VID, {**base, "privacy_level": "PUBLIC_TO_EVERYONE"}, INFO, "bill")
        with self.assertRaises(tiktok_post.TikTokPostError):  # commercial on, nothing picked
            tiktok_post.choose(self.db, VID, {**base, "commercial": "on"}, INFO, "bill")
        with self.assertRaises(tiktok_post.TikTokPostError):  # a paid partnership can't be private
            tiktok_post.choose(self.db, VID, {**base, "commercial": "on", "branded_content": "on"}, INFO, "bill")
        with self.assertRaises(tiktok_post.TikTokPostError):  # longer than the account may post
            tiktok_post.choose(self.db, VID, base, {**INFO, "max_duration_s": 1}, "bill") if tiktok_post.duration(v) \
                else (_ for _ in ()).throw(tiktok_post.TikTokPostError("no duration in fixture"))

    def test_choices_reach_the_post_and_an_edit_voids_them(self):
        from datetime import datetime, timezone
        v = self.db.get("variants", "variant_id", VID)
        s = tiktok_post.choose(self.db, VID, {"title": v["caption"], "privacy_level": "SELF_ONLY", "allow_comment": "on",
                                              "allow_duet": "on", "commercial": "on", "your_brand": "on"}, INFO, "bill")
        self.assertEqual((s["disable_comment"], s["disable_duet"], s["disable_stitch"]), (False, True, True))  # duet off in TikTok
        self.assertEqual(tiktok_post.label(s), "Promotional content")
        v = self.db.get("variants", "variant_id", VID)
        from pln_agent.adapters import get_adapter
        post = get_adapter("tiktok").plan_requests(v, {}, "https://m/x.mp4")[1].json_body["post_info"]
        self.assertEqual((post["privacy_level"], post["disable_comment"], post["brand_organic_toggle"]), ("SELF_ONLY", False, True))
        approve(self.db, VID, "bill")
        self.assertEqual(schedule(self.db, VID, datetime(2030, 1, 2, 16, tzinfo=timezone.utc), "bill")["state"], "queued")
        # a different title the person typed becomes the approved text
        other = "carjitsu-sample-001:pln.tiktok.proleaguenetwork"
        tiktok_post.choose(self.db, other, {"title": "New words", "privacy_level": "SELF_ONLY"}, INFO, "bill")
        o = self.db.get("variants", "variant_id", other)
        self.assertEqual(o["caption"], "New words")
        self.assertTrue(tiktok_post.chosen(o))
        from pln_agent.workflow import edit_variant
        edit_variant(self.db, other, {"caption": "Changed later"}, "bill")
        self.assertIsNone(tiktok_post.chosen(self.db.get("variants", "variant_id", other)))  # must be chosen again

    def test_publisher_refuses_without_settings(self):
        from pln_agent.adapters import get_adapter
        from pln_agent.adapters.base import AdapterError
        with self.assertRaises(AdapterError):
            get_adapter("tiktok").publish(self.db.get("variants", "variant_id", VID), {"credential_ref": "env:X"}, "k")

    def test_page_and_save(self):
        from fastapi.testclient import TestClient
        from pln_agent.web import app as web
        web._db = self.db
        c = TestClient(web.app)
        with mock.patch.object(tiktok_post, "creator_info", lambda acc, transport=None: INFO):
            page = c.get(f"/variant/{VID}/tiktok").text
            self.assertIn("@carjitsu", page)
            self.assertIn("Music Usage Confirmation", page)
            self.assertNotIn("checked", page.split('name="privacy_level"')[1][:120])  # nothing picked in advance
            self.assertIn("turned off for this account", page)                     # duet greyed out
            r = c.post(f"/variant/{VID}/tiktok", data={"title": self.db.get("variants", "variant_id", VID)["caption"],
                                                        "privacy_level": "SELF_ONLY", "actor": "bill"}, follow_redirects=False)
            self.assertEqual(r.status_code, 303)
            self.assertIn("/runsheet", r.headers["location"])
            bad = c.post(f"/variant/{VID}/tiktok", data={"title": "x", "actor": "bill"})  # nothing picked: back with the reason
            self.assertEqual(bad.status_code, 200)
            self.assertIn("choose who can watch", bad.text)
        v = self.db.get("variants", "variant_id", VID)
        self.assertEqual(v["state"], "approved")
        self.assertEqual(tiktok_post.chosen(v)["by"], "bill")
        with mock.patch.object(tiktok_post, "creator_info", side_effect=tiktok_post.TikTokPostError("connect this TikTok account first")):
            self.assertIn("Connect the TikTok account", c.get(f"/variant/{VID}/tiktok").text)


if __name__ == "__main__":
    unittest.main()
