import importlib.util
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch


spec = importlib.util.spec_from_file_location("subscription_compat_helper", Path(__file__).with_name("test_source_probe_compat.py"))
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


class SubscriptionToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.module, _, _, _ = helper._load_main("rss_tool_test")
        self.plugin = object.__new__(self.module.RSSPlugin)
        self.plugin._management_ready = True
        self.plugin._committed_raw = {}
        self.plugin.subscriptions = types.SimpleNamespace(
            snapshot=Mock(return_value={"revision": "rev"}),
            apply_subscription=AsyncMock(return_value={"job_id": "job"}),
            set_enabled=AsyncMock(return_value={"enabled": False}),
        )
        self.event = types.SimpleNamespace(
            is_admin=lambda: True, unified_msg_origin="test:GroupMessage:123",
            get_platform_name=lambda: "aiocqhttp",
        )

    async def test_member_rejected_before_snapshot_or_network(self):
        self.event.is_admin = lambda: False
        result = await self.plugin.rss_manage_subscription(self.event, "create", "https://example.com")
        self.assertIn("权限不足", result)
        self.plugin.subscriptions.snapshot.assert_not_called()

    async def test_current_target_comes_from_event_and_interval_converted(self):
        with patch.object(self.module, "discover_feed", AsyncMock(return_value={"url": "https://example.com/rss", "title": "News"})):
            result = await self.plugin.rss_manage_subscription(self.event, "create", "https://example.com", interval_minutes=120)
        self.assertIn("job", result)
        args = self.plugin.subscriptions.apply_subscription.call_args.kwargs
        self.assertEqual(args["interval_seconds"], 7200)
        self.assertEqual(args["target_ids"], [])
        self.assertEqual(args["current_origin"], self.event.unified_msg_origin)
        self.assertEqual(args["expected_revision"], "rev")

    async def test_cron_and_configured_target_passed_to_validator(self):
        with patch.object(self.module, "discover_feed", AsyncMock(return_value={"url": "https://example.com/rss", "title": "News"})):
            await self.plugin.rss_manage_subscription(self.event, "create", "https://example.com", "target1", cron="0 9 * * *")
        args = self.plugin.subscriptions.apply_subscription.call_args.kwargs
        self.assertEqual(args["target_ids"], ["target1"])
        self.assertEqual(args["cron"], "0 9 * * *")
        self.assertEqual(args["interval_seconds"], 0)

    async def test_discovery_failure_never_writes(self):
        with patch.object(self.module, "discover_feed", AsyncMock(side_effect=TimeoutError)):
            result = await self.plugin.rss_manage_subscription(self.event, "create", "https://example.com", interval_minutes=1)
        self.assertIn("失败", result)
        self.plugin.subscriptions.apply_subscription.assert_not_called()

    async def test_pauses_known_job_with_revision(self):
        await self.plugin.rss_manage_subscription(self.event, "pause", job_id="j1")
        self.plugin.subscriptions.set_enabled.assert_awaited_once_with("jobs", "j1", False, "rev")

    async def test_shutdown_rejects_calls(self):
        self.plugin._management_ready = False
        result = await self.plugin.rss_manage_subscription(self.event, "list")
        self.assertIn("未就绪", result)
        self.plugin.subscriptions.snapshot.assert_not_called()
