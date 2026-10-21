"""Community Manager: comment ingestion against a fake Graph API, triage, approval-gated replies, FAQs."""
import unittest
import urllib.parse
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from pln_agent import accounts as acc_mod
from pln_agent import community as cm
from pln_agent import oauth
from pln_agent.adapters.base import HttpResponse

from tests.helpers import make

IG = "carjitsu.instagram.carjitsu_championship"
NOW = datetime.now(timezone.utc)
D = lambda hours: (NOW - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S+0000")


class FakeGraph:
    def __init__(self, deny=False):
        self.posts, self.deny = [], deny

    def __call__(self, req):
        u = urllib.parse.urlparse(req.url)
        if req.method == "POST":
            self.posts.append((u.path, req.form_body))
            assert req.form_body["access_token"] == "PAGE-TOKEN"
            return HttpResponse(200, {"id": "reply-1"})
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        assert q["access_token"] == "PAGE-TOKEN"
        if self.deny:
            return HttpResponse(403, {"error": {"code": 10, "message": "Application does not have permission for this action"}})
        if u.path.endswith("/reel1/comments"):
            return HttpResponse(200, {"data": [
                {"id": "c1", "text": "How do I sign up to compete?", "username": "fan1", "timestamp": D(5), "like_count": 3},
                {"id": "c2", "text": "Is he ok? that looked like a concussion", "username": "fan2", "timestamp": D(4), "like_count": 40},
                {"id": "c3", "text": "🔥🔥 legend", "username": "fan3", "timestamp": D(3), "like_count": 1,
                 "replies": {"data": [{"id": "c3r", "text": "Glad you're here", "username": "carjitsu_championship", "timestamp": D(2)}]}},
                {"id": "c4", "text": "check my page for promo https://spam.example", "username": "bot", "timestamp": D(2)},
                {"id": "c5", "text": "where can i watch the full fight?", "username": "fan4", "timestamp": D(1)},
                {"id": "c6", "text": "where can i watch the full event", "username": "fan5", "timestamp": D(1)},
                {"id": "c7", "text": "Where do I watch full fights??", "username": "fan6", "timestamp": D(1)},
            ]})
        return HttpResponse(200, {"data": []})


class CommunityTests(unittest.TestCase):
    def setUp(self):
        self.db, self.s = make(packs=False)
        self.store = oauth.MemorySecretStore()
        oauth.set_store(self.store)
        self.addCleanup(oauth.set_store, None)
        name = f"pln/growth-agent/tokens/{IG}"
        self.store.put(name, {"provider": "meta", "access_token": "PAGE-TOKEN"})
        self.db.update("accounts", "account_id", IG, {"verification_status": "oauth_connected", "credential_ref": f"vault:{name}",
                                                      "stable_platform_id": "1784", "username_normalized": "carjitsu_championship"})
        from pln_agent.creative_db import upsert_ingested_post
        upsert_ingested_post(self.db, self.db.get("accounts", "account_id", IG),
                             {"platform_post_id": "reel1", "published_at": D(24), "text": "x", "post_type": "reels"}, self.s.timezone)

    def test_triage_escalates_safety_drafts_safe_replies_and_never_invents_links(self):
        r = cm.ingest(self.db, FakeGraph())
        self.assertEqual(r["errors"], {})
        c = {x["platform_comment_id"]: x for x in self.db.all("comments")}
        self.assertEqual(c["c2"]["status"], "escalated")
        self.assertFalse(c["c2"]["draft_reply"])
        self.assertEqual(c["c2"]["priority"], 0)
        self.assertEqual(c["c4"]["status"], "flagged_spam")
        self.assertEqual(c["c3"]["status"], "replied")  # we already answered it on the platform
        self.assertEqual(c["c3r"]["status"], "own")
        self.assertEqual(c["c1"]["category"], "athlete_interest")
        self.assertEqual(c["c1"]["status"], "needs_answer")  # no compete link configured
        self.assertIn("[add compete link", c["c1"]["draft_reply"])
        with self.assertRaises(ValueError):  # a placeholder can't be approved
            cm.decide(self.db, c["c1"]["comment_id"], "approve", "bill")
        # re-fetch keeps decisions
        cm.decide(self.db, c["c5"]["comment_id"], "ignore", "bill")
        self.assertEqual(cm.ingest(self.db, FakeGraph())["new"], 0)
        self.assertEqual(self.db.get("comments", "comment_id", c["c5"]["comment_id"])["status"], "ignored")

    def test_approved_replies_simulate_until_every_gate_is_open(self):
        cm.ingest(self.db, FakeGraph())
        cid = "instagram:c1"
        cm.decide(self.db, cid, "approve", "bill", text="Applications open soon — watch this page.")
        g = FakeGraph()
        out = cm.send_approved(self.db, self.s, g)
        self.assertEqual(out[0]["mode"], "simulated")
        self.assertEqual(g.posts, [])
        self.assertEqual(self.db.get("comments", "comment_id", cid)["status"], "simulated_reply")
        # all gates open → live reply to the comment's /replies edge
        cm.decide(self.db, cid, "reopen", "bill")
        cm.decide(self.db, cid, "approve", "bill", text="Applications open soon — watch this page.")
        self.db.update("accounts", "account_id", IG, {"publishing_authorized": True})
        live = replace(self.s, dry_run=False, publishing_enabled=True)
        out = cm.send_approved(self.db, live, g)
        self.assertEqual(out[0]["mode"], "live")
        self.assertTrue(g.posts[0][0].endswith("/c1/replies"))
        self.assertEqual(self.db.get("comments", "comment_id", cid)["status"], "replied")

    def test_permission_error_says_what_to_add(self):
        r = cm.ingest(self.db, FakeGraph(deny=True))
        self.assertIn("instagram_manage_comments", r["errors"][IG])

    def test_faqs_become_content_ideas(self):
        cm.ingest(self.db, FakeGraph())
        f = cm.faqs(self.db)
        self.assertEqual(len(f), 1)
        self.assertEqual((f[0]["category"], f[0]["people"]), ("where_to_watch", 3))
        from pln_agent.growth_officer import generate_brief
        b = generate_brief(self.db, persist=False)
        self.assertTrue(any(a["area"] == "community" and a["priority"] == "P0" for a in b["actions"]))

    def test_page_renders(self):
        from fastapi.testclient import TestClient
        from pln_agent.web import app as web
        cm.ingest(self.db, FakeGraph())
        web._db = self.db
        r = TestClient(web.app).get("/community")
        self.assertEqual(r.status_code, 200)
        self.assertIn("concussion", r.text)


if __name__ == "__main__":
    unittest.main()
