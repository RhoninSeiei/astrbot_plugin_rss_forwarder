import socket
import ssl
import sys
import types
import unittest
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from urllib.error import HTTPError, URLError

import httpx


astrbot_module = types.ModuleType("astrbot")
astrbot_api_module = types.ModuleType("astrbot.api")
astrbot_api_module.logger = types.SimpleNamespace(
    info=lambda *a, **k: None,
    warning=lambda *a, **k: None,
    error=lambda *a, **k: None,
)
sys.modules.setdefault("astrbot", astrbot_module)
sys.modules["astrbot.api"] = astrbot_api_module

ROOT = Path(__file__).resolve().parents[1]
SOURCE_PROBE_PATH = ROOT / "source_probe.py"
PACKAGE_NAME = "astrbot_source_probe_testpkg"
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
FeedConfig = config_module.FeedConfig
SourceHttpResponse = source_http_module.SourceHttpResponse
ProbeAttempt = source_probe_module.ProbeAttempt
SourceProbeService = source_probe_module.SourceProbeService
InvalidFeedError = source_probe_module.InvalidFeedError
classify_probe_error = source_probe_module.classify_probe_error
sanitize_error_message = source_probe_module.sanitize_error_message


def _feed(**overrides):
    values = {
        "id": "feed-1",
        "url": "https://example.com/feed.xml",
        "timeout": 1,
    }
    values.update(overrides)
    return FeedConfig(**values)


def _response(body=b"<rss version='2.0'></rss>", *, url="https://example.com/feed.xml"):
    return SourceHttpResponse(
        body=body,
        status=200,
        headers={"content-type": "application/rss+xml"},
        final_url=url,
    )


class _Clock:
    def __init__(self):
        self.value = 10.0

    def __call__(self):
        current = self.value
        self.value += 0.025
        return current


class SourceProbeModeTests(unittest.IsolatedAsyncioTestCase):
    async def test_report_types_serialize_nested_attempts(self):
        attempt = ProbeAttempt(
            mode="direct_strict",
            ok=True,
            http_status=200,
            content_type="application/rss+xml",
            latency_ms=25,
            is_feed=True,
            feed_kind="rss",
            truncated=False,
            error_type="",
            error_message="",
        )

        self.assertEqual(attempt.as_dict()["feed_kind"], "rss")

    async def test_no_proxy_stops_after_direct_strict_success(self):
        calls = []

        def requester(**kwargs):
            calls.append(kwargs)
            return _response()

        report = await SourceProbeService(requester, _Clock()).probe(_feed())

        self.assertEqual([attempt.mode for attempt in report.attempts], ["direct_strict"])
        self.assertEqual(report.attempts[0].latency_ms, 25)
        self.assertEqual(calls[0]["proxy_url"], "")
        self.assertIs(calls[0]["use_environment_proxy"], False)
        self.assertIs(calls[0]["verify_ssl"], True)
        self.assertEqual(calls[0]["timeout"], 3)
        self.assertEqual(calls[0]["max_bytes"], 256 * 1024)
        self.assertEqual(calls[0]["max_redirects"], 5)

    async def test_no_proxy_adds_direct_relaxed_after_strict_failure(self):
        calls = []

        def requester(**kwargs):
            calls.append(kwargs)
            if kwargs["verify_ssl"]:
                raise TimeoutError("timed out")
            return _response()

        report = await SourceProbeService(requester, _Clock()).probe(_feed(timeout=99))

        self.assertEqual(
            [attempt.mode for attempt in report.attempts],
            ["direct_strict", "direct_relaxed"],
        )
        self.assertEqual([call["timeout"] for call in calls], [30, 30])
        self.assertEqual([call["verify_ssl"] for call in calls], [True, False])

    async def test_proxy_runs_strict_modes_and_only_failed_counterpart_relaxed(self):
        calls = []

        def requester(**kwargs):
            calls.append(kwargs)
            if kwargs["proxy_url"] == "":
                if kwargs["verify_ssl"]:
                    raise OSError("connection refused")
                return _response()
            return _response()

        feed = _feed(proxy_url="http://proxy.example:8080")
        report = await SourceProbeService(requester, _Clock()).probe(feed)

        self.assertEqual(
            [attempt.mode for attempt in report.attempts],
            ["direct_strict", "proxy_strict", "direct_relaxed"],
        )
        self.assertEqual(
            [(call["proxy_url"], call["verify_ssl"]) for call in calls],
            [
                ("", True),
                ("http://proxy.example:8080", True),
                ("", False),
            ],
        )
        self.assertTrue(all(call["use_environment_proxy"] is False for call in calls))

    async def test_full_check_runs_all_four_modes_in_documented_order(self):
        calls = []

        def requester(**kwargs):
            calls.append(kwargs)
            return _response()

        report = await SourceProbeService(requester, _Clock()).probe(
            _feed(proxy_url="socks5://proxy.example:1080"),
            full_check=True,
        )

        self.assertEqual(
            [attempt.mode for attempt in report.attempts],
            ["direct_strict", "proxy_strict", "direct_relaxed", "proxy_relaxed"],
        )
        self.assertEqual([call["verify_ssl"] for call in calls], [True, True, False, False])

    async def test_http_source_uses_strict_network_modes_and_marks_tls_not_applicable(self):
        calls = []

        def requester(**kwargs):
            calls.append(kwargs)
            return _response(url="http://example.com/feed.xml")

        report = await SourceProbeService(requester, _Clock()).probe(
            _feed(
                url="http://example.com/feed.xml",
                proxy_url="http://proxy.example:8080",
            ),
            full_check=True,
        )

        self.assertEqual(
            [attempt.mode for attempt in report.attempts],
            ["direct_strict", "proxy_strict"],
        )
        self.assertTrue(all(call["verify_ssl"] is True for call in calls))
        self.assertIsNone(report.recommendation["verify_ssl"])
        self.assertIn("TLS 不适用", report.recommendation["message"])


class SourceProbeContentRecognitionTests(unittest.IsolatedAsyncioTestCase):
    async def _kind_for(self, body: bytes, *, feed=None):
        service = SourceProbeService(lambda **_kwargs: _response(body), _Clock())
        report = await service.probe(feed or _feed())
        return report.attempts[0]

    async def test_recognizes_xml_feed_roots_after_bom_whitespace_and_declaration(self):
        cases = {
            "rss": b"\xef\xbb\xbf  \n<?xml version='1.0'?>\n<RSS version='2.0'></RSS>",
            "atom": b"\n<?XML version='1.0'?>\n<FeEd xmlns='http://www.w3.org/2005/Atom'></FeEd>",
            "rdf": (
                b"\t<?xml version='1.0'?>\n"
                b"<rdf:RDF xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'>"
                b"</rdf:RDF>"
            ),
        }

        for expected, body in cases.items():
            with self.subTest(expected=expected):
                attempt = await self._kind_for(body)
                self.assertTrue(attempt.is_feed)
                self.assertEqual(attempt.feed_kind, expected)

    async def test_rejects_atom_without_standard_namespace(self):
        bodies = (
            b"<feed>marketing</feed>",
            b"<feed xmlns='urn:marketing'>marketing</feed>",
        )

        for body in bodies:
            with self.subTest(body=body):
                attempt = await self._kind_for(body)
                self.assertFalse(attempt.is_feed)
                self.assertEqual(attempt.feed_kind, "unknown")

    async def test_rejects_rdf_without_standard_namespace(self):
        bodies = (
            b"<RDF>marketing</RDF>",
            b"<rdf:RDF xmlns:rdf='urn:test'></rdf:RDF>",
        )

        for body in bodies:
            with self.subTest(body=body):
                attempt = await self._kind_for(body)
                self.assertFalse(attempt.is_feed)
                self.assertEqual(attempt.feed_kind, "unknown")

    async def test_recognizes_truncated_xml_after_first_root_start_element(self):
        cases = {
            "rss": b"<rss version='2.0'><channel><title>partial",
            "atom": (
                b"<feed xmlns='http://www.w3.org/2005/Atom'>"
                b"<title>partial"
            ),
            "rdf": (
                b"<rdf:RDF "
                b"xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'>"
                b"<item>partial"
            ),
        }

        for expected, body in cases.items():
            with self.subTest(expected=expected):
                attempt = await self._kind_for(body)
                self.assertTrue(attempt.is_feed)
                self.assertEqual(attempt.feed_kind, expected)

    async def test_rejects_rss_with_nonempty_namespace(self):
        attempt = await self._kind_for(
            b'<x:rss xmlns:x="urn:fake"><channel></channel></x:rss>'
        )

        self.assertFalse(attempt.is_feed)
        self.assertEqual(attempt.feed_kind, "unknown")

    async def test_recognizes_nitter_timeline_with_tweet_content(self):
        body = b"<html><div class='timeline-item'><div class='tweet-content'>post</div></div></html>"

        attempt = await self._kind_for(
            body,
            feed=_feed(
                source_type="twitter",
                username="Alice",
                nitter_url="https://nitter.example.com",
            ),
        )

        self.assertTrue(attempt.is_feed)
        self.assertEqual(attempt.feed_kind, "nitter")

    async def test_recognizes_nitter_timeline_with_matching_status_link(self):
        body = b"<html><div class='timeline-item'><a href='/alice/status/123'>post</a></div></html>"

        attempt = await self._kind_for(
            body,
            feed=_feed(
                source_type="twitter",
                username="Alice",
                nitter_url="https://nitter.example.com",
            ),
        )

        self.assertEqual(attempt.feed_kind, "nitter")

    async def test_generic_html_and_partial_nitter_markers_remain_unknown(self):
        bodies = (
            b"<html><main>ordinary page</main></html>",
            b"<html><a href='/alice/status/123'>post</a></html>",
            b"<html><div class='timeline-item'>empty</div></html>",
            b"<html><div class='timeline-item'><a href='/bob/status/123'>post</a></div></html>",
            b"<html><div class='timeline-item'><script>'/alice/status/123'</script></div></html>",
        )
        feed = _feed(
            source_type="twitter",
            username="Alice",
            nitter_url="https://nitter.example.com",
        )

        for body in bodies:
            with self.subTest(body=body):
                attempt = await self._kind_for(body, feed=feed)
                self.assertFalse(attempt.ok)
                self.assertFalse(attempt.is_feed)
                self.assertEqual(attempt.feed_kind, "unknown")
                self.assertEqual(attempt.error_type, "invalid_feed")

    async def test_nitter_markers_inside_non_content_nodes_remain_unknown(self):
        bodies = (
            (
                b"<html><script><div class='timeline-item'>"
                b"<div class='tweet-content'>fake</div></div></script></html>"
            ),
            (
                b"<html><style>.timeline-item .tweet-content { color: red; }"
                b"</style></html>"
            ),
            (
                b"<html><!-- <div class='timeline-item'>"
                b"<a href='/alice/status/123'>fake</a></div> --></html>"
            ),
            b"<html><p>timeline-item tweet-content /alice/status/123</p></html>",
            (
                b"<html><div class='not-timeline-item'>"
                b"<div class='tweet-content'>fake</div></div></html>"
            ),
        )
        feed = _feed(
            source_type="twitter",
            username="Alice",
            nitter_url="https://nitter.example.com",
        )

        for body in bodies:
            with self.subTest(body=body):
                attempt = await self._kind_for(body, feed=feed)
                self.assertFalse(attempt.is_feed)
                self.assertEqual(attempt.feed_kind, "unknown")


class SourceProbeErrorClassificationTests(unittest.IsolatedAsyncioTestCase):
    def test_classifies_urllib_and_builtin_exception_shapes(self):
        cases = (
            (socket.gaierror(socket.EAI_NONAME, "name not known"), "dns"),
            (URLError(ConnectionRefusedError(10061, "connection refused")), "connect"),
            (URLError(TimeoutError("timed out")), "timeout"),
            (
                URLError(ssl.SSLCertVerificationError(1, "certificate verify failed")),
                "tls_certificate",
            ),
            (URLError("Tunnel connection failed: 502 Bad Gateway"), "proxy"),
            (
                HTTPError(
                    "https://example.com/feed.xml?key=hidden#part",
                    503,
                    "Service Unavailable",
                    {},
                    None,
                ),
                "http_status",
            ),
            (InvalidFeedError("ordinary HTML"), "invalid_feed"),
            (RuntimeError("unexpected"), "unknown"),
        )

        for exc, expected in cases:
            with self.subTest(expected=expected):
                error_type, _message, _status = classify_probe_error(exc, secrets=())
                self.assertEqual(error_type, expected)

    def test_classifies_httpx_exception_shapes(self):
        request = httpx.Request("GET", "https://example.com/feed.xml?key=hidden")
        response = httpx.Response(502, request=request)
        cases = (
            (httpx.ConnectError("getaddrinfo failed", request=request), "dns"),
            (httpx.ConnectError("connection refused", request=request), "connect"),
            (httpx.ReadTimeout("read timed out", request=request), "timeout"),
            (
                httpx.ConnectError("CERTIFICATE_VERIFY_FAILED", request=request),
                "tls_certificate",
            ),
            (httpx.ProxyError("proxy authentication failed", request=request), "proxy"),
            (
                httpx.HTTPStatusError(
                    "502 Bad Gateway",
                    request=request,
                    response=response,
                ),
                "http_status",
            ),
        )

        for exc, expected in cases:
            with self.subTest(expected=expected):
                error_type, _message, status = classify_probe_error(exc, secrets=())
                self.assertEqual(error_type, expected)
                if expected == "http_status":
                    self.assertEqual(status, 502)

    async def test_non_success_response_object_is_classified_as_http_status(self):
        def requester(**kwargs):
            return SourceHttpResponse(
                body=b"<rss></rss>",
                status=503,
                headers={"content-type": "application/rss+xml"},
                final_url=kwargs["url"],
            )

        report = await SourceProbeService(requester, _Clock()).probe(
            _feed(url="http://example.com/feed.xml")
        )

        attempt = report.attempts[0]
        self.assertFalse(attempt.ok)
        self.assertEqual(attempt.http_status, 503)
        self.assertEqual(attempt.error_type, "http_status")

    def test_sanitizer_removes_urls_headers_secrets_and_limits_length(self):
        draft_key = "draft-source-key"
        proxy_password = "proxy-password"
        error = RuntimeError(
            "request https://source-user:source-pass@example.com/feed.xml?"
            f"key={draft_key}&next=1#fragment via "
            f"http://proxy-user:{proxy_password}@proxy.example:8080/path?mode=x#proxy "
            "Authorization: Bearer header-secret\n"
            "Cookie: session=cookie-secret\n" + "x" * 700
        )

        message = sanitize_error_message(
            error,
            secrets=(draft_key, proxy_password),
        )

        for forbidden in (
            "source-user",
            "source-pass",
            "draft-source-key",
            "proxy-user",
            "proxy-password",
            "?key=",
            "?mode=",
            "#fragment",
            "#proxy",
            "Authorization",
            "Cookie",
            "header-secret",
            "cookie-secret",
            "\n",
        ):
            self.assertNotIn(forbidden, message)
        self.assertLessEqual(len(message), 500)

    def test_sanitizer_handles_url_with_invalid_port_without_leaking_credentials(self):
        message = sanitize_error_message(
            RuntimeError(
                "failed http://user:password@example.com:bad/feed?key=source-secret#part"
            ),
            secrets=("source-secret", "password"),
        )

        self.assertIn("<invalid-url>", message)
        self.assertNotIn("user", message)
        self.assertNotIn("password", message)
        self.assertNotIn("?key=", message)
        self.assertNotIn("#part", message)

    def test_source_hostname_containing_proxy_does_not_imply_proxy_failure(self):
        error_type, _message, _status = classify_probe_error(
            RuntimeError("failed https://proxy-news.example/feed.xml"),
            secrets=(),
        )

        self.assertEqual(error_type, "unknown")

    def test_sanitizer_removes_query_and_fragment_from_relative_urls(self):
        message = sanitize_error_message(
            RuntimeError("failed /feed.xml?token=relative-secret&next=1#part"),
            secrets=(),
        )

        self.assertIn("<relative-url-redacted>", message)
        self.assertNotIn("token=", message)
        self.assertNotIn("relative-secret", message)
        self.assertNotIn("next=", message)
        self.assertNotIn("#part", message)

    def test_sanitizer_redacts_single_quoted_header_dictionary(self):
        message = sanitize_error_message(
            RuntimeError(
                "headers={'Authorization': 'Bearer leaked-token', "
                "'Cookie': 'sid=secret'}"
            ),
            secrets=(),
        )

        for forbidden in (
            "leaked-token",
            "sid=secret",
            "Authorization",
            "Cookie",
        ):
            self.assertNotIn(forbidden, message)

    def test_sanitizer_redacts_double_quoted_header_dictionary(self):
        message = sanitize_error_message(
            RuntimeError(
                'headers={"Authorization": "Bearer leaked-double", '
                '"Cookie": "sid=double-secret"}'
            ),
            secrets=(),
        )

        for forbidden in (
            "leaked-double",
            "sid=double-secret",
            "Authorization",
            "Cookie",
        ):
            self.assertNotIn(forbidden, message)

    def test_sanitizer_replaces_malformed_url_with_safe_placeholder(self):
        malformed = "https://user:password@[broken/feed?token=leaked-query#fragment"

        message = sanitize_error_message(
            RuntimeError(f"failed {malformed}"),
            secrets=(),
        )

        self.assertIn("<invalid-url>", message)
        for forbidden in (
            malformed,
            "user",
            "password",
            "leaked-query",
            "fragment",
            "?token=",
        ):
            self.assertNotIn(forbidden, message)
        self.assertLessEqual(len(message), 500)

    def test_sanitizer_redacts_mixed_quoted_header_dictionary_conservatively(self):
        message = sanitize_error_message(
            RuntimeError(
                "prefix {'aUtHoRiZaTiOn': \"Bearer mixed-token\", "
                "\"Cookie\": 'sid=mixed-secret'} trailing-text"
            ),
            secrets=(),
        )

        self.assertEqual(message, "<request-headers-redacted>")

    def test_sanitizer_redacts_header_dictionary_with_escaped_quote_values(self):
        message = sanitize_error_message(
            RuntimeError(
                "prefix {\"Authorization\": \"Bearer tok\\\"en\", "
                "'Cookie': 'sid=sec\\'ret'} trailing-text"
            ),
            secrets=(),
        )

        self.assertEqual(message, "<request-headers-redacted>")

    def test_sanitizer_removes_absolute_url_query_with_spaces_and_following_tokens(self):
        message = sanitize_error_message(
            RuntimeError(
                "failed https://user:password@example.com/feed.xml?first=one "
                "second=two#fragment trailing-token"
            ),
            secrets=(),
        )

        for forbidden in (
            "user",
            "password",
            "first=",
            "second=",
            "fragment",
            "trailing-token",
        ):
            self.assertNotIn(forbidden, message)
        self.assertLessEqual(len(message), 500)

    def test_sanitizer_removes_relative_url_query_with_spaces_and_following_tokens(self):
        message = sanitize_error_message(
            RuntimeError(
                "failed /feed.xml?first=one second=two#fragment trailing-token"
            ),
            secrets=(),
        )

        for forbidden in (
            "first=",
            "second=",
            "fragment",
            "trailing-token",
        ):
            self.assertNotIn(forbidden, message)
        self.assertLessEqual(len(message), 500)

    async def test_service_uses_sanitizer_for_reported_exceptions(self):
        def requester(**_kwargs):
            raise RuntimeError(
                "failed https://user:pass@example.com/feed?key=draft-source-key#frag "
                "through http://proxy:proxy-password@proxy.example:8080?x=1 "
                "Authorization: Bearer header-secret Cookie: session=cookie-secret"
            )

        report = await SourceProbeService(requester, _Clock()).probe(
            _feed(
                key="draft-source-key",
                proxy_url="http://proxy:proxy-password@proxy.example:8080",
            )
        )

        for attempt in report.attempts:
            for forbidden in (
                "draft-source-key",
                "proxy-password",
                "user:pass",
                "?key=",
                "Authorization",
                "Cookie",
                "header-secret",
                "cookie-secret",
            ):
                self.assertNotIn(forbidden, attempt.error_message)


class SourceProbeRecommendationTests(unittest.IsolatedAsyncioTestCase):
    async def test_direct_strict_success_recommends_strict_without_proxy(self):
        report = await SourceProbeService(
            lambda **_kwargs: _response(),
            _Clock(),
        ).probe(_feed())

        self.assertEqual(
            report.recommendation,
            {
                "code": "direct_strict",
                "verify_ssl": True,
                "use_proxy": False,
                "message": "默认网络与严格证书校验可用。",
            },
        )

    async def test_only_proxy_strict_success_recommends_saved_proxy(self):
        def requester(**kwargs):
            if not kwargs["proxy_url"]:
                raise ConnectionRefusedError("direct connection refused")
            return _response()

        report = await SourceProbeService(requester, _Clock()).probe(
            _feed(proxy_url="http://proxy.example:8080")
        )

        self.assertEqual(
            report.recommendation,
            {
                "code": "proxy_strict",
                "verify_ssl": True,
                "use_proxy": True,
                "message": "来源代理与严格证书校验可用。",
            },
        )

    async def test_relaxed_success_recommends_matching_network_with_warning(self):
        def requester(**kwargs):
            if kwargs["verify_ssl"]:
                raise ssl.SSLCertVerificationError(1, "certificate verify failed")
            if kwargs["proxy_url"]:
                raise ConnectionRefusedError("proxy connection refused")
            return _response()

        report = await SourceProbeService(requester, _Clock()).probe(
            _feed(proxy_url="http://proxy.example:8080")
        )

        self.assertEqual(report.recommendation["code"], "direct_relaxed")
        self.assertIs(report.recommendation["verify_ssl"], False)
        self.assertIs(report.recommendation["use_proxy"], False)
        self.assertIn("证书", report.recommendation["message"])
        self.assertIn("安全", report.recommendation["message"])

    async def test_http_success_with_unrecognized_content_recommends_invalid_feed(self):
        report = await SourceProbeService(
            lambda **_kwargs: _response(
                b"<html><main>ordinary page</main></html>",
                url="http://example.com/feed.xml",
            ),
            _Clock(),
        ).probe(_feed(url="http://example.com/feed.xml"))

        self.assertEqual(report.recommendation["code"], "invalid_feed")
        self.assertIsNone(report.recommendation["verify_ssl"])
        self.assertIsNone(report.recommendation["use_proxy"])

    async def test_all_modes_fail_uses_highest_priority_classified_failure(self):
        def requester(**kwargs):
            if kwargs["proxy_url"]:
                raise httpx.ProxyError(
                    "proxy authentication failed",
                    request=httpx.Request("GET", kwargs["url"]),
                )
            if kwargs["verify_ssl"]:
                raise ssl.SSLCertVerificationError(1, "certificate verify failed")
            raise RuntimeError("later unknown failure")

        report = await SourceProbeService(requester, _Clock()).probe(
            _feed(proxy_url="http://proxy.example:8080")
        )

        self.assertEqual(report.recommendation["code"], "unreachable")
        self.assertIsNone(report.recommendation["verify_ssl"])
        self.assertIsNone(report.recommendation["use_proxy"])
        self.assertIn("证书", report.recommendation["message"])
        self.assertIn("certificate verify failed", report.recommendation["message"])

    def test_probe_service_source_has_no_persistence_or_runtime_pipeline_imports(self):
        source = SOURCE_PROBE_PATH.read_text(encoding="utf-8")
        for forbidden in (
            "FeedStorage",
            "Dispatcher",
            "Scheduler",
            "mark_sent",
            "dispatcher",
            "scheduler",
            "pipeline",
            "from .parser",
            "semantic_dedup",
            "astrbot.api.web",
        ):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
