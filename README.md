# astrbot_plugin_rss_forwarder（中文）

[English](./README.en.md) | [日本語](./README.ja.md)

`astrbot_plugin_rss_forwarder` 是一个面向 AstrBot 的 RSS / RSSHub 推送编排插件，用于从多个订阅源拉取内容，并将结果按可视化配置的路由规则主动推送到指定平台会话（群/频道/私聊）。

## 定位

本项目不是对 `https://github.com/Soulter/astrbot_plugin_rss` 的简单重复实现，当前定位更偏向“RSS 推送编排”：

- 支持 RSS 去重持久化，避免重启后历史内容全量重推。
- 支持在插件面板中图形化定义 feed、target、job 和推送方式。
- 支持首轮启动延迟、历史条目抑制、无效 target 抑制等实际部署问题修复。
- 后续将扩展自动翻译、自动总结、Agent 辅助网页读取与图片提取等能力。

## 功能

- 支持多 RSS 源（每个源可单独启用/禁用）。
- 支持鉴权：
  - `none`：公开链接；
  - `query`：在 URL 上自动附加 `key`；
  - `header`：通过 `Authorization: Bearer <key>` 发送。
- 支持任务级路由：一个 Job 绑定多个 feed + 多个 target。
- 支持定时执行：`interval_seconds`（已实现）与 `cron`（预留字段，当前回退到 interval）。
- 支持启动首轮延迟：默认在插件启动后等待 `45` 秒再执行第一次轮询，避免平台适配器尚未就绪时抢跑。
- 支持去重（KV + TTL）与 feed 状态（ETag/Last-Modified/last_success_time）。
- 支持管理指令：`/rss list`、`/rss status`、`/rss run [job_id]`、`/rss pause [job_id]`、`/rss resume [job_id]`、`/rss reset`（清空去重记录）。
- 预留 LLM 处理管线（摘要/翻译增强，失败自动降级）。
- 支持 text / image 两种渲染模式（image 使用 `html_render`）。

## 与 `astrbot_plugin_rss` 的主要区别

- 更强调“推送编排”而不是基础订阅。
- 已实现去重持久化与重启恢复。
- 已实现可视化 feed/target/job 配置。
- 已实现启动阶段的稳态保护，减少平台未就绪时的误推送和误重试。
- 为后续 LLM/Agent 增强保留了清晰的处理管线。

## 配置（插件面板）

本插件使用 `_conf_schema.json`，可在 AstrBot 插件面板中直接可视化配置：

- `feeds[]`
  - `id`（唯一）
  - `url`
  - `auth_mode`：`none|query|header`
  - `key`
  - `enabled`
  - `timeout`
- `targets[]`
  - `id`（唯一）
  - `platform`
  - `unified_msg_origin`（建议优先）
  - `enabled`
- `jobs[]`
  - `id`（唯一）
  - `feed_ids[]`
  - `target_ids[]`
  - `interval_seconds`（推荐）
  - `cron`（可填，当前版本回退到 interval）
  - `batch_size`
  - `enabled`
- 处理渲染
  - `llm_enabled`
  - `llm_profile`
  - `max_input_chars`
  - `timeout`
  - `dedup_ttl_seconds`
  - `startup_delay_seconds`
  - `render_mode`（`text|image`）
  - `summary_max_chars`
  - `render_card_template`

说明：
- 去重记录会同时写入 AstrBot KV 与 `data/plugin_data/astrbot_rss/state.json`
- 若条目发布时间早于该 feed 的 `last_success_time`，插件会仅补记去重而不重复推送
- `startup_delay_seconds` 默认为 `45`，用于给平台适配器和主动消息通道预留启动时间

## 示例配置

```json
{
  "feeds": [
    {
      "id": "rsshub_it",
      "url": "https://rsshub.example.com/36kr/newsflash",
      "auth_mode": "query",
      "key": "YOUR_RSSHUB_KEY",
      "enabled": true,
      "timeout": 10
    }
  ],
  "targets": [
    {
      "id": "tg_group_a",
      "platform": "telegram",
      "unified_msg_origin": "telegram:group:xxxx",
      "enabled": true
    }
  ],
  "jobs": [
    {
      "id": "it_news",
      "feed_ids": ["rsshub_it"],
      "target_ids": ["tg_group_a"],
      "interval_seconds": 300,
      "batch_size": 10,
      "enabled": true
    }
  ],
  "llm_enabled": false,
  "render_mode": "text"
}
```


## 安装与环境依赖说明

### 1) 已修复的面板安装报错

若你遇到 `ModuleNotFoundError: No module named 'commands'`，这是由于旧版本插件使用了顶层导入方式（`from commands import ...`）导致的。

本仓库已修复为包内相对导入（`from .commands import ...`），可被 AstrBot 面板按 `astrbot_rss.main` 正确加载。

### 2) 依赖对比（相对 AstrBot 默认环境）

本插件核心功能仅依赖：
- AstrBot 运行时（由 AstrBot 主程序提供）
- Python 标准库（`asyncio`、`urllib`、`xml`、`json` 等）

**结论：本插件没有必须额外 `pip install` 的第三方 Python 依赖。**

### 3) 可选能力说明

- `render_mode = image` 时，依赖 AstrBot 侧提供的 `html_render` 能力。
- `llm_enabled = true` 时，依赖 AstrBot 已配置可用的大模型提供商。

若上述 AstrBot 能力未配置，本插件会记录日志并自动降级，不影响基础 RSS 文本推送。

## 开发参考

- Getting Started: https://docs.astrbot.app/dev/star/plugin-new.html
- Guides:
  - simple / listen-message-event / send-message / plugin-config
  - ai / storage / html-to-pic / session-control / other

## 路线图

见 [ROADMAP.md](./ROADMAP.md)。

## 已知限制

- 当前未实现真正的 cron 调度器（配置 `cron` 时会回退到最小 interval 轮询）。
- 主动消息依赖平台能力，若平台不支持会记录错误日志。
