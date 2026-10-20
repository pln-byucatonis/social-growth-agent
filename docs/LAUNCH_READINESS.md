# Launch readiness: permissions, checklist, 30/60/90 plan, backlog

PLN supplied **handles only**. There are no passwords, tokens, Page IDs or analytics. Nothing below is done yet.

## 1. Permissions needed to connect real PLN accounts

Re-verify every item against the platform's current docs when you connect. Policies change often.

| Platform | Account prerequisites | App / scopes | Review / audit blocker |
|---|---|---|---|
| **LinkedIn (PLN company Page)** | Company **Page ID** (not supplied). The authorizing person must have the ADMINISTRATOR or CONTENT_ADMIN role | Community Management API product; `w_organization_social`, `r_organization_social`; pinned `Linkedin-Version` | LinkedIn must approve Community Management API access |
| **Facebook (PLN Page)** | Confirm that `@Pro League Network` resolves to the intended managed Page ID; Page admin via Business Manager | `pages_manage_posts`, `pages_read_engagement`, `pages_show_list`, `read_insights` | Meta App Review (Advanced Access) + Business verification |
| **Instagram (PLN, CarJitsu, Putt Tour, WPL, P2 brands)** | Professional (Business/Creator) account, Page-linked or Instagram Login | `instagram_basic` + `instagram_content_publish` (or `instagram_business_*`), `instagram_manage_insights` | Meta App Review; renders must sit at a public HTTPS URL; rolling 24h publishing limit |
| **TikTok (PLN main, CarJitsu, WPL, P2)** | Owner consent per account; **`@pro.league.networ` must be confirmed first** | Content Posting API (Direct Post), `video.publish`, `user.info.basic`, `video.list` | **Unaudited clients can only post SELF_ONLY (private).** Public posting needs TikTok's audit. Pull-from-URL needs domain verification |
| **YouTube (PLN, CarJitsu, WPL, P2)** | Channel owner/manager | YouTube Data API v3 `youtube.upload`; YouTube Analytics API | Uploads from unverified API projects stay private until Google's audit; quota increase |
| **X (PLN, CarJitsu, WPL, P2)** | Account owner OAuth 2.0 | `tweet.write`, `users.read`, `media.write`, `offline.access` | A paid API tier that allows the volume |
| **Twitch (PLN)** | Channel owner | `channel:manage:schedule` | No clip/VOD upload API; live needs a host and streaming rights |
| **World FootGolf `@ezz.abde`** | Confirm brand association/ownership or a creator partnership agreement | — | Hard-blocked in code until cleared |

Infrastructure:
- A secret store (env or vault) for tokens, with the refresh-token flow per platform
- Public media hosting (S3/CDN) for renders
- A domain verified with TikTok/Meta where pull-from-URL is used
- SSO in front of the dashboard
- Hosted Postgres

## 2. Launch-readiness checklist (in order)

1. ☐ Confirm ownership/admin of each P0 account. Record it with `pln accounts verify <id> --owner "<name>"`.
2. ☐ Decide what `@pro.league.networ` is for (or retire it). Do the same for `@ezz.abde`. Clear the hard blocks only with a written justification.
3. ☐ Get the PLN LinkedIn company Page ID and connect it via OAuth. This replaces the `pln.linkedin.pending` placeholder.
4. ☐ Export 30–90 days of native analytics for the P0 accounts and all 4 WPL accounts, then `pln metrics import`. Re-run `pln migration sheet`.
5. ☐ Sign off each WPL migration decision (`pln migration approve <platform>`). Admins apply approved profile proposals **manually**.
6. ☐ Replace sample sidecars with real licensed footage: license, territory, expiry, verified people/facts and annotated moments.
7. ☐ Brand review of `pln_agent/strategists/platforms.py` LEX descriptors and CTAs (voice, taglines, debate prompts).
8. ☐ Create the platform developer apps, request scopes, and pass app review/audits (table above).
9. ☐ Set up media hosting (`PLN_MEDIA_PUBLIC_BASE_URL`) and token storage (`credential_ref`).
10. ☐ For each account: `accounts connect` → `accounts authorize`. Start with **one** low-risk account per platform.
11. ☐ Run the test suite against Postgres and deploy the worker and dashboard behind SSO.
12. ☐ First live post: set `PLN_DRY_RUN=false` and `PLN_PUBLISHING_ENABLED=true` for that one account only. Keep two-person approval on, have a human watching, and practice the emergency stop beforehand.
13. ☐ After 2 weeks with no errors, consider the opt-in evergreen policy (`config/auto_publish_policy.json`), and only for allowlisted series and accounts.

## 3. 30/60/90-day strategic plan (targets are set after baselines are in)

**Days 0–30: baseline and foundation**
- Inventory audit, analytics export and baselines (followers, posting frequency, reach per post, watch time, shares/saves, profile→follow) for PLN, CarJitsu, Putt Tour and WPL.
- WPL decision sheets with real evidence. Sign off by day 14, then three announcement creatives (founder, athlete-led, rules + highlights).
- Footage pipeline: production delivers 10+ annotated, licensed moments per week each for CarJitsu and Putt Tour.
- Dry-run calendar every week, with human approval of 100% of posts. First live posts go to one account per platform once access is granted.
- LinkedIn: 2–3 posts a week from the "Why This Sport Works" B2B series once the Page is connected. Track qualified conversations, not impressions.

**Days 31–60: native growth engine**
- CarJitsu and Putt Tour short video: run the TikTok/Reels/Shorts series ("Wait For It", "The Moment", "Rules in 20 Seconds") with at most 2 experiments per platform per week: hook style, then posting slot.
- PLN amplification: discovery and curation angles at least 24h after the canonical post, plus PLN Facebook community conversation.
- WPL bridge: at most 2 teasers a week on Instagram pointing to `@putttour`. WPL-only platforms carry Putt Tour content with transitional copy. Evaluate in-place rebrands by day 60.
- Turn on analytics API ingestion where scopes are granted. Hold weekly learning reviews.

**Days 61–90: consolidate and scale what works**
- Put more footage into series showing durable follows per 1k views or reach. Rest fatiguing series.
- WPL consolidation report (migration proxies): retained combined reach, link clicks from legacy, canonical follows during bridge windows. Decide signpost vs. in-place per platform. Nothing is deleted.
- Bring the P2 properties (SlapFIGHT with heightened policy review, STR33T, Pro Carrom) onto an evergreen cadence. Route standout hits to PLN.
- Set numeric 2027 targets from the observed baselines.

## 4. Prioritized backlog

| P | Item |
|---|---|
| P0 | OAuth connection flow + token refresh + vault client (`credential_ref vault:`) |
| P0 | Media upload to S3/CDN + signed public URLs; LinkedIn binary multipart upload; X chunked media upload; YouTube resumable upload |
| P0 | Analytics API connectors (IG insights, TikTok video.list/query, YouTube Analytics, LinkedIn org share statistics, FB Page insights) → `metrics` with checkpoints |
| P0 | Run the test suite against Postgres; Alembic-style migration versioning |
| P1 | Subject-aware 9:16 reframing (face/body tracking) to replace the center crop; caption styling templates per brand |
| P1 | Dashboard SSO/roles (editor vs. brand-safety vs. publisher), reviewer identity from SSO rather than a form field |
| P1 | Clip annotation UI (mark moments/facts/people with timecodes) to raise footage supply |
| P1 | `find_existing` per adapter (lookup by caption hash or recent posts) for automatic crash recovery |
| P1 | Webhooks (Meta/TikTok) for publish status instead of polling |
| P2 | Comment-derived content ideas (read-only, human-curated; no automated replies) |
| P2 | Scheduling-provider adapter, if PLN signs with an authorized partner |
| P2 | Twitch schedule automation tied to the event calendar |
| P2 | Quarterly platform-policy review task with a checklist diff |
