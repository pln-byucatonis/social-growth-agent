"""Durable storage. One table definition renders to SQLite (local dev / tests) and Postgres DDL.

SQLite is the runtime default. `python -m pln_agent schema --dialect postgres` emits the
migration-ready Postgres DDL (also committed at migrations/postgres/001_init.sql). Runtime Postgres
is supported through psycopg when installed and PLN_DATABASE_URL starts with postgres://.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

# column spec: (name, logical_type, constraints). logical types: text, int, real, bool, json, ts
TABLES: dict[str, list[tuple[str, str, str]]] = {
    "brands": [
        ("brand_id", "text", "PRIMARY KEY"),
        ("name", "text", "NOT NULL"),
        ("priority", "text", ""),
        ("legacy_of", "text", ""),  # brand_id this legacy brand now belongs to
        ("is_legacy", "bool", "NOT NULL DEFAULT 0"),
        ("voice", "json", ""),
        ("notes", "text", ""),
    ],
    "accounts": [
        ("account_id", "text", "PRIMARY KEY"),
        ("brand_id", "text", "NOT NULL"),  # brand the account is branded as today (e.g. world_putting_league)
        ("canonical_brand_id", "text", "NOT NULL"),  # brand it serves (e.g. putt_tour)
        ("source_brand_name", "text", ""),
        ("platform", "text", "NOT NULL"),
        ("username_original", "text", ""),  # exactly as supplied, never rewritten
        ("username_normalized", "text", ""),
        ("stable_platform_id", "text", ""),  # filled only after OAuth (page id, urn, open_id...)
        ("priority", "text", ""),
        ("role", "text", "NOT NULL"),  # canonical | secondary_unverified | legacy_bridge | creator_unverified | pending_placeholder
        ("supplied_status", "text", ""),
        ("verification_status", "text", "NOT NULL"),  # unverified | ownership_confirmed | oauth_connected
        ("ownership_owner", "text", ""),
        ("credential_ref", "text", ""),  # e.g. env:LINKEDIN_TOKEN_PLN — never the secret itself
        ("publishing_authorized", "bool", "NOT NULL DEFAULT 0"),
        ("publishing_policy", "text", "NOT NULL DEFAULT 'approval_required'"),
        ("hard_block_reason", "text", ""),
        ("redirect_to_account_id", "text", ""),
        ("campaign", "text", ""),
        ("notes", "text", ""),
        ("source", "text", "NOT NULL"),  # user_csv | system_placeholder
        ("csv_row", "int", ""),
        ("created_at", "ts", ""),
        ("updated_at", "ts", ""),
    ],
    "assets": [
        ("asset_id", "text", "PRIMARY KEY"),
        ("brand_id", "text", "NOT NULL"),  # canonical brand
        ("source_brand_name", "text", ""),  # e.g. "World Putting League" for legacy archive
        ("title", "text", ""),
        ("description", "text", ""),
        ("media_path", "text", ""),
        ("media_present", "bool", "NOT NULL DEFAULT 0"),
        ("sha256", "text", ""),
        ("content_id", "text", ""),
        ("parent_asset_id", "text", ""),
        ("provenance", "text", ""),
        ("owner", "text", ""),
        ("license", "text", ""),
        ("territory", "json", ""),
        ("rights_expires_at", "text", ""),
        ("rights_verified", "bool", "NOT NULL DEFAULT 0"),
        ("people_verified", "bool", "NOT NULL DEFAULT 0"),
        ("music_cleared", "bool", "NOT NULL DEFAULT 0"),
        ("sponsors", "json", ""),
        ("sponsor_approved", "bool", "NOT NULL DEFAULT 0"),
        ("people", "json", ""),
        ("facts", "json", ""),
        ("moments", "json", ""),
        ("flags", "json", ""),
        ("series", "text", ""),
        ("event_name", "text", ""),
        ("event_date", "text", ""),
        ("duration_s", "real", ""),
        ("width", "int", ""),
        ("height", "int", ""),
        ("has_audio", "bool", ""),
        ("probe", "json", ""),
        ("transcript", "json", ""),
        ("is_sample", "bool", "NOT NULL DEFAULT 0"),
        ("ingested_at", "ts", ""),
    ],
    "editorial_packs": [
        ("pack_id", "text", "PRIMARY KEY"),
        ("asset_id", "text", "NOT NULL"),
        ("created_at", "ts", ""),
        ("summary", "json", ""),
    ],
    "variants": [
        ("variant_id", "text", "PRIMARY KEY"),
        ("pack_id", "text", ""),
        ("asset_id", "text", "NOT NULL"),
        ("account_id", "text", "NOT NULL"),
        ("platform", "text", "NOT NULL"),
        ("brand_id", "text", "NOT NULL"),
        ("route_role", "text", ""),  # canonical | pln_amplification | legacy_bridge
        ("series", "text", ""),
        ("format", "text", ""),
        ("hook", "text", ""),
        ("hook_candidates", "json", ""),
        ("title", "text", ""),
        ("caption", "text", ""),
        ("hashtags", "json", ""),
        ("hashtag_rationale", "text", ""),
        ("voiceover", "text", ""),
        ("onscreen_text", "json", ""),
        ("edl", "json", ""),
        ("thumbnail", "json", ""),
        ("cta", "text", ""),
        ("angle", "text", ""),
        ("test_hypothesis", "text", ""),
        ("utm_url", "text", ""),
        ("score", "real", ""),
        ("score_breakdown", "json", ""),
        ("risk_flags", "json", ""),
        ("validation", "json", ""),
        ("state", "text", "NOT NULL"),
        ("caption_hash", "text", ""),
        ("render_status", "text", ""),
        ("render_path", "text", ""),
        ("created_at", "ts", ""),
        ("updated_at", "ts", ""),
    ],
    "approvals": [
        ("id", "pk", ""),
        ("variant_id", "text", "NOT NULL"),
        ("decision", "text", "NOT NULL"),  # approve | reject | request_changes | revoke
        ("reviewer", "text", "NOT NULL"),
        ("note", "text", ""),
        ("caption_hash", "text", ""),  # approval binds to exact text; edits invalidate it
        ("at", "ts", ""),
    ],
    "schedules": [
        ("schedule_id", "text", "PRIMARY KEY"),
        ("variant_id", "text", "NOT NULL"),
        ("account_id", "text", "NOT NULL"),
        ("slot_at", "ts", "NOT NULL"),
        ("state", "text", "NOT NULL"),  # planned | scheduled | cancelled | done | missed
        ("reason", "text", ""),
        ("created_at", "ts", ""),
    ],
    "publish_jobs": [
        ("job_id", "text", "PRIMARY KEY"),
        ("variant_id", "text", "NOT NULL"),
        ("account_id", "text", "NOT NULL"),
        ("idempotency_key", "text", "NOT NULL UNIQUE"),
        ("state", "text", "NOT NULL"),  # queued | publishing | published | simulated | failed | blocked | cancelled
        ("mode", "text", "NOT NULL"),  # dry_run | live
        ("attempts", "int", "NOT NULL DEFAULT 0"),
        ("next_attempt_at", "ts", ""),
        ("locked_at", "ts", ""),
        ("last_error", "text", ""),
        ("platform_post_id", "text", ""),
        ("permalink", "text", ""),
        ("receipt", "json", ""),
        ("remote_status", "text", ""),
        ("created_at", "ts", ""),
        ("updated_at", "ts", ""),
    ],
    "publish_attempts": [
        ("id", "pk", ""),
        ("job_id", "text", "NOT NULL"),
        ("at", "ts", ""),
        ("outcome", "text", ""),
        ("error_kind", "text", ""),
        ("error", "text", ""),
        ("response", "json", ""),
    ],
    "metrics": [
        ("id", "pk", ""),
        ("account_id", "text", "NOT NULL"),
        ("variant_id", "text", ""),
        ("platform_post_id", "text", ""),
        ("platform", "text", "NOT NULL"),
        ("metric", "text", "NOT NULL"),  # platform-native name, kept verbatim
        ("canonical_metric", "text", ""),  # mapped family (views, reach, shares...) for cross-reference only
        ("value", "real", ""),
        ("checkpoint", "text", ""),  # 2h | 24h | 72h | 7d | snapshot
        ("period_start", "text", ""),
        ("period_end", "text", ""),
        ("source", "text", "NOT NULL"),  # manual_csv | api
        ("captured_at", "ts", ""),
    ],
    "feedback": [
        ("id", "pk", ""),
        ("variant_id", "text", ""),
        ("kind", "text", ""),
        ("text", "text", ""),
        ("author", "text", ""),
        ("at", "ts", ""),
    ],
    "audit_log": [
        ("id", "pk", ""),
        ("at", "ts", ""),
        ("actor", "text", ""),
        ("action", "text", "NOT NULL"),
        ("entity", "text", ""),
        ("entity_id", "text", ""),
        ("detail", "json", ""),
    ],
    "settings_kv": [
        ("key", "text", "PRIMARY KEY"),
        ("value", "text", ""),
        ("updated_at", "ts", ""),
        ("updated_by", "text", ""),
    ],
    "migration_decisions": [
        ("platform", "text", "PRIMARY KEY"),
        ("option", "text", "NOT NULL"),
        ("canonical_account_id", "text", ""),
        ("legacy_account_id", "text", ""),
        ("status", "text", "NOT NULL"),  # proposed | approved
        ("rationale", "text", ""),
        ("evidence", "json", ""),
        ("decided_by", "text", ""),
        ("decided_at", "ts", ""),
    ],
    "profile_change_proposals": [
        ("id", "pk", ""),
        ("account_id", "text", "NOT NULL"),
        ("field", "text", "NOT NULL"),  # display_name | bio | username | banner | pinned_post | link
        ("current_value", "text", ""),
        ("proposed_value", "text", ""),
        ("status", "text", "NOT NULL"),  # proposed | approved_for_manual_change | rejected | done_manually
        ("note", "text", ""),
        ("created_at", "ts", ""),
        ("reviewed_by", "text", ""),
    ],
    "experiments": [
        ("experiment_id", "text", "PRIMARY KEY"),
        ("week_start", "text", ""),
        ("platform", "text", ""),
        ("account_id", "text", ""),
        ("variable", "text", ""),
        ("control", "text", ""),
        ("treatment", "text", ""),
        ("hypothesis", "text", ""),
        ("success_metric", "text", ""),
        ("status", "text", ""),
        ("result", "json", ""),
    ],
}

INDEXES = [
    "CREATE INDEX IF NOT EXISTS ix_variants_state ON variants(state)",
    "CREATE INDEX IF NOT EXISTS ix_variants_account ON variants(account_id)",
    "CREATE INDEX IF NOT EXISTS ix_metrics_account ON metrics(account_id, metric)",
    "CREATE INDEX IF NOT EXISTS ix_jobs_state ON publish_jobs(state, next_attempt_at)",
    "CREATE INDEX IF NOT EXISTS ix_sched_slot ON schedules(slot_at)",
]

_TYPES = {
    "sqlite": {"text": "TEXT", "int": "INTEGER", "real": "REAL", "bool": "INTEGER", "json": "TEXT",
               "ts": "TEXT", "pk": "INTEGER PRIMARY KEY AUTOINCREMENT"},
    "postgres": {"text": "TEXT", "int": "INTEGER", "real": "DOUBLE PRECISION", "bool": "BOOLEAN",
                 "json": "JSONB", "ts": "TIMESTAMPTZ", "pk": "BIGSERIAL PRIMARY KEY"},
}

JSON_COLS = {t: {c for c, ty, _ in cols if ty == "json"} for t, cols in TABLES.items()}
BOOL_COLS = {t: {c for c, ty, _ in cols if ty == "bool"} for t, cols in TABLES.items()}


def render_ddl(dialect: str = "sqlite") -> str:
    out = []
    for table, cols in TABLES.items():
        parts = []
        for name, ty, cons in cols:
            c = cons
            if dialect == "postgres" and ty == "bool":
                c = c.replace("DEFAULT 0", "DEFAULT FALSE").replace("DEFAULT 1", "DEFAULT TRUE")
            parts.append(f"  {name} {_TYPES[dialect][ty]} {c}".rstrip())
        out.append(f"CREATE TABLE IF NOT EXISTS {table} (\n" + ",\n".join(parts) + "\n);")
    out.extend(i + ";" for i in INDEXES)
    return "\n\n".join(out) + "\n"


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class DB:
    """Tiny DB wrapper; SQL is written with `?` placeholders and translated for Postgres."""

    def __init__(self, url: str):
        self.url = url
        if url.startswith("sqlite:///") or url == ":memory:":
            self.dialect = "sqlite"
            path = ":memory:" if url in (":memory:", "sqlite:///:memory:") else url[len("sqlite:///"):]
            if path != ":memory:":
                p = Path(path)
                if not p.is_absolute():
                    from .config import ROOT
                    p = ROOT / p
                p.parent.mkdir(parents=True, exist_ok=True)
                path = str(p)
            self.conn = sqlite3.connect(path, check_same_thread=False)
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA journal_mode=WAL" if path != ":memory:" else "PRAGMA foreign_keys=ON")
        elif url.startswith(("postgres://", "postgresql://")):
            import psycopg  # optional dependency
            from psycopg.rows import dict_row
            self.dialect = "postgres"
            self.conn = psycopg.connect(url, row_factory=dict_row, autocommit=True)
        else:
            raise ValueError(f"unsupported database url {url!r}")
        self.init_schema()

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass

    def __del__(self):
        self.close()

    def init_schema(self) -> None:
        ddl = render_ddl(self.dialect)
        if self.dialect == "sqlite":
            self.conn.executescript(ddl)
            self.conn.commit()
        else:
            with self.conn.cursor() as cur:
                cur.execute(ddl)

    def _sql(self, sql: str) -> str:
        return sql.replace("?", "%s") if self.dialect == "postgres" else sql

    def execute(self, sql: str, params: Iterable[Any] = ()) -> Any:
        if self.dialect == "sqlite":
            cur = self.conn.execute(sql, tuple(params))
            self.conn.commit()
            return cur
        with self.conn.cursor() as cur:
            cur.execute(self._sql(sql), tuple(params))
            return cur

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[dict]:
        if self.dialect == "sqlite":
            rows = [dict(r) for r in self.conn.execute(sql, tuple(params)).fetchall()]
        else:
            with self.conn.cursor() as cur:
                cur.execute(self._sql(sql), tuple(params))
                rows = list(cur.fetchall())
        return rows

    def one(self, sql: str, params: Iterable[Any] = ()) -> dict | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    # --- row helpers with JSON/bool decoding ---------------------------------------------
    def _encode(self, table: str, row: dict) -> dict:
        enc = {}
        for k, v in row.items():
            if k in JSON_COLS[table] and v is not None and not isinstance(v, str):
                v = json.dumps(v, sort_keys=True)
            elif k in BOOL_COLS[table] and v is not None:
                v = bool(v) if self.dialect == "postgres" else int(bool(v))
            enc[k] = v
        return enc

    def decode(self, table: str, row: dict | None) -> dict | None:
        if row is None:
            return None
        out = dict(row)
        for k in JSON_COLS[table]:
            if isinstance(out.get(k), str) and out[k]:
                try:
                    out[k] = json.loads(out[k])
                except json.JSONDecodeError:
                    pass
        for k in BOOL_COLS[table]:
            if k in out and out[k] is not None:
                out[k] = bool(out[k])
        return out

    def insert(self, table: str, row: dict, upsert_key: str | None = None) -> None:
        row = self._encode(table, row)
        cols = ", ".join(row)
        qs = ", ".join("?" for _ in row)
        sql = f"INSERT INTO {table} ({cols}) VALUES ({qs})"
        if upsert_key:
            updates = ", ".join(f"{c}=excluded.{c}" for c in row if c != upsert_key)
            sql += f" ON CONFLICT({upsert_key}) DO UPDATE SET {updates}"
        self.execute(sql, row.values())

    def update(self, table: str, key: str, key_value: Any, changes: dict) -> None:
        changes = self._encode(table, changes)
        sets = ", ".join(f"{c}=?" for c in changes)
        self.execute(f"UPDATE {table} SET {sets} WHERE {key}=?", [*changes.values(), key_value])

    def get(self, table: str, key: str, value: Any) -> dict | None:
        return self.decode(table, self.one(f"SELECT * FROM {table} WHERE {key}=?", [value]))

    def all(self, table: str, where: str = "", params: Iterable[Any] = (), order: str = "") -> list[dict]:
        sql = f"SELECT * FROM {table}"
        if where:
            sql += f" WHERE {where}"
        if order:
            sql += f" ORDER BY {order}"
        return [self.decode(table, r) for r in self.query(sql, params)]

    # --- audit + kv ----------------------------------------------------------------------
    def audit(self, actor: str, action: str, entity: str = "", entity_id: str = "", detail: dict | None = None) -> None:
        self.insert("audit_log", {"at": now_iso(), "actor": actor, "action": action, "entity": entity,
                                  "entity_id": entity_id, "detail": detail or {}})

    def kv_get(self, key: str, default: str | None = None) -> str | None:
        r = self.one("SELECT value FROM settings_kv WHERE key=?", [key])
        return r["value"] if r else default

    def kv_set(self, key: str, value: str, actor: str = "system") -> None:
        self.insert("settings_kv", {"key": key, "value": value, "updated_at": now_iso(), "updated_by": actor},
                    upsert_key="key")
        self.audit(actor, "setting_changed", "settings_kv", key, {"value": value})


def connect(url: str | None = None) -> DB:
    from .config import load_settings
    return DB(url or load_settings().database_url)
