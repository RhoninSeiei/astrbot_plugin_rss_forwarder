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
    def __init__(self):
        self.sent = []

    async def send_message(self, origin, payload):
        self.sent.append((origin, payload))


class _MessageChain:
    def __init__(self, chain=None):
        self.chain = list(chain or [])


class _Image:
    def __init__(self, path):
        self.path = path

    @classmethod
    def fromFileSystem(cls, path):
        return cls(path)


class AggregateDispatcherTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_aggregate_dispatch_uses_target_origins_and_blocks_duplicates(self):
        context = _FakeContext()
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
            "_target_origins": ["qq:group:2"],
            "target_ids": ["target-1"],
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


if __name__ == "__main__":
    unittest.main()
