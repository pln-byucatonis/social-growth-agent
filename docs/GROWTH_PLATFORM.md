# PLN audience growth platform: what's built

One system, not separate apps. Every capability reads and writes the same database (accounts, assets,
variants, posts, metrics, knowledge) and goes through the same approval, rights and publishing gates.
See `docs/GAP_ANALYSIS.md` for the starting point.

```
Chief Growth Officer ── allocation.json, routing, recommendations          (partial; Phase 3–4)
Intelligence  ── Audience Intelligence (audience.py) · Knowledge Engine (knowledge.py)        Phase 1 ✔
                 Competitive Intelligence                                                     (not built)
Creative      ── Viral Content Scout (scout.py) · Creative Director (creative_director.py)     Phase 2 ✔
                 Video Production (media.py: FFmpeg, crop, captions, thumbnails, CDN)         core ✔
Distribution  ── Publishing Strategist (calendar_planner.py + cadence.py) · Migration (migration.py)
                 Community Manager                                                            (not built)
Optimization  ── Creative Intelligence DB (creative_db.py) · Experiments (experiments.py)     DB ✔, loop Phase 4
```

## Phase 1 — Audience Intelligence, Creative Intelligence DB, Knowledge Engine

**Data in.** `audience.import_ops_social` mirrors the ops portal's platform-API ingest
(`social_account_metric_daily`, `social_post`, `social_post_metric_daily`, read-only grant) into
`metrics` and `posts`. Idempotent via `metrics.metric_key`. A field that is 0 on every row for an account is
recorded as *not reported by the platform* and skipped — e.g. Instagram impressions.

**KPIs** (`account_kpis`): non-follower reach, follows per 1k views, average watch time, shares per 1k
views, returning viewers, profile visits and outbound clicks per 1k views (plus saves and engagements).
Each value has a `basis` — `measured`, `derived` (with the attribution assumption written out) or
`unavailable` (with what integration would expose it). Breakdowns by brand, platform, format and
caption-inferred content type. `MEASURABILITY` and `OVERLAP_QUESTIONS` state what the official APIs can and
can't tell us (cross-account audience overlap is not measurable; proxies are listed).

**Creative Intelligence DB** (`posts` + `metrics`): one record per real post. Agent-published posts record
the agent's creative decisions (hook style, opening beat, duration and cut, burned captions, route role,
series, source footage, timecodes, transcript excerpt, render, public URL); historical posts record only
what the caption/metadata show, with every other variable `unknown`. Provenance is stored per variable.
`what_works(brand, platform, outcome)` compares each variable value against the rest of the slice with a
stated rule: < 5 posts a side → insufficient; permutation test on the median difference, adjusted for the
number of comparisons in the slice (Benjamini–Hochberg); ≥ 8 a side and q < 0.05 → conclusive, q < 0.2 →
suggestive; yes/no variables tested once; the effect must point the same way in the earlier and later
half of the posts or it is "unstable". Only per-1k-view rates become PLN evidence (raw views mostly
reflect a post's age and distribution).

**Knowledge Engine** (`knowledge`, `knowledge_sources`): seeded from `config/knowledge_seed.json` — every
platform entry has its URL, publisher, publication date (or "undated" + what the page shows), retrieval
date and a verbatim quote. Levels and precedence: platform rule → PLN evidence → platform guidance →
external research → hypothesis. The worker re-fetches each source weekly; a missing quote or unreachable
page moves the entry to *needs review* for a person (the engine never rewrites guidance). PLN evidence is
written from conclusive/suggestive `what_works` findings and superseded when it stops holding. PLN
evidence can't be typed in by hand.

**Adaptive cadence** (`cadence`): starting ranges per brand/platform in `config/cadence.json`
(from the Growth Engine 2.0 brief, labelled hypotheses). With ≥ 4 measured weeks the recommendation
follows the slope of weekly net follower change on weekly posts, checks diminishing returns in busier
weeks and engagement dilution, moves at most 2 posts/week at a time, and is capped by drafts on hand. The
calendar planner caps each account at its recommended weekly number.

## Phase 2 — Viral Content Scout and Creative Director

**Scout** (`scout_moments`): scores every clip in the ops library (incrementally per model version) for its
own property and for PLN crossover: taxonomy match (`config/scout_taxonomy.json` — CarJitsu reversals,
traps, submissions…; Putt Tour impossible putts, pressure, missed short putts…; PLN unfamiliar sports,
athletic feats, absurd competitions…), ops highlight and energy, personality, duration fit, novelty, and a
learned lift that comes only from current PLN evidence for that content type. `top_moments(event)` returns
the best 20 non-overlapping, category-diverse moments; *Make drafts* promotes them into assets, drafts and a
creative package. Dismissals survive re-scans.

**Creative Director** (`variants.treatment`, `creative_packages`): each destination gets a hook / edit /
context / payoff / conversion treatment from its platform recipe (TikTok opens on the payoff; Instagram
builds tension; Facebook adds context and invites discussion; YouTube sets up the challenge; LinkedIn is a
business insight, not the highlight re-posted), plus A ≈12s / B ≈25s / C ≈40s cuts around the payoff with
honest availability (a short vertical rendition can't make a 40s cut). The recommended cut comes from PLN
duration evidence when it exists, otherwise the platform's starting recipe, labelled a hypothesis; each
treatment lists the knowledge entries it used. Switching cuts revokes approval and clears the render.

## Dashboard

Growth agent in ops: Approval queue, **Scout**, Assets & rights, Calendar, Accounts, WPL → Putt Tour,
Overview, Audit log; Intelligence: **Audience**, **Creative DB**, **Knowledge Engine**, **Cadence**.

## Running it

The six-hourly ops refresh (EventBridge → `/internal/tick?ops=1`) imports accounts and clips, then starts
the intelligence refresh in a background thread: knowledge seed → social import → PLN evidence → scout scan
(1,500 clips per pass) → cadence → weekly source review. Each step is isolated and logged.

## Next (Phases 3–4 and supporting capabilities)

- Platform insights collectors for OAuth-connected accounts (Instagram/Facebook/YouTube/TikTok official
  APIs) writing the same `metrics` rows — fills non-follower reach, watch time, follows per post.
- Publishing Strategist: per-slot learning; frequency experiments designed from the cadence engine.
- Experiment evaluation with the Creative DB rule and write-back to PLN evidence (hook style, duration A/B/C,
  commentary, branding in the first two seconds).
- Chief Growth Officer weekly brief: allocation across brands from measured follows per 1k views and
  supply, experiment budget, migration status.
- Community Manager (comment ingestion, classification, drafted replies, human escalation) and Competitive
  Intelligence (benchmark set, technique notes as external research entries).
