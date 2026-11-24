PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS streamers (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  twitch_channel TEXT NOT NULL UNIQUE,
  credit_handle TEXT NOT NULL,
  campaign_name TEXT,
  required_hashtags TEXT,
  ad_disclosure_text TEXT,
  max_payout_cap_views INTEGER,
  submission_window_minutes INTEGER DEFAULT 30,
  enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0,1)),
  spike_ratio_threshold REAL,
  mode TEXT NOT NULL DEFAULT 'always' CHECK (mode IN ('always','gta6_only'))
);

CREATE TABLE IF NOT EXISTS clips (
  short_id TEXT PRIMARY KEY CHECK (length(short_id)=6),
  streamer_id INTEGER NOT NULL REFERENCES streamers(id),
  spike_ts REAL NOT NULL,
  spike_ratio REAL,
  chat_excerpt TEXT,
  raw_path TEXT,
  sanitized_path TEXT,
  master_916_path TEXT,
  preview_480p_path TEXT,
  hook_text TEXT,
  hashtags TEXT,
  layout TEXT,
  recut_prompt TEXT,
  recut_params TEXT,
  status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN
    ('queued','sanitizing','rendering','awaiting_approval','approved','skipped','published','failed')),
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_clips_status ON clips(status);

CREATE TABLE IF NOT EXISTS publications (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  clip_short_id TEXT NOT NULL REFERENCES clips(short_id),
  platform TEXT NOT NULL CHECK (platform IN ('tiktok','instagram','youtube')),
  live_url TEXT,
  published_at TEXT,
  bounty_submitted INTEGER NOT NULL DEFAULT 0 CHECK (bounty_submitted IN (0,1)),
  via TEXT,                               -- 'upload_post' | 'native'
  UNIQUE (clip_short_id, platform)
);
