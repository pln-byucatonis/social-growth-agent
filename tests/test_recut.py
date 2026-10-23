"""Editor → render → critic (pln_agent/editor.py, critic.py, recut.py) with a stand-in model."""
import json
import shutil
import unittest

from pln_agent import critic, editor, recut, team
from tests.helpers import CJ, make

HAS_FFMPEG = bool(shutil.which("ffmpeg"))
VID = f"{CJ}:carjitsu.instagram.carjitsu_championship"
CUT = {"readable": True, "why": "reversal is clear", "what_happens": "the bottom fighter flips the top one",
       "payoff": {"t": 11.5, "what": "the flip", "strength": 8, "replay_worthy": True, "replay_window": [10.8, 12.6]},
       "shots": [{"in": 8.5, "out": 10.0, "role": "setup", "why": "scramble"}, {"in": 10.0, "out": 13.0, "role": "payoff", "why": "flip"},
                 {"in": 14.0, "out": 15.5, "role": "reaction", "why": "corner"}], "cut_out": ["17-26 rules recap"]}
GOOD = {s: 4 for s in critic.CHECKS}


class Model:
    """Answers as the editor or the critic, from queues of replies; records what it was shown."""
    def __init__(self, cuts, verdicts):
        self.cuts, self.verdicts, self.calls = list(cuts), list(verdicts), []

    def __call__(self, system, messages, max_tokens):
        content = messages[0]["content"]
        if "senior editor" not in system and "toughest reviewer" not in system:
            return '{"frames": []}'  # the art director placing text: nothing on screen to avoid
        self.calls.append({"who": "editor" if "senior editor" in system else "critic",
                           "images": sum(1 for c in content if c.get("type") == "image"),
                           "text": " ".join(c.get("text", "") for c in content if c.get("type") == "text")})
        return json.dumps(self.cuts.pop(0) if self.calls[-1]["who"] == "editor" else self.verdicts.pop(0))


class Base(unittest.TestCase):
    def setUp(self):
        self.old = team._llm

    def tearDown(self):
        team._llm = self.old


class CheckTests(unittest.TestCase):
    def test_check_tidies_and_refuses_bad_cuts(self):
        ed = editor.check(CUT, 4.0, 20.0, {"max_s": 35})
        self.assertEqual(ed["length_s"], 6.0)
        overl = {**CUT, "shots": [{"in": 8, "out": 11}, {"in": 10, "out": 12.5}]}
        self.assertEqual([s["in"] for s in editor.check(overl, 4, 20, {})["shots"]], [8.0, 11.0])
        with self.assertRaises(editor.EditorError):
            editor.check({**CUT, "shots": [{"in": 4, "out": 40}]}, 4, 50, {"max_s": 35})
        with self.assertRaises(editor.EditorError):
            editor.check({**CUT, "payoff": {"t": 30}}, 4, 50, {})
        self.assertEqual(editor.check({"readable": False, "why": "dark car"}, 4, 20, {})["shots"], [])

    def test_pass_bar(self):
        self.assertEqual(critic.judge({"scores": GOOD, "verdict": "pass"})["verdict"], "pass")
        self.assertEqual(critic.judge({"scores": {**GOOD, "no_dead_air": 2}, "verdict": "pass"})["verdict"], "recut")
        self.assertEqual(critic.judge({"scores": {**GOOD, "payoff_visible": 3}, "verdict": "pass"})["verdict"], "recut")
        self.assertEqual(critic.judge({"verdict": "nonsense"})["verdict"], "recut")
        self.assertEqual(critic.judge({"scores": {**GOOD, "replay_earned": 1}, "verdict": "pass"})["verdict"], "pass")


@unittest.skipUnless(HAS_FFMPEG, "ffmpeg not installed")
class FlowTests(Base):
    def test_pull_then_pass(self):
        db, s = make()
        n = recut.pull(db, "test")
        self.assertGreater(n, 0)
        self.assertEqual(db.get("variants", "variant_id", VID)["state"], "recut")
        m = team._llm = Model([CUT], [{"scores": GOOD, "verdict": "pass"}])
        r = recut.process(db, VID, s)
        self.assertEqual(r["outcome"], "passed")
        v = db.get("variants", "variant_id", VID)
        self.assertIn(v["state"], ("in_review", "needs_fix"))
        self.assertEqual([(x["source_in"], x["source_out"]) for x in v["edl"]["segments"]], [(8.5, 10.0), (10.0, 13.0), (14.0, 15.5)])
        self.assertEqual(v["treatment"]["editor"]["payoff_at_s"], 3.0)
        self.assertTrue(v["treatment"]["editor"]["replay_worthy"])
        self.assertEqual(v["treatment"]["critic"]["verdict"], "pass")
        self.assertGreater(m.calls[0]["images"], 0)  # the editor watched frames of the source
        self.assertGreater(m.calls[1]["images"], 0)  # and the critic watched the render
        self.assertIn("rough moments", m.calls[0]["text"])

    def test_recut_with_notes_then_drop(self):
        db, s = make()
        recut.pull(db, "test")
        bad = {"scores": {**GOOD, "no_dead_air": 1}, "verdict": "recut", "problems": ["3 s of corner talk at the end"], "fix": "end at 15.0"}
        m = team._llm = Model([CUT, CUT], [bad, bad])
        r = recut.process(db, VID, s)
        self.assertEqual(r["outcome"], "dropped")
        self.assertIn("corner talk", m.calls[2]["text"])  # the second cut got the critic's notes
        v = db.get("variants", "variant_id", VID)
        self.assertEqual((v["state"], v["treatment"]["recut"]["attempts"]), ("archived", 2))

    def test_unreadable_is_dropped_without_rendering(self):
        db, s = make()
        recut.pull(db, "test")
        team._llm = Model([{"readable": False, "why": "bodies tangled in a dark car"}], [])
        renders = []
        r = recut.process(db, VID, s, render=lambda vid: renders.append(vid) or {"status": "rendered"})
        self.assertEqual((r["outcome"], renders), ("dropped", []))
        self.assertIn("dark car", db.get("variants", "variant_id", VID)["treatment"]["recut"]["why"])

    def test_replay_vetoed_by_critic_rerenders_without_it(self):
        db, s = make()
        recut.pull(db, "test")
        team._llm = Model([CUT], [{"scores": {**GOOD, "replay_earned": 3}, "verdict": "pass"}])
        self.assertEqual(recut.process(db, VID, s)["outcome"], "passed")
        team._llm = Model([CUT], [{"scores": {**GOOD, "replay_earned": 2}, "verdict": "pass"}])
        db.update("variants", "variant_id", VID, {"state": "recut", "treatment": {**db.get("variants", "variant_id", VID)["treatment"],
                                                                                  "recut": {"status": "pending", "attempts": 0}}})
        renders = []
        from pln_agent.media import render_variant
        r = recut.process(db, VID, s, render=lambda vid: renders.append(vid) or render_variant(db, vid, s))
        self.assertEqual((r["outcome"], len(renders)), ("passed", 2))  # rendered again, without the replay
        from pln_agent import edit_fx
        self.assertFalse(edit_fx.settings_for(db.get("variants", "variant_id", VID))["replay"]["enabled"])


class GateTests(unittest.TestCase):
    def test_new_drafts_wait_for_the_writers_then_go_to_the_editor(self):
        db, _ = make()
        db.update("variants", "variant_id", VID, {"writers_status": "pending"})
        n = recut.gate_new(db)
        self.assertGreater(n, 0)
        self.assertEqual(db.get("variants", "variant_id", VID)["state"], "in_review")  # still with the writers
        db.update("variants", "variant_id", VID, {"writers_status": "done"})
        recut.gate_new(db)
        self.assertEqual(db.get("variants", "variant_id", VID)["state"], "recut")
        self.assertEqual(recut.gate_new(db), 0)

    def test_page_shows_the_editor_card_and_recut_button(self):
        from fastapi.testclient import TestClient
        from pln_agent.web import app as web
        db, s = make()
        web._db, web.settings = db, s
        c = TestClient(web.app)
        r = c.post(f"/variant/{VID}/recut", follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertEqual(db.get("variants", "variant_id", VID)["state"], "recut")
        self.assertIn("Editor &amp; critic", c.get(f"/variant/{VID}").text)
        self.assertIn("With the editor", c.get("/queue?state=recut").text)


if __name__ == "__main__":
    unittest.main()
