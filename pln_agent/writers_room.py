"""Writers' room: Claude writes the copy for every new draft on a worked account.

For each post: five drafts, each from a different angle (stakes, the absurd premise, POV, debate, payoff tease), written
only from what the clip shows (the Scout's moment, the clip's transcript and machine caption, the annotated moments).
A second pass — the critic — scores every draft on hook strength, specificity, accuracy to the clip and payoff, and
rejects any that invents a fact, names an athlete, uses a number, or sells. The best draft goes on the post (hook,
caption, CTA and the burned-in opening text) and the post is rendered. Below the quality bar the post is sent back
(needs_fix) instead of reaching the queue. All five drafts and their scores stay on the post page, and a reviewer can
switch to another with one click.

Runs on the task runner (the CodeBuild job), never on a page request.
"""
from __future__ import annotations

import json
import re
import string

from .db import DB, now_iso

ANGLES = ["stakes: what is about to be won or lost", "the absurd premise of the sport itself",
          "POV: put the viewer in the seat", "debate: a question people will argue about in the comments",
          "payoff tease: promise the moment without giving it away"]
BAR = 6.5  # average critic score (0-10) a draft needs to go out for review
VOICE_FALLBACK = "confident, funny, specific sports social voice"

WRITER = """You write Instagram Reel / TikTok / YouTube Shorts copy for sports clips. Write FIVE drafts, one per angle below.
Rules for every draft:
- hook: 2 to 5 words shown big on screen in the first seconds. Specific to THIS clip, no brand names, no digits or numbers.
- caption: one or two short sentences that set up the moment, then one question that invites comments. No digits,
  no hashtags, no emojis, never name an athlete, no betting or "go bet" language.
- cta: one short line (share it / follow for the next one / tag someone).
- Use only facts in the clip description. Do not invent results, scores, times or quotes.
Angles: {angles}
Reply with JSON only: {{"drafts": [{{"angle": "...", "hook": "...", "caption": "...", "cta": "..."}}]}}"""

CRITIC = """You are the critic in a sports social writers' room. Score each draft 0-10 on: hook (stops the scroll),
specific (about THIS clip, not any clip), accurate (only facts in the clip description; 0 if anything is invented),
payoff (makes people watch to the end or comment). Disqualify (dq=true) any draft with a number, an athlete's name,
a brand name in the hook, or betting language.
Clip description: {facts}
Drafts: {drafts}
Reply with JSON only: {{"scores": [{{"i": 0, "hook": 0, "specific": 0, "accurate": 0, "payoff": 0, "dq": false, "note": "..."}}]}}"""


def _clean(t: str) -> str:
    return re.sub(r"[*_#`]+", "", t or "").strip()


def facts_for(db: DB, v: dict) -> dict:
    """What the writers may say: only what the clip and its annotations show."""
    a = db.get("assets", "asset_id", v["asset_id"]) or {}
    clip = (a.get("probe") or {}).get("ops_clip") or {}
    m = db.one("SELECT caption, content_type FROM scout_moments WHERE clip_id=?", [clip.get("id") or ""]) if clip.get("id") else None
    return {"moment": (m or {}).get("caption"), "type": (m or {}).get("content_type"),
            "title": clip.get("source_title"), "machine_caption": clip.get("machine_caption"),
            "transcript": (clip.get("transcript") or "")[:700],
            "moments": [{k: x.get(k) for k in ("kind", "description")} for x in (a.get("moments") or [])][:6]}


def _voice(brand_id: str, account_id: str) -> str:
    from .accounts import load_brands
    brands = {b["brand_id"]: b for b in load_brands()}
    if account_id.startswith("pln."):
        b = brands.get("pln") or {}
        return ((b.get("voice") or {}).get("tone") or VOICE_FALLBACK) + "; the audience has never heard of this sport"
    return ((brands.get(brand_id) or {}).get("voice") or {}).get("tone") or VOICE_FALLBACK


def _score(s: dict) -> float:
    if s.get("dq") or not s.get("accurate"):
        return 0.0
    return round((float(s.get("hook", 0)) + float(s.get("specific", 0)) + float(s.get("accurate", 0)) + float(s.get("payoff", 0))) / 4, 2)


def write(db: DB, vid: str, actor: str = "writers-room", llm=None) -> dict:
    """Run the room for one post and apply the winner. Returns {status, pick, drafts}."""
    from . import team
    from .workflow import edit_variant, refresh_validation
    call = llm or team._llm
    v = db.get("variants", "variant_id", vid)
    facts = facts_for(db, v)
    raw = call(WRITER.format(angles="; ".join(ANGLES)),
               [{"role": "user", "content": f"Voice: {_voice(v['brand_id'], v['account_id'])}\nPlatform: {v['platform']}\n"
                                            f"Clip description: {json.dumps(facts)}"}], 1400)
    drafts = [{k: _clean(d.get(k, "")) for k in ("angle", "hook", "caption", "cta")} for d in (team._json(raw).get("drafts") or [])][:5]
    if not drafts:
        raise ValueError("the writers returned no drafts")
    crit = team._json(call("You score social copy. JSON only.",
                           [{"role": "user", "content": CRITIC.format(facts=json.dumps(facts), drafts=json.dumps(drafts))}], 900))
    for s in crit.get("scores") or []:
        i = int(s.get("i", -1))
        if 0 <= i < len(drafts):
            drafts[i]["scores"] = {k: s.get(k) for k in ("hook", "specific", "accurate", "payoff", "dq")}
            drafts[i]["note"] = s.get("note", "")
            drafts[i]["score"] = _score(s)
    for d in drafts:
        d.setdefault("score", 0.0)
        if any(ch.isdigit() for ch in d["hook"] + d["caption"]):  # the rule the critic must enforce, checked again here
            d["score"], d["note"] = 0.0, (d.get("note", "") + " — has a number").strip(" —")
    pick = max(range(len(drafts)), key=lambda i: drafts[i]["score"])
    room = {"status": "done", "drafts": drafts, "pick": pick, "bar": BAR, "at": now_iso()}
    apply_draft(db, vid, drafts[pick], room, actor)
    if drafts[pick]["score"] < BAR:
        val = dict(db.get("variants", "variant_id", vid).get("validation") or {})
        val["errors"] = list(val.get("errors") or []) + [f"writers' room: no draft reached the quality bar ({drafts[pick]['score']} < {BAR})"]
        db.update("variants", "variant_id", vid, {"state": "needs_fix", "validation": val, "writers_status": "below_bar",
                                                  "updated_at": now_iso()})
        room["status"] = "below_bar"
    else:
        refresh_validation(db, vid)
    db.audit(actor, "writers_room", "variants", vid, {"pick": pick, "score": drafts[pick]["score"], "status": room["status"]})
    return room


def apply_draft(db: DB, vid: str, d: dict, room: dict | None, actor: str) -> None:
    """Put a draft on the post: copy, the burned-in opening text, and a fresh render."""
    from .workflow import edit_variant
    edit_variant(db, vid, {"hook": string.capwords(d["hook"].lower()), "caption": d["caption"], "cta": d["cta"]}, actor)
    v = db.get("variants", "variant_id", vid)
    ot = [dict(o) for o in (v.get("onscreen_text") or []) if not (o.get("position") or "").endswith("small")]
    if ot:
        ot[0]["text"] = d["hook"].upper()
    else:
        ot = [{"start": 0.0, "end": 2.5, "text": d["hook"].upper(), "position": "top"}]
    t = dict(v.get("treatment") or {})
    t["overlay"] = {**(t.get("overlay") or {}), "hook_custom": True}
    if room is not None:
        t["writers_room"] = room
    db.update("variants", "variant_id", vid, {"onscreen_text": ot, "treatment": t, "writers_status": (room or {}).get("status", "done"),
                                              "render_status": "queued", "updated_at": now_iso()})


def switch(db: DB, vid: str, i: int, actor: str) -> dict:
    """A reviewer picks another of the room's drafts."""
    v = db.get("variants", "variant_id", vid)
    room = dict((v.get("treatment") or {}).get("writers_room") or {})
    drafts = room.get("drafts") or []
    if not 0 <= i < len(drafts):
        raise ValueError("no such draft")
    room["pick"], room["status"] = i, "done"
    apply_draft(db, vid, drafts[i], room, actor)
    from .workflow import refresh_validation
    if v["state"] == "needs_fix":
        db.update("variants", "variant_id", vid, {"state": "in_review"})
    refresh_validation(db, vid)
    return {"pick": i, "hook": drafts[i]["hook"]}


def run_pending(db: DB, limit: int = 8, actor: str = "writers-room", llm=None) -> list[dict]:
    out = []
    for v in db.all("variants", "writers_status='pending' AND state IN ('in_review','needs_fix') AND COALESCE(post_type,'')=''", order="score DESC", limit=limit):
        try:
            r = write(db, v["variant_id"], actor, llm)
            out.append({"variant_id": v["variant_id"], "status": r["status"], "score": r["drafts"][r["pick"]]["score"]})
        except Exception as e:  # noqa: BLE001 — keep the template copy, try once only
            db.update("variants", "variant_id", v["variant_id"], {"writers_status": "failed", "updated_at": now_iso()})
            db.audit(actor, "writers_room_failed", "variants", v["variant_id"], {"error": str(e)[:300]})
            out.append({"variant_id": v["variant_id"], "status": "failed", "error": str(e)[:120]})
    return out
