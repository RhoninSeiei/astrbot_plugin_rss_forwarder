import inspect
import ssl
import sys
import types
import unittest
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from unittest import mock
from urllib.request import HTTPSHandler


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
PACKAGE_NAME = "astrbot_twitter_source_testpkg"
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


source_http_module = _load_module("source_http")
twitter_module = _load_module("twitter_source")
TwitterTimelineFetcher = twitter_module.TwitterTimelineFetcher


class _FakeCacheTarget:
    def __init__(self, name="cached.jpg"):
        self.name = name
        self.data = b""

    def with_name(self, name):
        return _FakeCacheTarget(name)

    def write_bytes(self, data):
        self.data = data

    def replace(self, target):
        target.data = self.data

    def __str__(self):
        return f"cache/{self.name}"


class _FakeCacheDir:
    def mkdir(self, **kwargs):
        pass

    def glob(self, pattern):
        return []

    def __truediv__(self, name):
        return _FakeCacheTarget(name)


class TwitterTimelineFetcherTests(unittest.IsolatedAsyncioTestCase):
    def test_extract_timeline_ids_dedupes_status_links(self):
        html = """
        <div class="timeline-item"><a class="tweet-link" href="/alice/status/200"></a></div>
        <div class="timeline-item"><a class="tweet-link" href="/alice/status/199"></a></div>
        <div class="timeline-item"><a class="tweet-link" href="/alice/status/200"></a></div>
        """

        ids = TwitterTimelineFetcher()._extract_timeline_ids(html, "alice")

        self.assertEqual(ids, ["200", "199"])

    def test_parse_tweet_detail_respects_media_switches(self):
        class Feed:
            id = "tw-1"
            send_images = False
            send_videos = True
            send_link = False

        html = """
        <div class="main-tweet">
          <a class="fullname">Alice</a>
          <div class="tweet-content media-body">hello <b>world</b></div>
          <a class="still-image"><img src="/pic/a.jpg"/></a>
          <div class="attachment"><video><source src="/video/a.mp4"/></video></div>
        </div>
        """

        item = TwitterTimelineFetcher()._parse_tweet_detail(
            Feed(),
            "https://nitter.example.com",
            "alice",
            "200",
            html,
        )

        self.assertEqual(item["text"], "hello world")
        self.assertEqual(item["images"], [])
        self.assertEqual(item["all_images"], ["https://nitter.example.com/pic/a.jpg"])
        self.assertEqual(item["videos"], ["https://nitter.example.com/video/a.mp4"])
        self.assertEqual(item["link"], "https://x.com/alice/status/200")
        self.assertIs(item["send_link"], False)

    async def test_fetch_keeps_since_id_at_last_success_when_detail_fails(self):
        class Feed:
            id = "tw-1"
            username = "alice"
            nitter_url = "https://nitter.example.com"
            url = ""
            proxy_url = ""
            timeout = 10
            send_images = True
            send_videos = True
            send_link = True
            max_new_items = 0

        fetcher = TwitterTimelineFetcher()

        def fake_open_text(url, proxy_url, timeout, verify_ssl):
            if url.endswith("/alice"):
                return """
                <div class="timeline-item"><a class="tweet-link" href="/alice/status/300"></a></div>
                <div class="timeline-item"><a class="tweet-link" href="/alice/status/200"></a></div>
                """
            if url.endswith("/status/200"):
                return """
                <div class="main-tweet">
                  <a class="fullname">Alice</a>
                  <div class="tweet-content media-body">old item</div>
                </div>
                """
            raise RuntimeError("temporary failure")

        original_open_text = TwitterTimelineFetcher._open_text
        TwitterTimelineFetcher._open_text = staticmethod(fake_open_text)
        try:
            result = await fetcher.fetch(Feed(), {"since_id": "100"})
        finally:
            TwitterTimelineFetcher._open_text = original_open_text

        self.assertIsNotNone(result)
        self.assertEqual(result.since_id, "200")
        self.assertEqual([item["tweet_id"] for item in result.items], ["200"])

    async def test_fetch_limits_detail_requests_to_latest_new_tweet(self):
        class Feed:
            id = "tw-1"
            username = "alice"
            nitter_url = "https://nitter.example.com"
            url = ""
            proxy_url = ""
            timeout = 10
            send_images = True
            send_videos = True
            send_link = True
            max_new_items = 1

        opened_urls = []
        fetcher = TwitterTimelineFetcher()

        def fake_open_text(url, proxy_url, timeout, verify_ssl):
            opened_urls.append(url)
            if url.endswith("/alice"):
                return """
                <div class="timeline-item"><a class="tweet-link" href="/alice/status/300"></a></div>
                <div class="timeline-item"><a class="tweet-link" href="/alice/status/200"></a></div>
                <div class="timeline-item"><a class="tweet-link" href="/alice/status/100"></a></div>
                """
            return """
            <div class="main-tweet">
              <a class="fullname">Alice</a>
              <div class="tweet-content media-body">limited item</div>
            </div>
            """

        original_open_text = TwitterTimelineFetcher._open_text
        TwitterTimelineFetcher._open_text = staticmethod(fake_open_text)
        try:
            result = await fetcher.fetch(Feed(), {"since_id": "50"})
        finally:
            TwitterTimelineFetcher._open_text = original_open_text

        self.assertIsNotNone(result)
        self.assertEqual(result.since_id, "300")
        self.assertEqual([item["tweet_id"] for item in result.items], ["300"])
        self.assertEqual(
            [url for url in opened_urls if "/status/" in url],
            ["https://nitter.example.com/alice/status/300"],
        )

    async def test_fetch_passes_relaxed_tls_to_timeline_and_detail(self):
        class Feed:
            id = "tw-relaxed"
            username = "alice"
            nitter_url = "https://nitter.example.com"
            url = ""
            proxy_url = "socks5://127.0.0.1:1080"
            timeout = 14
            verify_ssl = False
            send_images = True
            send_videos = True
            send_link = True
            max_new_items = 1

        calls = []

        def fake_open_text(url, proxy_url, timeout, verify_ssl):
            calls.append((url, proxy_url, timeout, verify_ssl))
            if url.endswith("/alice"):
                return '<a href="/alice/status/200"></a>'
            return """
            <div class="main-tweet">
              <a class="fullname">Alice</a>
              <div class="tweet-content media-body">relaxed item</div>
            </div>
            """

        with mock.patch.object(
            TwitterTimelineFetcher,
            "_open_text",
            staticmethod(fake_open_text),
        ):
            result = await TwitterTimelineFetcher().fetch(
                Feed(),
                {"since_id": "100"},
            )

        self.assertIsNotNone(result)
        self.assertEqual(
            calls,
            [
                (
                    "https://nitter.example.com/alice",
                    "socks5://127.0.0.1:1080",
                    14,
                    False,
                ),
                (
                    "https://nitter.example.com/alice/status/200",
                    "socks5://127.0.0.1:1080",
                    14,
                    False,
                ),
            ],
        )

    def test_open_text_uses_shared_source_transport(self):
        parameter_count = len(inspect.signature(TwitterTimelineFetcher._open_text).parameters)
        if parameter_count != 4:
            self.fail("_open_text must accept verify_ssl as its fourth parameter")

        captured = {}

        def fake_request_source(**kwargs):
            captured.update(kwargs)
            return source_http_module.SourceHttpResponse(
                body="<html>共享请求</html>".encode(),
                status=200,
                headers={},
                final_url=kwargs["url"],
            )

        with mock.patch.object(
            twitter_module,
            "request_source",
            fake_request_source,
            create=True,
        ):
            result = TwitterTimelineFetcher._open_text(
                "https://nitter.example.com/alice",
                "http://127.0.0.1:7890",
                12,
                False,
            )

        self.assertEqual(result, "<html>共享请求</html>")
        self.assertEqual(captured["proxy_url"], "http://127.0.0.1:7890")
        self.assertEqual(captured["timeout"], 12)
        self.assertIs(captured["verify_ssl"], False)
        self.assertIsNone(captured["max_bytes"])
        self.assertIsNone(captured["max_redirects"])
        self.assertIn("text/html", captured["headers"]["Accept"])

    async def test_relaxed_source_tls_is_not_passed_to_media_cache_downloader(self):
        class Feed:
            id = "tw-relaxed"
            username = "alice"
            nitter_url = "https://nitter.example.com"
            url = ""
            proxy_url = "http://127.0.0.1:7890"
            timeout = 13
            verify_ssl = False
            send_images = True
            send_videos = True
            send_link = True
            max_new_items = 1

        media_calls = []

        def fake_open_text(url, proxy_url, timeout, verify_ssl):
            if url.endswith("/alice"):
                return '<a href="/alice/status/200"></a>'
            return """
            <div class="main-tweet">
              <a class="fullname">Alice</a>
              <div class="tweet-content media-body">media item</div>
              <a class="still-image"><img src="/pic/a.jpg"/></a>
            </div>
            """

        def fake_cache_media_url(url, cache_dir, proxy_url, timeout, media_kind):
            media_calls.append((url, cache_dir, proxy_url, timeout, media_kind))
            return None

        fetcher = TwitterTimelineFetcher()
        cache_dir = types.SimpleNamespace(mkdir=lambda **kwargs: None)
        with (
            mock.patch.object(
                TwitterTimelineFetcher,
                "_open_text",
                staticmethod(fake_open_text),
            ),
            mock.patch.object(fetcher, "_cache_media_url", fake_cache_media_url),
            mock.patch.object(fetcher, "_cleanup_media_cache", lambda path: None),
        ):
            result = await fetcher.fetch(
                Feed(),
                {"since_id": "100"},
                cache_dir=cache_dir,
            )

        self.assertIsNotNone(result)
        self.assertEqual(len(media_calls), 1)
        self.assertEqual(media_calls[0][0], "https://nitter.example.com/pic/a.jpg")
        self.assertEqual(
            media_calls[0][2:],
            ("http://127.0.0.1:7890", 13, "image"),
        )

    async def test_urllib_media_downloader_uses_strict_ssl_context_for_relaxed_feed(self):
        class Feed:
            id = "tw-relaxed-urllib"
            username = "alice"
            nitter_url = "https://nitter.example.com"
            url = ""
            proxy_url = "http://127.0.0.1:7890"
            timeout = 13
            verify_ssl = False
            send_images = True
            send_videos = True
            send_link = True
            max_new_items = 1

        captured = {"source_verify_ssl": []}

        def fake_open_text(url, proxy_url, timeout, verify_ssl):
            captured["source_verify_ssl"].append(verify_ssl)
            if url.endswith("/alice"):
                return '<a href="/alice/status/200"></a>'
            return """
            <div class="main-tweet">
              <a class="fullname">Alice</a>
              <div class="tweet-content media-body">media item</div>
              <a class="still-image"><img src="/pic/a.jpg"/></a>
            </div>
            """

        class FakeResponse:
            headers = {"Content-Type": "image/jpeg", "Content-Length": "3"}

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self, size=-1):
                return b"img"

        class FakeOpener:
            def open(self, request, timeout):
                captured["media_request"] = request
                captured["media_timeout"] = timeout
                return FakeResponse()

        def fake_build_opener(*handlers):
            captured["handlers"] = handlers
            return FakeOpener()

        fetcher = TwitterTimelineFetcher()
        with (
            mock.patch.object(
                TwitterTimelineFetcher,
                "_open_text",
                staticmethod(fake_open_text),
            ),
            mock.patch.object(fetcher, "_cleanup_media_cache", lambda path: None),
            mock.patch.object(twitter_module, "build_opener", fake_build_opener),
        ):
            result = await fetcher.fetch(
                Feed(),
                {"since_id": "100"},
                cache_dir=_FakeCacheDir(),
            )

        self.assertIsNotNone(result)
        self.assertEqual(captured["source_verify_ssl"], [False, False])
        https_handlers = [
            handler
            for handler in captured["handlers"]
            if isinstance(handler, HTTPSHandler)
        ]
        self.assertEqual(len(https_handlers), 1)
        self.assertEqual(
            https_handlers[0]._context.verify_mode,
            ssl.CERT_REQUIRED,
        )

    async def test_httpx_media_downloader_keeps_strict_default_for_relaxed_feed(self):
        class Feed:
            id = "tw-relaxed-httpx"
            username = "alice"
            nitter_url = "https://nitter.example.com"
            url = ""
            proxy_url = "socks5://127.0.0.1:1080"
            timeout = 13
            verify_ssl = False
            send_images = True
            send_videos = True
            send_link = True
            max_new_items = 1

        captured = {"source_verify_ssl": []}

        def fake_open_text(url, proxy_url, timeout, verify_ssl):
            captured["source_verify_ssl"].append(verify_ssl)
            if url.endswith("/alice"):
                return '<a href="/alice/status/200"></a>'
            return """
            <div class="main-tweet">
              <a class="fullname">Alice</a>
              <div class="tweet-content media-body">media item</div>
              <a class="still-image"><img src="/pic/a.jpg"/></a>
            </div>
            """

        class FakeResponse:
            headers = {"Content-Type": "image/jpeg", "Content-Length": "3"}

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def raise_for_status(self):
                pass

            def iter_bytes(self):
                yield b"img"

        class FakeClient:
            def __init__(self, **kwargs):
                captured["client_kwargs"] = kwargs

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def stream(self, method, url):
                captured["stream"] = (method, url)
                return FakeResponse()

        fetcher = TwitterTimelineFetcher()
        with (
            mock.patch.object(
                TwitterTimelineFetcher,
                "_open_text",
                staticmethod(fake_open_text),
            ),
            mock.patch.object(fetcher, "_cleanup_media_cache", lambda path: None),
            mock.patch.dict(
                sys.modules,
                {"httpx": types.SimpleNamespace(Client=FakeClient)},
            ),
        ):
            result = await fetcher.fetch(
                Feed(),
                {"since_id": "100"},
                cache_dir=_FakeCacheDir(),
            )

        self.assertIsNotNone(result)
        self.assertEqual(captured["source_verify_ssl"], [False, False])
        self.assertIs(captured["client_kwargs"].get("verify", True), True)
        self.assertNotIn("verify_ssl", captured["client_kwargs"])
        self.assertEqual(
            captured["stream"],
            ("GET", "https://nitter.example.com/pic/a.jpg"),
        )


if __name__ == "__main__":
    unittest.main()
