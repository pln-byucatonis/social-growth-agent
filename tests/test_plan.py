import json
import unittest
from pathlib import Path
from src.plan import build_plan
ROOT=Path(__file__).resolve().parents[1]
class PlannerTests(unittest.TestCase):
    def test_platform_variants_and_approval_gate(self):
        assets=json.loads((ROOT/'examples/assets.json').read_text())
        strategy=json.loads((ROOT/'config/strategy.json').read_text())
        posts=build_plan(assets,strategy)
        self.assertEqual(len(posts),len(assets)*4)
        self.assertEqual(len(set(p['platform'] for p in posts)),4)
        self.assertTrue(all(not p['approved'] and not p['published'] for p in posts))
        self.assertTrue(all('review' in p['status'] for p in posts))
if __name__=='__main__': unittest.main()
