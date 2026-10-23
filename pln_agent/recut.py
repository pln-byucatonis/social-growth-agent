"""Re-cut: Reels go through the editor and the critic before anyone is asked to approve them.

A Reel draft in state 'recut' is out of Approvals. The task runner takes each one:

1. the editor watches the source around the moment and decides the shots (editor.py) — or says the moment can't
   be seen on a phone, and the draft is dropped;
2. the post is rendered from those shots (the replay only if the editor called the payoff worth it);
3. the critic watches the render (critic.py): pass → back to Approvals (in review); recut → the editor tries once
   more with the critic's notes; rewrite → the writers' room rewrites the copy once (a sound cut whose copy is still
   weak then goes to Approvals with the critic's note on it); kill, or a second bad cut → dropped.

Dropped drafts are archived, not deleted, with the reason on the post. `pull` moves the open Reel drafts here.
"""
from __future__ import annotations

from .db import DB, now_iso

MAX_ATTEMPTS = 2


def pull(db: DB, actor: str) -> int:
    """Take every open Reel draft (not approved, scheduled or published; not a carousel or Story) out of Approvals."""
    rows = db.all("variants", "state IN ('in_review','needs_fix') AND COALESCE(post_type, '') = '' AND platform <> 'x'")
    n = 0
    for v in rows:
        if not ((v.get("edl") or {}).get("segments")):
            continue  # text-only formats have nothing to cut
        t = dict(v.get("treatment") or {})
        t["recut"] = {"status": "pending", "from_state": v["state"], "pulled_at": now_iso(), "attempts": 0}
        db.update("variants", "variant_id", v["variant_id"], {"state": "recut", "treatment": t, "updated_at": now_iso()})
        n += 1
    db.audit(actor, "drafts_pulled_for_recut", "variants", "", {"count": n})
    return n


def gate_new(db: DB, actor: str = "editor") -> int:
    """New Reel drafts take the same road: once the writers' room is done with them, into re-cut before Approvals."""
    n = 0
    for v in db.all("variants", "state IN ('in_review','needs_fix') AND COALESCE(post_type, '') = '' AND platform <> 'x' "
                                "AND COALESCE(writers_status, '') <> 'pending' AND COALESCE(render_status, '') NOT IN ('queued','rendering')"):
        t = dict(v.get("treatment") or {})
        if t.get("recut") or not (v.get("edl") or {}).get("segments"):
            continue  # already through the editor (or pulled), or nothing to cut
        t["recut"] = {"status": "pending", "from_state": v["state"], "pulled_at": now_iso(), "attempts": 0, "new": True}
        db.update("variants", "variant_id", v["variant_id"], {"state": "recut", "treatment": t, "updated_at": now_iso()})
        n += 1
    if n:
        db.audit(actor, "new_drafts_to_recut", "variants", "", {"count": n})
    return n


def pending(db: DB, limit: int = 50) -> list[dict]:
    """Drafts waiting for the editor (none while the model allowance is paused)."""
    from datetime import datetime, timezone
    until = db.kv_get(PAUSE_KEY, "")
    if until and datetime.fromisoformat(until) > datetime.now(timezone.utc):
        return []
    return [v for v in db.all("variants", "state='recut'", order="updated_at", limit=limit * 3)
            if ((v.get("treatment") or {}).get("recut") or {}).get("status") in ("pending", "retry")][:limit]


def _mark(db: DB, vid: str, state: str, **rc) -> None:
    v = db.get("variants", "variant_id", vid)
    t = dict(v.get("treatment") or {})
    t["recut"] = {**(t.get("recut") or {}), **rc, "updated_at": now_iso()}
    db.update("variants", "variant_id", vid, {"state": state, "treatment": t, "updated_at": now_iso()})


def _rewrite(db: DB, vid: str, notes: str, actor: str) -> None:
    """The writers' room rewrites the copy on the critic's notes (it edits drafts in review, so step out of re-cut)."""
    from .writers_room import write
    db.update("variants", "variant_id", vid, {"state": "in_review"})
    try:
        write(db, vid, actor="writers-room", notes=notes)
    finally:
        db.update("variants", "variant_id", vid, {"state": "recut", "render_status": "rendered"})


def _copy_for_cut(db: DB, vid: str, actor: str) -> None:
    """Copy for the cut that was just made: the writers' room (with what the editor saw and the account's recent hooks),
    then the on-screen words. A failure keeps the copy the post already has."""
    from .overlay_copy import write as write_words
    try:
        _rewrite(db, vid, "", actor)
    except Exception as e:  # noqa: BLE001
        db.audit(actor, "writers_room_failed", "variants", vid, {"error": str(e)[:300]})
    write_words(db, vid)


def process(db: DB, variant_id: str, settings, actor: str = "editor", render=None, src: str | None = None) -> dict:
    """Editor → render → critic for one draft: one re-cut on the critic's cut notes, one rewrite on its copy notes."""
    from . import critic, editor
    from .media import render_variant
    from .workflow import refresh_validation
    render = render or (lambda vid: render_variant(db, vid, settings, actor=actor))
    v = db.get("variants", "variant_id", variant_id)
    asset = db.get("assets", "asset_id", v["asset_id"])
    rc = (v.get("treatment") or {}).get("recut") or {}
    notes, attempts, history = rc.get("notes", ""), int(rc.get("attempts") or 0), list(rc.get("history") or [])
    rewrites, need_cut, c = int(rc.get("rewrites") or 0), True, None
    while True:
        if need_cut:
            if attempts >= MAX_ATTEMPTS:
                break
            attempts += 1
            try:
                ed = editor.edit(db, v, asset, notes, src=src)
            except editor.Throttled as e:  # out of model allowance: wait, the draft keeps its place
                _mark(db, variant_id, "recut", status="pending", attempts=attempts - 1, error=str(e)[:200], history=history)
                raise
            except editor.EditorError as e:
                _mark(db, variant_id, "recut", status="failed", attempts=attempts, error=str(e)[:300], history=history)
                return {"variant_id": variant_id, "outcome": "failed", "why": str(e)}
            if not ed.get("readable"):
                _mark(db, variant_id, "archived", status="dropped", attempts=attempts, history=history,
                      why="editor: " + (ed.get("why") or "the action can't be seen on a phone"))
                db.audit(actor, "recut_dropped", "variants", variant_id, {"by": "editor", "why": ed.get("why")})
                return {"variant_id": variant_id, "outcome": "dropped", "why": ed.get("why")}
            editor.apply(db, variant_id, ed, actor)
            _copy_for_cut(db, variant_id, actor)
        r = render(variant_id)
        if r.get("status") != "rendered":
            _mark(db, variant_id, "recut", status="failed", attempts=attempts, error=f"render {r.get('status')}", history=history)
            return {"variant_id": variant_id, "outcome": "failed", "why": f"render {r.get('status')}"}
        v = db.get("variants", "variant_id", variant_id)
        ed = (v.get("treatment") or {}).get("editor") or {}
        dur = ((r.get("qc") or {}).get("duration_s")) or ed.get("length_s") or 0
        video = f"{r['dir']}/video.mp4" if r.get("dir") else r["path"]
        c = critic.review(db, variant_id, video, float(dur))
        t = dict(v.get("treatment") or {})
        t["critic"] = c
        db.update("variants", "variant_id", variant_id, {"treatment": t})
        history.append({"attempt": attempts, "rewrites": rewrites, "length_s": ed.get("length_s"), "verdict": c["verdict"],
                        "total": c["total"], "problems": c["problems"][:4]})
        if c["verdict"] == "pass" or (c["verdict"] == "rewrite" and rewrites >= 1):
            # a sound cut whose copy is still weak after one rewrite goes to a person, with the critic's notes on it
            if ed.get("replay_worthy") and c["scores"].get("replay_earned", 5) < 3:
                render(variant_id)  # the critic didn't think the replay earned its place: render without it
            _mark(db, variant_id, "in_review", status="passed", attempts=attempts, rewrites=rewrites, history=history,
                  copy_note="; ".join(c["copy_problems"][:3]) if c["verdict"] == "rewrite" else "")
            refresh_validation(db, variant_id)
            db.audit(actor, "recut_passed", "variants", variant_id, {"scores": c["scores"], "attempts": attempts, "rewrites": rewrites})
            return {"variant_id": variant_id, "outcome": "passed", "scores": c["scores"]}
        if c["verdict"] == "kill":
            break
        if c["verdict"] == "rewrite":
            rewrites += 1
            try:
                _rewrite(db, variant_id, "; ".join(c["copy_problems"][:5]), actor)
                from .overlay_copy import write as write_words
                write_words(db, variant_id)
            except Exception as e:  # noqa: BLE001 — the room failed: the cut is sound, so a person fixes the copy
                _mark(db, variant_id, "in_review", status="passed", attempts=attempts, rewrites=rewrites, history=history,
                      copy_note="; ".join(c["copy_problems"][:3]) + f" (writers' room failed: {str(e)[:80]})")
                refresh_validation(db, variant_id)
                return {"variant_id": variant_id, "outcome": "passed", "scores": c["scores"]}
            need_cut = False
            v = db.get("variants", "variant_id", variant_id)
            continue
        notes = "; ".join(c["cut_problems"][:5]) + (f". Fix: {c['fix']}" if c["fix"] else "")
        need_cut = True
        v = db.get("variants", "variant_id", variant_id)
    _mark(db, variant_id, "archived", status="dropped", attempts=attempts, rewrites=rewrites, history=history,
          why="critic: " + "; ".join((history[-1]["problems"] if history else [])[:3]))
    db.audit(actor, "recut_dropped", "variants", variant_id, {"by": "critic", "history": history[-2:]})
    return {"variant_id": variant_id, "outcome": "dropped", "why": history[-1]["problems"] if history else []}


PAUSE_KEY = "recut_paused_until"
PAUSE_H = 1


def run_pending(db: DB, settings, limit: int = 6, actor: str = "editor") -> list[dict]:
    """Runner step: a few drafts per pass, so other work keeps moving. When the model allowance runs out, everything
    waits an hour (and keeps its place) instead of failing one draft after another."""
    from datetime import datetime, timedelta, timezone
    from .editor import Throttled
    until = db.kv_get(PAUSE_KEY, "")
    if until and datetime.fromisoformat(until) > datetime.now(timezone.utc):
        return []
    out = []
    for v in pending(db, limit):
        try:
            out.append(process(db, v["variant_id"], settings, actor))
        except Throttled as e:
            db.kv_set(PAUSE_KEY, (datetime.now(timezone.utc) + timedelta(hours=PAUSE_H)).replace(microsecond=0).isoformat(), actor)
            db.audit(actor, "recut_paused", "variants", "", {"why": str(e)[:200], "hours": PAUSE_H})
            out.append({"variant_id": v["variant_id"], "outcome": "paused", "why": "model allowance used up"})
            break
        except Exception as e:  # noqa: BLE001 — one bad draft never stops the others
            _mark(db, v["variant_id"], "recut", status="failed", error=str(e)[:300])
            out.append({"variant_id": v["variant_id"], "outcome": "failed", "why": str(e)[:200]})
    return out


def summary(db: DB) -> dict:
    """Counts for the Approvals page: waiting, passed, dropped (with the commonest reasons), failed."""
    import collections
    rows = [v for v in db.all("variants", "state IN ('recut','in_review','archived')")
            if (v.get("treatment") or {}).get("recut")]
    st = collections.Counter(v["treatment"]["recut"].get("status") for v in rows)
    why = collections.Counter((v["treatment"]["recut"].get("why") or "")[:90] for v in rows
                              if v["treatment"]["recut"].get("status") == "dropped")
    return {"counts": dict(st), "reasons": why.most_common(5)}
