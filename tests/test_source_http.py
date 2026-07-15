import http.server
import inspect
import select
import socket
import socketserver
import ssl
import sys
import threading
import types
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request

import source_http


ROOT = Path(__file__).resolve().parents[1]


class _LocalHttpHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.requests.append(
            {
                "path": self.path,
                "headers": dict(self.headers.items()),
            }
        )
        body = b"socks4 integration response"
        self.send_response(200)
        self.send_header("Content-Type", "application/rss+xml")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format, *_args):
        return None


class _ThreadingServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class _Socks4Endpoint(socketserver.BaseRequestHandler):
    @staticmethod
    def _read_exact(connection, size):
        data = bytearray()
        while len(data) < size:
            chunk = connection.recv(size - len(data))
            if not chunk:
                raise ConnectionError("SOCKS4 client closed during handshake")
            data.extend(chunk)
        return bytes(data)

    @staticmethod
    def _read_until_null(connection):
        data = bytearray()
        while len(data) < 4096:
            chunk = connection.recv(1)
            if not chunk:
                raise ConnectionError("SOCKS4 client closed during handshake")
            if chunk == b"\x00":
                return bytes(data)
            data.extend(chunk)
        raise ValueError("SOCKS4 field is too long")

    def handle(self):
        header = self._read_exact(self.request, 8)
        version, command = header[0], header[1]
        destination_port = int.from_bytes(header[2:4], "big")
        destination_ip = socket.inet_ntoa(header[4:8])
        self._read_until_null(self.request)
        if header[4:7] == b"\x00\x00\x00" and header[7] != 0:
            destination_host = self._read_until_null(self.request).decode("idna")
        else:
            destination_host = destination_ip

        self.server.handshakes.append(
            {
                "version": version,
                "command": command,
                "host": destination_host,
                "port": destination_port,
            }
        )
        if version != 4 or command != 1:
            self.request.sendall(b"\x00\x5b" + header[2:8])
            return

        with socket.create_connection(
            (destination_host, destination_port),
            timeout=2,
        ) as upstream:
            self.request.sendall(b"\x00\x5a" + header[2:8])
            sockets = (self.request, upstream)
            while True:
                readable, _writable, _exceptional = select.select(
                    sockets,
                    [],
                    [],
                    2,
                )
                if not readable:
                    raise TimeoutError("SOCKS4 relay timed out")
                for source in readable:
                    data = source.recv(65536)
                    if not data:
                        return
                    target = upstream if source is self.request else self.request
                    target.sendall(data)


@contextmanager
def _running_server(server):
    thread = threading.Thread(
        target=server.serve_forever,
        kwargs={"poll_interval": 0.01},
        daemon=True,
    )
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


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
                self.assertEqual(source_http.source_http_client(proxy_url), "urllib3")
        for proxy_url in ("", "http://127.0.0.1:7890", "https://proxy.example"):
            with self.subTest(proxy_url=proxy_url):
                self.assertEqual(source_http.source_http_client(proxy_url), "urllib")

    def _stub_socks_manager(self, *, status=206, chunks=None):
        captured = {}
        chunks = list(chunks or [b"socks body"])

        class FakeResponse:
            headers = {"Content-Type": "application/rss+xml"}

            def __init__(self):
                self.status = status

            def get_redirect_location(self):
                return None

            def read(self):
                captured["content_reads"] = captured.get("content_reads", 0) + 1
                return b"".join(chunks)

            def stream(self, chunk_size=None, decode_content=None):
                captured["stream_chunk_size"] = chunk_size
                captured["decode_content"] = decode_content
                for chunk in chunks:
                    captured["streamed_chunks"] = (
                        captured.get("streamed_chunks", 0) + 1
                    )
                    captured["streamed_bytes"] = (
                        captured.get("streamed_bytes", 0) + len(chunk)
                    )
                    yield chunk

            def release_conn(self):
                captured["response_released"] = True

            def close(self):
                captured["response_closed"] = True

        class FakeManager:
            def request(self, method, url, **kwargs):
                captured["request"] = (method, url)
                captured["request_kwargs"] = kwargs
                return FakeResponse()

            def clear(self):
                captured["manager_cleared"] = True

        def fake_builder(proxy_url, verify_ssl):
            captured["builder"] = (proxy_url, verify_ssl)
            return FakeManager()

        return captured, fake_builder

    def test_socks_proxy_uses_urllib3_manager_with_finite_redirect_limit(self):
        captured, fake_builder = self._stub_socks_manager()

        with patch.object(
            source_http,
            "_build_socks_proxy_manager",
            fake_builder,
            create=True,
        ):
            response = _request(
                proxy_url="socks5h://127.0.0.1:1080",
                verify_ssl=False,
                max_redirects=5,
            )

        self.assertEqual(
            captured["builder"],
            ("socks5h://127.0.0.1:1080", False),
        )
        self.assertEqual(
            captured["request"],
            ("GET", "https://example.com/feed.xml"),
        )
        self.assertEqual(captured["request_kwargs"]["headers"], {"Accept": "application/xml"})
        self.assertEqual(captured["request_kwargs"]["timeout"], 17)
        self.assertIs(captured["request_kwargs"]["redirect"], False)
        self.assertIs(captured["request_kwargs"]["preload_content"], False)
        self.assertEqual(response.body, b"socks body")
        self.assertEqual(response.status, 206)
        self.assertEqual(response.final_url, "https://example.com/feed.xml")
        self.assertTrue(captured["response_closed"])
        self.assertTrue(captured["response_released"])
        self.assertTrue(captured["manager_cleared"])

    def test_finite_socks_read_streams_only_limit_plus_one_byte(self):
        captured, fake_builder = self._stub_socks_manager(
            chunks=[b"abc", b"def", b"unread", b"also unread"]
        )

        with patch.object(
            source_http,
            "_build_socks_proxy_manager",
            fake_builder,
            create=True,
        ):
            response = _request(
                proxy_url="socks5://127.0.0.1:1080",
                max_bytes=5,
            )

        self.assertEqual(captured["stream_chunk_size"], 6)
        self.assertEqual(captured["streamed_chunks"], 2)
        self.assertEqual(captured["streamed_bytes"], 6)
        self.assertEqual(captured.get("content_reads", 0), 0)
        self.assertTrue(captured["response_released"])
        self.assertEqual(response.body, b"abcde")
        self.assertIs(response.truncated, True)

    def test_socks_http_status_error_omits_userinfo_and_query_values(self):
        _captured, fake_builder = self._stub_socks_manager(status=401)
        source_url = (
            "https://source-user:source-password@example.com/feed.xml"
            "?token=query-secret"
        )

        with patch.object(
            source_http,
            "_build_socks_proxy_manager",
            fake_builder,
            create=True,
        ):
            with self.assertRaises(HTTPError) as raised:
                _request(
                    url=source_url,
                    proxy_url="socks4://127.0.0.1:1080",
                )

        rendered = f"{raised.exception} {raised.exception.geturl()}"
        self.assertNotIn("source-user", rendered)
        self.assertNotIn("source-password", rendered)
        self.assertNotIn("query-secret", rendered)
        self.assertNotIn("token=", rendered)

    def test_real_socks4_dependency_connects_to_local_endpoint(self):
        http_server = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0),
            _LocalHttpHandler,
        )
        http_server.requests = []
        socks_server = _ThreadingServer(("127.0.0.1", 0), _Socks4Endpoint)
        socks_server.handshakes = []

        with _running_server(http_server), _running_server(socks_server):
            response = _request(
                url=(
                    f"http://127.0.0.1:{http_server.server_address[1]}/feed.xml"
                    "?token=local-only"
                ),
                proxy_url=(
                    f"socks4://127.0.0.1:{socks_server.server_address[1]}"
                ),
            )

        self.assertEqual(response.body, b"socks4 integration response")
        self.assertEqual(response.status, 200)
        self.assertEqual(
            socks_server.handshakes,
            [
                {
                    "version": 4,
                    "command": 1,
                    "host": "127.0.0.1",
                    "port": http_server.server_address[1],
                }
            ],
        )
        self.assertEqual(
            http_server.requests[0]["path"],
            "/feed.xml?token=local-only",
        )


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
        handler = source_http._SafeRedirectHandler(max_redirects)
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
        secret_redirect = (
            "https://redirect-user:redirect-password@example.com/final"
            "?token=secret-value"
        )

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
        self.assertNotIn("redirect-user", str(raised.exception))
        self.assertNotIn("redirect-password", str(raised.exception))

    def test_unlimited_urllib_redirects_use_default_handler_behavior(self):
        captured, _response, fake_build_opener = _capture_urllib()

        with patch.object(source_http, "build_opener", fake_build_opener):
            _request(max_redirects=None)

        redirect_handlers = [
            handler
            for handler in captured["handlers"]
            if isinstance(handler, HTTPRedirectHandler)
        ]
        self.assertEqual(len(redirect_handlers), 1)
        self.assertIsNone(redirect_handlers[0]._max_redirects)

    def test_same_origin_urllib_redirect_preserves_sensitive_headers(self):
        for max_redirects in (None, 5):
            with self.subTest(max_redirects=max_redirects):
                handler = source_http._SafeRedirectHandler(max_redirects)
                request = Request(
                    "https://example.com/start",
                    headers={
                        "Authorization": "Bearer source-secret",
                        "Cookie": "session=cookie-secret",
                        "Proxy-Authorization": "Basic proxy-secret",
                    },
                )

                redirected = handler.redirect_request(
                    request,
                    None,
                    302,
                    "Found",
                    {},
                    "https://EXAMPLE.com:443/next?token=redirect-secret",
                )

                headers = {
                    name.lower(): value
                    for name, value in redirected.header_items()
                }
                self.assertEqual(headers["authorization"], "Bearer source-secret")
                self.assertEqual(headers["cookie"], "session=cookie-secret")
                self.assertEqual(
                    headers["proxy-authorization"],
                    "Basic proxy-secret",
                )

    def test_cross_origin_urllib_redirect_removes_sensitive_headers(self):
        redirect_targets = (
            "http://example.com/changed-scheme",
            "https://other.example/changed-host",
            "https://example.com:444/changed-port",
        )

        for max_redirects in (None, 5):
            for target in redirect_targets:
                with self.subTest(
                    max_redirects=max_redirects,
                    target=target,
                ):
                    handler = source_http._SafeRedirectHandler(max_redirects)
                    request = Request(
                        "https://example.com/start",
                        headers={
                            "Authorization": "Bearer source-secret",
                            "Cookie": "session=cookie-secret",
                            "Proxy-Authorization": "Basic proxy-secret",
                            "X-Preserved": "yes",
                        },
                    )

                    redirected = handler.redirect_request(
                        request,
                        None,
                        302,
                        "Found",
                        {},
                        target,
                    )

                    headers = {
                        name.lower(): value
                        for name, value in redirected.header_items()
                    }
                    self.assertNotIn("authorization", headers)
                    self.assertNotIn("cookie", headers)
                    self.assertNotIn("proxy-authorization", headers)
                    self.assertEqual(headers["x-preserved"], "yes")

    def test_cross_origin_socks_redirect_removes_sensitive_headers(self):
        captured = []

        class FakeResponse:
            def __init__(self, status, location=None):
                self.status = status
                self.headers = {"Location": location} if location else {}
                self._location = location

            def get_redirect_location(self):
                return self._location

            def read(self):
                return b"redirected body"

            def release_conn(self):
                return None

            def close(self):
                return None

        class FakeManager:
            def __init__(self):
                self.responses = [
                    FakeResponse(
                        302,
                        "https://other.example/feed?token=redirect-secret",
                    ),
                    FakeResponse(200),
                ]

            def request(self, method, url, **kwargs):
                captured.append((method, url, dict(kwargs["headers"])))
                return self.responses.pop(0)

            def clear(self):
                return None

        with patch.object(
            source_http,
            "_build_socks_proxy_manager",
            lambda _proxy_url, _verify_ssl: FakeManager(),
            create=True,
        ):
            response = _request(
                headers={
                    "Authorization": "Bearer source-secret",
                    "Cookie": "session=cookie-secret",
                    "Proxy-Authorization": "Basic proxy-secret",
                    "X-Preserved": "yes",
                },
                proxy_url="socks4://127.0.0.1:1080",
                max_redirects=5,
            )

        self.assertEqual(response.final_url, "https://other.example/feed?token=redirect-secret")
        first_headers = {name.lower(): value for name, value in captured[0][2].items()}
        second_headers = {name.lower(): value for name, value in captured[1][2].items()}
        self.assertIn("authorization", first_headers)
        self.assertIn("cookie", first_headers)
        self.assertIn("proxy-authorization", first_headers)
        self.assertNotIn("authorization", second_headers)
        self.assertNotIn("cookie", second_headers)
        self.assertNotIn("proxy-authorization", second_headers)
        self.assertEqual(second_headers["x-preserved"], "yes")

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

    def test_requirements_declares_urllib3_socks_dependency(self):
        requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")

        self.assertIn("Pillow", requirements.splitlines())
        self.assertIn("urllib3[socks]>=2.7,<3", requirements.splitlines())
        self.assertFalse(
            any(line.startswith("httpx") for line in requirements.splitlines())
        )


if __name__ == "__main__":
    unittest.main()
