"""Community Manager: pull comments, triage them, draft replies a person approves, escalate what needs a human.

* Ingestion — Instagram media comments (with replies) and Facebook Page post comments for the recent posts
  we already know from the Creative DB. Re-fetching never overwrites a classification or a decision.
* Triage — rules from config/community.json, top to bottom, first match wins: threats, safety, legal and
  business enquiries are escalated (no public reply is drafted); spam is flagged; the rest get a priority.
* Replies — drafted from templates that may only use links set in the config. A draft with a gap is marked
  `needs_answer`. Nothing is sent without approval, and sending uses the same live gates as publishing
  (dry-run, publishing enabled, emergency stop, per-account authorization): otherwise the reply is simulated.
* FAQs — questions asked by several different people become content ideas for the Creative Director.
"""
from __future__ import annotations

import json
import re
import statistics
import urllib.parse
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from .adapters.base import HttpRequest, urllib_transport
from .config import CONFIG_DIR, Settings
from .db import DB, now_iso

STOP = set("a an the and or but to of in on at for with is are was were be been it this that these those i you he she "
           "they we me my your his her their our us do does did can could would should will just so what when where who "
           "why how which there here from about up out if not no yes im its it's dont don't get got".split())
PERMISSION_HINT = {
    "instagram": "needs instagram_manage_comments: add it to the Facebook Login configuration and reconnect Meta",
    "facebook": "needs pages_read_user_content: add it to the Facebook Login configuration and reconnect Meta",
}


def config(db: DB | None = None) -> dict:
    from .team_params import get
    cfg = json.loads((CONFIG_DIR / "community.json").read_text())
    for k in ("lookback_days", "faq_min_people", "max_posts_per_account"):
        cfg[k] = get(db, "community", k, cfg[k])
    return cfg


# ---- triage ---------------------------------------------------------------------------------------

def classify(text: str, cfg: dict | None = None) -> dict:
    cfg = cfg or config()
    t = (text or "").strip().lower()
    cat = next((c for c in cfg["categories"] if any(re.search(p, t) for p in c["patterns"])), None)
    pos = sum(t.count(w) for w in cfg["sentiment"]["positive"])
    neg = sum(t.count(w) for w in cfg["sentiment"]["negative"])
    sentiment = "positive" if pos > neg else "negative" if neg > pos else "neutral"
    if cat is None:
        return {"category": "other", "label": "Other", "escalate": False, "priority": 3, "sentiment": sentiment,
                "route": "", "reply": False}
    return {"category": cat["key"], "label": cat["label"], "escalate": bool(cat.get("escalate")),
            "priority": int(cat["priority"]), "sentiment": sentiment, "route": cat.get("route", ""),
            "reply": cat.get("reply", True) and not cat.get("escalate")}


def draft_reply(category: str, brand_id: str, brand_name: str, cfg: dict | None = None) -> tuple[str, list[str]]:
    """(draft, gaps). Gaps name the config links the draft needs; the draft never invents them."""
    cfg = cfg or config()
    tpl = cfg["templates"].get(category)
    if not tpl:
        return "", []
    links = {k: v for k, v in (cfg["links"].get(brand_id) or {}).items() if not k.startswith("_")}
    gaps = [k for k in re.findall(r"{(\w+)}", tpl) if k != "brand" and not links.get(k)]
    text = tpl.format(brand=brand_name, **{k: links.get(k) or f"[add {k} link for {brand_name}]"
                                           for k in re.findall(r"{(\w+)}", tpl) if k != "brand"})
    return text, gaps


def _brand_names() -> dict:
    return {b["brand_id"]: b["name"] for b in json.loads((CONFIG_DIR / "brands.json").read_text())["brands"]}


def triage(db: DB, comment: dict, cfg: dict, names: dict) -> dict:
    c = classify(comment["text"], cfg)
    changes = {"category": c["category"], "sentiment": c["sentiment"], "priority": c["priority"] - (1 if (comment.get("like_count") or 0) >= 25 and c["priority"] > 0 else 0)}
    if c["escalate"]:
        changes.update({"status": "escalated", "escalation": f"{c['label']} → {c['route']}"})
    elif c["category"] in ("spam",):
        changes.update({"status": "flagged_spam"})
    elif c["reply"]:
        text, gaps = draft_reply(c["category"], comment.get("brand_id") or "", names.get(comment.get("brand_id") or "", "PLN"), cfg)
        if text:
            changes.update({"status": "needs_answer" if gaps or c["category"] == "question" else "drafted",
                            "draft_reply": text, "draft_gaps": gaps})
        else:
            changes.update({"status": "no_action"})
    else:
        changes.update({"status": "no_action"})
    return changes


# ---- ingestion ------------------------------------------------------------------------------------

class CommentsError(Exception):
    pass


class MetaComments:
    def __init__(self, transport=None):
        from .oauth import META_VERSION
        self.transport = transport or urllib_transport
        self.base = f"https://graph.facebook.com/{META_VERSION}"

    def _get(self, path: str, params: dict, token: str) -> dict:
        resp = self.transport(HttpRequest("GET", f"{self.base}/{path}?" + urllib.parse.urlencode({**params, "access_token": token}),
                                          {"Accept": "application/json"}, purpose="read comments"))
        if resp.status >= 300:
            err = (resp.body or {}).get("error") or {}
            perm = err.get("code") in (10, 200, 190) or "permission" in str(err.get("message", "")).lower()
            raise CommentsError(("permission: " if perm else "") + f"HTTP {resp.status} {err.get('message') or resp.body}"[:300])
        return resp.body or {}

    def post(self, path: str, params: dict, token: str) -> dict:
        resp = self.transport(HttpRequest("POST", f"{self.base}/{path}", {"Accept": "application/json"},
                                          form_body={**params, "access_token": token}, purpose="reply to comment"))
        if resp.status >= 300:
            err = (resp.body or {}).get("error") or {}
            raise CommentsError(f"HTTP {resp.status} {err.get('message') or resp.body}"[:300])
        return resp.body or {}

    def instagram(self, media_id: str, token: str) -> list[dict]:
        body = self._get(f"{media_id}/comments", {"fields": "id,text,username,timestamp,like_count,"
                                                            "replies{id,text,username,timestamp,like_count}", "limit": 50}, token)
        out = []
        for c in body.get("data") or []:
            out.append({"id": c["id"], "parent": "", "author": c.get("username", ""), "text": c.get("text", ""),
                        "at": c.get("timestamp"), "likes": c.get("like_count") or 0})
            for r in (c.get("replies") or {}).get("data") or []:
                out.append({"id": r["id"], "parent": c["id"], "author": r.get("username", ""), "text": r.get("text", ""),
                            "at": r.get("timestamp"), "likes": r.get("like_count") or 0})
        return out

    def facebook(self, post_id: str, token: str) -> list[dict]:
        body = self._get(f"{post_id}/comments", {"fields": "id,message,from{id,name},created_time,like_count,"
                                                           "comments{id,message,from{id,name},created_time,like_count}",
                                                 "filter": "toplevel", "limit": 50}, token)
        out = []
        for c in body.get("data") or []:
            out.append({"id": c["id"], "parent": "", "author": (c.get("from") or {}).get("name", ""),
                        "author_id": (c.get("from") or {}).get("id", ""), "text": c.get("message", ""),
                        "at": c.get("created_time"), "likes": c.get("like_count") or 0})
            for r in (c.get("comments") or {}).get("data") or []:
                out.append({"id": r["id"], "parent": c["id"], "author": (r.get("from") or {}).get("name", ""),
                            "author_id": (r.get("from") or {}).get("id", ""), "text": r.get("message", ""),
                            "at": r.get("created_time"), "likes": r.get("like_count") or 0})
        return out


def _ours(acc: dict, c: dict) -> bool:
    handles = {(acc.get("username_normalized") or "").lower().lstrip("@"), (acc.get("username_original") or "").lower().lstrip("@")}
    return (c.get("author") or "").lower().lstrip("@") in handles - {""} or (
        bool(acc.get("stable_platform_id")) and c.get("author_id") == acc.get("stable_platform_id"))


def ingest(db: DB, transport=None, now: datetime | None = None, actor: str = "community", token_for=None) -> dict:
    """Fetch comments for recent posts on every connected Instagram / Facebook account."""
    from .oauth import access_token
    cfg, names = config(db), _brand_names()
    now = now or datetime.now(timezone.utc)
    token_for = token_for or (lambda ref: access_token(ref))
    api = MetaComments(transport)
    cutoff = (now - timedelta(days=cfg["lookback_days"])).replace(microsecond=0).isoformat()
    out = {"accounts": {}, "errors": {}, "new": 0}
    for acc in db.all("accounts", "platform IN ('instagram','facebook') AND verification_status='oauth_connected'"):
        ref = acc.get("credential_ref") or ""
        if not ref.startswith("vault:"):
            continue
        try:
            token = token_for(ref[len("vault:"):])
        except (LookupError, PermissionError) as e:
            out["errors"][acc["account_id"]] = str(e)
            continue
        posts = db.query("SELECT platform_post_id, brand_id FROM posts WHERE account_id=? AND published_at >= ? "
                         "ORDER BY published_at DESC", [acc["account_id"], cutoff])[:cfg["max_posts_per_account"]]
        new = seen = 0
        for p in posts:
            try:
                rows = getattr(api, acc["platform"])(p["platform_post_id"], token)
            except CommentsError as e:
                msg = str(e)
                out["errors"][acc["account_id"]] = (PERMISSION_HINT[acc["platform"]] + f" ({msg})") if msg.startswith("permission") else msg
                break
            replied_parents = {r["parent"] for r in rows if r["parent"] and _ours(acc, r)}
            for r in rows:
                cid = f"{acc['platform']}:{r['id']}"
                seen += 1
                existing = db.get("comments", "comment_id", cid)
                if existing:
                    ch = {"like_count": r["likes"], "fetched_at": now_iso()}
                    if r["id"] in replied_parents and existing["status"] not in ("replied", "escalated"):
                        ch["status"] = "replied"
                    db.update("comments", "comment_id", cid, ch)
                    continue
                row = {"comment_id": cid, "account_id": acc["account_id"], "brand_id": p.get("brand_id") or acc["brand_id"],
                       "platform": acc["platform"], "platform_post_id": p["platform_post_id"], "platform_comment_id": r["id"],
                       "parent_id": f"{acc['platform']}:{r['parent']}" if r["parent"] else "", "author": r["author"],
                       "text": r["text"], "created_at": r["at"], "like_count": r["likes"], "fetched_at": now_iso(),
                       "is_ours": _ours(acc, r)}
                if row["is_ours"]:
                    row.update({"status": "own", "category": "own", "priority": 9})
                elif r["id"] in replied_parents:
                    row.update({**triage(db, row, cfg, names), "status": "replied"})
                elif r["parent"]:  # replies to other fans: triage for escalation only
                    t = triage(db, row, cfg, names)
                    row.update(t if t["status"] in ("escalated", "flagged_spam") else {**t, "status": "no_action", "draft_reply": ""})
                else:
                    row.update(triage(db, row, cfg, names))
                db.insert("comments", row)
                new += 1
        out["accounts"][acc["account_id"]] = {"posts": len(posts), "comments_seen": seen, "new": new}
        out["new"] += new
    db.audit(actor, "comments_ingested", "comments", "", {k: v for k, v in out.items() if k != "accounts"} | {"n_accounts": len(out["accounts"])})
    return out


# ---- decisions ------------------------------------------------------------------------------------

def decide(db: DB, comment_id: str, action: str, actor: str, text: str | None = None, note: str = "") -> dict:
    """action: approve | ignore | escalate | reopen. Approving stores the exact text that will be sent."""
    c = db.get("comments", "comment_id", comment_id)
    if not c:
        raise KeyError(comment_id)
    if action == "approve":
        body = (text if text is not None else c.get("draft_reply") or "").strip()
        if not body or "[add " in body:
            raise ValueError("reply is empty or still has a missing link placeholder")
        ch = {"status": "approved", "reply_text": body, "decided_by": actor, "decided_at": now_iso()}
    elif action == "ignore":
        ch = {"status": "ignored", "decided_by": actor, "decided_at": now_iso()}
    elif action == "escalate":
        ch = {"status": "escalated", "escalation": note or "escalated by " + actor, "decided_by": actor, "decided_at": now_iso()}
    elif action == "reopen":
        ch = {"status": "drafted" if c.get("draft_reply") else "needs_answer", "decided_by": actor, "decided_at": now_iso()}
    else:
        raise ValueError(action)
    db.update("comments", "comment_id", comment_id, ch)
    db.audit(actor, f"comment_{action}", "comments", comment_id, {"note": note, "text": ch.get("reply_text")})
    return {**c, **ch}


def send_approved(db: DB, settings: Settings, transport=None, actor: str = "community", token_for=None) -> list[dict]:
    """Send approved replies — live only when every publishing gate allows it, otherwise simulated."""
    from .accounts import publish_eligibility
    from .oauth import access_token
    from .workflow import kill_switch_on
    token_for = token_for or (lambda ref: access_token(ref))
    api, out, ks = MetaComments(transport), [], kill_switch_on(db)
    for c in db.all("comments", "status='approved'", order="priority, created_at"):
        acc = db.get("accounts", "account_id", c["account_id"])
        ok, reasons = publish_eligibility(acc, settings, ks)
        if not ok:
            db.update("comments", "comment_id", c["comment_id"], {"status": "simulated_reply", "replied_at": now_iso(),
                                                                 "send_result": {"mode": "simulated", "why_not_live": reasons}})
            out.append({"comment_id": c["comment_id"], "mode": "simulated", "why_not_live": reasons})
            continue
        path = f"{c['platform_comment_id']}/replies" if c["platform"] == "instagram" else f"{c['platform_comment_id']}/comments"
        try:
            body = api.post(path, {"message": c["reply_text"]}, token_for(acc["credential_ref"][len("vault:"):]))
            db.update("comments", "comment_id", c["comment_id"], {"status": "replied", "replied_at": now_iso(),
                                                                 "send_result": {"mode": "live", "id": body.get("id")}})
            out.append({"comment_id": c["comment_id"], "mode": "live", "id": body.get("id")})
        except (CommentsError, LookupError) as e:
            db.update("comments", "comment_id", c["comment_id"], {"send_result": {"mode": "live", "error": str(e)}})
            out.append({"comment_id": c["comment_id"], "mode": "live", "error": str(e)})
    if out:
        db.audit(actor, "comment_replies_sent", "comments", "", {"n": len(out), "live": sum(1 for o in out if o["mode"] == "live")})
    return out


# ---- reporting ------------------------------------------------------------------------------------

def _words(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9']+", (text or "").lower()) if w not in STOP and len(w) > 2]


def faqs(db: DB, days: int = 30, min_people: int | None = None, now: datetime | None = None) -> list[dict]:
    """Questions several different people asked, grouped by brand and their most distinctive shared word pair."""
    min_people = min_people or config(db)["faq_min_people"]
    since = ((now or datetime.now(timezone.utc)) - timedelta(days=days)).replace(microsecond=0).isoformat()
    qs = db.query("SELECT comment_id, brand_id, author, text, category FROM comments WHERE created_at >= ? AND is_ours=? "
                  "AND category IN ('question','where_to_watch','athlete_interest')", [since, False])
    groups: dict[tuple, dict] = {}
    for q in qs:
        ws = _words(q["text"])
        keys = {" ".join(sorted(p)) for p in zip(ws, ws[1:])} | set(ws)
        for k in keys:
            g = groups.setdefault((q["brand_id"], q["category"], k), {"people": set(), "examples": []})
            g["people"].add(q["author"])
            if len(g["examples"]) < 3:
                g["examples"].append(q["text"])
    out = []
    for (brand, cat, key), g in sorted(groups.items(), key=lambda kv: (-len(kv[1]["people"]), -len(kv[0][2]))):
        if len(g["people"]) < min_people:
            continue
        # one entry per cluster of askers: skip a topic mostly asked by people an earlier topic already covers
        if any(o["brand_id"] == brand and o["category"] == cat and len(g["people"] & o["_people"]) >= 0.5 * len(g["people"])
               for o in out):
            continue
        out.append({"brand_id": brand, "category": cat, "topic": key, "people": len(g["people"]), "examples": g["examples"],
                    "_people": g["people"],
                    "idea": f"Answer it on camera: a short post on \"{g['examples'][0][:80]}\" — {len(g['people'])} people asked in {days} days."})
    for o in out:
        o.pop("_people")
    return out[:15]


def summary(db: DB, days: int = 7, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(days=days)).replace(microsecond=0).isoformat()
    rows = db.query("SELECT * FROM comments WHERE created_at >= ? AND is_ours=?", [since, False])
    by_status, by_cat, sent = defaultdict(int), defaultdict(int), defaultdict(lambda: {"n": 0, "neg": 0})
    waits = []
    for r in rows:
        by_status[r["status"]] += 1
        by_cat[r["category"]] += 1
        sent[r["account_id"]]["n"] += 1
        sent[r["account_id"]]["neg"] += r["sentiment"] == "negative"
        if r.get("replied_at") and r.get("created_at"):
            try:
                a = datetime.fromisoformat(str(r["created_at"]).replace("Z", "+00:00").replace("+0000", "+00:00"))
                b = datetime.fromisoformat(str(r["replied_at"]))
                waits.append((b - a).total_seconds() / 3600)
            except ValueError:
                pass
    return {"days": days, "comments": len(rows), "by_status": dict(by_status), "by_category": dict(by_cat),
            "open_escalations": by_status.get("escalated", 0), "awaiting_approval": by_status.get("drafted", 0) + by_status.get("needs_answer", 0),
            "median_reply_hours": round(statistics.median(waits), 1) if waits else None,
            "negative_share": {a: round(v["neg"] / v["n"], 2) for a, v in sent.items() if v["n"] >= 10}}


def queue(db: DB, status: tuple[str, ...] = ("escalated", "needs_answer", "drafted", "approved"), limit: int = 200) -> list[dict]:
    marks = ",".join("?" for _ in status)
    return db.query(f"SELECT * FROM comments WHERE status IN ({marks}) ORDER BY priority, like_count DESC, created_at DESC",
                    list(status))[:limit]


def refresh(db: DB, settings: Settings, transport=None, actor: str = "community") -> dict:
    r = ingest(db, transport, actor=actor)
    r["faqs"] = faqs(db)
    db.kv_set("community:faqs", json.dumps(r["faqs"]), actor)
    return r
