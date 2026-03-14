# astrbot_plugin_rss_forwarder (English)

[中文](./README.md) | [日本語](./README.ja.md)

`astrbot_plugin_rss_forwarder` is an AstrBot plugin focused on RSS / RSSHub delivery orchestration. It fetches updates from multiple feeds and proactively pushes them to configured chat targets using panel-driven routing rules.

## Positioning

This project is not meant to be a drop-in clone of [`Soulter/astrbot_plugin_rss`](https://github.com/Soulter/astrbot_plugin_rss). Its current focus is broader:

- persistent deduplication that survives restarts
- panel-friendly visual configuration for feeds, targets, jobs, and delivery modes
- startup-safe scheduling and invalid-target suppression for real deployments
- future enrichment with translation, summarization, and agent-assisted page/image extraction

## Highlights

- Multiple feed sources with per-feed enable/disable.
- Auth modes: `none`, `query` (`?key=...`), `header` (`Authorization: Bearer ...`).
- Job-based routing (`feeds[] -> targets[]`).
- Scheduled polling via `interval_seconds` (implemented) and `cron` field (reserved, currently fallback).
- Startup-safe first poll delay: waits `45` seconds by default before the first poll after plugin startup.
- Deduplication + feed cursor persistence (ETag / Last-Modified / last_success_time).
- Admin commands: `/rss list`, `/rss status`, `/rss run [job_id]`, `/rss pause [job_id]`, `/rss resume [job_id]`.
- Optional LLM enrichment pipeline (safe fallback on failure).
- `text` / `image` rendering mode.

## Key Differences From `astrbot_plugin_rss`

- delivery orchestration instead of basic subscription polling only
- restart-safe dedup persistence
- panel-driven feed/target/job configuration
- startup-delay and retry-guard logic for unstable platform readiness
- a clearer path for future LLM and agent enrichment features

## Panel Configuration

The plugin ships `_conf_schema.json`, so all major settings can be edited from AstrBot plugin UI:

- `feeds[]`
- `targets[]`
- `jobs[]`
- `llm_*`, `dedup_ttl_seconds`, `startup_delay_seconds`, `render_mode`, `summary_max_chars`, `render_card_template`

See `README.md` for a full JSON example.

Notes:
- Dedup state is persisted to both AstrBot KV and `data/plugin_data/astrbot_rss/state.json`
- Items older than a feed's `last_success_time` are marked seen without being pushed again
- `startup_delay_seconds` defaults to `45` so platform adapters have time to become ready

## Roadmap

See [ROADMAP.md](./ROADMAP.md).
