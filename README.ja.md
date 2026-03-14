# astrbot_plugin_rss_forwarder（日本語）

[中文](./README.md) | [English](./README.en.md)

`astrbot_plugin_rss_forwarder` は、AstrBot 向けの RSS / RSSHub 配信オーケストレーション用プラグインです。複数フィードを取得し、管理画面で構成したルールに従って指定チャットへ配信します。

## 位置付け

このプロジェクトは [`Soulter/astrbot_plugin_rss`](https://github.com/Soulter/astrbot_plugin_rss) の単純な代替ではありません。現在の主な方向性は次の通りです。

- 再起動後も維持される重複排除
- feed / target / job / 配信方式を管理画面から視覚的に設定
- 起動直後の不安定な環境に配慮した初回遅延と無効 target 抑制
- 将来的な翻訳、要約、Agent によるページ・画像取得の拡張

## 主な機能

- 複数フィード対応（フィード単位で有効/無効）。
- 認証モード：`none` / `query` / `header`。
- ジョブ単位の配信ルーティング（複数 feed + 複数 target）。
- 定期実行：`interval_seconds` 実装済み、`cron` は将来拡張用（現状は interval フォールバック）。
- 起動直後の初回遅延：プラグイン起動後、既定で `45` 秒待ってから最初のポーリングを行います。
- 重複排除・ETag/Last-Modified 永続化。
- 管理コマンド：`/rss list` / `/rss status` / `/rss run` / `/rss pause` / `/rss resume`。
- LLM 拡張ポイント（失敗時は自動フォールバック）。

## `astrbot_plugin_rss` との主な違い

- 単なる購読取得ではなく、配信オーケストレーションを重視
- 再起動に強い重複排除の永続化
- 管理画面からの feed / target / job 設定
- プラットフォーム未準備時の誤送信や誤再試行を抑える保護
- 将来の LLM / Agent 拡張に向けた構造

## 設定

`_conf_schema.json` により、AstrBot のプラグイン管理画面から主要項目を編集できます。

- `dedup_ttl_seconds`
- `startup_delay_seconds`
- `render_mode` / `summary_max_chars` / `render_card_template`

補足:
- 重複排除状態は AstrBot KV と `data/plugin_data/astrbot_rss/state.json` の両方に保存されます
- `last_success_time` より古い記事は再送せず、既読扱いのみ行います
- `startup_delay_seconds` の既定値は `45` 秒です

## ロードマップ

[ROADMAP.md](./ROADMAP.md) を参照してください。
