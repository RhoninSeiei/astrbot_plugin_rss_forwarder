# AstrBot RSS Forwarder（日本語）

[中文](./README.md) | [English](./README.en.md)

AstrBot RSS Forwarder は、複数の RSS / RSSHub フィードを定期取得し、指定したチャット（グループ/チャンネル/DM）へ自動配信する AstrBot プラグインです。

## 主な機能

- 複数フィード対応（フィード単位で有効/無効）。
- 認証モード：`none` / `query` / `header`。
- ジョブ単位の配信ルーティング（複数 feed + 複数 target）。
- 定期実行：`interval_seconds` 実装済み、`cron` は将来拡張用（現状は interval フォールバック）。
- 起動直後の初回遅延：プラグイン起動後、既定で `45` 秒待ってから最初のポーリングを行います。
- 重複排除・ETag/Last-Modified 永続化。
- 管理コマンド：`/rss list` / `/rss status` / `/rss run` / `/rss pause` / `/rss resume`。
- LLM 拡張ポイント（失敗時は自動フォールバック）。

## 設定

`_conf_schema.json` により、AstrBot のプラグイン管理画面から主要項目を編集できます。

- `dedup_ttl_seconds`
- `startup_delay_seconds`
- `render_mode` / `summary_max_chars` / `render_card_template`

補足:
- 重複排除状態は AstrBot KV と `data/plugin_data/astrbot_rss/state.json` の両方に保存されます
- `last_success_time` より古い記事は再送せず、既読扱いのみ行います
- `startup_delay_seconds` の既定値は `45` 秒です
