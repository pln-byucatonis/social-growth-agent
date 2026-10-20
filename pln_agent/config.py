"""Runtime settings. Everything defaults to SAFE: dry-run on, publishing off, approval required.

Settings come from environment variables (optionally loaded from a local `.env`). Secrets are
never stored in the database: accounts reference credentials by *name* (e.g. `env:LINKEDIN_TOKEN_PLN`)
and the token itself is resolved only at publish time.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = ROOT / "config"


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    database_url: str = "sqlite:///data/pln_agent.db"
    timezone: str = "America/New_York"
    # Global gates. BOTH must be flipped (plus per-account authorization) before any live call.
    dry_run: bool = True
    publishing_enabled: bool = False
    require_approval: bool = True
    media_inbox: Path = ROOT / "sample_media" / "inbox"
    render_dir: Path = ROOT / "data" / "renders"
    content_calendar_csv: Path = CONFIG_DIR / "content_calendar.csv"
    llm_provider: str = "template"  # template | anthropic (optional, needs ANTHROPIC_API_KEY)
    meta_graph_version: str = "v26.0"
    linkedin_version: str = "202601"
    # ops portal integration (read-only): rdsdata://plnops?cluster=<arn>&secret=<arn>
    ops_database_url: str = ""
    ops_assets_bucket: str = "plnfaststack-assetsbucket5cb76180-4dbgjaidfcbv"
    # set by the ops proxy; when present the dashboard requires it and trusts X-PLN-User-Email
    agent_api_key: str = ""
    max_publish_attempts: int = 5
    backoff_base_seconds: int = 60
    extra: dict = field(default_factory=dict)

    @property
    def sqlite_path(self) -> Path:
        assert self.database_url.startswith("sqlite:///"), "only sqlite path for sqlite urls"
        p = Path(self.database_url[len("sqlite:///"):])
        return p if p.is_absolute() else ROOT / p


def load_settings(env_file: Path | None = None) -> Settings:
    _load_dotenv(env_file or ROOT / ".env")
    s = Settings()
    s.database_url = os.environ.get("PLN_DATABASE_URL", s.database_url)
    s.timezone = os.environ.get("PLN_TIMEZONE", s.timezone)
    s.dry_run = _bool("PLN_DRY_RUN", True)
    s.publishing_enabled = _bool("PLN_PUBLISHING_ENABLED", False)
    s.require_approval = _bool("PLN_REQUIRE_APPROVAL", True)
    if os.environ.get("PLN_MEDIA_INBOX"):
        s.media_inbox = Path(os.environ["PLN_MEDIA_INBOX"])
    if os.environ.get("PLN_RENDER_DIR"):
        s.render_dir = Path(os.environ["PLN_RENDER_DIR"])
    s.llm_provider = os.environ.get("PLN_LLM_PROVIDER", s.llm_provider)
    s.meta_graph_version = os.environ.get("META_GRAPH_VERSION", s.meta_graph_version)
    s.linkedin_version = os.environ.get("LINKEDIN_VERSION", s.linkedin_version)
    s.ops_database_url = os.environ.get("PLN_OPS_DATABASE_URL", s.ops_database_url)
    s.ops_assets_bucket = os.environ.get("PLN_OPS_ASSETS_BUCKET", s.ops_assets_bucket)
    s.agent_api_key = os.environ.get("PLN_AGENT_API_KEY", s.agent_api_key)
    return s


def load_json_config(name: str) -> dict:
    return json.loads((CONFIG_DIR / name).read_text())
