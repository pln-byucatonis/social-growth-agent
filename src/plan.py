"""Offline-safe social editorial planning CLI. No API credentials, uploads or posting."""
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def build_plan(assets, strategy):
    plan = []
    for asset in assets:
        for platform, cfg in strategy['platforms'].items():
            if platform == 'linkedin':
                hook = f"What {asset['ip']} reveals about building sports audiences"
                caption = (f"{hook}: {asset['description']}\n\nThe commercial question: how do distinctive formats turn memorable moments into repeat viewing and sponsor value? What would you measure first?")
            elif platform == 'facebook':
                hook = f"Did you see this {asset['ip']} moment?"
                caption = f"{hook} {asset['description']} What would you have done in that spot?"
            elif platform == 'instagram':
                hook = f"You won't expect this {asset['ip']} turn"
                caption = f"{hook}. {asset['description']} Follow for more moments from {asset['ip']}."
            else:
                hook = f"Wait for the turning point in {asset['ip']}"
                caption = f"{hook}. {asset['description']} Which moment should we break down next?"
            needs_review = not (asset.get('rights_verified') and asset.get('people_verified'))
            plan.append({
                'asset_id': asset['id'], 'ip': asset['ip'], 'platform': platform,
                'objective': cfg['job'], 'hook': hook, 'caption_draft': caption,
                'creative_brief': 'Build a native edit; use licensed source, verified on-screen details, captions and a platform-specific first 2 seconds.',
                'media_path': asset['media_path'], 'status': 'needs_rights_and_identity_review' if needs_review else 'draft_needs_editorial_approval',
                'approved': False, 'published': False,
                'measurement': cfg['primary_kpi'],
                'notes': 'Illustrative draft only; not checked against a real video or real-time trends.'
            })
    return plan


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--assets', default=str(ROOT / 'examples/assets.json'))
    ap.add_argument('--out', default=str(ROOT / 'examples/planned_posts.json'))
    args = ap.parse_args()
    with open(ROOT/'config/strategy.json') as f: strategy = json.load(f)
    with open(args.assets) as f: assets = json.load(f)
    result = {'generated_at_utc': datetime.now(timezone.utc).isoformat(), 'dry_run': True, 'posts': build_plan(assets, strategy)}
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    with open(args.out,'w') as f: json.dump(result, f, indent=2)
    print(f"Wrote {len(result['posts'])} unpublished drafts to {args.out}")

if __name__ == '__main__': main()
