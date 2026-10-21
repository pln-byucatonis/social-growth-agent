"""Social Growth Knowledge Engine: attributed, dated guidance that is re-checked, never assumed permanent.

Levels, in the order the agent applies them:
1. `platform_rule`      — binding platform requirements/limits (API docs, policy). Always obeyed.
2. `pln_evidence`       — conclusions supported by PLN's own measured data (written by `derive_pln_evidence`).
                          For recommendations, these outrank everything below.
3. `platform_guidance`  — official platform recommendations.
4. `external_research`  — credible third-party research/benchmarks. Never treated as a rule.
5. `hypothesis`         — ideas to test (starting cadences, experiment designs).

`review_sources` re-fetches every cited page (weekly from the worker), records a content hash and checks
that each entry's verbatim quote is still on the page; a vanished quote or an unreachable page moves the
entry to `needs_review` for a human. Nothing is rewritten automatically.
"""
from __future__ import annotations

import hashlib
import html
import json
import re
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from .config import CONFIG_DIR
from .db import DB, now_iso

LEVELS = ["platform_rule", "pln_evidence", "platform_guidance", "external_research", "hypothesis"]
LEVEL_LABELS = {"platform_rule": "Platform rule", "pln_evidence": "PLN evidence", "platform_guidance": "Platform guidance",
                "external_research": "External research", "hypothesis": "Hypothesis"}
REVIEW_EVERY = timedelta(days=7)
STALE_AFTER = timedelta(days=90)
UA = "PLN-Social-Growth-Agent/1.0 (+https://ops.proleaguenetwork.com; knowledge source review)"


def seed(db: DB, actor: str = "seed") -> int:
    data = json.loads((CONFIG_DIR / "knowledge_seed.json").read_text())
    n = 0
    for e in data["entries"]:
        cur = db.get("knowledge", "entry_id", e["entry_id"])
        rec = {"entry_id": e["entry_id"], "level": e["level"], "platform": e.get("platform", ""),
               "brand_id": e.get("brand_id", ""), "account_id": "", "topic": e.get("topic", ""),
               "statement": e["statement"], "quote": e.get("quote", ""), "source_url": e.get("source_url", ""),
               "source_title": e.get("source_title", ""), "publisher": e.get("publisher", ""),
               "source_date": e.get("source_date", ""), "retrieved_at": e.get("retrieved_at", ""),
               "applicability": e.get("applicability", ""), "evidence": {}, "supersedes": "",
               "created_by": actor, "updated_at": now_iso()}
        if cur:  # keep the review trail; refresh the wording from config
            rec.update({k: cur[k] for k in ("status", "reviewed_at", "reviewed_by", "created_at", "created_by")})
        else:
            rec.update({"status": "current", "reviewed_at": e.get("retrieved_at", ""), "reviewed_by": "seed",
                        "created_at": now_iso()})
        db.insert("knowledge", rec, upsert_key="entry_id")
        n += 1
    return n


def add_entry(db: DB, entry: dict, actor: str) -> dict:
    lvl = entry.get("level")
    if lvl not in LEVELS or lvl == "pln_evidence":
        raise ValueError("level must be platform_rule, platform_guidance, external_research or hypothesis "
                         "(PLN evidence is written by the engine from measured data)")
    if not (entry.get("statement") or "").strip():
        raise ValueError("statement is required")
    if lvl in ("platform_rule", "platform_guidance", "external_research"):
        if not re.match(r"^https://", entry.get("source_url") or ""):
            raise ValueError("a platform or research entry needs its source URL (https)")
        if not (entry.get("source_date") or "").strip():
            raise ValueError("a source date is required (use 'undated' plus what the page shows if there is none)")
    if not (entry.get("publisher") or entry.get("source_url")):
        raise ValueError("say where it came from (publisher or URL)")
    eid = entry.get("entry_id") or re.sub(r"[^a-z0-9]+", "-", f"{lvl}-{entry.get('platform','')}-{entry['statement'][:40]}".lower()).strip("-")
    rec = {"entry_id": eid, "level": lvl, "platform": entry.get("platform", ""), "brand_id": entry.get("brand_id", ""),
           "account_id": "", "topic": entry.get("topic", ""), "statement": entry["statement"].strip(),
           "quote": (entry.get("quote") or "").strip(), "source_url": entry.get("source_url", ""),
           "source_title": entry.get("source_title", ""), "publisher": entry.get("publisher", ""),
           "source_date": entry.get("source_date", ""), "retrieved_at": now_iso()[:10], "reviewed_at": now_iso(),
           "reviewed_by": actor, "status": "current", "applicability": entry.get("applicability", ""), "evidence": {},
           "supersedes": "", "created_by": actor, "created_at": now_iso(), "updated_at": now_iso()}
    db.insert("knowledge", rec, upsert_key="entry_id")
    db.audit(actor, "knowledge_added", "knowledge", eid, {"level": lvl, "source_url": rec["source_url"]})
    return rec


def set_status(db: DB, entry_id: str, status: str, actor: str, note: str = "") -> None:
    if status not in ("current", "needs_review", "retired"):
        raise ValueError("status must be current, needs_review or retired")
    db.update("knowledge", "entry_id", entry_id, {"status": status, "reviewed_at": now_iso(), "reviewed_by": actor,
                                                  "updated_at": now_iso()})
    db.audit(actor, "knowledge_" + status, "knowledge", entry_id, {"note": note})


# ---- source review ------------------------------------------------------------------------------

def _text(raw: str) -> str:
    raw = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", raw)
    raw = re.sub(r"(?s)<[^>]+>", " ", raw)
    return re.sub(r"\s+", " ", html.unescape(raw)).strip()


def _norm(s: str) -> str:
    s = s.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    return re.sub(r"\s+", " ", s).strip().lower()


def urllib_fetch(url: str, timeout: float = 20) -> tuple[int, str]:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html,*/*"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(3_000_000).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except (urllib.error.URLError, TimeoutError, OSError):
        return 0, ""


def review_sources(db: DB, fetch=urllib_fetch, actor: str = "knowledge-review", force: bool = False) -> dict:
    # benchmark entries are re-verified by every Business Discovery snapshot, not by fetching the profile page
    entries = [e for e in db.all("knowledge", "status IN ('current','needs_review') AND source_url LIKE ?", ["https://%"])
               if e.get("topic") != "benchmark"]
    by_url: dict[str, list[dict]] = {}
    for e in entries:
        by_url.setdefault(e["source_url"], []).append(e)
    now = datetime.now(timezone.utc)
    report = {"checked": 0, "skipped_recent": 0, "changed": [], "quote_missing": [], "unreachable": [], "ok": []}
    for url, es in by_url.items():
        src = db.get("knowledge_sources", "url", url)
        if src and not force and src.get("last_checked_at"):
            last = datetime.fromisoformat(str(src["last_checked_at"]).replace("Z", "+00:00"))
            if now - (last if last.tzinfo else last.replace(tzinfo=timezone.utc)) < REVIEW_EVERY:
                report["skipped_recent"] += 1
                continue
        status, body = fetch(url)
        report["checked"] += 1
        rec = {"url": url, "title": es[0].get("source_title", ""), "last_checked_at": now_iso(), "http_status": status,
               "content_hash": (src or {}).get("content_hash", ""), "changed_at": (src or {}).get("changed_at", ""),
               "status": "", "note": ""}
        if status != 200 or not body:
            rec.update({"status": "unreachable", "note": f"HTTP {status or 'error'}"})
            report["unreachable"].append(url)
            for e in es:
                _flag(db, e, f"source unreachable on {now_iso()[:10]} (HTTP {status or 'error'})", actor)
        else:
            text = _text(body)
            digest = hashlib.sha256(_norm(text).encode()).hexdigest()
            changed = bool(src and src.get("content_hash") and src["content_hash"] != digest)
            rec.update({"content_hash": digest, "status": "changed" if changed else ("unchanged" if src else "baseline")})
            if changed:
                rec["changed_at"] = now_iso()
                report["changed"].append(url)
            missing = [e for e in es if e.get("quote") and _norm(e["quote"]) not in _norm(text)]
            for e in missing:
                _flag(db, e, f"quote no longer found on the page ({now_iso()[:10]})", actor)
                report["quote_missing"].append(e["entry_id"])
            for e in es:
                if e not in missing:
                    db.update("knowledge", "entry_id", e["entry_id"],
                              {"reviewed_at": now_iso(), "reviewed_by": actor,
                               **({"status": "current"} if e["status"] == "needs_review" and e.get("reviewed_by") == actor else {})})
            if missing:
                rec["status"] = "quote_missing"
            else:
                report["ok"].append(url)
        db.insert("knowledge_sources", rec, upsert_key="url")
    for e in db.all("knowledge", "status='current' AND level IN ('platform_rule','platform_guidance','external_research')"):
        if e.get("topic") == "benchmark":
            continue
        ra = str(e.get("reviewed_at") or e.get("retrieved_at") or "")
        try:
            d = datetime.fromisoformat(ra.replace("Z", "+00:00")) if "T" in ra else datetime.fromisoformat(ra + "T00:00:00+00:00")
        except ValueError:
            continue
        if now - (d if d.tzinfo else d.replace(tzinfo=timezone.utc)) > STALE_AFTER:
            _flag(db, e, f"not verified in {STALE_AFTER.days} days", actor)
    db.audit(actor, "knowledge_reviewed", "knowledge", "", report)
    return report


def _flag(db: DB, e: dict, why: str, actor: str) -> None:
    if e["status"] == "needs_review":
        return
    db.update("knowledge", "entry_id", e["entry_id"], {"status": "needs_review", "updated_at": now_iso(),
                                                       "reviewed_by": actor,
                                                       "applicability": (e.get("applicability") or "") + f" [review: {why}]"})
    db.audit(actor, "knowledge_needs_review", "knowledge", e["entry_id"], {"why": why})


# ---- PLN evidence -------------------------------------------------------------------------------

def derive_pln_evidence(db: DB, slices: list[dict] | None = None, actor: str = "knowledge-engine") -> dict:
    """Turn conclusive/suggestive Creative Intelligence findings into dated `pln_evidence` entries.
    Re-running replaces each slice's entries; findings that no longer hold are superseded, not deleted."""
    from .creative_db import EVIDENCE_OUTCOMES, EVIDENCE_RULE, what_works
    full = slices is None
    if full:
        slices = []
        from .creative_db import MIN_N
        for r in db.query("SELECT brand_id, platform, COUNT(*) AS n FROM posts GROUP BY brand_id, platform"):
            if r["n"] < 2 * MIN_N:
                continue
            for out in EVIDENCE_OUTCOMES:
                slices.append({"brand_id": r["brand_id"], "platform": r["platform"], "outcome": out})
    written, superseded = [], []
    for sl in slices:
        res = what_works(db, sl.get("brand_id", ""), sl.get("platform", ""), sl["outcome"], sl.get("format", ""))
        prefix = f"pln-{sl.get('brand_id') or 'all'}-{sl.get('platform') or 'all'}-{sl['outcome']}-"
        keep = set()
        for f in res["findings"]:
            if f["evidence"] not in ("conclusive", "suggestive") or f["lift"] is None:
                continue
            eid = prefix + re.sub(r"[^a-z0-9]+", "-", f"{f['variable']}-{f['value']}".lower()).strip("-")
            keep.add(eid)
            direction = "higher" if f["lift"] > 0 else "lower"
            stmt = (f"On {sl.get('platform') or 'all platforms'} for {sl.get('brand_id') or 'all brands'}, posts with "
                    f"{f['variable']} = {f['value']} have {direction} median {sl['outcome']} "
                    f"({f['median']:.2f} vs {f['median_rest']:.2f}, {f['lift']:+.0%}; n={f['n']} vs {f['n_rest']}; "
                    f"q={f['q']:.3f}).")
            db.insert("knowledge", {
                "entry_id": eid, "level": "pln_evidence", "platform": sl.get("platform", ""),
                "brand_id": sl.get("brand_id", ""), "account_id": "", "topic": f["variable"], "statement": stmt,
                "quote": "", "source_url": "", "source_title": "PLN Creative Intelligence Database",
                "publisher": "PLN measured performance", "source_date": now_iso()[:10], "retrieved_at": now_iso()[:10],
                "reviewed_at": now_iso(), "reviewed_by": actor, "status": "current",
                "applicability": f"{f['evidence']} under the stated rule", "evidence": {**f, "outcome": sl["outcome"],
                                                                                         "posts": res["posts"], "rule": EVIDENCE_RULE},
                "supersedes": "", "created_by": actor, "created_at": now_iso(), "updated_at": now_iso()}, upsert_key="entry_id")
            written.append(eid)
        for old in db.all("knowledge", "level='pln_evidence' AND status='current' AND entry_id LIKE ?", [prefix + "%"]):
            if old["entry_id"] not in keep:
                db.update("knowledge", "entry_id", old["entry_id"], {"status": "superseded", "updated_at": now_iso(),
                                                                     "applicability": f"no longer supported as of {now_iso()[:10]}"})
                superseded.append(old["entry_id"])
    if full:  # a full run re-judges everything: what it didn't re-confirm (e.g. a retired outcome) is superseded
        for old in db.all("knowledge", "level='pln_evidence' AND status='current'"):
            if old["entry_id"] not in written:
                db.update("knowledge", "entry_id", old["entry_id"], {"status": "superseded", "updated_at": now_iso(),
                                                                     "applicability": f"not re-confirmed on {now_iso()[:10]}"})
                superseded.append(old["entry_id"])
    db.audit(actor, "pln_evidence_derived", "knowledge", "", {"written": len(written), "superseded": superseded})
    return {"written": written, "superseded": superseded}


def guidance(db: DB, topic: str = "", platform: str = "", brand_id: str = "") -> list[dict]:
    """Applicable current guidance, ordered by level precedence. Cross-platform/brand entries apply too."""
    rows = db.all("knowledge", "status IN ('current','needs_review')")
    out = [e for e in rows if (not topic or e["topic"] == topic)
           and (not platform or e["platform"] in ("", platform))
           and (not brand_id or e["brand_id"] in ("", brand_id))]
    out.sort(key=lambda e: (LEVELS.index(e["level"]) if e["level"] in LEVELS else 9, e["entry_id"]))
    return out
