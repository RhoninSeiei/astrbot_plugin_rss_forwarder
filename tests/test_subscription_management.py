import asyncio
import copy
import hashlib
import json
import sys
import types
import unittest
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from unittest.mock import AsyncMock, patch


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
PACKAGE_NAME = "astrbot_subscription_management_testpkg"
package_module = types.ModuleType(PACKAGE_NAME)
package_module.__path__ = [str(ROOT)]
sys.modules[PACKAGE_NAME] = package_module


def _load_module(name):
    full_name = f"{PACKAGE_NAME}.{name}"
    spec = spec_from_file_location(full_name, ROOT / f"{name}.py")
    module = module_from_spec(spec)
    sys.modules[full_name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


config_module = _load_module("config")
service_module = _load_module("subscription_management")
RSSConfig = config_module.RSSConfig
SubscriptionManagementService = service_module.SubscriptionManagementService


def _config():
    return {
        "feeds": [{"id": "existing-feed", "url": "https://existing.example/rss"}],
        "targets": [
            {"id": "group", "platform": "qq", "unified_msg_origin": "qq:group:1"},
            {"id": "private", "platform": "qq", "unified_msg_origin": "qq:private:2"},
            {"id": "disabled", "platform": "qq", "enabled": False},
        ],
        "jobs": [{
            "id": "existing-job", "feed_ids": ["existing-feed"],
            "target_ids": ["group"], "interval_seconds": 300,
        }],
        "daily_digests": [{
            "id": "digest", "title": "Daily", "feed_ids": ["existing-feed"],
            "target_ids": ["group"], "enabled": False,
        }],
    }


def _revision(raw):
    canonical = json.dumps(raw, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class SubscriptionManagementTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.raw = _config()
        self.save = AsyncMock()
        self.service = SubscriptionManagementService(self.raw, self.save)

    async def _subscribe(self, **overrides):
        kwargs = {"url": "https://new.example/rss", "target_ids": ["group"],
                  "interval_seconds": 60}
        kwargs.update(overrides)
        return await self.service.apply_subscription(**kwargs)

    async def test_creates_feed_and_job_and_returns_committed_revision(self):
        original = copy.deepcopy(self.raw)
        result = await self._subscribe(title="News")
        self.save.assert_awaited_once()
        candidate = self.save.call_args.args[0]
        parsed = RSSConfig.from_context(copy.deepcopy(candidate))
        self.assertEqual(len(parsed.feeds), 2)
        self.assertEqual(len(parsed.jobs), 2)
        feed = next(f for f in candidate["feeds"] if f["id"] == result["feed_id"])
        job = next(j for j in candidate["jobs"] if j["id"] == result["job_id"])
        self.assertEqual(feed["url"], "https://new.example/rss")
        self.assertEqual(feed["source_type"], "rss")
        self.assertEqual(feed["title"], "News")
        self.assertEqual(job["feed_ids"], [result["feed_id"]])
        self.assertEqual(job["target_ids"], ["group"])
        self.assertEqual(job["interval_seconds"], 60)
        self.assertEqual(result["revision"], _revision(candidate))
        self.assertEqual(result["revision"], self.service.snapshot()["revision"])
        self.assertEqual(self.raw, original)

    async def test_duplicate_subscription_is_idempotent_and_target_order_irrelevant(self):
        first = await self._subscribe(target_ids=["private", "group", "private"])
        second = await self._subscribe(target_ids=["group", "private"], title="Other title")
        self.assertEqual(first, second)
        snapshot = self.service.snapshot()
        self.assertEqual(len(snapshot["feeds"]), 2)
        self.assertEqual(len(snapshot["jobs"]), 2)

    async def test_reuses_existing_feed_and_existing_manually_named_job(self):
        result = await self._subscribe(url="https://existing.example/rss", interval_seconds=300)
        self.assertEqual(result["feed_id"], "existing-feed")
        self.assertEqual(result["job_id"], "existing-job")
        self.assertEqual(result["revision"], _revision(self.raw))

    async def test_same_url_different_schedule_or_targets_creates_only_new_job(self):
        first = await self._subscribe()
        second = await self._subscribe(interval_seconds=120)
        third = await self._subscribe(target_ids=["private"])
        self.assertEqual(first["feed_id"], second["feed_id"])
        self.assertEqual(first["feed_id"], third["feed_id"])
        self.assertEqual(len({r["job_id"] for r in [first, second, third]}), 3)
        self.assertEqual(len(self.service.snapshot()["feeds"]), 2)

    async def test_ids_are_stable_across_instances(self):
        other = SubscriptionManagementService(copy.deepcopy(self.raw), AsyncMock())
        first = await self._subscribe(target_ids=["private", "group"])
        second = await other.apply_subscription(
            url="https://new.example/rss", target_ids=["group", "private"],
            interval_seconds=60,
        )
        self.assertEqual(first, second)
        for name in ("feed_id", "job_id"):
            self.assertRegex(first[name], r"^[a-z]+-[0-9a-f]{16,64}$")

    async def test_valid_cron_and_whitespace_normalization(self):
        for cron in ("*/15 * * * *", "0 9,18 * * 1-5", "0 0 1-31/2 1,6,12 0,7"):
            with self.subTest(cron=cron):
                first = await self._subscribe(interval_seconds=0, cron=cron)
                second = await self._subscribe(interval_seconds=0, cron="  " + cron.replace(" ", "  ") + " ")
                self.assertEqual(first, second)

    async def test_invalid_intervals_and_crons_never_save(self):
        cases = [
            {"interval_seconds": value} for value in (0, -1, 1, 59, 60.0, "60", True, None)
        ] + [
            {"interval_seconds": 0, "cron": value} for value in (
                "bad cron", "* * * *", "* * * * * *", "60 * * * *",
                "0 24 * * *", "0 0 0 * *", "0 0 * 13 *", "0 0 * * 8",
                "*/0 * * * *", "*/-1 * * * *", "9-2 * * * *",
                "1,,2 * * * *", "*/2/3 * * * *", "0 0 * * MON", None,
            )
        ] + [{"interval_seconds": 60, "cron": "* * * * *"}]
        for kwargs in cases:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                await self._subscribe(**kwargs)
        self.save.assert_not_awaited()
        self.assertEqual(self.service.snapshot()["revision"], _revision(self.raw))

    async def test_invalid_urls_never_save(self):
        for url in ("ftp://example.com/rss", "https:///rss", "https://user:password@example.com/rss",
                    "https://user@example.com/rss", "https://@example.com/rss", "https://example.com:bad/rss",
                    "https://[bad/rss", "https://exam ple.com/rss", "https://example.com/\nfeed", None):
            with self.subTest(url=url), self.assertRaises(ValueError):
                await self._subscribe(url=url)
        self.save.assert_not_awaited()

    async def test_targets_must_be_existing_enabled_string_list(self):
        for targets in ([], ["missing"], ["disabled"], ["group", "missing"], "group", [1], [""], None):
            with self.subTest(targets=targets), self.assertRaises(ValueError):
                await self._subscribe(target_ids=targets)
        self.save.assert_not_awaited()

    async def test_current_session_creates_stable_target_and_reuses_it(self):
        for origin in ("qq:group:99", "qq:private:100"):
            with self.subTest(origin=origin):
                first = await self._subscribe(target_ids=[], current_origin=origin, current_platform="qq")
                candidate = self.save.call_args.args[0]
                target = next(t for t in candidate["targets"] if t.get("unified_msg_origin") == origin)
                self.assertTrue(target["enabled"])
                self.assertEqual(target["platform"], "qq")
                job = next(j for j in candidate["jobs"] if j["id"] == first["job_id"])
                self.assertEqual(job["target_ids"], [target["id"]])
                second = await self._subscribe(target_ids=[], current_origin=origin, current_platform="qq")
                self.assertEqual(first, second)
                other = SubscriptionManagementService(_config(), AsyncMock())
                again = await other.apply_subscription(
                    url="https://new.example/rss", target_ids=[], interval_seconds=60,
                    current_origin=origin, current_platform="qq",
                )
                self.assertEqual(first["job_id"], again["job_id"])
        self.assertEqual(len(self.service.snapshot()["targets"]), 5)

    async def test_current_session_reuses_existing_umo(self):
        result = await self._subscribe(target_ids=[], current_origin="qq:group:1", current_platform="qq")
        candidate = self.save.call_args.args[0]
        self.assertEqual(len(candidate["targets"]), 3)
        job = next(j for j in candidate["jobs"] if j["id"] == result["job_id"])
        self.assertEqual(job["target_ids"], ["group"])

    async def test_current_session_requires_valid_context_and_cannot_bypass_disabled_target(self):
        for origin, platform in (("", "qq"), ("qq:group:99", ""), (None, "qq"),
                                 ("bad-origin", "qq"), ("qq::99", "qq"),
                                 ("qq:group:99\n", "qq")):
            with self.subTest(origin=origin, platform=platform), self.assertRaises(ValueError):
                await self._subscribe(target_ids=[], current_origin=origin, current_platform=platform)
        self.raw["targets"][2]["unified_msg_origin"] = "qq:group:disabled"
        service = SubscriptionManagementService(self.raw, self.save)
        with self.assertRaises(ValueError):
            await service.apply_subscription(url="https://new.example/rss", target_ids=[],
                                             interval_seconds=60, current_origin="qq:group:disabled",
                                             current_platform="qq")
        self.save.assert_not_awaited()

    async def test_explicit_targets_do_not_create_current_session_target(self):
        await self._subscribe(current_origin="qq:group:99", current_platform="qq")
        self.assertEqual(len(self.service.snapshot()["targets"]), 3)
        with self.assertRaises(ValueError):
            await self._subscribe(target_ids=["missing"], current_origin="qq:group:99", current_platform="qq")

    async def test_cas_conflicts_reject_both_operations_without_save(self):
        revision = self.service.snapshot()["revision"]
        await self._subscribe(expected_revision=revision)
        self.save.reset_mock()
        with self.assertRaisesRegex(ValueError, "revision"):
            await self._subscribe(expected_revision=revision)
        with self.assertRaisesRegex(ValueError, "revision"):
            await self.service.set_enabled("jobs", "existing-job", False, revision)
        self.save.assert_not_awaited()

    async def test_set_enabled_supports_only_allowed_kinds_and_strict_bool(self):
        for kind, item_id in (("feeds", "existing-feed"), ("jobs", "existing-job"), ("daily_digests", "digest")):
            for enabled in (False, True):
                with self.subTest(kind=kind, enabled=enabled):
                    revision = self.service.snapshot()["revision"]
                    result = await self.service.set_enabled(kind, item_id, enabled, revision)
                    item = next(i for i in self.service.snapshot()[kind] if i["id"] == item_id)
                    self.assertIs(item["enabled"], enabled)
                    self.assertEqual(result["revision"], self.service.snapshot()["revision"])
        self.save.reset_mock()
        for kind, item_id, enabled in (("targets", "group", False), ("unknown", "id", True),
                                      ("jobs", "missing", False), ("jobs", "existing-job", 1),
                                      ("jobs", "existing-job", "false"), ("feeds", "", True)):
            with self.subTest(kind=kind, enabled=enabled), self.assertRaises(ValueError):
                await self.service.set_enabled(kind, item_id, enabled, "")
        self.save.assert_not_awaited()

    async def test_callback_failure_and_mutation_leave_service_and_input_unchanged(self):
        before = copy.deepcopy(self.raw)
        snapshot = self.service.snapshot()

        async def fail(candidate):
            candidate["targets"].clear()
            raise RuntimeError("save failed")

        self.save.side_effect = fail
        for action in (self._subscribe,
                       lambda: self.service.set_enabled("feeds", "existing-feed", False, ""),
                       lambda: self._subscribe(target_ids=[], current_origin="qq:group:99", current_platform="qq")):
            with self.assertRaisesRegex(RuntimeError, "save failed"):
                await action()
            self.assertEqual(self.raw, before)
            self.assertEqual(self.service.snapshot(), snapshot)

    async def test_full_config_validation_precedes_callback_for_both_operations(self):
        self.raw["jobs"][0]["batch_size"] = 0
        service = SubscriptionManagementService(self.raw, self.save)
        with self.assertRaises(config_module.ConfigValidationError):
            await service.apply_subscription(url="https://new.example/rss", target_ids=["group"], interval_seconds=60)
        self.save.assert_not_awaited()
        self.raw["jobs"][0]["batch_size"] = 10
        self.raw["daily_digests"][0]["feed_ids"] = ["missing"]
        service = SubscriptionManagementService(self.raw, self.save)
        with self.assertRaises(config_module.ConfigValidationError):
            await service.set_enabled("daily_digests", "digest", True, "")
        self.save.assert_not_awaited()

    async def test_preserves_existing_proxy_secrets_advanced_fields_and_template_markers(self):
        self.raw["feeds"][0].update({
            "__template_key": "feed", "proxy_url": "socks5://proxy-user:proxy-pass@localhost:1080",
            "key": "feed-secret", "auth_mode": "header", "custom": {"nested": [1, 2]},
        })
        self.raw.update({
            "translation": {"llm_proxy_mode": "custom", "llm_proxy_url": "http://user:pass@localhost:8000"},
            "advanced": {"token": "keep-me", "unknown": [1, {"enabled": True}]},
            "render_mode": "image",
        })
        before = copy.deepcopy(self.raw)
        self.service = SubscriptionManagementService(self.raw, self.save)
        await self._subscribe()
        candidate = self.save.call_args.args[0]
        for key in before:
            if key in {"feeds", "jobs"}:
                self.assertEqual(candidate[key][:len(before[key])], before[key])
            else:
                self.assertEqual(candidate[key], before[key])
        await self.service.set_enabled("jobs", "existing-job", False, "")
        saved = self.save.call_args.args[0]
        self.assertEqual(saved["feeds"][0], before["feeds"][0])
        self.assertEqual(saved["translation"], before["translation"])
        self.assertEqual(self.raw, before)

    async def test_snapshot_is_safe_detached_and_revision_covers_hidden_fields(self):
        self.raw["feeds"][0].update({
            "url": "https://url-user:url-pass@example.com/rss?key=query-secret&x=other-secret#fragment-secret",
            "key": "key-secret", "token": "token-secret", "password": "password-secret",
            "proxy_url": "socks5://proxy-user:proxy-pass@localhost:1080", "headers": {"Authorization": "header-secret"},
            "extra": {"api_key": "nested-secret"},
        })
        self.raw["targets"][0].update({"label": "Group 1", "name": "News group", "token": "target-secret"})
        self.raw["jobs"][0]["password"] = "job-secret"
        self.raw["daily_digests"][0]["prompt_template"] = "prompt-secret"
        self.raw["translation"] = {"google_translate_api_key": "global-secret"}
        self.service = SubscriptionManagementService(self.raw, self.save)
        snapshot = self.service.snapshot()
        self.assertEqual(set(snapshot), {"revision", "feeds", "targets", "jobs", "daily_digests"})
        self.assertEqual(snapshot["revision"], _revision(self.raw))
        serialized = json.dumps(snapshot)
        for secret in ("url-user", "url-pass", "query-secret", "other-secret", "fragment-secret", "key-secret",
                       "token-secret", "password-secret", "proxy-user", "proxy-pass", "header-secret", "nested-secret",
                       "target-secret", "job-secret", "prompt-secret", "global-secret", "proxy_url"):
            self.assertNotIn(secret, serialized)
        self.assertEqual(snapshot["targets"][0]["label"], "Group 1")
        self.assertTrue(snapshot["targets"][0]["enabled"])
        snapshot["feeds"][0]["url"] = "changed"
        snapshot["jobs"][0]["feed_ids"].clear()
        self.assertNotEqual(snapshot, self.service.snapshot())
        self.raw["translation"]["google_translate_api_key"] = "new-secret"
        other = SubscriptionManagementService(self.raw, AsyncMock())
        self.assertNotEqual(other.snapshot()["revision"], self.service.snapshot()["revision"])

    async def test_revision_is_key_order_independent_and_constructor_detaches_input(self):
        reversed_config = dict(reversed(list(self.raw.items())))
        other = SubscriptionManagementService(reversed_config, AsyncMock())
        before = self.service.snapshot()
        self.assertEqual(before["revision"], other.snapshot()["revision"])
        self.raw["targets"][0]["enabled"] = False
        self.assertEqual(before, self.service.snapshot())

    async def test_snapshot_redacts_urls_embedded_in_display_labels(self):
        self.raw["targets"][0]["label"] = "Proxy socks5://label-user:label-pass@localhost:1080"
        self.raw["feeds"][0]["title"] = "News https://example.com/?token=title-secret#title-fragment"
        service = SubscriptionManagementService(self.raw, self.save)
        serialized = json.dumps(service.snapshot())
        for secret in ("label-user", "label-pass", "title-secret", "title-fragment"):
            self.assertNotIn(secret, serialized)

    async def test_collection_mappings_and_legacy_id_strings_are_preserved(self):
        for kind in ("feeds", "targets", "jobs", "daily_digests"):
            self.raw[kind] = {item["id"]: item for item in self.raw[kind]}
        self.raw["jobs"]["existing-job"]["feed_ids"] = "existing-feed"
        self.raw["jobs"]["existing-job"]["target_ids"] = "group"
        self.service = SubscriptionManagementService(self.raw, self.save)
        first = await self._subscribe(url="https://existing.example/rss", interval_seconds=300)
        self.assertEqual(first["job_id"], "existing-job")
        await self._subscribe(target_ids=[], current_origin="qq:group:99", current_platform="qq")
        await self.service.set_enabled(kind="feeds", id="existing-feed", enabled=False)
        candidate = self.save.call_args.args[0]
        for kind in ("feeds", "targets", "jobs", "daily_digests"):
            self.assertIsInstance(candidate[kind], dict)
        self.assertEqual(candidate["jobs"]["existing-job"], self.raw["jobs"]["existing-job"])
        self.assertFalse(candidate["feeds"]["existing-feed"]["enabled"])

    async def test_equivalent_cron_rules_reuse_job(self):
        first = await self._subscribe(interval_seconds=0, cron="*/15 * * * 0")
        second = await self._subscribe(interval_seconds=0, cron="0,15,30,45 * * * 7")
        self.assertEqual(first, second)

    async def test_twitter_feed_with_same_url_is_not_reused_as_rss(self):
        self.raw["feeds"][0].update({"source_type": "twitter", "username": "example"})
        self.service = SubscriptionManagementService(self.raw, self.save)
        result = await self._subscribe(url="https://existing.example/rss")
        self.assertNotEqual(result["feed_id"], "existing-feed")
        self.assertEqual(len(self.service.snapshot()["feeds"]), 2)

    async def test_callback_cannot_mutate_committed_snapshot_after_return(self):
        await self._subscribe()
        before = self.service.snapshot()
        self.save.call_args.args[0]["feeds"].clear()
        self.assertEqual(self.service.snapshot(), before)

    async def test_concurrent_cas_writes_serialize_and_snapshot_stays_committed(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def save(candidate):
            entered.set()
            await release.wait()

        self.save.side_effect = save
        before = self.service.snapshot()
        first = asyncio.create_task(self._subscribe(expected_revision=before["revision"]))
        await entered.wait()
        second = asyncio.create_task(self.service.set_enabled("feeds", "existing-feed", False, before["revision"]))
        self.assertEqual(before, self.service.snapshot())
        release.set()
        results = await asyncio.gather(first, second, return_exceptions=True)
        self.assertIsInstance(results[0], dict)
        self.assertIsInstance(results[1], ValueError)
        self.save.assert_awaited_once()

    async def test_concurrent_subscriptions_without_cas_do_not_lose_updates(self):
        async def save(candidate):
            await asyncio.sleep(0)

        self.save.side_effect = save
        results = await asyncio.gather(*(self._subscribe(url=f"https://new.example/{i}") for i in range(6)))
        self.assertEqual(len({r["feed_id"] for r in results}), 6)
        self.assertEqual(len(self.service.snapshot()["feeds"]), 7)
        self.assertEqual(len(self.service.snapshot()["jobs"]), 7)

    async def test_service_does_not_open_network_connections(self):
        with patch("socket.socket.connect", side_effect=AssertionError("network forbidden")), \
             patch("socket.getaddrinfo", side_effect=AssertionError("DNS forbidden")):
            await self._subscribe()
            await self.service.set_enabled("jobs", "existing-job", False, "")
            self.service.snapshot()

    async def test_drain_waits_for_current_subscription_and_toggle_writes(self):
        for toggle in (False, True):
            with self.subTest(toggle=toggle):
                entered, release, draining = asyncio.Event(), asyncio.Event(), asyncio.Event()

                async def save(candidate):
                    entered.set()
                    await release.wait()

                async def drain():
                    draining.set()
                    await self.service.drain()

                self.save.side_effect = save
                if toggle:
                    write = asyncio.create_task(self.service.set_enabled(
                        kind="jobs", id="existing-job", enabled=False,
                    ))
                else:
                    write = asyncio.create_task(self._subscribe())
                pending_drain = None
                try:
                    await asyncio.wait_for(entered.wait(), 1)
                    pending_drain = asyncio.create_task(drain())
                    await draining.wait()
                    self.assertFalse(pending_drain.done())
                    release.set()
                    result = await asyncio.wait_for(write, 1)
                    await asyncio.wait_for(pending_drain, 1)
                    self.assertEqual(result["revision"], self.service.snapshot()["revision"])
                finally:
                    release.set()
                    await asyncio.gather(write, return_exceptions=True)
                    if pending_drain is not None:
                        await asyncio.gather(pending_drain, return_exceptions=True)

    async def test_drain_returns_when_idle_and_after_failed_write(self):
        await asyncio.wait_for(self.service.drain(), 1)
        self.save.side_effect = RuntimeError("save failed")
        with self.assertRaises(RuntimeError):
            await self._subscribe()
        await asyncio.wait_for(self.service.drain(), 1)


if __name__ == "__main__":
    unittest.main()
