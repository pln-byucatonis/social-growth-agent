"""Episode inbox (pln_agent/episode_inbox.py): pipeline highlights become PLN-owned, rights-clear assets that use the
captioned 9:16 as is, and never arrive twice through the ops clip import."""
import json
import unittest

from pln_agent import episode_inbox, media, rights
from pln_agent.ops_sources import clip_asset_id, import_ops_clips
from tests.helpers import make

P = "social-agent/episodes/inbox/carjitsu-20/"
A = "assets/carjitsu-20/highlights/"
SIDE = {
    "schema": "pln-episode-clip/1", "id": "ep-carjitsu-20-h1", "episode_key": "carjitsu-20", "episode_title": "CarJitsu 20",
    "property": "CarJitsu", "aired": "2026-11-17", "rank": 1, "score": 100, "kind": "finish",
    "title": "First Ever CarJitsu Armbar", "hook": "He pulled off an armbar INSIDE a car!",
    "why": "A unique and difficult submission.", "people": ["Arthur Walcott-Ceesay", "Corey Hynson", "unclear"],
    "duration_s": 35.0, "width": 1080, "height": 1920, "captioned": True,
    "media": {"vertical": A + "CarJitsu20_H1_First-Ever-CarJitsu-Armbar_9x16.mp4",
              "wide": A + "CarJitsu20_H1_First-Ever-CarJitsu-Armbar_16x9.mp4",
              "srt_en": A + "CarJitsu20_H1_First-Ever-CarJitsu-Armbar_en.srt",
              "srt_es": A + "CarJitsu20_H1_First-Ever-CarJitsu-Armbar_es.srt"},
    "social": [{"platform": "TikTok", "hook_first_2s": "He pulled off an armbar INSIDE a car!", "caption": "c"}],
    "notes_for_editor": ["Ensure the sponsor reads for Roobet are clear.", "Bleep 'shit' at 02:48."],
    "master": "PLNstudio/CarJitsu_20_11.17.26_final.mp4", "run_id": "20261128T130004Z",
}
SRT = "1\n00:00:00,010 --> 00:00:01,880\nOh, that's tight.\n\n2\n00:00:01,880 --> 00:00:05,320\nHe's going for\nthe armbar.\n"
FILES = {
    P + "CarJitsu20_H1.json": SIDE,
    P + "music.json": {**SIDE, "id": "ep-carjitsu-20-h2", "notes_for_editor": ["Intro music that may be copyrighted."]},
    P + "junk.json": {"schema": "something-else"},
    SIDE["media"]["srt_en"]: SRT,
}


def lister(bucket, prefix):
    return [k for k in FILES if k.startswith(prefix)]


def reader(bucket, key):
    v = FILES[key]
    return (v if isinstance(v, str) else json.dumps(v)).encode()


class EpisodeInboxTests(unittest.TestCase):
    def test_import_is_idempotent_and_skips_what_is_not_an_episode_clip(self):
        db, _ = make(packs=False)
        r = episode_inbox.import_inbox(db, "pln-dam", lister, reader)
        self.assertEqual(sorted(r["added"]), sorted([clip_asset_id("ep-carjitsu-20-h1"), clip_asset_id("ep-carjitsu-20-h2")]))
        self.assertEqual([s["key"] for s in r["skipped"]], [P + "junk.json"])
        self.assertEqual(episode_inbox.import_inbox(db, "pln-dam", lister, reader)["added"], [])

    def test_pln_owned_master_is_rights_clear_with_the_gambling_label(self):
        db, _ = make(packs=False)
        episode_inbox.import_inbox(db, "pln-dam", lister, reader)
        a = db.get("assets", "asset_id", clip_asset_id("ep-carjitsu-20-h1"))
        self.assertEqual(a["brand_id"], "carjitsu")
        self.assertEqual(a["media_path"], "s3://pln-dam/" + SIDE["media"]["vertical"])
        self.assertIn("PLN-owned master", a["provenance"])
        self.assertEqual([p["name"] for p in a["people"]], ["Arthur Walcott-Ceesay", "Corey Hynson"])
        ev = rights.evaluate(a)
        self.assertTrue(ev.ok, ev.blockers)
        self.assertIn("gambling", ev.review_flags)
        self.assertIn("Hook ideas:", a["description"])

    def test_possibly_copyrighted_music_blocks_until_cleared(self):
        db, _ = make(packs=False)
        episode_inbox.import_inbox(db, "pln-dam", lister, reader)
        ev = rights.evaluate(db.get("assets", "asset_id", clip_asset_id("ep-carjitsu-20-h2")))
        self.assertFalse(ev.ok)
        self.assertIn("audio present but music/sound not cleared", ev.blockers)

    def test_transcript_comes_from_the_english_srt_and_captions_are_not_burned_again(self):
        db, _ = make(packs=False)
        episode_inbox.import_inbox(db, "pln-dam", lister, reader)
        a = db.get("assets", "asset_id", clip_asset_id("ep-carjitsu-20-h1"))
        self.assertEqual(a["transcript"], [{"start": 0.01, "end": 1.88, "text": "Oh, that's tight."},
                                           {"start": 1.88, "end": 5.32, "text": "He's going for the armbar."}])
        self.assertTrue(media.captions_burned_in(a))
        self.assertFalse(media.captions_burned_in({"probe": {}}))
        self.assertEqual(a["moments"], [{"start": 0.0, "end": 35.0, "label": "First Ever CarJitsu Armbar", "kind": "payoff"}])


class OpsImportSkipsPipelineClipsTests(unittest.TestCase):
    class Ops:
        def __init__(self):
            self.sql = []

        def query(self, sql, params=None):
            self.sql.append(sql)
            return []

    def test_ranked_and_explicit_imports_both_exclude_ep_clips(self):
        db, _ = make(packs=False)
        ops = self.Ops()
        import_ops_clips(db, ops, "b", limit=5)
        import_ops_clips(db, ops, "b", clip_ids=["ep-carjitsu-20-h1", "CJ:1"])
        self.assertTrue(all("id NOT LIKE 'ep-%'" in s for s in ops.sql), ops.sql)


if __name__ == "__main__":
    unittest.main()
