"""Management's standing rights decisions (config/rights_policy.json)."""
import unittest

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

    def test_other_sensitive_flags_still_need_two(self):
        for f in ("injury", "minors", "gambling", "paid_partnership", "ai_generated_person"):
            self.assertTrue(evaluate({**BASE, "flags": {f: True}}).needs_two_person, f)


if __name__ == "__main__":
    unittest.main()
