Save the specification below as `CLAUDE.md` (or `HANDOFF.md`) in your new project root on your openSUSE Leap 16 host, then launch `claude` in that directory to begin building module by module.

---

# Project Handoff: Automated GTA VI Streamer Clipping & Multi-Platform Distribution Pipeline

## 1. Mission & Dual-Track Monetization Scope

Build a CLI-native, 24/7 automated clipping, audio-sanitizing, human-validated, and multi-platform publishing pipeline targeting *Grand Theft Auto VI* (releasing November 19, 2026) live streams on Twitch.

* **Track A — Automated Streamer Clipping (Scope of this Repository):** Monitor designated Twitch streamers live, detect viral chat spikes automatically, slice raw footage, strip copyrighted music stems, reframe to 9:16 with facecam inset and captions via **OpenShorts**, route a compressed preview to **Telegram** for 10-second human approval, publish simultaneously to brand-new **TikTok**, **Instagram Reels**, and **YouTube Shorts** channels via **Upload-Post**, and echo live URLs back to Telegram for rapid submission to paid clipping campaigns (e.g., Whop Content Rewards, Clipping Nation, Reach).
* **Track B — Traditional Brand UGC (Strictly Decoupled — Out of Automation Scope):** Flat-rate ($75–$250+/video) brand product ads (Aurelia, Billo, JoinBrands, SideShift, Trybe, Insense) must **never** touch this automated pipeline or these social handles. Brand UGC is filmed, edited, and archived manually on the secondary workstation (Acer Nitro AN515) as raw unwatermarked masters.

---

## 2. Target Hardware, OS & Compute Policy Constraints

### Primary 24/7 Host (This Machine)

* **Hardware:** HP ProBook 4 G1i 14", Intel Core Ultra 7 255U (12 cores / 14 threads: 2P + 8E + 2 LPE), **16 GB DDR5-5600 RAM**, **512 GB NVMe SSD**, integrated **Intel Xe Media Engine** (Quick Sync / VA-API supporting hardware H.264, HEVC, VP9, AV1). No discrete CUDA GPU.
* **OS:** **openSUSE Leap 16** (native Linux Docker / Docker Compose—zero WSL2 VM overhead).
* **Mandatory Resource Tuning:**
* Run OpenShorts in its default **CPU-only Docker build** (leverages its recent fast `libx264` CPU preset).
* Pass `/dev/dri/renderD128` into FFmpeg/n8n containers for hardware-accelerated Intel VA-API encoding of 480p Telegram previews and fast remuxing.
* **Strict Serialization (`concurrency = 1`):** Inside 16 GB RAM, never run Demucs/audio-separator and OpenShorts Whisper transcription concurrently. Enforce a queue lock in n8n or the local worker.
* **Disk Hygiene:** Enforce a rolling 30-minute FFmpeg ring buffer per monitored stream (~1.5 GB/stream) and an automated cleanup cron/n8n job that deletes local raw segments and rendered MP4s 48 hours after Upload-Post confirms publication.



### Excluded Hardware & Hard Policy Boundaries

* **Do NOT use Picasso HPC (UMA / SCBI / RES):** Picasso operates under Red Española de Supercomputación (RES) terms strictly restricted to non-profit research. Zero commercial clipping, UGC, or monetized vLLM/Slurm jobs may touch Picasso.
* **Do NOT deploy OpenShorts GPU mode on Acer Nitro AN515 (GTX 1050 4 GB):** OpenShorts' GPU build pins CUDA 13 and `onnxruntime-gpu`, both of which dropped Pascal (`sm_61`), and 4 GB VRAM falls short of the 8 GB minimum required for `large-v3-turbo` fp16 + detection models. Reserve the Nitro (24 GB RAM, 1 TB SSD) strictly for manual Brand UGC editing and cold archival.
* **Scale-Out Escape Hatch:** If launch-week volume exceeds CPU throughput across 5+ simultaneous streams, deploy the exact same `docker-compose.yml` to a commercial spot GPU rental (e.g., RunPod / Vast.ai with an RTX 4060-class 8 GB+ NVIDIA GPU).

---

## 3. Core Architecture & End-to-End Data Flow

```text
[Twitch Live Streams (Target Campaign Streamers)]
       │                                    │
       ▼ (Streamlink 8.6+ -> stdout)        ▼ (Anonymous IRC :6667, nick justinfan1234)
┌─────────────────────────────┐    ┌──────────────────────────────────────────┐
│ FFmpeg Rolling Ring Buffer  │    │ Python IRC Spike Watcher Daemon          │
│ 30 x 60s segments (~1.5 GB) │    │ • 10s message bins vs. 5-min rolling avg │
│ (auto-overwrites oldest)    │    │ • Trigger: >= 3x baseline                │
└──────────────┬──────────────┘    │ • Cooldown lock: 90s dedup window        │
               │                   └────────────────────┬─────────────────────┘
               │ (Concat last 2–3 segments,             │ (Passes spike timestamp +
               │  probe non-zero start_time,            │  45s chat transcript)
               │  slice [spike-30s .. +15s])            │
               ▼                                        ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ Ingestion Worker + SQLite State DB (`pipeline.db`)                          │
│ 1. Logs clip record (`clip_id`, streamer, campaign tags, chat window)       │
│ 2. Audio Stem Sanitizer (Demucs / audio-separator) -> strips music stems    │
│    (GTA VI plays licensed radio/podcasts ON FOOT via earbuds)               │
│ 3. Optional Claude Code / LLM call: turns chat spike into 2s viral hook     │
└───────────────────────────────────────┬─────────────────────────────────────┘
                                        │ POST /api/process (HMAC webhook_url)
                                        ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ OpenShorts Engine (`mutonby/openshorts`, Docker CPU Mode, :8000 / :5175)    │
│ • Whisper transcription + word-level captions                               │
│ • `screencast` / `facecam_inset` 9:16 layout + hook pill (`auto_hook: true`)│
│ • GTA VI prompt rules (stunts, chases, combat, NPC chaos, roleplay)         │
└───────────────────────────────────────┬─────────────────────────────────────┘
                                        │ HMAC-SHA256 Completion Webhook
                                        ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ n8n Orchestrator (Self-Hosted Docker, Concurrency = 1)                      │
│ 1. Generates 480p Telegram proxy (<15 MB, -movflags +faststart, VA-API)     │
│ 2. Sends preview to Telegram with inline buttons (`approve:<id>`, `skip:<id>`)│
│    (Callback payload strictly <= 64 bytes; metadata lives in SQLite)        │
└───────────────────────────────────────┬─────────────────────────────────────┘
                                        │ Human Tap: "Approve"
                                        ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ Publishing & Bounty Feedback Loop (n8n -> Upload-Post API / MCP)            │
│ 1. Randomized Wait jitter (15–45 min) to avoid robotic burst patterns       │
│ 2. POST https://api.upload-post.com/api/upload (TikTok, Instagram, YouTube) │
│    (Injects streamer @handle credit, campaign tags, ad disclosures)         │
│ 3. Echoes live TikTok/IG/YT URLs back to Telegram (for 30-min bounty window)│
│ 4. Schedules 48h local file deletion                                        │
└─────────────────────────────────────────────────────────────────────────────┘

```

---

## 4. Verified Tool Stack, Endpoints & Technical Gotchas

### A. Clipping & Reframing Engine: `mutonby/openshorts`

* **Repo / License:** `[https://github.com/mutonby/openshorts](https://github.com/mutonby/openshorts)` (MIT — commercial use permitted, $0 software cost, no watermarks).
* **Ports & Surfaces:** Dashboard on `http://localhost:5175`; REST API + OpenAPI docs + Streamable-HTTP MCP server (`mcp_server.py`) on `http://localhost:8000` (`http://localhost:8000/mcp`).
* **Reference Assets Inside Repo:**
* Agent skill directory: `skills/openshorts` (copy into `.claude/skills/openshorts`).
* Reference n8n workflow: `examples/n8n/openshorts-content-machine.json` (also published as n8n template #18584).


* **LLM & Gemini Nuance (CRITICAL):**
* OpenShorts requires a **programmatic API**, not a Claude Pro consumer web subscription.
* Moment picking can be pointed at any OpenAI-compatible server via `.env`: `LLM_BASE_URL`, `LLM_MODEL`, and optional `LLM_API_KEY`. If using a local LLM (e.g., Ollama / `llama.cpp`), **minimum 16,384-token context** (`OLLAMA_CONTEXT_LENGTH=16384`) and a **7B–8B+ model** are mandatory—Ollama's default 4096 context silently truncates transcripts and 3B models break JSON.
* **Frame-vision caveat:** Features that inspect video frames—automatic layout picker (`AUTO_LAYOUT`), on-screen content detection, and silent clips—**still require a `GEMINI_API_KEY**`; without it, OpenShorts falls back to plain face-tracking crop. **Recommended setup on 16 GB RAM:** Keep a free-tier `GEMINI_API_KEY` configured in `.env` (zero local LLM RAM contention, full visual layout detection) and reserve all 16 GB RAM for Whisper + FFmpeg + Demucs.


* **Rejected Tool (`ColinGPT9/clips-studio` & `Grand-Theft-Auto-6-Clips-Kitty-Plugin`):** Desktop GUI app (AGPL-3.0) lacking headless webhook/MCP architecture. The GTA 6 plugin relies on Clips Kitty's internal Python SDK and cannot run in OpenShorts. **Replacement:** Replicate its scoring logic directly in OpenShorts / Claude prompts (prioritizing high-retention chases, crashes, stunts, combat, NPC chaos, and roleplay reactions).

### B. Multi-Platform Publishing: `upload-post.com`

* **Why:** Eliminates maintaining 3 separate OAuth apps and TikTok's unaudited `SELF_ONLY` private-upload lock.
* **Plan Requirement:** Free tier allows 10 uploads/month (YouTube & Instagram only). **TikTok API publishing requires the Basic plan** ($24/mo or $16/mo billed annually) with unlimited uploads.
* **n8n Integration:** Official built-in node `n8n-nodes-upload-post` (search "Upload Post" in updated n8n) or direct HTTP `POST [https://api.upload-post.com/api/upload](https://api.upload-post.com/api/upload)` (`multipart/form-data` with `Authorization` header, `user`, `platform[]`, `title`, `video`).
* **MCP Server (MIT, free with any plan):** Hosted Streamable-HTTP at `[https://mcp.upload-post.com/mcp](https://mcp.upload-post.com/mcp)` with header `Authorization: ApiKey YOUR_UPLOAD_POST_API_KEY`, or local stdio via `npx -y @upload-post/mcp`.

### C. Live Twitch Capture: `streamlink` (v8.6+) & Anonymous IRC

* **Streamlink 8.x Breaking Changes (CRITICAL):**
* Since Streamlink 8.0, **Twitch ad filtering is mandatory by default and `--twitch-disable-ads` is deprecated**. Do NOT pass `--twitch-disable-ads` on Streamlink 8.x.
* Higher-quality Twitch streams now deliver **fragmented MPEG-4 (`.mp4`)** instead of `.ts`, and **always carry a non-zero presentation time offset** pointing to the broadcast start. Always inspect segments with `ffprobe` or normalize timestamps (`-reset_timestamps 1`) inside the FFmpeg segment muxer before slicing.
* Codec support can be declared via `--twitch-supported-codecs h264,h265,av1` if desired (the ProBook's Intel Xe engine decodes all three).
* Reference ring-buffer command:
```bash
streamlink twitch.tv/CHANNEL best --retry-streams 30 -O | \
ffmpeg -hide_banner -loglevel warning -i pipe:0 -c copy \
  -f segment -segment_time 60 -segment_wrap 30 -reset_timestamps 1 \
  ./buffer/CHANNEL/seg_%02d.ts

```




* **Anonymous Twitch IRC Watcher:** Connect read-only to `irc.chat.twitch.tv:6667` with `NICK justinfan1234` and `JOIN #channel` (no OAuth token required). Compute 10-second message counts vs. a 5-minute sliding baseline in pure Python (never burn LLM tokens counting lines). Only invoke Claude/LLM when a `>= 3x` spike occurs (outside a 90-second cooldown lock) to summarize the 45-second chat window into a 2-second hook banner.

### D. Telegram Bot API Constraints (Validation Gate)

* **File Size Limits:** Telegram caps `multipart/form-data` uploads at **50 MB** and HTTPS URL fetches at **20 MB**. Always transcode a fast 480p preview under **15 MB** (`-movflags +faststart`, VA-API accelerated) for Telegram review while storing the untouched 1080p master MP4 on disk for Upload-Post.
* **Callback Data Limit:** Inline keyboard `callback_data` is strictly capped at **64 bytes**. Store all clip metadata in SQLite and pass only compact keys (`ok:<short_id>`, `no:<short_id>`, `recut:<short_id>`).

---

## 5. Mandatory Copyright, Monetization & Platform Guardrails (Nov 2026)

Build these rules into the audio pipeline, OpenShorts prompts, n8n caption templates, and Telegram checklist:

1. **Always Scrub Licensed Audio (On-Foot Earbuds Rule):** *GTA VI* allows characters to play licensed radio stations and podcasts **on foot via smartphones/earbuds**, not just inside vehicles, and both the radio and Justice's score are protected recordings. Run `demucs` or `audio-separator` on **every** extracted clip to strip music stems while retaining streamer voice + game SFX.
2. **Never Post Standalone Cutscenes:** Rockstar explicitly prohibits unedited story cutscenes, endings, or standalone in-game TV/radio shows. Only approve clips driven by live streamer reactions, webcam framing, or commentary.
3. **YouTube First-7-Seconds Violence Rule:** Keep graphic violence/gore out of the **first 7 seconds** of every clip so YouTube Shorts stays eligible for full ad revenue. Enforce this in the moment-selection prompt and verify in Telegram.
4. **Transformative Framing & Streamer Credit:** Use `screencast` / `facecam_inset` 9:16 layout + hook pill + word-level captions. Always include the streamer's `@handle` and Twitch/YouTube credit link in descriptions to prevent reused-content demonetization and follow community etiquette.
5. **Monetization Thresholds & Platform Rules:**
* **TikTok:** Keep the channel on a **Personal Account** (Business Accounts are ineligible for Creator Rewards). Creator Rewards requires 10k followers + 100k views (30 days) and pays **only on videos greater than 60 seconds** (target 61–90s for long-term TikTok ad pools; shorter clips work for Content Rewards CPM bounties).
* **YouTube Shorts:** Tier 1 (Shopping/Memberships) unlocks at 500 subs + 3M views (90 days); Tier 2 (45% ad pool) unlocks at 1,000 subs + 10M views (90 days)—note YouTube announced entry thresholds double to 20M Shorts views on Feb 1, 2027.
* **Instagram Reels:** Switch to a **Professional (Creator) Account**; monetize via sponsorships, Creator Marketplace, and comment-to-DM funnels rather than native view payouts.


6. **Content Rewards Campaign Compliance:** Store per-streamer campaign rules in SQLite (`required_hashtags`, `ad_disclosure`, `max_payout_cap_views`, `submission_window_minutes`). After Upload-Post publishes, immediately message the live URLs to Telegram so they can be pasted into the bounty portal within the typical **30-minute window**.
7. **API Quotas & Anti-Spam Jitter:** Respect platform caps (TikTok ~15 API posts/day & 6 starts/min; YouTube 100/day; Instagram 50–100/24h). Ramp brand-new accounts slowly (manual 48h warm-up + 2–4 clips/day in week 1) with 15–45 min randomized Wait jitter in n8n.
8. **AI Transparency (EU AI Act, enforceable Aug 2, 2026):** AI-assisted clipping, captions, and reframing do not require synthetic media labels. Only toggle platform AI-disclosure flags if generating synthetic presenters, cloned voices, or realistic AI visuals.

---

## 6. Claude Code CLI & MCP Setup Commands

Run these commands once OpenShorts and Upload-Post accounts are provisioned:

```bash
# 1. Connect local self-hosted OpenShorts MCP server (Streamable HTTP)
claude mcp add --transport http openshorts http://localhost:8000/mcp

# 2. Connect hosted Upload-Post MCP server (add --scope user to enable globally)
claude mcp add --transport http upload-post https://mcp.upload-post.com/mcp \
  --header "Authorization: ApiKey YOUR_UPLOAD_POST_API_KEY"

# 3. Verify connections (confirm ✔ Connected for both servers)
claude mcp list

# 4. Install the official OpenShorts Agent Skill into this workspace
mkdir -p .claude/skills
cp -r ./vendor/openshorts/skills/openshorts .claude/skills/

```

---

## 7. Target Repository Layout & SQLite Schema

Instruct Claude Code to scaffold and maintain this structure:

```text
gta6-clip-pipeline/
├── CLAUDE.md                        # This handoff spec
├── .env.example                     # Secrets template (Gemini, Upload-Post, Telegram, HMAC)
├── docker-compose.yml               # n8n + OpenShorts (CPU) + Watcher worker + VA-API mount
├── vendor/openshorts/               # Git submodule or clone of mutonby/openshorts
├── watcher/
│   ├── Dockerfile                   # Python 3.12 + streamlink>=8.6 + ffmpeg + ffprobe
│   ├── irc_watcher.py               # Asyncio Twitch IRC client + 3x sliding-window spike detector
│   ├── buffer_manager.py            # Streamlink -> FFmpeg 30x60s ring buffer + safe slice concat
│   └── audio_sanitizer.py           # Serialized Demucs/audio-separator stem scrubber
├── db/
│   ├── schema.sql                   # SQLite tables: streamers, clips, publications
│   └── state.py                     # Thread-safe SQLite helper
├── n8n/
│   └── workflows/
│       ├── gta6-clip-approval.json  # Webhook -> 480p proxy -> Telegram gate -> Jitter -> Upload-Post
│       └── disk-cleanup.json        # Cron: purge raw/master MP4s 48h post-publish
├── prompts/
│   ├── gta6_moment_scoring.txt      # Clips-Kitty-inspired GTA VI scoring + 7s gore guardrail
│   └── hook_generator.txt           # Turns 45s Twitch chat spike into 2s on-screen hook text
└── scripts/
    ├── setup_opensuse.sh            # Verifies Docker, /dev/dri/renderD128 permissions, directories
    └── smoke_test.sh                # End-to-end dry run with private Upload-Post target

```

### Required SQLite Tables (`db/schema.sql`)

* `streamers`: `id`, `twitch_channel`, `credit_handle`, `campaign_name`, `required_hashtags`, `ad_disclosure_text`, `enabled`.
* `clips`: `short_id` (6-char PK for Telegram callback_data), `streamer_id`, `spike_ts`, `spike_ratio`, `chat_excerpt`, `raw_path`, `sanitized_path`, `master_916_path`, `preview_480p_path`, `hook_text`, `status` (`queued`, `sanitizing`, `rendering`, `awaiting_approval`, `approved`, `skipped`, `published`, `failed`), `created_at`.
* `publications`: `id`, `clip_short_id`, `platform` (`tiktok`, `instagram`, `youtube`), `live_url`, `published_at`, `bounty_submitted`.

---

## 8. Phased Build Order & Verification Checklist for Claude Code

Execute phases sequentially; do not advance until each verification passes:

1. **Phase 1 — Host & Container Foundation:** Scaffold repo, `.env.example`, `db/schema.sql`, and `docker-compose.yml` mounting `/dev/dri/renderD128` for VA-API and pinning `openshorts` in CPU-only mode with fast `libx264` preset.
* *Verify:* `http://localhost:5175` and `http://localhost:8000/docs` respond; `ffmpeg -vaapi_device /dev/dri/renderD128` test encode succeeds inside the worker container.


2. **Phase 2 — Live Capture & IRC Spike Daemon:** Implement `buffer_manager.py` (Streamlink 8.6+ without deprecated `--twitch-disable-ads` -> FFmpeg 30x60s segment ring buffer) and `irc_watcher.py` (anonymous `justinfan1234` IRC, 10s bin vs. 5-min rolling average, `>= 3x` trigger, 90s cooldown, `ffprobe`-verified slicing).
* *Verify:* Run against any active Twitch channel for 10 minutes; verify ring buffer caps disk usage, mock a 3x chat burst, and confirm a clean 45-second MP4 slice is logged to SQLite.


3. **Phase 3 — Serialized Audio Sanitizer & OpenShorts Webhook Integration:** Implement `audio_sanitizer.py` with a file/DB mutex so it never overlaps with OpenShorts Whisper in 16 GB RAM. Wire POST calls to OpenShorts `/api/process` with GTA VI scoring rules, `facecam_inset` layout, and signed HMAC completion webhook.
* *Verify:* Feed a clip with background car radio; confirm dialogue/SFX survive while music is removed, and OpenShorts returns a 9:16 captioned MP4.


4. **Phase 4 — n8n Telegram Gate, Jitter & Upload-Post:** Import/adapt `examples/n8n/openshorts-content-machine.json`. Add 480p `<15 MB` VA-API proxy encoding, Telegram inline buttons (`ok:<short_id>`, `no:<short_id>`), 15–45 min jitter Wait node, Upload-Post API publish node (TikTok/IG/YT), Telegram live-URL echo-back, and 48h cleanup cron.
* *Verify:* End-to-end dry run posting with private visibility; confirm Telegram preview plays cleanly, approval updates SQLite, Upload-Post succeeds, and URLs echo back to Telegram.


5. **Phase 5 — Claude Code MCP Wiring:** Register `openshorts` and `upload-post` MCP servers, copy `.claude/skills/openshorts`, and test natural-language recut/scheduling commands from the CLI.


---

# Pipeline Extension Handoff: $0 Native API Publishers, Content Rewards Tracking & Revenue Engine

> Implement this as the publishing, analytics, and revenue layer replacing Upload-Post. Where this section conflicts with the sections above, this section wins.

> **DECISION (2026-11-22) — Hybrid publishing, supersedes "remove Upload-Post":** Upload-Post is KEPT. `PUBLISH_BACKEND=hybrid`:
> * **TikTok (updated 2026-11-23):** native Inbox-draft uploader by default (`TIKTOK_ROUTE=native`, free; human taps Post in the app, watcher discovers the URL). `TIKTOK_ROUTE=upload_post` restores the paid Basic-plan route.
> * **YouTube / Instagram:** through Upload-Post until its monthly allowance (`UPLOAD_POST_MONTHLY_LIMIT`, default 10) is consumed, then through the native APIs (`watcher/native_publishers.py`). A quota refusal from Upload-Post also triggers a native retry.
> * Usage is counted from `publications.via` (`upload_post` | `native`). The Upload-Post MCP/API key stay in `.env`; Phase B's "remove Upload-Post" step is cancelled.
> * Routing lives in `watcher/publishing.py::route_platforms` (tests: `tests/test_routing.py`).

## 1. Architecture Delta (Supersedes Upload-Post in the sections above)

Remove all `upload-post.com` dependencies, API keys, and MCP entries from `CLAUDE.md` and `docker-compose.yml`. Replace them with a **$0/month native API publishing layer** plus a **SQLite + Telegram monetization tracker** running inside your existing openSUSE Leap 16 / n8n / Python stack:

* **Publishing:** Direct YouTube Data API v3 (`videos.insert`), Instagram Graph API (Facebook Login resumable Reels upload), and TikTok unaudited Inbox draft uploader (`video.upload` via `FILE_UPLOAD`).
* **Auto-Discovery & Stats Polling:** Daily view/retention ingestion from YouTube `videos.batchGetStats`, Instagram Media Insights (`views` / `total_views` / `reels_skip_rate`), and TikTok Display API (`video.list` + `video.query`).
* **Revenue Operations:** Campaign brief rules, 30-minute Content Rewards submission countdowns in Telegram, automated CPM/cap/fee math, 10-day payout-round tracking, YPP milestone tracking, and streamer retainer-pitch alerts.
* **Hardware Guardrails Unchanged:** Run strictly on the HP ProBook 4 G1i (16 GB RAM, Intel Xe VA-API, `concurrency = 1` between Demucs and OpenShorts). Zero commercial workloads on Picasso HPC.

---

## 2. Native Publishing & Token Lifecycle Spec (`publishers/`)

### A. Central OAuth Token Store (`db/tokens.py`)

Never rely on static `.env` tokens after initial bootstrap. Seed tokens from `.env` into SQLite table `oauth_tokens` on first boot, then let a scheduled worker/n8n cron refresh and persist updated tokens automatically:

* **YouTube:** Permanent refresh token (app set to *In Production*, unverified); exchange on demand for short-lived access tokens.
* **Instagram:** 60-day long-lived token; refresh automatically every 7 days via cron.
* **TikTok:** Access token expires every **24 hours** and refresh token lasts **365 days**. Refresh every 12 hours and **always overwrite both `access_token` and `refresh_token` in SQLite**, because TikTok may rotate the refresh token on each call.

### B. Platform Publishers

1. **YouTube Shorts (`publishers/youtube_pub.py`):**
* Endpoint: `videos.insert` (resumable upload). Dedicated default quota bucket: **100 uploads/day** (1 unit per call).
* **Mandatory Nov 2026 Payload Fields:** Explicitly set `"status": {"privacyStatus": "public", "selfDeclaredMadeForKids": false}` and `"paidProductPlacementDetails": {"hasPaidProductPlacement": false}`—since November 16, 2026, omitting `hasPaidProductPlacement` no longer defaults to `false`.
* **Circuit Breaker:** HTTP `403 quotaExceeded` still consumes 1 unit per failed retry. On any `quotaExceeded` error, flip a `youtube_paused_until` flag in SQLite and halt retries until the next daily window.

2. **Instagram Reels (`publishers/instagram_pub.py`):**
* **Critical Account Prerequisite:** Switch the Instagram channel to a **Business account** (not Creator) before connecting—Content Rewards requires Instagram accounts to be Business accounts to verify and track views. Link it to a free Facebook Page to unlock direct local binary uploads via Facebook Login. (This supersedes the Professional/Creator account note in section 5.5 above.)
* **3-Step Resumable Flow (Graph API v25–v27):**
  1. `POST https://graph.facebook.com/v27.0/{ig_user_id}/media?media_type=REELS&upload_type=resumable` (with caption, streamer credit, campaign tags).
  2. Stream the local 1080p master MP4 to the returned `rupload.facebook.com` URI.
  3. Poll `GET /{container_id}?fields=status_code` until `FINISHED`, then `POST /{ig_user_id}/media_publish` and save the returned `permalink` and `media_id`.
* **Originality Guardrail:** Instagram's April 30, 2026 recommendation policy excludes unoriginal repost accounts—borders, watermarks, subtitles, or caption credits alone do not qualify as original, so always enforce transformative facecam framing, hooks, or commentary.

3. **TikTok Unaudited Inbox Draft Uploader (`publishers/tiktok_inbox_pub.py`):**
* **Scopes (Sandbox Mode, Personal Account):** Request `user.info.basic`, `video.upload`, and `video.list`. Leave `video.publish` unchecked so you bypass Direct Post's forced-private sandbox lock.
* **Upload Flow:** Call `POST https://open.tiktokapis.com/v2/post/publish/inbox/video/init/` with `"source": "FILE_UPLOAD"` (rate cap: 6 init calls/min; up to 5 pending drafts per 24h) and `PUT` the local MP4 chunks to the returned `upload_url`.
* **Human Handoff + Auto-Discovery:** Immediately send a Telegram message containing the **ready-to-copy TikTok caption/tags** so you can open the TikTok App Inbox, paste, optionally attach a trending sound, and tap **Post** publicly. Then poll `POST https://open.tiktokapis.com/v2/video/list/` every 3 minutes for up to 30 minutes to detect the newly published public TikTok `id` and `share_url`, write it to SQLite, and fire the Content Rewards countdown alert.

---

## 3. Content Rewards & Monetization Operations (`monetization/`)

Content Rewards (`contentrewards.com`) split into its own app from Whop in August 2026, while Whop remains the payout processor (free transfer from Content Rewards Balance to Whop Balance starting at $1, then to bank/PayPal). Connect your posting accounts **directly inside Content Rewards** (accounts linked only inside Whop settings do not count).

### A. 30-Minute Submission Countdown & Telegram Actions

Every clip must be submitted to Content Rewards **within 30 minutes of posting** so views count from zero. Build this exact loop in n8n + Telegram:

1. The moment a live URL is obtained (immediately after YouTube/Instagram publish, or upon TikTok `video.list` discovery), send a high-priority Telegram card containing:
* Campaign name, required hashtags/disclosures check, and **tappable live URL**.
* Exact deadline timestamp (`published_at + 30 min`).
* Inline buttons (payload strictly under 64 bytes): `sub:{pub_id}` ("Mark Submitted"), `skipsub:{pub_id}` ("Not Submitting").

2. Schedule a Telegram reminder ping at **T+15 minutes** and an urgent ping at **T+25 minutes** if `bounty_status` is still `pending_submission`.
3. Once marked `submitted`, add follow-up Telegram commands (`/approve {pub_id}`, `/reject {pub_id}`) to mirror manual brand review status (every clip gets a bot score and human review; rejected clips can be appealed once; **never edit captions or delete submitted posts**).

### B. Payout & Fee Calculator (`monetization/cpm_calculator.py`)

Implement exact payout math matching Content Rewards' current creator terms:

* **Fee Rate:** `0.10` (10%) on all `cpm` and `pool` campaigns, and on `per_post` / `retainer` campaigns whose total budget is **under $5,000**; `0.00` (0%) on `per_post` / `retainer` campaigns with budget **at least $5,000**.
* **CPM Formula:**

```python
# Views earn for 30 days post-submission in 10-day rounds (7d earning + 3d hold)
gross = (counted_views / 1000.0) * cpm_rate
if gross < min_payout_usd:
    net_estimated = 0.0
else:
    capped_gross = min(gross, max_payout_cap_usd)
    net_estimated = round(capped_gross * (1.0 - creator_fee_rate), 2)
```

* **Payout Round Tracking:** For each `approved` CPM clip, auto-generate three 10-day rounds (Days 1–7 earning + Days 8–10 hold; Days 11–17 + 3-day hold; Days 21–27 + 3-day hold, capping at Day 30) transitioning across `queued` → `arriving` → `received`.
* **Per-Post Model:** Pays roughly 15 minutes after approval once minimum views are reached.

---

## 4. Daily Analytics Poller & Feedback Loop (`analytics/`)

Run a scheduled poller every 6 hours (and a daily 09:00 CET Telegram digest) to persist permanent metrics in SQLite (critical because Instagram account insights only retain 90 days):

| Platform | Endpoint & Quota | Metrics to Store in `metric_snapshots` | Key 2026 Nuance |
| --- | --- | --- | --- |
| **YouTube** | `videos.batchGetStats` (**1 unit** in its own 10,000/day bucket) | `viewCount`, `likeCount`, `commentCount` | Public `viewCount` includes TV co-views since Nov 16, 2026; YPP counts **Engaged Views** (no co-views) |
| **Instagram** | `GET /{media_id}/insights` (Graph API v27.0) | `views`, `total_views`, `reach`, `reels_skip_rate`, `saved_count`, `shares_count`, `total_interactions` | `impressions` and `plays` are deprecated; use `reels_skip_rate` (3-second skip %) to tune OpenShorts hooks |
| **TikTok** | `POST /v2/video/query/` (up to 20 IDs/req, 600 req/min) | `view_count`, `like_count`, `comment_count`, `share_count` | Requires `video.list` scope; Sandbox allows reading your authorized target user's posts |

### Automated Growth & Monetization Alerts (Daily Telegram Digest)

1. **Earnings & Cashflow Summary:** Total views (24h & cumulative), estimated `queued` / `arriving` / `received` net USD across active campaigns, and any clip approaching its `max_payout_cap_usd` (signal to rotate to a fresh clip/campaign).
2. **Hook Diagnostics:** Flag clips with `reels_skip_rate` above 60% so you can refine `prompts/hook_generator.txt`.
3. **Streamer Retainer Pitch Trigger:** Automatically alert Telegram whenever any clip crosses **50,000 views** (or a streamer's clips cross 150,000 combined views in 14 days) with a pre-drafted DM pitch to negotiate a direct monthly retainer (eliminating the 10% marketplace fee).
4. **Long-Term Native Monetization Tracker:**
* **YouTube YPP Ladder:** Track rolling 90-day Shorts views against **Tier 1** (500 subs + 3M views for Shopping/Fan Funding) and **Tier 2** (1,000 subs + 10M views for 45% ad pool), plus a reminder to accept updated YPP terms before **January 31, 2027** and maintain the 10M rolling 90-day floor starting **February 1, 2027**.
* **TikTok Regional Note:** Do not build VPN workarounds for TikTok Creator Rewards if the account is registered in Spain; track TikTok strictly as a Content Rewards, affiliate, and brand-deal funnel unless TikTok Studio natively enables Creator Rewards for the account.

---

## 5. SQLite Schema Additions (`db/schema_monetization.sql`)

Append these tables to `pipeline.db` and migrate safely with `CREATE TABLE IF NOT EXISTS`:

```sql
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
```

---

## 6. Target File Additions & `.env.example` Update

```text
gta6-clip-pipeline/
├── db/
│   ├── schema_monetization.sql      # Tables above
│   └── tokens.py                    # SQLite OAuth seed, auto-refresh & YT 403 circuit breaker
├── publishers/
│   ├── youtube_pub.py               # Resumable videos.insert + Nov 2026 paidProductPlacementDetails
│   ├── instagram_pub.py             # Graph API v27.0 Facebook-Login resumable Reels upload
│   ├── tiktok_inbox_pub.py          # Unaudited /inbox/video/init/ FILE_UPLOAD + video.list watcher
│   └── dispatcher.py                # Unified publish entrypoint called by n8n after Telegram Approve
├── monetization/
│   ├── campaign_cli.py              # CLI to add/edit/pause campaigns & link streamers
│   ├── cpm_calculator.py            # Net USD math, cap enforcement, 10-day payout rounds
│   └── bounty_watchdog.py           # 15m/25m Telegram submission reminders + callback handlers
├── analytics/
│   ├── stats_poller.py              # YT batchGetStats + IG Insights + TT video.query -> SQLite
│   └── daily_digest.py              # 09:00 CET Telegram report (views, cashflow, skip rate, YPP, retainer alerts)
└── n8n/workflows/
    ├── gta6-clip-approval.json      # Updated: calls publishers/dispatcher.py instead of Upload-Post
    └── token-and-stats-cron.json    # Schedules token refresh (12h), stats poll (6h), digest (24h)
```

Ensure `.env.example` includes:

```env
# Telegram
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=

# YouTube Data API v3 (OAuth 2.0 Production Unverified)
YOUTUBE_CLIENT_ID=
YOUTUBE_CLIENT_SECRET=
YOUTUBE_REFRESH_TOKEN=

# Instagram Graph API (Business Account + Facebook Login for resumable upload)
IG_AUTH_MODE=facebook_login
IG_APP_ID=
IG_APP_SECRET=
IG_USER_ID=
IG_ACCESS_TOKEN=

# TikTok Sandbox (Personal Account, scopes: user.info.basic,video.upload,video.list)
TIKTOK_CLIENT_KEY=
TIKTOK_CLIENT_SECRET=
TIKTOK_OPEN_ID=
TIKTOK_ACCESS_TOKEN=
TIKTOK_REFRESH_TOKEN=
```

---

## 7. Phased Build Order & Verification Checklist (Extension Phases A–D)

Execute in order and verify every step before moving forward:

1. **Phase A — Schema, Token Store & Campaign CLI:** Apply `db/schema_monetization.sql`, build `db/tokens.py` (seeding from `.env`, TikTok rotating refresh persistence, Instagram 60-day refresh, YouTube 403 circuit breaker), and build `monetization/campaign_cli.py`.
* *Verify:* Unit test token refresh mocks (confirm TikTok refresh token overwrite persists in SQLite) and create a sample GTA VI CPM campaign (`$1.00 CPM`, `$200 cap`, `10% fee`) via CLI.

2. **Phase B — Native Publishers & Dispatcher:** Build `youtube_pub.py`, `instagram_pub.py`, `tiktok_inbox_pub.py`, and `dispatcher.py`. Remove Upload-Post references from `CLAUDE.md`, `docker-compose.yml`, and n8n workflows.
* *Verify:* Dry-run a 5-second test 9:16 clip. Confirm YouTube returns a video ID (check whether visibility stays `public` or triggers the Compliance Audit lock), Instagram publishes a Reels container via resumable upload, and TikTok drops the draft in Inbox + sends the copy-paste caption to Telegram + discovers the public `share_url` via `video.list`.

3. **Phase C — 30-Minute Bounty Watchdog & Payout Engine:** Implement `cpm_calculator.py` and `bounty_watchdog.py` wired to Telegram inline callbacks (`sub:{id}`, `skipsub:{id}`) and commands (`/approve`, `/reject`, `/earnings`).
* *Verify:* Simulate a published clip with a compressed 2-minute deadline; confirm Telegram fires reminders, tapping **Mark Submitted** updates SQLite, and `/approve` generates the three 10-day `payout_rounds` rows.

4. **Phase D — Analytics Poller & Daily Digest:** Implement `stats_poller.py` (YouTube `videos.batchGetStats`, Instagram `views`/`total_views`/`reels_skip_rate`, TikTok `video.query`) and `daily_digest.py`, plus `token-and-stats-cron.json` in n8n.
* *Verify:* Poll your test uploads, confirm `metric_snapshots` rows populate cleanly, and trigger a manual daily digest to Telegram showing views, estimated net payout, hook skip-rate alerts, and YPP 90-day progress.

### First Prompt for Claude Code

```text
Read CLAUDE.md completely. Update our architecture to remove Upload-Post and implement the $0 native API publishers (YouTube Data API v3, Instagram Graph API resumable upload, TikTok Sandbox Inbox + video.list discovery), the SQLite token rotation store, the Content Rewards 30-minute bounty watchdog, and the daily analytics/earnings digest. Start with Phase A (schema migration, db/tokens.py with unit tests, and monetization/campaign_cli.py) and verify all tests pass before moving to Phase B.
```
