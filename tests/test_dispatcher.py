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
PACKAGE_NAME = "astrbot_rss_testpkg_dispatcher"
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
dispatcher_module = _load_module("dispatcher")
RSSConfig = config_module.RSSConfig
FeedDispatcher = dispatcher_module.FeedDispatcher


class _FakeContext:
    def __init__(self, fail_first_send: bool = False):
        self.fail_first_send = fail_first_send
        self.send_calls = 0
        self.sent: list[tuple[str, object]] = []

    async def send_message(self, unified_msg_origin, payload):
        self.send_calls += 1
        if self.fail_first_send and self.send_calls == 1:
            raise RuntimeError("temporary send failure")
        self.sent.append((unified_msg_origin, payload))


class _FakeStorage:
    def __init__(self):
        self.pending: set[str] = set()
        self.sent: set[str] = set()
        self.claims: list[str] = []
        self.confirms: list[str] = []
        self.releases: list[str] = []

    async def claim_dispatch(self, fingerprint: str, ttl_seconds: int = 0) -> bool:
        self.claims.append(fingerprint)
        if fingerprint in self.pending or fingerprint in self.sent:
            return False
        self.pending.add(fingerprint)
        return True

    async def confirm_dispatch(self, fingerprint: str, ttl_seconds: int = 0) -> None:
        self.confirms.append(fingerprint)
        self.pending.discard(fingerprint)
        self.sent.add(fingerprint)

    async def release_dispatch(self, fingerprint: str) -> None:
        self.releases.append(fingerprint)
        self.pending.discard(fingerprint)


class _Plain:
    def __init__(self, text=""):
        self.text = text


class _Image:
    def __init__(self, url=""):
        self.url = url
        self.path = ""

    @classmethod
    def fromURL(cls, url):
        return cls(url=url)

    @classmethod
    def fromFileSystem(cls, path):
        item = cls(url="")
        item.path = path
        return item


class _Video:
    def __init__(self, url=""):
        self.url = url
        self.path = ""

    @classmethod
    def fromURL(cls, url):
        return cls(url=url)

    @classmethod
    def fromFileSystem(cls, path):
        item = cls(url="")
        item.path = path
        return item


class _MessageChain:
    def __init__(self, chain=None):
        self.chain = chain or []


class _StarRenderer:
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.calls: list[tuple[str, dict]] = []

    async def html_render(self, tmpl, data, return_url=True, options=None):
        self.calls.append((tmpl, data))
        if self.fail:
            raise RuntimeError("render unavailable")
        return "digest-image"


class _FakeNetworkStrategy:
    def __init__(self):
        self.refresh_count = 0

    async def get_official_endpoints(self):
        self.refresh_count += 1


class _FakeHtmlRenderer:
    def __init__(self):
        self.network_strategy = _FakeNetworkStrategy()


html_renderer = _FakeHtmlRenderer()


class _RefreshableStarRenderer:
    def __init__(self):
        self.calls = 0

    async def html_render(self, tmpl, data, return_url=True, options=None):
        self.calls += 1
        if html_renderer.network_strategy.refresh_count == 0:
            raise RuntimeError("HTTP 502")
        return "digest-image"


class DispatcherTests(unittest.IsolatedAsyncioTestCase):
    def _build_config(self):
        return RSSConfig.from_context(
            {
                "feeds": [{"id": "feed-1", "url": "https://example.com/rss", "enabled": True}],
                "targets": [
                    {
                        "id": "target-1",
                        "platform": "qq",
                        "unified_msg_origin": "default:GroupMessage:1",
                        "enabled": True,
                    }
                ],
                "jobs": [
                    {
                        "id": "job-1",
                        "feed_ids": ["feed-1"],
                        "target_ids": ["target-1"],
                        "interval_seconds": 300,
                        "enabled": True,
                    }
                ],
                "render_mode": "text",
                "dedup_ttl_seconds": 3600,
            }
        )

    def _build_two_target_config(self):
        return RSSConfig.from_context(
            {
                "feeds": [{"id": "feed-1", "url": "https://example.com/rss", "enabled": True}],
                "targets": [
                    {
                        "id": "target-normal",
                        "platform": "qq",
                        "unified_msg_origin": "default:FriendMessage:562506516",
                        "compact_mode": "normal",
                        "enabled": True,
                    },
                    {
                        "id": "target-compact",
                        "platform": "qq",
                        "unified_msg_origin": "default:GroupMessage:764968756",
                        "compact_mode": "compact",
                        "enabled": True,
                    },
                ],
                "jobs": [
                    {
                        "id": "job-1",
                        "feed_ids": ["feed-1"],
                        "target_ids": ["target-normal", "target-compact"],
                        "interval_seconds": 300,
                        "compact_mode_enabled": False,
                        "enabled": True,
                    }
                ],
                "render_mode": "text",
                "dedup_ttl_seconds": 3600,
            }
        )

    async def test_duplicate_dispatch_is_blocked_before_send(self):
        context = _FakeContext()
        storage = _FakeStorage()
        dispatcher = FeedDispatcher(context=context, config=self._build_config(), storage=storage)
        dispatcher._build_text_message_chain = lambda item: "payload"

        async def fake_hash(_image_url: str) -> str:
            return "image-sha256"

        dispatcher._hash_image_bytes = fake_hash

        item = {
            "job_id": "job-1",
            "guid": "guid-1",
            "title": "Title",
            "summary": "Summary",
            "link": "https://example.com/post/1",
            "published_at": "2026-03-27T00:00:00+00:00",
            "image_url": "https://example.com/a.jpg",
        }

        first = await dispatcher.dispatch(item)
        second = await dispatcher.dispatch(item)

        self.assertEqual(first.success_count, 1)
        self.assertEqual(second.skipped_duplicate_count, 1)
        self.assertEqual(len(context.sent), 1)
        self.assertEqual(len(storage.confirms), 1)

    async def test_duplicate_dispatch_uses_source_fields_when_translation_differs(self):
        context = _FakeContext()
        storage = _FakeStorage()
        dispatcher = FeedDispatcher(context=context, config=self._build_config(), storage=storage)
        dispatcher._build_text_message_chain = lambda item: "payload"

        async def fake_hash(_image_url: str) -> str:
            return "image-sha256"

        dispatcher._hash_image_bytes = fake_hash

        first_item = {
            "job_id": "job-1",
            "guid": "guid-translation",
            "title": "第一版中文标题",
            "summary": "第一版中文摘要",
            "_source_title": "English title",
            "_source_summary": "English summary",
            "link": "https://example.com/post/source",
            "published_at": "2026-03-27T00:00:00+00:00",
            "image_url": "https://example.com/a.jpg",
        }
        second_item = {
            "job_id": "job-1",
            "guid": "guid-translation",
            "title": "第二版中文标题",
            "summary": "第二版中文摘要",
            "_source_title": "English title",
            "_source_summary": "English summary",
            "link": "https://example.com/post/source",
            "published_at": "2026-03-27T00:00:00+00:00",
            "image_url": "https://example.com/a.jpg",
        }

        first = await dispatcher.dispatch(first_item)
        second = await dispatcher.dispatch(second_item)

        self.assertEqual(first.success_count, 1)
        self.assertEqual(second.skipped_duplicate_count, 1)
        self.assertEqual(len(context.sent), 1)

    async def test_failed_send_releases_dispatch_claim_for_retry(self):
        context = _FakeContext(fail_first_send=True)
        storage = _FakeStorage()
        dispatcher = FeedDispatcher(context=context, config=self._build_config(), storage=storage)
        dispatcher._build_text_message_chain = lambda item: "payload"

        item = {
            "job_id": "job-1",
            "guid": "guid-2",
            "title": "Retry",
            "summary": "Retry summary",
            "link": "https://example.com/post/2",
            "published_at": "2026-03-27T00:01:00+00:00",
        }

        first = await dispatcher.dispatch(item)
        context.fail_first_send = False
        second = await dispatcher.dispatch(item)

        self.assertEqual(first.transient_failure_count, 1)
        self.assertEqual(second.success_count, 1)
        self.assertEqual(len(storage.releases), 1)
        self.assertEqual(len(context.sent), 1)

    async def test_daily_digest_dispatch_uses_target_ids_and_blocks_duplicates(self):
        context = _FakeContext()
        storage = _FakeStorage()
        dispatcher = FeedDispatcher(context=context, config=self._build_config(), storage=storage)
        dispatcher._build_daily_digest_text_chain = lambda digest: "digest-payload"

        digest = {
            "id": "digest-1",
            "title": "芯片日报",
            "target_ids": ["target-1"],
            "render_mode": "text",
            "window_start_text": "2026-03-27 09:00",
            "window_end_text": "2026-03-28 09:00",
            "item_count": 2,
            "content": "1. [TechPowerUp] AMD 推出新 CPU",
        }

        first = await dispatcher.dispatch_daily_digest(digest)
        second = await dispatcher.dispatch_daily_digest(digest)

        self.assertEqual(first.success_count, 1)
        self.assertEqual(second.skipped_duplicate_count, 1)
        self.assertEqual(len(context.sent), 1)

    async def test_daily_digest_dispatch_supports_image_render_mode(self):
        context = _FakeContext()
        async def html_render(_html):
            return "digest-image"

        context.html_render = html_render
        storage = _FakeStorage()
        dispatcher = FeedDispatcher(context=context, config=self._build_config(), storage=storage)
        dispatcher._resolve_messagechain_cls = lambda: _MessageChain
        dispatcher._resolve_image_cls = lambda: _Image

        digest = {
            "id": "digest-2",
            "title": "图卡日报",
            "target_ids": ["target-1"],
            "render_mode": "image",
            "window_start_text": "2026-03-27 09:00",
            "window_end_text": "2026-03-28 09:00",
            "item_count": 1,
            "content": "1. [Feed] Title",
        }

        result = await dispatcher.dispatch_daily_digest(digest)

        self.assertEqual(result.success_count, 1)
        payload = context.sent[0][1]
        self.assertIsInstance(payload, _MessageChain)
        self.assertEqual(payload.chain[0].url, "digest-image")

    async def test_daily_digest_image_render_uses_star_html_render_signature(self):
        context = _FakeContext()
        renderer = _StarRenderer()
        storage = _FakeStorage()
        dispatcher = FeedDispatcher(
            context=context,
            config=self._build_config(),
            storage=storage,
            renderer=renderer,
        )
        dispatcher._resolve_messagechain_cls = lambda: _MessageChain
        dispatcher._resolve_image_cls = lambda: _Image

        digest = {
            "id": "digest-star-render",
            "title": "图卡日报",
            "target_ids": ["target-1"],
            "render_mode": "image",
            "window_start_text": "2026-03-27 09:00",
            "window_end_text": "2026-03-28 09:00",
            "item_count": 1,
            "content": "1. [Feed] Title",
        }

        result = await dispatcher.dispatch_daily_digest(digest)

        self.assertEqual(result.success_count, 1)
        payload = context.sent[0][1]
        self.assertIsInstance(payload, _MessageChain)
        self.assertEqual(payload.chain[0].url, "digest-image")
        self.assertEqual(renderer.calls[0][1], {})

    async def test_html_render_refreshes_t2i_endpoints_after_first_failure(self):
        html_renderer.network_strategy.refresh_count = 0
        context = _FakeContext()
        renderer = _RefreshableStarRenderer()
        storage = _FakeStorage()
        dispatcher = FeedDispatcher(
            context=context,
            config=self._build_config(),
            storage=storage,
            renderer=renderer,
        )

        result = await dispatcher.html_render("<html><body>probe</body></html>")

        self.assertEqual(result, "digest-image")
        self.assertEqual(renderer.calls, 2)
        self.assertEqual(html_renderer.network_strategy.refresh_count, 1)

    async def test_image_render_active_send_wraps_rendered_url_as_image_chain(self):
        context = _FakeContext()
        async def html_render(_html):
            return "rendered-card-url"

        context.html_render = html_render
        storage = _FakeStorage()
        config = RSSConfig.from_context(
            {
                "feeds": [{"id": "feed-1", "url": "https://example.com/rss", "enabled": True}],
                "targets": [
                    {
                        "id": "target-1",
                        "platform": "qq",
                        "unified_msg_origin": "default:GroupMessage:1",
                        "enabled": True,
                    }
                ],
                "jobs": [
                    {
                        "id": "job-1",
                        "feed_ids": ["feed-1"],
                        "target_ids": ["target-1"],
                        "interval_seconds": 300,
                        "enabled": True,
                    }
                ],
                "render_mode": "image",
            }
        )
        dispatcher = FeedDispatcher(context=context, config=config, storage=storage)
        dispatcher._resolve_messagechain_cls = lambda: _MessageChain
        dispatcher._resolve_image_cls = lambda: _Image

        item = {
            "job_id": "job-1",
            "guid": "guid-image",
            "title": "Title",
            "summary": "Summary",
            "link": "https://example.com/post",
            "published_at": "2026-03-27T00:00:00+00:00",
        }

        result = await dispatcher.dispatch(item)

        self.assertEqual(result.success_count, 1)
        payload = context.sent[0][1]
        self.assertIsInstance(payload, _MessageChain)
        self.assertEqual(payload.chain[0].url, "rendered-card-url")

    async def test_daily_digest_image_render_falls_back_to_text_when_render_fails(self):
        context = _FakeContext()
        renderer = _StarRenderer(fail=True)
        storage = _FakeStorage()
        dispatcher = FeedDispatcher(
            context=context,
            config=self._build_config(),
            storage=storage,
            renderer=renderer,
        )
        dispatcher._resolve_messagechain_cls = lambda: _MessageChain
        dispatcher._resolve_plain_cls = lambda: _Plain

        digest = {
            "id": "digest-image-fallback",
            "title": "图卡日报",
            "target_ids": ["target-1"],
            "render_mode": "image",
            "window_start_text": "2026-03-27 09:00",
            "window_end_text": "2026-03-28 09:00",
            "item_count": 1,
            "content": "1. [Feed] Title",
        }

        result = await dispatcher.dispatch_daily_digest(digest)

        self.assertEqual(result.success_count, 1)
        payload = context.sent[0][1]
        self.assertIsInstance(payload, _MessageChain)
        self.assertIn("图卡日报", payload.chain[0].text)
        self.assertIn("1. [Feed] Title", payload.chain[0].text)

    async def test_twitter_text_dispatch_includes_multiple_images_and_videos(self):
        context = _FakeContext()
        storage = _FakeStorage()
        dispatcher = FeedDispatcher(context=context, config=self._build_config(), storage=storage)
        dispatcher._resolve_messagechain_cls = lambda: _MessageChain
        dispatcher._resolve_plain_cls = lambda: _Plain
        dispatcher._resolve_image_cls = lambda: _Image
        dispatcher._resolve_video_cls = lambda: _Video

        item = {
            "job_id": "job-1",
            "source_type": "twitter",
            "guid": "twitter:alice:1",
            "title": "@alice",
            "summary": "hello",
            "link": "https://x.com/alice/status/1",
            "image_urls": [
                "https://nitter.example.com/pic/a.jpg",
                "https://nitter.example.com/pic/b.jpg",
            ],
            "video_urls": ["https://nitter.example.com/video/a.mp4"],
            "image_paths": ["/tmp/a.jpg"],
            "video_paths": ["/tmp/a.mp4"],
        }

        result = await dispatcher.dispatch(item)

        self.assertEqual(result.success_count, 1)
        payload = context.sent[0][1]
        self.assertIsInstance(payload, _MessageChain)
        self.assertEqual(sum(isinstance(part, _Image) for part in payload.chain), 1)
        self.assertEqual(sum(isinstance(part, _Video) for part in payload.chain), 1)
        image = next(part for part in payload.chain if isinstance(part, _Image))
        video = next(part for part in payload.chain if isinstance(part, _Video))
        self.assertEqual(image.path, "/tmp/a.jpg")
        self.assertEqual(video.path, "/tmp/a.mp4")

    async def test_display_flags_hide_source_time_and_twitter_link(self):
        context = _FakeContext()
        storage = _FakeStorage()
        config = RSSConfig.from_context(
            {
                "feeds": [{"id": "feed-1", "url": "https://example.com/rss", "enabled": True}],
                "targets": [
                    {
                        "id": "target-1",
                        "platform": "qq",
                        "unified_msg_origin": "default:GroupMessage:1",
                        "enabled": True,
                    }
                ],
                "jobs": [
                    {
                        "id": "job-1",
                        "feed_ids": ["feed-1"],
                        "target_ids": ["target-1"],
                        "interval_seconds": 300,
                        "enabled": True,
                    }
                ],
                "display_source": False,
                "display_time": False,
                "display_link": True,
            }
        )
        dispatcher = FeedDispatcher(context=context, config=config, storage=storage)
        dispatcher._resolve_messagechain_cls = lambda: _MessageChain
        dispatcher._resolve_plain_cls = lambda: _Plain

        item = {
            "job_id": "job-1",
            "source_type": "twitter",
            "guid": "twitter:alice:2",
            "title": "@alice",
            "summary": "hello",
            "source": "Twitter @alice",
            "published_at": "2026-05-05T00:00:00+00:00",
            "link": "https://x.com/alice/status/2",
            "send_link": False,
        }

        result = await dispatcher.dispatch(item)

        self.assertEqual(result.success_count, 1)
        plain = context.sent[0][1].chain[0].text
        self.assertIn("@alice", plain)
        self.assertIn("hello", plain)
        self.assertNotIn("来源：", plain)
        self.assertNotIn("时间：", plain)
        self.assertNotIn("https://x.com/alice/status/2", plain)

    async def test_compact_mode_sends_title_only_text(self):
        context = _FakeContext()
        storage = _FakeStorage()
        dispatcher = FeedDispatcher(context=context, config=self._build_config(), storage=storage)
        dispatcher._resolve_messagechain_cls = lambda: _MessageChain
        dispatcher._resolve_plain_cls = lambda: _Plain

        item = {
            "job_id": "job-1",
            "compact_mode_enabled": True,
            "guid": "item-compact",
            "title": "Only Title",
            "summary": "Summary should not appear.",
            "source": "Feed",
            "published_at": "2026-05-05T00:00:00+00:00",
            "link": "https://example.com/post",
            "image_urls": ["https://example.com/a.jpg"],
        }

        result = await dispatcher.dispatch(item)

        self.assertEqual(result.success_count, 1)
        plain = context.sent[0][1].chain[0].text
        self.assertEqual(plain, "Only Title")
        self.assertEqual(len(context.sent[0][1].chain), 1)

    async def test_compact_mode_can_send_source_images_in_text_mode(self):
        context = _FakeContext()
        storage = _FakeStorage()
        dispatcher = FeedDispatcher(context=context, config=self._build_config(), storage=storage)
        dispatcher._resolve_messagechain_cls = lambda: _MessageChain
        dispatcher._resolve_plain_cls = lambda: _Plain
        dispatcher._resolve_image_cls = lambda: _Image

        item = {
            "job_id": "job-1",
            "compact_mode_enabled": True,
            "compact_mode_send_images": True,
            "guid": "item-compact-image",
            "title": "Only Title",
            "summary": "Summary should not appear.",
            "source": "Feed",
            "published_at": "2026-05-05T00:00:00+00:00",
            "link": "https://example.com/post",
            "image_urls": ["https://example.com/a.jpg"],
        }

        result = await dispatcher.dispatch(item)

        self.assertEqual(result.success_count, 1)
        payload = context.sent[0][1]
        self.assertEqual(payload.chain[0].text, "Only Title")
        self.assertEqual(payload.chain[1].url, "https://example.com/a.jpg")
        self.assertEqual(len(payload.chain), 2)

    async def test_compact_mode_can_send_single_source_image_in_text_mode(self):
        context = _FakeContext()
        storage = _FakeStorage()
        dispatcher = FeedDispatcher(context=context, config=self._build_config(), storage=storage)
        dispatcher._resolve_messagechain_cls = lambda: _MessageChain
        dispatcher._resolve_plain_cls = lambda: _Plain
        dispatcher._resolve_image_cls = lambda: _Image

        item = {
            "job_id": "job-1",
            "compact_mode_enabled": True,
            "compact_mode_send_images": True,
            "guid": "item-compact-single-image",
            "title": "Only Title",
            "summary": "Summary should not appear.",
            "source": "Feed",
            "published_at": "2026-05-05T00:00:00+00:00",
            "link": "https://example.com/post",
            "image_url": "https://example.com/single.jpg",
        }

        result = await dispatcher.dispatch(item)

        self.assertEqual(result.success_count, 1)
        payload = context.sent[0][1]
        self.assertEqual(payload.chain[0].text, "Only Title")
        self.assertEqual(payload.chain[1].url, "https://example.com/single.jpg")
        self.assertEqual(len(payload.chain), 2)

    async def test_compact_mode_can_send_source_images_with_image_card(self):
        context = _FakeContext()

        async def html_render(_html):
            return "rendered-card-url"

        context.html_render = html_render
        storage = _FakeStorage()
        config = RSSConfig.from_context(
            {
                "feeds": [{"id": "feed-1", "url": "https://example.com/rss", "enabled": True}],
                "targets": [
                    {
                        "id": "target-1",
                        "platform": "qq",
                        "unified_msg_origin": "default:GroupMessage:1",
                        "enabled": True,
                    }
                ],
                "jobs": [
                    {
                        "id": "job-1",
                        "feed_ids": ["feed-1"],
                        "target_ids": ["target-1"],
                        "interval_seconds": 300,
                        "compact_mode_enabled": True,
                        "compact_mode_send_images": True,
                        "enabled": True,
                    }
                ],
                "render_mode": "image",
            }
        )
        dispatcher = FeedDispatcher(context=context, config=config, storage=storage)
        dispatcher._resolve_messagechain_cls = lambda: _MessageChain
        dispatcher._resolve_plain_cls = lambda: _Plain
        dispatcher._resolve_image_cls = lambda: _Image

        item = {
            "job_id": "job-1",
            "compact_mode_enabled": True,
            "compact_mode_send_images": True,
            "guid": "item-compact-card-image",
            "title": "Only Title",
            "summary": "Summary should not appear.",
            "source": "Feed",
            "published_at": "2026-05-05T00:00:00+00:00",
            "link": "https://example.com/post",
            "image_urls": ["https://example.com/a.jpg"],
        }

        result = await dispatcher.dispatch(item)

        self.assertEqual(result.success_count, 1)
        self.assertEqual(len(context.sent), 2)
        self.assertEqual(context.sent[0][1].chain[0].url, "rendered-card-url")
        self.assertEqual(context.sent[1][1].chain[0].url, "https://example.com/a.jpg")

    async def test_target_compact_mode_overrides_job_default_per_origin(self):
        context = _FakeContext()
        storage = _FakeStorage()
        dispatcher = FeedDispatcher(
            context=context,
            config=self._build_two_target_config(),
            storage=storage,
        )
        dispatcher._resolve_messagechain_cls = lambda: _MessageChain
        dispatcher._resolve_plain_cls = lambda: _Plain

        item = {
            "job_id": "job-1",
            "guid": "item-target-compact",
            "title": "Only One Title",
            "summary": "Normal target should receive this summary.",
            "source": "Feed",
            "published_at": "2026-05-05T00:00:00+00:00",
            "link": "https://example.com/post",
        }

        result = await dispatcher.dispatch(item)

        self.assertEqual(result.success_count, 2)
        sent_by_origin = {
            origin: payload.chain[0].text
            for origin, payload in context.sent
        }
        self.assertIn(
            "Normal target should receive this summary.",
            sent_by_origin["default:FriendMessage:562506516"],
        )
        self.assertEqual(sent_by_origin["default:GroupMessage:764968756"], "Only One Title")

    async def test_display_flags_hide_image_card_meta_and_link(self):
        context = _FakeContext()
        storage = _FakeStorage()
        config = RSSConfig.from_context(
            {
                "feeds": [{"id": "feed-1", "url": "https://example.com/rss", "enabled": True}],
                "targets": [
                    {
                        "id": "target-1",
                        "platform": "qq",
                        "unified_msg_origin": "default:GroupMessage:1",
                        "enabled": True,
                    }
                ],
                "jobs": [
                    {
                        "id": "job-1",
                        "feed_ids": ["feed-1"],
                        "target_ids": ["target-1"],
                        "interval_seconds": 300,
                        "enabled": True,
                    }
                ],
                "display_source": False,
                "display_time": False,
                "display_link": False,
            }
        )
        dispatcher = FeedDispatcher(context=context, config=config, storage=storage)

        html = dispatcher._build_card_html(
            {
                "job_id": "job-1",
                "guid": "item-1",
                "title": "Title",
                "summary": "Summary",
                "source": "Feed",
                "published_at": "2026-05-05T00:00:00+00:00",
                "link": "https://example.com/post",
            }
        )

        self.assertNotIn("来源：", html)
        self.assertNotIn("时间：", html)
        self.assertNotIn("https://example.com/post", html)

    async def test_compact_mode_image_card_contains_title_only(self):
        dispatcher = FeedDispatcher(
            context=_FakeContext(),
            config=self._build_config(),
            storage=_FakeStorage(),
        )

        html = dispatcher._build_card_html(
            {
                "job_id": "job-1",
                "compact_mode_enabled": True,
                "guid": "item-compact",
                "title": "Only Title",
                "summary": "Summary should not appear.",
                "source": "Feed",
                "published_at": "2026-05-05T00:00:00+00:00",
                "link": "https://example.com/post",
            }
        )

        self.assertIn("Only Title", html)
        self.assertNotIn("Summary should not appear.", html)
        self.assertNotIn("来源：", html)
        self.assertNotIn("时间：", html)
        self.assertNotIn("https://example.com/post", html)


if __name__ == "__main__":
    unittest.main()
