import inspect
import ssl
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request

import source_http


ROOT = Path(__file__).resolve().parents[1]


class _FakeUrllibResponse:
    def __init__(
        self,
        body: bytes = b"response body",
        *,
        status: int = 200,
        headers=None,
        final_url: str = "https://example.com/final",
    ) -> None:
        self._body = body
        self.status = status
        self.headers = headers or {"Content-Type": "application/xml"}
        self._final_url = final_url
        self.read_sizes = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self, size=-1):
        self.read_sizes.append(size)
        return self._body if size is None or size < 0 else self._body[:size]

    def geturl(self):
        return self._final_url


class _FakeOpener:
    def __init__(self, response, captured) -> None:
        self._response = response
        self._captured = captured

    def open(self, request, timeout):
        self._captured["request"] = request
        self._captured["timeout"] = timeout
        return self._response


def _capture_urllib(response=None):
    captured = {}
    response = response or _FakeUrllibResponse()

    def fake_build_opener(*handlers):
        captured["handlers"] = handlers
        return _FakeOpener(response, captured)

    return captured, response, fake_build_opener


def _request(**overrides):
    values = {
        "url": "https://example.com/feed.xml",
        "headers": {"Accept": "application/xml"},
        "proxy_url": "",
        "timeout": 17,
        "verify_ssl": True,
    }
    values.update(overrides)
    return source_http.request_source(**values)


class SourceHttpPublicApiTests(unittest.TestCase):
    def test_build_rss_request_applies_authentication_and_conditional_headers(self):
        feed = types.SimpleNamespace(
            url="https://example.com/feed.xml?existing=yes",
            auth_mode="query",
            key="draft-secret",
        )

        url, headers = source_http.build_rss_request(
            feed,
            etag="etag-before",
            last_modified="Tue, 12 May 2026 00:00:00 GMT",
        )

        self.assertIn("existing=yes", url)
        self.assertIn("key=draft-secret", url)
        self.assertIn("application/rss+xml", headers["Accept"])
        self.assertEqual(headers["If-None-Match"], "etag-before")
        self.assertEqual(
            headers["If-Modified-Since"],
            "Tue, 12 May 2026 00:00:00 GMT",
        )

    def test_build_nitter_timeline_request_uses_saved_source_fields(self):
        feed = types.SimpleNamespace(
            username="@Alice",
            nitter_url="https://nitter.example.com/base/",
            url="",
        )

        url, headers = source_http.build_nitter_timeline_request(feed)

        self.assertEqual(url, "https://nitter.example.com/base/Alice")
        self.assertIn("text/html", headers["Accept"])
        self.assertIn("astrbot_plugin_rss_forwarder", headers["User-Agent"])

    def test_build_nitter_timeline_request_accepts_explicit_default_instance(self):
        feed = types.SimpleNamespace(
            username="alice",
            nitter_url="",
            url="",
        )

        url, _headers = source_http.build_nitter_timeline_request(
            feed,
            default_nitter_url="https://custom-nitter.example/base/",
        )

        self.assertEqual(url, "https://custom-nitter.example/base/alice")

    def test_response_type_and_request_signature_are_public_contract(self):
        response = source_http.SourceHttpResponse(
            body=b"body",
            status=200,
            headers={"X-Test": "yes"},
            final_url="https://example.com/final",
        )

        self.assertEqual(response.body, b"body")
        self.assertIs(response.truncated, False)
        self.assertEqual(
            list(inspect.signature(source_http.request_source).parameters),
            [
                "url",
                "headers",
                "proxy_url",
                "timeout",
                "verify_ssl",
                "max_bytes",
                "max_redirects",
                "use_environment_proxy",
            ],
        )
        self.assertTrue(
            all(
                parameter.kind is inspect.Parameter.KEYWORD_ONLY
                for parameter in inspect.signature(
                    source_http.request_source
                ).parameters.values()
            )
        )

    def test_response_header_helpers_normalize_and_read_case_insensitively(self):
        if not hasattr(source_http, "normalize_response_headers"):
            self.fail("normalize_response_headers must be public")
        if not hasattr(source_http, "get_response_header"):
            self.fail("get_response_header must be public")

        headers = source_http.normalize_response_headers(
            {
                "ETag": "etag-a",
                "last-modified": "Tue, 12 May 2026 00:00:00 GMT",
            }
        )

        self.assertEqual(
            headers,
            {
                "etag": "etag-a",
                "last-modified": "Tue, 12 May 2026 00:00:00 GMT",
            },
        )
        self.assertEqual(source_http.get_response_header(headers, "ETag"), "etag-a")
        self.assertEqual(
            source_http.get_response_header(headers, "Last-Modified"),
            "Tue, 12 May 2026 00:00:00 GMT",
        )
        self.assertEqual(source_http.get_response_header(headers, "Missing"), "")


class SourceHttpSslAndProxyTests(unittest.TestCase):
    def test_strict_https_uses_certificate_required_context(self):
        captured, _response, fake_build_opener = _capture_urllib()

        with patch.object(source_http, "build_opener", fake_build_opener):
            _request(verify_ssl=True)

        https_handler = next(
            handler
            for handler in captured["handlers"]
            if isinstance(handler, HTTPSHandler)
        )
        self.assertEqual(https_handler._context.verify_mode, ssl.CERT_REQUIRED)

    def test_relaxed_https_disables_hostname_and_certificate_checks(self):
        captured, _response, fake_build_opener = _capture_urllib()

        with patch.object(source_http, "build_opener", fake_build_opener):
            _request(verify_ssl=False)

        https_handler = next(
            handler
            for handler in captured["handlers"]
            if isinstance(handler, HTTPSHandler)
        )
        self.assertIs(https_handler._context.check_hostname, False)
        self.assertEqual(https_handler._context.verify_mode, ssl.CERT_NONE)

    def test_explicit_http_proxy_is_installed_for_http_and_https(self):
        captured, _response, fake_build_opener = _capture_urllib()
        proxy_mappings = []

        def fake_proxy_handler(mapping=None):
            proxy_mappings.append(mapping)
            return ("proxy", mapping)

        with (
            patch.object(source_http, "build_opener", fake_build_opener),
            patch.object(source_http, "ProxyHandler", fake_proxy_handler),
        ):
            _request(proxy_url="http://127.0.0.1:7890")

        self.assertEqual(
            proxy_mappings,
            [
                {
                    "http": "http://127.0.0.1:7890",
                    "https": "http://127.0.0.1:7890",
                }
            ],
        )
        self.assertIn(("proxy", proxy_mappings[0]), captured["handlers"])

    def test_direct_probe_installs_empty_proxy_handler(self):
        _captured, _response, fake_build_opener = _capture_urllib()
        proxy_mappings = []

        def fake_proxy_handler(mapping=None):
            proxy_mappings.append(mapping)
            return ("proxy", mapping)

        with (
            patch.object(source_http, "build_opener", fake_build_opener),
            patch.object(source_http, "ProxyHandler", fake_proxy_handler),
        ):
            _request(use_environment_proxy=False)

        self.assertEqual(proxy_mappings, [{}])

    def test_default_mode_retains_environment_proxy_handler(self):
        _captured, _response, fake_build_opener = _capture_urllib()
        proxy_mappings = []

        def fake_proxy_handler(mapping=None):
            proxy_mappings.append(mapping)
            return ("proxy", mapping)

        with (
            patch.object(source_http, "build_opener", fake_build_opener),
            patch.object(source_http, "ProxyHandler", fake_proxy_handler),
        ):
            _request()

        self.assertEqual(proxy_mappings, [None])


class SourceHttpSocksTests(unittest.TestCase):
    def test_public_client_selector_recognizes_all_socks_schemes(self):
        if not hasattr(source_http, "source_http_client"):
            self.fail("source_http_client must be public")

        for proxy_url in (
            "socks://127.0.0.1:1080",
            "socks4://127.0.0.1:1080",
            "socks5://127.0.0.1:1080",
            " SOCKS5H://127.0.0.1:1080 ",
        ):
            with self.subTest(proxy_url=proxy_url):
                self.assertEqual(source_http.source_http_client(proxy_url), "httpx")
        for proxy_url in ("", "http://127.0.0.1:7890", "https://proxy.example"):
            with self.subTest(proxy_url=proxy_url):
                self.assertEqual(source_http.source_http_client(proxy_url), "urllib")

    def _stub_httpx(self, *, error=None, chunks=None):
        captured = {}
        chunks = list(chunks or [b"socks body"])

        class FakeResponse:
            status_code = 206
            headers = {"Content-Type": "application/rss+xml"}
            url = "https://example.com/socks-final"

            @property
            def content(self):
                captured["content_reads"] = captured.get("content_reads", 0) + 1
                return b"".join(chunks)

            def __enter__(self):
                captured["response_entered"] = True
                return self

            def __exit__(self, exc_type, exc, tb):
                captured["response_closed"] = True
                return False

            def raise_for_status(self):
                captured["raise_for_status"] = True
                if error is not None:
                    raise error

            def iter_bytes(self, chunk_size=None):
                captured["iter_chunk_size"] = chunk_size
                for chunk in chunks:
                    captured["iterated_chunks"] = (
                        captured.get("iterated_chunks", 0) + 1
                    )
                    captured["iterated_bytes"] = (
                        captured.get("iterated_bytes", 0) + len(chunk)
                    )
                    yield chunk

        class FakeClient:
            def __init__(self, **kwargs):
                captured["kwargs"] = kwargs

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def get(self, url):
                captured["url"] = url
                return FakeResponse()

            def stream(self, method, url):
                captured["stream"] = (method, url)
                return FakeResponse()

        return captured, types.SimpleNamespace(Client=FakeClient)

    def test_socks_proxy_uses_httpx_with_finite_redirect_limit(self):
        captured, httpx_stub = self._stub_httpx()

        with patch.dict(sys.modules, {"httpx": httpx_stub}):
            response = _request(
                proxy_url="socks5h://127.0.0.1:1080",
                verify_ssl=False,
                max_redirects=5,
            )

        self.assertEqual(
            captured["kwargs"],
            {
                "headers": {"Accept": "application/xml"},
                "timeout": 17,
                "follow_redirects": True,
                "verify": False,
                "proxy": "socks5h://127.0.0.1:1080",
                "max_redirects": 5,
            },
        )
        self.assertTrue(captured["raise_for_status"])
        self.assertEqual(response.body, b"socks body")
        self.assertEqual(response.status, 206)
        self.assertEqual(response.final_url, "https://example.com/socks-final")

    def test_socks_proxy_omits_unlimited_redirect_setting(self):
        captured, httpx_stub = self._stub_httpx()

        with patch.dict(sys.modules, {"httpx": httpx_stub}):
            _request(proxy_url="socks://127.0.0.1:1080", max_redirects=None)

        self.assertNotIn("max_redirects", captured["kwargs"])

    def test_finite_socks_read_streams_only_limit_plus_one_byte(self):
        captured, httpx_stub = self._stub_httpx(
            chunks=[b"abc", b"def", b"unread", b"also unread"]
        )

        with patch.dict(sys.modules, {"httpx": httpx_stub}):
            response = _request(
                proxy_url="socks5://127.0.0.1:1080",
                max_bytes=5,
            )

        self.assertEqual(
            captured.get("stream"),
            ("GET", "https://example.com/feed.xml"),
        )
        self.assertEqual(captured["iter_chunk_size"], 6)
        self.assertEqual(captured["iterated_chunks"], 2)
        self.assertEqual(captured["iterated_bytes"], 6)
        self.assertEqual(captured.get("content_reads", 0), 0)
        self.assertTrue(captured["response_closed"])
        self.assertEqual(response.body, b"abcde")
        self.assertIs(response.truncated, True)

    def test_httpx_http_status_error_is_propagated(self):
        expected = RuntimeError("status failure")
        _captured, httpx_stub = self._stub_httpx(error=expected)

        with patch.dict(sys.modules, {"httpx": httpx_stub}):
            with self.assertRaisesRegex(RuntimeError, "status failure"):
                _request(proxy_url="socks5://127.0.0.1:1080")


class SourceHttpReadingAndResponseTests(unittest.TestCase):
    def test_finite_read_consumes_one_extra_byte_and_truncates(self):
        body = bytes(range(256)) * 1025
        captured, response, fake_build_opener = _capture_urllib(
            _FakeUrllibResponse(body)
        )

        with patch.object(source_http, "build_opener", fake_build_opener):
            result = _request(max_bytes=256 * 1024)

        self.assertEqual(response.read_sizes, [256 * 1024 + 1])
        self.assertEqual(result.body, body[: 256 * 1024])
        self.assertIs(result.truncated, True)
        self.assertEqual(captured["timeout"], 17)

    def test_unlimited_read_returns_full_body(self):
        body = b"complete feed body"
        _captured, response, fake_build_opener = _capture_urllib(
            _FakeUrllibResponse(body)
        )

        with patch.object(source_http, "build_opener", fake_build_opener):
            result = _request(max_bytes=None)

        self.assertEqual(response.read_sizes, [-1])
        self.assertEqual(result.body, body)
        self.assertIs(result.truncated, False)

    def test_urllib_response_preserves_status_headers_and_final_url(self):
        source_response = _FakeUrllibResponse(
            b"created",
            status=201,
            headers={"X-Source": "rss", "X-Count": "2"},
            final_url="https://example.com/redirected/feed.xml",
        )
        _captured, _response, fake_build_opener = _capture_urllib(source_response)

        with patch.object(source_http, "build_opener", fake_build_opener):
            result = _request()

        self.assertEqual(result.status, 201)
        self.assertEqual(result.headers, {"x-source": "rss", "x-count": "2"})
        self.assertEqual(
            result.final_url, "https://example.com/redirected/feed.xml"
        )

    def test_non_positive_read_limit_is_rejected_before_transport(self):
        for value in (0, -1):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    _request(max_bytes=value)


class SourceHttpRedirectAndDependencyTests(unittest.TestCase):
    def _assert_caller_redirect_limit(self, urls, *, max_redirects):
        class FakeParent:
            def __init__(self):
                self.open_count = 0

            def open(self, request, timeout):
                self.open_count += 1
                request.timeout = timeout
                return request

        class FakeResponse:
            def read(self):
                return b""

            def close(self):
                pass

        parent = FakeParent()
        handler = source_http._LimitedRedirectHandler(max_redirects)
        handler.parent = parent
        request = Request("https://example.com/start")
        request.timeout = 17
        raised = None

        for url in urls:
            try:
                request = handler.http_error_302(
                    request,
                    FakeResponse(),
                    302,
                    "Found",
                    {"location": url},
                )
            except Exception as exc:
                raised = exc
                break

        self.assertIsInstance(raised, source_http.TooManyRedirects)
        self.assertEqual(parent.open_count, max_redirects)

    def test_sixth_urllib_redirect_raises_sanitized_error(self):
        secret_redirect = "https://example.com/final?token=secret-value"

        class RedirectingOpener:
            def __init__(self, handler):
                self._handler = handler

            def open(self, request, timeout):
                current = request
                for _ in range(6):
                    current = self._handler.redirect_request(
                        current,
                        None,
                        302,
                        "Found",
                        {},
                        secret_redirect,
                    )
                raise AssertionError("redirect limit was not enforced")

        def fake_build_opener(*handlers):
            redirect_handler = next(
                handler
                for handler in handlers
                if isinstance(handler, HTTPRedirectHandler)
            )
            return RedirectingOpener(redirect_handler)

        with patch.object(source_http, "build_opener", fake_build_opener):
            with self.assertRaises(source_http.TooManyRedirects) as raised:
                _request(max_redirects=5)

        self.assertNotIn("secret-value", str(raised.exception))
        self.assertNotIn("token=", str(raised.exception))

    def test_unlimited_urllib_redirects_use_default_handler_behavior(self):
        captured, _response, fake_build_opener = _capture_urllib()

        with patch.object(source_http, "build_opener", fake_build_opener):
            _request(max_redirects=None)

        self.assertFalse(
            any(
                isinstance(handler, HTTPRedirectHandler)
                for handler in captured["handlers"]
            )
        )

    def test_caller_limit_precedes_standard_library_repeat_limit(self):
        repeated_url = "https://example.com/repeated"

        self._assert_caller_redirect_limit(
            [repeated_url] * 13,
            max_redirects=12,
        )

    def test_caller_limit_above_ten_precedes_standard_library_total_limit(self):
        self._assert_caller_redirect_limit(
            [f"https://example.com/redirect-{index}" for index in range(13)],
            max_redirects=12,
        )

    def test_requirements_declares_httpx_socks_dependency(self):
        requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")

        self.assertIn("Pillow", requirements.splitlines())
        self.assertIn("httpx[socks]>=0.27,<1", requirements.splitlines())


if __name__ == "__main__":
    unittest.main()
