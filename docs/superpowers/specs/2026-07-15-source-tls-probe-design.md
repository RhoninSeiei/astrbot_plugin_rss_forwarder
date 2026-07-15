# RSS 来源 TLS 配置与探测设计

日期：2026-07-15

## 背景

部分 RSS 或 Nitter 来源能够返回有效内容，但源站提供的 TLS 证书序列不完整，导致 Python、curl 等严格校验客户端拒绝访问。当前插件对 RSS 请求统一使用系统默认校验，配置界面只能设置来源地址、代理和超时，缺少按来源控制 TLS 校验以及诊断网络配置的能力。

本设计采用 AstrBot 官方 `_conf_schema.json`、Plugin Pages 和 `context.register_web_api()` 能力，不要求修改 AstrBot Core。Plugin Pages 自 AstrBot v4.24.1 起可用；更早版本仍可通过命令执行同一套探测逻辑。

## 目标

1. 每个 RSS 或 Twitter/Nitter 来源分别控制主来源请求是否校验 TLS 证书。
2. 通过 AstrBot Plugin Page 检查来源地址、代理和 TLS 校验组合。
3. 探测过程不更新 ETag、Last-Modified、已推送记录或任务状态，也不产生消息推送。
4. 探测结果给出可解释的配置建议，配置仍由管理员确认后保存。
5. 旧配置保持严格校验，现有订阅行为不发生隐式变化。

## 非目标

1. 不在普通轮询中遇到证书错误后自动关闭校验。
2. 不修改 AstrBot Dashboard 的通用 schema 渲染器。
3. 不替源站修补或续签证书。
4. 不把关闭校验扩展到正文、图片、视频等第三方资源，只影响该来源的主请求。

## 配置模型

在 RSS 与 Twitter/Nitter 两类来源模板中增加：

```json
"verify_ssl": {
  "type": "bool",
  "default": true,
  "description": "校验来源 TLS 证书",
  "hint": "建议保持开启。仅在来源内容可访问但证书序列异常时关闭。"
}
```

`FeedConfig` 增加 `verify_ssl: bool = True`。配置迁移依赖 AstrBot schema 默认值和 `RSSConfig.from_context()` 的兼容读取；缺少字段时始终使用 `True`。

运行时不会根据异常自动改变该值。关闭校验后，插件在首次请求及插件重载后记录一次带来源 ID 的警告，避免管理员遗漏安全状态。

## 组件设计

### SourceProbeService

新增独立服务，负责构造探测请求、限制响应大小、分类错误并生成建议。轮询抓取器与探测服务共享请求参数构造辅助函数，避免 User-Agent、鉴权、代理和 TLS 行为出现差异。

服务输入支持两种形式：

1. `feed_id`：使用已保存来源的完整配置。
2. 草稿参数：使用 Page 中填写的 URL、代理、超时和鉴权模式，便于保存前检查。

草稿中的鉴权值只发送到插件后端，不回显到结果。日志中的 URL 查询参数和凭据继续脱敏。

### 探测矩阵

探测按顺序执行以下组合：

| 模式 | 网络方式 | TLS 校验 |
| --- | --- | --- |
| direct_strict | 默认网络 | 开启 |
| proxy_strict | 来源代理 | 开启 |
| direct_relaxed | 默认网络 | 关闭 |
| proxy_relaxed | 来源代理 | 关闭 |

来源未配置代理时跳过两个代理模式。严格模式成功后仍可跳过对应宽松模式，以减少请求次数；需要完整比较时由 Page 提供“完整检查”选项。

每个结果包含：

```json
{
  "mode": "direct_strict",
  "ok": false,
  "http_status": null,
  "content_type": "",
  "latency_ms": 182,
  "is_feed": false,
  "error_type": "tls_certificate",
  "error_message": "unable to get local issuer certificate"
}
```

错误类型至少区分 `dns`、`connect`、`timeout`、`tls_certificate`、`proxy`、`http_status`、`invalid_feed` 和 `unknown`。

### 配置建议

建议规则保持确定性，不调用 LLM：

| 检查结果 | 建议 |
| --- | --- |
| 默认网络严格模式成功 | 保持 `verify_ssl=true`，代理留空 |
| 仅代理严格模式成功 | 保持 `verify_ssl=true`，使用当前代理 |
| 严格模式失败且宽松模式成功 | 可为该来源设置 `verify_ssl=false`，同时显示证书安全提示 |
| 所有模式失败 | 不推荐配置变更，显示最接近根因的错误 |
| HTTP 成功但内容不是 RSS 或 Atom | 检查来源地址，TLS 设置不变 |

探测结果只给出建议值，不保存配置。

## Plugin Page

页面目录：

```text
pages/source-diagnostics/
├── index.html
├── app.js
└── style.css
```

页面通过 `window.AstrBotPluginPage` bridge 获取主题、语言和插件身份，并调用插件内相对 API。页面包含来源选择、地址、代理、超时、TLS 当前值、可选鉴权模式与临时凭据、检查按钮和结果表。选择已保存来源后加载脱敏后的展示值；后端仍按 `feed_id` 使用完整配置。草稿模式中的临时凭据只参与本次请求，页面在请求完成后清空该输入。

结果区域显示各模式状态、耗时、HTTP 状态、内容识别结果、错误原因和建议。宽松模式成功时使用警告样式，并明确显示“仅影响此来源”。页面遵循 AstrBot 明暗主题，不访问父页面 DOM、LocalStorage 或 Dashboard 凭据。

`_conf_schema.json` 的来源配置提示中标明入口位置：“插件详情 → 来源诊断”。Plugin Page 由 AstrBot 插件详情页打开，不依赖额外端口。

## 插件 API

注册以下插件 API：

```text
GET  /astrbot_plugin_rss_forwarder/source-probe/feeds
POST /astrbot_plugin_rss_forwarder/source-probe/run
```

Page 通过 bridge 分别调用 `source-probe/feeds` 和 `source-probe/run`。AstrBot 负责插件命名空间、Dashboard 身份和请求转发。

`feeds` 只返回来源 ID、类型、脱敏地址、代理是否配置、超时和当前 TLS 开关。`run` 校验请求结构，限制 URL 为 `http` 或 `https`，代理只接受插件已有的受支持协议。

## 命令兼容

新增命令：

```text
/rss probe <feed_id>
```

命令调用同一个 `SourceProbeService`，输出压缩后的模式状态和配置建议。该入口服务于 AstrBot v4.24.1 之前的环境、移动端管理以及 Page 无法打开时的诊断。

## 请求限制与安全要求

1. 单模式超时沿用来源超时，限制在 3 至 30 秒。
2. 最多读取 256 KiB 响应内容，仅用于识别 RSS 或 Atom 根元素。
3. 最多跟随 5 次重定向。
4. 同一 Dashboard 用户同一时间最多执行一个来源探测。
5. 宽松模式只在显式探测或来源配置为 `verify_ssl=false` 时使用。
6. API 响应和日志不包含鉴权 key、Authorization 头、Cookie 或完整查询参数。
7. 探测请求不会写入持久化缓存和任务执行记录。

允许访问内网来源，因为 RSSHub、自建 Nitter 和局域网服务属于插件既有使用场景。访问权限与 Dashboard 管理员权限保持一致。

## 兼容性

1. 旧配置缺少 `verify_ssl` 时采用 `True`。
2. AstrBot v4.24.1 及以上显示 Plugin Page。
3. 较早版本忽略 `pages/`，核心抓取与 `/rss probe` 仍可使用。
4. 使用系统默认证书校验的现有来源行为保持不变。
5. HTTP 来源忽略 TLS 开关，并在 Page 中标记为“不适用”。

## 测试

### 配置测试

验证新旧配置的默认值、RSS 与 Twitter/Nitter 模板字段以及 schema JSON 解析。

### 请求测试

使用受控 opener 或本地测试服务器验证严格校验、宽松校验、HTTP 代理、SOCKS 代理、超时、重定向和响应大小限制。

### 探测测试

验证错误分类、探测模式跳过规则、建议规则、缓存无写入以及日志脱敏。

### API 与 Page 测试

验证 API 输入校验、来源列表脱敏、bridge 调用端点和 Page 的加载、明暗主题、按钮禁用状态及长错误文本布局。生产同步后通过目标插件热重载，并在 Dashboard 中完成一次严格成功来源和一次证书序列异常来源的检查。

## 发布范围

本功能属于用户可见更新，应记录在 `CHANGELOG.md`。README 增加 Plugin Page 入口、`verify_ssl` 安全提示和 `/rss probe` 命令用法；版本号在实施阶段按发布安排确定。
