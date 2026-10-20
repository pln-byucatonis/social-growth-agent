CREATE TABLE IF NOT EXISTS brands (
  brand_id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  priority TEXT,
  legacy_of TEXT,
  is_legacy BOOLEAN NOT NULL DEFAULT FALSE,
  voice JSONB,
  notes TEXT
);

CREATE TABLE IF NOT EXISTS accounts (
  account_id TEXT PRIMARY KEY,
  brand_id TEXT NOT NULL,
  canonical_brand_id TEXT NOT NULL,
  source_brand_name TEXT,
  platform TEXT NOT NULL,
  username_original TEXT,
  username_normalized TEXT,
  stable_platform_id TEXT,
  priority TEXT,
  role TEXT NOT NULL,
  supplied_status TEXT,
  verification_status TEXT NOT NULL,
  ownership_owner TEXT,
  credential_ref TEXT,
  publishing_authorized BOOLEAN NOT NULL DEFAULT FALSE,
  publishing_policy TEXT NOT NULL DEFAULT 'approval_required',
  hard_block_reason TEXT,
  redirect_to_account_id TEXT,
  campaign TEXT,
  notes TEXT,
  source TEXT NOT NULL,
  ops_account_id INTEGER,
  csv_row INTEGER,
  created_at TEXT,
  updated_at TEXT
);

CREATE TABLE IF NOT EXISTS assets (
  asset_id TEXT PRIMARY KEY,
  brand_id TEXT NOT NULL,
  source_brand_name TEXT,
  title TEXT,
  description TEXT,
  media_path TEXT,
  media_present BOOLEAN NOT NULL DEFAULT FALSE,
  sha256 TEXT,
  content_id TEXT,
  parent_asset_id TEXT,
  provenance TEXT,
  owner TEXT,
  license TEXT,
  territory JSONB,
  rights_expires_at TEXT,
  rights_verified BOOLEAN NOT NULL DEFAULT FALSE,
  people_verified BOOLEAN NOT NULL DEFAULT FALSE,
  music_cleared BOOLEAN NOT NULL DEFAULT FALSE,
  sponsors JSONB,
  sponsor_approved BOOLEAN NOT NULL DEFAULT FALSE,
  people JSONB,
  facts JSONB,
  moments JSONB,
  flags JSONB,
  series TEXT,
  event_name TEXT,
  event_date TEXT,
  duration_s DOUBLE PRECISION,
  width INTEGER,
  height INTEGER,
  has_audio BOOLEAN,
  probe JSONB,
  transcript JSONB,
  is_sample BOOLEAN NOT NULL DEFAULT FALSE,
  ingested_at TEXT
);

CREATE TABLE IF NOT EXISTS asset_rights (
  asset_id TEXT PRIMARY KEY,
  license TEXT,
  owner TEXT,
  territory JSONB,
  rights_expires_at TEXT,
  rights_verified BOOLEAN NOT NULL DEFAULT FALSE,
  people_verified BOOLEAN NOT NULL DEFAULT FALSE,
  music_cleared BOOLEAN NOT NULL DEFAULT FALSE,
  sponsor_approved BOOLEAN NOT NULL DEFAULT FALSE,
  note TEXT,
  updated_by TEXT,
  updated_at TEXT
);

CREATE TABLE IF NOT EXISTS editorial_packs (
  pack_id TEXT PRIMARY KEY,
  asset_id TEXT NOT NULL,
  created_at TEXT,
  summary JSONB
);

CREATE TABLE IF NOT EXISTS variants (
  variant_id TEXT PRIMARY KEY,
  pack_id TEXT,
  asset_id TEXT NOT NULL,
  account_id TEXT NOT NULL,
  platform TEXT NOT NULL,
  brand_id TEXT NOT NULL,
  route_role TEXT,
  series TEXT,
  format TEXT,
  hook TEXT,
  hook_candidates JSONB,
  title TEXT,
  caption TEXT,
  hashtags JSONB,
  hashtag_rationale TEXT,
  voiceover TEXT,
  onscreen_text JSONB,
  edl JSONB,
  thumbnail JSONB,
  cta TEXT,
  angle TEXT,
  test_hypothesis TEXT,
  utm_url TEXT,
  score DOUBLE PRECISION,
  score_breakdown JSONB,
  risk_flags JSONB,
  validation JSONB,
  state TEXT NOT NULL,
  caption_hash TEXT,
  render_status TEXT,
  render_path TEXT,
  public_media_url TEXT,
  created_at TEXT,
  updated_at TEXT
);

CREATE TABLE IF NOT EXISTS approvals (
  id BIGSERIAL PRIMARY KEY,
  variant_id TEXT NOT NULL,
  decision TEXT NOT NULL,
  reviewer TEXT NOT NULL,
  note TEXT,
  caption_hash TEXT,
  at TEXT
);

CREATE TABLE IF NOT EXISTS schedules (
  schedule_id TEXT PRIMARY KEY,
  variant_id TEXT NOT NULL,
  account_id TEXT NOT NULL,
  slot_at TEXT NOT NULL,
  state TEXT NOT NULL,
  reason TEXT,
  created_at TEXT
);

CREATE TABLE IF NOT EXISTS publish_jobs (
  job_id TEXT PRIMARY KEY,
  variant_id TEXT NOT NULL,
  account_id TEXT NOT NULL,
  idempotency_key TEXT NOT NULL UNIQUE,
  state TEXT NOT NULL,
  mode TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  next_attempt_at TEXT,
  locked_at TEXT,
  last_error TEXT,
  platform_post_id TEXT,
  permalink TEXT,
  receipt JSONB,
  remote_status TEXT,
  created_at TEXT,
  updated_at TEXT
);

CREATE TABLE IF NOT EXISTS publish_attempts (
  id BIGSERIAL PRIMARY KEY,
  job_id TEXT NOT NULL,
  at TEXT,
  outcome TEXT,
  error_kind TEXT,
  error TEXT,
  response JSONB
);

CREATE TABLE IF NOT EXISTS metrics (
  id BIGSERIAL PRIMARY KEY,
  account_id TEXT NOT NULL,
  variant_id TEXT,
  platform_post_id TEXT,
  platform TEXT NOT NULL,
  metric TEXT NOT NULL,
  canonical_metric TEXT,
  value DOUBLE PRECISION,
  checkpoint TEXT,
  period_start TEXT,
  period_end TEXT,
  source TEXT NOT NULL,
  captured_at TEXT,
  metric_key TEXT
);

CREATE TABLE IF NOT EXISTS feedback (
  id BIGSERIAL PRIMARY KEY,
  variant_id TEXT,
  kind TEXT,
  text TEXT,
  author TEXT,
  at TEXT
);

CREATE TABLE IF NOT EXISTS audit_log (
  id BIGSERIAL PRIMARY KEY,
  at TEXT,
  actor TEXT,
  action TEXT NOT NULL,
  entity TEXT,
  entity_id TEXT,
  detail JSONB
);

CREATE TABLE IF NOT EXISTS settings_kv (
  key TEXT PRIMARY KEY,
  value TEXT,
  updated_at TEXT,
  updated_by TEXT
);

CREATE TABLE IF NOT EXISTS migration_decisions (
  platform TEXT PRIMARY KEY,
  option TEXT NOT NULL,
  canonical_account_id TEXT,
  legacy_account_id TEXT,
  status TEXT NOT NULL,
  rationale TEXT,
  evidence JSONB,
  decided_by TEXT,
  decided_at TEXT
);

CREATE TABLE IF NOT EXISTS profile_change_proposals (
  id BIGSERIAL PRIMARY KEY,
  account_id TEXT NOT NULL,
  field TEXT NOT NULL,
  current_value TEXT,
  proposed_value TEXT,
  status TEXT NOT NULL,
  note TEXT,
  created_at TEXT,
  reviewed_by TEXT
);

CREATE TABLE IF NOT EXISTS experiments (
  experiment_id TEXT PRIMARY KEY,
  week_start TEXT,
  platform TEXT,
  account_id TEXT,
  variable TEXT,
  control TEXT,
  treatment TEXT,
  hypothesis TEXT,
  success_metric TEXT,
  status TEXT,
  result JSONB
);

CREATE TABLE IF NOT EXISTS posts (
  post_id TEXT PRIMARY KEY,
  account_id TEXT NOT NULL,
  brand_id TEXT,
  platform TEXT NOT NULL,
  platform_post_id TEXT NOT NULL,
  published_at TEXT,
  permalink TEXT,
  post_type TEXT,
  origin TEXT,
  variant_id TEXT,
  asset_id TEXT,
  caption TEXT,
  duration_s DOUBLE PRECISION,
  creative JSONB,
  provenance JSONB,
  source_footage JSONB,
  first_seen_at TEXT,
  updated_at TEXT
);

CREATE TABLE IF NOT EXISTS knowledge (
  entry_id TEXT PRIMARY KEY,
  level TEXT NOT NULL,
  platform TEXT,
  brand_id TEXT,
  account_id TEXT,
  topic TEXT,
  statement TEXT NOT NULL,
  quote TEXT,
  source_url TEXT,
  source_title TEXT,
  publisher TEXT,
  source_date TEXT,
  retrieved_at TEXT,
  reviewed_at TEXT,
  reviewed_by TEXT,
  status TEXT NOT NULL,
  applicability TEXT,
  evidence JSONB,
  supersedes TEXT,
  created_by TEXT,
  created_at TEXT,
  updated_at TEXT
);

CREATE TABLE IF NOT EXISTS knowledge_sources (
  url TEXT PRIMARY KEY,
  title TEXT,
  last_checked_at TEXT,
  http_status INTEGER,
  content_hash TEXT,
  changed_at TEXT,
  status TEXT,
  note TEXT
);

CREATE TABLE IF NOT EXISTS cadence (
  account_id TEXT PRIMARY KEY,
  start_min DOUBLE PRECISION,
  start_max DOUBLE PRECISION,
  start_source TEXT,
  recommended_per_week DOUBLE PRECISION,
  action TEXT,
  confidence TEXT,
  rationale TEXT,
  evidence JSONB,
  updated_at TEXT
);

CREATE INDEX IF NOT EXISTS ix_variants_state ON variants(state);

CREATE INDEX IF NOT EXISTS ix_variants_account ON variants(account_id);

CREATE INDEX IF NOT EXISTS ix_metrics_account ON metrics(account_id, metric);

CREATE UNIQUE INDEX IF NOT EXISTS ux_metrics_key ON metrics(metric_key);

CREATE INDEX IF NOT EXISTS ix_metrics_post ON metrics(platform_post_id);

CREATE INDEX IF NOT EXISTS ix_posts_account ON posts(account_id, published_at);

CREATE INDEX IF NOT EXISTS ix_jobs_state ON publish_jobs(state, next_attempt_at);

CREATE INDEX IF NOT EXISTS ix_sched_slot ON schedules(slot_at);

