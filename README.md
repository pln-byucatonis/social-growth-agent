# PLN Social Growth Agent — v0.4

A local engine for organic social growth across Pro League Network's portfolio. It works in **dry-run mode first** and **requires approval before anything publishes**.
The agent turns licensed footage into a different native edit and copy for each destination account:
LinkedIn, Facebook, Instagram and TikTok first, then YouTube, X and Twitch. It routes each asset to the canonical brand account first, then to selective PLN amplification. It runs the World Putting League → Putt Tour migration as proposals with approval checkpoints. Every publish passes rights, approval, account-authorization, duplicate, rate-cap and emergency-stop gates.

> **Status:** No social account is connected. The supplied handles are **unverified**: these are 28 user-supplied handles, not 28 integrations. All publishing in this repo is **simulated** (`dryrun://` receipts). Live adapter code exists for the official APIs, but nobody has run it against a real platform. It needs OAuth credentials, platform app review, and admin roles on each account. See [docs/LAUNCH_READINESS.md](docs/LAUNCH_READINESS.md).

## Quick start

```bash
python3 -m unittest discover -s tests -v        # 52 tests, stdlib only (dashboard tests skip without FastAPI)
python3 -m pln_agent demo                        # full end-to-end simulation → data/demo_output/
python3 src/plan.py                              # original v0.3 planner (preserved)

# dashboard (optional deps)
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
PLN_DATABASE_URL=sqlite:///data/demo_output/demo.db PLN_RENDER_DIR=$PWD/data/demo_output/renders \
  .venv/bin/python -m pln_agent serve            # http://127.0.0.1:8000
```

Requirements: Python 3.11+ and FFmpeg/ffprobe on PATH (for metadata and rendering). The engine needs no API keys and no paid models.

## Day-to-day workflow (CLI: `python -m pln_agent …`)

| Step | Command |
|---|---|
| Load the 28-account inventory and migration config | `init` |
| Ingest licensed footage plus sidecar JSON (`--watch` keeps polling) | `ingest [--scenes] [--watch]` |
| Build editorial packs (routes → strategists → validation → scoring) | `plan --all` |
| Render native media from the edit decision lists (EDLs) with FFmpeg | `render --all` |
| Plan the 7-day coordinated calendar | `calendar --start 2026-10-21` |
| Review the queue, edit, approve or reject | `queue`, `show <id>`, `edit <id> --caption …`, `approve <id> --reviewer you`, `reject …` |
| Schedule an approved post (with duplicate prevention) | `schedule <id> [--at 2026-10-21T18:30]` |
| Run the publisher (simulates unless every live gate passes) | `publish` |
| Emergency stop | `kill-switch on --note "reason"` / `kill-switch off` |
| Import analytics and view baselines | `metrics import export.csv`, `metrics baselines` |
| WPL → Putt Tour migration | `migration sheet / plan / propose / proposals / approve <platform> / review --proposal-id N --decision … / announcements / report` |
| Experiments and recommendations | `experiments`, `recommend` |
| Report and API requirements | `report`, `capabilities` |
| Account lifecycle (audited; human-initiated only) | `accounts list / verify <id> --owner … / connect <id> --credential-ref env:NAME --platform-id … / authorize <id>` |
| Postgres DDL | `schema --dialect postgres` (committed at `migrations/postgres/001_init.sql`) |

### Asset sidecar (one JSON per clip in the inbox)
Required fields are `asset_id`, `brand` and `title`. The rights fields are `license`, `owner`, `territory`, `rights_expires_at`, `rights_verified`, `people_verified`, `music_cleared`, `sponsors[]` and `sponsor_approved`. Grounding comes from `facts[]` (only `verified:true` facts are used in copy), `people[]` (names appear only if verified) and `moments[]` (`start`, `end`, `label`, `kind`, `intensity`, `description`, `stakes`). These timecoded moments are what every edit decision list is built from. The sensitivity flags are `gambling`, `paid_partnership`, `combat`, `injury`, `minors` and `ai_generated_person`. See `sample_media/inbox/*.json`. **All sample clips are synthetic FFmpeg test patterns labeled "SAMPLE – NOT REAL".**

### Analytics CSV (manual import first)
The CSV is long-format: `account_id` (or `platform` + `username`), `metric`, `value`, then optionally `platform_post_id`, `variant_id`, `checkpoint` (2h/24h/72h/7d/snapshot), `period_start` and `period_end`. Metric names stay platform-native, for example `followers_count`, `video_views` and `ig_reels_avg_watch_time`. Blank values stay **unknown** and are never imported as 0.

## Safety model (enforced in code, covered by tests)
- **Three independent gates must all be open before a live call**:
  - `PLN_DRY_RUN=false` and `PLN_PUBLISHING_ENABLED=true`
  - the account is `oauth_connected` with a credential *reference*, and a human has authorized it
  - the emergency stop is off

  Otherwise the publisher **simulates**, records the exact token-redacted requests it would have sent, and marks the post `simulated`, never `published`.
- **Approvals bind to the exact caption hash.** Any edit drops the post back to review. Paid partnerships, combat, gambling, minors, unreleased likenesses and AI-generated people need **two different reviewers** and the correct platform disclosure label.
- **Rights are checked at approval and again at publish time**: license, territory, expiry, music, people and sponsor approval.
- **Hard blocks:** the PLN LinkedIn placeholder (no Page ID was supplied), the ambiguous `@pro.league.networ` TikTok, and the creator-associated `@ezz.abde` FootGolf account. These cannot be authorized until ownership is confirmed and a human clears the block with a written justification.
- **No profile edits, renames, deletes or merges.** No adapter has such a method, and tests assert this. Migration changes are proposals that an admin applies manually.
- **No engagement automation:** no likes, follows, comments, scraping, browser automation or trend guessing. Validation rejects clickbait phrases, numbers not found in verified source material, unverified names, unresolved placeholders and near-duplicate copy across destinations.
- **Secrets:** the database stores `credential_ref` (`env:NAME` / `vault:path`) only, and refuses anything that looks like a raw token. Logs and receipts redact `Authorization` headers and `access_token` fields. The dashboard binds to 127.0.0.1; set `PLN_DASHBOARD_PASSWORD` for Basic auth, and put it behind SSO before any shared deployment.

## Layout
```
pln_agent/   config, db (SQLite + Postgres DDL), accounts, ingest, rights, strategists/, llm (optional), scoring,
             routing, editorial, media (FFmpeg), calendar_planner, workflow (approvals + publisher), adapters/,
             metrics, migration, experiments, reports, demo, cli, web/ (FastAPI dashboard)
config/      accounts.csv (28 user rows), brands, allocation (planning assumption), slots (hypotheses),
             migration (editable canonical selection), account_state (verification overrides), auto_publish_policy (off)
docs/        STRATEGY_AND_ARCHITECTURE, ACCOUNT_PORTFOLIO_AND_MIGRATION, IMPLEMENTATION, LAUNCH_READINESS, screenshots/
tests/       acceptance + component tests; fixtures/ are SYNTHETIC test data only
```

Start with [docs/IMPLEMENTATION.md](docs/IMPLEMENTATION.md) for the architecture and the working vs. stubbed feature table. Then read [docs/LAUNCH_READINESS.md](docs/LAUNCH_READINESS.md) for permissions, the launch checklist, the 30/60/90-day plan and the backlog.
