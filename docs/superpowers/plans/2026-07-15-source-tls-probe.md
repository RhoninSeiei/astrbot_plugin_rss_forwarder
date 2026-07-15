# Source TLS Configuration And Probe Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为每个 RSS 与 Twitter/Nitter 来源增加独立 TLS 证书校验开关，并通过 AstrBot 官方 Plugin Page 和 `/rss probe` 命令检查直连、来源代理及严格、宽松 TLS 组合。

**Architecture:** 把 RSS 与 Nitter 主来源请求提取到无状态的共享网络模块，轮询抓取和探测服务使用同一套 URL、请求头、鉴权、代理与 TLS 参数。探测服务只读取有限响应并返回结构化报告，不接触缓存和发送状态。Plugin Page 通过 `window.AstrBotPluginPage` bridge 调用插件 Web API；旧版 AstrBot 仍可使用相同探测服务支持的命令入口。

**Tech Stack:** Python 3.10+、`asyncio`、`urllib.request`、`httpx[socks]`、AstrBot `_conf_schema.json`、`context.register_web_api()`、`astrbot.api.web`、原生 HTML/CSS/JavaScript、标准库 `unittest`。

## Global Constraints

1. 旧来源缺少 `verify_ssl` 时必须采用 `True`，普通轮询遇到证书错误时不得自动关闭校验。
2. 来源级 TLS 开关只控制 RSS XML、Nitter 时间线和 Nitter 推文详情请求；正文、图片、视频和其他媒体下载继续使用严格证书校验。
3. 探测不得更新 ETag、Last-Modified、`since_id`、已发送条目、语义判重记录、任务时间或任何持久化数据。
4. 探测响应与日志不得包含 Authorization、Cookie、鉴权 key 或完整查询参数。
5. 单次模式读取上限为 256 KiB，重定向上限为 5，超时限制为 3 至 30 秒；同一 Dashboard 用户同时只允许一个探测请求。
6. 页面只能使用 Plugin Page bridge，不访问 Dashboard Cookie、LocalStorage、父页面 DOM 或额外端口。
7. 生产更新只同步此插件，随后调用目标插件重载接口；禁止重启 `astrbot` 容器。
8. 当前未提交的 `fetcher.py` 与 `tests/test_fetcher.py` 日志增强必须先形成独立提交，后续提交不得丢失或混入无关文件。
9. 本计划文档在 Task 0 开始前形成独立提交，因此 Task 0 提交日志补丁后，工作目录应当为空。
10. AstrBot v4.24.1 以上注册官方 Page API；缺少 `astrbot.api.web` 的较早版本跳过 Page API，轮询和 `/rss probe` 继续初始化。

---

## Task 0: Preserve The Existing Fetch Failure Logging Patch

**Files:**

- Modify: `fetcher.py`
- Modify: `tests/test_fetcher.py`

- [ ] **Step 1: Review the existing diff and confirm its scope**

Run:

```powershell
git diff -- fetcher.py tests/test_fetcher.py
```

Expected: only job ID propagation, redacted source URL, proxy state, client name, and the matching log assertion appear.

- [ ] **Step 2: Re-run the focused regression tests**

Run:

```powershell
python -m unittest tests.test_fetcher tests.test_scheduler -v
```

Expected: all tests finish with `OK`.

- [ ] **Step 3: Check whitespace and syntax**

Run:

```powershell
python -m py_compile fetcher.py tests/test_fetcher.py
git diff --check -- fetcher.py tests/test_fetcher.py
```

Expected: both commands exit with status 0 and print no errors.

- [ ] **Step 4: Commit only the logging patch**

```powershell
git add fetcher.py tests/test_fetcher.py
git commit -m "fix: identify RSS fetch failures"
```

Expected: `git status --short` becomes empty before feature development begins.

---

## Task 1: Add Per-Source TLS Configuration

**Files:**

- Modify: `config.py`
- Modify: `_conf_schema.json`
- Modify: `tests/test_config_translation.py`

- [ ] **Step 1: Add failing configuration tests**

Extend `tests/test_config_translation.py` with these cases:

```python
def test_feed_verify_ssl_defaults_to_true_for_legacy_config(self):
    conf = _minimal_runtime_conf()
    config = RSSConfig.from_context(conf)
    self.assertTrue(config.feeds[0].verify_ssl)

def test_feed_verify_ssl_parses_false_for_rss_and_twitter(self):
    conf = _minimal_runtime_conf()
    conf["feeds"] = [
        {
            "id": "rss-insecure",
            "url": "https://example.com/feed.xml",
            "verify_ssl": False,
        },
        {
            "id": "twitter-insecure",
            "source_type": "twitter",
            "username": "example",
            "nitter_url": "https://nitter.example.com",
            "verify_ssl": False,
        },
    ]
    conf["jobs"][0]["feed_ids"] = ["rss-insecure", "twitter-insecure"]
    config = RSSConfig.from_context(conf)
    self.assertEqual(
        [feed.verify_ssl for feed in config.feeds],
        [False, False],
    )

def test_source_templates_expose_verify_ssl(self):
    schema = json.loads(Path("_conf_schema.json").read_text(encoding="utf-8"))
    templates = schema["feeds"]["templates"]
    self.assertTrue(templates["rss_feed"]["items"]["verify_ssl"]["default"])
    self.assertTrue(templates["twitter_feed"]["items"]["verify_ssl"]["default"])
```

Use the file's existing `_minimal_runtime_conf()`, `RSSConfig.from_context()` and schema traversal style rather than introducing a second loader.

- [ ] **Step 2: Confirm the tests fail for the missing field**

Run:

```powershell
python -m unittest tests.test_config_translation -v
```

Expected: new tests fail because `FeedConfig.verify_ssl` and schema entries do not exist.

- [ ] **Step 3: Add the runtime field and compatible parser**

Add this field after `timeout` in `FeedConfig`:

```python
verify_ssl: bool = True
```

In both source-template parsing branches, construct the field with:

```python
verify_ssl=bool(item.get("verify_ssl", True)),
```

Do not coerce string values such as `"false"`; AstrBot schema supplies a JSON boolean and existing programmatic config callers must also use a boolean. Add a validation error when the raw value exists and is not `bool`, following the surrounding validation style.

- [ ] **Step 4: Add schema controls and the Page entry hint**

Add the same field to `rss_feed.items` and `twitter_feed.items`:

```json
"verify_ssl": {
  "type": "bool",
  "default": true,
  "description": "校验来源 TLS 证书",
  "hint": "建议保持开启。仅在来源内容可访问但证书序列异常时关闭。"
}
```

Extend each source section hint with `插件详情 -> 来源诊断` while retaining existing configuration guidance.

- [ ] **Step 5: Run configuration tests and parse the schema**

Run:

```powershell
python -m unittest tests.test_config_translation -v
python -m json.tool _conf_schema.json
```

Expected: tests finish with `OK`; the JSON command emits formatted JSON and exits with status 0.

- [ ] **Step 6: Commit the configuration contract**

```powershell
git add config.py _conf_schema.json tests/test_config_translation.py
git commit -m "feat: configure TLS verification per source"
```

---

## Task 2: Build The Shared Source HTTP Transport

**Files:**

- Create: `source_http.py`
- Create: `tests/test_source_http.py`
- Modify: `requirements.txt`

- [ ] **Step 1: Write transport tests before implementation**

Create `tests/test_source_http.py` with fake urllib openers and a stub `httpx` module. Cover:

1. Strict HTTPS creates an `HTTPSHandler` with a context whose `verify_mode` is `ssl.CERT_REQUIRED`.
2. Relaxed HTTPS creates a context with `check_hostname is False` and `verify_mode is ssl.CERT_NONE`.
3. An explicit HTTP proxy is installed for both `http` and `https` schemes.
4. Probe direct mode installs `ProxyHandler({})`, preventing environment proxy inheritance.
5. SOCKS proxy dispatches through `httpx.Client` with `verify`, `proxy`, `follow_redirects=True`, and `max_redirects`.
6. `max_bytes=256 * 1024` reads at most one extra byte, returns only the limit, and sets `truncated=True`.
7. Unlimited runtime reads return the entire body and `truncated=False`.
8. More than five urllib redirects raises `TooManyRedirects` without exposing the redirected query string.
9. `requirements.txt` declares `httpx[socks]>=0.27,<1` so a fresh plugin installation receives both httpx and socksio support.

The public response type and request function must be asserted explicitly:

```python
@dataclass(slots=True)
class SourceHttpResponse:
    body: bytes
    status: int
    headers: dict[str, str]
    final_url: str
    truncated: bool = False

def request_source(
    *,
    url: str,
    headers: dict[str, str],
    proxy_url: str,
    timeout: int,
    verify_ssl: bool,
    max_bytes: int | None = None,
    max_redirects: int | None = None,
    use_environment_proxy: bool = True,
) -> SourceHttpResponse:
    ...
```

- [ ] **Step 2: Confirm the module is missing**

Run:

```powershell
python -m unittest tests.test_source_http -v
```

Expected: import failure for `source_http`.

- [ ] **Step 3: Implement SSL context construction and urllib handling**

Create `source_http.py` with:

```python
def build_ssl_context(verify_ssl: bool) -> ssl.SSLContext:
    context = ssl.create_default_context()
    if not verify_ssl:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return context
```

Build an opener containing `HTTPSHandler(context=context)`. Apply these proxy rules:

```python
if proxy_url:
    proxy_handler = ProxyHandler({"http": proxy_url, "https": proxy_url})
elif use_environment_proxy:
    proxy_handler = ProxyHandler()
else:
    proxy_handler = ProxyHandler({})
```

Implement a small `HTTPRedirectHandler` subclass that counts redirects per request and raises a dedicated `TooManyRedirects` when a finite `max_redirects` is supplied. When the value is `None`, retain the underlying client's existing redirect behavior so normal polling keeps its current compatibility. Store no cookies and no global mutable request state.

- [ ] **Step 4: Implement SOCKS handling through httpx**

Use `httpx` only when the explicit proxy begins with `socks://`, `socks4://`, `socks5://`, or `socks5h://`. Pass these common values, and add `max_redirects` only when the caller supplies a finite value:

```python
{
    "headers": headers,
    "timeout": timeout,
    "follow_redirects": True,
    "verify": verify_ssl,
    "proxy": proxy_url,
}
```

Normalize urllib and httpx headers to `dict[str, str]`. Preserve status and final URL. The function must raise transport and HTTP exceptions for the caller to classify.

- [ ] **Step 5: Declare and install the SOCKS transport dependency**

Add this line to `requirements.txt` while retaining `Pillow`:

```text
httpx[socks]>=0.27,<1
```

Run:

```powershell
python -m pip install -r requirements.txt
python -c "import httpx, socksio; print(httpx.__version__)"
```

Expected: dependency installation succeeds and the import command prints the installed httpx version. Production currently provides httpx 0.28.1 and socksio 1.0.0; the declaration makes that requirement explicit for fresh installations.

- [ ] **Step 6: Implement bounded response reading**

For a finite limit, read `max_bytes + 1`, return the first `max_bytes`, and set `truncated` from the extra byte. Reject zero and negative limits with `ValueError`. Runtime callers pass `None` and keep full-feed behavior.

- [ ] **Step 7: Run transport tests**

Run:

```powershell
python -m unittest tests.test_source_http -v
```

Expected: all transport tests finish with `OK`.

- [ ] **Step 8: Commit the isolated transport**

```powershell
git add source_http.py tests/test_source_http.py requirements.txt
git commit -m "feat: add source HTTP transport"
```

---

## Task 3: Move RSS And Nitter Primary Requests To The Shared Transport

**Files:**

- Modify: `fetcher.py`
- Modify: `twitter_source.py`
- Modify: `tests/test_fetcher.py`
- Modify: `tests/test_twitter_source.py`

- [ ] **Step 1: Add failing RSS integration tests**

Extend `tests/test_fetcher.py` to verify:

1. `_fetch_single_feed()` passes `feed.verify_ssl`, the configured proxy and conditional headers to `request_source()`.
2. A legacy feed object without the attribute passes `True`.
3. A feed with `verify_ssl=False` logs one warning containing only the feed ID, even across repeated fetches.
4. The warning contains no source query or key.
5. ETag, Last-Modified, status and decoded XML still populate `FetchedFeed` exactly as before.

Patch the imported `request_source` callable instead of exercising external networking.

- [ ] **Step 2: Add failing Nitter integration tests**

Update `tests/test_twitter_source.py` fakes from three arguments to four and add assertions that:

```python
_open_text(url, proxy_url, timeout, verify_ssl)
```

receives the source-level value for timeline and detail requests. Add a media-cache test showing that `_download_media_file()` does not receive or inherit the source-level relaxed setting.

- [ ] **Step 3: Confirm the new tests fail**

Run:

```powershell
python -m unittest tests.test_fetcher tests.test_twitter_source -v
```

Expected: failures identify the missing `verify_ssl` propagation and old request helpers.

- [ ] **Step 4: Refactor RSS fetching**

Move generic URL opening into `request_source()`. Keep `_build_url_and_headers()` in `fetcher.py` because it defines RSS query and bearer authentication. Call the shared transport from the existing `asyncio.to_thread()` block:

```python
response = request_source(
    url=url,
    headers=headers,
    proxy_url=proxy_url,
    timeout=feed.timeout,
    verify_ssl=bool(getattr(feed, "verify_ssl", True)),
    max_bytes=None,
    max_redirects=None,
)
```

Convert `response.body` with UTF-8 `errors="ignore"`, and retain current 304 handling and enhanced failure log fields. Remove duplicated urllib, httpx and SOCKS-selection code from `fetcher.py` after parity tests pass.

- [ ] **Step 5: Refactor Nitter timeline and detail fetching**

Define one Nitter request-header constant and implement `_open_text()` through `request_source()`. Pass `verify_ssl` to timeline and detail calls. Keep media request methods unchanged so source-level relaxed TLS never reaches media downloads.

- [ ] **Step 6: Add the once-per-load relaxed TLS warning**

In `FeedFetcher.__init__`, initialize:

```python
self._relaxed_tls_warned_feed_ids: set[str] = set()
```

Before dispatching either RSS or Twitter fetching, call a helper that logs once:

```text
feed=<id> source TLS certificate verification is disabled
```

Do not log the URL, username, proxy or credentials in this warning.

- [ ] **Step 7: Run RSS and Twitter tests**

Run:

```powershell
python -m unittest tests.test_fetcher tests.test_twitter_source -v
```

Expected: all tests finish with `OK`.

- [ ] **Step 8: Check for stale duplicate transport code**

Run:

```powershell
rg -n "build_opener|ProxyHandler|httpx.Client|CERT_NONE" fetcher.py twitter_source.py source_http.py
```

Expected: primary-source transport construction appears only in `source_http.py`; media-download code may retain its strict downloader.

- [ ] **Step 9: Commit runtime integration**

```powershell
git add fetcher.py twitter_source.py tests/test_fetcher.py tests/test_twitter_source.py
git commit -m "feat: apply source TLS settings to fetches"
```

---

## Task 4: Implement Deterministic Source Probing

**Files:**

- Create: `source_probe.py`
- Create: `tests/test_source_probe.py`

- [ ] **Step 1: Define failing report and mode tests**

Create `tests/test_source_probe.py`. Use an injected request callable and monotonic clock. Cover these mode sequences:

| Saved configuration | Full check | Expected attempted modes |
| --- | ---: | --- |
| No source proxy | false | `direct_strict`, then `direct_relaxed` only when strict fails |
| Source proxy set | false | `direct_strict`, `proxy_strict`, then only the required relaxed modes |
| Source proxy set | true | all four modes in documented order |
| HTTP source | either | strict network modes only; TLS shown as not applicable |

Use these public report types:

```python
@dataclass(slots=True)
class ProbeAttempt:
    mode: str
    ok: bool
    http_status: int | None
    content_type: str
    latency_ms: int
    is_feed: bool
    feed_kind: str
    truncated: bool
    error_type: str
    error_message: str

    def as_dict(self) -> dict[str, Any]: ...


@dataclass(slots=True)
class ProbeReport:
    feed_id: str
    source_type: str
    attempts: list[ProbeAttempt]
    recommendation: dict[str, Any]

    def as_dict(self) -> dict[str, Any]: ...
```

`feed_kind` is one of `rss`, `atom`, `rdf`, `nitter`, or `unknown`. It supplements the approved `is_feed` field without changing its meaning.

- [ ] **Step 2: Add failing content-recognition tests**

Test case-insensitive recognition after optional BOM, whitespace and XML declaration:

```text
<rss ...>
<feed xmlns="http://www.w3.org/2005/Atom">
<rdf:RDF ...>
```

For Nitter, require a timeline marker such as `timeline-item` together with a tweet marker such as `tweet-content` or a matching `/<username>/status/<id>` link. Generic HTML with status 200 must remain `unknown`.

- [ ] **Step 3: Add failing error-classification tests**

Cover `dns`, `connect`, `timeout`, `tls_certificate`, `proxy`, `http_status`, `invalid_feed`, and `unknown`. Include both urllib and httpx exception shapes. Assert that classifier output strips URL query strings and never includes Authorization or key values.

Implement one `sanitize_error_message(exc, *, secrets)` helper used by every classifier branch. It must redact URL userinfo, query and fragment, replace each non-empty source key and proxy password, remove Authorization and Cookie header values, normalize whitespace, and truncate the displayed message to 500 characters. Tests must include exceptions containing the draft key and an authenticated proxy URL.

- [ ] **Step 4: Add failing recommendation tests**

Recommendation dictionaries must have this shape:

```python
{
    "code": "direct_strict",
    "verify_ssl": True,
    "use_proxy": False,
    "message": "默认网络与严格证书校验可用。",
}
```

Implement exact cases:

1. `direct_strict` success: `verify_ssl=True`, `use_proxy=False`.
2. Only `proxy_strict` success: `verify_ssl=True`, `use_proxy=True`.
3. Strict modes fail and a relaxed mode succeeds: `verify_ssl=False`, with the corresponding proxy value and a certificate warning message.
4. HTTP success with unrecognized content: `code="invalid_feed"`, both setting values `None`.
5. Every mode fails: `code="unreachable"`, both setting values `None`, message derived from the highest-priority classified failure.

- [ ] **Step 5: Confirm probe tests fail before implementation**

Run:

```powershell
python -m unittest tests.test_source_probe -v
```

Expected: import failure for `source_probe`.

- [ ] **Step 6: Implement SourceProbeService**

Use this constructor and method contract:

```python
class SourceProbeService:
    MAX_RESPONSE_BYTES = 256 * 1024
    MIN_TIMEOUT_SECONDS = 3
    MAX_TIMEOUT_SECONDS = 30

    def __init__(
        self,
        requester: Callable[..., SourceHttpResponse] = request_source,
        clock: Callable[[], float] = time.monotonic,
    ) -> None: ...

    async def probe(
        self,
        feed: FeedConfig,
        *,
        full_check: bool = False,
    ) -> ProbeReport: ...
```

For RSS, reuse `FeedFetcher._build_url_and_headers()` only after moving that pure helper to `source_http.py` as `build_rss_request(feed, etag="", last_modified="")`; update `FeedFetcher` to import it. For Twitter, add `build_nitter_timeline_request(feed)` to the same module and update `TwitterTimelineFetcher` to use the shared URL and header result. This ensures saved and draft probes reproduce normal source authentication and headers without instantiating storage-backed fetchers.

Run each blocking request with `asyncio.to_thread()`. Probe direct modes use `proxy_url=""` and `use_environment_proxy=False`; proxy modes use the source's explicit proxy. Always pass the bounded read and five-redirect limits.

- [ ] **Step 7: Keep probe execution free of persistence**

`SourceProbeService` must import neither `FeedStorage` nor dispatch, scheduler, pipeline, parser or dedup modules. Add a source-level assertion test:

```python
source = SOURCE_PROBE_PATH.read_text(encoding="utf-8")
for forbidden in ("FeedStorage", "Dispatcher", "Scheduler", "mark_sent"):
    self.assertNotIn(forbidden, source)
```

- [ ] **Step 8: Run service and fetcher regression tests**

Run:

```powershell
python -m unittest tests.test_source_http tests.test_source_probe tests.test_fetcher tests.test_twitter_source -v
```

Expected: all tests finish with `OK`.

- [ ] **Step 9: Commit the probe service**

```powershell
git add source_http.py source_probe.py fetcher.py twitter_source.py tests/test_source_probe.py tests/test_fetcher.py tests/test_twitter_source.py
git commit -m "feat: diagnose source network settings"
```

---

## Task 5: Register The Plugin Web API

**Files:**

- Create: `source_probe_api.py`
- Create: `tests/test_source_probe_api.py`
- Create: `tests/test_source_probe_compat.py`
- Modify: `main.py`

- [ ] **Step 1: Add API tests with AstrBot web stubs**

Following the repository's dynamic-import test style, stub:

```python
from astrbot.api.web import error_response, json_response, request
```

Test the following handler behavior:

1. `GET /astrbot_plugin_rss_forwarder/source-probe/feeds` returns enabled and disabled sources with ID, source type, redacted display URL, `proxy_configured`, timeout and current `verify_ssl`.
2. URL query values, authentication keys and Authorization data never appear in serialized JSON.
3. `POST /astrbot_plugin_rss_forwarder/source-probe/run` accepts either `feed_id` or a draft, never both.
4. Draft RSS accepts `url`, `proxy_url`, `timeout`, `verify_ssl`, `auth_mode`, and ephemeral `key`.
5. Draft Twitter accepts `username`, `nitter_url`, `proxy_url`, `timeout`, and `verify_ssl`.
6. URL schemes outside `http` and `https`, unsupported proxy schemes, malformed booleans and timeout values outside 3 to 30 return HTTP 400.
7. Missing saved feed returns HTTP 404.
8. A second in-flight request for the same `request.username` returns HTTP 429; another username can run concurrently.
9. The request key is absent from the report, captured logs and retained API object state after completion.
10. Registering the same two route-and-method pairs twice replaces the handlers instead of leaving duplicate routes.

Create `tests/test_source_probe_compat.py` with a minimal AstrBot context and two initialization cases:

1. When `importlib.util.find_spec("astrbot.api.web")` returns a module, `SourceProbeApi.register()` is called and polling plus commands initialize.
2. When the module is absent, API registration is skipped, one compatibility warning is logged, and polling plus `/rss probe` still receive the shared `SourceProbeService`.

The compatibility test must not create a Quart adapter. AstrBot releases before Plugin Pages use the command entry and do not expose the Page API.

- [ ] **Step 2: Confirm API tests fail**

Run:

```powershell
python -m unittest tests.test_source_probe_api tests.test_source_probe_compat -v
```

Expected: import failure for `source_probe_api`.

- [ ] **Step 3: Implement SourceProbeApi**

Use this public structure:

```python
PLUGIN_NAME = "astrbot_plugin_rss_forwarder"


class SourceProbeApi:
    def __init__(self, config: RSSConfig, service: SourceProbeService) -> None: ...

    def register(self, context: Context) -> None:
        context.register_web_api(
            f"/{PLUGIN_NAME}/source-probe/feeds",
            self.list_feeds,
            ["GET"],
            "List RSS Forwarder sources for diagnostics",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/source-probe/run",
            self.run_probe,
            ["POST"],
            "Probe RSS Forwarder source connectivity",
        )
```

Handlers use `request.username`, `await request.json(default={})`, `json_response()` and `error_response(message, status_code=400)`. Keep AstrBot request types inside this adapter; `SourceProbeService` accepts only `FeedConfig` and primitive values.

- [ ] **Step 4: Implement input normalization and redaction**

Create a short-lived `FeedConfig` for drafts. Validate raw JSON types before coercion. URL display redaction must remove userinfo, query and fragment while retaining scheme, hostname, optional safe port and path. Proxy serialization returns only a boolean.

Use a dictionary of `asyncio.Lock` instances keyed by `request.username or request.client_host or "anonymous"`. Reject an already locked key with 429 rather than queuing. Remove unused lock entries in `finally` after the request completes.

- [ ] **Step 5: Initialize the service and register API routes**

In `main.py`:

1. Construct one `SourceProbeService` after config validation.
2. Check for `astrbot.api.web` with `importlib.util.find_spec()` before importing `source_probe_api.py`.
3. When present, import `SourceProbeApi`, construct it with the active `RSSConfig`, and call `register(self.context)` once.
4. When absent, keep `_source_probe_api` as `None` and continue initialization.
5. Store the service on the plugin instance and pass it to commands regardless of Page API availability.

Only the deliberate module-availability check may skip registration. Import errors raised from plugin code after support is detected must propagate and fail initialization, preventing real defects from being hidden. AstrBot v4.20.0 therefore keeps polling and the command while ignoring `pages/`; v4.24.1 and newer use the official `astrbot.api.web` adapter and Plugin Page.

- [ ] **Step 6: Run API and initialization tests**

Run:

```powershell
python -m unittest tests.test_source_probe_api tests.test_source_probe_compat tests.test_config_translation -v
```

Expected: all tests finish with `OK`, including a registration assertion for both route names and methods.

- [ ] **Step 7: Commit the API adapter**

```powershell
git add source_probe_api.py main.py tests/test_source_probe_api.py tests/test_source_probe_compat.py tests/test_config_translation.py
git commit -m "feat: expose source probe API"
```

---

## Task 6: Add The `/rss probe` Compatibility Command

**Files:**

- Modify: `commands.py`
- Modify: `main.py`
- Modify: `tests/test_commands.py`

- [ ] **Step 1: Add failing route and formatting tests**

Extend `tests/test_commands.py` with:

1. `probe` appears in the route map.
2. Missing feed ID returns `用法：/rss probe <feed_id>`.
3. Unknown feed ID returns a concise not-found message.
4. A report formats each attempted mode with success or failure, latency, HTTP status, feed recognition and classified error.
5. The recommendation appears on the final line.
6. Credentials and full query strings are absent.
7. Concurrent command probes for the same command service are serialized with an `asyncio.Lock` to prevent accidental request bursts.

- [ ] **Step 2: Confirm command tests fail**

Run:

```powershell
python -m unittest tests.test_commands -v
```

Expected: the new route and handler assertions fail.

- [ ] **Step 3: Inject SourceProbeService into CommandService**

Extend the existing constructor with optional dependencies so legacy tests remain simple:

```python
def __init__(
    self,
    ...,
    source_probe_service: SourceProbeService | None = None,
) -> None:
    ...
```

Pass the service created in `main.py`. The command resolves feeds from the active `RSSConfig`, calls `service.probe(feed)`, and formats the same report returned by the Page API. The command never constructs a separate network implementation.

- [ ] **Step 4: Add help text**

Include `/rss probe <feed_id>` in the existing `/rss help` output with a short statement that the command only checks connectivity and does not save settings.

- [ ] **Step 5: Run command regressions**

Run:

```powershell
python -m unittest tests.test_commands tests.test_config_translation -v
```

Expected: all tests finish with `OK`.

- [ ] **Step 6: Commit command support**

```powershell
git add commands.py main.py tests/test_commands.py
git commit -m "feat: add RSS source probe command"
```

---

## Task 7: Build The Official AstrBot Plugin Page

**Files:**

- Create: `pages/source-diagnostics/index.html`
- Create: `pages/source-diagnostics/app.js`
- Create: `pages/source-diagnostics/style.css`
- Create: `.astrbot-plugin/i18n/zh-CN.json`
- Create: `.astrbot-plugin/i18n/en-US.json`
- Create: `.astrbot-plugin/i18n/ja-JP.json`
- Create: `tests/test_source_probe_page.py`

- [ ] **Step 1: Add static contract tests**

Create `tests/test_source_probe_page.py` and assert:

1. All three page files exist and `index.html` loads `./style.css` and `./app.js`.
2. JavaScript uses `window.AstrBotPluginPage`, `await bridge.ready()`, `bridge.apiGet("source-probe/feeds")`, and `bridge.apiPost("source-probe/run", ...)`.
3. JavaScript contains no `fetch(`, `localStorage`, `document.cookie`, `window.parent`, hard-coded Dashboard route, or plugin asset token.
4. The secret input uses `type="password"`, has autocomplete disabled, and is cleared in `finally` after every probe.
5. The run button is disabled while a request is active.
6. All locale files parse as JSON and contain `pages.source-diagnostics.title`, `description`, headings, mode labels, error labels, recommendation labels and security warning text.
7. CSS defines both light and dark theme variables and contains mobile media rules.

- [ ] **Step 2: Confirm page tests fail**

Run:

```powershell
python -m unittest tests.test_source_probe_page -v
```

Expected: missing-file failures.

- [ ] **Step 3: Create the semantic page structure**

Build a compact operational interface with:

1. A source selector and `草稿来源` choice.
2. Source type control using a segmented control for RSS and Twitter/Nitter.
3. URL or Nitter base URL, Twitter username, explicit proxy, timeout and current TLS setting fields.
4. Authentication mode and temporary key fields shown only for RSS drafts.
5. `完整检查` checkbox and one `开始探测` command button with a familiar activity icon or plain text when no icon dependency exists.
6. A result table whose rows are fixed to the four documented modes and can display `skipped`.
7. An unframed recommendation section with a distinct certificate warning for relaxed TLS success.

Use labels associated with every field, `aria-live="polite"` for status, and native controls. Keep cards limited to the result item grouping; do not nest cards.

- [ ] **Step 4: Implement bridge state and request behavior**

On startup:

```javascript
const bridge = window.AstrBotPluginPage;
await bridge.ready();
const feeds = await bridge.apiGet("source-probe/feeds");
```

Selecting a saved source sends only `{ feed_id, full_check }`. Draft mode sends validated visible fields. Do not copy masked saved URLs into draft requests. Catch bridge errors, render `error.message` as text content, and clear the key input in `finally`.

Register `bridge.onContext(renderTranslations)` so language and theme updates apply without page reload. All user-visible strings use `bridge.t()` with a Chinese fallback.

- [ ] **Step 5: Implement responsive and theme-aware styling**

Use AstrBot light and dark theme state through `[data-theme="dark"]`. Keep text at fixed rem sizes, use stable form heights, wrap long errors with `overflow-wrap:anywhere`, and switch the result table to labeled rows below 720 px. Avoid gradients, decorative blobs, oversized headings and rounded pill containers.

- [ ] **Step 6: Run page contract tests**

Run:

```powershell
python -m unittest tests.test_source_probe_page -v
```

Expected: all tests finish with `OK`.

- [ ] **Step 7: Commit the Plugin Page**

```powershell
git add pages .astrbot-plugin tests/test_source_probe_page.py
git commit -m "feat: add source diagnostics page"
```

---

## Task 8: Document And Version The User-Facing Update

**Files:**

- Modify: `metadata.yaml`
- Modify: `main.py`
- Modify: `CHANGELOG.md`
- Modify: `README.md`
- Modify: `README.en.md`
- Modify: `README.ja.md`
- Modify: `tests/test_config_translation.py`

- [ ] **Step 1: Add a failing version-consistency test**

Parse `metadata.yaml` with a small anchored regular expression and parse the `@register(...)` call in `main.py` with `ast`. Assert that normalized versions match and equal `0.7.1`.

Run:

```powershell
python -m unittest tests.test_config_translation -v
```

Expected: failure because metadata remains `v0.7.0` and the runtime decorator remains `0.6.5`.

- [ ] **Step 2: Update both version declarations**

Set:

```yaml
version: v0.7.1
```

and use `"0.7.1"` in the runtime `@register` decorator. Leave package name, repository and author metadata unchanged.

- [ ] **Step 3: Add the changelog entry**

At the top of `CHANGELOG.md`, add `v0.7.1` with:

1. Per-source TLS certificate verification for RSS and Twitter/Nitter.
2. Source diagnostics Plugin Page and `/rss probe` command.
3. Strict default behavior and media-request scope.
4. Enhanced fetch-failure identification from Task 0.

Keep release history out of README files.

- [ ] **Step 4: Update all README languages**

Add equivalent sections to the three README variants covering:

1. Where `verify_ssl` appears in source settings.
2. Why strict verification should remain enabled in normal cases.
3. How to open `插件详情 -> 来源诊断` and interpret direct, proxy, strict and relaxed modes.
4. The fact that probe recommendations are not automatically saved.
5. `/rss probe <feed_id>` usage for older AstrBot or mobile administration.
6. Plugin Pages require AstrBot v4.24.1 or newer while polling and the command remain available on earlier builds.

Do not include production source IDs, group IDs, personal IDs, source credentials or live URLs.

- [ ] **Step 5: Run metadata and documentation checks**

Run:

```powershell
python -m unittest tests.test_config_translation tests.test_source_probe_page -v
python -m json.tool _conf_schema.json
git diff --check
```

Expected: all tests and validation commands exit with status 0.

- [ ] **Step 6: Commit release metadata and documentation**

```powershell
git add metadata.yaml main.py CHANGELOG.md README.md README.en.md README.ja.md tests/test_config_translation.py
git commit -m "docs: prepare RSS Forwarder 0.7.1"
```

---

## Task 9: Complete Local Verification

**Files:**

- Verify: all changed Python, JSON, JavaScript, CSS and documentation files

- [ ] **Step 1: Run the complete unit-test suite with repository-local temporary files**

Create `.tmp` only when the test environment requires a writable temporary directory, then set `TEMP` and `TMP` for the current shell. Run:

```powershell
python -m unittest discover -s tests -v
```

Expected: all tests finish with `OK`; no external network request occurs in unit tests.

- [ ] **Step 2: Compile every Python module**

Run:

```powershell
python -m compileall -q .
```

Expected: exit status 0.

- [ ] **Step 3: Validate JSON resources**

Run once per file:

```powershell
python -m json.tool _conf_schema.json
python -m json.tool .astrbot-plugin/i18n/zh-CN.json
python -m json.tool .astrbot-plugin/i18n/en-US.json
python -m json.tool .astrbot-plugin/i18n/ja-JP.json
```

Expected: all four commands exit with status 0.

- [ ] **Step 4: Inspect scope and whitespace**

Run:

```powershell
git status --short
git diff --check HEAD
git log --oneline --decorate -10
```

Expected: only intended files appear, whitespace check is clean, and each completed task has a focused commit.

- [ ] **Step 5: Request a code review subagent**

Use `superpowers:requesting-code-review` against the full range from the Task 0 parent commit through current HEAD. Address correctness findings with tests before production synchronization.

---

## Task 10: Synchronize And Verify The Live Plugin

**Files:**

- Local source: `S:\Projects\astrbot_plugin_rss_forwarder`
- Remote plugin: `/volume1/docker/astrbot/data/plugins/astrbot_plugin_rss_forwarder`
- Remote container: `astrbot`

- [ ] **Step 1: Compare local, GitHub and live revisions before synchronization**

Record local HEAD, `origin/master`, remote plugin HEAD, live metadata version and live dirty status. Stop if the live plugin contains unrelated uncommitted source changes; preserve and review them before any copy.

- [ ] **Step 2: Push the verified commits**

Run:

```powershell
git push origin master
```

Expected: GitHub `master` advances to the locally verified HEAD.

- [ ] **Step 3: Back up and synchronize only the target plugin**

Create a timestamped backup of the remote plugin directory outside the live plugin directory. Synchronize repository files while excluding `.git`, Python caches, test caches and local temporary directories. Do not touch other plugins or containers.

- [ ] **Step 4: Compile the deployed plugin inside the AstrBot container**

Run remote `python -m compileall -q` against the deployed plugin directory from inside `astrbot`.

Expected: exit status 0.

- [ ] **Step 5: Reload only `astrbot_plugin_rss_forwarder`**

Use the authenticated Dashboard target-plugin reload API on `127.0.0.1:16185`. Do not restart the container. Capture the reload response and the plugin-specific log window.

Expected: version `0.7.1` loads, scheduler starts once, both source-probe routes register, and no import or duplicate-task errors appear.

- [ ] **Step 6: Verify a strict-success source through the Page API**

Open `插件详情 -> 来源诊断`, select a known healthy source, and run the normal check.

Expected: `direct_strict` or `proxy_strict` succeeds, content is recognized, and the recommendation keeps `verify_ssl=true`.

- [ ] **Step 7: Verify the known incomplete-certificate source**

Select saved feed `102` and run the full check.

Expected from the current source state: strict mode reports `tls_certificate`; the corresponding relaxed mode receives HTTP content and recognizes the feed. The recommendation proposes `verify_ssl=false` only for that source and displays the certificate warning. If the source operator has repaired the certificate chain before execution, record the new strict success and run the same classifier against a temporary localhost HTTPS fixture serving a minimal RSS document with a self-signed certificate; remove that fixture immediately after verification.

- [ ] **Step 8: Confirm probing leaves runtime state unchanged**

Before and after the feed `102` probe, compare its ETag, Last-Modified and sent-state records, plus the job's last-run data.

Expected: values are byte-for-byte unchanged and no outbound message is sent.

- [ ] **Step 9: Verify the compatibility command**

Run `/rss probe 102` from an authorized administration conversation.

Expected: compact mode rows and the same recommendation appear; no credentials or complete query strings appear in the message or logs.

- [ ] **Step 10: Inspect the Plugin Page visually**

Using the signed-in browser session, capture desktop and mobile screenshots in both light and dark themes. Check that controls remain visible, text does not overlap, long certificate errors wrap inside the result area, and the page console has no errors.

- [ ] **Step 11: Verify relaxed runtime fetching without changing saved production configuration**

Run an isolated in-container integration script against a copied `FeedConfig` for feed `102` with `verify_ssl=False`; use a temporary storage stub and call only `_fetch_single_feed()`. Confirm the main source request succeeds and the once-per-load warning contains only the feed ID. Exercise the media downloader separately with a mocked strict transport assertion. Do not write the copied setting to AstrBot configuration and do not invoke the scheduler or dispatcher.

- [ ] **Step 12: Record final repository parity**

Confirm local HEAD, GitHub `master` and remote plugin HEAD match. Confirm live metadata reports `v0.7.1` and `git status --short` is empty on both local and remote repositories.
