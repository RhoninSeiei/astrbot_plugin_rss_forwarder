# 当前状态

## 版本

- 仓库版本：`0.6.1`
- 市场版本：`v0.6.1`
- 插件元数据名称：`astrbot_plugin_rss_forwarder`
- 运行时注册名：`astrbot_plugin_rss_forwarder`

## 当前能力

- 多 feed、多 target、多 job 的 RSS 推送编排。
- Twitter/Nitter 源 `feeds[].source_type=twitter`，支持按推主用户名采集时间线。
- Twitter 源可通过 `feeds[].send_images` 与 `feeds[].send_videos` 分别控制图片和视频发送。
- Twitter 源可通过 `feeds[].max_new_items` 控制每轮抓取的新推文数量，默认只取最新 1 条。
- 即时推送可通过 `display_source`、`display_time`、`display_link` 控制来源、时间、链接展示。
- 持久化去重、启动延迟、历史条目抑制、失效 target 抑制。
- 中文翻译增强，顺序为 `LLM -> Google Translate -> GitHub Models`。
- 日报任务 `daily_digests[]`，支持文本与图片两种发送形式。
- 日报图片模式使用插件自身的 `Star.html_render`，并在渲染失败时回退文本日报。
- 发送前指纹查重，包含文本身份信息与图片 `sha256`。
- 任务级去重记录保留时间 `jobs[].dedup_ttl_seconds`。
- 任务级语义重复判定 `jobs[].semantic_dedup_enabled`，支持独立选择模型、候选保留时间、候选数量上限与置信度阈值。
- RSS 源可通过 `feeds[].proxy_url` 单独配置抓取代理。
- 日报任务可通过 `daily_digests[].llm_timeout_seconds` 单独配置 LLM 等待时间。

## 线上活跃任务记录

### 1110001

- feeds：`101`、`104`、`105`
- 当前源：
  - `https://www.techpowerup.com/rss/news`
  - `https://videocardz.com/rss-feed`
  - `https://www.tomshardware.com/feeds.xml`
- 观察到的保留窗口约为 6 天多
- 当前任务级去重时效：`604800` 秒

### 2110002

- feeds：`102`、`103`
- 当前源：
  - `https://ngs.pso2-makapo.com/feed/`
  - `https://pso2roboarks.jp/ngs/feed`
- 观察到的保留窗口：
  - `102` 约 42 天
  - `103` 约 9 天多
- 当前任务级去重时效：`4320000` 秒

## 关键线上路径

- 宿主机：`wty1996@192.168.1.17`
- SSH 端口：`44012`
- 插件目录：`/volume1/docker/astrbot/data/plugins/astrbot_plugin_rss_forwarder`
- 配置文件：`/volume1/docker/astrbot/data/config/astrbot_plugin_rss_forwarder_config.json`
- 面板配置：`/volume1/docker/astrbot/data/cmd_config.json`
- 去重状态：`/volume1/docker/astrbot/data/plugin_data/astrbot_plugin_rss_forwarder/state.json`
- 旧状态兼容：首次使用新运行时注册名时，会从 `/volume1/docker/astrbot/data/plugin_data/astrbot_rss/state.json` 复制旧去重记录
- GitHub token：`/volume1/docker/astrbot/data/github.token`

## 运维约束

- 只允许重载单一插件。
- 只允许使用 AstrBot 仪表盘接口 `/api/plugin/reload`。
- 禁止重启容器。
- 禁止重载全部插件。
