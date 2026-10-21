"""Agent Team: see each agent's charter and live rules, course-correct it, talk to it, or challenge the team.

Two kinds of course correction, and the page says which is which:
* **Parameters** — numbers and switches the agent's code reads on its next run (team_params). These change
  behaviour deterministically: e.g. the CGO's weekly allocation shift cap or the migration decline threshold.
* **Directives** — written guidance from the manager. Every conversation and team challenge carries the
  agent's active directives, so its recommendations follow them; they don't rewrite code paths.

Talking to agents uses Claude (Amazon Bedrock, through the service's own AWS role). Each agent is grounded in
a snapshot of its own data and rules, and is told to say when the data doesn't support an answer. A team
challenge runs in three steps: every agent answers from its own data, agents answer questions other agents
asked them (the liaison round), then the Chief Growth Officer synthesises one recommendation with owners.
Nothing an agent says is executed: the manager adopts an action, which becomes a directive (and, when it
names a parameter, a proposed parameter change that still needs its own click).
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

from . import team_params
from .db import DB, now_iso

MODEL = os.environ.get("PLN_TEAM_MODEL", "us.anthropic.claude-sonnet-4-6")
REGION = os.environ.get("PLN_TEAM_REGION", os.environ.get("AWS_REGION", "us-east-1"))
CONTEXT_CHARS = 9000


def _num(label, default, lo, hi, step=None):
    return {"label": label, "default": default, "min": lo, "max": hi, "type": "number", "step": step or (1 if isinstance(default, int) else 0.01)}


def _bool(label, default):
    return {"label": label, "default": default, "type": "bool"}


# ---- context snapshots (each agent's own data) ----------------------------------------------------

def _safe(fn):
    try:
        return fn()
    except Exception as e:  # noqa: BLE001 — a missing data source shouldn't break the page
        return {"unavailable": str(e)[:200]}


def _ctx_cgo(db):
    from .growth_officer import current_shares, latest_brief
    b = latest_brief(db) or {}
    return {"week": b.get("week_start"), "actions": b.get("actions", [])[:15],
            "scoreboard": [{k: r.get(k) for k in ("brand_id", "tier", "followers", "follows", "unfollows", "net", "reach", "follows_per_1k_reach", "last_post")}
                           for r in b.get("scoreboard", [])],
            "effort_split": current_shares(db), "allocation_proposal": (b.get("allocation") or {}).get("changes")}


def _ctx_audience(db):
    from .audience import portfolio
    return {"accounts": [{**{k: a.get(k) for k in ("account_id", "followers", "follows_28d", "unfollows_28d", "days_since_post")},
                          "kpis": {k: (v or {}).get("value") for k, v in (a.get("kpis") or {}).items() if (v or {}).get("value") is not None}}
                         for a in portfolio(db)][:14]}


def _ctx_scout(db):
    from .scout import sources, top_moments
    return {"top_moments": [{k: m.get(k) for k in ("brand_id", "caption", "content_type", "potential", "status")} for m in top_moments(db, n=12)],
            "sources": [{k: s.get(k) for k in ("source_id", "brand_id", "moments", "best")} for s in sources(db)][:12]}


def _ctx_creative(db):
    rows = db.query("SELECT entry_id, statement, applicability FROM knowledge WHERE level='pln_evidence' AND status='current'")[:12]
    pk = db.query("SELECT package_id, asset_id, created_at FROM creative_packages ORDER BY created_at DESC")[:8]
    return {"pln_evidence": rows, "recent_packages": pk}


def _ctx_video(db):
    out = {"render_status": {r["render_status"]: r["n"] for r in db.query("SELECT render_status, COUNT(*) AS n FROM variants GROUP BY render_status")}}
    out["qc_failures"] = [{"variant_id": v["variant_id"], "errors": (v.get("render_qc") or {}).get("errors")}
                          for v in db.all("variants", "render_status='rendered'") if not (v.get("render_qc") or {}).get("passed", True)][:10]
    return out


def _ctx_publishing(db):
    rows = db.all("cadence")
    return {"cadence": [{k: r.get(k) for k in ("account_id", "action", "recommended_per_week", "confidence", "rationale")} for r in rows][:16],
            "frequency_tests": [{k: e.get(k) for k in ("account_id", "week_start", "status", "treatment")} for e in db.all("experiments", "variable='frequency'")][:9]}


def _ctx_community(db):
    from .community import faqs, summary
    return {"last_7_days": summary(db), "faqs": faqs(db)[:6],
            "escalated": [{k: c.get(k) for k in ("account_id", "text", "escalation")} for c in db.all("comments", "status='escalated'")][:8]}


def _ctx_competitive(db):
    from .competitive import gaps, latest, standouts
    return {"benchmarks": [{"handle": b["handle"], **{k: (b["profile"] or {}).get(k) for k in ("posts_per_week", "reels_share", "median_er", "growth")}} for b in latest(db)],
            "gaps": gaps(db)[:8], "standouts": standouts(db, limit=6)}


def _ctx_lab(db):
    exps = [e for e in db.all("experiments") if (e.get("result") or {}).get("mode")]
    return {"experiments": [{"id": e["experiment_id"], "account": e["account_id"], "variable": e["variable"], "status": e["status"],
                             "verdict": ((e.get("result") or {}).get("latest") or {}).get("verdict")} for e in exps][:12]}


def _ctx_migration(db):
    from .migration_tracking import track
    t = track(db, persist=False)
    return {"alerts": t["alerts"], "platforms": [{"platform": p["platform"], "option": p["option"], "recommendation": p["recommendation"],
                                                  "legacy": {k: (p["legacy"] or {}).get(k) for k in ("handle", "followers", "change_28d", "days_since_post")},
                                                  "canonical": {k: (p["canonical"] or {}).get(k) for k in ("handle", "followers", "change_28d", "days_since_post")} if p["canonical"] else None,
                                                  "bridge_posts": p["bridge"]["bridge_posts"]} for p in t["platforms"]]}


AGENTS = [
    {"key": "cgo", "name": "Chief Growth Officer", "layer": "strategy", "page": "/growth",
     "mission": "Decides where effort goes across brands each week, by priority tier and measured follower efficiency, and turns the team's findings into one ranked action list.",
     "rules": ["Priority tiers P0 PLN / CarJitsu / Putt Tour, P1 WPL consolidation, P2 SlapFIGHT / STR33T / Pro Carrom, P3 the rest; every tier keeps a floor share.",
               "Moves effort toward the brands earning the most follows per 1k reach, never more than the weekly shift cap, and only once reach passes the efficiency minimum.",
               "Flags any P0/P1 brand silent longer than the posting-gap limit, negative net followers, and thin footage supply.",
               "Reallocation is a proposal until you approve it."],
     "params": {"max_shift_per_week": _num("Max effort shift per week", 0.05, 0.0, 0.3), "posting_gap_days": _num("Posting gap alert (days)", 7, 1, 60),
                "min_reach_for_efficiency": _num("Reach needed before efficiency counts", 5000, 0, 1_000_000, 500)},
     "context": _ctx_cgo},
    {"key": "audience", "name": "Audience Intelligence", "layer": "intelligence", "page": "/intelligence",
     "mission": "Measures each account: reach split by followers / non-followers, follows and unfollows, watch time, per-1k-view rates.",
     "rules": ["Every KPI is labelled measured, derived or unavailable; nothing unavailable is estimated.",
               "Follows per 1k views are measured only where the platform reports post-level follows (Instagram feed posts).",
               "Pulls Instagram + YouTube insights through the connected accounts and the ops social ingest every 6 hours."],
     "params": {}, "context": _ctx_audience},
    {"key": "scout", "name": "Viral Content Scout", "layer": "creative", "page": "/scout",
     "mission": "Scores every clipped moment in the ops library for scroll-stopping potential and surfaces the strongest unused ones per brand.",
     "rules": ["Score = category strength × brand fit × duration fit × learned lift from PLN evidence.",
               "Injury / knockout moments are penalised by the safety multiplier, sent to manual review, and never used as the hook.",
               "Overlapping clips from the same source are de-duplicated."],
     "params": {"safety_multiplier": _num("Safety penalty multiplier (injury/KO)", 0.6, 0.0, 1.0)}, "context": _ctx_scout},
    {"key": "creative", "name": "Creative Director", "layer": "creative", "page": "/creative",
     "mission": "Builds the platform-native treatment for each post: hook, opening beat, A/B/C cuts, captions and CTA.",
     "rules": ["Uses only facts in the source footage and metadata; no invented names, scores or claims.",
               "Prefers what PLN evidence shows works for that brand and platform; otherwise the platform guidance; otherwise flags it as a hypothesis.",
               "Offers three cuts (≈12s / ≈25s / ≈40s) and says honestly when the footage can't support one."],
     "params": {}, "context": _ctx_creative},
    {"key": "video", "name": "Video Production", "layer": "creative", "page": "/queue",
     "mission": "Renders the cuts — through the ops vertical pipeline (MediaConvert smart crop) where available, FFmpeg otherwise — and checks quality.",
     "rules": ["QC blocks publishing on: over platform max duration, wrong size, opening on black.",
               "QC warns on: long black, ≥2s silence, loudness outside −20…−8 LUFS, true peak above −1 dB.",
               "Text stays out of the platform UI safe zones (top 12%, bottom 20%)."],
     "params": {}, "context": _ctx_video},
    {"key": "publishing", "name": "Publishing Strategist", "layer": "distribution", "page": "/cadence",
     "mission": "Sets how often and when each account posts, from measured incremental followers per extra post and learned posting windows.",
     "rules": ["Starting cadence is a hypothesis; after ≥4 measured weeks it follows followers per extra post, engagement dilution and supply.",
               "Moves at most the weekly step; never above supply.",
               "Learns 2-hour windows with the evidence rule and keeps one exploration slot so it keeps learning.",
               "Frequency tests run three weeks: A start, B one step up, C start re-spaced."],
     "params": {"max_step_per_week": _num("Max cadence change per week (posts)", 2, 0, 14), "exploration_slot": _bool("Keep an exploration slot", True)},
     "context": _ctx_publishing},
    {"key": "community", "name": "Community Manager", "layer": "distribution", "page": "/community",
     "mission": "Reads comments, escalates safety / legal / threats / sponsor enquiries, drafts replies for approval and turns repeated questions into content ideas.",
     "rules": ["Escalated categories never get a drafted public reply.",
               "Drafts use only links set in config; missing links are marked, never invented.",
               "Nothing is sent without approval; sending follows the same live gates as publishing."],
     "params": {"lookback_days": _num("Comment lookback (days)", 14, 1, 90), "faq_min_people": _num("People asking before it's an FAQ", 3, 2, 50)},
     "context": _ctx_community},
    {"key": "competitive", "name": "Competitive Intelligence", "layer": "intelligence", "page": "/competitors",
     "mission": "Tracks benchmark accounts' public numbers and standout posts, and where our accounts differ most.",
     "rules": ["Comparison metric is engagement per follower, calculated the same way for them and us.",
               "Benchmarks are external research, never evidence for PLN — anything worth copying goes to the Growth Lab first."],
     "params": {"min_posts": _num("Posts needed before a benchmark counts", 6, 1, 30)}, "context": _ctx_competitive},
    {"key": "growth_lab", "name": "Growth Experimentation", "layer": "optimization", "page": "/experiments",
     "mission": "Runs controlled tests (cuts, hooks, observed variables, frequency) and writes conclusive results back as PLN evidence.",
     "rules": ["≥5 measured posts per arm before anything is said; ≥8 and q<0.05 for conclusive; per-1k-view rates only.",
               "Assigned arms only touch drafts nobody has approved.",
               "Experiments without enough posts after the time limit close as inconclusive."],
     "params": {"max_weeks": _num("Weeks before an under-sampled test closes", 6, 1, 26)}, "context": _ctx_lab},
    {"key": "migration", "name": "Audience Migration", "layer": "distribution", "page": "/migration",
     "mission": "Tracks WPL → Putt Tour on every platform: follower trends, bridge posts, decline alerts, and which account the numbers say should be home.",
     "rules": ["No follower transfer is assumed; bridge results are associations with their sample size.",
               "Nothing is renamed, deleted or edited automatically; profile changes are proposals an admin applies by hand."],
     "params": {"decline_pct_28d": _num("Decline alert threshold (28-day %)", 0.02, 0.0, 0.5), "silent_days": _num("Silent-account alert (days)", 14, 1, 90),
                "bridge_every_days": _num("Bridge post due every (days)", 7, 1, 60)},
     "context": _ctx_migration},
]
BY_KEY = {a["key"]: a for a in AGENTS}


def roster(db: DB) -> list[dict]:
    out = []
    for a in AGENTS:
        ov = team_params.overrides(db, a["key"])
        params = [{"key": k, **spec, "value": ov.get(k, spec["default"]), "overridden": k in ov} for k, spec in a["params"].items()]
        out.append({**{k: a[k] for k in ("key", "name", "layer", "page", "mission", "rules")}, "params": params,
                    "directives": directives(db, a["key"])})
    return out


# ---- course correction ----------------------------------------------------------------------------

def set_param(db: DB, agent: str, name: str, raw: str, actor: str) -> object:
    spec = BY_KEY[agent]["params"].get(name)
    if not spec:
        raise ValueError(f"{agent} has no parameter {name}")
    if spec["type"] == "bool":
        value = str(raw).lower() in ("1", "true", "on", "yes")
    else:
        value = float(raw)
        if not spec["min"] <= value <= spec["max"]:
            raise ValueError(f"{spec['label']} must be between {spec['min']} and {spec['max']}")
        if isinstance(spec["default"], int):
            value = int(round(value))
    team_params.put(db, agent, name, value, actor)
    db.audit(actor, "team_param_set", "agents", agent, {"param": name, "value": value, "default": spec["default"]})
    return value


def add_directive(db: DB, agent: str, text: str, actor: str, source: str = "manager", thread_id: str = "") -> dict:
    if agent != "team" and agent not in BY_KEY:
        raise ValueError(f"unknown agent {agent}")
    text = (text or "").strip()
    if not text:
        raise ValueError("directive text is empty")
    d = {"directive_id": f"dir-{uuid.uuid4().hex[:8]}", "agent_key": agent, "text": text[:2000], "status": "active",
         "source": source, "thread_id": thread_id, "created_by": actor, "created_at": now_iso()}
    db.insert("agent_directives", d)
    db.audit(actor, "directive_added", "agents", agent, {"text": d["text"], "source": source})
    return d


def retire_directive(db: DB, directive_id: str, actor: str) -> None:
    db.update("agent_directives", "directive_id", directive_id, {"status": "retired"})
    db.audit(actor, "directive_retired", "agent_directives", directive_id, {})


def directives(db: DB, agent: str) -> list[dict]:
    return db.all("agent_directives", "status='active' AND agent_key IN (?, 'team')", [agent], order="created_at")


# ---- LLM ------------------------------------------------------------------------------------------

class TeamLLMError(Exception):
    pass


def bedrock_call(system: str, messages: list[dict], max_tokens: int = 2500) -> str:
    import boto3
    from botocore.exceptions import BotoCoreError, ClientError
    body = {"anthropic_version": "bedrock-2023-05-31", "max_tokens": max_tokens, "system": system, "messages": messages}
    try:
        r = boto3.client("bedrock-runtime", region_name=REGION).invoke_model(
            modelId=MODEL, body=json.dumps(body), contentType="application/json", accept="application/json")
    except (ClientError, BotoCoreError) as e:
        raise TeamLLMError(str(e)[:300]) from e
    data = json.loads(r["body"].read())
    if data.get("stop_reason") == "refusal":
        raise TeamLLMError("the model declined this request")
    return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text").strip()


_llm = bedrock_call


def set_llm(fn) -> None:
    """Tests inject a fake; None restores Bedrock."""
    global _llm
    _llm = fn or bedrock_call


TEAM_RULES = ("You work for Pro League Network (PLN), a network of novelty sports leagues (CarJitsu, Putt Tour, SlapFIGHT, STR33T, "
              "Pro Carrom Tour ...). You are one agent in its social growth team. Ground every claim in the DATA below; if the data "
              "doesn't support an answer, say what is missing instead of guessing. Distinguish measured numbers from assumptions. "
              "You can't publish, reply, rename or change settings yourself — you recommend, and the manager decides. Follow the "
              "manager's directives. Be concise and concrete: plain sentences, numbers where you have them.")


def _system(db: DB, a: dict) -> str:
    ctx = json.dumps(_safe(lambda: a["context"](db)), default=str)[:CONTEXT_CHARS]
    ov = team_params.overrides(db, a["key"])
    params = {k: ov.get(k, s["default"]) for k, s in a["params"].items()}
    dirs = [d["text"] for d in directives(db, a["key"])]
    team = "; ".join(f"{x['name']} — {x['mission']}" for x in AGENTS if x["key"] != a["key"])
    return (f"{TEAM_RULES}\n\nYOU ARE: {a['name']}. Mission: {a['mission']}\nYOUR RULES:\n- " + "\n- ".join(a["rules"])
            + f"\nYOUR PARAMETERS (current): {json.dumps(params)}\nMANAGER DIRECTIVES:\n- " + ("\n- ".join(dirs) if dirs else "(none)")
            + f"\nTEAMMATES: {team}\n\nDATA (your latest snapshot, {now_iso()[:10]}):\n{ctx}")


def _fallback_answer(db: DB, a: dict, why: str) -> str:
    ctx = _safe(lambda: a["context"](db))
    return (f"(Claude isn't reachable: {why}. Here is my current data instead.)\n\n"
            + json.dumps(ctx, default=str, indent=1)[:3000])


# ---- conversations --------------------------------------------------------------------------------

def _thread(db: DB, kind: str, agent: str, title: str, actor: str) -> dict:
    t = {"thread_id": f"th-{uuid.uuid4().hex[:10]}", "kind": kind, "agent_key": agent, "title": title[:200], "status": "open",
         "created_by": actor, "created_at": now_iso(), "result": {}}
    db.insert("team_threads", t)
    return t


def _msg(db: DB, thread_id: str, role: str, agent: str, content: str, meta: dict | None = None) -> None:
    db.insert("team_messages", {"thread_id": thread_id, "role": role, "agent_key": agent, "content": content,
                                "created_at": now_iso(), "meta": meta or {}})


def messages(db: DB, thread_id: str) -> list[dict]:
    return db.all("team_messages", "thread_id=?", [thread_id], order="id")


def chat(db: DB, agent: str, text: str, actor: str, thread_id: str = "") -> dict:
    a = BY_KEY[agent]
    t = db.get("team_threads", "thread_id", thread_id) if thread_id else None
    if not t:
        t = _thread(db, "chat", agent, text, actor)
    _msg(db, t["thread_id"], "manager", agent, text, {"by": actor})
    hist = [{"role": "user" if m["role"] == "manager" else "assistant", "content": m["content"]}
            for m in messages(db, t["thread_id"]) if m["agent_key"] == agent][-12:]
    try:
        answer = _llm(_system(db, a), hist)
        meta = {"model": MODEL}
    except TeamLLMError as e:
        answer, meta = _fallback_answer(db, a, str(e)), {"fallback": True}
    _msg(db, t["thread_id"], "agent", agent, answer, meta)
    db.audit(actor, "agent_chat", "team_threads", t["thread_id"], {"agent": agent})
    return t


PERSPECTIVE = ("The manager has put this challenge to the whole team:\n\n{challenge}\n\nAnswer from your own role and data. Return JSON only: "
               '{{"position": "2-4 sentences", "recommendations": [{{"action": "...", "why": "...", "evidence": "numbers from your data or \'no data\'", '
               '"param_change": {{"agent": "<agent key>", "param": "<name>", "value": <number|bool>}} or null}}], '
               '"asks": [{{"agent": "<teammate key>", "question": "..."}}], "confidence": "low|medium|high"}}. '
               "Teammate keys: {keys}. At most 3 recommendations and 2 asks.")
LIAISON = ("Challenge: {challenge}\n\nTeammates asked you:\n{questions}\n\nAnswer each from your data, briefly. Return JSON only: "
           '{{"answers": [{{"from": "<agent key>", "question": "...", "answer": "..."}}]}}')
SYNTH = ("Challenge from the manager:\n{challenge}\n\nTeam perspectives (JSON):\n{perspectives}\n\nLiaison answers:\n{liaison}\n\n"
         "As Chief Growth Officer, synthesise ONE recommendation for the manager. Resolve disagreements by priority tier and evidence "
         "strength; say which ones you couldn't resolve. Return JSON only: "
         '{{"summary": "3-5 sentences", "actions": [{{"action": "...", "owner": "<agent key>", "why": "...", "evidence": "...", '
         '"priority": "P0|P1|P2", "param_change": {{"agent": "...", "param": "...", "value": ...}} or null}}], '
         '"disagreements": ["..."], "open_questions": ["..."], "needs_from_manager": ["..."]}}. At most 6 actions.')


def _json(text: str) -> dict:
    s = text.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1].rsplit("```", 1)[0]
    a, b = s.find("{"), s.rfind("}")
    if a < 0 or b < 0:
        raise ValueError("no JSON object in reply")
    return json.loads(s[a:b + 1])


def _ask(system: str, prompt: str, max_tokens: int = 1800) -> dict:
    """One LLM call; no database access, so several can run in parallel."""
    try:
        raw = _llm(system, [{"role": "user", "content": prompt}], max_tokens)
    except TeamLLMError as e:
        return {"error": str(e)}
    try:
        return _json(raw)
    except (ValueError, json.JSONDecodeError):
        return {"position": raw[:1500], "recommendations": [], "asks": [], "unparsed": True}


def start_challenge(db: DB, text: str, actor: str, agents: list[str] | None = None, background: bool = True,
                    db_factory=None) -> dict:
    """background=True runs the rounds on their own thread and, given `db_factory`, their own connection."""
    t = _thread(db, "challenge", "team", text, actor)
    _msg(db, t["thread_id"], "manager", "team", text, {"by": actor, "agents": agents or [a["key"] for a in AGENTS]})
    db.update("team_threads", "thread_id", t["thread_id"], {"status": "running"})
    db.audit(actor, "team_challenge", "team_threads", t["thread_id"], {"text": text[:300]})
    if background:
        def work():
            run_challenge(db_factory() if db_factory else db, t["thread_id"], text, agents)
        threading.Thread(target=work, daemon=True, name="team-challenge").start()
    else:
        run_challenge(db, t["thread_id"], text, agents)
    return t


def run_challenge(db: DB, thread_id: str, text: str, agents: list[str] | None = None) -> dict:
    members = [BY_KEY[k] for k in (agents or [a["key"] for a in AGENTS]) if k in BY_KEY]
    keys = ", ".join(a["key"] for a in AGENTS)
    try:
        systems = {a["key"]: _system(db, a) for a in AGENTS}  # all database reads happen here, before the parallel calls
        with ThreadPoolExecutor(max_workers=5) as ex:
            persp = dict(zip([a["key"] for a in members],
                             ex.map(lambda a: _ask(systems[a["key"]], PERSPECTIVE.format(challenge=text, keys=keys)), members)))
        if all("error" in p for p in persp.values()):
            err = next(iter(persp.values()))["error"]
            for a in members:
                _msg(db, thread_id, "agent", a["key"], _fallback_answer(db, a, err), {"fallback": True, "round": "perspective"})
            db.update("team_threads", "thread_id", thread_id, {"status": "done", "result": {"error": err}})
            return {"error": err}
        for k, p in persp.items():
            _msg(db, thread_id, "agent", k, p.get("position") or p.get("error", ""), {"round": "perspective", "data": p})
        asks: dict[str, list[dict]] = {}
        for k, p in persp.items():
            for q in (p.get("asks") or [])[:2]:
                if q.get("agent") in BY_KEY and q.get("agent") != k:
                    asks.setdefault(q["agent"], []).append({"from": k, "question": q.get("question", "")})
        liaison = {}
        if asks:
            with ThreadPoolExecutor(max_workers=5) as ex:
                items = list(asks.items())
                res = ex.map(lambda kv: _ask(systems[kv[0]], LIAISON.format(
                    challenge=text, questions="\n".join(f"- {q['from']}: {q['question']}" for q in kv[1])), 1200), items)
                for (k, _), r in zip(items, res):
                    liaison[k] = r
                    for ans in (r.get("answers") or []):
                        _msg(db, thread_id, "agent", k, f"→ {ans.get('from')}: {ans.get('answer', '')}", {"round": "liaison", "data": ans})
        synth = _ask(systems["cgo"], SYNTH.format(challenge=text, perspectives=json.dumps(persp, default=str)[:14000],
                                                     liaison=json.dumps(liaison, default=str)[:6000]), 3000)
        _msg(db, thread_id, "agent", "cgo", synth.get("summary") or synth.get("position") or synth.get("error", ""), {"round": "synthesis", "data": synth})
        db.update("team_threads", "thread_id", thread_id, {"status": "done", "result": synth})
        return synth
    except Exception as e:  # noqa: BLE001 — the thread must not stay "running" forever
        db.update("team_threads", "thread_id", thread_id, {"status": "failed", "result": {"error": str(e)[:300]}})
        raise


def adopt(db: DB, thread_id: str, index: int, actor: str, apply_param: bool = False) -> dict:
    """Turn one synthesised action into a directive for its owner (and optionally apply its parameter change)."""
    t = db.get("team_threads", "thread_id", thread_id)
    acts = (t.get("result") or {}).get("actions") or []
    if not 0 <= index < len(acts):
        raise ValueError("no such action")
    act = acts[index]
    owner = act.get("owner") if act.get("owner") in BY_KEY else "team"
    d = add_directive(db, owner, f"{act.get('action')} (why: {act.get('why', '')})", actor, source="challenge", thread_id=thread_id)
    out = {"directive": d}
    pc = act.get("param_change") or {}
    if apply_param and pc.get("agent") in BY_KEY and pc.get("param"):
        out["param"] = set_param(db, pc["agent"], pc["param"], str(pc.get("value")), actor)
    res = dict(t["result"])
    res.setdefault("adopted", []).append(index)
    db.update("team_threads", "thread_id", thread_id, {"result": res})
    return out


def threads(db: DB, agent: str = "", limit: int = 20) -> list[dict]:
    where, params = ("agent_key=?", [agent]) if agent else ("1=1", [])
    return db.all("team_threads", where, params, order="created_at DESC")[:limit]


def llm_status() -> dict:
    return {"model": MODEL, "region": REGION}


def status_line(db: DB, agent: str) -> str:
    """One line of 'what this agent knows right now' for the roster card."""
    try:
        c = BY_KEY[agent]["context"](db)
    except Exception as e:  # noqa: BLE001
        return f"data unavailable ({str(e)[:80]})"
    if agent == "cgo":
        return f"{len(c.get('actions') or [])} actions in the {c.get('week') or 'current'} brief"
    if agent == "community":
        s = c["last_7_days"]
        return f"{s['comments']} comments in 7 days · {s['open_escalations']} escalated · {s['awaiting_approval']} awaiting approval"
    if agent == "migration":
        return f"{len(c['alerts'])} alert(s)"
    if agent == "growth_lab":
        return f"{sum(1 for e in c['experiments'] if e['status'] == 'running')} running · {sum(1 for e in c['experiments'] if e['status'] == 'concluded')} concluded"
    if agent == "competitive":
        return f"{len(c['benchmarks'])} benchmarks · {len(c['gaps'])} gap(s)"
    if agent == "publishing":
        return f"{len(c['cadence'])} accounts with a cadence · {len(c['frequency_tests'])} frequency-test weeks"
    if agent == "scout":
        return f"{len(c['top_moments'])} top moments ready"
    if agent == "audience":
        return f"{len(c['accounts'])} accounts measured"
    if agent == "video":
        return ", ".join(f"{k or 'none'} {v}" for k, v in c["render_status"].items()) + (f" · {len(c['qc_failures'])} QC failures" if c["qc_failures"] else "")
    if agent == "creative":
        return f"{len(c['pln_evidence'])} PLN evidence entries · {len(c['recent_packages'])} recent packages"
    return ""

