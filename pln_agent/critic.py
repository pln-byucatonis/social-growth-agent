"""The critic: watches every finished Reel before it reaches Approvals.

Measurements (QC) only say a file is the right length and loudness. The critic watches the render the way a viewer
would: frames at 2 a second across the whole post, tiled into contact sheets with the post's own times printed on
them, alongside the hook, the on-screen text with its timings, the editor's account of what happens, and the hooks
this account has used lately. It scores the post on a short checklist and gives a verdict:

* pass    — goes to Approvals;
* recut   — the cut is the problem: the editor gets one more go, with the critic's notes;
* rewrite — the cut is fine but the hook or text isn't (it repeats recent posts, doesn't match the shot): the writers'
            room rewrites the copy with the critic's notes, once;
* kill    — dropped (archived, nothing deleted): the moment isn't worth a post.

A pass needs the critic's own pass and every score at 3 or more, with the payoff clearly visible (4+). A replay that scores
under 3 doesn't fail the post: the post is rendered again without it. The scores and the notes
are stored on the post (treatment.critic) and shown on its page.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from .db import DB, now_iso
from .editor import _llm, frames, images, sheets

CHECKS = {
    "first_second": "the FOOTAGE in the first second shows motion that makes you want to keep watching (not a desk, a scoreboard or a broadcast title card); judge the pictures, not our own hook text on top, which is copy",
    "payoff_visible": "the decisive moment is clearly visible and readable on a phone",
    "no_dead_air": "no stretch of commentators talking, scoreboards, scenery, resets or anything else that isn't the action or a reaction",
    "text_fits_shot": "each piece of on-screen text matches what is on screen when it appears, and isn't generic filler",
    "readable": "a stranger understands what happened without sound or context",
    "replay_earned": "if there is a slow-motion REPLAY, it replays the payoff and the slow motion adds something (score 5 if there is no replay)",
    "fresh": "the hook and text don't repeat what this account posted recently",
}
PASS_MIN, PAYOFF_MIN = 3, 4
COPY_CHECKS = ("text_fits_shot", "fresh")  # the writers fix these; the rest are the cut, which the editor fixes

SYSTEM = """You are the toughest reviewer on a sports network's social team. You watch every Reel before it can be
approved and you are paid to stop weak posts, not to be kind. You only answer with JSON."""


def recent_text(db: DB, v: dict, n: int = 10) -> list[str]:
    rows = db.all("variants", "account_id=? AND variant_id<>? AND state IN ('in_review','approved','scheduled','published')",
                  [v["account_id"], v["variant_id"]], order="updated_at DESC", limit=n)
    out = []
    for r in rows:
        bits = [r.get("hook") or ""] + [o.get("text", "") for o in r.get("onscreen_text") or []]
        out.append(" / ".join(b for b in bits if b))
    return out


def _brief(db: DB, v: dict, duration: float) -> str:
    ed = (v.get("treatment") or {}).get("editor") or {}
    ost = "\n".join(f"- {o['start']:.1f}-{o['end']:.1f}s: \"{o['text']}\"" for o in v.get("onscreen_text") or []) or "- none"
    qc = v.get("render_qc") or {}
    words = ", ".join(f"\"{w}\"" for w in (qc.get("overlay_words") or {}).values()) if isinstance(qc.get("overlay_words"), dict) else ""
    recent = "\n".join(f"- {r}" for r in recent_text(db, v)) or "- none yet"
    checks = "\n".join(f'- "{k}": {d}' for k, d in CHECKS.items())
    return f"""This is the finished {v.get('platform')} post for {v.get('brand_id')}, {duration:.1f} s long, shown as frames at
2 a second with the post's own time on each.
Hook: "{v.get('hook') or ''}"
On-screen text:
{ost}
{('Punch words burned in: ' + words) if words else ''}
The editor says what happens: "{ed.get('what_happens', 'unknown')}". Payoff at {ed.get('payoff_at_s', '?')} s in the post.
{'A slow-motion replay follows the main edit.' if ed.get('replay_worthy') else 'There should be no replay.'}
This account's recent posts (hook / text):
{recent}

Score each check 0-5:
{checks}
Then a verdict: "pass" (good enough to post as is), "recut" (the moment is good but something needs fixing), or "kill"
(the moment itself isn't worth a post). Keep the two kinds of problem apart: "cut_problems" are about the pictures and
the edit (what the editor changes, in source terms it can act on); "copy_problems" are about the hook and on-screen
text (what the writers change). Our own burned-in text (the hook over the opening, the words on the payoff) is copy:
if it is too big or covers the action, that is a copy problem, never a reason to score the cut down.
Reply with JSON only: {{"scores": {{"first_second": 4, ...}}, "verdict": "pass", "cut_problems": ["..."],
"copy_problems": ["..."], "fix": "what the editor should change"}}"""


def judge(res: dict) -> dict:
    """Apply the pass bar. A good cut with weak copy goes to the writers ("rewrite"), not back to the editor."""
    scores = {k: max(0, min(5, int(res.get("scores", {}).get(k, 0)))) for k in CHECKS}
    said = res.get("verdict") if res.get("verdict") in ("pass", "recut", "kill") else "recut"
    cut_low = [k for k, s in scores.items() if s < PASS_MIN and k not in COPY_CHECKS and k != "replay_earned"]
    if scores["payoff_visible"] < PAYOFF_MIN and "payoff_visible" not in cut_low:
        cut_low.append("payoff_visible")
    copy_low = [k for k in COPY_CHECKS if scores[k] < PASS_MIN]  # a weak replay is removed, not re-cut
    cut_p, copy_p = list(res.get("cut_problems") or []), list(res.get("copy_problems") or [])
    if res.get("problems"):  # older single list
        cut_p += [p for p in res["problems"] if p not in cut_p]
    if said == "kill":
        verdict = "kill"
    elif cut_low or (said == "recut" and cut_p and not copy_low and not copy_p):
        verdict = "recut"
    elif copy_low or (said == "recut" and copy_p):
        verdict = "rewrite"
    else:
        verdict = "pass" if said == "pass" or not (cut_p or copy_p) else "recut"
    return {"scores": scores, "verdict": verdict, "said": said, "problems": cut_p + copy_p, "cut_problems": cut_p,
            "copy_problems": copy_p, "fix": res.get("fix") or "", "below_bar": cut_low + copy_low, "total": sum(scores.values())}


def review(db: DB, variant_id: str, video: str, duration: float) -> dict:
    """Watch the finished render and give the verdict."""
    from .team import _json
    v = db.get("variants", "variant_id", variant_id)
    with tempfile.TemporaryDirectory() as d:
        fr = frames(video, 0.0, duration, Path(d), width=180)
        if len(fr) < 4:
            raise RuntimeError("couldn't read frames from the render")
        sh = sheets(fr, cols=8, rows=3)
    content = images(sh) + [{"type": "text", "text": _brief(db, v, duration)}]
    raw = _llm(SYSTEM, [{"role": "user", "content": content}], 1200)
    try:
        out = judge(_json(raw))
    except (ValueError, KeyError, TypeError) as e:
        out = judge({"verdict": "recut", "problems": [f"critic answer unusable: {e}"]})
    return {**out, "frames": len(fr), "at": now_iso()}
