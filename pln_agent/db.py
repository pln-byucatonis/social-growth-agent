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


class _J:
    """A JSON-encoded bind value (so each backend can bind it as jsonb)."""

    __slots__ = ("v",)

    def __init__(self, v: str):
        self.v = v

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
        ("source", "text", "NOT NULL"),  # user_csv | system_placeholder | ops_registry
        ("ops_account_id", "int", ""),  # ops portal social_account.id when matched
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
    # Human-entered rights/identity record per asset. Survives re-imports of the asset
    # from the ops clip library (which carries no rights metadata at all).
    "asset_rights": [
        ("asset_id", "text", "PRIMARY KEY"),
        ("license", "text", ""),
        ("owner", "text", ""),
        ("territory", "json", ""),
        ("rights_expires_at", "text", ""),
        ("rights_verified", "bool", "NOT NULL DEFAULT 0"),
        ("people_verified", "bool", "NOT NULL DEFAULT 0"),
        ("music_cleared", "bool", "NOT NULL DEFAULT 0"),
        ("sponsor_approved", "bool", "NOT NULL DEFAULT 0"),
        ("note", "text", ""),
        ("updated_by", "text", ""),
        ("updated_at", "ts", ""),
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
        ("public_media_url", "text", ""),  # approved render on the public CDN (platform pull-from-URL)
        ("treatment", "json", ""),  # Creative Director: hook / edit / context / payoff / conversion + duration variants
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
        ("source", "text", "NOT NULL"),  # manual_csv | api | ops_social | platform_api
        ("captured_at", "ts", ""),
        ("metric_key", "text", ""),  # stable id for idempotent re-imports (unique when set)
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
    # Creative Intelligence Database: one row per real post (historical or agent-published) with the
    # creative variables, where each value came from, the source footage and publishing details.
    # Performance lives in `metrics` (post-level rows share platform_post_id).
    "posts": [
        ("post_id", "text", "PRIMARY KEY"),  # <platform>:<platform_post_id>
        ("account_id", "text", "NOT NULL"),
        ("brand_id", "text", ""),
        ("platform", "text", "NOT NULL"),
        ("platform_post_id", "text", "NOT NULL"),
        ("published_at", "ts", ""),
        ("permalink", "text", ""),
        ("post_type", "text", ""),  # reels | feed | carousel | short | video | story ...
        ("origin", "text", ""),  # ops_ingested | agent_published
        ("variant_id", "text", ""),
        ("asset_id", "text", ""),
        ("caption", "text", ""),
        ("duration_s", "real", ""),
        ("creative", "json", ""),  # variable -> value
        ("provenance", "json", ""),  # variable -> agent_decision | inferred_from_caption | inferred_from_metadata | unknown
        ("source_footage", "json", ""),
        ("first_seen_at", "ts", ""),
        ("updated_at", "ts", ""),
    ],
    # Viral Content Scout: every library clip scored for creative potential (own property + PLN crossover).
    "scout_moments": [
        ("clip_id", "text", "PRIMARY KEY"),
        ("source_id", "text", ""),  # the master (event) the clip was cut from
        ("brand_id", "text", ""),
        ("start_sec", "real", ""),
        ("end_sec", "real", ""),
        ("potential", "real", ""),
        ("pln_potential", "real", ""),
        ("categories", "json", ""),
        ("content_type", "text", ""),
        ("detail", "json", ""),  # explainable parts
        ("caption", "text", ""),  # machine description (reviewer context only — never copy)
        ("model_version", "text", ""),
        ("status", "text", ""),  # candidate | promoted | dismissed
        ("scored_at", "ts", ""),
    ],
    # Creative Director: one package per asset — five-part treatment per destination + duration variants.
    "creative_packages": [
        ("package_id", "text", "PRIMARY KEY"),
        ("asset_id", "text", "NOT NULL"),
        ("moment", "json", ""),
        ("treatments", "json", ""),  # account_id -> treatment
        ("knowledge_refs", "json", ""),
        ("created_by", "text", ""),
        ("created_at", "ts", ""),
    ],
    # Knowledge Engine: every piece of guidance is attributed, dated and classified.
    "knowledge": [
        ("entry_id", "text", "PRIMARY KEY"),
        ("level", "text", "NOT NULL"),  # platform_rule | platform_guidance | external_research | hypothesis | pln_evidence
        ("platform", "text", ""),  # '' = cross-platform
        ("brand_id", "text", ""),  # '' = all brands
        ("account_id", "text", ""),
        ("topic", "text", ""),  # cadence | duration | hook | originality | analytics | publishing_limits ...
        ("statement", "text", "NOT NULL"),
        ("quote", "text", ""),  # verbatim excerpt from the source, checked on every review
        ("source_url", "text", ""),
        ("source_title", "text", ""),
        ("publisher", "text", ""),
        ("source_date", "text", ""),  # publication date, or 'undated' + what the page shows
        ("retrieved_at", "ts", ""),
        ("reviewed_at", "ts", ""),
        ("reviewed_by", "text", ""),
        ("status", "text", "NOT NULL"),  # current | needs_review | superseded | retired
        ("applicability", "text", ""),
        ("evidence", "json", ""),
        ("supersedes", "text", ""),
        ("created_by", "text", ""),
        ("created_at", "ts", ""),
        ("updated_at", "ts", ""),
    ],
    "knowledge_sources": [
        ("url", "text", "PRIMARY KEY"),
        ("title", "text", ""),
        ("last_checked_at", "ts", ""),
        ("http_status", "int", ""),
        ("content_hash", "text", ""),
        ("changed_at", "ts", ""),
        ("status", "text", ""),  # baseline | unchanged | changed | quote_missing | unreachable
        ("note", "text", ""),
    ],
    # Publishing Strategist: configurable starting cadence + the current measured recommendation.
    "cadence": [
        ("account_id", "text", "PRIMARY KEY"),
        ("start_min", "real", ""),
        ("start_max", "real", ""),
        ("start_source", "text", ""),
        ("recommended_per_week", "real", ""),
        ("action", "text", ""),  # hold | increase | decrease | production_limited | collect_baseline
        ("confidence", "text", ""),
        ("rationale", "text", ""),
        ("evidence", "json", ""),
        ("updated_at", "ts", ""),
    ],
}

INDEXES = [
    "CREATE INDEX IF NOT EXISTS ix_variants_state ON variants(state)",
    "CREATE INDEX IF NOT EXISTS ix_variants_account ON variants(account_id)",
    "CREATE INDEX IF NOT EXISTS ix_metrics_account ON metrics(account_id, metric)",
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_metrics_key ON metrics(metric_key)",
    "CREATE INDEX IF NOT EXISTS ix_metrics_post ON metrics(platform_post_id)",
    "CREATE INDEX IF NOT EXISTS ix_posts_account ON posts(account_id, published_at)",
    "CREATE INDEX IF NOT EXISTS ix_scout_source ON scout_moments(source_id, potential)",
    "CREATE INDEX IF NOT EXISTS ix_scout_brand ON scout_moments(brand_id, potential)",
    "CREATE INDEX IF NOT EXISTS ix_jobs_state ON publish_jobs(state, next_attempt_at)",
    "CREATE INDEX IF NOT EXISTS ix_sched_slot ON schedules(slot_at)",
]

# Timestamps are stored as ISO-8601 TEXT on every backend: the engine compares and
# parses them as strings, and the Aurora Data API has no clean timestamptz binding.
_TYPES = {
    "sqlite": {"text": "TEXT", "int": "INTEGER", "real": "REAL", "bool": "INTEGER", "json": "TEXT",
               "ts": "TEXT", "pk": "INTEGER PRIMARY KEY AUTOINCREMENT"},
    "postgres": {"text": "TEXT", "int": "INTEGER", "real": "DOUBLE PRECISION", "bool": "BOOLEAN",
                 "json": "JSONB", "ts": "TEXT", "pk": "BIGSERIAL PRIMARY KEY"},
}

JSON_COLS = {t: {c for c, ty, _ in cols if ty == "json"} for t, cols in TABLES.items()}
BOOL_COLS = {t: {c for c, ty, _ in cols if ty == "bool"} for t, cols in TABLES.items()}


def render_ddl(dialect: str = "sqlite", indexes: bool = True) -> str:
    dialect = "postgres" if dialect == "rdsdata" else dialect
    out = []
    for table, cols in TABLES.items():
        parts = []
        for name, ty, cons in cols:
            c = cons
            if dialect == "postgres" and ty == "bool":
                c = c.replace("DEFAULT 0", "DEFAULT FALSE").replace("DEFAULT 1", "DEFAULT TRUE")
            parts.append(f"  {name} {_TYPES[dialect][ty]} {c}".rstrip())
        out.append(f"CREATE TABLE IF NOT EXISTS {table} (\n" + ",\n".join(parts) + "\n);")
    if indexes:
        out.extend(i + ";" for i in INDEXES)
    return "\n\n".join(out) + "\n"


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class _RdsData:
    """Aurora Data API connection (same access pattern as the ops portal's src/lib/aws/db.ts).

    URL form: rdsdata://<database>?cluster=<cluster-arn>&secret=<secret-arn>[&region=us-east-1]
    """

    def __init__(self, url: str):
        import boto3  # optional dependency
        from urllib.parse import parse_qs, urlparse
        u = urlparse(url)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        self.database = u.netloc or u.path.lstrip("/") or "growth"
        self.cluster, self.secret = q["cluster"], q["secret"]
        self.client = boto3.client("rds-data", region_name=q.get("region", "us-east-1"))

    @staticmethod
    def _param(name: str, v: Any, json_hint: bool) -> dict:
        if v is None:
            return {"name": name, "value": {"isNull": True}}
        if isinstance(v, bool):
            return {"name": name, "value": {"booleanValue": v}}
        if isinstance(v, int):
            return {"name": name, "value": {"longValue": v}}
        if isinstance(v, float):
            return {"name": name, "value": {"doubleValue": v}}
        p = {"name": name, "value": {"stringValue": str(v)}}
        if json_hint:
            p["typeHint"] = "JSON"
        return p

    def run(self, sql: str, params: Iterable[Any] = (), json_params: set[int] | None = None) -> list[dict]:
        params = list(params)
        parts = sql.split("?")
        if len(parts) - 1 != len(params):
            raise ValueError("placeholder/param count mismatch")
        named = parts[0] + "".join(f":p{i}" + parts[i + 1] for i in range(len(params)))
        bound = [self._param(f"p{i}", v, i in (json_params or set())) for i, v in enumerate(params)]
        try:
            return self._exec(named, bound)
        except Exception as e:
            # The Data API refuses any single result over 1 MB. Re-read a SELECT in pages instead.
            if "UnsupportedResultException" not in type(e).__name__ + str(e) or \
                    not named.lstrip().lower().startswith(("select", "with")):
                raise
            return self._paged(named, bound)

    def _exec(self, sql: str, bound: list[dict]) -> list[dict]:
        out = self.client.execute_statement(resourceArn=self.cluster, secretArn=self.secret, database=self.database,
                                            sql=sql, parameters=bound, formatRecordsAs="JSON")
        return json.loads(out["formattedRecords"]) if out.get("formattedRecords") else []

    def _paged(self, sql: str, bound: list[dict], size: int = 400) -> list[dict]:
        rows: list[dict] = []
        offset = 0
        while True:
            page_sql = f"SELECT * FROM ({sql}) AS _page LIMIT {size} OFFSET {offset}"
            try:
                page = self._exec(page_sql, bound)
            except Exception as e:
                if "UnsupportedResultException" in type(e).__name__ + str(e) and size > 1:
                    size = max(1, size // 2)  # rows are large: shrink the page and retry
                    continue
                raise
            rows.extend(page)
            if len(page) < size:
                return rows
            offset += size


class DB:
    """Tiny DB wrapper; SQL is written with `?` placeholders and translated per backend.

    Backends: sqlite:///path (local/tests), postgresql://... (psycopg), and
    rdsdata://<db>?cluster=..&secret=.. (Aurora Data API, used when running beside the ops portal).
    """

    def __init__(self, url: str, init: bool = True):
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
        elif url.startswith("rdsdata://"):
            self.dialect = "rdsdata"
            self.conn = _RdsData(url)
        else:
            raise ValueError(f"unsupported database url {url!r}")
        if init:  # init=False: read-only handle onto someone else's database (e.g. the ops portal's)
            self.init_schema()

    def close(self) -> None:
        try:
            if self.dialect != "rdsdata":
                self.conn.close()
        except Exception:
            pass

    def __del__(self):
        self.close()

    def init_schema(self) -> None:
        # tables → additive columns → indexes (an index may cover a column an older table lacks)
        self._run_ddl(render_ddl(self.dialect, indexes=False))
        self._add_missing_columns()
        self._run_ddl("\n\n".join(i + ";" for i in INDEXES) + "\n")

    def _run_ddl(self, ddl: str) -> None:
        if self.dialect == "sqlite":
            self.conn.executescript(ddl)
            self.conn.commit()
        elif self.dialect == "postgres":
            with self.conn.cursor() as cur:
                cur.execute(ddl)
        else:  # Data API: one statement per call
            for stmt in [x.strip() for x in ddl.split(";\n") if x.strip()]:
                self.conn.run(stmt)

    def _add_missing_columns(self) -> None:
        """Additive migrations: CREATE TABLE IF NOT EXISTS never adds new columns to an
        existing table, so add any column the code defines but the table lacks."""
        types = _TYPES["sqlite" if self.dialect == "sqlite" else "postgres"]
        for table, cols in TABLES.items():
            if self.dialect == "sqlite":
                have = {r["name"] for r in self.query(f"PRAGMA table_info({table})")}
            else:
                have = {r["column_name"] for r in self.query(
                    "SELECT column_name FROM information_schema.columns WHERE table_name = ? "
                    "AND table_schema = current_schema()", [table])}
            for name, ty, cons in cols:
                if name in have or ty == "pk" or "PRIMARY KEY" in cons:
                    continue
                default = " DEFAULT 0" if "DEFAULT 0" in cons and self.dialect == "sqlite" else (
                    " DEFAULT FALSE" if "DEFAULT 0" in cons else "")
                self.execute(f"ALTER TABLE {table} ADD COLUMN {name} {types[ty]}{default}")

    def _sql(self, sql: str) -> str:
        return sql.replace("?", "%s") if self.dialect == "postgres" else sql

    def _adapt(self, params: Iterable[Any]) -> list:
        params = list(params)
        if self.dialect == "postgres":
            from psycopg.types.json import Jsonb
            return [Jsonb(json.loads(p.v)) if isinstance(p, _J) else p for p in params]
        return [p.v if isinstance(p, _J) else p for p in params]

    def execute(self, sql: str, params: Iterable[Any] = ()) -> Any:
        params = list(params)
        if self.dialect == "sqlite":
            cur = self.conn.execute(sql, tuple(self._adapt(params)))
            self.conn.commit()
            return cur
        if self.dialect == "rdsdata":
            return self.conn.run(sql, self._adapt(params), {i for i, p in enumerate(params) if isinstance(p, _J)})
        with self.conn.cursor() as cur:
            cur.execute(self._sql(sql), tuple(self._adapt(params)))
            return cur

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[dict]:
        params = list(params)
        if self.dialect == "sqlite":
            return [dict(r) for r in self.conn.execute(sql, tuple(self._adapt(params))).fetchall()]
        if self.dialect == "rdsdata":
            return self.conn.run(sql, self._adapt(params), {i for i, p in enumerate(params) if isinstance(p, _J)})
        with self.conn.cursor() as cur:
            cur.execute(self._sql(sql), tuple(self._adapt(params)))
            return list(cur.fetchall()) if cur.description else []

    def one(self, sql: str, params: Iterable[Any] = ()) -> dict | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    # --- row helpers with JSON/bool decoding ---------------------------------------------
    def _encode(self, table: str, row: dict) -> dict:
        enc = {}
        for k, v in row.items():
            if k in JSON_COLS[table] and v is not None:
                v = _J(v if isinstance(v, str) else json.dumps(v, sort_keys=True))
            elif k in BOOL_COLS[table] and v is not None:
                v = int(bool(v)) if self.dialect == "sqlite" else bool(v)
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

    def insert_many(self, table: str, rows: list[dict], upsert_key: str | None = None, chunk_params: int = 900) -> int:
        """Multi-row INSERT (one round trip per chunk — matters on the Data API). All rows must share keys."""
        if not rows:
            return 0
        cols = list(rows[0])
        per = max(1, chunk_params // len(cols))
        for i in range(0, len(rows), per):
            part = [self._encode(table, r) for r in rows[i:i + per]]
            values = ", ".join("(" + ", ".join("?" for _ in cols) + ")" for _ in part)
            sql = f"INSERT INTO {table} ({', '.join(cols)}) VALUES {values}"
            if upsert_key:
                updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c != upsert_key)
                sql += f" ON CONFLICT({upsert_key}) DO UPDATE SET {updates}"
            self.execute(sql, [r[c] for r in part for c in cols])
        return len(rows)

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
