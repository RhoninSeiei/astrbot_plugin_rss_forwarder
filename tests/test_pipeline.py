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
PACKAGE_NAME = "astrbot_rss_testpkg_pipeline"
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
pipeline_module = _load_module("pipeline")
RSSConfig = config_module.RSSConfig
FeedPipeline = pipeline_module.FeedPipeline


class _DummyContext:
    def __init__(self):
        self.last_llm_kwargs = None
        self.llm_calls = 0

    async def llm_generate(self, **kwargs):
        self.llm_calls += 1
        self.last_llm_kwargs = kwargs
        return types.SimpleNamespace(
            completion_text='{"title":"中文标题","summary":"中文摘要"}'
        )

    async def get_current_chat_provider_id(self, umo):
        return "umo-provider"


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_llm_uses_configured_provider_id_and_translates_title_summary(self):
        ctx = _DummyContext()
        cfg = RSSConfig(
            feeds=[],
            targets=[],
            jobs=[],
            llm_enabled=True,
            llm_provider_id="manual-provider",
            llm_profile="rss_enrich",
            llm_timeout_seconds=5,
            max_input_chars=2000,
        )
        pipe = FeedPipeline(ctx, cfg)

        out = await pipe.process({"title": "Hello", "summary": "World"})

        self.assertEqual(out.get("title"), "中文标题")
        self.assertEqual(out.get("summary"), "中文摘要")
        self.assertEqual(ctx.last_llm_kwargs.get("chat_provider_id"), "manual-provider")

    async def test_llm_success_should_not_call_google(self):
        ctx = _DummyContext()
        cfg = RSSConfig(
            feeds=[],
            targets=[],
            jobs=[],
            llm_enabled=True,
            llm_provider_id="manual-provider",
            google_translate_enabled=True,
            google_translate_api_key="k",
        )
        pipe = FeedPipeline(ctx, cfg)

        calls = {"google": 0}

        async def google_mock(_source):
            calls["google"] += 1
            return {"title": "谷歌标题", "summary": "谷歌摘要"}, "ok"

        pipe._try_google_translate_fields = google_mock

        out = await pipe.process({"title": "Hello", "summary": "World"})

        self.assertEqual(out.get("title"), "中文标题")
        self.assertEqual(out.get("summary"), "中文摘要")
        self.assertEqual(calls["google"], 0)

    async def test_fallback_to_google_when_llm_times_out(self):
        class TimeoutContext(_DummyContext):
            async def llm_generate(self, **kwargs):
                await asyncio.sleep(0.05)
                return types.SimpleNamespace(completion_text='{"title":"慢","summary":"慢"}')

        ctx = TimeoutContext()
        cfg = RSSConfig(
            feeds=[],
            targets=[],
            jobs=[],
            llm_enabled=True,
            llm_provider_id="manual-provider",
            llm_timeout_seconds=0.01,
            google_translate_enabled=True,
            google_translate_api_key="k",
        )
        pipe = FeedPipeline(ctx, cfg)

        calls = {"google": 0}

        async def google_mock(_source):
            calls["google"] += 1
            return {"title": "谷歌标题", "summary": "谷歌摘要"}, "ok"

        pipe._try_google_translate_fields = google_mock

        out = await pipe.process({"title": "Hello", "summary": "World"})

        self.assertEqual(out.get("title"), "谷歌标题")
        self.assertEqual(out.get("summary"), "谷歌摘要")
        self.assertEqual(calls["google"], 1)

    async def test_google_direct_when_llm_disabled(self):
        ctx = _DummyContext()
        cfg = RSSConfig(
            feeds=[],
            targets=[],
            jobs=[],
            llm_enabled=False,
            google_translate_enabled=True,
            google_translate_api_key="k",
        )
        pipe = FeedPipeline(ctx, cfg)
        async def google_mock(_source):
            return {"title": "直连标题", "summary": "直连摘要"}, "ok"

        pipe._try_google_translate_fields = google_mock

        out = await pipe.process({"title": "Hello", "summary": "World"})

        self.assertEqual(out.get("title"), "直连标题")
        self.assertEqual(out.get("summary"), "直连摘要")
        self.assertEqual(ctx.llm_calls, 0)

    async def test_google_should_precede_github_models_when_llm_fails(self):
        class FailingContext(_DummyContext):
            async def llm_generate(self, **kwargs):
                raise RuntimeError("boom")

        ctx = FailingContext()
        cfg = RSSConfig(
            feeds=[],
            targets=[],
            jobs=[],
            llm_enabled=True,
            llm_provider_id="manual-provider",
            github_models_enabled=True,
            google_translate_enabled=True,
            google_translate_api_key="k",
        )
        pipe = FeedPipeline(ctx, cfg)

        calls = {"github": 0, "google": 0}

        async def google_mock(_source):
            calls["google"] += 1
            return {"title": "谷歌标题", "summary": "谷歌摘要"}, "ok"

        async def github_mock(_source):
            calls["github"] += 1
            return {"title": "GitHub标题", "summary": "GitHub摘要"}, "ok"

        pipe._try_google_translate_fields = google_mock
        pipe._try_github_models_translate_fields = github_mock

        out = await pipe.process({"title": "Hello", "summary": "World"})

        self.assertEqual(out.get("title"), "谷歌标题")
        self.assertEqual(out.get("summary"), "谷歌摘要")
        self.assertEqual(calls["google"], 1)
        self.assertEqual(calls["github"], 0)

    async def test_fallback_to_github_models_when_google_fails(self):
        class FailingContext(_DummyContext):
            async def llm_generate(self, **kwargs):
                raise RuntimeError("boom")

        ctx = FailingContext()
        cfg = RSSConfig(
            feeds=[],
            targets=[],
            jobs=[],
            llm_enabled=True,
            llm_provider_id="manual-provider",
            github_models_enabled=True,
            google_translate_enabled=True,
            google_translate_api_key="k",
        )
        pipe = FeedPipeline(ctx, cfg)

        calls = {"github": 0, "google": 0}

        async def fail_google(_source):
            calls["google"] += 1
            return {}, "exception:RuntimeError"

        async def github_mock(_source):
            calls["github"] += 1
            return {"title": "GitHub标题", "summary": "GitHub摘要"}, "ok"

        pipe._try_google_translate_fields = fail_google
        pipe._try_github_models_translate_fields = github_mock

        out = await pipe.process({"title": "Hello", "summary": "World"})

        self.assertEqual(out.get("title"), "GitHub标题")
        self.assertEqual(out.get("summary"), "GitHub摘要")
        self.assertEqual(calls["google"], 1)
        self.assertEqual(calls["github"], 1)

    async def test_prompt_uses_cleaned_content_without_html_tags(self):
        ctx = _DummyContext()
        cfg = RSSConfig(
            feeds=[],
            targets=[],
            jobs=[],
            llm_enabled=True,
            llm_provider_id="manual-provider",
            llm_timeout_seconds=5,
        )
        pipe = FeedPipeline(ctx, cfg)

        await pipe.process(
            {
                "title": 'AMD "Medusa Point"',
                "summary": 'AMD &quot;<a href="https://example.com">Medusa</a>&quot; test',
            }
        )

        prompt = str(ctx.last_llm_kwargs.get("prompt", ""))
        self.assertIn('AMD "Medusa Point"', prompt)
        self.assertIn('AMD " Medusa " test', prompt)
        self.assertNotIn("<a href", prompt)
        self.assertNotIn("&quot;", prompt)

    async def test_fallback_to_cleaned_english_when_llm_and_google_fail(self):
        class FailingContext(_DummyContext):
            async def llm_generate(self, **kwargs):
                raise RuntimeError("boom")

        ctx = FailingContext()
        cfg = RSSConfig(
            feeds=[],
            targets=[],
            jobs=[],
            llm_enabled=True,
            llm_provider_id="manual-provider",
            google_translate_enabled=True,
            google_translate_api_key="k",
            llm_timeout_seconds=1,
            google_translate_timeout_seconds=1,
        )
        pipe = FeedPipeline(ctx, cfg)

        async def fail_google(_source):
            return {}, "exception:RuntimeError"

        pipe._try_google_translate_fields = fail_google

        out = await pipe.process(
            {
                "title": "English Title",
                "summary": 'A &quot;<a href="https://x">link</a>&quot; remains',
            }
        )

        self.assertEqual(out.get("title"), "English Title")
        self.assertEqual(out.get("summary"), 'A " link " remains')


if __name__ == "__main__":
    unittest.main()
