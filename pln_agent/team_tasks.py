"""Team tasks: an adopted challenge action becomes a tracked task that the system actually works on.

Adopting used to stop at a directive (guidance the agent reads in conversation). Now every adopted action also
becomes a task with an owner, a status and a reason. The worker advances open tasks every minute, and a task's
status is read from what really happened — renders finished, drafts created, posts scheduled — never from what
an agent says about itself.

Kinds (picked from the owner and the action's wording):
* render    — Video: promote the named clips, ask MediaConvert (ops VerticalizeFn) for a subject-tracked 9:16 of
               the recommended cut, render text/captions on it and QC. Instagram posts on the pilot accounts.
* creative  — Creative: drafts exist for the named clips; shows each draft's caption facts against the rules.
* decision  — Scout: answers which brand each named clip belongs to and how much unused supply a brand has.
* schedule  — Publishing: pins rendered, QC-passed posts to the run sheet at the action's times. You lock them
               there (approve + schedule); nothing is approved on your behalf.
* post_on   — Publishing: one post on a named account today (e.g. the WPL bridge); covered by a schedule pin
               when there is one.
* measure   — Experimentation: waits for this challenge's posts to publish, then for their metrics.
* manual    — anything else: a checklist item you mark done.

Nothing here publishes: scheduled posts go through the publisher, which simulates unless every live gate is open.
"""
from __future__ import annotations

import re
import threading
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .db import DB, now_iso

OPEN = ("todo", "in_progress", "waiting", "needs_you", "blocked")
PILOT_PLATFORM = "instagram"
MEDIACONVERT_TIMEOUT_MIN = 30
BRAND_WORDS = [("putt tour", "putt_tour"), ("putttour", "putt_tour"), ("carjitsu", "carjitsu"), ("wpl", "world_putting_league"),
               ("world putting league", "world_putting_league"), ("slapfight", "slapfight"), ("str33t", "str33t"),
               ("pln", "pln"), ("pro league network", "pln")]
STOP = {"post", "posts", "pln", "carjitsu", "putt", "tour", "wpl", "all", "the", "via", "render", "today", "no", "short", "medium",
        "instagram", "mediaconvert", "qc", "cta", "if", "and", "or", "one", "round", "black", "max", "in", "at", "on", "for"}


# ---- creation ----------------------------------------------------------------------------------------

def classify(owner: str, text: str) -> str:
    t = text.lower()
    if owner == "video":
        return "render"
    if owner == "scout":
        return "decision"
    if owner == "creative":
        return "creative"
    if re.search(r"\brender", t):
        return "render"
    if owner == "publishing" and len(_times(text)) >= 2:
        return "schedule"
    if owner == "publishing" and re.search(r"\bpost (one|a|1)\b", t):
        return "post_on"
    if re.search(r"\b(collect|measure|baseline|metrics|results)\b", t):
        return "measure"
    return "manual"


def create(db: DB, thread_id: str, index: int, act: dict, actor: str) -> dict:
    existing = db.one("SELECT * FROM team_tasks WHERE thread_id=? AND action_index=?", [thread_id, index])
    if existing:
        return existing
    owner = act.get("owner") or "team"
    text = act.get("action") or ""
    task = {"task_id": f"task-{uuid.uuid4().hex[:8]}", "thread_id": thread_id, "action_index": index, "owner": owner,
            "kind": classify(owner, text), "title": text[:2000], "priority": act.get("priority") or "", "status": "todo",
            "progress": "Queued — picked up on the next worker pass (about a minute).", "blocked_reason": "",
            "detail": {}, "created_by": actor, "created_at": now_iso(), "updated_at": now_iso()}
    db.insert("team_tasks", task)
    db.audit(actor, "team_task_created", "team_tasks", task["task_id"], {"kind": task["kind"], "owner": owner})
    return task


def ensure_for_adopted(db: DB) -> int:
    """Backfill: actions adopted before tasks existed get their task."""
    made = 0
    for t in db.all("team_threads", "kind='challenge'"):
        r = t.get("result") or {}
        for i in r.get("adopted") or []:
            acts = r.get("actions") or []
            if 0 <= i < len(acts) and not db.one("SELECT task_id FROM team_tasks WHERE thread_id=? AND action_index=?", [t["thread_id"], i]):
                create(db, t["thread_id"], i, acts[i], t.get("created_by") or "team")
                made += 1
    return made


def tasks(db: DB, thread_id: str = "", owner: str = "", open_only: bool = False) -> list[dict]:
    where, params = ["1=1"], []
    if thread_id:
        where.append("thread_id=?"), params.append(thread_id)
    if owner:
        where.append("owner=?"), params.append(owner)
    rows = db.all("team_tasks", " AND ".join(where), params, order="created_at")
    rows.sort(key=lambda t: (t["thread_id"], t["action_index"]))
    return [t for t in rows if t["status"] in OPEN] if open_only else rows


def mark(db: DB, task_id: str, status: str, actor: str, note: str = "") -> None:
    if status not in ("done", "blocked", "todo"):
        raise ValueError("status must be done, blocked or todo")
    t = db.get("team_tasks", "task_id", task_id)
    if not t:
        raise KeyError(task_id)
    d = dict(t.get("detail") or {})
    if status == "todo":  # retry: forget the MediaConvert/render failure so the executor starts that step again
        for x in d.get("targets") or []:
            if x.get("state") in ("blocked", "failed"):
                x["state"], x["note"], x["retry"] = "", "", True
    d["manual"] = status == "done"
    _set(db, t, status, note or {"done": "Marked done", "blocked": "Marked blocked", "todo": "Retrying"}[status], d,
         note if status == "blocked" else "")
    db.audit(actor, "team_task_marked", "team_tasks", task_id, {"status": status, "note": note})


def _set(db: DB, t: dict, status: str, progress: str, detail: dict, blocked: str = "") -> None:
    changes = {"status": status, "progress": progress[:600], "detail": detail, "blocked_reason": blocked[:600]}
    if any(t.get(k) != v for k, v in changes.items()):
        db.update("team_tasks", "task_id", t["task_id"], {**changes, "updated_at": now_iso()})
        t.update(changes)


# ---- resolving what an action names ------------------------------------------------------------------

def _names(caption: str) -> list[str]:
    words = re.findall(r"[A-Za-z][A-Za-z'’-]+", caption or "")[:3]
    return [w for w in words if w[0].isupper() and w.lower() not in STOP and len(w) >= 3]


def resolve_clips(db: DB, text: str, limit: int = 300) -> list[dict]:
    """Scout moments an action names ("Davidson seatbelt choke 89.5"), in the order the action lists them.
    When a score follows the name it must be that moment's score; otherwise the best match near the name wins."""
    low = text.lower()
    rows = db.memo(f"scout_top:{limit}", lambda: db.all("scout_moments", "status <> 'dismissed'", order="potential DESC", limit=limit))
    hits: dict[str, tuple[int, float, dict]] = {}
    for m in rows:
        pot = f"{float(m.get('potential') or 0):.1f}"
        cap_words = set(re.findall(r"[a-z]{3,}", (m.get("caption") or "").lower()))
        for name in _names(m.get("caption") or ""):
            x = re.search(rf"\b{re.escape(name.lower())}\b", low)
            if not x:
                continue
            window = low[max(0, x.start() - 30):x.end() + 40]
            nxt = re.search(r"\b\d{2}\.\d\b", text[x.end():x.end() + 40])
            if nxt and nxt.group(0) != pot:
                continue  # the action gives this name a different score: a different moment
            rank = (2 if nxt else 0) + sum(1 for w in cap_words if re.search(rf"\b{w}\b", window)) / 100
            key = name.lower()
            if key not in hits or rank > hits[key][1]:
                hits[key] = (x.start(), rank, m)
    seen, out = set(), []
    for _, _, m in sorted(hits.values(), key=lambda h: h[0]):
        if m["clip_id"] in seen:
            continue
        seen.add(m["clip_id"])
        out.append({"clip_id": m["clip_id"], "caption": m.get("caption", ""), "potential": m.get("potential"),
                    "brand_id": m.get("brand_id", ""), "status": m.get("status", "")})
    return out


def _thread_clips(db: DB, t: dict) -> list[dict]:
    """The clips an action names, preferring the exact moments the same challenge's render task resolved
    (a name without a score, e.g. "Savard ace", can match several moments)."""
    own = resolve_clips(db, t["title"])
    rendered = [c for x in tasks(db, t["thread_id"]) if x["kind"] == "render" for c in (x.get("detail") or {}).get("clips") or []]
    out = []
    for c in own:
        names = {n.lower() for n in _names(c["caption"])}
        same = next((r for r in rendered if names & {n.lower() for n in _names(r["caption"])}), None)
        pick = same or c
        if pick["clip_id"] not in {o["clip_id"] for o in out}:
            out.append(pick)
    return out


def _times(text: str) -> list[tuple[int, str, int, int]]:
    """(position, preceding words, hour24, minute) for each clock time in the text."""
    out, last = [], 0
    for m in re.finditer(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", text, flags=re.I):
        h, mi = int(m.group(1)) % 12, int(m.group(2) or 0)
        if m.group(3).lower() == "pm":
            h += 12
        out.append((m.start(), text[last:m.start()], h, mi))
        last = m.end()
    return out


def _brands_in(fragment: str) -> list[str]:
    low, found = fragment.lower(), []
    for word, bid in BRAND_WORDS:
        for x in re.finditer(rf"\b{re.escape(word)}\b", low):
            found.append((x.start(), bid))
    found.sort()
    out = []
    for _, b in found:
        if b not in out:
            out.append(b)
    return out


def parse_slots(text: str) -> list[dict]:
    slots = []
    for i, (_, before, h, mi) in enumerate(_times(text)):
        frag = re.split(r"\bpost\s*\d+\b", before, flags=re.I)[-1] if re.search(r"\bpost\s*\d+\b", before, re.I) else before[-80:]
        brands = _brands_in(frag)
        if brands:
            slots.append({"n": i + 1, "brands": brands, "hour": h, "minute": mi, "label": frag.strip(" ,.:;")[:80]})
    return slots


def _pilot_account(db: DB, brand_id: str) -> dict | None:
    accs = [a for a in db.all("accounts", "brand_id=? AND platform=?", [brand_id, PILOT_PLATFORM]) if not a.get("hard_block_reason")]
    accs.sort(key=lambda a: 0 if a.get("role") in ("canonical", "legacy_bridge") else 1)
    return accs[0] if accs else None


# ---- executors ---------------------------------------------------------------------------------------

class Hooks:
    """The side-effecting steps, swappable in tests."""

    def __init__(self, settings, ops: DB | None):
        self.settings, self.ops = settings, ops

    def promote(self, db: DB, clip_ids: list[str]) -> dict:
        from .scout import promote
        if not self.ops:
            raise RuntimeError("the ops database isn't configured — can't import clips")
        return promote(db, self.ops, clip_ids, self.settings, actor="team-tasks")

    def regenerate(self, db: DB, asset_id: str) -> None:
        from .editorial import generate_pack
        generate_pack(db, asset_id, self.settings, actor="team-tasks")

    def request_cut(self, db: DB, variant_id: str, label: str) -> dict:
        from .ops_vertical import request_cuts
        return request_cuts(db, variant_id, "team-tasks", only=[label])

    def render(self, db: DB, variant_id: str) -> dict:
        from .media import render_variant
        return render_variant(db, variant_id, self.settings, actor="team-tasks")


def _asset_for(db: DB, clip_id: str) -> dict | None:
    from .ops_sources import clip_asset_id
    return db.get("assets", "asset_id", clip_asset_id(clip_id))


def _ensure_drafts(db: DB, hooks: Hooks, clips: list[dict]) -> list[str]:
    """Promote named clips that aren't assets with drafts yet. Returns problems."""
    todo = []
    for c in clips:
        a = _asset_for(db, c["clip_id"])
        if not a or not db.query("SELECT variant_id FROM variants WHERE asset_id=?", [a["asset_id"]]):
            todo.append(c["clip_id"])
    # drafts made before the Creative Director existed have no cut plan: regenerate them (generate_pack keeps
    # approved / scheduled posts as they are)
    for c in clips:
        if c["clip_id"] in todo:
            continue
        stale = [v for v in _pilot_variants(db, c["clip_id"]) if not (v.get("treatment") or {}).get("duration_variants")
                 and v["state"] in ("in_review", "needs_fix")]
        if stale:
            hooks.regenerate(db, stale[0]["asset_id"])
    if not todo:
        return []
    r = hooks.promote(db, todo)
    return [str(p) for p in r.get("packs", []) if ":" in str(p) and not str(p).startswith("pack-")] + \
        [f"{s}" for s in r.get("skipped", [])]


def _pilot_variants(db: DB, clip_id: str) -> list[dict]:
    a = _asset_for(db, clip_id)
    if not a:
        return []
    pilot = {x["account_id"] for b in ("pln", "carjitsu", "putt_tour", "world_putting_league", a["brand_id"])
             if (x := _pilot_account(db, b))}
    return [v for v in db.all("variants", "asset_id=?", [a["asset_id"]]) if v["account_id"] in pilot
            and v["state"] not in ("rejected", "cancelled")]


def _render_step(db: DB, hooks: Hooks, v: dict, target: dict) -> None:
    """Advance one variant: MediaConvert 9:16 of the recommended cut → switch the post to that cut → render + QC."""
    from .creative_director import use_cut
    t = v.get("treatment") or {}
    rec = t.get("recommended_variant")
    cut = next((c for c in t.get("duration_variants") or [] if c["label"] == rec), None)
    target["cut"] = rec
    if v.get("render_status") == "rendered" and (v.get("edl") or {}).get("variant_label") == rec:
        qc = v.get("render_qc") or {}
        if qc.get("passed"):
            target.update(state="done", note="rendered from the MediaConvert 9:16 and QC passed")
        else:
            target.update(state="blocked", note="QC failed: " + "; ".join((qc.get("errors") or [])[:3]))
        return
    if not cut or not cut.get("available"):
        target.update(state="blocked", note="no usable cut for this clip (" + ((cut or {}).get("why") or "no payoff moment") + ")")
        return
    ov = cut.get("ops_vertical") or {}
    if target.pop("retry", False) and ov.get("status") == "requested":
        ov = {}
    if ov.get("status") != "ready":
        if ov.get("status") == "requested":
            asked = datetime.fromisoformat(ov["requested_at"])
            if datetime.now(timezone.utc) - asked > timedelta(minutes=MEDIACONVERT_TIMEOUT_MIN):
                target.update(state="blocked", note=f"MediaConvert hasn't returned the 9:16 after {MEDIACONVERT_TIMEOUT_MIN} min — Retry")
            else:
                target.update(state="working", note=f"MediaConvert is cropping cut {rec} (asked {asked:%H:%M} UTC)")
            return
        r = hooks.request_cut(db, v["variant_id"], rec)
        if r.get("requested"):
            target.update(state="working", note=f"asked MediaConvert for a subject-tracked 9:16 of cut {rec}")
        else:
            target.update(state="blocked", note=r.get("skipped") or "MediaConvert request wasn't sent")
        return
    if v["state"] in ("scheduled", "publishing", "published", "simulated"):
        target.update(state="blocked", note=f"post is already {v['state']} — not re-cut")
        return
    if (v.get("edl") or {}).get("variant_label") != rec:
        use_cut(db, v["variant_id"], rec, "team-tasks")
    r = hooks.render(db, v["variant_id"])
    qc = r.get("qc") or {}
    if r.get("status") == "rendered" and qc.get("passed"):
        target.update(state="done", note="rendered from the MediaConvert 9:16 and QC passed")
    elif r.get("status") == "rendered":
        target.update(state="blocked", note="QC failed: " + "; ".join((qc.get("errors") or [])[:3]))
    else:
        target.update(state="blocked", note=f"render {r.get('status')}: {(r.get('detail') or '')[:160]}")


def run_render(db: DB, t: dict, hooks: Hooks) -> None:
    d = dict(t.get("detail") or {})
    clips = d.get("clips") or resolve_clips(db, t["title"])
    d["clips"] = clips
    if not clips:
        return _set(db, t, "blocked", "Couldn't match any clip in the action to the scout's moments.", d,
                    "no named clips found — edit the action or render from the Scout page")
    problems = _ensure_drafts(db, hooks, clips)
    prev = {x["variant_id"]: x for x in d.get("targets") or []}
    targets = []
    for c in clips:
        vs = _pilot_variants(db, c["clip_id"])
        if not vs:
            targets.append({"variant_id": "", "clip_id": c["clip_id"], "account_id": "", "state": "blocked",
                            "note": "no Instagram draft for this clip (rights or routing) " + "; ".join(problems)[:200]})
            continue
        for v in vs:
            x = dict(prev.get(v["variant_id"]) or {"variant_id": v["variant_id"], "clip_id": c["clip_id"], "account_id": v["account_id"]})
            if x.get("state") != "done" or v.get("render_status") != "rendered":
                try:
                    _render_step(db, hooks, db.get("variants", "variant_id", v["variant_id"]), x)
                except Exception as e:  # noqa: BLE001 — one post's failure is reported, the rest continue
                    x.update(state="failed", note=str(e)[:200])
            targets.append(x)
    d["targets"] = targets
    n_done = sum(x["state"] == "done" for x in targets)
    bad = [x for x in targets if x["state"] in ("blocked", "failed")]
    summary = f"{n_done} of {len(targets)} Instagram posts rendered and QC-passed across {len(clips)} clips"
    if n_done == len(targets):
        _set(db, t, "done", summary + ".", d)
    elif bad and not any(x["state"] == "working" for x in targets):
        _set(db, t, "blocked", summary + f"; {len(bad)} need attention.", d, bad[0]["note"])
    else:
        _set(db, t, "in_progress", summary + "; the rest are with MediaConvert or rendering.", d, "")


def _caption_facts(v: dict) -> dict:
    cap = v.get("caption") or ""
    tags = v.get("hashtags") or re.findall(r"#\w+", cap)
    body = re.sub(r"#\w+", "", cap)
    return {"sentences": len([s for s in re.split(r"[.!?]+", body) if s.strip()]), "hashtags": len(tags),
            "question": "?" in cap or "?" in (v.get("cta") or "")}


def run_creative(db: DB, t: dict, hooks: Hooks) -> None:
    d = dict(t.get("detail") or {})
    clips = _thread_clips(db, t)
    d["clips"] = clips
    if not clips:
        return _set(db, t, "blocked", "Couldn't match any clip in the action to the scout's moments.", d, "no named clips found")
    problems = _ensure_drafts(db, hooks, clips)
    drafts = []
    for c in clips:
        for v in _pilot_variants(db, c["clip_id"]):
            drafts.append({"variant_id": v["variant_id"], "clip_id": c["clip_id"], "account_id": v["account_id"], "state": v["state"],
                           **_caption_facts(v)})
    d["drafts"] = drafts
    covered = {x["clip_id"] for x in drafts}
    missing = [c for c in clips if c["clip_id"] not in covered]
    if missing:
        _set(db, t, "blocked", f"Drafts for {len(covered)} of {len(clips)} clips.", d,
             "no Instagram draft for " + ", ".join(m["caption"][:40] for m in missing) + ("; " + "; ".join(problems) if problems else ""))
    else:
        _set(db, t, "needs_you", f"{len(drafts)} drafts ready. Check each caption against the action's rules before you lock it "
             "(sentence and hashtag counts are shown per draft).", d)


def run_decision(db: DB, t: dict, hooks: Hooks) -> None:
    from .accounts import load_brands
    names = {b["brand_id"]: b["name"] for b in load_brands()}
    d = dict(t.get("detail") or {})
    clips = _thread_clips(db, t)
    d["clips"] = clips
    lines = [f"“{c['caption']}” ({c['potential']}) belongs to {names.get(c['brand_id'], c['brand_id'])}." for c in clips]
    for bid in _brands_in(t["title"]):
        n = db.one("SELECT COUNT(*) AS n FROM scout_moments WHERE brand_id=? AND status='candidate' AND potential >= 80", [bid])["n"]
        lines.append(f"{names.get(bid, bid)}: {n} unused moment(s) scoring 80+ of its own.")
        if bid == "pln" and n == 0:
            lines.append("So PLN has no clip of its own today — its slot uses a PLN amplification post of another brand's clip "
                         "if one exists, otherwise falls back to the next brand named for that slot.")
    d["answer"] = lines
    _set(db, t, "done" if clips or lines else "blocked", " ".join(lines) or "Nothing to answer.", d, "" if lines else "no clips named")


def _thread_render_targets(db: DB, thread_id: str) -> list[dict]:
    out = []
    for x in tasks(db, thread_id):
        if x["kind"] == "render":
            out += [y for y in (x.get("detail") or {}).get("targets") or [] if y.get("variant_id")]
    return out


def _slot_time(tz: ZoneInfo, hour: int, minute: int, after: datetime | None, now: datetime) -> tuple[datetime, str]:
    today = now.astimezone(tz).date()
    want = datetime(today.year, today.month, today.day, hour, minute, tzinfo=tz)
    floor = max(now + timedelta(minutes=30), (after + timedelta(minutes=60)) if after else now)
    if want >= floor:
        return want, ""
    moved = floor.astimezone(tz)
    moved = moved.replace(minute=(moved.minute // 15 + 1) * 15 % 60, second=0, microsecond=0) + \
        (timedelta(hours=1) if moved.minute >= 45 else timedelta())
    if moved.date() != today or moved.hour >= 23:
        return want + timedelta(days=1), f"{hour % 12 or 12}:{minute:02d} had passed and today is full — moved to tomorrow"
    return moved, f"{hour % 12 or 12}:{minute:02d} had passed — moved to {moved:%-I:%M %p}"


def run_schedule(db: DB, t: dict, hooks: Hooks, settings) -> None:
    tz, now = ZoneInfo(settings.timezone), datetime.now(timezone.utc)
    d = dict(t.get("detail") or {})
    slots = d.get("slots") or parse_slots(t["title"])
    from .workflow import refresh_validation
    ready = []
    for x in _thread_render_targets(db, t["thread_id"]):
        v = db.get("variants", "variant_id", x["variant_id"])
        if v and x.get("state") == "done":
            if v["state"] == "needs_fix":  # validated on the old edit before the cut changed
                refresh_validation(db, v["variant_id"])
                v = db.get("variants", "variant_id", v["variant_id"])
            ready.append(v)
    used = {s.get("variant_id") for s in slots if s.get("variant_id")}
    renders_open = any(x["kind"] == "render" and x["status"] in OPEN for x in tasks(db, t["thread_id"]))
    last: dict[str, datetime] = {}  # spacing applies per account: different accounts can post minutes apart
    for s in slots:
        if s.get("slot_at") and s.get("account_id"):
            last[s["account_id"]] = max(last.get(s["account_id"], datetime.min.replace(tzinfo=timezone.utc)),
                                        datetime.fromisoformat(s["slot_at"]))
        if s.get("variant_id"):
            v = db.get("variants", "variant_id", s["variant_id"]) or {}
            if v.get("state") == "needs_fix":
                refresh_validation(db, s["variant_id"])
                v = db.get("variants", "variant_id", s["variant_id"]) or {}
            s["state"] = v.get("state", "")
            if v.get("state") == "needs_fix":
                s["problem"] = "; ".join((v.get("validation") or {}).get("errors") or [])[:200]
            continue
        pick = None
        for bid in s["brands"]:
            acc = _pilot_account(db, bid)
            pick = next((v for v in ready if acc and v["account_id"] == acc["account_id"] and v["variant_id"] not in used), None)
            if pick:
                s["brand_used"] = bid
                break
        if not pick:
            s["state"] = "waiting for a render" if renders_open else "no rendered post for " + " or ".join(s["brands"])
            continue
        at, why = _slot_time(tz, s["hour"], s["minute"], last.get(pick["account_id"]), now)
        last[pick["account_id"]] = at
        s.update(variant_id=pick["variant_id"], account_id=pick["account_id"], slot_at=at.astimezone(timezone.utc).isoformat(),
                 moved=why, state=pick["state"])
        used.add(pick["variant_id"])
    d["slots"] = slots
    if not slots:
        return _set(db, t, "blocked", "No post times found in the action.", d, "no times like '4:00pm' in the action")
    pinned = [s for s in slots if s.get("variant_id")]
    locked = [s for s in pinned if s["state"] in ("scheduled", "publishing", "published", "simulated")]
    stuck = [s for s in slots if not s.get("variant_id") and not s["state"].startswith("waiting")]
    day = min((datetime.fromisoformat(s["slot_at"]).astimezone(tz).date() for s in pinned), default=now.astimezone(tz).date())
    d["day"] = day.isoformat()
    if len(locked) == len(slots):
        _set(db, t, "done", f"All {len(slots)} posts are on the schedule.", d)
    elif len(pinned) > len(locked):
        _set(db, t, "needs_you", f"{len(pinned) - len(locked)} post(s) pinned on the run sheet for {day:%a %b %-d} — approve & schedule "
             f"them there. {len(locked)} of {len(slots)} scheduled." + (f" {len(stuck)} slot(s) have no post." if stuck else ""), d)
    elif stuck and not renders_open:
        _set(db, t, "blocked", f"{len(locked)} of {len(slots)} scheduled; {len(stuck)} slot(s) have no rendered post.", d, stuck[0]["state"])
    else:
        _set(db, t, "waiting", f"{len(locked)} of {len(slots)} scheduled — waiting for Video to finish renders.", d)


def run_post_on(db: DB, t: dict, hooks: Hooks, settings) -> None:
    d = dict(t.get("detail") or {})
    brands = _brands_in(t["title"].split(" to ")[0].split("—")[0]) or _brands_in(t["title"])
    acc = _pilot_account(db, brands[0]) if brands else None
    if not acc:
        return _set(db, t, "blocked", "Couldn't tell which account this post is for.", d, "no account named")
    d["account_id"] = acc["account_id"]
    for x in tasks(db, t["thread_id"]):
        for s in (x.get("detail") or {}).get("slots") or []:
            if s.get("account_id") == acc["account_id"] and s.get("variant_id"):
                v = db.get("variants", "variant_id", s["variant_id"]) or {}
                d["variant_id"] = s["variant_id"]
                if v.get("state") in ("scheduled", "publishing", "published", "simulated"):
                    return _set(db, t, "done", f"{acc['username_original']} post is {v['state']} — covered by the schedule task.", d)
                return _set(db, t, "needs_you", f"{acc['username_original']} post is pinned on the run sheet — approve & schedule it there.", d)
    _set(db, t, "waiting", f"Waiting for a rendered {acc['username_original']} post (the schedule task pins it).", d)


def run_measure(db: DB, t: dict, hooks: Hooks) -> None:
    d = dict(t.get("detail") or {})
    vids = [s["variant_id"] for x in tasks(db, t["thread_id"]) for s in (x.get("detail") or {}).get("slots") or [] if s.get("variant_id")]
    if not vids:
        return _set(db, t, "waiting", "Waiting for this challenge's posts to be scheduled.", d)
    vs = [db.get("variants", "variant_id", v) or {} for v in vids]
    live = [v for v in vs if v.get("state") == "published"]
    sim = [v for v in vs if v.get("state") == "simulated"]
    d["posts"] = len(vs)
    if not live and sim and len(sim) == len(vs):
        return _set(db, t, "blocked", f"All {len(vs)} posts went out as simulations — there are no real reach or follow numbers to collect.",
                    d, "publishing is simulated until you authorise the live pilot")
    if not live:
        return _set(db, t, "waiting", f"Waiting for {len(vs)} posts to publish ({len(sim)} simulated so far).", d)
    with_metrics = db.query("SELECT DISTINCT p.variant_id FROM posts p JOIN metrics m ON m.platform_post_id = p.platform_post_id "
                            "WHERE p.variant_id IN (" + ",".join("?" * len(live)) + ")", [v["variant_id"] for v in live])
    n = len(with_metrics)
    status = "done" if n == len(live) and len(live) == len(vs) else "in_progress"
    _set(db, t, status, f"Metrics in for {n} of {len(live)} published posts ({len(vs)} planned). Results feed the Publishing and "
         "Experimentation agents on the next intelligence refresh.", d)


def run_manual(db: DB, t: dict) -> None:
    if t["status"] == "todo":
        _set(db, t, "needs_you", "No automatic step for this one — do it and mark it done.", dict(t.get("detail") or {}))


ORDER = {"render": 0, "decision": 1, "creative": 2, "schedule": 3, "post_on": 4, "measure": 5, "manual": 6}


COMPUTE_KINDS = ("render", "creative")  # FFmpeg renders and draft generation: real CPU, minutes of work


def advance(db: DB, settings, ops: DB | None = None, hooks: Hooks | None = None, kinds: tuple | None = None) -> dict:
    """One pass over open tasks (render before schedule so pins follow finished renders)."""
    hooks = hooks or Hooks(settings, ops)
    ensure_for_adopted(db)
    out = {}
    for t in sorted(tasks(db, open_only=True), key=lambda x: ORDER.get(x["kind"], 9)):
        if (t.get("detail") or {}).get("manual") or (kinds is not None and t["kind"] not in kinds):
            continue
        try:
            fn = {"render": lambda: run_render(db, t, hooks), "creative": lambda: run_creative(db, t, hooks),
                  "decision": lambda: run_decision(db, t, hooks), "schedule": lambda: run_schedule(db, t, hooks, settings),
                  "post_on": lambda: run_post_on(db, t, hooks, settings), "measure": lambda: run_measure(db, t, hooks),
                  "manual": lambda: run_manual(db, t)}[t["kind"]]
            if t["status"] == "todo" and t["kind"] != "manual":
                _set(db, t, "in_progress", "Working on it…", dict(t.get("detail") or {}))
            fn()
        except Exception as e:  # noqa: BLE001 — a failing task reports, the others still advance
            _set(db, t, "failed", f"Step failed: {str(e)[:300]}", dict(t.get("detail") or {}), str(e)[:300])
        out[t["task_id"]] = t["status"]
    return out


def pins_for_day(db: DB, day_iso: str, tz: str) -> list[dict]:
    """Run sheet rows the schedule tasks pinned for a local day."""
    z = ZoneInfo(tz)
    out = []
    for t in db.all("team_tasks", "kind='schedule'"):
        for s in (t.get("detail") or {}).get("slots") or []:
            if s.get("variant_id") and s.get("slot_at") and datetime.fromisoformat(s["slot_at"]).astimezone(z).date().isoformat() == day_iso:
                out.append({**s, "task_id": t["task_id"], "thread_id": t["thread_id"]})
    return out


def needs_compute(db: DB) -> bool:
    if db.one("SELECT variant_id FROM variants WHERE render_status='queued'"):
        return True  # dashboard re-renders / overlay edits
    if db.one("SELECT variant_id FROM variants WHERE writers_status='pending' AND state IN ('in_review','needs_fix')"):
        return True  # the writers' room has copy to write
    return any(t["kind"] in COMPUTE_KINDS and t["status"] in ("todo", "in_progress") for t in tasks(db, open_only=True))


_lock = threading.Lock()
_runner_checked = [0.0, ""]


def _runner_project() -> str:
    import os
    return os.environ.get("PLN_TASK_RUNNER_PROJECT", "")


def _start_runner(project: str) -> str:
    """App Runner throttles CPU between requests, so background renders stall there. Heavy steps run in a
    CodeBuild job on the agent's own image instead; at most one at a time."""
    import time

    import boto3
    if time.time() - _runner_checked[0] < 60:
        return _runner_checked[1]
    cb = boto3.client("codebuild", region_name="us-east-1")
    ids = cb.list_builds_for_project(projectName=project, sortOrder="DESCENDING").get("ids", [])[:3]
    running = [b for b in (cb.batch_get_builds(ids=ids)["builds"] if ids else []) if b["buildStatus"] == "IN_PROGRESS"]
    state = f"runner {running[0]['id'].split(':')[-1][:8]} running" if running else \
        f"runner {cb.start_build(projectName=project)['build']['id'].split(':')[-1][:8]} started"
    _runner_checked[:] = [time.time(), state]
    return state


def start(settings) -> str:
    """Advance tasks off the request path. With a runner project configured, only the light kinds run here and
    renders go to the runner; otherwise everything runs in this thread (local / dev)."""
    if _lock.locked():
        return "already running"
    project = _runner_project()

    def work():
        import logging
        with _lock:
            try:
                own = DB(settings.database_url, init=False)
                ops = DB(settings.ops_database_url, init=False) if settings.ops_database_url else None
                if project:
                    advance(own, settings, ops, kinds=tuple(k for k in ORDER if k not in COMPUTE_KINDS))
                    ensure_for_adopted(own)
                    if needs_compute(own):
                        _start_runner(project)
                else:
                    advance(own, settings, ops)
            except Exception:  # noqa: BLE001
                logging.getLogger("pln_agent.team_tasks").exception("team task pass failed")

    threading.Thread(target=work, daemon=True, name="pln-team-tasks").start()
    return "started"


def run_runner(settings, max_minutes: int = 50, pause_s: int = 20, idle_passes: int = 3) -> dict:
    """The CodeBuild job: pick up finished MediaConvert crops, advance every task, repeat until nothing needs
    compute for a few passes (or time runs out)."""
    import logging
    import time

    from .ops_vertical import poll
    log = logging.getLogger("pln_agent.team_tasks")
    own = DB(settings.database_url, init=False)
    ops = DB(settings.ops_database_url, init=False) if settings.ops_database_url else None
    end, idle, passes = time.time() + max_minutes * 60, 0, 0
    while time.time() < end:
        r = poll(own, settings.ops_assets_bucket, settings, render=False)
        from .media import render_queued
        from .writers_room import run_pending
        wrote = run_pending(own)  # copy first, so the render that follows burns in the room's hook
        if wrote:
            log.info("runner pass %s: writers' room %s", passes + 1, wrote)
        queued = render_queued(own, settings)
        out = advance(own, settings, ops)
        if queued:
            log.info("runner pass %s: re-rendered %s", passes + 1, queued)
        passes += 1
        log.info("runner pass %s: mediaconvert ready %s; tasks %s", passes, r["ready"], out)
        idle = 0 if needs_compute(own) else idle + 1
        if idle >= idle_passes:
            break
        time.sleep(pause_s)
    return {"passes": passes, "open": len(tasks(own, open_only=True))}
