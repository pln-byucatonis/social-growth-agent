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
        if "Write FIVE drafts" in system:  # the writers' room
            self.writers = getattr(self, "writers", []) + [messages[0]["content"]]
            return json.dumps({"drafts": [{"angle": "stakes", "hook": "Flipped from the bottom", "caption": "He was stuck. Then he wasn't.",
                                           "cta": "Who wins the rematch?"}]})
        if "few words burned" in system:  # the on-screen words for the cut
            self.words = getattr(self, "words", []) + [messages[0]["content"]]
            return json.dumps(getattr(self, "word_reply", {"hook": "", "payoff_word": "FROM THE BOTTOM.", "reaction_word": "", "why": "flip"}))
        if "score social copy" in system:
            return json.dumps({"scores": [{"i": 0, "hook": 9, "specific": 9, "accurate": 9, "payoff": 9, "dq": 0, "invented": []}]})
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
        self.assertEqual(critic.judge({"scores": {**GOOD, "fresh": 1}, "verdict": "recut"})["verdict"], "rewrite")
        self.assertEqual(critic.judge({"scores": {**GOOD, "fresh": 1, "no_dead_air": 2}, "verdict": "recut"})["verdict"], "recut")
        self.assertEqual(critic.judge({"scores": GOOD, "verdict": "recut", "copy_problems": ["hook repeats"]})["verdict"], "rewrite")


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

    def test_replay_crops_in_on_the_payoff(self):
        from pln_agent.media import probe_output, render_variant
        db, s = make()
        recut.pull(db, "test")
        cut = {**CUT, "payoff": {**CUT["payoff"], "focus": {"x": 0.8, "y": 0.3}, "replay_zoom": 2.2, "replay_window": [3.0, 4.0]}}
        team._llm = Model([cut], [{"scores": GOOD, "verdict": "pass"}])
        seen = []
        self.assertEqual(recut.process(db, VID, s, render=lambda vid: seen.append(render_variant(db, vid, s)) or seen[-1])["outcome"], "passed")
        ed = db.get("variants", "variant_id", VID)["treatment"]["editor"]
        self.assertEqual((ed["replay_focus"], ed["replay_zoom"]), ({"x": 0.8, "y": 0.3}, 1.5))  # capped: more is mush
        self.assertEqual(ed["replay_window"], [10.3, 12.7])  # a window that missed the payoff is moved onto it
        self.assertIn("punched in 1.5x", seen[0]["detail"])
        self.assertEqual(probe_output(seen[0]["path"])["height"], 1920)

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

    def test_weak_copy_goes_to_the_writers_not_the_editor(self):
        db, s = make()
        recut.pull(db, "test")
        stale = {"scores": {**GOOD, "fresh": 1}, "verdict": "recut", "copy_problems": ["hook repeats the last four posts"]}
        m = team._llm = Model([CUT], [stale, {"scores": GOOD, "verdict": "pass"}])
        r = recut.process(db, VID, s)
        self.assertEqual(r["outcome"], "passed")
        self.assertEqual(sum(1 for c in m.calls if c["who"] == "editor"), 1)  # the cut was kept
        self.assertIn("repeats the last four", json.dumps(m.writers[-1]))  # the rewrite got the critic's note
        self.assertIn("bottom fighter flips", json.dumps(m.writers[0]))  # and what the editor saw happen
        v = db.get("variants", "variant_id", VID)
        self.assertEqual((v["hook"], v["state"] in ("in_review", "needs_fix")), ("Flipped From The Bottom", True))
        self.assertEqual(v["treatment"]["recut"]["rewrites"], 1)

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


class AllowanceTests(Base):
    def test_one_look_per_clip_and_pause_when_the_allowance_runs_out(self):
        from pln_agent import editor as ed_mod
        db, s = make()
        recut.pull(db, "test")
        sibs = [v["variant_id"] for v in db.all("variants", "asset_id=? AND state='recut'", [CJ])]
        self.assertGreater(len(sibs), 1)
        m = team._llm = Model([{"readable": False, "why": "dark car"}], [])
        outs = [recut.process(db, vid, s, render=lambda vid: {"status": "rendered"}) for vid in sibs]
        self.assertEqual({o["outcome"] for o in outs}, {"dropped"})
        self.assertEqual(sum(1 for c in m.calls if c["who"] == "editor"), 1)  # watched once for every account's draft

        db2, s2 = make()
        recut.pull(db2, "test")

        def out_of_tokens(*a, **k):
            raise ed_mod.Throttled("Too many tokens per day")
        team._llm = out_of_tokens
        r = recut.run_pending(db2, s2)
        self.assertEqual([x["outcome"] for x in r], ["paused"])
        self.assertEqual(recut.pending(db2), [])  # everything waits
        self.assertEqual({v["treatment"]["recut"]["status"] for v in db2.all("variants", "state='recut'")}, {"pending"})
        db2.kv_set(recut.PAUSE_KEY, "2000-01-01T00:00:00+00:00")
        self.assertTrue(recut.pending(db2))


class WordsTests(Base):
    def _posted(self, db, words, style="pill", hook="Clutch Putt Under Pressure"):
        """Another post on the same account that already carries these words."""
        import uuid
        from pln_agent.db import now_iso
        v = dict(db.get("variants", "variant_id", VID))
        v.update(variant_id=f"other-{uuid.uuid4().hex[:6]}", state="published", hook=hook, updated_at=now_iso(),
                 render_qc={"overlay_words": {"hook": hook.upper(), **words}, "overlay_style": f"{style}+kinetic+stamp"})
        db.insert("variants", v)

    def test_words_are_written_for_the_cut_and_never_repeat(self):
        from pln_agent import overlay_copy
        db, _ = make()
        editor.apply(db, VID, editor.check(CUT, 4, 20, {}))
        self._posted(db, {"payoff": "FROM THE BOTTOM."})
        self._posted(db, {"payoff": "FLIPPED."})
        m = team._llm = Model([], [])
        w = overlay_copy.write(db, VID)
        self.assertEqual(w["payoff"], "")  # the model's word was used on this account lately: refused
        self.assertIn("bottom fighter flips", json.dumps(m.words[0]))  # it was told what the editor saw
        self.assertIn("TAP.", json.dumps(m.words[0]))  # and the stock bank is off limits
        m.word_reply = {"hook": "Stuck Then Free", "payoff_word": "OUT THE BACK.", "reaction_word": "TAP.", "why": "x"}
        w = overlay_copy.write(db, VID)
        self.assertEqual((w["hook"], w["payoff"], w["reaction"]), ("STUCK THEN FREE", "OUT THE BACK.", ""))  # stock word refused
        t = db.get("variants", "variant_id", VID)["treatment"]["overlay"]
        self.assertEqual((t["stamp"], t["reaction"]), ("OUT THE BACK.", False))
        self.assertEqual(db.get("variants", "variant_id", VID)["onscreen_text"][0]["text"], "STUCK THEN FREE")

    def test_repeated_hook_shape_and_style_rotate(self):
        from pln_agent import overlay_copy
        db, _ = make()
        self._posted(db, {}, "pill", "Clutch Putt Under Pressure")
        self._posted(db, {}, "pill", "Clutch Putt From Distance")
        rec = overlay_copy.recent(db, db.get("variants", "variant_id", VID))
        self.assertFalse(overlay_copy.fresh_hook("Clutch putt for the win", rec))
        self.assertTrue(overlay_copy.fresh_hook("Off the rocks", rec))
        self.assertEqual(overlay_copy.pick_style(rec, "pill"), "knockout")
        team._llm = Model([], [])
        team._llm.word_reply = {"hook": "Clutch Putt Again", "payoff_word": "", "reaction_word": ""}
        w = overlay_copy.write(db, VID)
        self.assertNotIn("CLUTCH PUTT", w["hook"])
        self.assertEqual(w["style"], "knockout")

    def test_no_model_falls_back_to_unused_stock_words(self):
        from pln_agent import overlay_copy
        db, _ = make()
        self._posted(db, {"payoff": "TAP."})

        def down(*a, **k):
            raise RuntimeError("bedrock down")
        w = overlay_copy.write(db, VID, llm=down)
        self.assertEqual(w["by"], "bank")
        self.assertNotEqual(w["payoff"], "TAP.")


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
