# PLN Organic Social Growth Agent — Product and Editorial Strategy

## Strategic thesis
PLN's feed should feel like a network of exceptional sports/IP brands, not a warehouse of highlight reposts. Repurpose once at source, re-edit per destination, and learn from meaningful audience response. Separate B2B credibility on LinkedIn from fan community on Facebook, visual franchise identity on Instagram, and creator-style discovery on TikTok.

## Channel playbooks
| Platform | Role | Recurring series | Starting test cadence | North-star KPI |
|---|---|---|---|---|
| LinkedIn | Sports media executive credibility and deal origination | Why This Sport Works; Inside Production; Distribution Lab; Partner Spotlight | 3–4/week | Qualified inbound conversations, meeting requests, right-person engagement |
| Facebook | Existing fan retention + community | Full Finish Friday; Fan Debate; Match Recap; Next Event | 1/day | Returning viewers, meaningful comments, 1-minute views where available |
| Instagram | Show-level brand building | The Moment; Rules in 20 Seconds; Faces of PLN; BTS | 1–2/day, plus stories | Nonfollower reach-to-follow conversion, shares/saves |
| TikTok | Fast discovery + serial viewership | Wait For It; Can This Be Legal? (only if accurate); Rookie Reacts; Part 2 | 2/day test | Watch duration, completion, follows per 1,000 qualified views |

These posting rates are **hypotheses to test**, not platform-proven optimizations. Favor quality over quota.

## Franchise pillars
- CarJitsu: irresistible unexpected moments, personality, rules, stakes.
- Putt Tour: pressure, mic'd reactions, easy-to-understand shots and scores.
- SlapFIGHT: combat storytelling with platform-safety screening, no sensationalized injury bait.
- STR33T: athlete creators, team rivalries, local identity and collaborations.
- Carrom: explain the beautiful difficulty; accessible technical highlights.
- Ultimate Tire Wrestling / CartJitsu: absurdity balanced with sports legitimacy.

## Content operations
1. Ingest only authorized sources: asset library, rights metadata, show/event schedules, team notes, official metrics.
2. Generate transcript, scene markers, people and sponsor references. Human verifies identities and timestamps.
3. Pick hooks grounded in actual footage; score curiosity, stakes, clarity, replayability, freshness, brand fit, rights safety, and sponsor requirements.
4. Create native variations: LinkedIn business framing; Facebook fan conversation; Instagram 9:16 story/reel; TikTok first-frame hook and ongoing series.
5. Compliance/accuracy gate: approvals, disclosures, media rights, minors/safety, gambling laws, branded posts, AI labeling, platform terms.
6. Queue, review and publish via approved APIs/authorized scheduler (NO browser bot evasion).
7. Monitor publish status; ingest official organic analytics at 2h, 24h, 72h, 7d where available.
8. Allocate more effort to content families with durable growth (not raw view count alone); weekly explainable recommendations.

## Learning and experiments
- Start with 30-day measurement baseline (platform, show, format, post time, asset type, views, reach, watch duration, completion, shares, saves, comments, follows, attributed traffic, meetings).
- Label metrics by platform rather than comparing raw impressions across incompatible definitions.
- Run 2 variable experiments/week/platform max. E.g. hook styles, first shot, length, posting window, CTA; preserve a control.
- Use meaningful feedback; don't reward rage bait or engagement farming.
- Weekly human report: winners/losers with confounders, content supply, growth by IP, monetizable partner proof points.

## Architecture
- Python 3.12/FastAPI; Postgres for jobs/assets/analytics; Redis + durable worker; S3-compatible object storage for licensed media; FFmpeg for render; LLM abstraction for scripts; background scheduler.
- Adapters: LinkedIn Posts API; Meta Instagram Publishing and Facebook Pages; TikTok Content Posting API after approval/audit, or compliant scheduling partner.
- OAuth connection page and token vault, per-platform access controls, retry/rate limiting, idempotency keys, webhook/poll status, audit logs.
- Human review queue and two-person signoff for flagged sponsorship/sensitive posts; emergency kill switch; dry-run mode as default.
- Distinguish full auto for content research/draft/render/analytics from approval-gated publishing. Later allow preapproved evergreen content to publish within strict per-channel budget and kill switch.

## KPIs / success hypotheses
- 90-day objective: increase new qualified followers and repeat engaged viewers while maintaining partner-safe creative. Determine concrete numeric targets after pulling 30-day baseline data.
- LinkedIn: sales-qualified conversations, high-relevance engagements and referral activity; do not optimize merely impressions.
- Facebook: repeat viewing and quality comment threads per 1k reached.
- Instagram: nonfollower discovery, reel retention, follow conversion, shares/saves.
- TikTok: retention, completion, rewatch and follows per 1k viewers.
- Brand aggregate: share of content output by IP, cross-platform referrals, view-through to events, sponsor reporting.

## Permissions & publishing realities
- LinkedIn company posting requires approved scopes and page roles; use versioned Posts API.
- Meta Instagram publishing requires a Professional account and appropriate scopes; confirm current login model, publish limits, asset formats and media URL requirements. Facebook Pages require Page access and publishing permissions.
- TikTok Direct Post needs approved scopes and user consent; unaudited clients are private-only. Include TikTok creator info, privacy settings and AI-label handling. Avoid app-injected promotional watermarks prohibited by platform guidelines.
- No credentials are bundled here. Never hard-code secrets.

## Safety
No unauthorized copyrighted clips, fabricated sponsorships, undisclosed paid endorsements, fake engagement, bot comments, unauthorized music, age-inappropriate promotion or unreviewed wagering calls to action. Maintain platform-specific policies and review new versions quarterly.

## Official technical references
- https://learn.microsoft.com/en-us/linkedin/marketing/community-management/shares/posts-api
- https://www.postman.com/meta/instagram/documentation/6yqw8pt/instagram-api
- https://www.postman.com/meta/facebook/documentation/r56bjfd/facebook-api
- https://developers.tiktok.com/docs/en/content-posting-api-get-started
- https://developers.tiktok.com/docs/en/content-posting-api-reference-direct-post
- https://developers.tiktok.com/docs/en/content-sharing-guidelines
