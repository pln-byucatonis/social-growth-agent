"""Management's standing rights decisions (config/rights_policy.json)."""
import unittest
from unittest import mock

from pln_agent import rights
from pln_agent.rights import SENSITIVE, evaluate

BASE = {"rights_verified": True, "license": "PLN owned", "territory": ["WORLDWIDE"], "people_verified": True}


class RightsPolicyTests(unittest.TestCase):
    def test_combat_is_flagged_but_takes_one_approver(self):
        r = evaluate({**BASE, "flags": {"combat": True}})
        self.assertIn("combat", r.review_flags)
        self.assertFalse(r.needs_two_person)
        self.assertNotIn("combat", SENSITIVE)

    def test_likeness_releases_on_file_for_all_athletes(self):
        r = evaluate({**BASE, "people": [{"name": "x", "likeness_release": False}]})
        self.assertNotIn("likeness_unreleased", r.review_flags)
        self.assertFalse(r.needs_two_person)

    def test_one_approver_for_everything_flags_still_label(self):
        self.assertEqual(SENSITIVE, set())
        for f in ("injury", "minors", "gambling", "paid_partnership", "ai_generated_person"):
            r = evaluate({**BASE, "flags": {f: True}})
            self.assertIn(f, r.review_flags)
            self.assertFalse(r.needs_two_person, f)
        self.assertTrue(any("branded-content" in lbl for lbl in evaluate({**BASE, "flags": {"paid_partnership": True}}).labels))

    def test_a_listed_flag_needs_two(self):
        with mock.patch.object(rights, "SENSITIVE", {"gambling"}):
            self.assertTrue(evaluate({**BASE, "flags": {"gambling": True}}).needs_two_person)


if __name__ == "__main__":
    unittest.main()
