import asyncio
import sys
import types
import unittest
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from unittest import mock

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
source_http_module = _load_module("source_http")
fetcher_module = _load_module("fetcher")
FeedFetcher = fetcher_module.FeedFetcher


class _FakeStorage:
    def __init__(self, states=None):
        self._states = states or {}

    async def get_feed_state(self, feed_id):
        return dict(self._states.get(feed_id, {}))

    def plugin_cache_dir(self):
        return Path("/tmp/astrbot-rss-fetcher-test")


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

    def test_rss_fetch_uses_shared_transport_with_tls_proxy_conditionals_and_metadata(self):
        captured = {}

        def fake_request_source(**kwargs):
            captured["request_source"] = kwargs
            return source_http_module.SourceHttpResponse(
                body=b"<rss><channel><title>shared</title></channel></rss>",
                status=206,
                headers={
                    "ETag": "etag-shared",
                    "Last-Modified": "Wed, 13 May 2026 00:00:00 GMT",
                },
                final_url=kwargs["url"],
            )

        with mock.patch.object(
            fetcher_module,
            "request_source",
            fake_request_source,
            create=True,
        ):
            feed = types.SimpleNamespace(
                id="rss-1",
                url="https://example.com/feed.xml",
                auth_mode="none",
                key="",
                timeout=17,
                proxy_url="http://172.20.0.1:7890",
                verify_ssl=False,
            )
            fetcher = FeedFetcher(
                types.SimpleNamespace(feeds=[]),
                _FakeStorage(
                    {
                        "rss-1": {
                            "etag": "etag-before",
                            "last_modified": "Tue, 12 May 2026 00:00:00 GMT",
                        }
                    }
                ),
            )

            result = asyncio.run(fetcher._fetch_single_feed(feed))

        self.assertIsNotNone(result)
        self.assertIn("request_source", captured)
        request_args = captured["request_source"]
        self.assertEqual(request_args["proxy_url"], "http://172.20.0.1:7890")
        self.assertEqual(request_args["timeout"], 17)
        self.assertIs(request_args["verify_ssl"], False)
        self.assertIsNone(request_args["max_bytes"])
        self.assertIsNone(request_args["max_redirects"])
        self.assertIn("Mozilla/5.0", request_args["headers"]["User-Agent"])
        self.assertIn("application/rss+xml", request_args["headers"]["Accept"])
        self.assertIn("en-US", request_args["headers"]["Accept-Language"])
        self.assertEqual(request_args["headers"]["If-None-Match"], "etag-before")
        self.assertEqual(
            request_args["headers"]["If-Modified-Since"],
            "Tue, 12 May 2026 00:00:00 GMT",
        )
        self.assertEqual(result.body, "<rss><channel><title>shared</title></channel></rss>")
        self.assertEqual(result.etag, "etag-shared")
        self.assertEqual(result.last_modified, "Wed, 13 May 2026 00:00:00 GMT")
        self.assertEqual(result.status, 206)

    def test_rss_fetch_defaults_legacy_feed_to_strict_tls(self):
        captured = {}

        def fake_request_source(**kwargs):
            captured.update(kwargs)
            return source_http_module.SourceHttpResponse(
                body=b"<rss/>",
                status=200,
                headers={},
                final_url=kwargs["url"],
            )

        with mock.patch.object(
            fetcher_module,
            "request_source",
            fake_request_source,
            create=True,
        ):
            feed = types.SimpleNamespace(
                id="legacy-rss",
                url="https://example.com/feed.xml",
                auth_mode="none",
                key="",
                timeout=10,
                proxy_url="",
            )
            result = asyncio.run(
                FeedFetcher(
                    types.SimpleNamespace(feeds=[]),
                    _FakeStorage(),
                )._fetch_single_feed(feed)
            )

        self.assertIsNotNone(result)
        self.assertIn("verify_ssl", captured)
        self.assertIs(captured["verify_ssl"], True)

    def test_relaxed_tls_warning_is_once_per_rss_feed_and_contains_only_feed_id(self):
        warnings = []

        class FakeLogger:
            def info(self, *args, **kwargs):
                pass

            def warning(self, *args, **kwargs):
                warnings.append(args[0] % args[1:])

        feed = types.SimpleNamespace(
            id="rss-relaxed",
            source_type="rss",
            enabled=True,
            verify_ssl=False,
            url="https://user:secret@example.com/feed.xml?key=query-secret",
            username="source-user",
            proxy_url="http://proxy-user:proxy-secret@127.0.0.1:7890",
        )
        fetcher = FeedFetcher(types.SimpleNamespace(feeds=[feed]), _FakeStorage())

        async def fake_fetch_single(_feed, **_kwargs):
            return None

        fetcher._fetch_single_feed = fake_fetch_single
        with mock.patch.object(fetcher_module, "logger", FakeLogger()):
            asyncio.run(fetcher.fetch_feed_ids([feed.id]))
            asyncio.run(fetcher.fetch_feed_ids([feed.id]))

        self.assertEqual(
            warnings,
            ["feed=rss-relaxed source TLS certificate verification is disabled"],
        )
        rendered = "\n".join(warnings)
        for secret in (
            "query-secret",
            "source-user",
            "proxy-user",
            "proxy-secret",
            "example.com",
        ):
            self.assertNotIn(secret, rendered)

    def test_relaxed_tls_warning_is_once_per_twitter_feed(self):
        warnings = []

        class FakeLogger:
            def info(self, *args, **kwargs):
                pass

            def warning(self, *args, **kwargs):
                warnings.append(args[0] % args[1:])

        feed = types.SimpleNamespace(
            id="twitter-relaxed",
            source_type="twitter",
            enabled=True,
            verify_ssl=False,
            username="private-user",
            nitter_url="https://nitter.example.com/private-user",
            proxy_url="http://proxy-user:proxy-secret@127.0.0.1:7890",
        )
        fetcher = FeedFetcher(types.SimpleNamespace(feeds=[feed]), _FakeStorage())

        async def fake_fetch_twitter(_feed):
            return None

        fetcher._fetch_single_twitter_feed = fake_fetch_twitter
        with mock.patch.object(fetcher_module, "logger", FakeLogger()):
            asyncio.run(fetcher.fetch_feed_ids([feed.id]))
            asyncio.run(fetcher.fetch_feed_ids([feed.id]))

        self.assertEqual(
            warnings,
            ["feed=twitter-relaxed source TLS certificate verification is disabled"],
        )

    def test_rss_fetch_failure_log_includes_job_feed_url_and_proxy_state(self):
        captured = {}

        class FakeLogger:
            def info(self, *args, **kwargs):
                pass

            def warning(self, *args, **kwargs):
                captured["warning"] = (args, kwargs)

        def fake_request_source(**kwargs):
            raise OSError("certificate verify failed")

        with (
            mock.patch.object(
                fetcher_module,
                "request_source",
                fake_request_source,
            ),
            mock.patch.object(fetcher_module, "logger", FakeLogger()),
        ):
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

        class FakeLogger:
            def info(self, *args, **kwargs):
                pass

            def warning(self, *args, **kwargs):
                captured["warning"] = (args, kwargs)

        def fake_request_source(**kwargs):
            raise error

        with (
            mock.patch.object(
                fetcher_module,
                "request_source",
                fake_request_source,
            ),
            mock.patch.object(fetcher_module, "logger", FakeLogger()),
        ):
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
