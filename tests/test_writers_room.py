"""Writers' room: five angles, a critic, the best draft goes on the post; below the bar it is sent back."""
import json
import unittest

from pln_agent import writers_room
from tests.helpers import make

GOOD = ["SEAT BELT, DIFFERENT USE", "NOBODY ESCAPES THIS", "YOU'RE IN THE PASSENGER SEAT", "FAIR OR FOUL?", "WAIT FOR THE TAP"]


def fake_llm(scores, hooks=GOOD):
    def call(system, messages, max_tokens=0):
        if system.startswith("You score"):
            return json.dumps({"scores": [{"i": i, "hook": s, "specific": s, "accurate": s, "payoff": s, "dq": False, "note": "ok"}
                                          for i, s in enumerate(scores)]})
        return "```json\n" + json.dumps({"drafts": [{"angle": a, "hook": f"**{h}**", "caption": "He had nowhere to go. Fair finish?",
                                                     "cta": "Tag someone who would tap"} for a, h in zip(writers_room.ANGLES, hooks)]}) + "\n```"
    return call


class WritersRoomTests(unittest.TestCase):
    def _variant(self, db):
        v = db.all("variants", "state='in_review'")[0]
        return v["variant_id"]

    def test_best_draft_goes_on_the_post(self):
        db, _ = make()
        vid = self._variant(db)
        room = writers_room.write(db, vid, llm=fake_llm([5, 9, 6, 7, 4]))
        self.assertEqual(room["pick"], 1)
        v = db.get("variants", "variant_id", vid)
        self.assertEqual(v["hook"], "Nobody Escapes This")
        self.assertEqual(v["onscreen_text"][0]["text"], "NOBODY ESCAPES THIS")  # markdown stripped, burned-in text updated
        self.assertEqual(v["writers_status"], "done")
        self.assertEqual(v["render_status"], "queued")
        self.assertEqual(len(v["treatment"]["writers_room"]["drafts"]), 5)
        self.assertTrue(v["treatment"]["overlay"]["hook_custom"])

    def test_numbers_are_disqualified(self):
        db, _ = make()
        vid = self._variant(db)
        hooks = ["8 SECONDS FLAT"] + GOOD[1:]
        room = writers_room.write(db, vid, llm=fake_llm([10, 7, 7, 7, 7], hooks))
        self.assertEqual(room["drafts"][0]["score"], 0.0)
        self.assertNotEqual(room["pick"], 0)

    def test_below_the_bar_goes_back(self):
        db, _ = make()
        vid = self._variant(db)
        room = writers_room.write(db, vid, llm=fake_llm([3, 4, 2, 5, 1]))
        self.assertEqual(room["status"], "below_bar")
        v = db.get("variants", "variant_id", vid)
        self.assertEqual(v["state"], "needs_fix")
        writers_room.switch(db, vid, 2, "reviewer")  # a reviewer can still pick one
        v = db.get("variants", "variant_id", vid)
        self.assertEqual(v["hook"], "You're In The Passenger Seat")
        self.assertEqual(v["writers_status"], "done")

    def test_run_pending_only_takes_pending_and_survives_failure(self):
        db, _ = make()
        vids = [v["variant_id"] for v in db.all("variants", "state='in_review'")][:2]
        db.update("variants", "variant_id", vids[0], {"writers_status": "pending"})
        out = writers_room.run_pending(db, llm=lambda *a, **k: "not json")
        self.assertEqual([o["variant_id"] for o in out], [vids[0]])
        self.assertEqual(out[0]["status"], "failed")
        self.assertEqual(db.get("variants", "variant_id", vids[0])["writers_status"], "failed")


if __name__ == "__main__":
    unittest.main()
