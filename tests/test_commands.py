import asyncio
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
FeedConfig = config_module.FeedConfig


class _FakeEvent:
    def __init__(self, message: str):
        self.message_str = message

    def plain_result(self, text: str):
        return text


class _ProbeReport:
    def __init__(self, payload=None):
        self._payload = payload or {
            "feed_id": "feed-1",
            "source_type": "rss",
            "attempts": [],
            "recommendation": {
                "code": "direct_strict",
                "verify_ssl": True,
                "use_proxy": False,
                "message": "默认网络与严格证书校验可用。",
            },
        }

    def as_dict(self):
        return dict(self._payload)


class _ProbeService:
    def __init__(self, report=None):
        self.report = report or _ProbeReport()
        self.calls = []

    async def probe(self, feed):
        self.calls.append(feed)
        return self.report


class CommandsTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _probe_commands(service, feeds):
        commands = RSSCommands(source_probe_service=service)
        commands.scheduler = types.SimpleNamespace(
            config=types.SimpleNamespace(feeds=feeds),
        )
        return commands

    async def test_rss_probe_is_in_router_map(self):
        feed = FeedConfig(
            id="feed-1",
            url="https://feeds.example/rss.xml",
        )
        service = _ProbeService()
        commands = self._probe_commands(service, [feed])

        results = [
            result
            async for result in commands.rss_router(
                _FakeEvent("/rss probe feed-1")
            )
        ]

        self.assertEqual(service.calls, [feed])
        self.assertEqual(len(results), 1)

    async def test_rss_probe_without_feed_id_shows_exact_usage(self):
        commands = self._probe_commands(_ProbeService(), [])

        results = [
            result
            async for result in commands.rss_router(_FakeEvent("/rss probe"))
        ]

        self.assertEqual(results, ["用法：/rss probe <feed_id>"])

    async def test_rss_probe_unknown_feed_id_returns_concise_message(self):
        commands = self._probe_commands(_ProbeService(), [])

        results = [
            result
            async for result in commands.rss_router(
                _FakeEvent("/rss probe missing-feed")
            )
        ]

        self.assertEqual(results, ["未找到指定来源。"])

    async def test_rss_probe_formats_attempts_and_final_recommendation(self):
        payload = {
            "feed_id": "feed-1",
            "source_type": "rss",
            "attempts": [
                {
                    "mode": "direct_strict",
                    "ok": True,
                    "http_status": 200,
                    "content_type": "application/rss+xml",
                    "latency_ms": 18,
                    "is_feed": True,
                    "feed_kind": "rss",
                    "truncated": False,
                    "error_type": "",
                    "error_message": "",
                },
                {
                    "mode": "proxy_strict",
                    "ok": False,
                    "http_status": 502,
                    "content_type": "text/html",
                    "latency_ms": 27,
                    "is_feed": False,
                    "feed_kind": "unknown",
                    "truncated": False,
                    "error_type": "proxy",
                    "error_message": "proxy failed",
                },
            ],
            "recommendation": {
                "code": "direct_strict",
                "verify_ssl": True,
                "use_proxy": False,
                "message": "默认网络与严格证书校验可用。",
            },
        }
        feed = FeedConfig(id="feed-1", url="https://feeds.example/rss.xml")
        commands = self._probe_commands(
            _ProbeService(_ProbeReport(payload)),
            [feed],
        )

        results = [
            result
            async for result in commands.rss_probe(
                _FakeEvent("/rss probe feed-1")
            )
        ]
        lines = results[0].splitlines()

        self.assertIn(
            "direct_strict：成功 延迟=18ms HTTP=200 内容=已识别(rss) 分类错误=-",
            lines,
        )
        self.assertIn(
            "proxy_strict：失败 延迟=27ms HTTP=502 内容=未识别 分类错误=proxy",
            lines,
        )
        self.assertEqual(
            lines[-1],
            "建议：默认网络与严格证书校验可用。 "
            "code=direct_strict verify_ssl=true use_proxy=false",
        )

    async def test_rss_probe_output_omits_secrets_proxy_address_and_full_query(self):
        secret = "source-secret-key"
        proxy_address = "proxy.internal.example:8443"
        full_query = "?auth=source-secret-key&format=rss"
        feed_id = f"feed{full_query}&proxy={proxy_address}"
        payload = {
            "feed_id": feed_id,
            "source_type": "rss",
            "attempts": [
                {
                    "mode": "proxy_strict",
                    "ok": False,
                    "http_status": None,
                    "content_type": "",
                    "latency_ms": 31,
                    "is_feed": False,
                    "feed_kind": "unknown",
                    "truncated": False,
                    "error_type": "proxy",
                    "error_message": (
                        f"https://feeds.example/rss.xml{full_query} via "
                        f"http://operator:proxy-password@{proxy_address}"
                    ),
                }
            ],
            "recommendation": {
                "code": "unreachable",
                "verify_ssl": None,
                "use_proxy": None,
                "message": (
                    f"来源代理访问失败：http://operator:proxy-password@{proxy_address}"
                ),
            },
        }
        feed = FeedConfig(
            id=feed_id,
            url=f"https://feeds.example/rss.xml{full_query}",
            auth_mode="query",
            key=secret,
            proxy_url=f"http://operator:proxy-password@{proxy_address}",
        )
        commands = self._probe_commands(
            _ProbeService(_ProbeReport(payload)),
            [feed],
        )

        results = [
            result
            async for result in commands.rss_probe(
                _FakeEvent(f"/rss probe {feed_id}")
            )
        ]
        message = results[0]

        self.assertNotIn(secret, message)
        self.assertNotIn("proxy-password", message)
        self.assertNotIn(proxy_address, message)
        self.assertNotIn(full_query, message)

    async def test_rss_probe_serializes_concurrent_calls_per_command_service(self):
        entered = asyncio.Event()
        release = asyncio.Event()

        class SerialProbeService:
            def __init__(self):
                self.calls = 0
                self.active = 0
                self.max_active = 0

            async def probe(self, feed):
                self.calls += 1
                self.active += 1
                self.max_active = max(self.max_active, self.active)
                if self.calls == 1:
                    entered.set()
                    await release.wait()
                self.active -= 1
                return _ProbeReport()

        service = SerialProbeService()
        feed = FeedConfig(id="feed-1", url="https://feeds.example/rss.xml")
        commands = self._probe_commands(service, [feed])

        async def run_probe():
            return [
                result
                async for result in commands.rss_probe(
                    _FakeEvent("/rss probe feed-1")
                )
            ]

        first = asyncio.create_task(run_probe())
        await asyncio.wait_for(entered.wait(), timeout=1)
        second = asyncio.create_task(run_probe())
        await asyncio.sleep(0)

        self.assertEqual(service.calls, 1)
        self.assertFalse(second.done())
        release.set()
        await asyncio.gather(first, second)
        self.assertEqual(service.calls, 2)
        self.assertEqual(service.max_active, 1)

    async def test_rss_probe_cancellation_waits_before_releasing_reusable_lock(self):
        entered = asyncio.Event()
        release = asyncio.Event()

        class CancellationProbeService:
            def __init__(self):
                self.calls = 0
                self.active = 0
                self.max_active = 0

            async def probe(self, feed):
                self.calls += 1
                call_number = self.calls
                self.active += 1
                self.max_active = max(self.max_active, self.active)
                try:
                    if call_number == 1:
                        entered.set()
                        await release.wait()
                    await asyncio.sleep(0)
                    return _ProbeReport()
                finally:
                    self.active -= 1

        service = CancellationProbeService()
        feed = FeedConfig(id="feed-1", url="https://feeds.example/rss.xml")
        commands = self._probe_commands(service, [feed])

        async def run_probe():
            return [
                result
                async for result in commands.rss_probe(
                    _FakeEvent("/rss probe feed-1")
                )
            ]

        first = asyncio.create_task(run_probe())
        await asyncio.wait_for(entered.wait(), timeout=1)
        first.cancel()
        second = asyncio.create_task(run_probe())
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        outcomes = None
        try:
            self.assertFalse(first.done())
            self.assertEqual(service.calls, 1)
            self.assertFalse(second.done())
        finally:
            release.set()
            outcomes = await asyncio.gather(
                first,
                second,
                return_exceptions=True,
            )

        self.assertIsInstance(outcomes[0], asyncio.CancelledError)
        self.assertEqual(len(outcomes[1]), 1)
        self.assertEqual(service.calls, 2)
        self.assertEqual(service.max_active, 1)

        third = await run_probe()

        self.assertEqual(len(third), 1)
        self.assertEqual(service.calls, 3)
        self.assertEqual(service.max_active, 1)
        self.assertFalse(commands._rss_probe_lock.locked())

    async def test_rss_probe_returns_fixed_message_when_service_raises(self):
        class FailingProbeService:
            async def probe(self, feed):
                raise RuntimeError("probe-secret-detail")

        feed = FeedConfig(id="feed-1", url="https://feeds.example/rss.xml")
        commands = self._probe_commands(FailingProbeService(), [feed])

        try:
            results = [
                result
                async for result in commands.rss_probe(
                    _FakeEvent("/rss probe feed-1")
                )
            ]
        except Exception as exc:
            self.fail(f"普通探测异常逸出：{type(exc).__name__}")

        self.assertEqual(results, ["来源探测失败，请稍后重试。"])
        self.assertNotIn("probe-secret-detail", results[0])

    async def test_rss_probe_returns_fixed_message_when_report_serialization_raises(self):
        class SerializationReport:
            def as_dict(self):
                raise RuntimeError("report-secret-detail")

        feed = FeedConfig(id="feed-1", url="https://feeds.example/rss.xml")
        commands = self._probe_commands(
            _ProbeService(SerializationReport()),
            [feed],
        )

        try:
            results = [
                result
                async for result in commands.rss_probe(
                    _FakeEvent("/rss probe feed-1")
                )
            ]
        except Exception as exc:
            self.fail(f"普通报告异常逸出：{type(exc).__name__}")

        self.assertEqual(results, ["来源探测失败，请稍后重试。"])
        self.assertNotIn("report-secret-detail", results[0])

    async def test_rss_probe_returns_fixed_message_when_formatting_raises(self):
        class FormattingPayload(dict):
            def get(self, key, default=None):
                raise RuntimeError("format-secret-detail")

        class FormattingReport:
            def as_dict(self):
                return FormattingPayload({"attempts": []})

        feed = FeedConfig(id="feed-1", url="https://feeds.example/rss.xml")
        commands = self._probe_commands(
            _ProbeService(FormattingReport()),
            [feed],
        )

        try:
            results = [
                result
                async for result in commands.rss_probe(
                    _FakeEvent("/rss probe feed-1")
                )
            ]
        except Exception as exc:
            self.fail(f"普通格式化异常逸出：{type(exc).__name__}")

        self.assertEqual(results, ["来源探测失败，请稍后重试。"])
        self.assertNotIn("format-secret-detail", results[0])

    async def test_rss_help_describes_probe_as_non_persistent_connectivity_check(self):
        commands = RSSCommands()

        results = [
            result
            async for result in commands.rss_router(_FakeEvent("/rss help"))
        ]

        self.assertIn("/rss probe <feed_id>", results[0])
        self.assertIn("仅检查连接，不保存设置", results[0])

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
