import asyncio
import copy
import json
import sys
import tempfile
import types
import unittest
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch


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
PACKAGE_NAME = "astrbot_subscription_runtime_testpkg"
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


RSSConfig = _load_module("config").RSSConfig
FeedParser = _load_module("parser").FeedParser
FeedStorage = _load_module("storage").FeedStorage
fetcher_module = _load_module("fetcher")
RSSScheduler = _load_module("scheduler").RSSScheduler
runtime_module = _load_module("subscription_runtime")
apply_runtime = runtime_module.apply_runtime


def _config():
    return {
        "feeds": [{"id": "old-feed", "url": "https://old.example/rss", "max_new_items": 1}],
        "targets": [
            {"id": "group", "platform": "qq", "unified_msg_origin": "qq:group:1"},
            {"id": "private", "platform": "qq", "unified_msg_origin": "qq:private:2"},
        ],
        "jobs": [{"id": "old-job", "feed_ids": ["old-feed"], "target_ids": ["group"],
                  "interval_seconds": 300}],
        "dedup_ttl_seconds": 3600,
        "subscription_proxy_url": "socks5://test-user:test-password@localhost:1080",
        "advanced": {"preserve": [1, 2]},
    }


def _rss(feed_id, count=3):
    items = "".join(
        f"<item><guid>{feed_id}-{i}</guid><title>Item {i}</title>"
        f"<link>https://example.com/{feed_id}/{i}</link></item>" for i in range(count)
    )
    return f"<rss version='2.0'><channel><title>News</title>{items}</channel></rss>"


class SavingConfig(dict):
    def __init__(self, raw, path, events):
        super().__init__(copy.deepcopy(raw))
        self.path, self.events = path, events
        self.failures = []
        self.sync_calls = 0
        self.path.write_text(json.dumps(self), encoding="utf-8")

    def _persist(self):
        self.events.append("save")
        failure = self.failures.pop(0) if self.failures else None
        if failure == "before":
            raise OSError("save failed before write")
        if failure is False:
            return False
        self.path.write_text(json.dumps(self), encoding="utf-8")
        if failure == "after":
            raise OSError("save failed after write")
        return True

    def save_config(self):
        self.sync_calls += 1
        self._persist()

    def persisted(self):
        return json.loads(self.path.read_text(encoding="utf-8"))


class AsyncSavingConfig(SavingConfig):
    def __init__(self, *args):
        super().__init__(*args)
        self.async_calls = 0
        self.hook = None

    async def save_config_async(self):
        self.async_calls += 1
        if self.hook is not None:
            await self.hook()
        return self._persist()


class TrackedScheduler(RSSScheduler):
    def __init__(self, raw, storage, events, name):
        config = RSSConfig.from_context(copy.deepcopy(raw))
        super().__init__(config, fetcher_module.FeedFetcher(config, storage),
                         FeedParser(), types.SimpleNamespace(), storage)
        self.events, self.name = events, name
        self.active = name == "old"
        self.start_error = None
        self.stop_error = None
        self.stop_hook = None

        async def fetch_one(feed, **kwargs):
            self.events.append("fetch:" + feed.id)
            return fetcher_module.FetchedFeed(feed.id, _rss(feed.id), "new-etag", "new-date", 200)

        self._fetcher._fetch_single_feed = AsyncMock(side_effect=fetch_one)
        self._parser.parse = Mock(wraps=self._parser.parse)

    async def start(self):
        self.events.append(self.name + ":start")
        self.active = True
        if self.start_error:
            raise self.start_error

    async def stop(self):
        self.events.append(self.name + ":stop")
        self.active = False
        if self.stop_hook is not None:
            await self.stop_hook()
        if self.stop_error:
            raise self.stop_error


class SubscriptionRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.events = []
        self.original = _config()
        self.raw = AsyncSavingConfig(self.original, self.root / "config.json", self.events)
        self.storage = FeedStorage(storage_dir=self.root / "storage")
        original_mark = self.storage.mark_seen

        async def mark(key, ttl_seconds=86400):
            self.events.append("seed:" + key)
            await original_mark(key, ttl_seconds=ttl_seconds)

        self.storage.mark_seen = AsyncMock(side_effect=mark)
        self.old = TrackedScheduler(self.original, self.storage, self.events, "old")
        self.new = None
        self.tune = lambda scheduler: None

        def make(raw):
            self.new = TrackedScheduler(raw, self.storage, self.events, "new")
            self.tune(self.new)
            return self.new

        self.plugin = types.SimpleNamespace(
            _raw_config=self.raw, _committed_raw=copy.deepcopy(self.original),
            _management_ready=True, scheduler=self.old, _make_scheduler=Mock(side_effect=make),
            _source_probe_api=types.SimpleNamespace(_config=self.old._config),
        )
        self.candidate = copy.deepcopy(self.original)
        self.candidate["feeds"].append({"id": "new-feed", "url": "https://new.example/rss", "max_new_items": 1})
        self.candidate["jobs"].append({
            "id": "new-job", "feed_ids": ["old-feed", "new-feed"], "target_ids": ["group", "private"],
            "interval_seconds": 60, "batch_size": 1, "dedup_ttl_seconds": 120,
        })

    def assert_restored(self):
        self.assertIs(self.plugin._raw_config, self.raw)
        self.assertEqual(dict(self.raw), self.original)
        self.assertEqual(self.raw.persisted(), self.original)
        self.assertEqual(self.plugin._committed_raw, self.original)
        self.assertIs(self.plugin.scheduler, self.old)
        self.assertTrue(self.old.active)
        self.assertIs(self.plugin._source_probe_api._config, self.old._config)

    async def test_success_seeds_all_items_and_origins_with_job_ttl_before_start(self):
        candidate_before = copy.deepcopy(self.candidate)
        old_key = self.old._target_seen_key("old-job", "qq:group:1", "old-feed-0")
        await self.storage.mark_seen(old_key, ttl_seconds=777)
        old_record = copy.deepcopy(await self.storage.get(self.storage._content_key(old_key)))
        self.events.clear()
        self.storage.mark_seen.reset_mock()
        await apply_runtime(self.plugin, self.candidate)
        self.assertEqual(self.events[:3], ["fetch:old-feed", "fetch:new-feed", "old:stop"])
        self.assertEqual(self.events[-2:], ["save", "new:start"])
        self.assertIs(self.plugin.scheduler, self.new)
        self.assertFalse(self.old.active)
        self.assertTrue(self.new.active)
        self.assertEqual(dict(self.raw), self.candidate)
        self.assertEqual(self.raw.persisted(), self.candidate)
        self.assertEqual(self.plugin._committed_raw, self.candidate)
        self.assertIsNot(self.plugin._committed_raw, self.candidate)
        self.assertIs(self.plugin._source_probe_api._config, self.new._config)
        self.assertEqual(self.raw.async_calls, 1)
        self.assertEqual(self.raw.sync_calls, 0)
        self.assertEqual(self.candidate, candidate_before)
        self.assertEqual(old_record, await self.storage.get(self.storage._content_key(old_key)))
        job = self.new._config.jobs[-1]
        for feed_id in job.feed_ids:
            for i in range(3):
                item = {"guid": f"{feed_id}-{i}", "link": f"https://example.com/{feed_id}/{i}"}
                for origin in ("qq:group:1", "qq:private:2"):
                    for key in self.storage.build_seen_keys(item):
                        scoped = self.new._target_seen_key(job.id, origin, key)
                        self.assertTrue(await self.storage.has_seen(scoped))
                        record = await self.storage.get(self.storage._content_key(scoped))
                        self.assertEqual(record["expire_at"] - record["updated_at"], 120)
                self.assertFalse(await self.storage.has_seen(item["guid"]))
        self.assertEqual(self.storage.mark_seen.await_count, 24)
        self.assertTrue(all(call.kwargs["ttl_seconds"] == 120 for call in self.storage.mark_seen.await_args_list))

    async def test_identical_candidate_is_noop_but_still_checks_gui_conflict(self):
        await apply_runtime(self.plugin, copy.deepcopy(self.original))
        self.plugin._make_scheduler.assert_not_called()
        self.assertEqual(self.events, [])
        self.raw["advanced"]["gui"] = True
        with self.assertRaises(ValueError):
            await apply_runtime(self.plugin, copy.deepcopy(self.original))
        self.assertEqual(self.raw["advanced"]["gui"], True)

    async def test_not_ready_and_gui_conflicts_fail_before_factory_or_save(self):
        self.plugin._management_ready = False
        with self.assertRaises(RuntimeError):
            await apply_runtime(self.plugin, self.candidate)
        self.plugin._management_ready = True
        self.raw["advanced"]["gui"] = True
        with self.assertRaises(ValueError):
            await apply_runtime(self.plugin, self.candidate)
        self.plugin._make_scheduler.assert_not_called()
        self.assertEqual(self.events, [])

    async def test_bootstrap_forces_unconditional_fetch_without_changing_http_state(self):
        for feed_id in ("old-feed", "new-feed"):
            await self.storage.update_feed_state(feed_id, etag="cached-etag", last_modified="cached-date")
        before = {feed_id: await self.storage.get_feed_state(feed_id) for feed_id in ("old-feed", "new-feed")}
        headers = []

        def request(**kwargs):
            headers.append(kwargs["headers"])
            status = 304 if "If-None-Match" in kwargs["headers"] else 200
            body = _rss("new-feed" if "new.example" in kwargs["url"] else "old-feed").encode()
            return types.SimpleNamespace(body=body, status=status, headers={})

        def tune(scheduler):
            del scheduler._fetcher._fetch_single_feed

        self.tune = tune
        with patch.object(fetcher_module, "request_source", side_effect=request):
            await apply_runtime(self.plugin, self.candidate)
        self.assertEqual(len(headers), 2)
        for header in headers:
            self.assertNotIn("If-None-Match", header)
            self.assertNotIn("If-Modified-Since", header)
        for feed_id, state in before.items():
            self.assertEqual(await self.storage.get_feed_state(feed_id), state)
        self.assertIs(self.new._fetcher._storage, self.storage)
        self.assertIs(self.old._fetcher._storage, self.storage)

    async def test_missing_feed_and_non_200_responses_abort_before_old_stop(self):
        for response in (None, fetcher_module.FetchedFeed("new-feed", "", "", "", 304),
                         fetcher_module.FetchedFeed("new-feed", _rss("new-feed"), "", "", 503)):
            with self.subTest(response=response):
                def tune(s):
                    s._fetcher._fetch_single_feed.side_effect = None
                    s._fetcher._fetch_single_feed.return_value = response
                self.tune = tune
                with self.assertRaises(ValueError):
                    await apply_runtime(self.plugin, self.candidate)
                self.assertNotIn("old:stop", self.events)
                self.assertEqual(self.storage.mark_seen.await_count, 0)
                self.assert_restored()

    async def test_parser_gets_unlimited_raw_items_for_new_jobs_only(self):
        await apply_runtime(self.plugin, self.candidate)
        self.assertEqual(self.new._parser.parse.call_count, 1)
        raw, job = self.new._parser.parse.call_args.args
        self.assertEqual(job.id, "new-job")
        self.assertEqual({item["feed_id"] for item in raw}, {"old-feed", "new-feed"})
        self.assertTrue(all(item["max_new_items"] == 0 for item in raw))
        self.assertTrue(all(feed.max_new_items == 1 for feed in self.new._config.feeds))

    async def test_existing_job_toggle_does_not_fetch_or_seed_and_global_ttl_applies(self):
        toggle = copy.deepcopy(self.original)
        toggle["jobs"][0]["enabled"] = False
        await apply_runtime(self.plugin, toggle)
        self.assertFalse(any(event.startswith("fetch:") for event in self.events))
        self.storage.mark_seen.assert_not_awaited()
        self.candidate["jobs"][-1]["dedup_ttl_seconds"] = 0
        await apply_runtime(self.plugin, self.candidate)
        self.assertTrue(all(call.kwargs["ttl_seconds"] == 3600 for call in self.storage.mark_seen.await_args_list))

    async def test_valid_empty_feed_is_allowed_but_invalid_xml_is_rejected(self):
        for body in ("", "<rss>", "<html><body>Not a feed</body></html>"):
            with self.subTest(body=body):
                def tune(s):
                    async def fetch(feed, **kwargs):
                        return fetcher_module.FetchedFeed(feed.id, body, "", "", 200)
                    s._fetcher._fetch_single_feed.side_effect = fetch
                self.tune = tune
                with self.assertRaises(ValueError):
                    await apply_runtime(self.plugin, self.candidate)
                self.assertNotIn("old:stop", self.events)
                self.assert_restored()
        self.tune = lambda s: setattr(s._fetcher._fetch_single_feed, "side_effect", lambda feed, **kw:
                                     fetcher_module.FetchedFeed(feed.id, _rss(feed.id, 0), "", "", 200))
        await apply_runtime(self.plugin, self.candidate)
        self.storage.mark_seen.assert_not_awaited()
        self.assertTrue(self.new.active)

    async def test_save_failure_restores_memory_disk_and_old_scheduler(self):
        for failure in ("before", "after", False):
            with self.subTest(failure=failure):
                self.raw.failures = [failure]
                with self.assertRaises((OSError, RuntimeError)):
                    await apply_runtime(self.plugin, self.candidate)
                self.assert_restored()
                self.assertFalse(self.new.active)
                self.assertNotIn("new:start", self.events)
                self.assertEqual(self.events[-3:], ["new:stop", "save", "old:start"])

    async def test_new_start_failure_stops_partial_new_and_restores_persisted_old_config(self):
        self.tune = lambda s: setattr(s, "start_error", RuntimeError("new start failed"))
        with self.assertRaisesRegex(RuntimeError, "new start failed"):
            await apply_runtime(self.plugin, self.candidate)
        self.assert_restored()
        self.assertFalse(self.new.active)
        self.assertEqual(self.events[-5:], ["save", "new:start", "new:stop", "save", "old:start"])

    async def test_seed_failure_restarts_old_without_persisting_candidate(self):
        self.storage.mark_seen.side_effect = OSError("seed failed")
        with self.assertRaisesRegex(OSError, "seed failed"):
            await apply_runtime(self.plugin, self.candidate)
        self.assert_restored()
        self.assertEqual(self.raw.async_calls, 0)
        self.assertFalse(self.new.active)

    async def test_old_stop_failure_attempts_recovery_without_saving(self):
        self.old.stop_error = RuntimeError("old stop failed")
        with self.assertRaisesRegex(RuntimeError, "old stop failed"):
            await apply_runtime(self.plugin, self.candidate)
        self.assert_restored()
        self.assertEqual(self.raw.async_calls, 0)

    async def test_gui_edit_during_fetch_is_not_overwritten(self):
        async def fetch(feed, **kwargs):
            self.raw["advanced"]["gui"] = True
            self.raw._persist()
            return fetcher_module.FetchedFeed(feed.id, _rss(feed.id), "", "", 200)
        self.tune = lambda s: setattr(s._fetcher._fetch_single_feed, "side_effect", fetch)
        with self.assertRaises(ValueError):
            await apply_runtime(self.plugin, self.candidate)
        self.assertNotIn("old:stop", self.events)
        self.assertTrue(self.old.active)
        self.assertTrue(self.raw["advanced"]["gui"])
        self.assertEqual(dict(self.raw), self.raw.persisted())
        self.assertEqual(self.plugin._committed_raw, self.original)

    async def test_gui_edit_after_stop_before_persist_is_not_overwritten(self):
        async def edit():
            self.raw["advanced"]["gui"] = True
            self.raw._persist()
        self.old.stop_hook = edit
        with self.assertRaises(ValueError):
            await apply_runtime(self.plugin, self.candidate)
        self.assertTrue(self.old.active)
        self.assertTrue(self.raw["advanced"]["gui"])
        self.assertEqual(dict(self.raw), self.raw.persisted())
        self.assertEqual(self.raw.async_calls, 0)

    async def test_termination_during_fetch_aborts_without_stopping_old_scheduler(self):
        async def fetch(feed, **kwargs):
            self.plugin._management_ready = False
            return fetcher_module.FetchedFeed(feed.id, _rss(feed.id), "", "", 200)
        self.tune = lambda s: setattr(s._fetcher._fetch_single_feed, "side_effect", fetch)
        with self.assertRaises(RuntimeError):
            await apply_runtime(self.plugin, self.candidate)
        self.assertNotIn("old:stop", self.events)
        self.assert_restored()

    async def test_sync_save_fallback_and_missing_source_probe_api(self):
        raw = SavingConfig(self.original, self.root / "sync.json", self.events)
        self.plugin._raw_config = raw
        del self.plugin._source_probe_api
        await apply_runtime(self.plugin, self.candidate)
        self.assertEqual(raw.sync_calls, 1)
        self.assertEqual(raw.persisted(), self.candidate)

    async def test_failed_persistent_rollback_is_reported_and_disables_management(self):
        self.tune = lambda s: setattr(s, "start_error", RuntimeError("new start failed"))
        self.raw.failures = [None, "before"]
        with self.assertRaisesRegex(RuntimeError, "rollback"):
            await apply_runtime(self.plugin, self.candidate)
        self.assertFalse(self.plugin._management_ready)
        self.assertEqual(dict(self.raw), self.original)
        self.assertEqual(self.raw.persisted(), self.candidate)
        self.assertFalse(self.new.active)
        self.assertTrue(self.old.active)

    async def test_cancelled_async_save_settles_before_rollback(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def wait_once():
            self.raw.hook = None
            entered.set()
            await release.wait()
        self.raw.hook = wait_once
        write = asyncio.create_task(apply_runtime(self.plugin, self.candidate))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            write.cancel()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await write
            self.assert_restored()
            self.assertFalse(self.new.active)
        finally:
            release.set()
            await asyncio.gather(write, return_exceptions=True)

    async def test_preserves_pause_and_execution_results_for_retained_ids_before_start(self):
        digest = {"id": "digest", "title": "Daily", "feed_ids": ["old-feed"],
                  "target_ids": ["group"], "enabled": False}
        self.original["daily_digests"] = [digest]
        self.raw["daily_digests"] = copy.deepcopy([digest])
        self.raw._persist()
        self.plugin._committed_raw = copy.deepcopy(self.original)
        self.candidate["daily_digests"] = copy.deepcopy([digest])
        self.old._config.daily_digests = RSSConfig.from_context(copy.deepcopy(self.original)).daily_digests
        self.old._paused_jobs = {"old-job", "removed-job"}
        self.old._job_results = {"old-job": {"count": 3}, "removed-job": {"count": 9}}
        self.old._digest_results = {"digest": {"count": 2}, "removed-digest": {"count": 8}}
        observed = []

        def tune(scheduler):
            start = scheduler.start
            async def check_start():
                observed.append((set(scheduler._paused_jobs), copy.deepcopy(scheduler._job_results),
                                 copy.deepcopy(scheduler._digest_results)))
                self.assertFalse(scheduler.get_job("old-job").enabled)
                await start()
            scheduler.start = check_start

        self.tune = tune
        await apply_runtime(self.plugin, self.candidate)
        self.assertEqual(observed, [({"old-job"}, {"old-job": {"count": 3}}, {"digest": {"count": 2}})])
        self.assertTrue(self.new.get_job("old-job").enabled)
        self.assertIsNot(self.new._paused_jobs, self.old._paused_jobs)
        self.assertIsNot(self.new._job_results, self.old._job_results)
        self.assertIsNot(self.new._digest_results, self.old._digest_results)

    async def test_real_scheduler_does_not_register_retained_paused_job(self):
        self.old._paused_jobs.add("old-job")
        self.tune = lambda s: setattr(s, "start", types.MethodType(RSSScheduler.start, s))
        try:
            await apply_runtime(self.plugin, self.candidate)
            self.assertNotIn("old-job", self.new._job_tasks)
            self.assertIn("new-job", self.new._job_tasks)
            self.assertEqual(self.new.paused_jobs, {"old-job"})
            self.assertTrue(self.new.get_job("old-job").enabled)
        finally:
            if self.new is not None:
                await RSSScheduler.stop(self.new)


if __name__ == "__main__":
    unittest.main()
