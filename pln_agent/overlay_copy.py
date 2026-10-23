"""The words burned into a Reel, written for the cut it goes on.

The stamps used to come from a fixed bank ("TAP.", "CRAZY.", "BANKED.", "NO WAY OUT"…) picked by the post's id, so an
account's posts all said the same few words, often over the wrong shot. Now, once the editor has cut a post
(editor.py), Claude writes its on-screen words from what the editor saw happen:

* the opening text — the writers' room hook, unless it repeats what this account said lately;
* the payoff word — one to three words naming what happens at the payoff ("FROM THE BOTTOM.", "OFF THE ROCKS."), or
  nothing when the moment speaks for itself;
* a reaction word only when there is a reaction shot and a word adds something.

Freshness is enforced, not suggested: a word or opening used in the account's last ten posts is refused, and so is
anything from the old stock bank. The hook's look (pill, knockout, outline) rotates so consecutive posts don't match.
When Claude can't be reached, the bank is the fallback, minus anything used lately.
"""
from __future__ import annotations

import json
import re

from .db import DB, now_iso

RECENT = 10
STYLES = ("pill", "knockout", "outline")
SYSTEM = "You write the few words burned into sports Reels. You only answer with JSON."
PROMPT = """The editor cut this {platform} Reel for {brand}. What happens, as the editor saw it: "{what}"
The payoff ({payoff_at} s into the post): "{payoff}". Shots: {shots}.
The writers' hook for the opening text: "{hook}".

Write the on-screen words:
- "hook": the opening text, 2-5 words. Keep the writers' hook unless it repeats this account's recent openings below (same
  words or the same shape, e.g. another "Clutch putt" or "Sports you didn't know you needed"); then write a new one
  about THIS clip.
- "payoff_word": 1-3 words slammed on screen at the payoff, naming what actually happens there in this clip (what
  someone would shout watching it), ending with a full stop. "" if the moment speaks for itself.
- "reaction_word": "" unless there is a reaction shot and a word adds something to it.
Rules: no digits or numbers, no names, no brand names, nothing the editor's description doesn't support. Never use any
of these, they are worn out on this account or are stock filler: {avoid}.
Reply with JSON only: {{"hook": "...", "payoff_word": "...", "reaction_word": "", "why": "one line"}}"""


def _norm(s: str) -> str:
    return re.sub(r"[^a-z ]+", "", (s or "").lower()).strip()


def stock() -> list[str]:
    from .overlays import config
    return sorted({w for fam in (config().get("words") or {}).values() for ws in fam.values() for w in ws})


def recent(db: DB, v: dict, n: int = RECENT) -> dict:
    """What this account's last posts said and looked like: openings, stamped words, hook styles (newest first)."""
    rows = db.all("variants", "account_id=? AND variant_id<>? AND COALESCE(post_type,'')='' AND "
                  "state IN ('in_review','approved','scheduled','published','simulated','recut')",
                  [v["account_id"], v["variant_id"]], order="updated_at DESC", limit=n)
    hooks, words, styles = [], [], []
    for r in rows:
        qc = r.get("render_qc") or {}
        ow = qc.get("overlay_words") if isinstance(qc.get("overlay_words"), dict) else {}
        hooks += [x for x in [ow.get("hook") or r.get("hook")] if x]
        words += [x for k, x in ow.items() if k != "hook" and x]
        if qc.get("overlay_style"):
            styles.append(qc["overlay_style"].split("+")[0])
    return {"hooks": hooks, "words": words, "styles": styles}


def _shape(text: str) -> str:
    """The first two words: 'clutch putt …', 'sports you …' — repeating the shape reads as the same post."""
    return " ".join(_norm(text).split()[:2])


def fresh(word: str, rec: dict, banned: set[str]) -> bool:
    n = _norm(word)
    return bool(n) and n not in banned and n not in {_norm(x) for x in rec["words"] + rec["hooks"]}


def fresh_hook(hook: str, rec: dict) -> bool:
    return bool(_norm(hook)) and _norm(hook) not in {_norm(x) for x in rec["hooks"]} \
        and _shape(hook) not in {_shape(x) for x in rec["hooks"][:RECENT]}


def pick_style(rec: dict, current: str) -> str:
    """A hook look the account didn't use on its last two posts (the current one when it's already fresh)."""
    last = rec["styles"][:2]
    if current not in last:
        return current
    return next((s for s in STYLES if s not in last), current)


def _clean(s: str, max_words: int) -> str:
    s = re.sub(r"[*_#`\"]+", "", s or "").strip()
    return "" if (not s or any(ch.isdigit() for ch in s) or len(s.split()) > max_words) else s


def write(db: DB, variant_id: str, llm=None, actor: str = "overlay-copy") -> dict:
    """Write and put the on-screen words on the post (treatment.overlay), and choose a fresh hook look."""
    from . import team
    from .overlays import _pick, style_for, words_for
    v = db.get("variants", "variant_id", variant_id)
    t = dict(v.get("treatment") or {})
    ed = t.get("editor") or {}
    rec = recent(db, v)
    banned = {_norm(w) for w in stock()}
    ot = [dict(o) for o in v.get("onscreen_text") or [] if not (o.get("position") or "").endswith("small")]
    hook = (ot[0]["text"] if ot else v.get("hook") or "").strip()
    out, why, by = {}, "", "claude"
    try:
        call = llm or team._llm
        raw = call(SYSTEM, [{"role": "user", "content": PROMPT.format(
            platform=v.get("platform"), brand=v.get("brand_id"), what=ed.get("what_happens") or "unknown",
            payoff_at=ed.get("payoff_at_s"), payoff=(ed.get("payoff") or {}).get("what") or "",
            shots=", ".join(f"{s['role']} {s['out'] - s['in']:.1f}s" for s in ed.get("shots") or []) or "unknown",
            hook=hook, avoid=", ".join(sorted(set(stock()) | {x.upper() for x in rec["words"] + rec["hooks"]})) or "none")}], 500)
        out = team._json(raw)
        why = out.get("why") or ""
    except Exception as e:  # noqa: BLE001 — no model: the stock bank, minus what this account used lately
        by, why = "bank", f"no model ({str(e)[:80]}): stock words not used lately"
    s = style_for(v.get("brand_id", ""), t.get("overlay"))
    bank = words_for(s)
    new_hook = _clean(out.get("hook", ""), 6).upper()
    if not (new_hook and fresh_hook(new_hook, rec)):
        new_hook = hook.upper() if fresh_hook(hook, rec) else ""
    if not new_hook:  # even the writers' hook repeats: a fresh one from the bank, or keep it with a note
        opts = [x for x in bank.get("hook") or [] if fresh_hook(x, rec)]
        new_hook = (_pick(opts, variant_id + ":hook") if opts else hook).upper()
    payoff = _clean(out.get("payoff_word", ""), 3).upper() if by == "claude" else ""
    if payoff and not fresh(payoff, rec, banned):
        payoff = ""
    if by == "bank":
        opts = [x for x in bank.get("payoff") or [] if fresh(x, rec, set())]
        payoff = _pick(opts, variant_id + ":payoff") if opts else ""
    reaction = _clean(out.get("reaction_word", ""), 3).upper() if by == "claude" else ""
    if reaction and (not fresh(reaction, rec, banned) or _norm(reaction) == _norm(payoff)):
        reaction = ""
    style = pick_style(rec, s.get("hook") or "pill")
    if ot:
        ot[0]["text"] = new_hook
    else:
        ot = [{"start": 0.0, "end": 2.5, "text": new_hook, "position": "top"}]
    t["overlay"] = {**(t.get("overlay") or {}), "hook": style, "hook_custom": True,
                    "stamp": payoff or "none", "reaction_word": reaction, "reaction": bool(reaction),
                    "words": {"hook": new_hook, "payoff": payoff, "reaction": reaction, "by": by, "why": why,
                              "avoided": len(rec["words"]) + len(rec["hooks"]), "at": now_iso()}}
    db.update("variants", "variant_id", variant_id, {"onscreen_text": ot, "treatment": t, "updated_at": now_iso()})
    db.audit(actor, "overlay_words", "variants", variant_id, t["overlay"]["words"] | {"style": style})
    return t["overlay"]["words"] | {"style": style}
