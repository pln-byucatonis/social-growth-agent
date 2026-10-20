# PLN Social Portfolio — Account Audit & Migration Playbook

Source: user-provided account inventory, October 20, 2026. The handles and account ownership have NOT been verified. Never assert follower counts without actual connected analytics. See `config/accounts.csv` for 28 account rows and priority tags.

## Portfolio architecture
- **P0 / master publisher:** Pro League Network (PLN) — B2B LinkedIn, platform-native Instagram/TikTok/Facebook, YouTube longform/Shorts and event trailers, X live commentary, Twitch live community. Treat TikTok `@pro.league.networ` as an unverified secondary account: verify owner, purpose and correct spelling before posting. Facebook `@Pro League Network` may be a page display name rather than a valid handle. LinkedIn is strategically important but no page handle was supplied; request authorized company page ID when connecting, not before implementing offline planner.
- **P0 / creator-first growth:** CarJitsu Championship — Instagram/TikTok narrative hooks, YouTube episodic clips and Shorts, X moment-based results. PLN amplifies and cross-promotes without duplicating exact exports.
- **P0 / controlled transition:** Putt Tour — `@putttour` Instagram is target brand; WPL accounts are legacy distribution assets and migration funnels. Do NOT assume social platforms support merging accounts, transferring subscribers or moving historical content/engagement.
- **P2:** SlapFIGHT Championship, STR33T, Pro Carrom Tour — distinct sports audiences; maintain evergreen cadence and route standout hits to PLN. SlapFIGHT requires heightened violence/platform-policy and advertiser-suitability review.
- **P3:** World FootGolf Tour TikTok `@ezz.abde` — confirm brand association, ownership and rights before including in automated publishing.

## Channel-specific responsibilities
LinkedIn: PLN's verified company page / executives (admin credentials still required): industry POV, partnership proof, media-rights/format case studies; track qualified B2B responses, not vanity views. No LinkedIn handle was given.
Facebook: PLN home base, community posts/events/native clips and fan conversations. Verify that listed Facebook identifier resolves to the intended managed Page.
Instagram: visual property-led fandom, Reels/Stories/carousels; CarJitsu = competitive tension/comedy, Putt Tour = skill/pressure/tour personalities, PLN = discovery/cross-property curation.
TikTok: immediate visual hook, concise context, native edits, repeat series and comment-derived content ideas; no copied watermarked clips or engagement bots.
YouTube: SEO-led evergreen explainers and full matches where licensed, Shorts-to-full-video playlists, event archives; use property channels and selective PLN channel compilation.
X: live event context, schedules, scores, creator/athlete moments and distribution news, not duplicate social captions.
Twitch: PLN event-oriented live programming and community management subject to streaming rights, rather than unattended looping to satisfy posting targets.

## World Putting League → Putt Tour: proposed 90-day audience migration
**Days 0–14: audit and decide.** Export platform-authorized account-level analytics: followers, unique reach, 30/90 day activity, top posts, geo/demographics, watch-through, organic visits and links. Establish ownership/admin of all four WPL channels and Putt Tour Instagram. Assess naming availability and whether each legacy handle has stronger distribution than a new account. Catalog archives, URLs, backlinks, badges, watermarks, partnerships and rights. Choose primary account per platform (generally preserve the largest healthy existing audience when possible, after audit). DO NOT change usernames until plan is signed off and handle availability checked. No follower merging is promised.
**Days 15–30: communicate rebrand.** On legacy WPL properties update display names, bios, banners and pinned explanations to 'World Putting League is now Putt Tour' when administratively supported. Publish three distinct announcement creatives: founder/tour story, athlete-led announcement, and fast rules/highlights; update website/link hub and add tracked migration links. On Instagram choose between (A) repurposing strong WPL account as Putt Tour if its followers and engagement materially exceed @putttour and rename logistics permit, or (B) retaining @putttour as canonical and using @worldputtingleague as a 60–90-day audience bridge. Do not try to force a two-account merge.
**Days 31–60: run migration funnel.** Keep a low-frequency WPL legacy posting stream (e.g. 2/week) featuring teasers linking/tagging Putt Tour and exact follow CTA; put all exclusive new originals first on canonical Putt Tour account. Pin updated announcement and reply helpfully to genuine questions. Where Putt Tour has no matching platform channel, evaluate rebranding the existing WPL account IN PLACE rather than creating a competing fresh account. Test hook variants; avoid repetitive identical posts.
**Days 61–90: consolidate.** Compare audiences and baseline vs outcome: canonical follower growth, unique viewers reached, shares/saves, attributable profile visits, link clicks, audience overlap proxy, returning viewers, % of legacy audience reached, and watch time. Maintain legacy accounts as signposts or repurpose in place; do not delete archives or accounts without a separate backup, rights review and explicit authorization.

## Agent implementation requirements
1. Account is first-class data model: stable ID, brand, platform, username, canonical brand, status, verification, owner/admin, credential reference, integration capability, active/legacy role and redirect destination.
2. Asset route: original video → sport-brand canonical account → selected PLN amplification variant, with platform spacing/dedup and provenance. One source can generate unique native posts; never blindly blast every handle.
3. Per-account priorities and budgets (P0 gets roughly 80% creative effort initially as a *planning assumption*, not actual staffing budget). Avoid arbitrary daily content volume without proven sufficient footage.
4. Analytics by account, property, creative format and platform, with baseline capture before migration and 7/30/90-day cohorts.
5. Suggested strategy changes require evidence, don't silently change account handles, bios, public profile data or publish content. Account changes and any first live publication require explicit approval.
6. Obtain required credentials, rights and permissions through official OAuth or authorized publishing providers. No scraping or account transfer workaround.

## Open audit questions (do not block offline build)
Are PLN's two TikTok accounts both owned and in use? Which has higher engaged reach? Does WPL's Instagram account have significantly more engagement than @putttour? Are the legacy WPL TikTok/YouTube/X profiles centrally controlled? Is `@ezz.abde` owned or a creator partnership? What is the PLN LinkedIn company Page ID? Are there property Facebook Pages?
