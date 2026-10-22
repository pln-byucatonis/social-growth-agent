"""Run sheet: tomorrow's proposed posts by learned time, lock in (approve + schedule), unschedule."""
import unittest
from datetime import date, timedelta

from pln_agent import runsheet

from tests.helpers import TODAY, make


class RunSheetTests(unittest.TestCase):
    def setUp(self):
        self.db, self.s = make(packs=True)
        self.day = TODAY + timedelta(days=1)

    def test_proposed_then_locked_then_unscheduled(self):
        sheet = runsheet.day_sheet(self.db, self.s, self.day, focus=False)
        props = [r for r in sheet["rows"] if r["kind"] == "proposed"]
        self.assertTrue(props)
        self.assertTrue(all(r["time"].date() == self.day for r in props))
        self.assertTrue(all(r["mode"] == "simulated" for r in props))  # dry-run
        one = next(r for r in props if not r["two_person"])
        res = runsheet.lock(self.db, [(one["variant_id"], one["time"].isoformat())], "bill")
        self.assertEqual(res["scheduled"], [one["variant_id"]], res)
        sheet = runsheet.day_sheet(self.db, self.s, self.day, focus=False)
        sch = [r for r in sheet["rows"] if r["kind"] == "scheduled"]
        self.assertEqual([r["variant_id"] for r in sch], [one["variant_id"]])
        self.assertEqual(sch[0]["time"], one["time"])
        runsheet.unschedule(self.db, one["variant_id"], "bill")
        self.assertEqual(self.db.get("variants", "variant_id", one["variant_id"])["state"], "approved")
        res = runsheet.lock(self.db, [(one["variant_id"], one["time"].isoformat())], "bill")  # can be rescheduled
        self.assertEqual(res["scheduled"], [one["variant_id"]], res)

    def test_combat_needs_second_approver(self):
        props = [r for r in runsheet.day_sheet(self.db, self.s, self.day, focus=False)["rows"] if r["kind"] == "proposed" and r["two_person"]]
        if not props:
            self.skipTest("fixture has no two-person content placed that day")
        r = runsheet.lock(self.db, [(props[0]["variant_id"], props[0]["time"].isoformat())], "bill")
        self.assertEqual(r["needs_second_approver"], [props[0]["variant_id"]])

    def test_page_renders_and_locks(self):
        from fastapi.testclient import TestClient
        from pln_agent.web import app as web
        web._db = self.db
        web.BACKGROUND = False
        self.addCleanup(setattr, web, "BACKGROUND", True)
        c = TestClient(web.app)
        r = c.get(f"/runsheet?day={self.day.isoformat()}&all=1")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Run sheet", r.text)
        props = [x for x in runsheet.day_sheet(self.db, self.s, self.day, focus=False)["rows"] if x["kind"] == "proposed" and not x["two_person"]]
        r = c.post("/runsheet/lock", data={"pick": [f"{props[0]['variant_id']}|{props[0]['time'].isoformat()}"], "day": self.day.isoformat(),
                                           "actor": "bill"}, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertEqual(self.db.get("variants", "variant_id", props[0]["variant_id"])["state"], "scheduled")


if __name__ == "__main__":
    unittest.main()
