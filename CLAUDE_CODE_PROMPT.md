# CLAUDE CODE — BUILD PLN SOCIAL GROWTH AGENT V0.4 (PORTFOLIO HANDOFF)

You are lead engineer, Head of Social Strategy, and production architect for Pro League Network. Your job is to turn this existing offline planner into an autonomous, measurable, rights-safe organic social growth operation. Build running software and tests, not just a proposal.

Start by reading README.md, docs/STRATEGY_AND_ARCHITECTURE.md, config/strategy.json, src/plan.py, tests/test_plan.py, and examples/assets.json. Run tests and sample planner. Preserve working functionality.

## Non-negotiable mission
Grow PLN's audience and engagement across LinkedIn, Facebook, Instagram, and TikTok through **differentiated native creative**, not duplicate cross-posts. Optimize each for different audiences and business outcomes. Maintain PLN's irreverent, high-energy sports entertainment identity without inventing facts or sacrificing sponsor suitability. Consider CarJitsu, SlapFIGHT, Putt Tour, STR33T, Pro Carrom Tour, Ultimate Tire Wrestling and CartJitsu.

## Implement in phases; proceed without needless clarification
1. Durable schemas for asset inventory, editorial packs, platform variants, schedules, approvals, publishing attempts, feedback and metrics. SQLite fallback for local dev and migration-ready Postgres models. Include asset ownership, territory, rights expiration, license, people/sponsor verification, content ID and provenance.
2. Ingestion: watch licensed local asset folder, metadata sidecar JSON, official content calendar CSV. Add FFmpeg/ffprobe metadata and optional video scene/transcript hooks without requiring paid model credits to boot.
3. Platform strategists: independent prompts and validation schemas for each platform. Create candidate hooks, shot selection edit decision lists, voiceover, captions, thumbnail/title, hashtags with reasoned sparing use, business/community angle, CTA, and test hypothesis. No manufactured controversies or inaccurate clickbait.
4. Score/rank based on actual source clip, platform fit, timeliness and empirical results. Do not rely on guessed footage. Store supporting source timecodes.
5. Native vertical media packaging via FFmpeg: 9:16 reframing, burn captions, simple templates, thumbnail. If no files present, produce EDL/preview placeholders instead of pretending to render video. Do not add TikTok-unapproved watermarks or noncompliant branding.
6. Human approval dashboard (FastAPI + simple HTML/HTMX or React if justified). Calendar, asset previews, edit/post text, risk badges, permission state, per-platform queue, approval/reject with audit trail, rate caps, emergency stop.
7. Provider adapters with typed interfaces for LinkedIn company page, Facebook Page, Instagram Professional and TikTok. Begin in dry-run. Real OAuth/token handling and current permissions; no hidden browser automation or brittle scraping. Surface exact app-review blockers honestly. Support authorized scheduling-provider integration as fallback. TikTok never assume public direct publishing until client audited and authorized.
8. Scheduler/publisher with idempotency, exponential backoff, recoverable job state, publication receipts/permalinks, eventual status poll, duplicate prevention and manual rollback guidance. Nothing publishes until user explicitly configures accounts and turns on publishing after testing. Require approval by default. Allow future preapproved evergreen auto-post policy as an opt-in only.
9. Collect platform-native performance where permissions permit, normalize without erasing platform definitions, capture 2h/24h/72h/7d points, avoid comparing raw view counts naively. Track platform-specific north-star KPIs, reporting and test attribution.
10. Weekly experiment planner: select promising series, hook tests, creative beats, time slot tests, plus explainable strategy recommendations. Detect fatigue, diminishing returns, missed posts and partnership approvals.
11. Build a command-line simulator with sample data and realistic output: seven-day content calendar, one asset => four genuinely distinct scripts, approval queue, platform-specific report. Add tests for each approval, media rights, duplicate prevention, safe publish-gate, API failure cases. Include .env.example, Docker Compose if appropriate, setup and security README.

## Scope boundaries
- Do NOT claim actual social API connectivity without verified OAuth credentials and API calls.
- Do NOT fabricate historical analytics, account audience sizes, trends or partner agreements.
- No bots for likes/comments/follows, scraping behind authentication, credential sharing, evasion of API audits or manipulative fake engagement.
- No unlicensed clips or copyrighted music.
- Flag gambling content, paid partnerships, combat/injury footage, likenesses and AI-generated realistic persons for manual review and correct labeling.
- Create clear separation between draft, approved, scheduled, published and failed states.
- Treat user account data and OAuth tokens as secrets.

## Delivery expectations
Finish working code incrementally; run tests and smoke tests. At completion provide: architecture summary; commands to run locally; sample calendar and creative package; clear distinction between functional, mocked and blocked integrations; permissions/checklist to connect real PLN accounts; 30/60/90-day strategic plan; prioritized backlog. Do not stop at a plan.

## IMPORTANT: REAL ACCOUNT PORTFOLIO AND WPL MIGRATION (added October 20, 2026)
Read `config/accounts.csv`, `config/accounts.json`, and `docs/ACCOUNT_PORTFOLIO_AND_MIGRATION.md` **first**. These list user-supplied accounts across PLN, CarJitsu, Putt Tour, legacy World Putting League, SlapFIGHT, STR33T, Pro Carrom Tour and World FootGolf Tour. Treat handles as *unverified inputs* until authenticated. The three growth priorities are (1) PLN corporate network, (2) CarJitsu, (3) Putt Tour. PLN LinkedIn must be implemented conceptually even though no Page ID was provided. Add YouTube, X and Twitch strategy/adapters after the first four-platform workflow; use dry-run if integrations unavailable.

Implement a **legacy WPL -> Putt Tour rebrand/migration manager** with editable canonical-account selection by platform, legacy-to-canonical routing, pinned announcement templates, migration calendar, UTM campaigns, baseline vs migration metrics, approval checkpoints, and explicit no-auto-handle-change/no-delete safety. Social accounts usually cannot simply be merged; support either preserving WPL's stronger existing audience through in-place rebrand if possible, or a bridge from WPL to `@putttour` on Instagram. Never assume follower transfer.

Build a multi-tenant account routing data model (brand, network, username, stable ID when available, canonical status, account ownership, permission, posting policy, campaign, content/clip ID, rights flags). A media asset should be repurposed with DIFFERENT editing/storytelling for each eligible outlet; canonical brand first, then selective PLN amplification. Avoid duplicate content spam. Test routing for CarJitsu, PLN secondary TikTok ambiguity, Putt Tour vs WPL Instagram, WPL-only TikTok/YT/X, and unverified World FootGolf creator account.


## Implementation order and definition of done (v0.4 handoff)
Work from the repository root. First run `python3 -m unittest discover -s tests -v` and `python3 src/plan.py`. Do not claim the provided prototype connects to accounts: it currently produces offline draft captions from sample metadata. Do not assume that listing 28 accounts means 28 authorized integrations.

**Milestone A — Trusted inventory & baselines**
- Load all 28 user-provided entries from `config/accounts.csv`, keep original handle spelling unchanged, and define canonical brand IDs and account IDs separately. Add optional LinkedIn PLN company page as a pending, not verified, account; do not fabricate a handle.
- Build account credential/ownership verification status, and support multiple accounts per platform per brand.
- Accept manual CSV analytics imports first; optional official provider connectors second. Compute prelaunch baselines per account: follower count, frequency, median watch time/retention when available, reach, shares, saves, and profile-to-follow conversion. Unknown metrics must display as unknown.
- P0 resource allocation should focus PLN, CarJitsu, Putt Tour. Propose allocation as an editable config, not a hard-coded fact about performance.

**Milestone B — Editorial production and orchestration**
- Asset library with rights, source timestamps and content lineage; brand fit; per-platform distinctly tailored copy and edit decision lists.
- Prioritize short-video native strategy for CarJitsu and Putt Tour; PLN uses discovery/portfolio amplification and B2B LinkedIn strategy. Calendar and queue must coordinate across accounts to avoid competing crossposts and content fatigue.
- Support manual footage imports, a seven-day real-asset-aware calendar, and approved creative variants. Every pending publication has an explicit destination brand/account, exact caption, asset, and user-approved status.

**Milestone C — Migration workstream: World Putting League → Putt Tour**
- Inventory original WPL Instagram, TikTok, YouTube and X accounts plus Putt Tour Instagram. Do not presume Putt Tour has other platform accounts.
- For each platform build a decision sheet: keep existing WPL identity temporarily, in-place rebrand (if permitted and approved), or redirect to a different Putt Tour account. Compare verified audience size, engagement quality, content history, discoverability and handle availability before choosing. No platform follower merge assumed.
- Simulate a 90-day transition with announcement variants, pinned content, bio updates proposed for approval, historic-content reuse, UTM tags, account-overlap estimates only when supportable, and weekly migration reporting. Never automatically rename, delete, or modify profiles.
- KPI examples: retained combined reach/engagement, percent of new Putt Tour viewers coming from WPL links, WPL audience response, follower and subscriber continuity. Report *migration proxies* when individual follower movement cannot be measured.

**Milestone D — Publishing connectors and feedback**
- Use public supported APIs and platform permissions, or a sanctioned scheduling provider; auth and app review differ by platform and should be verified against current docs during implementation.
- Never publish from the unverified second PLN TikTok handle or creator-associated FootGolf account without ownership/authorization. Never send drafts publicly while in dry-run.
- Add approval audit log, kill switch, publish retry and idempotency, error transparency, reporting by brand/platform and weekly learning recommendations.

**Acceptance tests**
- 28 account inventory entries load without dropped or silently deduplicated handles.
- Distinct P0 routes for PLN, CarJitsu and Putt Tour; unverified accounts cannot publish.
- WPL assets/accounts mapped to Putt Tour canonical brand while preserving legacy source identity; no accidental account merge or automatic rename.
- LinkedIn PLN pending account can be planned but not posted to until authorized.
- Demo from one source clip produces different creative for each eligible platform; licensed-rights gate, approval gate, no-duplicate scheduling and emergency stop are demonstrated.
- System can run end-to-end in local dry-run with repeatable tests and without tokens or paid model providers; actual posting requires explicit authorization.

**When finished**, provide a concise working-vs-stubbed feature table, screenshots or console evidence for the local demonstration, remaining permissions, and explicit launch-readiness checklist. Do not imply users supplied account passwords or analytics; they supplied handles only.
