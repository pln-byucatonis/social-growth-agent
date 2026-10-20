"""Connect-accounts flow against a fake platform: no network, tokens only in the (memory) secret store."""
import json
import os
import time
import unittest
import urllib.parse

from pln_agent import oauth
from pln_agent.adapters.base import HttpResponse, resolve_token
from pln_agent.public_media import PublicMediaError, publish_render
from pln_agent.workflow import approve

from helpers import CJ, make


class FakePlatform:
    def __init__(self):
        self.calls = []

    def __call__(self, req):
        self.calls.append(req)
        u = req.url
        if "oauth/access_token" in u and "fb_exchange_token" in u:
            return HttpResponse(200, {"access_token": "LONG-USER", "expires_in": 5184000})
        if "oauth/access_token" in u:
            return HttpResponse(200, {"access_token": "SHORT-USER", "expires_in": 3600})
        if "/me/accounts" in u:
            return HttpResponse(200, {"data": [
                {"id": "1001", "name": "Pro League Network", "access_token": "PAGE-PLN",
                 "instagram_business_account": {"id": "17841", "username": "proleaguenetwork"}},
                {"id": "2002", "name": "Some Other Page", "access_token": "PAGE-OTHER"}]})
        if "oauth2.googleapis.com/token" in u:
            grant = req.form_body["grant_type"]
            return HttpResponse(200, {"access_token": "YT-" + grant, "refresh_token": "YT-REFRESH", "expires_in": 3600})
        if "youtube/v3/channels" in u:
            return HttpResponse(200, {"items": [{"id": "UC123", "snippet": {"customUrl": "@carjitsuchampionship6540",
                                                                         "title": "CarJitsu"}}]})
        if "linkedin.com/oauth" in u:
            return HttpResponse(200, {"access_token": "LI", "expires_in": 5184000})
        if "organizationAcls" in u:
            return HttpResponse(200, {"elements": [{"organization": "urn:li:organization:91515598", "role": "ADMINISTRATOR"}]})
        return HttpResponse(404, {"error": "unexpected " + u})


class OAuthTests(unittest.TestCase):
    def setUp(self):
        self.db, self.s = make(packs=True)
        self.store = oauth.MemorySecretStore()
        oauth.set_store(self.store)
        self.fake = FakePlatform()
        oauth.set_transport(self.fake)
        for p in ("meta", "google", "linkedin"):
            oauth.save_app(p, f"{p}-client-id-1234", f"{p}-SECRET", self.db, "admin@pln")

    def tearDown(self):
        oauth.set_transport(None)
        oauth.set_store(None)

    def _state(self, url):
        return urllib.parse.parse_qs(urllib.parse.urlparse(url).query)

    def test_app_secret_is_stored_but_never_audited_or_shown(self):
        self.assertEqual(oauth.app_status("meta"), {"configured": True, "client_id_hint": "…1234"})
        audit = json.dumps(self.db.all("audit_log"))
        self.assertNotIn("meta-SECRET", audit)

    def test_one_meta_login_connects_matching_page_and_instagram(self):
        url = oauth.start(self.db, "meta", "pln.facebook.pro_league_network", "admin@pln")
        q = self._state(url)
        self.assertIn("pages_manage_posts", q["scope"][0])
        self.assertEqual(q["redirect_uri"][0], "https://ops.proleaguenetwork.com/api/social-agent/oauth/callback/meta")
        res = oauth.callback(self.db, "meta", {"code": "C", "state": q["state"][0]}, "admin@pln")
        got = {c["account_id"] for c in res["connected"]}
        self.assertIn("pln.facebook.pro_league_network", got)
        self.assertIn("pln.instagram.proleaguenetwork", got)
        self.assertEqual([u["platform_id"] for u in res["unmatched"]], ["2002"])  # never bound to a guess
        fb = self.db.get("accounts", "account_id", "pln.facebook.pro_league_network")
        self.assertEqual(fb["verification_status"], "oauth_connected")
        self.assertEqual(fb["stable_platform_id"], "1001")
        self.assertFalse(fb["publishing_authorized"])  # connecting never authorizes publishing
        self.assertTrue(fb["credential_ref"].startswith("vault:pln/growth-agent/tokens/"))
        self.assertEqual(resolve_token(fb["credential_ref"]), "PAGE-PLN")
        self.assertNotIn("PAGE-PLN", json.dumps(self.db.all("accounts")) + json.dumps(self.db.all("audit_log")))

    def test_state_is_single_use_and_bound_to_provider(self):
        q = self._state(oauth.start(self.db, "meta", "", "admin@pln"))
        with self.assertRaises(PermissionError):
            oauth.callback(self.db, "google", {"code": "C", "state": q["state"][0]}, "admin@pln")
        with self.assertRaises(PermissionError):  # consumed by the failed attempt too
            oauth.callback(self.db, "meta", {"code": "C", "state": q["state"][0]}, "admin@pln")
        with self.assertRaises(PermissionError):
            oauth.callback(self.db, "meta", {"code": "C", "state": "forged-state-value-xxxxxxxx"}, "admin@pln")

    def test_google_uses_pkce_and_refreshes_expired_tokens(self):
        q = self._state(oauth.start(self.db, "google", "carjitsu.youtube.carjitsuchampionship6540", "admin@pln"))
        self.assertEqual(q["code_challenge_method"][0], "S256")
        res = oauth.callback(self.db, "google", {"code": "C", "state": q["state"][0]}, "admin@pln")
        self.assertEqual(res["connected"][0]["platform_id"], "UC123")
        exch = [c for c in self.fake.calls if "oauth2.googleapis.com" in c.url][0]
        self.assertTrue(exch.form_body["code_verifier"])
        name = "pln/growth-agent/tokens/carjitsu.youtube.carjitsuchampionship6540"
        self.assertEqual(oauth.access_token(name), "YT-authorization_code")
        rec = self.store.get(name)
        rec["expires_at"] = int(time.time()) + 10
        self.store.put(name, rec)
        self.assertEqual(oauth.access_token(name), "YT-refresh_token")

    def test_linkedin_requires_admin_role_on_the_pln_page(self):
        q = self._state(oauth.start(self.db, "linkedin", "pln.linkedin.pending", "admin@pln"))
        res = oauth.callback(self.db, "linkedin", {"code": "C", "state": q["state"][0]}, "admin@pln")
        self.assertEqual(res["connected"][0]["account_id"], "pln.linkedin.pending")
        self.assertEqual(self.db.get("accounts", "account_id", "pln.linkedin.pending")["stable_platform_id"], "91515598")

    def test_refused_consent_is_reported(self):
        with self.assertRaises(PermissionError):
            oauth.callback(self.db, "meta", {"error": "access_denied", "error_description": "User denied"}, "a")


class PublicMediaTests(unittest.TestCase):
    def setUp(self):
        self.db, self.s = make(packs=True)
        os.environ["PLN_MEDIA_PUBLIC_BUCKET"] = "pub-bucket"
        os.environ["PLN_MEDIA_PUBLIC_BASE_URL"] = "https://media.example.invalid"
        self.copies = []

    def tearDown(self):
        os.environ.pop("PLN_MEDIA_PUBLIC_BUCKET", None)
        os.environ.pop("PLN_MEDIA_PUBLIC_BASE_URL", None)

    def test_only_approved_rendered_posts_get_an_unguessable_link(self):
        vid = f"{CJ}:carjitsu.tiktok.carjitsuchampionship"
        self.db.update("variants", "variant_id", vid, {"render_status": "rendered",
                                                     "render_path": "s3://pln-dam/social-agent/renders/x/video.mp4"})
        with self.assertRaises(PublicMediaError):
            publish_render(self.db, vid, "t", copier=lambda *a: self.copies.append(a))
        approve(self.db, vid, "editor")
        url = publish_render(self.db, vid, "t", copier=lambda *a: self.copies.append(a))
        self.assertRegex(url, r"^https://media\.example\.invalid/v/[A-Za-z0-9_-]{24}/carjitsu-tiktok\.mp4$")
        self.assertEqual(self.copies[0][:2], ("pln-dam", "social-agent/renders/x/video.mp4"))
        self.assertEqual(publish_render(self.db, vid, "t", copier=lambda *a: self.copies.append(a)), url)  # idempotent
        self.assertEqual(len(self.copies), 1)


if __name__ == "__main__":
    unittest.main()
