import sys
import types
import unittest
from datetime import datetime
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


astrbot_module = types.ModuleType("astrbot")
astrbot_api_module = types.ModuleType("astrbot.api")
astrbot_event_module = types.ModuleType("astrbot.api.event")
astrbot_api_module.logger = types.SimpleNamespace(
    info=lambda *a, **k: None,
    warning=lambda *a, **k: None,
    error=lambda *a, **k: None,
)
astrbot_event_module.AstrMessageEvent = object
sys.modules.setdefault("astrbot", astrbot_module)
sys.modules["astrbot.api"] = astrbot_api_module
sys.modules["astrbot.api.event"] = astrbot_event_module

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "astrbot_rss_testpkg_commands"
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
commands_module = _load_module("commands")
scheduler_module = _load_module("scheduler")
RSSCommands = commands_module.RSSCommands
DailyDigestConfig = config_module.DailyDigestConfig
DigestExecutionResult = scheduler_module.DigestExecutionResult


class _FakeEvent:
    def __init__(self, message: str):
        self.message_str = message

    def plain_result(self, text: str):
        return text


class CommandsTests(unittest.IsolatedAsyncioTestCase):
    async def test_rss_digest_run_routes_to_scheduler(self):
        commands = RSSCommands()
        digest_result = DigestExecutionResult(
            started_at=datetime(2026, 3, 29, 9, 0, 0),
            duration_ms=120,
            item_count=3,
            pushed_count=1,
            error_summary="",
        )

        class FakeScheduler:
            def __init__(self):
                self.called = []
                self.digest_results = {"digest-1": digest_result}

            async def run_daily_digest_once(self, digest_id):
                self.called.append(digest_id)
                return True

        commands.scheduler = FakeScheduler()
        event = _FakeEvent("/rss digest run digest-1")

        results = [result async for result in commands.rss_router(event)]

        self.assertEqual(commands.scheduler.called, ["digest-1"])
        self.assertEqual(len(results), 1)
        self.assertIn("已触发日报 digest-1", results[0])

    async def test_rss_list_includes_daily_digest_status(self):
        commands = RSSCommands()
        digest = DailyDigestConfig(
            id="digest-1",
            title="芯片日报",
            feed_ids=["feed-1"],
            target_ids=["target-1"],
            send_time="09:00",
            enabled=True,
        )

        class FakeStorage:
            async def get_daily_digest_status(self, digest_id):
                return {"last_sent_at": 1774746000, "last_error": ""}

        commands.scheduler = types.SimpleNamespace(
            config=types.SimpleNamespace(
                feeds=[object()],
                jobs=[],
                targets=[object()],
                daily_digests=[digest],
            ),
            last_results={},
            paused_jobs=set(),
            running=True,
            storage=FakeStorage(),
        )
        event = _FakeEvent("/rss list")

        results = [result async for result in commands.rss_list(event)]

        self.assertEqual(len(results), 1)
        self.assertIn("日报任务列表", results[0])
        self.assertIn("digest-1 [启用]", results[0])
        self.assertIn("send=09:00", results[0])


if __name__ == "__main__":
    unittest.main()
