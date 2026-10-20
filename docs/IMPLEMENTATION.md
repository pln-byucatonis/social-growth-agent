# Implementation — v0.4

## Architecture

```
 licensed footage + sidecar JSON ─┐        official calendar CSV ─┐     manual analytics CSV ─┐
                                  ▼                               ▼                           ▼
                           ingest (ffprobe, sha256,         calendar loader               metrics (native names,
                           caption-file transcript,                                        2h/24h/72h/7d, baselines)
                           optional scene detect / ASR)                                            │
                                  │                                                                │
                                  ▼                                                                │
   accounts (28 rows + LinkedIn placeholder) ──► routing: canonical brand → PLN amplification/B2B → WPL bridge/in-place
                                  │                     (migration decisions decide WPL roles)
                                  ▼
                  platform strategists (LinkedIn, Facebook, Instagram, TikTok, YouTube, X, Twitch)
                  hooks w/ source timecodes · EDL · captions · VO · thumbnail · hashtags+rationale · CTA · test
                                  │  optional LLM refine (re-validated, discarded if it fails)
                                  ▼
                  validation (grounding, banned phrases, limits, distinctness) + rights gate + scoring
                                  │
                                  ▼
                  needs_fix / in_review ──► approve (exact text; 2 people if sensitive) ──► schedule (idempotency key,
                                                                                            duplicate checks)
                                  │                                                              │
                     FFmpeg render (9:16/4:5/1:1, burned text, thumbnail)                        ▼
                                                                  publisher: kill switch → rights re-check → rate cap →
                                                                  live eligibility → adapter.simulate | adapter.publish
                                                                  (backoff, crash recovery, receipts, status poll)
                                  ▼
                  reports · experiments (≤2/platform/week, control) · recommendations (evidence-cited) · audit log
```

* **Storage:** one table definition (`pln_agent/db.py`) renders to SQLite for runtime and tests, and to Postgres DDL (`migrations/postgres/001_init.sql`). There are 16 tables, covering accounts, assets, packs, variants, approvals, schedules, publish jobs and attempts, metrics, feedback, the audit log, settings, migration decisions, profile-change proposals and experiments. Assets carry owner, license, territory, expiry, people/sponsor verification, content ID, provenance, sha256 and lineage (`parent_asset_id`).
* **Multi-tenant routing model:** each account has brand, canonical brand, platform, the original handle (kept verbatim), a normalized handle, a stable platform ID (filled after OAuth), role, verification, credential reference, publishing policy and authorization, a hard-block reason, a redirect, and a campaign. Multiple accounts per platform per brand are supported; PLN has two TikToks.
* **Dashboard:** FastAPI with server-rendered Jinja and no JS build. Pages are Overview (pipeline, emergency stop, recommendations, jobs), Approval queue (risk badges, permission state, per-platform filter), Variant (edit exact text, approve/reject, schedule, render preview, EDL, score explanation, exact API requests that would be sent, approval and audit trail), Calendar, Accounts (eligibility and API requirements), WPL → Putt Tour (decision sheets, sign-off, proposals, 90-day plan, announcements, report) and Audit. Screenshots are in `docs/screenshots/`.

## Working vs. simulated vs. blocked

| Capability | Status | Notes |
|---|---|---|
| 28-account inventory, IDs, roles, hard blocks, LinkedIn placeholder | ✅ Working | Loads verbatim; reload is idempotent and preserves human verification |
| Verification lifecycle (ownership → OAuth reference → authorization) | ✅ Working | Audited CLI; raw tokens rejected |
| Asset ingestion (sidecar, ffprobe, sha256, .srt/.vtt transcript, scene detect, watch folder, calendar CSV) | ✅ Working | ASR only via the optional `PLN_TRANSCRIBE_CMD` hook (none bundled) |
| Rights/identity/sensitivity gate | ✅ Working | Runs at approval and at publish time |
| Per-platform strategists (7) with validation schemas | ✅ Working | Deterministic templates grounded in sidecar data; copy quality depends on how well clips are annotated |
| Optional LLM refinement (Claude) | ⚠️ Implemented, untested live | Off by default; needs `anthropic` SDK + key; output is re-validated |
| Scoring/ranking (source, fit, timeliness, empirical, rights) | ✅ Working | Empirical part is a neutral prior until real metrics exist |
| FFmpeg packaging (trim, reframe, burned text/captions, thumbnail) | ✅ Working | Center-crop reframe; subject tracking is a hook, not implemented |
| EDL-only placeholder when media is missing | ✅ Working | Never fakes a render |
| 7-day coordinated, supply-aware calendar | ✅ Working | Posting times are hypotheses |
| Approval workflow, two-person signoff, edit invalidation, audit | ✅ Working | |
| Scheduler/publisher: idempotency, duplicate prevention, backoff, rate caps, crash recovery, status poll, kill switch | ✅ Working | Live path verified against a **mock** transport only |
| Dry-run simulation with exact request previews | ✅ Working | `simulated_receipts.json` |
| LinkedIn / Facebook Page / Instagram / TikTok / X adapters | 🟡 Code ready, **not connected** | Request shapes follow current public docs; unverified against live APIs. LinkedIn needs a pre-uploaded video URN (binary upload step not wired) |
| YouTube / Twitch adapters | 🟡 Preview only | YouTube byte upload is disabled until the OAuth project is verified; Twitch has schedule segments only (no clip upload API) |
| Authorized scheduling-provider fallback | 🟡 Stub | Interface only; no provider chosen |
| Hosting renders at a public HTTPS URL (needed by Instagram/Facebook/TikTok pull) | ⛔ Blocked | Needs S3/CDN + `PLN_MEDIA_PUBLIC_BASE_URL` |
| Analytics: manual CSV import, baselines, checkpoints, rates per 1k | ✅ Working | No real data imported; everything shows "unknown" |
| Analytics API connectors | ⛔ Not built | Needs read scopes per platform (see LAUNCH_READINESS) |
| WPL → Putt Tour manager: decision sheets, provisional vs evidence-based recommendations, proposals, sign-off, 90-day plan, announcements, UTM, weekly proxy report | ✅ Working | No profile is ever changed by the system |
| Weekly experiments, fatigue/scale detection, missed posts, partnership/rights/supply alerts | ✅ Working | Fatigue needs ≥4 measured posts per series |
| Dashboard | ✅ Working | Local, no SSO |
| Postgres runtime | ⚠️ DDL generated and committed; runtime path untested | Two queries were made portable; run the test suite against Postgres before relying on it |
| Docker Compose | ⚠️ Provided, not run here | No Docker daemon in the build environment |

## Console evidence
`python -m pln_agent demo` writes:
- `data/demo_output/demo_console.txt`: the full narrated run (inventory, routing, the 4 distinct core scripts with similarity matrix, renders, calendar, gates, mock live rehearsal, migration, experiments, report)
- `creative_package.json`, `calendar.json`, `simulated_receipts.json`, `migration_decision_sheet.json`
- `renders/<variant>/video.mp4 | thumbnail.jpg | edl.json` (synthetic sample footage)

The sample creative uses fictional SAMPLE metadata. Replace it with real, verified sidecars before judging copy.
