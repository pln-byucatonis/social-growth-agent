"""Music library (pln_agent/music.py): generated beds with provenance, licensed tracks, approval by ear, clearance."""
import json
import unittest

from pln_agent import edit_fx, music
from tests.helpers import make

SIDE = {"model": "ACE-Step", "model_version": "1.5", "license": "Apache-2.0", "prompt": "tense drums, no vocals",
        "seed": 18446744073709551615, "sha256": "ab" * 32, "mood": "tense", "brands": ["carjitsu"], "bpm": 100,
        "duration_s": 45, "instrumental": True}
FILES = {"social-agent/music/inbox/carjitsu/tense-01.wav": None, "social-agent/music/inbox/carjitsu/tense-01.json": SIDE,
         "social-agent/music/inbox/x/nc-01.wav": None, "social-agent/music/inbox/x/nc-01.json": {**SIDE, "license": "CC-BY-NC-4.0"},
         "social-agent/music/inbox/x/song.wav": None, "social-agent/music/inbox/x/song.json": {**SIDE, "instrumental": False},
         "social-agent/music/inbox/x/orphan.json": SIDE}


def lister(bucket, prefix):
    return [k for k in FILES if k.startswith(prefix)]


def reader(bucket, key):
    return json.dumps(FILES[key]).encode()


class MusicTests(unittest.TestCase):
    def test_import_keeps_only_commercial_instrumental_beds_with_provenance(self):
        db, _ = make(packs=False)
        r = music.import_inbox(db, "b", lister, reader)
        self.assertEqual(len(r["added"]), 1)
        self.assertEqual(len(r["skipped"]), 3)
        bed = db.get("music_beds", "bed_id", r["added"][0])
        self.assertEqual((bed["status"], bed["source"], bed["seed"]), ("pending", "generated", "18446744073709551615"))
        self.assertEqual(bed["path"], "s3://b/social-agent/music/inbox/carjitsu/tense-01.wav")
        self.assertEqual(music.import_inbox(db, "b", lister, reader)["added"], [])  # idempotent

    def test_nothing_plays_until_approved_and_cleared_for_the_platform(self):
        db, _ = make(packs=False)
        bid = music.import_inbox(db, "b", lister, reader)["added"][0]
        fx = edit_fx.settings_for({"brand_id": "carjitsu", "treatment": {}})
        self.assertIsNone(edit_fx.music_bed("carjitsu", fx, db, "instagram", "v1"))
        music.review(db, bid, True, "mike")
        self.assertEqual(edit_fx.music_bed("carjitsu", fx, db, "instagram", "v1")["bed_id"], bid)
        self.assertIsNone(edit_fx.music_bed("putt_tour", fx, db, "instagram", "v1"))  # other brand
        lic = music.add_licensed(db, "s3://b/lib/t.mp3", "T", "Library X", "Business plan", ["instagram"], [], "upbeat", "mike")
        music.review(db, lic, True, "mike")
        self.assertIsNone(music.pick(db, "putt_tour", "tiktok", "v1"))  # licence doesn't cover TikTok
        self.assertEqual(music.pick(db, "putt_tour", "instagram", "v1")["bed_id"], lic)
        fx["music"]["enabled"] = False
        self.assertIsNone(edit_fx.music_bed("carjitsu", fx, db, "instagram", "v1"))

    def test_pick_prefers_brand_mood_and_is_stable_per_post(self):
        db, _ = make(packs=False)
        ids = [music.add_licensed(db, f"s3://b/{m}{i}.mp3", "", "L", "lic", ["tiktok"], [], m, "mike")
               for m in ("tense", "upbeat") for i in range(3)]
        for b in ids:
            music.review(db, b, True, "mike")
        picks = {music.pick(db, "putt_tour", "tiktok", f"v{i}")["mood"] for i in range(20)}
        self.assertEqual(picks, {"upbeat"})
        self.assertEqual(music.pick(db, "putt_tour", "tiktok", "v7"), music.pick(db, "putt_tour", "tiktok", "v7"))

    def test_licensed_needs_provider_license_and_platform(self):
        db, _ = make(packs=False)
        for args in (("https://x/t.mp3", "L", "lic", ["tiktok"]), ("s3://b/t.mp3", "", "lic", ["tiktok"]),
                     ("s3://b/t.mp3", "L", "lic", []), ("s3://b/t.txt", "L", "lic", ["tiktok"])):
            with self.assertRaises(music.MusicError):
                music.add_licensed(db, args[0], "", args[1], args[2], args[3], [], "", "mike")


if __name__ == "__main__":
    unittest.main()
