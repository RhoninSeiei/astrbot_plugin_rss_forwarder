import hashlib
import json
import sys
import types
import unittest
from datetime import datetime
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from zoneinfo import ZoneInfo


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
PACKAGE_NAME = "astrbot_rss_testpkg_cron_aggregate"
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
_load_module("fetcher")
_load_module("parser")
pipeline_module = _load_module("pipeline")
_load_module("storage")
scheduler_module = _load_module("scheduler")

RSSConfig = config_module.RSSConfig
ConfigValidationError = config_module.ConfigValidationError
DispatchResult = dispatcher_module.DispatchResult
FeedPipeline = pipeline_module.FeedPipeline
RSSScheduler = scheduler_module.RSSScheduler


def _minimal_runtime_conf():
    return {
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


class CronAggregateConfigTests(unittest.TestCase):
    def test_job_aggregate_config_parses(self):
        conf = _minimal_runtime_conf()
        conf["jobs"][0].update(
            {
                "cron": "0 9,18 * * *",
                "interval_seconds": 0,
                "aggregate_enabled": True,
                "aggregate_provider_id": "provider-aggregate",
                "aggregate_render_mode": "image",
                "aggregate_include_images": True,
                "aggregate_max_items": 12,
                "aggregate_llm_timeout_seconds": 120,
                "aggregate_prompt_template": "聚合 {items}",
            }
        )

        cfg = RSSConfig.from_context(conf)

        job = cfg.jobs[0]
        self.assertEqual(job.cron, "0 9,18 * * *")
        self.assertTrue(job.aggregate_enabled)
        self.assertEqual(job.aggregate_provider_id, "provider-aggregate")
        self.assertEqual(job.aggregate_render_mode, "image")
        self.assertTrue(job.aggregate_include_images)
        self.assertEqual(job.aggregate_max_items, 12)
        self.assertEqual(job.aggregate_llm_timeout_seconds, 120)
        self.assertEqual(job.aggregate_prompt_template, "聚合 {items}")

    def test_job_aggregate_config_validates_render_mode(self):
        conf = _minimal_runtime_conf()
        conf["jobs"][0].update(
            {
                "aggregate_enabled": True,
                "aggregate_render_mode": "html",
            }
        )

        with self.assertRaises(ConfigValidationError):
            RSSConfig.from_context(conf)

    def test_schema_exposes_job_aggregate_options(self):
        schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))

        job_items = schema["jobs"]["templates"]["job"]["items"]
        self.assertIn("aggregate_enabled", job_items)
        self.assertEqual(job_items["aggregate_provider_id"]["_special"], "select_provider")
        self.assertEqual(job_items["aggregate_render_mode"]["default"], "text")


class CronScheduleTests(unittest.TestCase):
    def test_cron_next_delay_uses_local_timezone(self):
        scheduler = RSSScheduler(
            config=types.SimpleNamespace(timezone="Asia/Shanghai"),
            fetcher=types.SimpleNamespace(),
            parser=types.SimpleNamespace(),
            dispatcher=types.SimpleNamespace(),
            storage=types.SimpleNamespace(),
            pipeline=None,
        )
        job = types.SimpleNamespace(id="job-cron", cron="*/15 * * * *")
        now = datetime(2026, 5, 31, 9, 7, 30, tzinfo=ZoneInfo("Asia/Shanghai"))

        self.assertEqual(scheduler._cron_next_delay_seconds(job, now=now), 450)

    def test_invalid_cron_falls_back_to_interval(self):
        scheduler = RSSScheduler(
            config=types.SimpleNamespace(poll_interval_seconds=300, timezone="Asia/Shanghai"),
            fetcher=types.SimpleNamespace(),
            parser=types.SimpleNamespace(),
            dispatcher=types.SimpleNamespace(),
            storage=types.SimpleNamespace(),
            pipeline=None,
        )
        job = types.SimpleNamespace(id="job-cron", cron="bad cron", interval_seconds=0)

        self.assertEqual(scheduler._resolve_interval(job), 300)


class AggregatePipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_build_aggregate_content_parses_llm_json(self):
        class FakeContext:
            def __init__(self):
                self.last_llm_kwargs = None

            async def llm_generate(self, **kwargs):
                self.last_llm_kwargs = kwargs
                return types.SimpleNamespace(
                    completion_text=(
                        '{"title":"显卡快讯：驱动与新品更新",'
                        '"sections":[{"title":"NVIDIA 驱动更新",'
                        '"summary":"新版驱动修复多个游戏问题。",'
                        '"item_indices":[1],"image_index":1}]}'
                    )
                )

        ctx = FakeContext()
        cfg = RSSConfig(
            feeds=[],
            targets=[],
            jobs=[],
            llm_enabled=True,
            llm_provider_id="provider-default",
            llm_timeout_seconds=5,
        )
        pipe = FeedPipeline(ctx, cfg)

        result = await pipe.build_aggregate_content(
            {
                "id": "job-aggregate",
                "aggregate_provider_id": "provider-aggregate",
                "aggregate_max_items": 5,
            },
            [
                {
                    "feed_title": "TechPowerUp",
                    "title": "NVIDIA driver released",
                    "summary": "Driver fixes games.",
                    "image_url": "https://example.com/driver.jpg",
                    "image_paths": ["/tmp/driver.jpg"],
                    "link": "https://example.com/driver",
                    "proxy_url": "http://127.0.0.1:7890",
                }
            ],
            unified_msg_origin="qq:group:1",
        )

        self.assertEqual(result["engine"], "llm")
        self.assertEqual(result["title"], "显卡快讯：驱动与新品更新")
        self.assertEqual(result["sections"][0]["image_url"], "https://example.com/driver.jpg")
        self.assertEqual(result["sections"][0]["image_path"], "/tmp/driver.jpg")
        self.assertEqual(result["sections"][0]["proxy_url"], "http://127.0.0.1:7890")
        self.assertEqual(ctx.last_llm_kwargs["chat_provider_id"], "provider-aggregate")
        self.assertIn("NVIDIA driver released", ctx.last_llm_kwargs["prompt"])

    async def test_build_aggregate_content_falls_back_to_sections(self):
        ctx = types.SimpleNamespace()
        cfg = RSSConfig(feeds=[], targets=[], jobs=[], llm_enabled=False)
        pipe = FeedPipeline(ctx, cfg)

        result = await pipe.build_aggregate_content(
            {"id": "job-aggregate", "aggregate_max_items": 5},
            [
                {
                    "feed_title": "VideoCardz",
                    "title": "New GPU appears",
                    "summary": "Board partners prepare cards.",
                }
            ],
        )

        self.assertEqual(result["engine"], "fallback")
        self.assertIn("New GPU appears", result["title"])
        self.assertEqual(result["sections"][0]["source"], "VideoCardz")


class AggregateSchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def test_aggregate_job_dispatches_one_digest_and_marks_each_item_seen(self):
        group_origin = "qq:group:1"

        class FakeStorage:
            def __init__(self):
                self.marked = []

            def build_seen_keys(self, item):
                return [item["guid"]]

            async def has_seen(self, item_id, ttl_seconds=None):
                return False

            async def mark_seen(self, item_id, ttl_seconds=0):
                self.marked.append((item_id, ttl_seconds))

            async def get_feed_state(self, feed_id):
                return {"last_success_time": 0}

            async def update_feed_state(self, *args, **kwargs):
                return {}

            async def archive_digest_items(self, items):
                self.archived = list(items)

        class FakeFetcher:
            async def fetch(self, job):
                return [{"feed_id": "feed-1"}]

        class FakeParser:
            def parse(self, raw_items, job):
                return [
                    {
                        "feed_id": "feed-1",
                        "guid": "guid-1",
                        "title": "NVIDIA driver released",
                        "summary": "Driver fixes games.",
                        "published_at": "",
                    },
                    {
                        "feed_id": "feed-1",
                        "guid": "guid-2",
                        "title": "AMD BIOS update",
                        "summary": "New BIOS improves memory training.",
                        "published_at": "",
                    },
                ]

        class FakePipeline:
            async def build_aggregate_content(self, job, items, unified_msg_origin=""):
                self.last_job = job
                self.last_items = list(items)
                self.last_origin = unified_msg_origin
                return {
                    "title": "硬件快讯：驱动与 BIOS 更新",
                    "content": "1. NVIDIA 驱动更新\n2. AMD BIOS 更新",
                    "sections": [
                        {"title": "NVIDIA 驱动更新", "summary": "修复多个游戏问题。"},
                        {"title": "AMD BIOS 更新", "summary": "改善内存训练。"},
                    ],
                    "engine": "llm",
                    "llm_reason": "ok",
                }

        class FakeDispatcher:
            def __init__(self):
                self.aggregates = []

            async def dispatch_aggregate_digest(self, digest):
                self.aggregates.append(digest)
                return DispatchResult(success_count=1, success_origins=[group_origin])

        config = RSSConfig.from_context(
            {
                "feeds": [{"id": "feed-1", "url": "https://example.com/rss", "enabled": True}],
                "targets": [
                    {
                        "id": "target-1",
                        "platform": "qq",
                        "unified_msg_origin": group_origin,
                        "enabled": True,
                    }
                ],
                "jobs": [
                    {
                        "id": "job-aggregate",
                        "feed_ids": ["feed-1"],
                        "target_ids": ["target-1"],
                        "interval_seconds": 300,
                        "aggregate_enabled": True,
                        "aggregate_max_items": 5,
                        "enabled": True,
                    }
                ],
                "dedup_ttl_seconds": 123,
            }
        )
        storage = FakeStorage()
        dispatcher = FakeDispatcher()
        pipeline = FakePipeline()
        scheduler = RSSScheduler(
            config=config,
            fetcher=FakeFetcher(),
            parser=FakeParser(),
            dispatcher=dispatcher,
            storage=storage,
            pipeline=pipeline,
        )

        await scheduler._run_job_once_guarded(config.jobs[0])

        self.assertEqual(len(dispatcher.aggregates), 1)
        aggregate = dispatcher.aggregates[0]
        self.assertEqual(aggregate["title"], "硬件快讯：驱动与 BIOS 更新")
        self.assertEqual(aggregate["_target_origins"], [group_origin])
        self.assertEqual(aggregate["item_count"], 2)
        self.assertEqual([item["guid"] for item in pipeline.last_items], ["guid-1", "guid-2"])
        origin_hash = hashlib.sha256(group_origin.encode("utf-8")).hexdigest()
        self.assertEqual(
            storage.marked,
            [
                (f"job:job-aggregate:origin:{origin_hash}:guid-1", 123),
                (f"job:job-aggregate:origin:{origin_hash}:guid-2", 123),
            ],
        )


if __name__ == "__main__":
    unittest.main()
