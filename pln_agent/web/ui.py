"""Presentation helpers for the dashboard: human labels for ids and states, local dates, numbers, status chips.

Every template uses these so operators never read snake_case ids, raw ISO timestamps or 0.8912 scores.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from markupsafe import Markup, escape

from ..config import CONFIG_DIR

PLATFORMS = {"instagram": "Instagram", "tiktok": "TikTok", "youtube": "YouTube", "x": "X", "facebook": "Facebook",
             "linkedin": "LinkedIn", "twitch": "Twitch", "threads": "Threads", "snapchat": "Snapchat"}

# status → (label, tone). Tones: ok · warn · bad · info · review · muted
STATUS = {
    "needs_fix": ("Needs fix", "bad"), "in_review": ("In review", "review"), "approved": ("Approved", "ok"),
    "scheduled": ("Scheduled", "info"), "simulated": ("Simulated", "muted"), "published": ("Published", "ok"),
    "failed": ("Failed", "bad"), "rejected": ("Rejected", "muted"), "cancelled": ("Cancelled", "muted"),
    "publishing": ("Publishing", "info"), "queued": ("Queued", "info"), "halted": ("Halted", "warn"),
    "blocked": ("Blocked", "bad"), "deferred": ("Deferred", "warn"), "done": ("Done", "ok"), "missed": ("Missed", "bad"),
    "planned": ("Planned", "muted"), "proposed": ("Proposed", "muted"),
    "drafted": ("Draft ready", "review"), "needs_answer": ("Needs an answer", "warn"), "escalated": ("Escalated", "bad"),
    "flagged_spam": ("Spam", "muted"), "no_action": ("No action", "muted"), "ignored": ("Ignored", "muted"),
    "replied": ("Replied", "ok"), "simulated_reply": ("Reply simulated", "muted"), "own": ("Ours", "muted"),
    "running": ("Running", "info"), "concluded": ("Concluded", "ok"), "inconclusive": ("Inconclusive", "warn"),
    "abandoned": ("Stopped", "muted"), "open": ("Open", "muted"), "thinking": ("Thinking", "info"),
    "current": ("Current", "ok"), "needs_review": ("Needs review", "warn"), "superseded": ("Superseded", "muted"),
    "retired": ("Retired", "muted"), "active": ("Active", "ok"),
    "oauth_connected": ("Connected", "ok"), "ownership_confirmed": ("Owner confirmed", "info"), "unverified": ("Not connected", "muted"),
    "increase": ("Post more", "ok"), "decrease": ("Post less", "warn"), "hold": ("Hold", "muted"),
    "production_limited": ("Supply-limited", "warn"), "collect_baseline": ("Collecting baseline", "muted"),
    "conclusive": ("Conclusive", "ok"), "suggestive": ("Suggestive", "info"), "insufficient": ("Not enough data", "muted"),
    "no_clear_difference": ("No clear difference", "muted"), "unstable": ("Unstable", "warn"),
    "measured": ("Measured", "ok"), "derived": ("Derived", "info"), "unavailable": ("Unavailable", "muted"),
    "rendered": ("Rendered", "ok"), "not_rendered": ("Not rendered", "muted"), "render_failed": ("Render failed", "bad"),
    "P0": ("P0", "bad"), "P1": ("P1", "warn"), "P2": ("P2", "muted"), "P3": ("P3", "muted"),
}
WORDS = {"pln": "PLN", "wpl": "WPL", "ig": "IG", "yt": "YouTube", "utm": "UTM", "qc": "QC", "cta": "CTA", "faq": "FAQ",
         "b2b": "B2B", "kpi": "KPI", "1k": "1k"}
LABELS = {
    "posting_gap": "Posting gap", "partnership_approval": "Partnership sign-off", "rights_blocker": "Rights blocker",
    "missed_post": "Missed post", "baseline": "No analytics", "fatigue": "Series fatigue", "scale": "Scale up",
    "pln_amplification": "PLN amplification", "legacy_bridge": "WPL bridge", "legacy_inplace": "Legacy (in place)",
    "canonical": "Home account", "pln_b2b": "PLN B2B", "where_to_watch": "Where to watch", "athlete_interest": "Wants to compete",
    "shares_per_1k_views": "Shares / 1k views", "engagements_per_1k_views": "Engagements / 1k views",
    "follows_per_1k_views": "Follows / 1k views", "comments_per_1k_views": "Comments / 1k views",
    "saves_per_1k_views": "Saves / 1k views", "likes_per_1k_views": "Likes / 1k views", "avg_watch_time_s": "Avg watch time",
    "posting_hour_bucket": "Posting time", "hook_style": "Hook style", "captions_burned": "Burned-in captions",
}


def label(v) -> str:
    if v is None or v == "":
        return "—"
    s = str(v)
    if s in LABELS:
        return LABELS[s]
    if s in STATUS:
        return STATUS[s][0]
    words = s.replace("-", "_").split("_")
    out = [WORDS.get(w.lower(), w) for w in words if w]
    return (out[0][:1].upper() + out[0][1:] + (" " + " ".join(out[1:]) if len(out) > 1 else "")) if out else s


def chip(v, tone: str | None = None, text: str | None = None) -> Markup:
    s = "" if v is None else str(v)
    lab, t = STATUS.get(s, (label(s), "muted"))
    return Markup(f'<span class="chip {escape(tone or t)}">{escape(text or lab)}</span>')


_brands: dict | None = None


def brand(bid) -> str:
    global _brands
    if _brands is None:
        _brands = {b["brand_id"]: b["name"] for b in json.loads((CONFIG_DIR / "brands.json").read_text())["brands"]}
    return _brands.get(bid or "", label(bid))


def platform(p) -> str:
    return PLATFORMS.get(p or "", label(p))


def _dt(v) -> datetime | None:
    if not v:
        return None
    if isinstance(v, datetime):
        d = v
    else:
        try:
            d = datetime.fromisoformat(str(v).replace("Z", "+00:00").replace("+0000", "+00:00"))
        except ValueError:
            return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


class Fmt:
    def __init__(self, tz: str, db_fn):
        self.tz = ZoneInfo(tz)
        self.db_fn = db_fn
        self._acc: tuple[float, dict] = (0.0, {})

    def when(self, v, short: bool = False) -> str:
        d = _dt(v)
        if not d:
            return "—" if not v else str(v)
        if isinstance(v, str) and len(v) == 10:  # a plain date
            return d.strftime("%b %-d") if short else d.strftime("%a, %b %-d, %Y")
        d = d.astimezone(self.tz)
        return d.strftime("%b %-d, %-I:%M %p") if short else d.strftime("%a, %b %-d · %-I:%M %p")

    def localinput(self, v) -> str:
        """Value for an <input type=datetime-local> in the desk's time zone."""
        d = _dt(v)
        return d.astimezone(self.tz).strftime("%Y-%m-%dT%H:%M") if d else ""

    def ago(self, v) -> str:
        d = _dt(v)
        if not d:
            return "never"
        s = (datetime.now(timezone.utc) - d).total_seconds()
        if s < 0:
            s = -s
            for n, u in ((86400, "day"), (3600, "hour"), (60, "minute")):
                if s >= n:
                    k = int(s // n)
                    return f"in {k} {u}{'s' if k != 1 else ''}"
            return "in a moment"
        for n, u in ((86400 * 365, "year"), (86400 * 30, "month"), (86400, "day"), (3600, "hour"), (60, "minute")):
            if s >= n:
                k = int(s // n)
                return f"{k} {u}{'s' if k != 1 else ''} ago"
        return "just now"

    def accounts(self) -> dict:
        t, m = self._acc
        if time.time() - t > 60 or not m:
            m = {a["account_id"]: a for a in self.db_fn().all("accounts")}
            self._acc = (time.time(), m)
        return m

    def acct(self, aid, with_platform: bool = True) -> str:
        a = self.accounts().get(aid or "")
        if not a:
            return label(aid)
        h = a.get("username_original") or aid
        if a["platform"] in ("instagram", "tiktok", "x", "youtube", "threads") and not str(h).startswith("@"):
            h = "@" + str(h)
        return f"{h} · {platform(a['platform'])}" if with_platform else str(h)


_ACCT_RE = None


def prose_factory(fmt: "Fmt"):
    """Generated sentences use ids ("putt_tour: …", "carjitsu.instagram.x vs …"); show names instead."""
    import re

    def prose(text) -> str:
        if not text:
            return ""
        t = str(text)
        t = re.sub(r"\b([a-z0-9_]+\.(?:instagram|tiktok|youtube|x|facebook|linkedin|twitch)\.[a-z0-9_.]+)\b",
                   lambda m: fmt.acct(m.group(1)) if m.group(1) in fmt.accounts() else m.group(1), t)
        for k, v in LABELS.items():
            if "_" in k:
                t = t.replace(k, v.lower() if not k.endswith("_1k_views") else v)
        t = re.sub(r"\b(?:for|on) (pln|[a-z]+_[a-z_]+|carjitsu|slapfight|str33t)\b",
                   lambda m: m.group(0).split(" ")[0] + " " + brand(m.group(1)) if brand(m.group(1)) != label(m.group(1)) or m.group(1) in ("pln", "carjitsu", "slapfight", "str33t") else m.group(0), t)
        t = re.sub(r"\b(on) (instagram|tiktok|youtube|facebook|linkedin)\b", lambda m: m.group(1) + " " + platform(m.group(2)), t, flags=re.I)
        t = re.sub(r"^([a-z][a-z0-9_]+):", lambda m: (brand(m.group(1)) + ":") if brand(m.group(1)) != label(m.group(1)) or m.group(1) == "pln" else m.group(0), t)
        return t
    return prose


def num(v, digits: int = 0) -> str:
    if v is None or v == "":
        return "—"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    return f"{f:,.{digits}f}"


def compact(v) -> str:
    if v is None or v == "":
        return "—"
    f = float(v)
    for n, u in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(f) >= n:
            return f"{f / n:.1f}".rstrip("0").rstrip(".") + u
    return f"{f:,.0f}"


def signed(v) -> str:
    if v is None or v == "":
        return "—"
    return f"{float(v):+,.0f}"


def pct(v, digits: int = 0) -> str:
    if v is None or v == "":
        return "—"
    return f"{float(v) * 100:.{digits}f}%"


def register(templates, settings, db_fn) -> Fmt:
    f = Fmt(settings.timezone, db_fn)
    env = templates.env
    env.filters.update({"label": label, "chip": chip, "brand": brand, "platform": platform, "when": f.when, "ago": f.ago,
                        "acct": f.acct, "localinput": f.localinput, "num": num, "compact": compact, "signed": signed, "pct": pct})
    env.filters["prose"] = prose_factory(f)
    env.globals.update({"chip": chip, "tz_name": settings.timezone})
    return f
