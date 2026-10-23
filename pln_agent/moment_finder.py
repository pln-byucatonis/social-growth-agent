"""Moment finder: when every draft from a recording is dropped as unreadable, look through the whole recording.

The editor only ever looked at the window around the moment the clip library had tagged, and on our footage those
tagged moments are often the ones you can't follow on a phone. But the library has already split each recording
into many candidate clips (each with a highlight score, an energy score, tags and a machine caption), and only a
few were ever made into drafts. So for a recording whose drafts were all dropped:

1. take the recording's other library clips, best scored first, skipping overlaps and the ones already tried;
2. look at eight frames of each of the top fifteen, all in one call, and ask which ones plainly show a payoff a
   stranger can follow on a phone, told what the editor drops moments for and why earlier drafts from this recording
   were dropped (and whether the footage is even the brand's sport — the library has some clips filed under the
   wrong league);
2b. take a closer look at the best few, the way the editor will (two frames a second, its rules), before any draft
   is made, since the survey's small frames make moments look clearer than they are;
3. make drafts from the best one or two, which then go through the editor and the critic like any other draft.

Each recording is searched once (re-searched after SEARCH_AGAIN_DAYS); what was looked at, what was picked and why are
kept in `moment_searches` and shown on the Approvals page.
"""
from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .db import DB, now_iso

SURVEY = 15  # candidates looked at per recording
FRAMES = 8  # frames per candidate in the survey
PICK = 2  # drafts made per recording
MIN_STRENGTH = 7
VERIFY = 4  # closer looks per recording before giving up on it
SEARCH_AGAIN_DAYS = 14

SYSTEM = """You are the senior editor of a sports network's social team, scouting long event footage for moments worth a
9:16 Reel. You only answer with JSON."""

PROMPT = """Each sheet is one candidate moment from the same {brand} recording: eight frames across the clip, left to
right, top to bottom, each with its time in seconds of the recording. {sport}

For each candidate say whether its payoff can be followed by a stranger on a phone with the sound off (the Reel will show
a 9:16 crop on the action, about the middle third of the frame at full phone width). Be strict: the editor who cuts it
will watch every frame and drop it, after we have paid for the work, if the payoff turns out to be
- shot in the dark or in harsh coloured light, motion-blurred, overexposed, or through a window or windscreen;
- an extreme close-up where bodies, fabric or skin fill the frame and you can't tell who does what;
- tangled bodies with no visible tap, hold, escape or referee stop;
- a ball too small to follow, or a camera that whips away before it drops;
- a static or empty frame, a title card, a commentary desk or a scoreboard.
Earlier drafts from this recording were dropped for: {dropped}
"readable" is true only if one of the frames shown plainly shows the payoff itself: give that frame's time as
"clear_frame". No such frame: "readable": false. Then how strong it is (0-10: would people stop scrolling, watch it twice,
send it on), when the payoff happens, and what happens in one plain sentence. "wrong_sport": true if the footage isn't
{brand} at all.
Reply with JSON only: {{"candidates": [{{"c": 1, "readable": true, "clear_frame": 1234.5, "strength": 8, "payoff_t": 1234.5,
"what": "...", "wrong_sport": false}}]}}"""

VERIFY_PROMPT = """These frames are one candidate moment from a {brand} recording, at two frames a second, each with its time
in seconds. A scout thinks the payoff is: "{what}". {sport}
Before we make a Reel of it, check it the way the editor will. {rules}
Answer only whether the payoff can be followed on a phone, by these rules. Reply with JSON only:
{{"readable": true, "why": "one sentence"}}"""


class FinderError(RuntimeError):
    pass


def _sport(brand_id: str) -> str:
    from .editor import config
    return (config().get("sports") or {}).get(brand_id, "")


def exhausted(db: DB) -> list[dict]:
    """Recordings whose drafts all went through the editor and none came out: [{media_path, brand_id, asset_ids}]."""
    by_src: dict[str, dict] = {}
    for v in db.all("variants", "COALESCE(post_type,'')=''"):
        rc = (v.get("treatment") or {}).get("recut")
        if not rc:
            continue
        a = db.get("assets", "asset_id", v["asset_id"]) or {}
        if not a.get("media_path") or not (a.get("probe") or {}).get("ops_clip"):
            continue
        s = by_src.setdefault(a["media_path"], {"media_path": a["media_path"], "brand_id": a["brand_id"], "asset_ids": set(),
                                                "open": 0})
        s["asset_ids"].add(a["asset_id"])
        if rc.get("status") != "dropped" or v["state"] != "archived":
            s["open"] += 1
    out = []
    for s in by_src.values():
        if s["open"]:
            continue
        done = db.get("moment_searches", "media_path", s["media_path"])
        if done and datetime.fromisoformat(done["searched_at"]) > datetime.now(timezone.utc) - timedelta(days=SEARCH_AGAIN_DAYS):
            continue
        out.append({**s, "asset_ids": sorted(s["asset_ids"])})
    return out


def candidates(db: DB, ops: DB, media_path: str, limit: int = SURVEY) -> list[dict]:
    """The recording's library clips, best scored first, not overlapping each other or a clip already made a draft."""
    from .ops_sources import clip_asset_id
    bucket, _, key = media_path[5:].partition("/")
    rows = ops.query("SELECT id, property, start_sec, end_sec, highlight_score, energy_score, caption FROM clips "
                     "WHERE superseded_by IS NULL AND status='tagged' AND source_bucket=? AND source_key=? "
                     "ORDER BY highlight_score DESC, id LIMIT 200", [bucket, key])
    tried = [(float(a["probe"]["ops_clip"]["start_sec"]), float(a["probe"]["ops_clip"]["end_sec"]))
             for a in db.all("assets", "media_path=?", [media_path]) if ((a.get("probe") or {}).get("ops_clip") or {}).get("end_sec")]
    rows.sort(key=lambda r: -(float(r["highlight_score"] or 0) + 30 * float(r["energy_score"] or 0)))
    out, taken = [], list(tried)
    for r in rows:
        s, e = float(r["start_sec"]), float(r["end_sec"])
        if db.get("assets", "asset_id", clip_asset_id(r["id"])) or any(s < te and ts < e for ts, te in taken):
            continue
        out.append({"clip_id": r["id"], "start": s, "end": e, "score": r["highlight_score"], "energy": r["energy_score"],
                    "caption": r["caption"] or ""})
        taken.append((s, e))
        if len(out) >= limit:
            break
    return out


def _frames(src: str, c: dict, outdir: Path) -> list:
    from .editor import frames
    dur = max(1.0, c["end"] - c["start"])
    return frames(src, c["start"], c["end"], outdir, fps=FRAMES / dur, width=400)[:FRAMES]


def _close_frames(src: str, c: dict, outdir: Path) -> list:
    """What the editor will see: two frames a second at the editor's width, the clip and a second either side."""
    from .editor import config, frames
    cfg = config()
    return frames(src, max(0.0, c["start"] - 1.0), c["end"] + 1.0, outdir, fps=float(cfg.get("fps", 2)),
                  width=int(cfg.get("frame_width", 480)))[:60]


def dropped_reasons(db: DB, media_path: str, n: int = 6) -> list[str]:
    """Why the editor and the critic turned down earlier drafts from this recording (the survey's negative examples)."""
    out = []
    for a in db.all("assets", "media_path=?", [media_path]):
        for v in db.all("variants", "asset_id=?", [a["asset_id"]]):
            why = ((v.get("treatment") or {}).get("recut") or {}).get("why") or ""
            why = why.split(":", 1)[-1].strip()[:160]
            if why and why not in out:
                out.append(why)
    return out[:n]


def verify(brand_id: str, src: str, c: dict, frames_fn=_close_frames) -> tuple[bool, str]:
    """A closer look at one pick, at the editor's frame rate and size and by the editor's rules."""
    from .editor import RULES, SYSTEM as EDITOR_SYSTEM, _llm, images, sheets
    from .team import _json
    with tempfile.TemporaryDirectory() as d:
        fr = frames_fn(src, c, Path(d))
        if len(fr) < 4:
            return False, "couldn't read frames"
        content = images(sheets(fr, cols=3, rows=3))
    rules = RULES.split("Reply with JSON only")[0]
    content.append({"type": "text", "text": VERIFY_PROMPT.format(brand=brand_id, what=c.get("what") or "", sport=_sport(brand_id),
                                                                 rules=rules)})
    res = _json(_llm(EDITOR_SYSTEM, [{"role": "user", "content": content}], 400))
    return bool(res.get("readable")), (res.get("why") or "")[:200]


def survey(db: DB, brand_id: str, src: str, cands: list[dict], frames_fn=_frames, dropped: list[str] | None = None) -> list[dict]:
    """One look at every candidate: [{...candidate, readable, strength, payoff_t, what, wrong_sport}]."""
    from .editor import _llm, sheets
    from .team import _json
    content, shown = [], []
    with tempfile.TemporaryDirectory() as d:
        for i, c in enumerate(cands, 1):
            fr = frames_fn(src, c, Path(d) / f"c{i}")
            if len(fr) < 3:
                continue
            b64, ts = sheets(fr, cols=4, rows=2)[0]
            content += [{"type": "text", "text": f"Candidate {i}: {ts[0]:.0f}s to {ts[-1]:.0f}s"},
                        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}}]
            shown.append((i, c))
    if not shown:
        raise FinderError("couldn't read frames from the recording")
    content.append({"type": "text", "text": PROMPT.format(brand=brand_id, sport=_sport(brand_id),
                                                          dropped="; ".join(dropped or []) or "nothing yet")})
    res = _json(_llm(SYSTEM, [{"role": "user", "content": content}], 1800))
    seen = {int(x.get("c", 0)): x for x in res.get("candidates") or []}
    out = []
    for i, c in shown:
        x = seen.get(i) or {}
        out.append({**c, "readable": bool(x.get("readable")) and x.get("clear_frame") is not None, "clear_frame": x.get("clear_frame"),
                    "strength": int(x.get("strength") or 0),
                    "payoff_t": x.get("payoff_t"), "what": (x.get("what") or "")[:200], "wrong_sport": bool(x.get("wrong_sport"))})
    return out


def search(db: DB, ops: DB, settings, src: dict, promote=None, frames_fn=_frames, close_fn=_close_frames,
           actor: str = "moment-finder") -> dict:
    """Search one recording and make drafts from its best moments."""
    from .media import resolve_media
    from .scout import promote as scout_promote
    promote = promote or (lambda ids: scout_promote(db, ops, ids, settings, actor))
    cands = candidates(db, ops, src["media_path"])
    row = {"media_path": src["media_path"], "brand_id": src["brand_id"], "searched_at": now_iso(), "looked_at": len(cands),
           "candidates": [], "picked": [], "note": ""}
    if not cands:
        row["note"] = "no other library clips from this recording"
    else:
        media = resolve_media(src["media_path"])
        found = survey(db, src["brand_id"], media, cands, frames_fn, dropped_reasons(db, src["media_path"]))
        row["candidates"] = found
        if sum(1 for f in found if f["wrong_sport"]) > len(found) / 2:
            row["note"] = f"most of this recording isn't {src['brand_id']}: the clip library has it filed under the wrong league"
        hopefuls = sorted([f for f in found if f["readable"] and not f["wrong_sport"] and f["strength"] >= MIN_STRENGTH],
                          key=lambda f: -f["strength"])
        good, failed = [], 0
        for f in hopefuls[:VERIFY]:  # a closer look before paying for a draft
            ok, why = verify(src["brand_id"], media, f, close_fn)
            f["verified"], f["verify_why"] = ok, why
            if ok:
                good.append(f)
            else:
                failed += 1
            if len(good) >= PICK:
                break
        row["picked"] = [f["clip_id"] for f in good]
        if good:
            r = promote(row["picked"])
            row["note"] = (row["note"] + "; " if row["note"] else "") + f"made drafts from {len(r.get('assets') or [])} moment(s)"
        elif not row["note"]:
            row["note"] = "nothing in the recording reads on a phone" + (f" ({failed} pick(s) failed the closer look)" if failed else "")
    db.insert("moment_searches", row, upsert_key="media_path")
    db.audit(actor, "moment_search", "moment_searches", src["media_path"], {"looked_at": row["looked_at"], "picked": row["picked"],
                                                                            "note": row["note"]})
    return row


def run_pending(db: DB, ops: DB | None, settings, limit: int = 2, actor: str = "moment-finder") -> list[dict]:
    """Runner step: a couple of recordings per pass, once the drafts waiting for the editor are done."""
    from .editor import Throttled
    from .recut import PAUSE_KEY, pending
    if not ops or pending(db, 1) or db.kv_get(PAUSE_KEY, "") > now_iso():
        return []
    out = []
    for src in exhausted(db)[:limit]:
        try:
            out.append(search(db, ops, settings, src, actor=actor))
        except Throttled as e:
            db.kv_set(PAUSE_KEY, (datetime.now(timezone.utc) + timedelta(hours=1)).replace(microsecond=0).isoformat(), actor)
            out.append({"media_path": src["media_path"], "note": f"paused: {str(e)[:80]}"})
            break
        except Exception as e:  # noqa: BLE001 — one recording failing never stops the others
            db.insert("moment_searches", {"media_path": src["media_path"], "brand_id": src["brand_id"], "searched_at": now_iso(),
                                          "looked_at": 0, "candidates": [], "picked": [], "note": f"failed: {str(e)[:200]}"},
                      upsert_key="media_path")
            out.append({"media_path": src["media_path"], "note": f"failed: {str(e)[:120]}"})
    return out
