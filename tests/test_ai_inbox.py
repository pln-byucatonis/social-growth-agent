"""AI asset inbox (pln_agent/ai_inbox.py): AI video from ops AI Tools becomes a rights-clear, AI-labelled asset."""
import json
import unittest

from pln_agent import ai_inbox, rights
from pln_agent.adapters.providers import TikTokAdapter, YouTubeAdapter
from tests.helpers import make

SIDE = {"job_id": "vid-20261126-ab12cd34ef56", "model": "ltx-2.5-distilled-22b-int8", "model_version": "49b093a39c19",
        "license": "ltx-2.x-community-license-agreement (Lightricks files); apache-2.0 (Comfy-Org/gemma-4)",
        "prompt": "A slow push in on a putting green at dusk. Crowd murmurs.", "seed": 9, "sha256": "ab" * 32,
        "brand": "putt_tour", "duration_s": 5, "width": 1280, "height": 704, "has_audio": True,
        "requested_by": "by@proleaguenetwork.com", "sent_by": "by@proleaguenetwork.com"}
P = "social-agent/ai/inbox/20261126/"
FILES = {P + "vid-20261126-ab12cd34ef56.mp4": None, P + "vid-20261126-ab12cd34ef56.json": SIDE,
         P + "nc.mp4": None, P + "nc.json": {**SIDE, "job_id": "vid-nc", "license": "CC-BY-NC-4.0"},
         P + "mitigated.mp4": None, P + "mitigated.json": {**SIDE, "job_id": "vid-mitig", "license": "mitigated-proprietary"},
         P + "person.mp4": None, P + "person.json": {**SIDE, "job_id": "vid-person", "license": "apache-2.0", "likeness": True},
         P + "stylised.mp4": None, P + "stylised.json": {**SIDE, "job_id": "vid-style", "license": "mit", "disclosure": "none"},
         P + "orphan.json": SIDE}


def lister(bucket, prefix):
    return [k for k in FILES if k.startswith(prefix)]


def reader(bucket, key):
    return json.dumps(FILES[key]).encode()


class AiInboxTests(unittest.TestCase):
    def test_import_keeps_commercial_licences_only_and_is_idempotent(self):
        db, _ = make(packs=False)
        r = ai_inbox.import_inbox(db, "b", lister, reader)
        self.assertEqual(sorted(r["added"]), ["ai-vid-20261126-ab12cd34ef56", "ai-vid-person", "ai-vid-style"])
        self.assertEqual(sorted(s["why"][:14] for s in r["skipped"]), ["model licence ", "model licence ", "no video next "])
        self.assertEqual(ai_inbox.import_inbox(db, "b", lister, reader)["added"], [])

    def test_ai_asset_is_rights_clear_and_carries_the_ai_label(self):
        db, _ = make(packs=False)
        ai_inbox.import_inbox(db, "b", lister, reader)
        a = db.get("assets", "asset_id", "ai-vid-20261126-ab12cd34ef56")
        self.assertEqual((a["brand_id"], a["media_path"]), ("putt_tour", "s3://b/" + P + "vid-20261126-ab12cd34ef56.mp4"))
        self.assertIn("ltx-2.5", a["provenance"])
        ev = rights.evaluate(a)
        self.assertTrue(ev.ok, ev.blockers)
        self.assertIn("ai_generated", ev.review_flags)
        self.assertTrue(any("AI label" in l for l in ev.labels))

    def test_likeness_is_flagged_as_an_ai_person_and_stylised_drops_the_label(self):
        db, _ = make(packs=False)
        ai_inbox.import_inbox(db, "b", lister, reader)
        person = rights.evaluate(db.get("assets", "asset_id", "ai-vid-person"))
        self.assertIn("ai_generated_person", person.review_flags)
        style = rights.evaluate(db.get("assets", "asset_id", "ai-vid-style"))
        self.assertNotIn("ai_generated", style.review_flags)

    def test_platform_requests_set_the_ai_flags(self):
        v = {"risk_flags": {"review_flags": ["ai_generated"]}, "caption": "c", "hashtags": [], "hook": "h",
             "edl": {"segments": [{"source_in": 0}]}, "thumbnail": {"source_time": 0}}
        tt = TikTokAdapter().plan_requests(v, {}, "https://x/v.mp4")[1].json_body["post_info"]
        self.assertTrue(tt["is_aigc"])
        yt = YouTubeAdapter().plan_requests(v, {}, "https://x/v.mp4")[0].json_body["status"]
        self.assertTrue(yt["containsSyntheticMedia"])
        v["risk_flags"]["review_flags"] = []
        self.assertFalse(TikTokAdapter().plan_requests(v, {}, "u")[1].json_body["post_info"]["is_aigc"])


if __name__ == "__main__":
    unittest.main()


class VeoLicenceTests(unittest.TestCase):
    def test_veo_from_vertex_is_accepted_and_lookalikes_are_not(self):
        allowed = json.load(open("config/ai_inbox.json"))["licenses"]
        self.assertTrue(ai_inbox.license_ok("google-vertex-ai-generative-terms", allowed))
        self.assertFalse(ai_inbox.license_ok("google-gemini-api-terms", allowed))
        self.assertFalse(ai_inbox.license_ok("vertex-ai", allowed))
