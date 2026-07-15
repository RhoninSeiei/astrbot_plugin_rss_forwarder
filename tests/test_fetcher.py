import asyncio
import sys
import types
import unittest
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

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
PACKAGE_NAME = "astrbot_rss_fetcher_testpkg"
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


_load_module("config")
fetcher_module = _load_module("fetcher")
FeedFetcher = fetcher_module.FeedFetcher


class _FakeStorage:
    async def get_feed_state(self, feed_id):
        return {}

    def plugin_cache_dir(self):
        return Path("/tmp/astrbot-rss-fetcher-test")


class _FakeResponse:
    status = 200
    headers = {"ETag": "etag-a", "Last-Modified": "Tue, 12 May 2026 00:00:00 GMT"}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return b"<rss><channel></channel></rss>"


class FeedFetcherTests(unittest.TestCase):
    def test_rss_fetch_metadata_includes_media_flags_and_max_items(self):
        async def fake_fetch_single(feed, **_kwargs):
            return fetcher_module.FetchedFeed(
                feed_id=feed.id,
                body="<rss><channel></channel></rss>",
                etag="etag-a",
                last_modified="Tue, 12 May 2026 00:00:00 GMT",
                status=200,
            )

        feed = types.SimpleNamespace(
            id="rss-1",
            source_type="rss",
            enabled=True,
            max_new_items=2,
            send_images=False,
            send_videos=False,
            proxy_url="http://127.0.0.1:7890",
        )
        fetcher = FeedFetcher(types.SimpleNamespace(feeds=[feed]), _FakeStorage())
        fetcher._fetch_single_feed = fake_fetch_single

        result = asyncio.run(fetcher.fetch_feed_ids(["rss-1"]))

        self.assertEqual(result[0]["max_new_items"], 2)
        self.assertIs(result[0]["send_images"], False)
        self.assertIs(result[0]["send_videos"], False)
        self.assertEqual(result[0]["proxy_url"], "http://127.0.0.1:7890")

    def test_rss_fetch_uses_configured_http_proxy_and_browser_headers(self):
        captured = {}

        def fake_proxy_handler(mapping):
            captured["proxy_mapping"] = mapping
            return ("proxy", mapping)

        class FakeOpener:
            def open(self, request, timeout):
                captured["timeout"] = timeout
                captured["headers"] = dict(request.header_items())
                return _FakeResponse()

        def fake_build_opener(*args):
            captured["opener_args"] = args
            return FakeOpener()

        original_proxy_handler = fetcher_module.ProxyHandler
        original_build_opener = fetcher_module.build_opener
        fetcher_module.ProxyHandler = fake_proxy_handler
        fetcher_module.build_opener = fake_build_opener
        try:
            feed = types.SimpleNamespace(
                id="rss-1",
                url="https://example.com/feed.xml",
                auth_mode="none",
                key="",
                timeout=17,
                proxy_url="http://172.20.0.1:7890",
            )
            fetcher = FeedFetcher(types.SimpleNamespace(feeds=[]), _FakeStorage())

            result = asyncio.run(fetcher._fetch_single_feed(feed))
        finally:
            fetcher_module.ProxyHandler = original_proxy_handler
            fetcher_module.build_opener = original_build_opener

        self.assertIsNotNone(result)
        self.assertEqual(captured["proxy_mapping"]["http"], "http://172.20.0.1:7890")
        self.assertEqual(captured["proxy_mapping"]["https"], "http://172.20.0.1:7890")
        self.assertEqual(captured["timeout"], 17)
        self.assertIn("Mozilla/5.0", captured["headers"]["User-agent"])
        self.assertIn("application/rss+xml", captured["headers"]["Accept"])
        self.assertIn("en-US", captured["headers"]["Accept-language"])

    def test_rss_fetch_failure_log_includes_job_feed_url_and_proxy_state(self):
        captured = {}

        class FakeOpener:
            def open(self, request, timeout):
                raise OSError("certificate verify failed")

        def fake_build_opener(*args):
            return FakeOpener()

        class FakeLogger:
            def info(self, *args, **kwargs):
                pass

            def warning(self, *args, **kwargs):
                captured["warning"] = (args, kwargs)

        original_build_opener = fetcher_module.build_opener
        original_logger = fetcher_module.logger
        fetcher_module.build_opener = fake_build_opener
        fetcher_module.logger = FakeLogger()
        try:
            feed = types.SimpleNamespace(
                id="rss-1",
                source_type="rss",
                enabled=True,
                url="https://example.com/feed.xml?key=secret-token",
                auth_mode="none",
                key="",
                timeout=17,
                proxy_url="",
            )
            fetcher = FeedFetcher(types.SimpleNamespace(feeds=[feed]), _FakeStorage())

            result = asyncio.run(
                fetcher.fetch(types.SimpleNamespace(id="job-1", feed_ids=["rss-1"]))
            )
        finally:
            fetcher_module.build_opener = original_build_opener
            fetcher_module.logger = original_logger

        self.assertEqual(result, [])
        args, _kwargs = captured["warning"]
        self.assertIn("job=%s", args[0])
        self.assertIn("feed=%s", args[0])
        self.assertIn("url=%s", args[0])
        self.assertIn("proxy=%s", args[0])
        self.assertEqual(args[1], "job-1")
        self.assertEqual(args[2], "rss-1")
        self.assertEqual(args[3], "https://example.com/feed.xml?<redacted>")

    def test_rss_fetch_failure_log_redacts_http_status_exception_details(self):
        captured = {}
        request = httpx.Request(
            "GET",
            "https://user:password@example.com/feed.xml?key=secret-token&x=1",
            headers={
                "Authorization": "Bearer header-secret",
                "Cookie": "session=cookie-secret",
            },
        )
        response = httpx.Response(401, request=request)
        error = httpx.HTTPStatusError(
            "401 Unauthorized for request URL "
            "https://user:password@example.com/feed.xml?key=secret-token&x=1\n"
            "Authorization: Bearer header-secret\nCookie: session=cookie-secret",
            request=request,
            response=response,
        )

        class FakeOpener:
            def open(self, request, timeout):
                raise error

        def fake_build_opener(*args):
            return FakeOpener()

        class FakeLogger:
            def info(self, *args, **kwargs):
                pass

            def warning(self, *args, **kwargs):
                captured["warning"] = (args, kwargs)

        original_build_opener = fetcher_module.build_opener
        original_logger = fetcher_module.logger
        fetcher_module.build_opener = fake_build_opener
        fetcher_module.logger = FakeLogger()
        try:
            feed = types.SimpleNamespace(
                id="rss-1",
                source_type="rss",
                enabled=True,
                url="https://example.com/feed.xml",
                auth_mode="none",
                key="",
                timeout=17,
                proxy_url="",
            )
            fetcher = FeedFetcher(types.SimpleNamespace(feeds=[feed]), _FakeStorage())

            result = asyncio.run(fetcher.fetch_feed_ids(["rss-1"], job_id="job-1"))
        finally:
            fetcher_module.build_opener = original_build_opener
            fetcher_module.logger = original_logger

        self.assertEqual(result, [])
        args, _kwargs = captured["warning"]
        rendered = args[0] % args[1:]
        self.assertIn("HTTPStatusError", rendered)
        self.assertIn("401", rendered)
        self.assertNotIn("secret-token", rendered)
        self.assertNotIn("key=", rendered)
        self.assertNotIn("x=1", rendered)
        self.assertNotIn("Authorization", rendered)
        self.assertNotIn("Cookie", rendered)
        self.assertNotIn("user:password", rendered)
        self.assertNotIn("header-secret", rendered)
        self.assertNotIn("cookie-secret", rendered)
        self.assertNotIn("\n", rendered)
        self.assertLessEqual(len(args[-1]), 240)


if __name__ == "__main__":
    unittest.main()
