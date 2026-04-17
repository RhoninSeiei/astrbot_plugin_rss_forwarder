# 当前状态

## 版本

- 仓库版本：`0.4.2`
- 市场版本：`v0.4.2`
- 插件元数据名称：`astrbot_plugin_rss_forwarder`
- 运行时注册名：`astrbot_rss`

## 当前能力

- 多 feed、多 target、多 job 的 RSS 推送编排。
- 持久化去重、启动延迟、历史条目抑制、失效 target 抑制。
- 中文翻译增强，顺序为 `LLM -> Google Translate -> GitHub Models`。
- 日报任务 `daily_digests[]`，支持文本与图片两种发送形式。
- 发送前指纹查重，包含文本身份信息与图片 `sha256`。
- 任务级去重记录保留时间 `jobs[].dedup_ttl_seconds`。

## 线上活跃任务记录

### 1110001

- feed：`101`
- 当前源：`https://www.techpowerup.com/rss/news`
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
- 去重状态：`/volume1/docker/astrbot/data/plugin_data/astrbot_rss/state.json`
- GitHub token：`/volume1/docker/astrbot/data/github.token`

## 运维约束

- 只允许重载单一插件。
- 只允许使用 AstrBot 仪表盘接口 `/api/plugin/reload`。
- 禁止重启容器。
- 禁止重载全部插件。
