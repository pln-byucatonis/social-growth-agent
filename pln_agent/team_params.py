"""Manager overrides for agent parameters (Agent Team page). Each agent reads its tunable numbers through
`get`, so a course correction takes effect on the agent's next run without a deploy. Kept dependency-free
so any agent module can import it."""
from __future__ import annotations

import json

from .db import DB


def _key(agent: str, name: str) -> str:
    return f"team:param:{agent}:{name}"


def get(db: DB | None, agent: str, name: str, default):
    if db is None:
        return default
    try:
        raw = db.kv_get(_key(agent, name))
    except Exception:  # noqa: BLE001 — a missing settings table must never break an agent
        return default
    if raw is None:
        return default
    v = json.loads(raw)
    return type(default)(v) if isinstance(default, (int, float)) and not isinstance(default, bool) else v


def overrides(db: DB, agent: str) -> dict:
    pre = f"team:param:{agent}:"
    return {r["key"][len(pre):]: json.loads(r["value"]) for r in db.kv_prefix(pre)}


def put(db: DB, agent: str, name: str, value, actor: str) -> None:
    db.kv_set(_key(agent, name), json.dumps(value), actor)


def clear(db: DB, agent: str, name: str, actor: str) -> None:
    db.execute("DELETE FROM settings_kv WHERE key=?", [_key(agent, name)])
    db.audit(actor, "team_param_reset", "settings_kv", _key(agent, name), {})
