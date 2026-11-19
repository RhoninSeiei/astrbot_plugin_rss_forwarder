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
PACKAGE_NAME = "astrbot_rss_testpkg_aggregate_dispatcher"
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
_load_module("aggregate_card_image")
dispatcher_module = _load_module("dispatcher")

RSSConfig = config_module.RSSConfig
FeedDispatcher = dispatcher_module.FeedDispatcher


class _FakeStorage:
    def __init__(self):
        self.claimed = set()
        self.confirmed = set()

    async def claim_dispatch(self, fingerprint: str, ttl_seconds: int = 0) -> bool:
        if fingerprint in self.claimed:
            return False
        self.claimed.add(fingerprint)
        return True

    async def confirm_dispatch(self, fingerprint: str, ttl_seconds: int = 0) -> None:
        self.confirmed.add(fingerprint)

    async def release_dispatch(self, fingerprint: str) -> None:
        self.claimed.discard(fingerprint)

    def plugin_cache_dir(self):
        return str(ROOT / "data" / "test-cache")


class _FakeContext:
    def __init__(self, send_result=True):
        self.send_result = send_result
        self.sent = []

    async def send_message(self, origin, payload):
        self.sent.append((origin, payload))
        return self.send_result


class _MessageChain:
    def __init__(self, chain=None):
        self.chain = list(chain or [])


class _Plain:
    def __init__(self, text):
        self.text = text


class _Image:
    def __init__(self, path):
        self.path = path

    @classmethod
    def fromFileSystem(cls, path):
        return cls(path)


class AggregateDispatcherTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_text_builder_sends_aggregate_and_image_fallback(self):
        for mode in ('text', 'image'):
            with self.subTest(mode=mode):
                context = _FakeContext()
                dispatcher = FeedDispatcher(context=context, config=self._build_config(), storage=_FakeStorage())
                dispatcher._resolve_messagechain_cls = lambda: _MessageChain
                dispatcher._resolve_plain_cls = lambda: _Plain

                async def fail_image(_digest):
                    raise RuntimeError('test renderer unavailable')

                dispatcher._build_aggregate_digest_image_payload = fail_image
                result = await dispatcher.dispatch_aggregate_digest({
                    'id': 'real-builder', 'title': 'Hardware news', 'content': '1. New CPU',
                    'target_ids': ['target-1'], 'render_mode': mode,
                    'links': [{'source': 'Example', 'link': 'https://example.com/news'}],
                })
                self.assertEqual(result.success_count, 1)
                self.assertEqual(result.transient_failure_count, 0)
                self.assertEqual(len(context.sent), 1)
                text = context.sent[0][1].chain[0].text
                self.assertIn('Hardware news', text)
                self.assertIn('1. New CPU', text)
                self.assertIn('Example: https://example.com/news', text)

    def test_times_are_human_readable_in_configured_timezone(self):
        config = self._build_config()
        config.timezone = 'Asia/Shanghai'
        dispatcher = FeedDispatcher(context=_FakeContext(), config=config)
        for value in ['2026-05-11T03:30:01+00:00', '2026-05-11T03:30:01Z', 'Mon, 11 May 2026 03:30:01 GMT']:
            with self.subTest(value=value):
                self.assertEqual(dispatcher._format_time({'published_at': value}), '2026-05-11 11:30')
        config.timezone = 'America/New_York'
        self.assertEqual(dispatcher._format_time({'published_at': '2026-05-11T03:30:01Z'}), '2026-05-10 23:30')
        self.assertEqual(dispatcher._format_time({'published_at': '2026-05-11 03:30:01'}), '2026-05-11 03:30')
        self.assertEqual(dispatcher._format_time({'published_at': 'unknown'}), 'unknown')

    def test_twitter_text_and_card_use_formatted_time(self):
        config = self._build_config()
        config.timezone = 'Asia/Shanghai'
        dispatcher = FeedDispatcher(context=_FakeContext(), config=config)
        dispatcher._resolve_messagechain_cls = lambda: _MessageChain
        dispatcher._resolve_plain_cls = lambda: _Plain
        item = {'title': 'News', 'source_type': 'twitter', 'published_at': '2026-05-11T03:30:01+00:00'}
        text = dispatcher._build_text_message_chain(item, include_media=False).chain[0].text
        card = dispatcher._build_card_html(item)
        for output in (text, card):
            self.assertIn('2026-05-11 11:30', output)
            self.assertNotIn('T03:30:01+00:00', output)

    def test_invalid_timezone_falls_back_to_utc(self):
        config = self._build_config()
        config.timezone = 'Invalid/Timezone'
        dispatcher = FeedDispatcher(context=_FakeContext(), config=config)
        self.assertEqual(dispatcher._format_time({'published_at': '2026-05-11T03:30:01Z'}), '2026-05-11 03:30')

    def _build_config(self):
        return RSSConfig.from_context(
            {
                "feeds": [{"id": "feed-1", "url": "https://example.com/rss", "enabled": True}],
                "targets": [
                    {
                        "id": "target-1",
                        "platform": "qq",
                        "unified_msg_origin": "qq:group:1",
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
            }
        )

    async def test_aggregate_dispatch_intersects_job_targets_and_pending_origins(self):
        config = self._build_config()
        config.targets.extend([
            config_module.TargetConfig("target-2", "qq", "qq:group:2"),
            config_module.TargetConfig("target-3", "qq", "qq:group:3"),
            config_module.TargetConfig("target-disabled", "qq", "qq:group:4", enabled=False),
        ])
        config.jobs[0].target_ids.append("target-2")
        config.jobs.extend([
            config_module.JobConfig("job-2", ["feed-1"], ["target-3"]),
            config_module.JobConfig("job-disabled", ["feed-1"], ["target-1"], enabled=False),
        ])

        cases = [
            ("scheduler pending subset", {
                "job_id": "job-1", "target_ids": ["target-1", "target-2"],
                "_target_origins": ["qq:group:2", "qq:group:2", "qq:group:3", "qq:group:9"],
            }, ["qq:group:2"]),
            ("target IDs cannot add a job destination", {
                "job_id": "job-1", "target_ids": ["target-1", "target-3"],
            }, ["qq:group:1"]),
            ("pending origins must also match target IDs", {
                "job_id": "job-1", "target_ids": ["target-1"], "_target_origins": ["qq:group:2"],
            }, []),
            ("empty pending origins", {
                "job_id": "job-1", "target_ids": ["target-1"], "_target_origins": [],
            }, []),
            ("invalid pending origins", {
                "job_id": "job-1", "target_ids": ["target-1"], "_target_origins": None,
            }, []),
            ("empty target IDs", {
                "job_id": "job-1", "target_ids": [], "_target_origins": ["qq:group:1"],
            }, []),
            ("invalid target IDs", {
                "job_id": "job-1", "target_ids": [None, {}], "_target_origins": ["qq:group:1"],
            }, []),
            ("disabled and unknown targets", {
                "job_id": "job-1", "target_ids": ["target-disabled", "missing"],
                "_target_origins": ["qq:group:4", "qq:group:9"],
            }, []),
            ("unknown job", {
                "job_id": "missing", "target_ids": ["target-1"],
            }, []),
            ("disabled job", {
                "job_id": "job-disabled", "target_ids": ["target-1"],
            }, []),
            ("job with pending origins only", {
                "job_id": "job-1", "_target_origins": ["qq:group:2", "qq:group:3"],
            }, ["qq:group:2"]),
            ("job without any requested targets", {"job_id": "job-1"}, []),
            ("jobless target IDs", {
                "target_ids": ["target-1", "target-3", "target-disabled", "missing"],
            }, ["qq:group:1", "qq:group:3"]),
            ("jobless target IDs and pending origins", {
                "target_ids": ["target-1"], "_target_origins": ["qq:group:1", "qq:group:2"],
            }, ["qq:group:1"]),
            ("pending origins without a job or target IDs", {
                "_target_origins": ["qq:group:1"],
            }, []),
        ]
        for case, scope, expected in cases:
            with self.subTest(case=case):
                context = _FakeContext()
                dispatcher = FeedDispatcher(context=context, config=config)
                dispatcher._build_aggregate_digest_text_chain = lambda digest: "aggregate-payload"

                result = await dispatcher.dispatch_aggregate_digest({"title": "Aggregate", **scope})

                self.assertEqual([origin for origin, _ in context.sent], expected)
                self.assertEqual(result.success_origins, expected)

    async def test_aggregate_dispatch_uses_target_origins_and_blocks_duplicates(self):
        context = _FakeContext()
        config = self._build_config()
        config.targets.append(config_module.TargetConfig("target-2", "qq", "qq:group:2"))
        config.jobs[0].target_ids.append("target-2")
        dispatcher = FeedDispatcher(
            context=context,
            config=config,
            storage=_FakeStorage(),
        )
        dispatcher._build_aggregate_digest_text_chain = lambda digest: "aggregate-payload"

        digest = {
            "id": "aggregate-1",
            "job_id": "job-1",
            "title": "聚合标题",
            "content": "1. 内容",
            "item_count": 1,
            "_target_origins": ["qq:group:2"],
            "target_ids": ["target-1", "target-2"],
            "render_mode": "text",
        }

        first = await dispatcher.dispatch_aggregate_digest(digest)
        second = await dispatcher.dispatch_aggregate_digest(digest)

        self.assertEqual(first.success_count, 1)
        self.assertEqual(second.skipped_duplicate_count, 1)
        self.assertEqual(context.sent, [("qq:group:2", "aggregate-payload")])

    async def test_aggregate_image_mode_uses_local_renderer(self):
        context = _FakeContext()
        dispatcher = FeedDispatcher(
            context=context,
            config=self._build_config(),
            storage=_FakeStorage(),
        )
        dispatcher._resolve_messagechain_cls = lambda: _MessageChain
        dispatcher._resolve_image_cls = lambda: _Image
        dispatcher._render_aggregate_digest_image_file = lambda digest: "/tmp/aggregate.png"

        digest = {
            "id": "aggregate-image",
            "job_id": "job-1",
            "title": "聚合标题",
            "content": "1. 内容",
            "sections": [{"title": "标题", "summary": "摘要"}],
            "item_count": 1,
            "target_ids": ["target-1"],
            "render_mode": "image",
        }

        result = await dispatcher.dispatch_aggregate_digest(digest)

        self.assertEqual(result.success_count, 1)
        payload = context.sent[0][1]
        self.assertIsInstance(payload, _MessageChain)
        self.assertIsInstance(payload.chain[0], _Image)
        self.assertEqual(payload.chain[0].path, "/tmp/aggregate.png")

    def test_target_error_classifier_keeps_transient_send_errors_retryable(self):
        transient_messages = [
            "",
            "Timeout: NTEvent serviceAndMethod:NodeIKernelMsgService/sendMsg",
            "EventChecker Failed: rich media transfer failed",
            "ApiNotAvailable",
            "retcode=1200",
        ]
        for message in transient_messages:
            with self.subTest(message=message):
                self.assertFalse(FeedDispatcher._is_permanent_target_error(Exception(message)))

        permanent_messages = [
            "unsupported target platform",
            "target not found",
            "no such target",
            "目标不存在",
            "平台不支持主动消息",
        ]
        for message in permanent_messages:
            with self.subTest(message=message):
                self.assertTrue(FeedDispatcher._is_permanent_target_error(Exception(message)))

    async def test_transient_send_error_does_not_disable_origin(self):
        class FailingContext(_FakeContext):
            async def send_message(self, origin, payload):
                self.sent.append((origin, payload))
                raise Exception("")

        context = FailingContext()
        dispatcher = FeedDispatcher(
            context=context,
            config=self._build_config(),
            storage=_FakeStorage(),
        )
        dispatcher._build_aggregate_digest_text_chain = lambda digest: "aggregate-payload"

        digest = {
            "id": "aggregate-1",
            "job_id": "job-1",
            "title": "聚合标题",
            "content": "1. 内容",
            "item_count": 1,
            "_target_origins": ["qq:group:1"],
            "target_ids": ["target-1"],
            "render_mode": "text",
        }

        first = await dispatcher.dispatch_aggregate_digest(digest)
        second = await dispatcher.dispatch_aggregate_digest(digest)

        self.assertEqual(first.transient_failure_count, 1)
        self.assertEqual(first.permanent_failure_count, 0)
        self.assertEqual(second.transient_failure_count, 1)
        self.assertEqual(second.skipped_disabled_count, 0)
        self.assertEqual(context.sent, [("qq:group:1", "aggregate-payload"), ("qq:group:1", "aggregate-payload")])

    async def test_aggregate_send_false_releases_dispatch_claim_for_retry(self):
        context = _FakeContext(send_result=False)
        storage = _FakeStorage()
        dispatcher = FeedDispatcher(
            context=context,
            config=self._build_config(),
            storage=storage,
        )
        dispatcher._build_aggregate_digest_text_chain = lambda digest: "aggregate-payload"

        digest = {
            "id": "aggregate-1",
            "job_id": "job-1",
            "title": "聚合标题",
            "content": "1. 内容",
            "item_count": 1,
            "_target_origins": ["qq:group:1"],
            "target_ids": ["target-1"],
            "render_mode": "text",
        }

        first = await dispatcher.dispatch_aggregate_digest(digest)
        context.send_result = True
        second = await dispatcher.dispatch_aggregate_digest(digest)

        self.assertEqual(first.transient_failure_count, 1)
        self.assertEqual(first.success_count, 0)
        self.assertEqual(second.success_count, 1)
        self.assertEqual(context.sent, [("qq:group:1", "aggregate-payload"), ("qq:group:1", "aggregate-payload")])



if __name__ == "__main__":
    unittest.main()
