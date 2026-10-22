"""Growth Experimentation: run controlled tests and turn the results into PLN evidence.

Three ways an arm is decided:
* assigned — the lab puts drafts into arms before review: `cut` (A ≈12s / B ≈18s / C ≈40s from the Creative
  Director) or `hook_style` (the top hook vs an alternative style). Assignment alternates so arms get similar
  content and slots, and only touches drafts nobody has approved yet: the reviewer sees what will be posted.
* observed — the arm is a creative variable already recorded on each post (`posting_hour_bucket`,
  `commentary`, `captions_burned`, ...), compared within the experiment window.
* weekly — the frequency test from the Publishing Strategist (each week is an arm).

Reading uses the Creative DB evidence rule: seeded permutation test on medians, Benjamini–Hochberg across
the experiments read together, ≥5 posts per arm before anything is said, ≥8 and q<0.05 for conclusive. Only
per-1k-view rates count. A conclusive or suggestive result is written to the Knowledge Engine as
`pln_evidence` with the experiment as its source; everything else is reported as such, never as a winner.
"""
from __future__ import annotations

import statistics
import uuid
from datetime import date, datetime, timedelta, timezone

from .creative_db import CONCLUSIVE_N, MIN_N, bh_adjust, evidence_class, permutation_p
from .db import DB, now_iso

RATE_OUTCOMES = ("engagements_per_1k_views", "shares_per_1k_views", "follows_per_1k_views", "saves_per_1k_views",
                 "comments_per_1k_views", "watch_pct")
# Raw counts never decide a test — except where the variable acts on how many people see the post and a per-view rate
# can't show it: a Story alongside the Reel, the time it goes out. Those are read on reach.
REACH_TESTS = {"story_companion": ("reach",), "post_time": ("reach",)}
POST_TYPES = {"A": "reel", "B": "carousel"}  # `post_type`: the same moment as a Reel or as a swipe carousel (Instagram)
STORY = {"A": "Reel only", "B": "Reel + Story"}  # `story_companion`: does a Story of the Reel lift the Reel's reach?
LAYOUTS = {"A": "crop", "B": "stack"}  # `layout`: full-bleed 9:16 crop vs the whole landscape frame stacked over a blurred fill
DEFAULT_OUTCOME = "engagements_per_1k_views"
MAX_WEEKS = 6  # an experiment that still lacks sample after this is closed as inconclusive
ASSIGNABLE = ("in_review", "needs_fix")


def _res(e: dict) -> dict:
    return dict(e.get("result") or {})


def outcome_of(e: dict) -> str:
    m = (e.get("success_metric") or "").strip()
    return m if m in RATE_OUTCOMES + REACH_TESTS.get(e.get("variable") or "", ()) else _res(e).get("outcome") or DEFAULT_OUTCOME


# ---- design ---------------------------------------------------------------------------------------

def create(db: DB, account_id: str, variable: str, arms: dict[str, str], hypothesis: str, actor: str,
           mode: str = "assigned", outcome: str = DEFAULT_OUTCOME, start: date | None = None, observe: str = "") -> dict:
    """arms: label → description (for observed experiments, label → the creative value that defines the arm)."""
    if len(arms) < 2:
        raise ValueError("an experiment needs at least two arms")
    if outcome not in RATE_OUTCOMES + REACH_TESTS.get(variable, ()):
        raise ValueError(f"outcome must be a per-1k-view rate: {', '.join(RATE_OUTCOMES)}"
                         + (f" (or {', '.join(REACH_TESTS[variable])} for this test)" if variable in REACH_TESTS else ""))
    acc = db.get("accounts", "account_id", account_id)
    if not acc:
        raise KeyError(account_id)
    start = start or datetime.now(timezone.utc).date()
    labels = list(arms)
    e = {"experiment_id": f"lab-{uuid.uuid4().hex[:8]}", "week_start": start.isoformat(), "platform": acc["platform"],
         "account_id": account_id, "variable": variable, "control": f"{labels[0]}: {arms[labels[0]]}",
         "treatment": "; ".join(f"{k}: {arms[k]}" for k in labels[1:]), "hypothesis": hypothesis,
         "success_metric": outcome, "status": "running",
         "result": {"mode": mode, "arms": arms, "observe": observe or variable, "outcome": outcome,
                    "started_at": start.isoformat(), "created_by": actor, "reads": []}}
    db.insert("experiments", e)
    db.audit(actor, "experiment_created", "experiments", e["experiment_id"], {"variable": variable, "mode": mode, "arms": arms})
    if mode == "assigned":
        assign(db, e["experiment_id"], actor)
    return db.get("experiments", "experiment_id", e["experiment_id"])


def propose(db: DB) -> list[dict]:
    """Tests worth running now, per account, from what the drafts on hand make possible."""
    out = []
    running = {(e["account_id"], e["variable"]) for e in db.all("experiments", "status IN ('running','planned')")}
    by_acc: dict[str, list[dict]] = {}
    for v in db.all("variants", "state IN ('in_review','needs_fix')", order="account_id"):  # one read, not one per account
        by_acc.setdefault(v["account_id"], []).append(v)
    for aid, vs in by_acc.items():
        multi_cut = [v for v in vs if sum(1 for c in ((v.get("treatment") or {}).get("duration_variants") or []) if c.get("available")) >= 2]
        if len(multi_cut) >= 4 and (aid, "cut") not in running:
            out.append({"account_id": aid, "variable": "cut", "mode": "assigned", "drafts": len(multi_cut),
                        "arms": {"A": "short cut (≈12s)", "B": "medium cut (≈18s)"},
                        "hypothesis": "A shorter cut earns more engagement per 1k views than the medium cut on this account"})
        land = [v for v in vs if _landscape_video(db, v)]
        if len(land) >= 2 and (aid, "layout") not in running and aid.split(".")[1] in ("instagram", "tiktok", "youtube", "facebook"):
            out.append({"account_id": aid, "variable": "layout", "mode": "assigned", "drafts": len(land),
                        "arms": dict(LAYOUTS), "outcome": "watch_pct",
                        "hypothesis": "Showing the whole frame (stacked) keeps people watching longer than a full-screen crop"})
        reels = [v for v in vs if v["platform"] == "instagram" and not v.get("post_type")]
        if len(reels) >= 2 and (aid, "post_type") not in running:
            out.append({"account_id": aid, "variable": "post_type", "mode": "assigned", "drafts": len(reels),
                        "arms": dict(POST_TYPES), "outcome": "shares_per_1k_views",
                        "hypothesis": "The same moment as a swipe carousel earns more shares per 1k views than as a Reel"})
        if len(reels) >= 2 and (aid, "story_companion") not in running:
            out.append({"account_id": aid, "variable": "story_companion", "mode": "assigned", "drafts": len(reels),
                        "arms": dict(STORY), "outcome": "reach",
                        "hypothesis": "Posting a Story of the Reel lifts the Reel's reach"})
        styles = {}
        for v in vs:
            for h in (v.get("hook_candidates") or [])[:3]:
                styles[h.get("style")] = styles.get(h.get("style"), 0) + 1
        top = [s for s, n in sorted(styles.items(), key=lambda kv: -kv[1]) if s and n >= 4][:2]
        if len(top) == 2 and (aid, "hook_style") not in running:
            out.append({"account_id": aid, "variable": "hook_style", "mode": "assigned", "drafts": len(vs),
                        "arms": {"A": top[0], "B": top[1]},
                        "hypothesis": f"'{top[1]}' hooks beat '{top[0]}' hooks on engagement per 1k views"})
    return out


# ---- assignment -----------------------------------------------------------------------------------

def _landscape_video(db: DB, v: dict) -> bool:
    """A Reel made from a landscape master: the only kind where crop vs stacked is a choice."""
    if v.get("post_type") or v["platform"] not in ("instagram", "tiktok", "youtube", "facebook"):
        return False
    a = db.get("assets", "asset_id", v["asset_id"]) or {}
    w, h = a.get("width"), a.get("height")
    return not (w and h and h >= w) and "/verticals/" not in (a.get("media_path") or "")


def _apply(db: DB, e: dict, v: dict, arm: str, actor: str) -> dict | None:
    from .creative_director import use_cut
    from .workflow import WorkflowError, edit_variant
    res = _res(e)
    if e["variable"] == "cut":
        cuts = {c["label"]: c for c in ((v.get("treatment") or {}).get("duration_variants") or []) if c.get("available")}
        if arm not in cuts:
            return None
        if (v.get("edl") or {}).get("variant_label") != arm:
            use_cut(db, v["variant_id"], arm, actor)
        return {"cut": arm}
    if e["variable"] == "layout":
        if not _landscape_video(db, v):
            return None
        layout = res["arms"][arm]
        t = dict(v.get("treatment") or {})
        if t.get("layout_arm") != layout:
            t["layout_arm"] = layout
            db.update("variants", "variant_id", v["variant_id"],
                      {"treatment": t, **({"render_status": "queued"} if v.get("render_status") == "rendered" else {})})
        return {"layout": layout}
    if e["variable"] in ("post_type", "story_companion"):
        from . import carousel
        if v["platform"] != "instagram" or v.get("post_type"):
            return None
        choice = res["arms"][arm]
        if choice in ("reel", "Reel only"):
            return {e["variable"]: choice}
        try:
            made = carousel.make(db, v["variant_id"], "carousel" if e["variable"] == "post_type" else "story", actor)
        except carousel.CarouselError:
            return None
        if e["variable"] == "post_type":  # this moment goes out as the carousel: the Reel steps aside, the arm is the carousel
            db.update("variants", "variant_id", v["variant_id"], {"state": "archived", "updated_at": now_iso()})
            db.audit(actor, "archived_for_test", "variants", v["variant_id"], {"experiment": e["experiment_id"], "posted_as": made["variant_id"]})
            return {"post_type": "carousel", "variant_id": made["variant_id"], "reel": v["variant_id"]}
        return {"story_companion": True, "story": made["variant_id"]}  # the Reel is the measured post; its Story rides along
    if e["variable"] == "post_time":
        hhmm = res["arms"][arm]
        t = dict(v.get("treatment") or {})
        t["slot_arm"] = hhmm
        db.update("variants", "variant_id", v["variant_id"], {"treatment": t})
        return {"post_time": hhmm}
    if e["variable"] == "hook_style":
        style = res["arms"][arm]
        cand = next((h for h in v.get("hook_candidates") or [] if h.get("style") == style), None)
        if not cand:
            return None
        if cand["text"] != v.get("hook"):
            caption = v.get("caption") or ""
            new_caption = caption.replace(v["hook"], cand["text"], 1) if v.get("hook") and v["hook"] in caption else caption
            try:
                edit_variant(db, v["variant_id"], {"hook": cand["text"], "caption": new_caption}, actor)
            except WorkflowError:
                return None
        return {"hook_style": style, "hook": cand["text"]}
    return None


def assign(db: DB, experiment_id: str, actor: str = "growth-lab", limit: int = 40) -> list[dict]:
    """Put unassigned, unapproved drafts of the account into arms, alternating (A, B, A, B...) by score."""
    e = db.get("experiments", "experiment_id", experiment_id)
    labels = list(_res(e)["arms"])
    taken = {r["variant_id"] for r in db.query("SELECT variant_id FROM experiment_arms")}
    counts = {a: 0 for a in labels}
    for r in db.all("experiment_arms", "experiment_id=?", [experiment_id]):
        counts[r["arm"]] = counts.get(r["arm"], 0) + 1
    vs = [v for v in db.all("variants", "account_id=? AND state IN ('in_review','needs_fix')", [e["account_id"]])
          if v["variant_id"] not in taken and not v.get("post_type")]
    vs.sort(key=lambda v: -(v.get("score") or 0))
    # several tests on one account share its new drafts instead of the first one taking them all
    peers = sorted(x["experiment_id"] for x in db.all("experiments", "account_id=? AND status='running'", [e["account_id"]])
                   if _res(x).get("mode") == "assigned")
    if len(peers) > 1 and experiment_id in peers:  # each running test takes every n-th draft, offset by its place in the list
        vs = vs[peers.index(experiment_id)::len(peers)]
    done = []
    for v in vs[:limit]:
        for arm in sorted(labels, key=lambda a: counts[a]):  # the arm with fewest posts first, falling back if it can't apply
            applied = _apply(db, e, v, arm, actor)
            if applied is not None:
                db.insert("experiment_arms", {"experiment_id": experiment_id, "variant_id": applied.pop("variant_id", v["variant_id"]),
                                              "arm": arm, "assigned_at": now_iso(), "applied": applied})
                counts[arm] += 1
                done.append({"variant_id": v["variant_id"], "arm": arm})
                break
    if done:
        db.audit(actor, "experiment_assigned", "experiments", experiment_id, {"n": len(done), "counts": counts})
    return done


# ---- reading --------------------------------------------------------------------------------------

def _arm_values(db: DB, e: dict) -> dict[str, list[float]]:
    from .audience import _account_series, post_outcomes
    res, outcome = _res(e), outcome_of(e)
    start = res.get("started_at") or e["week_start"]
    vals: dict[str, list[float]] = {a: [] for a in res.get("arms") or {}}
    mode = res.get("mode")
    if e["variable"] == "frequency":  # weekly arm: engagement per 1k views of that week's posts
        week = date.fromisoformat(e["week_start"])
        end = (week + timedelta(days=7)).isoformat()
        posts = [p for p in post_outcomes(db, [e["account_id"]]) if p["published_at"] and e["week_start"] <= str(p["published_at"])[:10] < end]
        return {res.get("arm", "?"): [p["outcomes"][outcome] for p in posts if p["outcomes"].get(outcome) is not None and not p["young"]]}
    posts = [p for p in post_outcomes(db, [e["account_id"]]) if p["published_at"] and str(p["published_at"])[:10] >= start]
    if mode == "assigned":
        arm_of = {r["variant_id"]: r["arm"] for r in db.all("experiment_arms", "experiment_id=?", [e["experiment_id"]])}
        for p in posts:
            a = arm_of.get(p.get("variant_id") or "")
            if a and not p["young"] and p["outcomes"].get(outcome) is not None:
                vals.setdefault(a, []).append(p["outcomes"][outcome])
    else:  # observed: arm label → creative value
        key = res.get("observe") or e["variable"]
        value_to_arm = {str(v): a for a, v in (res.get("arms") or {}).items()}
        for p in posts:
            a = value_to_arm.get(str((p.get("creative") or {}).get(key)))
            if a and not p["young"] and p["outcomes"].get(outcome) is not None:
                vals[a].append(p["outcomes"][outcome])
    return vals


def read(db: DB, persist: bool = True, today: date | None = None, actor: str = "growth-lab") -> list[dict]:
    """Read every running experiment; adjust p-values across all comparisons made in this read."""
    from .team_params import get as _param
    today = today or datetime.now(timezone.utc).date()
    exps = [e for e in db.all("experiments", "status IN ('running','planned') AND variable<>'frequency'") if _res(e).get("mode")]
    comps = []
    for e in exps:
        vals = _arm_values(db, e)
        labels = list(_res(e).get("arms") or vals)
        ctrl = labels[0]
        for arm in labels[1:]:
            a, b = vals.get(arm, []), vals.get(ctrl, [])
            p = permutation_p(a, b) if min(len(a), len(b)) >= MIN_N else None
            comps.append({"e": e, "arm": arm, "control": ctrl, "a": a, "b": b, "p": p})
    comps += _frequency_comparisons(db)
    tested = [c for c in comps if c["p"] is not None]
    for c, q in zip(tested, bh_adjust([c["p"] for c in tested]) if tested else []):
        c["q"] = q
    out = []
    by_exp: dict[str, list[dict]] = {}
    for c in comps:
        by_exp.setdefault(c["e"]["experiment_id"], []).append(c)
    for eid, cs in by_exp.items():
        e = cs[0]["e"]
        rows = []
        for c in cs:
            q = c.get("q")
            med_a = statistics.median(c["a"]) if c["a"] else None
            med_b = statistics.median(c["b"]) if c["b"] else None
            lift = (med_a / med_b - 1) if med_a is not None and med_b else None
            rows.append({"arm": c["arm"], "control": c["control"], "n_arm": len(c["a"]), "n_control": len(c["b"]),
                         "median_arm": med_a, "median_control": med_b, "lift": lift, "p": c["p"], "q": q,
                         "evidence": evidence_class(len(c["a"]), len(c["b"]), q)})
        weeks = (today - date.fromisoformat((_res(e).get("started_at") or e["week_start"])[:10])).days / 7
        best = max((r for r in rows if r["evidence"] in ("conclusive", "suggestive")), key=lambda r: abs(r["lift"] or 0), default=None)
        if best and best["evidence"] == "conclusive":
            status, verdict = "concluded", (f"{best['arm']} beats {best['control']}" if (best["lift"] or 0) > 0 else f"{best['control']} beats {best['arm']}")
        elif best:
            status, verdict = "running", f"leaning {best['arm'] if (best['lift'] or 0) > 0 else best['control']} (suggestive, keep going)"
        elif all(r["evidence"] == "no_clear_difference" and min(r["n_arm"], r["n_control"]) >= CONCLUSIVE_N for r in rows):
            status, verdict = "concluded", "no clear difference"
        elif weeks > (max_weeks := _param(db, "growth_lab", "max_weeks", MAX_WEEKS)):
            status, verdict = "inconclusive", f"not enough posts after {max_weeks} weeks"
        else:
            status, verdict = "running", "collecting (needs ≥5 measured posts per arm)"
        read_ = {"at": now_iso(), "outcome": outcome_of(e), "rows": rows, "verdict": verdict}
        out.append({"experiment_id": eid, "account_id": e["account_id"], "variable": e["variable"], "status": status, **read_})
        if not persist:
            continue
        targets = [e] if e["variable"] != "frequency" else [
            x for x in db.all("experiments", "variable='frequency'") if _res(x).get("series") == eid]
        for t in targets:
            res = _res(t)
            res["reads"] = (res.get("reads") or [])[-11:] + [read_]
            res["latest"] = read_
            db.update("experiments", "experiment_id", t["experiment_id"], {"status": t["status"] if e["variable"] == "frequency" and status == "running" else status,
                                                                     "result": res})
        if best:
            _write_evidence(db, e, best, outcome_of(e), actor)
    if persist and out:
        db.audit(actor, "experiments_read", "experiments", "", {"n": len(out), "concluded": sum(1 for o in out if o["status"] == "concluded")})
    return out


def _frequency_comparisons(db: DB) -> list[dict]:
    """A frequency test reads once all three weeks are in: week B (more posts) against week A + C pooled."""
    out = []
    groups: dict[str, list[dict]] = {}
    for e in db.all("experiments", "variable='frequency'"):
        groups.setdefault(_res(e).get("series", e["experiment_id"]), []).append(e)
    for series, es in groups.items():
        es.sort(key=lambda e: e["week_start"])
        if len(es) < 3 or date.fromisoformat(es[-1]["week_start"]) + timedelta(days=7 + 3) > datetime.now(timezone.utc).date():
            continue
        vals = {}
        for e in es:
            for arm, v in _arm_values(db, e).items():
                vals.setdefault(arm, []).extend(v)
        a, b = vals.get("B", []), vals.get("A", []) + vals.get("C", [])
        p = permutation_p(a, b) if min(len(a), len(b)) >= MIN_N else None
        head = {**es[0], "experiment_id": series, "variable": "frequency", "result": {"arms": {"A+C": "starting cadence", "B": "one step up"}}}
        out.append({"e": head, "arm": "B", "control": "A+C", "a": a, "b": b, "p": p})
    return out


def _write_evidence(db: DB, e: dict, row: dict, outcome: str, actor: str) -> None:
    res = _res(e)
    arms = res.get("arms") or {}
    win, lose = (row["arm"], row["control"]) if (row["lift"] or 0) > 0 else (row["control"], row["arm"])
    stmt = (f"Experiment on {e['account_id']}: {e['variable']} '{arms.get(win, win)}' beat '{arms.get(lose, lose)}' on "
            f"{outcome.replace('_', ' ')} by {abs(row['lift'] or 0):.0%} (n={row['n_arm']}/{row['n_control']}, q={row['q']:.3f}, "
            f"{row['evidence']}).")
    db.insert("knowledge", {"entry_id": f"pln-exp-{e['experiment_id']}", "level": "pln_evidence", "platform": e["platform"],
                            "brand_id": (db.get("accounts", "account_id", e["account_id"]) or {}).get("brand_id", ""),
                            "account_id": e["account_id"], "topic": e["variable"], "statement": stmt, "quote": "",
                            "source_url": "", "source_title": f"Growth Lab experiment {e['experiment_id']}", "publisher": "PLN Growth Lab",
                            "source_date": now_iso()[:10], "retrieved_at": now_iso()[:10], "reviewed_at": now_iso(), "reviewed_by": actor,
                            "status": "current", "applicability": f"{e['account_id']} ({e['platform']})",
                            "evidence": {"experiment_id": e["experiment_id"], **{k: row[k] for k in ("n_arm", "n_control", "lift", "p", "q", "evidence")},
                                         "outcome": outcome, "controlled": res.get("mode") == "assigned"},
                            "supersedes": "", "created_by": actor, "created_at": now_iso(), "updated_at": now_iso()},
              upsert_key="entry_id")


def stop(db: DB, experiment_id: str, actor: str, reason: str) -> None:
    e = db.get("experiments", "experiment_id", experiment_id)
    res = _res(e)
    res["stopped"] = {"by": actor, "reason": reason, "at": now_iso()}
    db.update("experiments", "experiment_id", experiment_id, {"status": "abandoned", "result": res})
    db.audit(actor, "experiment_stopped", "experiments", experiment_id, {"reason": reason})


def refresh(db: DB, actor: str = "growth-lab") -> dict:
    """Worker step: top up assignments for new drafts, then read everything."""
    n = sum(len(assign(db, e["experiment_id"], actor)) for e in db.all("experiments", "status='running'")
            if _res(e).get("mode") == "assigned")
    r = read(db, actor=actor)
    return {"assigned": n, "read": len(r), "concluded": sum(1 for x in r if x["status"] == "concluded")}
