import os
import tempfile
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path

from pln_agent import accounts as acc_mod
from pln_agent import migration as mig
from pln_agent.config import ROOT, load_settings
from pln_agent.db import DB
from pln_agent.editorial import generate_pack
from pln_agent.ingest import scan_inbox

TODAY = date(2026, 10, 20)
SLOT = datetime(2026, 10, 21, 16, 0, tzinfo=timezone.utc)
CJ = "carjitsu-sample-001"
PUTT = "putt-sample-002"
WPL = "wpl-archive-003"
SLAP = "slapfight-sample-004"


def make(packs=True, **overrides):
    tmp = Path(tempfile.mkdtemp(prefix="pln-test-"))
    s = replace(load_settings(), dry_run=True, publishing_enabled=False, database_url="sqlite:///:memory:",
                render_dir=tmp / "renders", media_inbox=ROOT / "sample_media" / "inbox", **overrides)
    db = DB(":memory:")
    acc_mod.load_accounts(db, actor="test")
    mig.sync_decisions(db)
    scan_inbox(db, s.media_inbox)
    if packs:
        for a in (CJ, PUTT, WPL, SLAP):
            generate_pack(db, a, s, today=TODAY)
    return db, s


def go_live(db, account_id, token_env="TEST_FAKE_TOKEN"):
    """Simulate a human completing ownership + OAuth + authorization for one account (tests only)."""
    os.environ[token_env] = "not-a-real-token"
    acc_mod.set_verification(db, account_id, "ownership_confirmed", "test")
    acc_mod.connect_credentials(db, account_id, f"env:{token_env}", "123456", "test")
    acc_mod.authorize_publishing(db, account_id, "test")
