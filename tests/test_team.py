"""Agent Team: roster, parameter overrides reaching the agents, directives, chat and team challenges (fake LLM)."""
import json
import unittest

from pln_agent import team, team_params

from tests.helpers import make


class FakeLLM:
    def __init__(self):
        self.calls = []

    def __call__(self, system, messages, max_tokens=2500):
        self.calls.append((system, messages))
        who = system.split("YOU ARE: ")[1].split(".")[0]
        prompt = messages[-1]["content"]
        if "synthesise ONE recommendation" in prompt:
            return json.dumps({"summary": "Restart Putt Tour posting first.", "actions": [
                {"action": "Post 3 Putt Tour reels this week", "owner": "publishing", "why": "silent 90 days", "evidence": "last post Jul",
                 "priority": "P0", "param_change": {"agent": "migration", "param": "silent_days", "value": 10}}],
                "disagreements": [], "open_questions": [], "needs_from_manager": ["approve drafts"]})
        if "Teammates asked you" in prompt:
            return json.dumps({"answers": [{"from": "cgo", "question": "q", "answer": f"{who} answer"}]})
        if "put this challenge to the whole team" in prompt:
            asks = [{"agent": "scout", "question": "How many Putt Tour moments?"}] if who == "Chief Growth Officer" else []
            return "```json\n" + json.dumps({"position": f"{who} view", "recommendations": [{"action": "x", "why": "y", "evidence": "z"}],
                                             "asks": asks, "confidence": "medium"}) + "\n```"
        return f"{who} reply to: {prompt}"


class TeamTests(unittest.TestCase):
    def setUp(self):
        self.db, self.s = make(packs=True)
        self.llm = FakeLLM()
        team.set_llm(self.llm)
        self.addCleanup(team.set_llm, None)

    def test_roster_has_all_ten_with_rules(self):
        r = team.roster(self.db)
        self.assertEqual(len(r), 10)
        self.assertTrue(all(a["rules"] and a["mission"] for a in r))
        for a in r:
            self.assertIsInstance(team.status_line(self.db, a["key"]), str)

    def test_parameters_change_agent_behaviour(self):
        from pln_agent.growth_officer import goals_for
        self.assertEqual(goals_for(self.db)["rules"]["max_shift_per_week"], 0.05)
        team.set_param(self.db, "cgo", "max_shift_per_week", "0.1", "bill")
        self.assertEqual(goals_for(self.db)["rules"]["max_shift_per_week"], 0.1)
        with self.assertRaises(ValueError):
            team.set_param(self.db, "cgo", "max_shift_per_week", "0.9", "bill")  # outside the allowed range
        team.set_param(self.db, "migration", "silent_days", "30", "bill")
        from pln_agent.migration_tracking import track
        self.assertEqual(track(self.db, persist=False)["rules"]["silent_days"], 30)
        team.set_param(self.db, "publishing", "exploration_slot", "false", "bill")
        self.assertIs(team_params.get(self.db, "publishing", "exploration_slot", True), False)
        team_params.clear(self.db, "cgo", "max_shift_per_week", "bill")
        self.assertEqual(goals_for(self.db)["rules"]["max_shift_per_week"], 0.05)

    def test_directives_reach_the_agent_prompt_and_chat_keeps_history(self):
        team.add_directive(self.db, "community", "Never reply to rival promoters.", "bill")
        team.add_directive(self.db, "team", "Putt Tour is the priority through November.", "bill")
        t = team.chat(self.db, "community", "What are fans asking?", "bill")
        system = self.llm.calls[-1][0]
        self.assertIn("Never reply to rival promoters.", system)
        self.assertIn("Putt Tour is the priority", system)
        self.assertIn("DATA (your latest snapshot", system)
        team.chat(self.db, "community", "And yesterday?", "bill", t["thread_id"])
        self.assertEqual(len(self.llm.calls[-1][1]), 3)  # user, assistant, user
        self.assertEqual([m["role"] for m in team.messages(self.db, t["thread_id"])], ["manager", "agent", "manager", "agent"])

    def test_challenge_liaison_synthesis_and_adopt(self):
        t = team.start_challenge(self.db, "How do we grow Putt Tour?", "bill", background=False)
        msgs = team.messages(self.db, t["thread_id"])
        rounds = [(m.get("meta") or {}).get("round") for m in msgs]
        self.assertEqual(rounds.count("perspective"), 10)
        self.assertIn("liaison", rounds)  # scout answered the CGO's question
        self.assertEqual(rounds[-1], "synthesis")
        th = self.db.get("team_threads", "thread_id", t["thread_id"])
        self.assertEqual(th["status"], "done")
        r = team.adopt(self.db, t["thread_id"], 0, "bill", apply_param=True)
        self.assertEqual(r["directive"]["agent_key"], "publishing")
        self.assertEqual(team_params.get(self.db, "migration", "silent_days", 14), 10)
        self.assertEqual(self.db.get("team_threads", "thread_id", t["thread_id"])["result"]["adopted"], [0])

    def test_llm_down_falls_back_to_data(self):
        def down(*a, **k):
            raise team.TeamLLMError("AccessDenied")
        team.set_llm(down)
        t = team.chat(self.db, "cgo", "status?", "bill")
        m = team.messages(self.db, t["thread_id"])[-1]
        self.assertIn("Claude isn't reachable", m["content"])
        c = team.start_challenge(self.db, "x", "bill", background=False)
        self.assertEqual(self.db.get("team_threads", "thread_id", c["thread_id"])["result"]["error"], "AccessDenied")

    def test_pages_render(self):
        from fastapi.testclient import TestClient
        from pln_agent.web import app as web
        web._db = self.db
        c = TestClient(web.app)
        t = team.start_challenge(self.db, "How do we grow Putt Tour?", "bill", background=False)
        for path, needle in (("/team", "Challenge the team"), ("/team/agent/migration", "Decline alert threshold"),
                             (f"/team/challenge/{t['thread_id']}", "Restart Putt Tour posting first.")):
            r = c.get(path)
            self.assertEqual(r.status_code, 200, path)
            self.assertIn(needle, r.text, path)
        r = c.post("/team/agent/cgo/chat", data={"text": "hello", "actor": "bill"}, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        r = c.post("/team/agent/cgo/param", data={"name": "posting_gap_days", "value": "10", "actor": "bill"}, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertEqual(team_params.get(self.db, "cgo", "posting_gap_days", 7), 10)


if __name__ == "__main__":
    unittest.main()
