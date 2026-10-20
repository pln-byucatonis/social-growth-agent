# Gap analysis: Social Growth Agent → audience growth intelligence platform

Date: 2026-10-20. Baseline: `main` @ 846c751 (85 tests passing on SQLite and Postgres; deployed on App Runner
behind ops.proleaguenetwork.com). Target: the ten coordinated capabilities in the Growth Engine 2.0 brief
plus a Knowledge Engine and a Creative Intelligence Database.

Legend: **Have** = working and tested · **Partial** = exists but doesn't meet the brief · **Missing**.

| # | Capability | Today | Gap |
|---|---|---|---|
| 1 | Chief Growth Officer | **Partial.** `config/allocation.json` (static effort shares), `routing.py` (canonical-first + PLN amplification), `experiments.recommendations()` (rules on supply/rights/fatigue). | No goals, no priority tiers in decisions, no measured brand-efficiency reallocation, no single weekly brief across agents. |
| 2 | Audience Intelligence | **Partial.** `metrics.py`: native metric names kept, metric families, baselines, per-post checkpoints. Data only arrives by manual CSV. | No ingestion of the real data ops already collects (`social_account_metric_daily`, `social_post`, `social_post_metric_daily`: 6 Instagram accounts, 234 posts, Jun 30–Aug 1 2026). No six-category KPI model, no breakdown by brand/format/content type, no measured-vs-derived-vs-unavailable labelling, no overlap/measurability statement. No platform insights collectors for connected accounts. |
| 3 | Viral Content Scout | **Partial.** `ops_sources.import_ops_clips` ranks ops clips by `highlight_score` with brand fair share; `scoring._source` uses moment intensity + ops energy. | No per-brand moment taxonomy (reversal, pressure putt, unfamiliar sport…), no explainable creative-potential score, no "top 20 moments of this event" per source master, no learning from results. |
| 4 | Creative Director | **Partial.** `strategists/platforms.py` writes per-platform hooks, captions, on-screen text and an EDL; validation blocks invented facts/clickbait. | Creative is mostly copy. No explicit Hook / Edit / Context / Payoff / Conversion treatment, no duration variants (12s / 25s / 40s), no LinkedIn business treatment of the same moment beyond copy, no links to the evidence behind a choice. |
| 5 | Video Production | **Have (core).** FFmpeg EDL render, 9:16 reframe with motion-centred crop, burned text, captions track, thumbnail, S3 + public CDN link, background render. | Multi-variant renders per package, QC checks (black frames, loudness, safe zones). Phase 3. |
| 6 | Publishing Strategist | **Partial.** `calendar_planner.py`: slot hypotheses, spacing, daily caps, canonical-before-amplification. | Caps are fixed per platform (`max_posts_per_account_per_day`). No per-account starting cadence, no measured incremental-follower-per-post recommendation, no production-capacity check. |
| 7 | Community Manager | **Missing.** | Comment ingestion, classification, triage, drafted replies, human escalation. Supporting capability. |
| 8 | Competitive Intelligence | **Missing.** | Benchmark set, technique extraction, recommendations. Supporting capability. |
| 9 | Growth Experimentation | **Partial.** `experiments.plan_experiments`: ≤2/platform/week, hook-style and slot tests, min-sample note. | No evaluation: no decision rule (conclusive / suggestive / inconclusive), no results written back, no duration/commentary/branding tests. Phase 4. |
| 10 | Audience Migration Specialist | **Have (core).** `migration.py`: WPL decision sheets, signoffs, bridge posts, no profile edits without approval. | Uses no real numbers yet: ops has WPL Instagram at 1,348 followers vs Putt Tour 62,101; transition tracking and decline alerts need the Audience Intelligence feed. |
| KE | Knowledge Engine | **Missing.** Guidance is scattered in docstrings, adapter `requirements` and README. | Source-attributed, dated entries separated into platform rules, official guidance, external research, hypotheses and PLN evidence; scheduled source review; PLN evidence taking precedence over recommendations. |
| CIDB | Creative Intelligence Database | **Missing.** Variants hold creative decisions but only pre-publish; nothing for historical posts. | One record per post (agent-published or historical) with creative variables + provenance, source footage, publishing details and real metrics; "what works" queries with a stated evidence rule. |

## Constraints carried forward

- Official APIs only. No scraping behind login, no engagement inflation, no unapproved publishing.
- Missing metrics stay unknown — never zero-filled or estimated.
- Starting cadences are configurable hypotheses (`config/cadence.json`), never permanent rules.
- Irreversible account actions stay behind explicit human authorization.

## Build order

1. **Phase 1** — Audience Intelligence (ops social ingest + KPI model + measurability matrix), Creative
   Intelligence Database, Knowledge Engine (seeded with verified sources, weekly source review, PLN
   evidence findings), adaptive cadence recommendations. Dashboard pages for each.
2. **Phase 2** — Viral Content Scout (taxonomy + explainable potential score + per-event top 20, learned
   lift from the creative DB) and Creative Director (five-part treatments, duration variants, evidence refs).
3. Phases 3–4 and the supporting capabilities (Community, Competitive) follow.
