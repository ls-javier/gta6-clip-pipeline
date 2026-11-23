-- Monetization layer (see CLAUDE.md, Pipeline Extension Handoff §5). Idempotent.

CREATE TABLE IF NOT EXISTS oauth_tokens (
    platform TEXT PRIMARY KEY,          -- 'youtube', 'instagram', 'tiktok'
    access_token TEXT NOT NULL,
    refresh_token TEXT,
    expires_at INTEGER NOT NULL,        -- Unix timestamp
    extra_json TEXT,                    -- open_id, ig_user_id, circuit_breaker_until
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS campaigns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform_source TEXT DEFAULT 'content_rewards', -- 'content_rewards', 'retainer', 'affiliate'
    name TEXT NOT NULL,
    streamer_id INTEGER REFERENCES streamers(id),
    payment_model TEXT NOT NULL,        -- 'cpm', 'per_post', 'retainer', 'pool'
    cpm_rate_usd REAL DEFAULT 0.0,
    flat_fee_usd REAL DEFAULT 0.0,
    min_payout_usd REAL DEFAULT 0.0,
    max_payout_cap_usd REAL DEFAULT 200.0,
    total_budget_usd REAL DEFAULT 1000.0,
    creator_fee_rate REAL DEFAULT 0.10, -- 0.0 if per_post/retainer & budget >= 5000
    submission_window_min INTEGER DEFAULT 30,
    earning_window_days INTEGER DEFAULT 30,
    allowed_platforms TEXT NOT NULL,    -- JSON array: ["tiktok","instagram","youtube"]
    required_tags TEXT,
    ad_disclosure TEXT,
    status TEXT DEFAULT 'active',       -- 'active', 'paused', 'depleted'
    created_at INTEGER NOT NULL
);

-- Extend publications tracking for native APIs + bounty workflow
CREATE TABLE IF NOT EXISTS publications_v2 (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    clip_short_id TEXT NOT NULL REFERENCES clips(short_id),
    campaign_id INTEGER REFERENCES campaigns(id),
    platform TEXT NOT NULL,             -- 'youtube', 'instagram', 'tiktok'
    platform_video_id TEXT,
    live_url TEXT,
    published_at INTEGER,
    submit_deadline_at INTEGER,         -- published_at + (submission_window_min * 60)
    bounty_status TEXT DEFAULT 'pending_submission',
        -- 'pending_submission', 'submitted', 'approved', 'rejected', 'appealed', 'not_applicable'
    submitted_at INTEGER,
    approved_at INTEGER,
    last_views INTEGER DEFAULT 0,
    est_net_usd REAL DEFAULT 0.0,
    reminded_15m INTEGER DEFAULT 0,
    reminded_25m INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS metric_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    publication_id INTEGER NOT NULL REFERENCES publications_v2(id),
    captured_at INTEGER NOT NULL,
    views INTEGER DEFAULT 0,
    total_views INTEGER DEFAULT 0,      -- IG Facebook-Login aggregated views
    likes INTEGER DEFAULT 0,
    comments INTEGER DEFAULT 0,
    shares INTEGER DEFAULT 0,
    saves INTEGER DEFAULT 0,
    reels_skip_rate REAL                -- IG 3-second skip rate (%)
);

CREATE TABLE IF NOT EXISTS payout_rounds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    publication_id INTEGER NOT NULL REFERENCES publications_v2(id),
    round_number INTEGER NOT NULL,      -- 1, 2, 3 (10-day rounds up to Day 30)
    period_start INTEGER NOT NULL,
    period_end INTEGER NOT NULL,
    hold_release_at INTEGER NOT NULL,   -- period_end + 3 days
    net_usd REAL DEFAULT 0.0,
    state TEXT DEFAULT 'queued'         -- 'queued', 'arriving', 'received'
);
