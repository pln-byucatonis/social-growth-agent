"""Creative Intelligence Database: what was posted, how it was made, and how it actually performed.

Every post gets one `posts` row:
* agent-published posts carry the creative decisions the agent made (hook style, duration, captions,
  route role, series, source footage and timecodes) — provenance `agent_decision`;
* historical posts imported from the ops portal carry only what can honestly be read from the caption and
  metadata — provenance `inferred_from_caption` / `inferred_from_metadata`; everything else is `unknown`
  (we don't pretend to know the opening shot of a post we didn't make).

`what_works` answers "which creative variables go with better outcomes for this brand/platform?" with a
stated evidence rule (sample size + seeded permutation test on the median difference), and says
"insufficient data" instead of guessing.
"""
from __future__ import annotations

import random
import re
import statistics
from zoneinfo import ZoneInfo

from .db import DB, now_iso

CTA_PATTERNS = {"follow": r"\bfollow\b", "comment": r"\b(comment|tell us|let us know|drop a)\b",
                "tag": r"\btag (a|your|someone)\b", "link": r"\b(link in bio|tickets|watch (the )?full|stream)\b",
                "subscribe": r"\bsubscribe\b"}
CONTENT_TYPES = [  # (label, pattern) — first match wins; inferred from caption text only
    ("rules_explainer", r"\b(rules?|how (it|this) works|explained|what is)\b"),
    ("announcement", r"\b(announc|tickets|on sale|coming soon|save the date|register|sign up)\w*"),
    ("event_promo", r"\b(tonight|this (weekend|saturday|sunday|friday)|live on|tune in|premiere)\b"),
    ("submission_highlight", r"\b(submission|tap(ped)? out|taps|choke|armbar)\b"),
    ("reversal_highlight", r"\b(reversal|comeback|flip(ped)?|turned the tables)\b"),
    ("putt_highlight", r"\b(putt|hole in one|ace|birdie|green)\b"),
    ("knockout_highlight", r"\b(knock(ed)? ?out|ko|slap)\b"),
    ("personality", r"\b(meet|interview|behind the scenes|bts|story of)\b"),
    ("highlight", r"\b(highlight|moment|watch|insane|wild|unreal)\b"),
]
EMOJI = re.compile("[\U0001F300-\U0001FAFF☀-➿]")


def hour_bucket(h: int | None) -> str:
    if h is None:
        return "unknown"
    return "morning" if 5 <= h < 12 else "afternoon" if 12 <= h < 17 else "evening" if 17 <= h < 22 else "night"


def duration_bucket(d: float | None) -> str:
    if d is None:
        return "unknown"
    return "≤15s" if d <= 15 else "16–30s" if d <= 30 else "31–60s" if d <= 60 else ">60s"


def caption_features(caption: str, brand_names: list[str]) -> dict:
    c = caption or ""
    low = c.lower()
    first = c.strip().split("\n", 1)[0]
    ctype = next((label for label, pat in CONTENT_TYPES if re.search(pat, low)), "other")
    ctas = [k for k, pat in CTA_PATTERNS.items() if re.search(pat, low)]
    n = len(c)
    return {"content_type": ctype, "caption_length": "none" if n == 0 else "short" if n < 80 else "medium" if n < 220 else "long",
            "asks_question": "yes" if "?" in first or re.search(r"\?\s*$", c.strip()) else "no",
            "hashtags": "none" if not re.findall(r"#\w+", c) else "1–3" if len(re.findall(r"#\w+", c)) <= 3 else "4+",
            "mentions": "yes" if re.search(r"@\w+", c) else "no",
            "cta": ctas[0] if ctas else "none",
            "emoji": "yes" if EMOJI.search(c) else "no",
            "brand_named": "yes" if any(b.lower() in low for b in brand_names if b) else "no",
            "hook_text": first[:140]}


def _brand_names(db: DB, brand_id: str) -> list[str]:
    from .accounts import load_brands
    for b in load_brands():
        if b["brand_id"] == brand_id:
            return [b["name"], *b.get("aliases", [])]
    return []


def upsert_ingested_post(db: DB, acc: dict, p: dict, tz: str, origin: str = "ops_ingested") -> dict:
    """Historical post read from a platform API (via the ops portal ingest, or the agent's own collectors)."""
    from .audience import _dt, _extra
    pub = _dt(p["published_at"])
    media = _extra(p.get("media"))
    dur = media.get("duration_s") or media.get("duration") or media.get("video_duration")
    feats = caption_features(p.get("text") or "", _brand_names(db, acc["brand_id"]))
    local = pub.astimezone(ZoneInfo(tz)) if pub else None
    creative = {**feats, "format": p.get("post_type") or "unknown",
                "posting_hour_bucket": hour_bucket(local.hour if local else None),
                "weekday": local.strftime("%a") if local else "unknown",
                "duration_bucket": duration_bucket(float(dur)) if dur else "unknown",
                "hook_style": "unknown", "opening_style": "unknown", "captions_burned": "unknown",
                "commentary": "unknown", "route_role": "unknown", "series": "unknown"}
    prov = {k: "inferred_from_caption" for k in feats}
    prov.update({k: "inferred_from_metadata" for k in ("format", "posting_hour_bucket", "weekday")})
    prov["duration_bucket"] = "inferred_from_metadata" if dur else "unknown"
    prov.update({k: "unknown" for k in ("hook_style", "opening_style", "captions_burned", "commentary", "route_role", "series")})
    pid = f"{acc['platform']}:{p['platform_post_id']}"
    existing = db.get("posts", "post_id", pid)
    if existing and existing.get("origin") == "agent_published":  # never downgrade the agent's own record
        return existing
    rec = {"post_id": pid, "account_id": acc["account_id"], "brand_id": acc["brand_id"], "platform": acc["platform"],
           "platform_post_id": str(p["platform_post_id"]), "published_at": pub.replace(microsecond=0).isoformat() if pub else None,
           "permalink": p.get("permalink") or "", "post_type": p.get("post_type") or "", "origin": origin,
           "variant_id": "", "asset_id": "", "caption": p.get("text") or "", "duration_s": float(dur) if dur else None,
           "creative": creative, "provenance": prov, "source_footage": {},
           "first_seen_at": existing["first_seen_at"] if existing else now_iso(), "updated_at": now_iso()}
    db.insert("posts", rec, upsert_key="post_id")
    return rec


def upsert_agent_post(db: DB, variant: dict, platform_post_id: str, permalink: str, published_at: str, tz: str) -> dict:
    """Called when the agent's own post goes live: the creative decisions are known exactly."""
    from .audience import _dt
    acc = db.get("accounts", "account_id", variant["account_id"])
    asset = db.get("assets", "asset_id", variant["asset_id"]) or {}
    edl = variant.get("edl") or {}
    segs = edl.get("segments") or []
    dur = sum(s["source_out"] - s["source_in"] for s in segs) if segs else None
    pub = _dt(published_at)
    local = pub.astimezone(ZoneInfo(tz)) if pub else None
    hooks = variant.get("hook_candidates") or []
    feats = caption_features(variant.get("caption") or "", _brand_names(db, variant["brand_id"]))
    has_speech = bool(asset.get("transcript")) or bool(((asset.get("probe") or {}).get("ops_clip") or {}).get("transcript"))
    creative = {**feats, "format": variant.get("format") or "unknown", "hook_text": variant.get("hook") or feats["hook_text"],
                "hook_style": hooks[0].get("style", "unknown") if hooks else "unknown",
                "opening_style": (segs[0].get("purpose") or "unknown") if segs else "unknown",
                "captions_burned": "yes" if variant.get("onscreen_text") else "no",
                "commentary": "speech in source audio" if has_speech else "unknown",
                "duration_bucket": duration_bucket(dur), "route_role": variant.get("route_role") or "unknown",
                "series": variant.get("series") or "none",
                "posting_hour_bucket": hour_bucket(local.hour if local else None),
                "weekday": local.strftime("%a") if local else "unknown",
                "duration_variant": edl.get("variant_label") or "default",
                "overlay_style": (variant.get("render_qc") or {}).get("overlay_style") or "plain",
                "trend": ((variant.get("treatment") or {}).get("trend") or "none").split(":")[1] if (variant.get("treatment") or {}).get("trend") else "none"}
    prov = {k: "agent_decision" for k in creative}
    prov.update({k: "inferred_from_caption" for k in ("content_type", "caption_length", "asks_question", "hashtags",
                                                      "mentions", "cta", "emoji", "brand_named")})
    if creative["commentary"] == "unknown":
        prov["commentary"] = "unknown"
    clip = ((asset.get("probe") or {}).get("ops_clip") or {})
    footage = {"asset_id": variant["asset_id"], "media_path": asset.get("media_path"), "ops_clip_id": clip.get("id"),
               "source_title": clip.get("source_title"), "segments": segs,
               "transcript_excerpt": (clip.get("transcript") or "")[:400], "visual_description": clip.get("machine_caption"),
               "render_path": variant.get("render_path"), "public_media_url": variant.get("public_media_url")}
    pid = f"{acc['platform']}:{platform_post_id}"
    rec = {"post_id": pid, "account_id": acc["account_id"], "brand_id": variant["brand_id"], "platform": acc["platform"],
           "platform_post_id": str(platform_post_id), "published_at": pub.replace(microsecond=0).isoformat() if pub else None,
           "permalink": permalink or "", "post_type": variant.get("post_type") or variant.get("format") or "", "origin": "agent_published",
           "variant_id": variant["variant_id"], "asset_id": variant["asset_id"], "caption": variant.get("caption") or "",
           "duration_s": dur, "creative": creative, "provenance": prov, "source_footage": footage,
           "first_seen_at": now_iso(), "updated_at": now_iso()}
    db.insert("posts", rec, upsert_key="post_id")
    return rec


# ---- "what works" -------------------------------------------------------------------------------

VARIABLES = ["content_type", "format", "hook_style", "opening_style", "duration_bucket", "captions_burned", "commentary",
             "caption_length", "asks_question", "cta", "hashtags", "mentions", "emoji", "brand_named",
             "posting_hour_bucket", "weekday", "series", "route_role", "duration_variant", "overlay_style", "trend"]
OUTCOMES = ["views", "follows_per_1k_views", "shares_per_1k_views", "saves_per_1k_views", "comments_per_1k_views",
            "engagements_per_1k_views", "avg_watch_time_s", "completion_rate"]
MIN_N, CONCLUSIVE_N = 5, 8


def permutation_p(a: list[float], b: list[float], iters: int = 2000, seed: int = 7) -> float:
    """Two-sided p-value for the difference in medians (seeded → reproducible). Sequential: a quick first
    pass stops early when the difference is clearly not significant, so only close calls pay full cost."""
    obs = abs(statistics.median(a) - statistics.median(b))
    pool, na = a + b, len(a)
    rng = random.Random(seed)
    hits, done = 0, 0
    for stage in (200, iters):
        while done < stage:
            rng.shuffle(pool)
            if abs(statistics.median(pool[:na]) - statistics.median(pool[na:])) >= obs - 1e-12:
                hits += 1
            done += 1
        if (hits + 1) / (done + 1) > 0.3:
            break
    return (hits + 1) / (done + 1)


def evidence_class(n_a: int, n_b: int, q: float | None) -> str:
    """`q` is the false-discovery-adjusted p-value (Benjamini–Hochberg within the slice)."""
    if min(n_a, n_b) < MIN_N or q is None:
        return "insufficient"
    if min(n_a, n_b) >= CONCLUSIVE_N and q < 0.05:
        return "conclusive"
    if q < 0.2:
        return "suggestive"
    return "no_clear_difference"


def bh_adjust(ps: list[float]) -> list[float]:
    """Benjamini–Hochberg q-values (same order as input)."""
    m = len(ps)
    order = sorted(range(m), key=lambda i: ps[i])
    q = [0.0] * m
    prev = 1.0
    for rank, i in reversed(list(enumerate(order, start=1))):
        prev = min(prev, ps[i] * m / rank)
        q[i] = prev
    return q


EVIDENCE_OUTCOMES = ["follows_per_1k_views", "shares_per_1k_views", "saves_per_1k_views", "comments_per_1k_views",
                     "engagements_per_1k_views", "completion_rate"]
EVIDENCE_RULE = (f"Each value is compared with all other posts in the same slice (yes/no variables are tested once). "
                 f"Fewer than {MIN_N} posts on either side → insufficient. p-values come from a seeded permutation test on "
                 "the median difference and are adjusted for the number of comparisons in the slice (Benjamini–Hochberg). "
                 f"≥{CONCLUSIVE_N} posts a side and adjusted q<0.05 → conclusive; q<0.2 → suggestive. The difference must "
                 "point the same way in the earlier and the later half of the posts, otherwise it is reported as unstable "
                 "(a sign of a time confound, e.g. a strategy change). Posts younger than 72h are excluded; slices compare "
                 "like with like. Only per-1k-view rates become PLN evidence — raw views mostly reflect age and distribution.")


def what_works(db: DB, brand_id: str = "", platform: str = "", outcome: str = "shares_per_1k_views",
               fmt: str = "", account_id: str = "", variables: list[str] | None = None) -> dict:
    from .audience import post_outcomes
    if outcome not in OUTCOMES:
        raise ValueError(f"outcome must be one of {OUTCOMES}")
    accs = [account_id] if account_id else [a["account_id"] for a in db.all("accounts")
                                            if (not brand_id or a["brand_id"] == brand_id)
                                            and (not platform or a["platform"] == platform)]
    posts = [p for p in post_outcomes(db, accs) if not p["young"] and p["outcomes"].get(outcome) is not None
             and (not fmt or p.get("post_type") == fmt)]
    findings = []
    ordered = sorted(posts, key=lambda p: str(p.get("published_at") or ""))
    half = len(ordered) // 2
    early = {id(p) for p in ordered[:half]}
    for var in variables or VARIABLES:
        if var == "format" and fmt:
            continue
        groups: dict[str, list[dict]] = {}
        for p in posts:
            val = (p.get("creative") or {}).get(var)
            if val in (None, "", "unknown"):
                continue
            groups.setdefault(str(val), []).append(p)
        if len(groups) < 2:
            continue
        values = list(groups)
        if len(values) == 2:  # two-valued: one comparison, reported once ('yes' side if any, else the smaller group)
            values = ["yes"] if "yes" in values else [min(values, key=lambda v: (len(groups[v]), v))]
        for val in values:
            xs = [p["outcomes"][outcome] for p in groups[val]]
            rest_p = [p for v, ps in groups.items() if v != val for p in ps]
            rest = [p["outcomes"][outcome] for p in rest_p]
            pv = permutation_p(xs, rest) if min(len(xs), len(rest)) >= MIN_N else None
            med, rmed = statistics.median(xs), statistics.median(rest) if rest else None

            def half_sign(in_early: bool):
                a = [p["outcomes"][outcome] for p in groups[val] if (id(p) in early) == in_early]
                b = [p["outcomes"][outcome] for p in rest_p if (id(p) in early) == in_early]
                if len(a) < 3 or len(b) < 3:
                    return None
                d = statistics.median(a) - statistics.median(b)
                return 0 if d == 0 else (1 if d > 0 else -1)
            s1, s2 = half_sign(True), half_sign(False)
            findings.append({"variable": var, "value": val, "n": len(xs), "n_rest": len(rest), "median": med,
                             "median_rest": rmed, "lift": (med / rmed - 1) if rmed else None, "p": pv,
                             "stable": None if s1 is None or s2 is None else (s1 == s2 and s1 != 0)})
    testable = [f for f in findings if f["p"] is not None]
    for f, q in zip(testable, bh_adjust([f["p"] for f in testable]) if testable else []):
        f["q"] = q
    for f in findings:
        f.setdefault("q", None)
        f["evidence"] = evidence_class(f["n"], f["n_rest"], f["q"])
        if f["evidence"] in ("conclusive", "suggestive"):
            if f["stable"] is False:
                f["evidence"] = "unstable"
            elif f["stable"] is None and f["evidence"] == "conclusive":
                f["evidence"] = "suggestive"  # can't check stability with this few posts per half
    order = {"conclusive": 0, "suggestive": 1, "unstable": 2, "no_clear_difference": 3, "insufficient": 4}
    findings.sort(key=lambda f: (order[f["evidence"]], -(abs(f["lift"]) if f["lift"] is not None else 0)))
    return {"brand_id": brand_id, "platform": platform, "format": fmt, "outcome": outcome, "posts": len(posts),
            "rule": EVIDENCE_RULE, "findings": findings}
