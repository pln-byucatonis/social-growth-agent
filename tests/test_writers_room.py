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

    def test_spelled_out_counts_are_disqualified_too(self):
        db, _ = make()
        vid = self._variant(db)
        room = writers_room.write(db, vid, llm=fake_llm([10, 7, 7, 7, 7], ["TEN SECONDS TO SURVIVE"] + GOOD[1:]))
        self.assertEqual(room["drafts"][0]["score"], 0.0)
        self.assertFalse(writers_room._counts("One touch ends it"))

    def test_reviewer_can_go_back_to_the_original(self):
        db, _ = make()
        vid = self._variant(db)
        before = db.get("variants", "variant_id", vid)["hook"]
        writers_room.write(db, vid, llm=fake_llm([5, 9, 6, 7, 4]))
        writers_room.write(db, vid, llm=fake_llm([5, 6, 9, 7, 4]))  # a rewrite keeps the very first copy as the original
        writers_room.switch(db, vid, -1, "reviewer")
        v = db.get("variants", "variant_id", vid)
        self.assertEqual(v["hook"].lower(), before.lower())
        self.assertEqual(v["treatment"]["writers_room"]["pick"], -1)

    def test_post_page_shows_the_room(self):
        from fastapi.testclient import TestClient
        from pln_agent.web import app as web
        db, s = make()
        web._db, web.settings = db, s
        vid = self._variant(db)
        writers_room.write(db, vid, llm=fake_llm([5, 9, 6, 7, 4]))
        c = TestClient(web.app)
        r = c.get(f"/variant/{vid}")
        self.assertIn("Writers&#39; room", r.text.replace("'", "&#39;"))
        self.assertIn("before the room", r.text)
        r = c.post(f"/variant/{vid}/writers/-1", follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertEqual(db.get("variants", "variant_id", vid)["treatment"]["writers_room"]["pick"], -1)

    def test_invented_details_zero_and_stock_phrases_cost(self):
        db, _ = make()
        vid = self._variant(db)

        def call(system, messages, max_tokens=0):
            if system.startswith("You score"):
                return json.dumps({"scores": [{"i": 0, "hook": 9, "specific": 9, "accurate": 8, "payoff": 9, "invented": ["the crowd"]},
                                              *[{"i": i, "hook": 8, "specific": 8, "accurate": 8, "payoff": 8, "invented": []} for i in (1, 2, 3, 4)]]})
            caps = ["The crowd loses it.", "Stay until the end for this one.", "Nowhere to go. Fair finish?", "Who saw that coming?", "Thoughts?"]
            return json.dumps({"drafts": [{"angle": a, "hook": h, "caption": c, "cta": "Tag someone"}
                                          for a, h, c in zip(writers_room.ANGLES, GOOD, caps)]})
        room = writers_room.write(db, vid, llm=call)
        sc = [d["score"] for d in room["drafts"]]
        self.assertEqual(sc[0], 0.0)
        self.assertIn("invented: the crowd", room["drafts"][0]["note"])
        self.assertEqual(sc[1], 8.0 - writers_room.CLICHE_PENALTY)
        self.assertEqual(sc[2], 8.0)
        self.assertIn(room["pick"], (2, 3, 4))

    def test_an_angle_that_keeps_winning_pays_for_it(self):
        db, _ = make()
        vids = [v["variant_id"] for v in db.all("variants", "state='in_review'")]
        acct = db.get("variants", "variant_id", vids[0])["account_id"]
        same = [v for v in vids if db.get("variants", "variant_id", v)["account_id"] == acct]
        self.assertGreaterEqual(len(same), 1)
        for k in range(4):  # four earlier posts on this account all won with the payoff tease
            db.insert("variants", {**db.get("variants", "variant_id", same[0]), "variant_id": f"old-{k}", "writers_status": "done",
                                   "treatment": {"writers_room": {"pick": 0, "drafts": [{"angle": writers_room.ANGLES[4]}]}}})
        room = writers_room.write(db, same[0], llm=fake_llm([7, 7, 7, 7, 7.5]))
        self.assertLess(room["drafts"][4]["score"], 7.5)
        self.assertIn("keeps winning", room["drafts"][4]["note"])
        self.assertNotEqual(room["pick"], 4)

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
