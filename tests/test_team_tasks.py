"""Team tasks: adopted challenge actions become tracked tasks whose status follows real renders, drafts and schedules."""
import unittest
from datetime import datetime, timezone
from unittest import mock
from zoneinfo import ZoneInfo

from pln_agent import runsheet, team, team_tasks
from pln_agent.db import now_iso

from tests.helpers import CJ, PUTT, make

ACTIONS = [
    {"owner": "video", "priority": "P0", "action": "Render and QC today's priority clips in this order: (1) Davidson seatbelt choke 89.5, "
     "(2) Julian Moody hole-in-one 88.1. All via MediaConvert smart-crop."},
    {"owner": "scout", "priority": "P0", "action": "Confirm which brand the golf clip (Moody 88.1) belongs to and whether PLN has a "
     "viable moment of its own."},
    {"owner": "creative", "priority": "P0", "action": "Build creative for Davidson and Moody: medium caption, 4+ hashtags."},
    {"owner": "publishing", "priority": "P0", "action": "Schedule today: Post 1 CarJitsu 9:00pm, Post 2 Putt Tour 10:00pm, "
     "Post 3 WPL bridge post 10:30pm."},
    {"owner": "publishing", "priority": "P1", "action": "Post one WPL bridge post today — short caption, explicit tag to @putttour."},
    {"owner": "growth_experimentation", "priority": "P0", "action": "After today's posts go live, collect reach and follows per post."},
    {"owner": "cgo", "priority": "P2", "action": "Tell the sponsors."},
]
CLIPS = {"cj.mp4:10.0-20.0": CJ, "putt.mp4:30.0-40.0": PUTT}


class FakeHooks:
    def __init__(self):
        self.promoted, self.requested, self.rendered = [], [], []

    def promote(self, db, clip_ids):
        self.promoted += clip_ids
        return {"assets": [], "packs": [], "skipped": []}

    def regenerate(self, db, asset_id):
        self.regenerated = getattr(self, "regenerated", []) + [asset_id]

    def request_cut(self, db, vid, label):
        v = db.get("variants", "variant_id", vid)
        t = dict(v["treatment"])
        for c in t["duration_variants"]:
            if c["label"] == label:
                c["ops_vertical"] = {"label": f"agent-x-{label}", "status": "requested", "requested_at": now_iso(),
                                     "window": [c["segments"][0]["source_in"], c["segments"][0]["source_out"]]}
        db.update("variants", "variant_id", vid, {"treatment": t})
        self.requested.append((vid, label))
        return {"requested": [label]}

    def render(self, db, vid):
        self.rendered.append(vid)
        qc = {"passed": True, "errors": []}
        db.update("variants", "variant_id", vid, {"render_status": "rendered", "render_qc": qc})
        return {"status": "rendered", "qc": qc}


def mediaconvert_finishes(db):
    for v in db.all("variants"):
        t = dict(v.get("treatment") or {})
        changed = False
        for c in t.get("duration_variants") or []:
            if (c.get("ops_vertical") or {}).get("status") == "requested":
                c["ops_vertical"].update(status="ready", key=f"verticals/{c['ops_vertical']['label']}/x_9x16.mp4")
                changed = True
        if changed:
            db.update("variants", "variant_id", v["variant_id"], {"treatment": t})


class TeamTaskTests(unittest.TestCase):
    def setUp(self):
        self.db, self.s = make(packs=True)
        self.s = __import__("dataclasses").replace(self.s, timezone="America/New_York")
        for cid, aid, cap, pot, brand in (("cj.mp4:10.0-20.0", CJ, "Davidson secures seatbelt choke with two seconds remaining.", 89.5, "carjitsu"),
                                          ("cj.mp4:50.0-60.0", CJ, "Davidson locks shoulder strap choke on Moore.", 87.6, "carjitsu"),
                                          ("putt.mp4:30.0-40.0", PUTT, "Julian Moody holes it in one on the mini golf course.", 88.1, "putt_tour")):
            self.db.insert("scout_moments", {"clip_id": cid, "source_id": cid.split(":")[0], "brand_id": brand, "caption": cap,
                                             "potential": pot, "status": "candidate", "start_sec": 0, "end_sec": 10, "categories": []})
        self.thread = {"thread_id": "th-test", "kind": "challenge", "agent_key": "team", "title": "restart", "status": "done",
                       "created_by": "bill", "created_at": now_iso(), "result": {"summary": "s", "actions": ACTIONS}}
        self.db.insert("team_threads", self.thread)
        self.hooks = FakeHooks()
        p = mock.patch.object(team_tasks, "_asset_for", lambda db, cid: db.get("assets", "asset_id", CLIPS.get(cid, "")))
        p.start()
        self.addCleanup(p.stop)
        # evening slots on a fixed "today" so nothing has passed
        now = datetime(2026, 10, 21, 16, 0, tzinfo=timezone.utc)
        q = mock.patch.object(team_tasks, "datetime", wraps=datetime)
        dt = q.start()
        dt.now.side_effect = lambda tz=None: now if tz else now.replace(tzinfo=None)
        self.addCleanup(q.stop)

    def adopt_all(self):
        for i in range(len(ACTIONS)):
            team.adopt(self.db, "th-test", i, "bill")
        return {t["action_index"]: t for t in team_tasks.tasks(self.db, "th-test")}

    def advance(self):
        team_tasks.advance(self.db, self.s, None, hooks=self.hooks)
        return {t["action_index"]: t for t in team_tasks.tasks(self.db, "th-test")}

    def test_classify_and_resolve(self):
        kinds = [team_tasks.classify(a["owner"], a["action"]) for a in ACTIONS]
        self.assertEqual(kinds, ["render", "decision", "creative", "schedule", "post_on", "measure", "manual"])
        clips = team_tasks.resolve_clips(self.db, ACTIONS[0]["action"])
        self.assertEqual([c["clip_id"] for c in clips], ["cj.mp4:10.0-20.0", "putt.mp4:30.0-40.0"])  # score picks 89.5, not 87.6
        self.assertEqual([s["brands"] for s in team_tasks.parse_slots(ACTIONS[3]["action"])],
                         [["carjitsu"], ["putt_tour"], ["world_putting_league"]])

    def test_adopt_creates_task_and_backfill(self):
        r = team.adopt(self.db, "th-test", 0, "bill")
        self.assertEqual(r["task"]["kind"], "render")
        self.assertEqual(r["task"]["status"], "todo")
        res = dict(self.db.get("team_threads", "thread_id", "th-test")["result"])
        res["adopted"] = [0, 1]  # adopted before tasks existed
        self.db.update("team_threads", "thread_id", "th-test", {"result": res})
        self.assertEqual(team_tasks.ensure_for_adopted(self.db), 1)
        self.assertEqual(len(team_tasks.tasks(self.db, "th-test")), 2)

    def test_end_to_end_render_schedule_lock(self):
        self.adopt_all()
        t = self.advance()
        self.assertEqual(t[0]["status"], "in_progress")  # MediaConvert asked
        self.assertTrue(self.hooks.requested)
        self.assertEqual(self.hooks.rendered, [])
        self.assertEqual(t[1]["status"], "done")
        self.assertIn("Putt Tour", t[1]["progress"])
        self.assertEqual(t[3]["status"], "waiting")
        self.assertEqual(t[6]["status"], "needs_you")  # manual
        mediaconvert_finishes(self.db)
        t = self.advance()
        self.assertEqual(t[0]["status"], "done", t[0]["progress"])
        for x in t[0]["detail"]["targets"]:
            v = self.db.get("variants", "variant_id", x["variant_id"])
            self.assertEqual(v["edl"]["variant_label"], x["cut"])  # switched to the MediaConvert-cropped cut
        self.assertEqual(t[3]["status"], "needs_you", t[3]["progress"])
        pinned = [s for s in t[3]["detail"]["slots"] if s.get("variant_id")]
        self.assertEqual(len(pinned), 3)
        self.assertEqual(t[4]["status"], "needs_you")  # WPL covered by the schedule pin
        day = datetime.fromisoformat(pinned[0]["slot_at"]).astimezone(ZoneInfo(self.s.timezone)).date()
        sheet = runsheet.day_sheet(self.db, self.s, day, focus=False)
        rows = [r for r in sheet["rows"] if r.get("pinned")]
        self.assertEqual({r["variant_id"] for r in rows}, {s["variant_id"] for s in pinned})
        from fastapi.testclient import TestClient
        from pln_agent.web import app as web
        web._db = self.db
        page = TestClient(web.app).get(f"/runsheet?day={day.isoformat()}").text
        others = [r for r in sheet["rows"] if not r.get("pinned")]
        for r in rows:
            self.assertIn(r["variant_id"], page)
        for r in others:  # a challenge day shows only the challenge's posts
            self.assertNotIn(f'value="{r["variant_id"]}|', page)
        out = runsheet.lock(self.db, [(r["variant_id"], r["time"].isoformat()) for r in rows], "bill")
        self.assertEqual(len(out["scheduled"]) + len(out["needs_second_approver"]) + len(out["failed"]), 3)
        t = self.advance()
        if not out["needs_second_approver"] and not out["failed"]:
            self.assertEqual(t[3]["status"], "done")
            self.assertEqual(t[4]["status"], "done")
        self.assertEqual(t[5]["status"], "waiting")  # nothing published yet

    def test_old_drafts_without_a_cut_plan_are_regenerated(self):
        for v in self.db.all("variants", "asset_id=?", [CJ]):
            self.db.update("variants", "variant_id", v["variant_id"], {"treatment": {}})
        team.adopt(self.db, "th-test", 0, "bill")
        self.advance()
        self.assertIn(CJ, self.hooks.regenerated)

    def test_light_pass_leaves_renders_to_the_runner(self):
        self.adopt_all()
        team_tasks.advance(self.db, self.s, None, hooks=self.hooks,
                           kinds=tuple(k for k in team_tasks.ORDER if k not in team_tasks.COMPUTE_KINDS))
        t = {x["action_index"]: x for x in team_tasks.tasks(self.db, "th-test")}
        self.assertEqual(t[0]["status"], "todo")  # render untouched
        self.assertEqual(self.hooks.requested, [])
        self.assertEqual(t[1]["status"], "done")  # scout decision ran
        self.assertTrue(team_tasks.needs_compute(self.db))

    def test_mark_retry_and_pages(self):
        from fastapi.testclient import TestClient
        from pln_agent.web import app as web
        self.adopt_all()
        self.advance()
        tid = team_tasks.tasks(self.db, "th-test")[6]["task_id"]
        team_tasks.mark(self.db, tid, "done", "bill")
        self.assertEqual(self.db.get("team_tasks", "task_id", tid)["status"], "done")
        web._db = self.db
        web.BACKGROUND = False
        web.TEAM_TASK_HOOKS = self.hooks
        self.addCleanup(setattr, web, "TEAM_TASK_HOOKS", None)
        self.addCleanup(setattr, web, "BACKGROUND", True)
        c = TestClient(web.app)
        r = c.get("/team")
        self.assertIn("What the team is working on", r.text)
        r = c.get("/team/challenge/th-test")
        self.assertIn("Progress on the adopted actions", r.text)
        self.assertIn("MediaConvert", r.text)
        r = c.get("/team/agent/video")
        self.assertIn("Its tasks", r.text)
        r = c.post("/team/tasks/run", data={"back_to": "/team/challenge/th-test"}, follow_redirects=False)
        self.assertIn(r.status_code, (302, 303))


if __name__ == "__main__":
    unittest.main()
