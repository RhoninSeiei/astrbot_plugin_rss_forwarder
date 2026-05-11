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
PACKAGE_NAME = "astrbot_rss_testpkg_semantic"
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
semantic_module = _load_module("semantic_dedup")
RSSConfig = config_module.RSSConfig
SemanticDedupService = semantic_module.SemanticDedupService


class _DummyContext:
    def __init__(self, text):
        self.text = text
        self.last_llm_kwargs = None

    async def llm_generate(self, **kwargs):
        self.last_llm_kwargs = kwargs
        return types.SimpleNamespace(completion_text=self.text)


class SemanticDedupServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_duplicate_result_uses_job_provider_and_match_confidence(self):
        class FakeStorage:
            async def list_semantic_dedup_records(self, job_id, limit, ttl_seconds):
                return [
                    {
                        "record_id": "record-1",
                        "title": "NVIDIA announces RTX 6090",
                        "summary": "NVIDIA announced a new GPU.",
                        "source": "Tom's Hardware",
                        "link": "https://example.com/a",
                        "published_at": "2026-05-11T00:00:00+00:00",
                    }
                ]

        ctx = _DummyContext(
            '{"duplicate": true, "matched_record_id": "record-1", '
            '"confidence": 0.91, "reason": "same launch"}'
        )
        cfg = RSSConfig(feeds=[], targets=[], jobs=[], llm_timeout_seconds=5)
        service = SemanticDedupService(ctx, cfg, FakeStorage())
        job = types.SimpleNamespace(
            id="job-1",
            semantic_dedup_enabled=True,
            semantic_dedup_provider_id="provider-news",
            semantic_dedup_ttl_seconds=86400,
            semantic_dedup_max_candidates=12,
            semantic_dedup_min_confidence=0.8,
        )

        result = await service.check(
            job,
            {
                "feed_id": "feed-2",
                "title": "NVIDIA unveils RTX 6090",
                "summary": "NVIDIA introduced a new GPU.",
            },
        )

        self.assertTrue(result.duplicate)
        self.assertEqual(result.matched_record_id, "record-1")
        self.assertEqual(ctx.last_llm_kwargs["chat_provider_id"], "provider-news")

    async def test_low_confidence_duplicate_is_allowed(self):
        class FakeStorage:
            async def list_semantic_dedup_records(self, job_id, limit, ttl_seconds):
                return [{"record_id": "record-1", "title": "A", "summary": "B"}]

        ctx = _DummyContext(
            '{"duplicate": true, "matched_record_id": "record-1", '
            '"confidence": 0.5, "reason": "weak"}'
        )
        cfg = RSSConfig(feeds=[], targets=[], jobs=[], llm_timeout_seconds=5)
        service = SemanticDedupService(ctx, cfg, FakeStorage())
        job = types.SimpleNamespace(
            id="job-1",
            semantic_dedup_enabled=True,
            semantic_dedup_provider_id="provider-news",
            semantic_dedup_ttl_seconds=86400,
            semantic_dedup_max_candidates=12,
            semantic_dedup_min_confidence=0.8,
        )

        result = await service.check(job, {"title": "C", "summary": "D"})

        self.assertFalse(result.duplicate)
        self.assertEqual(result.reason, "below_confidence")

    async def test_duplicate_without_match_id_is_allowed(self):
        class FakeStorage:
            async def list_semantic_dedup_records(self, job_id, limit, ttl_seconds):
                return [{"record_id": "record-1", "title": "A", "summary": "B"}]

        ctx = _DummyContext(
            '{"duplicate": true, "matched_record_id": "", "confidence": 0.95, "reason": "same"}'
        )
        cfg = RSSConfig(feeds=[], targets=[], jobs=[], llm_timeout_seconds=5)
        service = SemanticDedupService(ctx, cfg, FakeStorage())
        job = types.SimpleNamespace(
            id="job-1",
            semantic_dedup_enabled=True,
            semantic_dedup_provider_id="provider-news",
            semantic_dedup_ttl_seconds=86400,
            semantic_dedup_max_candidates=12,
            semantic_dedup_min_confidence=0.8,
        )

        result = await service.check(job, {"title": "C", "summary": "D"})

        self.assertFalse(result.duplicate)
        self.assertEqual(result.reason, "missing_match_id")


if __name__ == "__main__":
    unittest.main()
