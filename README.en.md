# AstrBot RSS Forwarder (English)

[中文](./README.md) | [日本語](./README.ja.md)

AstrBot RSS Forwarder is an AstrBot plugin that fetches updates from multiple RSS / RSSHub feeds and proactively pushes news to configured chat targets.

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
