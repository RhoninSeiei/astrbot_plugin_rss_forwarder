# 关键决策

## 去重键策略

### 内容去重

内容去重使用 `storage.py` 里的 `content_seen:v{version}:{item_id}`。`item_id` 由 `guid/id`、规范化 `link`、条目哈希等信息组合生成。

### 发送前保护

发送层另有一层发送指纹保护，优先使用原文身份信息，并在有图片时加入图片 `sha256`。这一层主要用于并发场景下避免同一消息重复发给同一会话。

## 重复推送问题处理过程

### 同批次重复 item

在调度器执行批次内检查，避免同一轮抓取里同一条内容重复进入处理。

### `guid` 变化引起的重复

增加基于规范化 `link` 的第二层内容键，降低源站 `guid` 漂移带来的重复推送。

### 热重载后旧任务残留

调度器启动时会清理残留 `rss-job-*` 任务，减少旧任务与新任务并存。

### 发送阶段重复

发送前增加原文身份指纹与图片哈希保护，避免并发翻译后文本略有差异时重复发出。

## 草稿配置保存

新建 `feed`、`target`、`job`、`daily_digest` 默认关闭。配置校验只针对启用项执行，便于先保存草稿，再补齐字段并启用。

## 任务级去重时效

### 设计目的

不同 RSS 源的保留窗口差异较大，全局固定时效容易出现两类问题：

- 短窗口源保留太久，状态文件增长较快。
- 长窗口源保留太短，旧条目在源中仍可见时会重新进入发送判断。

### 当前选择

- 全局 `dedup_ttl_seconds` 继续保留，作为默认值。
- `jobs[].dedup_ttl_seconds` 大于 `0` 时覆盖当前任务。
- `jobs[].dedup_ttl_seconds` 等于 `0` 时继续继承全局值。

### 当前线上建议

- `1110001`：`604800` 秒。
- `2110002`：`4320000` 秒。

## Twitter/Nitter 源整合

### 当前范围

- 只整合定时采集、普通推送、去重、翻译与日报归档。
- 暂缓迁移 Twitter 链接识别。
- 暂缓迁移合并转发消息。

### 设计选择

- Twitter 推主作为 `feeds[]` 的一种源类型，通过 `source_type=twitter` 表示。
- Nitter 解析结果转换为统一 item，继续复用现有 job、target、pipeline、storage 与 dispatcher。
- 推文主去重键使用 `twitter:{username}:{tweet_id}`，同时保留规范化链接指纹。
- Twitter 首次启用时记录最新 `since_id`，后续轮询只发送新增推文。
- 图片和视频发送由 `feeds[].send_images`、`feeds[].send_videos` 分别控制。
- 原推文链接发送由 `feeds[].send_link` 单独控制；普通 RSS 链接显示由全局 `display_link` 控制。
- 每轮 Twitter 抓取数量由 `feeds[].max_new_items` 控制，默认值 `1` 用于减少详情页请求并抑制积压推文刷屏。
- 来源、时间、链接显示由 `display_source`、`display_time`、`display_link` 控制，文本推送与图片图卡共用同一组开关。
- Twitter 媒体优先缓存到 `data/plugin_data/astrbot_plugin_rss_forwarder/twitter_media` 后以本地文件发送，降低代理环境下图片和视频发送失败的概率。

### 原因

RSS 插件已有日报聚合能力，合并转发与日报场景重叠。实时推送使用普通消息链承载文字、图片和视频，结构更简单，也减少视频放入合并转发节点导致的超时问题。

## 运维原则

- 线上只允许单插件热重载。
- 单插件热重载固定通过 `/api/plugin/reload` 执行。
- 禁止容器重启。
- 禁止全部插件重载。
