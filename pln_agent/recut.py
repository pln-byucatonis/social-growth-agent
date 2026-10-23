"""Re-cut: Reels go through the editor and the critic before anyone is asked to approve them.

A Reel draft in state 'recut' is out of Approvals. The task runner takes each one:

1. the editor watches the source around the moment and decides the shots (editor.py) — or says the moment can't
   be seen on a phone, and the draft is dropped;
2. the post is rendered from those shots (the replay only if the editor called the payoff worth it);
3. the critic watches the render (critic.py): pass → back to Approvals (in review); recut → the editor tries once
   more with the critic's notes; kill, or a second miss → dropped.

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
    return [v for v in db.all("variants", "state='recut'", order="updated_at", limit=limit * 3)
            if ((v.get("treatment") or {}).get("recut") or {}).get("status") in ("pending", "retry")][:limit]


def _mark(db: DB, vid: str, state: str, **rc) -> None:
    v = db.get("variants", "variant_id", vid)
    t = dict(v.get("treatment") or {})
    t["recut"] = {**(t.get("recut") or {}), **rc, "updated_at": now_iso()}
    db.update("variants", "variant_id", vid, {"state": state, "treatment": t, "updated_at": now_iso()})


def process(db: DB, variant_id: str, settings, actor: str = "editor", render=None, src: str | None = None) -> dict:
    """Editor → render → critic for one draft, with one re-cut on the critic's notes."""
    from . import critic, editor
    from .media import render_variant
    from .workflow import refresh_validation
    render = render or (lambda vid: render_variant(db, vid, settings, actor=actor))
    v = db.get("variants", "variant_id", variant_id)
    asset = db.get("assets", "asset_id", v["asset_id"])
    rc = (v.get("treatment") or {}).get("recut") or {}
    notes, attempts, history = rc.get("notes", ""), int(rc.get("attempts") or 0), list(rc.get("history") or [])
    while attempts < MAX_ATTEMPTS:
        attempts += 1
        try:
            ed = editor.edit(db, v, asset, notes, src=src)
        except editor.EditorError as e:
            _mark(db, variant_id, "recut", status="failed", attempts=attempts, error=str(e)[:300], history=history)
            return {"variant_id": variant_id, "outcome": "failed", "why": str(e)}
        if not ed.get("readable"):
            _mark(db, variant_id, "archived", status="dropped", attempts=attempts, history=history,
                  why="editor: " + (ed.get("why") or "the action can't be seen on a phone"))
            db.audit(actor, "recut_dropped", "variants", variant_id, {"by": "editor", "why": ed.get("why")})
            return {"variant_id": variant_id, "outcome": "dropped", "why": ed.get("why")}
        editor.apply(db, variant_id, ed, actor)
        r = render(variant_id)
        if r.get("status") != "rendered":
            _mark(db, variant_id, "recut", status="failed", attempts=attempts, error=f"render {r.get('status')}", history=history)
            return {"variant_id": variant_id, "outcome": "failed", "why": f"render {r.get('status')}"}
        dur = ((r.get("qc") or {}).get("duration_s")) or ed.get("length_s") or 0
        video = f"{r['dir']}/video.mp4" if r.get("dir") else r["path"]
        c = critic.review(db, variant_id, video, float(dur))
        v = db.get("variants", "variant_id", variant_id)
        t = dict(v.get("treatment") or {})
        t["critic"] = c
        db.update("variants", "variant_id", variant_id, {"treatment": t})
        history.append({"attempt": attempts, "length_s": ed.get("length_s"), "verdict": c["verdict"], "total": c["total"],
                        "problems": c["problems"][:4]})
        if c["verdict"] == "pass":
            if t.get("editor", {}).get("replay_worthy") and c["scores"].get("replay_earned", 5) < 3:
                r = render(variant_id)  # the critic didn't think the replay earned its place: render without it
            _mark(db, variant_id, "in_review", status="passed", attempts=attempts, history=history)
            refresh_validation(db, variant_id)
            db.audit(actor, "recut_passed", "variants", variant_id, {"scores": c["scores"], "attempts": attempts})
            return {"variant_id": variant_id, "outcome": "passed", "scores": c["scores"]}
        if c["verdict"] == "kill":
            break
        notes = "; ".join(c["problems"][:5]) + (f". Fix: {c['fix']}" if c["fix"] else "")
    _mark(db, variant_id, "archived", status="dropped", attempts=attempts, history=history,
          why="critic: " + "; ".join((history[-1]["problems"] if history else [])[:3]))
    db.audit(actor, "recut_dropped", "variants", variant_id, {"by": "critic", "history": history[-2:]})
    return {"variant_id": variant_id, "outcome": "dropped", "why": history[-1]["problems"] if history else []}


def run_pending(db: DB, settings, limit: int = 6, actor: str = "editor") -> list[dict]:
    """Runner step: a few drafts per pass, so other work keeps moving."""
    out = []
    for v in pending(db, limit):
        try:
            out.append(process(db, v["variant_id"], settings, actor))
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
