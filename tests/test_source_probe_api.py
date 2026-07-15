import asyncio
import contextvars
import json
import sys
import types
import unittest
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


class _RequestStub:
    def __init__(self) -> None:
        self._body = contextvars.ContextVar("source_probe_body", default={})
        self._username = contextvars.ContextVar("source_probe_username", default=None)
        self._client_host = contextvars.ContextVar(
            "source_probe_client_host", default=None
        )

    @property
    def username(self):
        return self._username.get()

    @property
    def client_host(self):
        return self._client_host.get()

    async def json(self, default=None):
        return self._body.get(default)

    async def invoke(self, handler, body, username, client_host):
        body_token = self._body.set(body)
        username_token = self._username.set(username)
        client_host_token = self._client_host.set(client_host)
        try:
            return await handler()
        finally:
            self._client_host.reset(client_host_token)
            self._username.reset(username_token)
            self._body.reset(body_token)


request_stub = _RequestStub()
captured_logs = []


def _capture_log(*args, **_kwargs):
    captured_logs.append(" ".join(str(value) for value in args))


def _json_response(data, status_code=200):
    return {"status_code": status_code, "data": data}


def _error_response(message, status_code=400):
    return {"status_code": status_code, "message": message}


astrbot_module = types.ModuleType("astrbot")
astrbot_api_module = types.ModuleType("astrbot.api")
astrbot_api_module.logger = types.SimpleNamespace(
    info=_capture_log,
    warning=_capture_log,
    error=_capture_log,
)
astrbot_web_module = types.ModuleType("astrbot.api.web")
astrbot_web_module.request = request_stub
astrbot_web_module.json_response = _json_response
astrbot_web_module.error_response = _error_response
astrbot_star_module = types.ModuleType("astrbot.api.star")
astrbot_star_module.Context = object
sys.modules.setdefault("astrbot", astrbot_module)
sys.modules["astrbot.api"] = astrbot_api_module
sys.modules["astrbot.api.web"] = astrbot_web_module
sys.modules["astrbot.api.star"] = astrbot_star_module

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "astrbot_source_probe_api_testpkg"
package_module = types.ModuleType(PACKAGE_NAME)
package_module.__path__ = [str(ROOT)]
sys.modules[PACKAGE_NAME] = package_module


def _load_module(module_name: str):
    full_name = f"{PACKAGE_NAME}.{module_name}"
    spec = spec_from_file_location(full_name, ROOT / f"{module_name}.py")
    module = module_from_spec(spec)
    sys.modules[full_name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


config_module = _load_module("config")
source_http_module = _load_module("source_http")
source_probe_module = _load_module("source_probe")
source_probe_api_module = _load_module("source_probe_api")
FeedConfig = config_module.FeedConfig
RSSConfig = config_module.RSSConfig
SourceProbeApi = source_probe_api_module.SourceProbeApi


class _Report:
    def __init__(self, payload=None) -> None:
        self._payload = payload or {
            "feed_id": "draft",
            "source_type": "rss",
            "attempts": [],
            "recommendation": {"code": "direct_strict"},
        }

    def as_dict(self):
        return dict(self._payload)


class _Service:
    def __init__(self, callback=None) -> None:
        self._callback = callback

    async def probe(self, feed, *, full_check=False):
        if self._callback is None:
            return _Report()
        result = self._callback(feed, full_check)
        if hasattr(result, "__await__"):
            return await result
        return result


def _config(*feeds):
    return RSSConfig(feeds=list(feeds), targets=[], jobs=[])


def _rss_feed(**overrides):
    values = {
        "id": "rss-1",
        "url": "https://example.com/feed.xml",
        "timeout": 10,
    }
    values.update(overrides)
    return FeedConfig(**values)


def _twitter_feed(**overrides):
    values = {
        "id": "twitter-1",
        "url": "",
        "source_type": "twitter",
        "username": "astrbot",
        "nitter_url": "https://nitter.example.com",
        "timeout": 10,
    }
    values.update(overrides)
    return FeedConfig(**values)


class SourceProbeApiListTests(unittest.IsolatedAsyncioTestCase):
    async def test_list_feeds_redacts_urls_and_exposes_only_safe_fields(self):
        config = _config(
            _rss_feed(
                url=(
                    "https://source-user:source-pass@example.com:8443/private/feed.xml"
                    "?token=query-secret&Authorization=bearer-secret#fragment-secret"
                ),
                proxy_url="http://proxy-user:proxy-pass@proxy.example:8080",
                auth_mode="header",
                key="saved-auth-secret",
                verify_ssl=False,
            ),
            _twitter_feed(
                nitter_url=(
                    "https://nitter-user:nitter-pass@nitter.example.com/base"
                    "?session=nitter-secret#nitter-fragment"
                ),
                enabled=False,
            ),
        )
        api = SourceProbeApi(config, _Service())

        response = await api.list_feeds()

        self.assertEqual(response["status_code"], 200)
        self.assertEqual(
            response["data"],
            [
                {
                    "id": "rss-1",
                    "source_type": "rss",
                    "enabled": True,
                    "display_url": "https://example.com:8443/private/feed.xml",
                    "proxy_configured": True,
                    "timeout": 10,
                    "verify_ssl": False,
                },
                {
                    "id": "twitter-1",
                    "source_type": "twitter",
                    "enabled": False,
                    "display_url": "https://nitter.example.com/base",
                    "proxy_configured": False,
                    "timeout": 10,
                    "verify_ssl": True,
                },
            ],
        )
        serialized = json.dumps(response, ensure_ascii=False)
        for secret in (
            "source-user",
            "source-pass",
            "query-secret",
            "bearer-secret",
            "fragment-secret",
            "proxy-user",
            "proxy-pass",
            "saved-auth-secret",
            "nitter-user",
            "nitter-pass",
            "nitter-secret",
            "nitter-fragment",
            "Authorization",
            "token=",
            "session=",
        ):
            self.assertNotIn(secret, serialized)

    async def test_twitter_default_instance_is_displayed_without_query_data(self):
        api = SourceProbeApi(
            _config(_twitter_feed(nitter_url="", url="")),
            _Service(),
        )

        response = await api.list_feeds()

        self.assertEqual(
            response["data"][0]["display_url"],
            "https://nitter.net",
        )


class SourceProbeApiRunTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, api, body, username="operator", client_host=None):
        return await request_stub.invoke(
            api.run_probe,
            body,
            username,
            client_host,
        )

    async def test_saved_feed_runs_with_boolean_full_check(self):
        feed = _rss_feed(enabled=False)
        captured = []

        def capture(candidate, full_check):
            captured.append((candidate, full_check))
            return _Report({"feed_id": candidate.id, "attempts": []})

        api = SourceProbeApi(_config(feed), _Service(capture))

        response = await self._run(
            api,
            {"feed_id": "rss-1", "full_check": True},
        )

        self.assertEqual(response["status_code"], 200)
        self.assertIs(captured[0][0], feed)
        self.assertIs(captured[0][1], True)

    async def test_draft_rss_accepts_only_typed_probe_fields(self):
        captured = []
        captured_logs.clear()

        def capture(feed, full_check):
            captured.append(
                {
                    "id": feed.id,
                    "source_type": feed.source_type,
                    "url": feed.url,
                    "proxy_url": feed.proxy_url,
                    "timeout": feed.timeout,
                    "verify_ssl": feed.verify_ssl,
                    "auth_mode": feed.auth_mode,
                    "key": feed.key,
                    "full_check": full_check,
                }
            )
            return _Report(
                {
                    "feed_id": "draft",
                    "attempts": [
                        {"error_message": "request failed for ephemeral-key"}
                    ],
                }
            )

        api = SourceProbeApi(_config(), _Service(capture))
        body = {
            "draft": {
                "source_type": "rss",
                "url": "https://example.com/feed.xml?private=query-value",
                "proxy_url": "socks5://proxy-user:proxy-pass@proxy.example:1080",
                "timeout": 12,
                "verify_ssl": False,
                "auth_mode": "query",
                "key": "ephemeral-key",
            },
            "full_check": False,
        }

        response = await self._run(api, body)

        self.assertEqual(response["status_code"], 200)
        self.assertEqual(
            captured,
            [
                {
                    "id": "draft",
                    "source_type": "rss",
                    "url": body["draft"]["url"],
                    "proxy_url": body["draft"]["proxy_url"],
                    "timeout": 12,
                    "verify_ssl": False,
                    "auth_mode": "query",
                    "key": "ephemeral-key",
                    "full_check": False,
                }
            ],
        )
        self.assertNotIn("ephemeral-key", json.dumps(response, ensure_ascii=False))
        self.assertNotIn("ephemeral-key", "\n".join(captured_logs))
        self.assertNotIn("ephemeral-key", repr(vars(api)))

    async def test_report_redacts_proxy_url_and_host_variants_longest_first(self):
        proxy_url = (
            "socks5://proxy-user:proxy-pass@proxy.example:1080/private"
            "?route=secret#fragment"
        )
        proxy_without_userinfo = (
            "socks5://proxy.example:1080/private?route=secret#fragment"
        )
        proxy_without_userinfo_and_query = "socks5://proxy.example:1080/private"
        proxy_host_port = "proxy.example:1080"
        proxy_hostname = "proxy.example"

        def report_proxy_values(_feed, _full_check):
            return _Report(
                {
                    "feed_id": "draft",
                    "attempts": [
                        {"error_message": f"full={proxy_url}"},
                        {
                            "error_message": (
                                f"without_userinfo={proxy_without_userinfo}"
                            )
                        },
                        {
                            "error_message": (
                                "without_userinfo_and_query="
                                f"{proxy_without_userinfo_and_query}"
                            )
                        },
                        {"error_message": f"endpoint={proxy_host_port}"},
                        {"error_message": f"hostname={proxy_hostname}"},
                    ],
                }
            )

        api = SourceProbeApi(_config(), _Service(report_proxy_values))

        response = await self._run(
            api,
            {
                "draft": {
                    "source_type": "rss",
                    "url": "https://example.com/feed.xml",
                    "proxy_url": proxy_url,
                }
            },
        )

        self.assertEqual(
            [attempt["error_message"] for attempt in response["data"]["attempts"]],
            [
                "full=<redacted>",
                "without_userinfo=<redacted>",
                "without_userinfo_and_query=<redacted>",
                "endpoint=<redacted>",
                "hostname=<redacted>",
            ],
        )

    async def test_recommendation_message_redacts_secrets_without_changing_fields(self):
        proxy_url = "socks5://proxy-user:proxy-pass@proxy.example:1080/private"

        def report_recommendation(_feed, _full_check):
            return _Report(
                {
                    "feed_id": "draft",
                    "source_type": "rss",
                    "attempts": [],
                    "recommendation": {
                        "code": "unreachable",
                        "verify_ssl": False,
                        "use_proxy": True,
                        "message": (
                            f"proxy={proxy_url}; host=proxy.example; "
                            "key=ephemeral-key; user=proxy-user; "
                            "password=proxy-pass"
                        ),
                    },
                }
            )

        api = SourceProbeApi(_config(), _Service(report_recommendation))

        response = await self._run(
            api,
            {
                "draft": {
                    "source_type": "rss",
                    "url": "https://example.com/feed.xml",
                    "proxy_url": proxy_url,
                    "auth_mode": "query",
                    "key": "ephemeral-key",
                }
            },
        )

        self.assertEqual(
            response["data"]["recommendation"],
            {
                "code": "unreachable",
                "verify_ssl": False,
                "use_proxy": True,
                "message": (
                    "proxy=<redacted>; host=<redacted>; key=<redacted>; "
                    "user=<redacted>; password=<redacted>"
                ),
            },
        )

    async def test_report_redacts_only_the_standalone_proxy_port_number(self):
        def report_proxy_port(_feed, _full_check):
            return _Report(
                {
                    "feed_id": "draft",
                    "source_type": "rss",
                    "attempts": [
                        {
                            "error_type": "proxy",
                            "error_message": (
                                "proxy port 1080 failed after 3 attempts with status 503"
                            )
                        }
                    ],
                    "recommendation": {"code": "direct_strict"},
                }
            )

        api = SourceProbeApi(_config(), _Service(report_proxy_port))

        response = await self._run(
            api,
            {
                "draft": {
                    "source_type": "rss",
                    "url": "https://example.com/feed.xml",
                    "proxy_url": "socks5://proxy.example:1080",
                }
            },
        )

        self.assertEqual(
            response["data"]["attempts"][0]["error_message"],
            "proxy port <redacted> failed after 3 attempts with status 503",
        )

    async def test_proxy_port_redaction_requires_proxy_error_or_address_context(self):
        def report_port_context(_feed, _full_check):
            return _Report(
                {
                    "feed_id": "draft",
                    "source_type": "rss",
                    "attempts": [
                        {
                            "error_type": "http_status",
                            "error_message": (
                                "HTTP status 1080 after 3 attempts with status 503"
                            ),
                        },
                        {
                            "error_type": "connect",
                            "error_message": (
                                "endpoint proxy.example:1080 rejected proxy port 1080"
                            ),
                        },
                    ],
                    "recommendation": {"code": "direct_strict"},
                }
            )

        api = SourceProbeApi(_config(), _Service(report_port_context))

        response = await self._run(
            api,
            {
                "draft": {
                    "source_type": "rss",
                    "url": "https://example.com/feed.xml",
                    "proxy_url": "socks5://proxy.example:1080",
                }
            },
        )

        self.assertEqual(
            [attempt["error_message"] for attempt in response["data"]["attempts"]],
            [
                "HTTP status 1080 after 3 attempts with status 503",
                "endpoint <redacted> rejected proxy port <redacted>",
            ],
        )

    async def test_short_proxy_hostnames_only_redact_error_message_boundaries(self):
        cases = (
            ("rss", "rssfeed"),
            ("a", "data"),
        )

        for hostname, ordinary_word in cases:
            proxy_url = f"socks5://proxy-user:proxy-pass@{hostname}:1080"

            def report_short_hostname(_feed, _full_check):
                return _Report(
                    {
                        "feed_id": hostname,
                        "source_type": "rss",
                        "attempts": [
                            {
                                "error_type": hostname,
                                "error_message": (
                                    f"url={proxy_url}; endpoint={hostname}:1080; "
                                    f"host={hostname}; ordinary={ordinary_word}; "
                                    "key=ephemeral-key; user=proxy-user; "
                                    "password=proxy-pass"
                                )
                            }
                        ],
                        "recommendation": {"code": "direct_strict"},
                    }
                )

            api = SourceProbeApi(_config(), _Service(report_short_hostname))
            with self.subTest(hostname=hostname):
                response = await self._run(
                    api,
                    {
                        "draft": {
                            "source_type": "rss",
                            "url": "https://example.com/feed.xml",
                            "proxy_url": proxy_url,
                            "auth_mode": "query",
                            "key": "ephemeral-key",
                        }
                    },
                )

                self.assertEqual(response["data"]["feed_id"], hostname)
                self.assertEqual(response["data"]["source_type"], "rss")
                self.assertEqual(
                    response["data"]["recommendation"],
                    {"code": "direct_strict"},
                )
                self.assertEqual(
                    response["data"]["attempts"][0]["error_type"],
                    hostname,
                )
                self.assertEqual(
                    response["data"]["attempts"][0]["error_message"],
                    (
                        "url=<redacted>; endpoint=<redacted>; host=<redacted>; "
                        f"ordinary={ordinary_word}; key=<redacted>; "
                        "user=<redacted>; password=<redacted>"
                    ),
                )

    async def test_proxy_hostname_redaction_normalizes_dns_idna_and_ipv6(self):
        cases = (
            (
                "socks5://Proxy.Example:1080",
                "host PROXY.EXAMPLE failed; ordinary=prefixPROXY.EXAMPLEsuffix",
                "host <redacted> failed; ordinary=prefixPROXY.EXAMPLEsuffix",
            ),
            (
                "socks5://例子.测试:1080",
                (
                    "host xn--fsqu00a.xn--0zwm56d failed; "
                    "ordinary=prefixxn--fsqu00a.xn--0zwm56dsuffix"
                ),
                (
                    "host <redacted> failed; "
                    "ordinary=prefixxn--fsqu00a.xn--0zwm56dsuffix"
                ),
            ),
            (
                "socks5://xn--fsqu00a.xn--0zwm56d:1080",
                "host 例子.测试 failed; ordinary=前缀例子.测试后缀",
                "host <redacted> failed; ordinary=前缀例子.测试后缀",
            ),
            (
                (
                    "socks5://[2001:0db8:0000:0000:0000:0000:0000:0001]"
                    ":1080"
                ),
                "host [2001:db8::1] failed",
                "host [<redacted>] failed",
            ),
            (
                "socks5://[2001:db8::1]:1080",
                "host 2001:0db8:0000:0000:0000:0000:0000:0001 failed",
                "host <redacted> failed",
            ),
        )

        for proxy_url, error_message, expected in cases:
            def report_hostname(_feed, _full_check):
                return _Report(
                    {
                        "feed_id": "draft",
                        "source_type": "rss",
                        "attempts": [
                            {
                                "error_type": "proxy",
                                "error_message": error_message,
                            }
                        ],
                        "recommendation": {"code": "direct_strict"},
                    }
                )

            api = SourceProbeApi(_config(), _Service(report_hostname))
            with self.subTest(proxy_url=proxy_url):
                response = await self._run(
                    api,
                    {
                        "draft": {
                            "source_type": "rss",
                            "url": "https://example.com/feed.xml",
                            "proxy_url": proxy_url,
                        }
                    },
                )

                self.assertEqual(
                    response["data"]["attempts"][0]["error_message"],
                    expected,
                )
                self.assertEqual(response["data"]["source_type"], "rss")
                self.assertEqual(
                    response["data"]["recommendation"],
                    {"code": "direct_strict"},
                )

    async def test_draft_twitter_accepts_typed_source_fields(self):
        captured = []

        def capture(feed, full_check):
            captured.append(
                (
                    feed.source_type,
                    feed.username,
                    feed.nitter_url,
                    feed.proxy_url,
                    feed.timeout,
                    feed.verify_ssl,
                    feed.key,
                    full_check,
                )
            )
            return _Report({"feed_id": "draft", "source_type": "twitter"})

        api = SourceProbeApi(_config(), _Service(capture))

        response = await self._run(
            api,
            {
                "draft": {
                    "source_type": "twitter",
                    "username": "@astrbot",
                    "nitter_url": "https://nitter.example.com/base",
                    "proxy_url": "https://proxy.example:8443",
                    "timeout": 30,
                    "verify_ssl": True,
                },
                "full_check": True,
            },
        )

        self.assertEqual(response["status_code"], 200)
        self.assertEqual(
            captured,
            [
                (
                    "twitter",
                    "astrbot",
                    "https://nitter.example.com/base",
                    "https://proxy.example:8443",
                    30,
                    True,
                    "",
                    True,
                )
            ],
        )

    async def test_feed_id_and_draft_are_mutually_exclusive_and_required(self):
        api = SourceProbeApi(_config(_rss_feed()), _Service())
        cases = (
            {},
            {"feed_id": "rss-1", "draft": {}},
            {"feed_id": "", "draft": {"source_type": "rss"}},
        )

        for body in cases:
            with self.subTest(body=body):
                response = await self._run(api, body)
                self.assertEqual(response["status_code"], 400)

    async def test_missing_saved_feed_returns_404(self):
        api = SourceProbeApi(_config(), _Service())

        response = await self._run(api, {"feed_id": "missing"})

        self.assertEqual(response["status_code"], 404)

    async def test_raw_json_types_are_validated_before_coercion(self):
        api = SourceProbeApi(_config(_rss_feed()), _Service())
        invalid_bodies = (
            [],
            "feed",
            1,
            False,
            None,
            {"feed_id": 1},
            {"draft": []},
            {"feed_id": "rss-1", "full_check": 1},
            {"draft": {"source_type": 1, "url": "https://example.com/feed"}},
            {"draft": {"source_type": "rss", "url": 1}},
            {"draft": {"source_type": "rss", "url": "https://example.com/feed", "proxy_url": False}},
            {"draft": {"source_type": "rss", "url": "https://example.com/feed", "timeout": True}},
            {"draft": {"source_type": "rss", "url": "https://example.com/feed", "timeout": "10"}},
            {"draft": {"source_type": "rss", "url": "https://example.com/feed", "verify_ssl": "false"}},
            {"draft": {"source_type": "rss", "url": "https://example.com/feed", "auth_mode": 1}},
            {"draft": {"source_type": "rss", "url": "https://example.com/feed", "key": 1}},
            {"draft": {"source_type": "twitter", "username": True, "nitter_url": "https://nitter.example"}},
            {"draft": {"source_type": "twitter", "username": "name", "nitter_url": []}},
        )

        for body in invalid_bodies:
            with self.subTest(body=body):
                response = await self._run(api, body)
                self.assertEqual(response["status_code"], 400)

    async def test_url_proxy_timeout_and_auth_values_are_validated(self):
        api = SourceProbeApi(_config(), _Service())
        invalid_drafts = (
            {"source_type": "rss", "url": "ftp://example.com/feed"},
            {"source_type": "rss", "url": "relative/feed.xml"},
            {"source_type": "rss", "url": "https://example.com:bad/feed"},
            {"source_type": "rss", "url": "https://example.com/feed", "proxy_url": "ftp://proxy.example"},
            {"source_type": "rss", "url": "https://example.com/feed", "timeout": 2},
            {"source_type": "rss", "url": "https://example.com/feed", "timeout": 31},
            {"source_type": "rss", "url": "https://example.com/feed", "auth_mode": "cookie"},
            {"source_type": "email", "url": "https://example.com/feed"},
            {"source_type": "twitter", "username": "", "nitter_url": "https://nitter.example"},
            {"source_type": "twitter", "username": "name", "nitter_url": "file:///tmp/nitter"},
        )

        for draft in invalid_drafts:
            with self.subTest(draft=draft):
                response = await self._run(api, {"draft": draft})
                self.assertEqual(response["status_code"], 400)

    async def test_url_authorities_reject_whitespace_controls_and_backslashes(self):
        api = SourceProbeApi(_config(), _Service())
        invalid_drafts = (
            {"source_type": "rss", "url": "https://exa mple.com/feed"},
            {"source_type": "rss", "url": "https://example.com\t.evil/feed"},
            {"source_type": "rss", "url": "https://example.com\\evil/feed"},
            {
                "source_type": "twitter",
                "username": "name",
                "nitter_url": "https://nitt er.example/base",
            },
            {
                "source_type": "twitter",
                "username": "name",
                "nitter_url": "https://nitter.example\r.evil/base",
            },
            {
                "source_type": "twitter",
                "username": "name",
                "nitter_url": "https://nitter.example\\evil/base",
            },
            {
                "source_type": "rss",
                "url": "https://example.com/feed",
                "proxy_url": "socks5://proxy .example:1080",
            },
            {
                "source_type": "rss",
                "url": "https://example.com/feed",
                "proxy_url": "socks5://proxy.example\n.evil:1080",
            },
            {
                "source_type": "rss",
                "url": "https://example.com/feed",
                "proxy_url": "socks5://proxy.example\\evil:1080",
            },
        )

        for draft in invalid_drafts:
            with self.subTest(draft=draft):
                response = await self._run(api, {"draft": draft})
                self.assertEqual(response["status_code"], 400)

    async def test_full_raw_urls_reject_forbidden_characters_in_all_components(self):
        api = SourceProbeApi(_config(), _Service())
        characters = (
            ("space", " "),
            ("newline", "\n"),
            ("tab", "\t"),
            ("backslash", "\\"),
        )
        fields = (
            ("url", "https://rss.example"),
            ("nitter_url", "https://nitter.example"),
            ("proxy_url", "socks5://proxy.example:1080"),
        )
        components = (
            ("path", lambda base, char: f"{base}/bad{char}value"),
            ("query", lambda base, char: f"{base}/feed?q=bad{char}value"),
            ("fragment", lambda base, char: f"{base}/feed#bad{char}value"),
        )

        for field_name, base_url in fields:
            for component_name, build_url in components:
                for character_name, character in characters:
                    value = build_url(base_url, character)
                    if field_name == "url":
                        draft = {"source_type": "rss", "url": value}
                    elif field_name == "nitter_url":
                        draft = {
                            "source_type": "twitter",
                            "username": "name",
                            "nitter_url": value,
                        }
                    else:
                        draft = {
                            "source_type": "rss",
                            "url": "https://example.com/feed",
                            "proxy_url": value,
                        }

                    with self.subTest(
                        field=field_name,
                        component=component_name,
                        character=character_name,
                    ):
                        response = await self._run(api, {"draft": draft})
                        self.assertEqual(response["status_code"], 400)

    async def test_percent_encoded_url_characters_remain_valid(self):
        api = SourceProbeApi(_config(), _Service())
        encoded_suffix = "/feed%20path?q=%5C#%0A"
        drafts = (
            {
                "source_type": "rss",
                "url": f"https://rss.example{encoded_suffix}",
                "proxy_url": f"socks5://proxy.example:1080{encoded_suffix}",
            },
            {
                "source_type": "twitter",
                "username": "name",
                "nitter_url": f"https://nitter.example{encoded_suffix}",
            },
        )

        for draft in drafts:
            with self.subTest(source_type=draft["source_type"]):
                response = await self._run(api, {"draft": draft})
                self.assertEqual(response["status_code"], 200)

    async def test_url_validation_accepts_ipv6_idn_and_percent_encoded_paths(self):
        captured = []

        def capture(feed, _full_check):
            captured.append(feed)
            return _Report()

        api = SourceProbeApi(_config(), _Service(capture))
        ipv6_response = await self._run(
            api,
            {
                "draft": {
                    "source_type": "rss",
                    "url": "https://[2001:db8::1]:8443/%E8%AE%A2%E9%98%85%20feed",
                    "proxy_url": "socks5://[2001:db8::2]:1080/%2Froute",
                }
            },
        )
        idn_response = await self._run(
            api,
            {
                "draft": {
                    "source_type": "twitter",
                    "username": "astrbot",
                    "nitter_url": "https://例子.测试/%E9%95%9C%E5%83%8F",
                    "proxy_url": "https://代理.测试:8443/%2Fproxy",
                }
            },
        )

        self.assertEqual(ipv6_response["status_code"], 200)
        self.assertEqual(idn_response["status_code"], 200)
        self.assertEqual(len(captured), 2)

    async def test_request_and_draft_fields_use_source_specific_whitelists(self):
        api = SourceProbeApi(_config(_rss_feed()), _Service())
        invalid_bodies = (
            {"feed_id": "rss-1", "unexpected": "value"},
            {
                "draft": {
                    "source_type": "rss",
                    "url": "https://example.com/feed",
                    "username": "unused",
                }
            },
            {
                "draft": {
                    "source_type": "twitter",
                    "username": "astrbot",
                    "nitter_url": "https://nitter.example",
                    "key": "must-not-be-accepted",
                }
            },
        )

        for body in invalid_bodies:
            with self.subTest(body=body):
                response = await self._run(api, body)
                self.assertEqual(response["status_code"], 400)

    async def test_same_username_is_rejected_while_another_username_runs(self):
        entered = {"alice": asyncio.Event(), "bob": asyncio.Event()}
        release = asyncio.Event()

        async def block(_feed, _full_check):
            username = request_stub.username
            entered[username].set()
            await release.wait()
            return _Report()

        api = SourceProbeApi(_config(_rss_feed()), _Service(block))
        first = asyncio.create_task(
            self._run(api, {"feed_id": "rss-1"}, username="alice")
        )
        await entered["alice"].wait()

        duplicate = await self._run(
            api,
            {"feed_id": "rss-1"},
            username="alice",
        )
        other = asyncio.create_task(
            self._run(api, {"feed_id": "rss-1"}, username="bob")
        )
        await entered["bob"].wait()

        self.assertEqual(duplicate["status_code"], 429)
        self.assertFalse(other.done())
        release.set()
        self.assertEqual((await first)["status_code"], 200)
        self.assertEqual((await other)["status_code"], 200)
        self.assertEqual(api._probe_locks, {})

    async def test_blank_username_and_client_host_use_anonymous_lock_key(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def block(_feed, _full_check):
            nonlocal calls
            calls += 1
            entered.set()
            if calls == 1:
                await release.wait()
            return _Report()

        api = SourceProbeApi(_config(_rss_feed()), _Service(block))
        first = asyncio.create_task(
            self._run(
                api,
                {"feed_id": "rss-1"},
                username="  ",
                client_host="\t ",
            )
        )
        await entered.wait()

        lock_keys = list(api._probe_locks)
        duplicate = await self._run(
            api,
            {"feed_id": "rss-1"},
            username=None,
            client_host="",
        )

        release.set()
        self.assertEqual((await first)["status_code"], 200)
        self.assertEqual(lock_keys, ["anonymous"])
        self.assertEqual(duplicate["status_code"], 429)
        self.assertEqual(api._probe_locks, {})

    async def test_client_host_limits_concurrency_and_cleans_lock_entries(self):
        entered = {
            "192.0.2.10": asyncio.Event(),
            "192.0.2.11": asyncio.Event(),
        }
        calls = {host: 0 for host in entered}
        release = asyncio.Event()

        async def block(_feed, _full_check):
            client_host = request_stub.client_host.strip()
            calls[client_host] += 1
            entered[client_host].set()
            if calls[client_host] == 1:
                await release.wait()
            return _Report()

        api = SourceProbeApi(_config(_rss_feed()), _Service(block))
        first = asyncio.create_task(
            self._run(
                api,
                {"feed_id": "rss-1"},
                username="  ",
                client_host=" 192.0.2.10 ",
            )
        )
        await entered["192.0.2.10"].wait()

        duplicate = await self._run(
            api,
            {"feed_id": "rss-1"},
            username="",
            client_host="192.0.2.10",
        )
        other = asyncio.create_task(
            self._run(
                api,
                {"feed_id": "rss-1"},
                username=None,
                client_host="192.0.2.11",
            )
        )
        await entered["192.0.2.11"].wait()

        self.assertEqual(duplicate["status_code"], 429)
        self.assertFalse(other.done())
        self.assertEqual(set(api._probe_locks), {"192.0.2.10", "192.0.2.11"})
        release.set()
        self.assertEqual((await first)["status_code"], 200)
        self.assertEqual((await other)["status_code"], 200)
        self.assertEqual(api._probe_locks, {})


class SourceProbeApiRegistrationTests(unittest.TestCase):
    class _Context:
        def __init__(self) -> None:
            self.registered_web_apis = []

        def register_web_api(self, route, view_handler, methods, desc):
            for index, registered in enumerate(self.registered_web_apis):
                if registered[0] == route and registered[2] == methods:
                    self.registered_web_apis[index] = (
                        route,
                        view_handler,
                        methods,
                        desc,
                    )
                    return
            self.registered_web_apis.append((route, view_handler, methods, desc))

    def test_repeated_registration_replaces_both_official_route_handlers(self):
        context = self._Context()
        original = SourceProbeApi(_config(), _Service())
        replacement = SourceProbeApi(_config(), _Service())

        original.register(context)
        replacement.register(context)

        self.assertEqual(len(context.registered_web_apis), 2)
        self.assertEqual(
            [(route, methods) for route, _handler, methods, _desc in context.registered_web_apis],
            [
                (
                    "/astrbot_plugin_rss_forwarder/source-probe/feeds",
                    ["GET"],
                ),
                (
                    "/astrbot_plugin_rss_forwarder/source-probe/run",
                    ["POST"],
                ),
            ],
        )
        self.assertIs(context.registered_web_apis[0][1].__self__, replacement)
        self.assertIs(context.registered_web_apis[1][1].__self__, replacement)


if __name__ == "__main__":
    unittest.main()
