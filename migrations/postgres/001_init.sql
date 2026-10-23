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
  render_qc JSONB,
  post_type TEXT,
  slides JSONB,
  writers_status TEXT,
  treatment JSONB,
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

CREATE TABLE IF NOT EXISTS experiment_arms (
  id BIGSERIAL PRIMARY KEY,
  experiment_id TEXT NOT NULL,
  variant_id TEXT NOT NULL,
  arm TEXT NOT NULL,
  assigned_at TEXT,
  applied JSONB
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

CREATE TABLE IF NOT EXISTS predct_candidates (
  cand_id TEXT PRIMARY KEY,
  show TEXT,
  kind TEXT,
  title TEXT,
  sources JSONB,
  recorded_at TEXT,
  duration_s DOUBLE PRECISION,
  preview_path TEXT,
  thumb_path TEXT,
  preview_status TEXT,
  status TEXT,
  picks JSONB,
  note TEXT,
  created_at TEXT,
  updated_at TEXT
);

CREATE TABLE IF NOT EXISTS moment_searches (
  media_path TEXT PRIMARY KEY,
  brand_id TEXT,
  searched_at TEXT,
  looked_at INTEGER,
  candidates JSONB,
  picked JSONB,
  note TEXT
);

CREATE TABLE IF NOT EXISTS music_beds (
  bed_id TEXT PRIMARY KEY,
  path TEXT,
  title TEXT,
  brands JSONB,
  mood TEXT,
  bpm DOUBLE PRECISION,
  duration_s DOUBLE PRECISION,
  source TEXT,
  provider TEXT,
  model_version TEXT,
  license TEXT,
  license_url TEXT,
  platforms JSONB,
  prompt TEXT,
  seed TEXT,
  sha256 TEXT,
  status TEXT,
  reviewed_by TEXT,
  reviewed_at TEXT,
  note TEXT,
  created_at TEXT,
  updated_at TEXT
);

CREATE TABLE IF NOT EXISTS scout_moments (
  clip_id TEXT PRIMARY KEY,
  source_id TEXT,
  brand_id TEXT,
  start_sec DOUBLE PRECISION,
  end_sec DOUBLE PRECISION,
  potential DOUBLE PRECISION,
  pln_potential DOUBLE PRECISION,
  categories JSONB,
  content_type TEXT,
  detail JSONB,
  caption TEXT,
  model_version TEXT,
  status TEXT,
  scored_at TEXT
);

CREATE TABLE IF NOT EXISTS creative_packages (
  package_id TEXT PRIMARY KEY,
  asset_id TEXT NOT NULL,
  moment JSONB,
  treatments JSONB,
  knowledge_refs JSONB,
  created_by TEXT,
  created_at TEXT
);

CREATE TABLE IF NOT EXISTS growth_briefs (
  brief_id TEXT PRIMARY KEY,
  week_start TEXT,
  created_at TEXT,
  created_by TEXT,
  content JSONB,
  status TEXT
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

CREATE TABLE IF NOT EXISTS agent_directives (
  directive_id TEXT PRIMARY KEY,
  agent_key TEXT NOT NULL,
  text TEXT NOT NULL,
  status TEXT NOT NULL,
  source TEXT,
  thread_id TEXT,
  created_by TEXT,
  created_at TEXT
);

CREATE TABLE IF NOT EXISTS team_threads (
  thread_id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  agent_key TEXT,
  title TEXT,
  status TEXT,
  created_by TEXT,
  created_at TEXT,
  result JSONB
);

CREATE TABLE IF NOT EXISTS yt_videos (
  video_id TEXT PRIMARY KEY,
  channel_id TEXT,
  handle TEXT,
  title TEXT,
  url TEXT,
  published_at TEXT,
  views DOUBLE PRECISION,
  captured_at TEXT
);

CREATE TABLE IF NOT EXISTS market_snapshots (
  snap_id TEXT PRIMARY KEY,
  day TEXT,
  source TEXT,
  market_id TEXT,
  title TEXT,
  category TEXT,
  price DOUBLE PRECISION,
  change_1d DOUBLE PRECISION,
  change_7d DOUBLE PRECISION,
  volume DOUBLE PRECISION,
  url TEXT,
  end_date TEXT,
  captured_at TEXT
);

CREATE TABLE IF NOT EXISTS trend_briefs (
  week_start TEXT PRIMARY KEY,
  created_at TEXT,
  items JSONB,
  status TEXT
);

CREATE TABLE IF NOT EXISTS trend_signals (
  signal_id TEXT PRIMARY KEY,
  source TEXT NOT NULL,
  text TEXT NOT NULL,
  url TEXT,
  score DOUBLE PRECISION,
  seen_at TEXT
);

CREATE TABLE IF NOT EXISTS trend_cards (
  card_id TEXT PRIMARY KEY,
  kind TEXT,
  title TEXT,
  summary TEXT,
  why_now TEXT,
  signals JSONB,
  sightings INTEGER,
  velocity DOUBLE PRECISION,
  score DOUBLE PRECISION,
  lifespan_days INTEGER,
  fits JSONB,
  recipe JSONB,
  risk JSONB,
  status TEXT,
  applied JSONB,
  first_seen TEXT,
  last_seen TEXT,
  expires_at TEXT,
  updated_at TEXT
);

CREATE TABLE IF NOT EXISTS team_tasks (
  task_id TEXT PRIMARY KEY,
  thread_id TEXT NOT NULL,
  action_index INTEGER,
  owner TEXT,
  kind TEXT,
  title TEXT,
  priority TEXT,
  status TEXT,
  progress TEXT,
  blocked_reason TEXT,
  detail JSONB,
  created_by TEXT,
  created_at TEXT,
  updated_at TEXT
);

CREATE TABLE IF NOT EXISTS team_messages (
  id BIGSERIAL PRIMARY KEY,
  thread_id TEXT NOT NULL,
  role TEXT NOT NULL,
  agent_key TEXT,
  content TEXT,
  created_at TEXT,
  meta JSONB
);

CREATE TABLE IF NOT EXISTS competitor_snapshots (
  snapshot_id TEXT PRIMARY KEY,
  handle TEXT NOT NULL,
  platform TEXT NOT NULL,
  name TEXT,
  captured_at TEXT,
  followers DOUBLE PRECISION,
  media_count INTEGER,
  profile JSONB,
  compare_to JSONB
);

CREATE TABLE IF NOT EXISTS comments (
  comment_id TEXT PRIMARY KEY,
  account_id TEXT NOT NULL,
  brand_id TEXT,
  platform TEXT NOT NULL,
  platform_post_id TEXT,
  platform_comment_id TEXT,
  parent_id TEXT,
  author TEXT,
  text TEXT,
  created_at TEXT,
  like_count INTEGER,
  is_ours BOOLEAN NOT NULL DEFAULT FALSE,
  category TEXT,
  sentiment TEXT,
  priority INTEGER,
  status TEXT,
  escalation TEXT,
  draft_reply TEXT,
  draft_gaps JSONB,
  reply_text TEXT,
  decided_by TEXT,
  decided_at TEXT,
  replied_at TEXT,
  send_result JSONB,
  fetched_at TEXT
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

CREATE INDEX IF NOT EXISTS ix_scout_source ON scout_moments(source_id, potential);

CREATE INDEX IF NOT EXISTS ix_scout_brand ON scout_moments(brand_id, potential);

CREATE UNIQUE INDEX IF NOT EXISTS ux_experiment_arms_variant ON experiment_arms(variant_id);

CREATE INDEX IF NOT EXISTS ix_team_messages_thread ON team_messages(thread_id, id);

CREATE UNIQUE INDEX IF NOT EXISTS ux_team_tasks_action ON team_tasks(thread_id, action_index);

CREATE INDEX IF NOT EXISTS ix_trend_signals_seen ON trend_signals(seen_at);

CREATE INDEX IF NOT EXISTS ix_competitor_handle ON competitor_snapshots(handle, captured_at);

CREATE INDEX IF NOT EXISTS ix_comments_status ON comments(status, priority);

CREATE INDEX IF NOT EXISTS ix_comments_created ON comments(created_at);

CREATE INDEX IF NOT EXISTS ix_jobs_state ON publish_jobs(state, next_attempt_at);

CREATE INDEX IF NOT EXISTS ix_sched_slot ON schedules(slot_at);

