import asyncio
import sys
import types
import unittest
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


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


if __name__ == "__main__":
    unittest.main()
