# 项目结构

## 核心文件

### `main.py`

插件入口。负责注册插件、构造配置、存储、抓取、解析、处理管线、分发器与调度器。

### `config.py`

配置模型与校验逻辑。`feeds[]`、`targets[]`、`jobs[]`、`daily_digests[]`、翻译配置、全局去重参数都在此定义。

### `scheduler.py`

调度核心。负责：

- 启动与停止轮询任务。
- 执行 job 抓取、解析、归档、分发。
- 执行日报抓取与日报发送。
- 处理历史条目抑制、批次内去重、任务级去重时效选择。

### `storage.py`

持久化层。负责：

- AstrBot KV 与本地 `state.json` 的统一封装。
- feed 状态记录。
- 内容去重键与发送保护键。
- 日报归档与日报状态。

### `dispatcher.py`

消息发送层。负责：

- 文本与图片渲染。
- 发送前查重。
- 多 target 分发。
- 图片摘要指纹与发送结果统计。

### `pipeline.py`

内容处理层。负责：

- 标题与摘要清洗。
- LLM、Google、GitHub Models 翻译与日报总结。
- 回退策略与诊断结果生成。

### `fetcher.py`

HTTP 抓取。负责 feed 请求、鉴权方式、ETag 与 Last-Modified 相关元数据处理。

### `parser.py`

RSS 解析。负责将源数据转换为统一 item 结构，并保留 `guid`、`link`、发布时间、图片等字段。

### `commands.py`

命令入口，当前包括：

- `/rss list`
- `/rss status`
- `/rss run [job_id]`
- `/rss pause [job_id]`
- `/rss resume [job_id]`
- `/rss reset`
- `/rss test [sample text]`
- `/rss digest run [digest_id]`

## 运行流程

### 即时推送

1. `scheduler.py` 按 job 拉取 feed。
2. `parser.py` 解析为统一 item。
3. `storage.py` 归档日报素材。
4. `scheduler.py` 检查批次内去重与持久化去重。
5. `pipeline.py` 做翻译、摘要与正文清洗。
6. `dispatcher.py` 做发送前查重与消息发送。
7. `storage.py` 写入去重键与 feed 状态。

### 日报

1. 即时任务或日报专用抓取任务先把 item 写入日报归档。
2. `scheduler.py` 在指定时间读取窗口内条目。
3. `pipeline.py` 生成日报正文。
4. `dispatcher.py` 发送文本或图片日报。
5. `storage.py` 记录日报发送状态。

## 命名差异

- 发布与安装层名称：`astrbot_plugin_rss_forwarder`
- 运行时注册名与本地状态命名空间：`astrbot_rss`

当前两者并存，维护时需要同时留意。
