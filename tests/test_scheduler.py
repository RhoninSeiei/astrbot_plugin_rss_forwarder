import asyncio
import hashlib
import sys
import tempfile
import types
import unittest
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from unittest import mock


astrbot_module = types.ModuleType("astrbot")
astrbot_api_module = types.ModuleType("astrbot.api")
astrbot_api_module.logger = types.SimpleNamespace(
    info=lambda *a, **k: None,
    warning=lambda *a, **k: None,
    error=lambda *a, **k: None,
    exception=lambda *a, **k: None,
)
sys.modules.setdefault("astrbot", astrbot_module)
sys.modules["astrbot.api"] = astrbot_api_module

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "astrbot_rss_testpkg"
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


_load_module("config")
dispatcher_module = _load_module("dispatcher")
_load_module("fetcher")
_load_module("parser")
_load_module("pipeline")
_load_module("storage")
config_module = sys.modules[f"{PACKAGE_NAME}.config"]
RSSScheduler = _load_module("scheduler").RSSScheduler
DispatchResult = dispatcher_module.DispatchResult
RSSConfig = config_module.RSSConfig
DailyDigestConfig = config_module.DailyDigestConfig


class SchedulerSubscriptionBootstrapTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.storage_dir = temp.name
        self.storage = sys.modules[f"{PACKAGE_NAME}.storage"].FeedStorage(storage_dir=temp.name)
        self.raw = {
            "feeds": [{"id": "feed", "url": "https://example.com/rss", "max_new_items": 1}],
            "targets": [
                {"id": "old", "platform": "qq", "unified_msg_origin": "qq:group:1"},
                {"id": "new", "platform": "qq", "unified_msg_origin": "qq:group:2"},
            ],
            "jobs": [{"id": "job", "feed_ids": ["feed"], "target_ids": ["old"],
                      "batch_size": 1, "interval_seconds": 300}],
        }
        self.guids = ["history-1", "history-2", "history-3"]
        self.status = 200
        self.etag = "fresh-validator"
        self.body = None
        self.feed_bodies = {}
        self.requests = []
        self.delivered = []

    def scheduler(self, storage=None):
        config = RSSConfig.from_context(self.raw)
        owner = self

        class Fetcher:
            def __init__(self):
                self._config = config
                self._storage = storage or owner.storage

            async def fetch(self, job):
                result = []
                for feed in self._config.feeds:
                    if feed.id not in job.feed_ids:
                        continue
                    state = await self._storage.get_feed_state(feed.id)
                    owner.requests.append(dict(state))
                    body = owner.feed_bodies.get(feed.id, owner.body)
                    if body is None:
                        body = "<rss><channel>" + "".join(
                            f"<item><guid>{guid}</guid><title>{guid}</title></item>"
                            for guid in owner.guids
                        ) + "</channel></rss>"
                    result.append({"feed_id": feed.id, "body": body, "status": owner.status,
                                   "etag": owner.etag, "max_new_items": feed.max_new_items})
                return result

        async def dispatch(item):
            self.delivered.append(item)
            return DispatchResult(success_count=1, success_origins=item["_target_origins"])

        return RSSScheduler(config, Fetcher(), sys.modules[f"{PACKAGE_NAME}.parser"].FeedParser(),
                            types.SimpleNamespace(dispatch=mock.AsyncMock(side_effect=dispatch),
                                                  dispatch_daily_digest=mock.AsyncMock(side_effect=dispatch)),
                            storage or self.storage)

    async def run_job(self, scheduler, job_id="job"):
        await scheduler.run_job_once(job_id)
        self.assertEqual(scheduler.last_results[job_id].error_summary, "")

    async def establish_old(self):
        await self.storage.update_feed_state("feed", bootstrap_done=True, last_success_time=1,
                                             etag="old-validator", since_id="100")
        await self.run_job(self.scheduler())
        self.delivered.clear()

    async def test_standard_config_new_target_seeds_full_snapshot_without_affecting_old_target(self):
        await self.establish_old()
        self.raw["jobs"][0]["target_ids"].append("new")
        scheduler = self.scheduler()
        await self.run_job(scheduler)
        self.assertEqual(self.delivered, [])
        self.assertNotIn("etag", self.requests[-1])
        self.assertEqual(self.requests[-1]["since_id"], "0")
        for guid in self.guids:
            self.assertTrue(await self.storage.has_seen(
                scheduler._bootstrap_seen_keys(scheduler.get_job("job"), "qq:group:2", "feed", [guid])[0]))
        self.assertFalse(await self.storage.has_seen(
            scheduler._target_seen_key("job", "qq:group:1", "history-2")))
        self.assertFalse(await self.storage.has_seen("history-2"))

        # Restart must retain initialization and resume ordinary conditional polling.
        restored = sys.modules[f"{PACKAGE_NAME}.storage"].FeedStorage(storage_dir=self.storage_dir)
        self.guids.insert(0, "next")
        await self.run_job(self.scheduler(restored))
        self.assertEqual([(item["guid"], item["_target_origins"]) for item in self.delivered],
                         [("next", ["qq:group:1", "qq:group:2"])])
        self.assertEqual(self.requests[-1]["etag"], "fresh-validator")

    async def test_new_job_reusing_source_does_not_replay_undated_history(self):
        await self.establish_old()
        self.raw["jobs"].append({"id": "new-job", "feed_ids": ["feed"], "target_ids": ["new"],
                                 "interval_seconds": 300})
        scheduler = self.scheduler()
        await self.run_job(scheduler, "new-job")
        self.assertEqual(self.delivered, [])
        self.guids.insert(0, "next")
        await self.run_job(scheduler, "new-job")
        self.assertEqual([(item["guid"], item["_target_origins"]) for item in self.delivered],
                         [("next", ["qq:group:2"])])

    async def test_old_target_receives_new_entry_while_new_target_establishes_baseline(self):
        await self.establish_old()
        self.guids.insert(0, "concurrent-new")
        self.raw["jobs"][0]["target_ids"].append("new")
        await self.run_job(self.scheduler())
        self.assertEqual([(item["guid"], item["_target_origins"]) for item in self.delivered],
                         [("concurrent-new", ["qq:group:1"])])

    async def test_new_job_snapshot_does_not_advance_shared_source_state(self):
        await self.establish_old()
        before = dict(await self.storage.get_feed_state("feed"))
        self.guids.insert(0, "concurrent-new")
        self.etag = "bootstrap-validator"
        self.raw["jobs"].append({"id": "new-job", "feed_ids": ["feed"], "target_ids": ["new"],
                                 "interval_seconds": 300})
        scheduler = self.scheduler()
        await self.run_job(scheduler, "new-job")
        self.assertEqual(await self.storage.get_feed_state("feed"), before)
        await self.run_job(scheduler)
        self.assertEqual([(item["guid"], item["_target_origins"]) for item in self.delivered],
                         [("concurrent-new", ["qq:group:1"])])

    async def test_existing_job_new_feed_is_initialized_independently(self):
        await self.establish_old()
        self.raw["feeds"].append({"id": "other", "url": "https://other.example/rss"})
        self.raw["jobs"][0]["feed_ids"].append("other")
        await self.run_job(self.scheduler())
        self.assertEqual(self.delivered, [])
        self.assertTrue(await self.storage.is_subscription_initialized("job", "qq:group:1", "other"))

    async def test_new_feed_history_cannot_consume_another_feeds_unseen_guid(self):
        await self.establish_old()
        self.raw["feeds"].append({"id": "other", "url": "https://other.example/rss"})
        self.raw["jobs"][0]["feed_ids"].append("other")
        for feed_id in ("feed", "other"):
            self.feed_bodies[feed_id] = (
                "<rss><channel><item><guid>shared-guid</guid><title>Shared</title>"
                f"<link>https://example.com/{feed_id}/new</link></item></channel></rss>"
            )
        await self.run_job(self.scheduler())
        self.assertEqual([(item["feed_id"], item["guid"], item["_target_origins"])
                          for item in self.delivered], [("feed", "shared-guid", ["qq:group:1"])])

    async def test_valid_empty_snapshot_initializes_and_next_undated_entry_is_sent(self):
        await self.establish_old()
        self.guids.clear()
        self.raw["jobs"][0]["target_ids"].append("new")
        await self.run_job(self.scheduler())
        self.assertTrue(await self.storage.is_subscription_initialized("job", "qq:group:2", "feed"))
        self.guids.append("next")
        await self.run_job(self.scheduler())
        self.assertEqual([(item["guid"], item["_target_origins"]) for item in self.delivered],
                         [("next", ["qq:group:1", "qq:group:2"])])

    async def test_failed_or_cached_bootstrap_does_not_initialize_new_target(self):
        await self.establish_old()
        self.raw["jobs"][0]["target_ids"].append("new")
        for status, body in [(503, ""), (304, ""), (200, "<broken>")]:
            with self.subTest(status=status, body=body):
                self.status, self.body = status, body
                await self.run_job(self.scheduler())
                self.assertFalse(await self.storage.is_subscription_initialized("job", "qq:group:2", "feed"))
                self.assertEqual(self.delivered, [])
        self.status, self.body = 200, None
        await self.run_job(self.scheduler())
        self.assertEqual(self.delivered, [])
        self.assertTrue(await self.storage.is_subscription_initialized("job", "qq:group:2", "feed"))

    async def test_legacy_migration_keeps_first_poll_new_items_for_all_existing_jobs(self):
        self.raw["jobs"].append({"id": "other", "feed_ids": ["feed"], "target_ids": ["new"],
                                 "interval_seconds": 300})
        await self.storage.update_feed_state("feed", bootstrap_done=True, last_success_time=1)
        scheduler = self.scheduler()
        await self.run_job(scheduler)
        await self.run_job(scheduler, "other")
        self.assertEqual({item["job_id"] for item in self.delivered}, {"job", "other"})

    async def test_aggregate_new_target_uses_same_bootstrap_boundary(self):
        await self.establish_old()
        self.raw["jobs"][0].update(target_ids=["old", "new"], aggregate_enabled=True)
        await self.run_job(self.scheduler())
        self.assertEqual(self.delivered, [])

    async def test_twitter_new_target_snapshot_does_not_replay_old_tweets_to_old_target(self):
        self.raw["feeds"] = [{"id": "feed", "source_type": "twitter", "username": "alice",
                              "max_new_items": 1}]
        self.raw["jobs"][0]["batch_size"] = 10
        await self.storage.update_feed_state("feed", since_id="100", bootstrap_done=True)
        old = self.scheduler()
        await old._ensure_subscription_registry()
        self.raw["jobs"][0]["target_ids"].append("new")
        timeline_ids = ["102", "101", "99"]
        twitter_calls = []

        async def fetch_timeline(feed, state, **kwargs):
            cursor = state.get("since_id", "")
            twitter_calls.append((cursor, feed.max_new_items))
            ids = [item_id for item_id in timeline_ids if int(item_id) > int(cursor or 0)]
            if feed.max_new_items:
                ids = ids[:feed.max_new_items]
            return types.SimpleNamespace(feed_id="feed", status=200,
                items=[{"username": "alice", "tweet_id": item_id, "text": item_id} for item_id in ids],
                since_id=ids[0] if ids else cursor)

        config = RSSConfig.from_context(self.raw)
        fetcher = sys.modules[f"{PACKAGE_NAME}.fetcher"].FeedFetcher(config, self.storage)
        fetcher._twitter_fetcher.fetch = mock.AsyncMock(side_effect=fetch_timeline)

        async def dispatch(item):
            self.delivered.append(item)
            return DispatchResult(success_count=1, success_origins=item["_target_origins"])

        scheduler = RSSScheduler(config, fetcher, sys.modules[f"{PACKAGE_NAME}.parser"].FeedParser(),
                                 types.SimpleNamespace(dispatch=mock.AsyncMock(side_effect=dispatch)), self.storage)
        await self.run_job(scheduler)
        self.assertEqual([(item["guid"], item["_target_origins"]) for item in self.delivered],
                         [("twitter:alice:102", ["qq:group:1"])])
        self.assertIn(("100", 1), twitter_calls)
        self.assertIn(("0", 0), twitter_calls)
        for tweet_id in timeline_ids:
            self.assertTrue(await self.storage.has_seen(scheduler._bootstrap_seen_keys(
                scheduler.get_job("job"), "qq:group:2", "feed", ["twitter:alice:" + tweet_id])[0]))
        self.assertFalse(await self.storage.has_seen(scheduler._target_seen_key(
            "job", "qq:group:1", "twitter:alice:99")))
        self.assertEqual((await self.storage.get_feed_state("feed"))["since_id"], "102")
        timeline_ids.insert(0, "103")
        self.delivered.clear()
        await self.run_job(scheduler)
        self.assertEqual([(item["guid"], item["_target_origins"]) for item in self.delivered],
                         [("twitter:alice:103", ["qq:group:1", "qq:group:2"])])


class RSSSchedulerTests(unittest.TestCase):
    def test_config_defaults_startup_delay_to_45_seconds(self):
        config = RSSConfig.from_context(
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

        self.assertEqual(config.startup_delay_seconds, 45)

    def test_history_items_are_suppressed_when_older_than_last_success(self):
        item = {
            "feed_id": "feed-1",
            "published_at": "2026-03-15T00:00:00+00:00",
        }
        feed_state_map = {"feed-1": {"last_success_time": 1773536400}}

        self.assertTrue(RSSScheduler._should_mark_history_only(item, feed_state_map))

    def test_newer_items_are_not_suppressed(self):
        item = {
            "feed_id": "feed-1",
            "published_at": "2026-03-15T03:00:01+00:00",
        }
        feed_state_map = {"feed-1": {"last_success_time": 1773536400}}

        self.assertFalse(RSSScheduler._should_mark_history_only(item, feed_state_map))

    def test_items_without_timestamp_are_not_suppressed(self):
        item = {"feed_id": "feed-1", "published_at": ""}
        feed_state_map = {"feed-1": {"last_success_time": 1773536400}}

        self.assertFalse(RSSScheduler._should_mark_history_only(item, feed_state_map))


class SchedulerFeedSuccessTests(unittest.IsolatedAsyncioTestCase):
    def _scheduler(self, raw_items, feed_ids):
        states = {
            feed_id: {
                "last_success_time": 123, "bootstrap_done": False,
                "etag": "old-etag", "last_modified": "old-modified", "since_id": "10",
            }
            for feed_id in feed_ids
        }
        storage = types.SimpleNamespace(
            get_feed_state=mock.AsyncMock(side_effect=lambda feed_id: dict(states[feed_id])),
            update_feed_state=mock.AsyncMock(),
            archive_digest_items=mock.AsyncMock(),
        )
        parser = sys.modules[f"{PACKAGE_NAME}.parser"].FeedParser()
        parser._parse_xml = mock.Mock(wraps=parser._parse_xml)
        scheduler = RSSScheduler(
            config=types.SimpleNamespace(jobs=[], targets=[], dedup_ttl_seconds=123),
            fetcher=types.SimpleNamespace(
                fetch=mock.AsyncMock(return_value=raw_items),
                fetch_feed_ids=mock.AsyncMock(return_value=raw_items),
            ),
            parser=parser,
            dispatcher=types.SimpleNamespace(dispatch=mock.AsyncMock()),
            storage=storage,
        )
        return scheduler

    async def _run_job(self, scheduler, feed_ids):
        await scheduler._run_job_once_guarded(types.SimpleNamespace(
            id="job-status", feed_ids=feed_ids, target_ids=[], interval_seconds=300,
        ))
        self.assertEqual(scheduler.last_results["job-status"].error_summary, "")

    def _assert_success(self, storage, feed_id, **metadata):
        storage.update_feed_state.assert_awaited_once()
        args, fields = storage.update_feed_state.await_args
        self.assertEqual(args, (feed_id,))
        self.assertGreater(fields["last_success_time"], 123)
        self.assertIs(fields["bootstrap_done"], True)
        for key, value in metadata.items():
            self.assertEqual(fields[key], value)

    async def test_partial_failure_updates_only_successful_feed(self):
        scheduler = self._scheduler([
            {"feed_id": "ok", "status": 200, "body": "<rss><channel/></rss>",
             "etag": "fresh-etag", "last_modified": "fresh-modified", "since_id": "20"},
            {"feed_id": "failed", "status": 503, "body": "", "etag": "bad-etag",
             "last_modified": "bad-modified", "since_id": "999"},
        ], ["ok", "failed", "missing"])
        await self._run_job(scheduler, ["ok", "failed", "missing"])
        self._assert_success(
            scheduler.storage, "ok", etag="fresh-etag",
            last_modified="fresh-modified", since_id="20",
        )

    async def test_all_failures_do_not_update_any_feed_state(self):
        for raw in [[], [{"feed_id": "failed", "status": 500, "since_id": "999"}]]:
            with self.subTest(raw=raw):
                scheduler = self._scheduler(raw, ["failed", "missing"])
                await self._run_job(scheduler, ["failed", "missing"])
                scheduler.storage.update_feed_state.assert_not_awaited()

    async def test_empty_success_without_validators_updates_feed_state(self):
        for raw in [
            {"feed_id": "ok", "status": 200, "body": "<rss><channel/></rss>"},
            {"feed_id": "ok", "status": 200, "body": ""},
            {"feed_id": "ok", "status": 204, "body": ""},
            {"feed_id": "ok", "source_type": "twitter", "status": 200, "items": []},
            {"feed_id": "ok", "body": "<rss><channel/></rss>"},
        ]:
            with self.subTest(raw=raw):
                scheduler = self._scheduler([raw], ["ok"])
                await self._run_job(scheduler, ["ok"])
                self._assert_success(scheduler.storage, "ok")
                scheduler._dispatcher.dispatch.assert_not_awaited()

    async def test_304_updates_success_without_parsing_or_dispatch(self):
        scheduler = self._scheduler([
            {"feed_id": "ok", "status": 304, "body": "", "etag": "old-etag",
             "last_modified": "old-modified"},
        ], ["ok"])
        await self._run_job(scheduler, ["ok"])
        self._assert_success(
            scheduler.storage, "ok", etag="old-etag", last_modified="old-modified",
        )
        scheduler._parser._parse_xml.assert_not_called()
        scheduler._dispatcher.dispatch.assert_not_awaited()
        scheduler.storage.archive_digest_items.assert_awaited_once_with([])

    async def test_daily_collection_rejects_failed_or_missing_response(self):
        for raw in [[], [{"feed_id": "failed", "status": 503, "since_id": "999"}]]:
            with self.subTest(raw=raw):
                scheduler = self._scheduler(raw, ["failed"])
                await scheduler._collect_digest_feed_once("failed")
                scheduler.storage.update_feed_state.assert_not_awaited()
                self.assertFalse(any(
                    call.args[0] for call in scheduler.storage.archive_digest_items.await_args_list
                ))

    async def test_daily_empty_success_and_304_update_feed_state(self):
        for raw in [
            {"feed_id": "ok", "status": 200, "body": "<rss><channel/></rss>"},
            {"feed_id": "ok", "status": 200, "body": ""},
            {"feed_id": "ok", "status": 304, "body": "", "etag": "old-etag",
             "last_modified": "old-modified"},
            {"feed_id": "ok", "body": "<rss><channel/></rss>"},
        ]:
            with self.subTest(raw=raw):
                scheduler = self._scheduler([raw], ["ok"])
                await scheduler._collect_digest_feed_once("ok")
                self._assert_success(scheduler.storage, "ok")
                if raw.get("status") == 304:
                    scheduler._parser._parse_xml.assert_not_called()
                    self._assert_success(
                        scheduler.storage, "ok", etag="old-etag", last_modified="old-modified",
                    )

    def test_feed_metadata_includes_only_successful_responses(self):
        raw = [
            {"feed_id": str(status), "status": status, "since_id": "999"}
            for status in [0, 200, 206, 299, 304, 302, 401, 503, None, "invalid"]
        ]
        raw.extend([{"feed_id": "legacy"}, {}, None])
        self.assertEqual(
            set(RSSScheduler._extract_feed_meta(raw)), {"200", "206", "299", "304", "legacy"},
        )


class SchedulerPermanentFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_job_batch_size_limits_successful_dispatches_per_run(self):
        class FakeStorage:
            def __init__(self):
                self.marked = []
                self.updated = False

            def build_dedup_key(self, item):
                return item["guid"]

            def build_seen_keys(self, item):
                return [item["guid"]]

            async def has_seen(self, item_id, ttl_seconds=None):
                return False

            async def mark_seen(self, item_id, ttl_seconds=0):
                self.marked.append((item_id, ttl_seconds))

            async def get_feed_state(self, feed_id):
                return {"last_success_time": 0}

            async def update_feed_state(self, *args, **kwargs):
                self.updated = True

        class FakeFetcher:
            async def fetch(self, job):
                return [{"feed_id": "feed-1"}]

        class FakeParser:
            def parse(self, raw_items, job):
                return [
                    {
                        "feed_id": "feed-1",
                        "guid": f"guid-{index}",
                        "title": f"Title {index}",
                        "published_at": "",
                    }
                    for index in range(1, 6)
                ]

        class FakeDispatcher:
            def __init__(self):
                self.items = []

            async def dispatch(self, item):
                self.items.append(item)
                return DispatchResult(success_count=1)

        config = types.SimpleNamespace(
            jobs=[],
            dedup_ttl_seconds=123,
            poll_interval_seconds=300,
            targets=[],
        )
        job = types.SimpleNamespace(
            id="job-1",
            feed_ids=["feed-1"],
            target_ids=[],
            enabled=True,
            interval_seconds=300,
            batch_size=2,
            dedup_ttl_seconds=0,
            semantic_dedup_enabled=False,
        )
        storage = FakeStorage()
        dispatcher = FakeDispatcher()
        scheduler = RSSScheduler(
            config=config,
            fetcher=FakeFetcher(),
            parser=FakeParser(),
            dispatcher=dispatcher,
            storage=storage,
            pipeline=None,
        )

        await scheduler._run_job_once_guarded(job)

        self.assertEqual([item["guid"] for item in dispatcher.items], ["guid-1", "guid-2"])
        self.assertEqual(storage.marked, [("guid-1", 123), ("guid-2", 123)])
        self.assertTrue(storage.updated)

    async def test_permanent_target_failures_are_not_marked_seen(self):
        class FakeStorage:
            def __init__(self):
                self.marked = []

            def build_dedup_key(self, item):
                return item["guid"]

            async def has_seen(self, item_id):
                return False

            async def mark_seen(self, item_id, ttl_seconds=0):
                self.marked.append((item_id, ttl_seconds))

            async def get_feed_state(self, feed_id):
                return {"last_success_time": 0}

            async def update_feed_state(self, *args, **kwargs):
                return {}

        class FakeFetcher:
            async def fetch(self, job):
                return [{"feed_id": "feed-1"}]

        class FakeParser:
            def parse(self, raw_items, job):
                return [{"feed_id": "feed-1", "guid": "item-1", "published_at": ""}]

        class FakeDispatcher:
            async def dispatch(self, item):
                return DispatchResult(permanent_failure_count=1)

        config = types.SimpleNamespace(
            jobs=[],
            dedup_ttl_seconds=123,
            poll_interval_seconds=300,
        )
        job = types.SimpleNamespace(id="job-1", feed_ids=["feed-1"], enabled=True, interval_seconds=300)
        storage = FakeStorage()
        scheduler = RSSScheduler(
            config=config,
            fetcher=FakeFetcher(),
            parser=FakeParser(),
            dispatcher=FakeDispatcher(),
            storage=storage,
            pipeline=None,
        )

        await scheduler._run_job_once_guarded(job)

        self.assertEqual(storage.marked, [])

    async def test_job_level_dedup_ttl_overrides_global_default(self):
        class FakeStorage:
            def __init__(self):
                self.marked = []

            def build_dedup_key(self, item):
                return item["guid"]

            async def has_seen(self, item_id):
                return False

            async def mark_seen(self, item_id, ttl_seconds=0):
                self.marked.append((item_id, ttl_seconds))

            async def get_feed_state(self, feed_id):
                return {"last_success_time": 0}

            async def update_feed_state(self, *args, **kwargs):
                return {}

        class FakeFetcher:
            async def fetch(self, job):
                return [{"feed_id": "feed-1"}]

        class FakeParser:
            def parse(self, raw_items, job):
                return [{"feed_id": "feed-1", "guid": "item-1", "published_at": ""}]

        class FakeDispatcher:
            async def dispatch(self, item):
                return DispatchResult(success_count=1)

        config = types.SimpleNamespace(
            jobs=[],
            dedup_ttl_seconds=123,
            poll_interval_seconds=300,
        )
        job = types.SimpleNamespace(
            id="job-1",
            feed_ids=["feed-1"],
            enabled=True,
            interval_seconds=300,
            dedup_ttl_seconds=3888000,
        )
        storage = FakeStorage()
        scheduler = RSSScheduler(
            config=config,
            fetcher=FakeFetcher(),
            parser=FakeParser(),
            dispatcher=FakeDispatcher(),
            storage=storage,
            pipeline=None,
        )

        await scheduler._run_job_once_guarded(job)

        self.assertEqual(storage.marked, [("item-1", 3888000)])

    async def test_job_level_dedup_ttl_is_used_when_checking_seen_records(self):
        class FakeStorage:
            def __init__(self):
                self.seen_calls = []

            def build_dedup_key(self, item):
                return item["guid"]

            async def has_seen(self, item_id, ttl_seconds=None):
                self.seen_calls.append((item_id, ttl_seconds))
                return True

            async def mark_seen(self, item_id, ttl_seconds=0):
                raise AssertionError("seen item should not be marked again")

            async def get_feed_state(self, feed_id):
                return {"last_success_time": 0}

            async def update_feed_state(self, *args, **kwargs):
                return {}

        class FakeFetcher:
            async def fetch(self, job):
                return [{"feed_id": "feed-1"}]

        class FakeParser:
            def parse(self, raw_items, job):
                return [{"feed_id": "feed-1", "guid": "item-1", "published_at": ""}]

        class FakeDispatcher:
            def __init__(self):
                self.count = 0

            async def dispatch(self, item):
                self.count += 1
                return DispatchResult(success_count=1)

        config = types.SimpleNamespace(
            jobs=[],
            dedup_ttl_seconds=123,
            poll_interval_seconds=300,
        )
        job = types.SimpleNamespace(
            id="job-1",
            feed_ids=["feed-1"],
            enabled=True,
            interval_seconds=300,
            dedup_ttl_seconds=3888000,
        )
        storage = FakeStorage()
        dispatcher = FakeDispatcher()
        scheduler = RSSScheduler(
            config=config,
            fetcher=FakeFetcher(),
            parser=FakeParser(),
            dispatcher=dispatcher,
            storage=storage,
            pipeline=None,
        )

        await scheduler._run_job_once_guarded(job)

        self.assertEqual(storage.seen_calls, [("item-1", 3888000)])
        self.assertEqual(dispatcher.count, 0)

    async def test_job_compact_mode_is_forwarded_to_dispatcher(self):
        class FakeStorage:
            def build_dedup_key(self, item):
                return item["guid"]

            async def has_seen(self, item_id, ttl_seconds=None):
                return False

            async def mark_seen(self, item_id, ttl_seconds=0):
                return None

            async def get_feed_state(self, feed_id):
                return {"last_success_time": 0}

            async def update_feed_state(self, *args, **kwargs):
                return {}

        class FakeFetcher:
            async def fetch(self, job):
                return [{"feed_id": "feed-1"}]

        class FakeParser:
            def parse(self, raw_items, job):
                return [{"feed_id": "feed-1", "guid": "item-1", "published_at": ""}]

        class FakeDispatcher:
            def __init__(self):
                self.items = []

            async def dispatch(self, item):
                self.items.append(item)
                return DispatchResult(success_count=1)

        dispatcher = FakeDispatcher()
        scheduler = RSSScheduler(
            config=types.SimpleNamespace(
                jobs=[],
                dedup_ttl_seconds=123,
                poll_interval_seconds=300,
            ),
            fetcher=FakeFetcher(),
            parser=FakeParser(),
            dispatcher=dispatcher,
            storage=FakeStorage(),
            pipeline=None,
        )
        job = types.SimpleNamespace(
            id="job-compact",
            feed_ids=["feed-1"],
            enabled=True,
            interval_seconds=300,
            compact_mode_enabled=True,
            compact_mode_send_images=True,
        )

        await scheduler._run_job_once_guarded(job)

        self.assertTrue(dispatcher.items[0]["compact_mode_enabled"])
        self.assertTrue(dispatcher.items[0]["compact_mode_send_images"])


class SchedulerTaskCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_cancels_stale_job_tasks(self):
        async def stale_loop():
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                raise

        stale_task = asyncio.create_task(stale_loop(), name="rss-job-stale")
        stale_digest_task = asyncio.create_task(stale_loop(), name="rss-digest-send-stale")
        await asyncio.sleep(0)

        config = types.SimpleNamespace(
            jobs=[],
            dedup_ttl_seconds=123,
            poll_interval_seconds=300,
            startup_delay_seconds=0,
        )
        scheduler = RSSScheduler(
            config=config,
            fetcher=types.SimpleNamespace(),
            parser=types.SimpleNamespace(),
            dispatcher=types.SimpleNamespace(),
            storage=types.SimpleNamespace(),
            pipeline=None,
        )

        await scheduler.start()

        self.assertTrue(stale_task.cancelled() or stale_task.done())
        self.assertTrue(stale_digest_task.cancelled() or stale_digest_task.done())


class SchedulerDailyDigestTests(unittest.IsolatedAsyncioTestCase):
    async def test_collect_digest_feed_once_archives_items(self):
        class FakeStorage:
            def __init__(self):
                self.archived = []
                self.updated = []

            async def archive_digest_items(self, items):
                self.archived.extend(items)

            async def update_feed_state(self, *args, **kwargs):
                self.updated.append((args, kwargs))
                return {}

        class FakeFetcher:
            async def fetch_feed_ids(self, feed_ids):
                return [{"feed_id": feed_ids[0], "body": "<rss/>", "etag": "etag", "last_modified": ""}]

        class FakeParser:
            def parse(self, raw_items, job=None):
                return [
                    {
                        "feed_id": raw_items[0]["feed_id"],
                        "guid": "guid-1",
                        "title": "Title",
                        "feed_title": "Feed",
                        "summary": "Summary",
                        "published_at": "2026-03-28T00:00:00+00:00",
                    }
                ]

        scheduler = RSSScheduler(
            config=types.SimpleNamespace(
                jobs=[],
                daily_digests=[],
                poll_interval_seconds=300,
                startup_delay_seconds=0,
                timezone="Asia/Shanghai",
            ),
            fetcher=FakeFetcher(),
            parser=FakeParser(),
            dispatcher=types.SimpleNamespace(),
            storage=FakeStorage(),
            pipeline=None,
        )

        await scheduler._collect_digest_feed_once("feed-1")

        self.assertEqual(len(scheduler.storage.archived), 1)
        self.assertEqual(scheduler.storage.archived[0]["guid"], "guid-1")

    async def test_run_daily_digest_once_builds_and_dispatches(self):
        class FakeStorage:
            def __init__(self):
                self.status_updates = []

            async def list_digest_items(self, feed_ids, **kwargs):
                return [
                    {
                        "feed_id": "feed-1",
                        "feed_title": "Feed",
                        "title": "Title",
                        "summary": "Summary",
                        "link": "https://example.com/post/1",
                        "published_at": "2026-03-28T00:00:00+00:00",
                    }
                ]

            async def update_daily_digest_status(self, digest_id, **fields):
                self.status_updates.append((digest_id, fields))
                return fields

            async def get_daily_digest_status(self, digest_id):
                return {}

        class FakeDispatcher:
            def __init__(self):
                self.digests = []

            async def dispatch_daily_digest(self, digest):
                self.digests.append(digest)
                return DispatchResult(success_count=1)

        class FakePipeline:
            async def build_daily_digest_content(self, digest, items, unified_msg_origin=""):
                self.last_digest = digest
                self.last_items = items
                self.last_origin = unified_msg_origin
                return {"content": "1. [Feed] Title", "engine": "llm", "llm_reason": "ok"}

        digest = DailyDigestConfig(
            id="digest-1",
            title="芯片日报",
            feed_ids=["feed-1"],
            target_ids=["target-1"],
            llm_timeout_seconds=90,
            enabled=True,
        )
        scheduler = RSSScheduler(
            config=types.SimpleNamespace(
                jobs=[],
                daily_digests=[digest],
                poll_interval_seconds=300,
                startup_delay_seconds=0,
                timezone="Asia/Shanghai",
                targets=[
                    types.SimpleNamespace(
                        id="target-1",
                        enabled=True,
                        unified_msg_origin="qq:group:1",
                    )
                ],
            ),
            fetcher=types.SimpleNamespace(),
            parser=types.SimpleNamespace(),
            dispatcher=FakeDispatcher(),
            storage=FakeStorage(),
            pipeline=FakePipeline(),
        )

        await scheduler.run_daily_digest_once("digest-1")

        self.assertEqual(len(scheduler._dispatcher.digests), 1)
        self.assertEqual(scheduler._dispatcher.digests[0]["title"], "芯片日报")
        self.assertEqual(scheduler._dispatcher.digests[0]["item_count"], 1)
        self.assertEqual(scheduler._pipeline.last_digest["llm_timeout_seconds"], 90)

    async def test_run_daily_digest_once_merges_semantic_items_before_pipeline(self):
        class FakeStorage:
            def __init__(self):
                self.status_updates = []

            async def list_digest_items(self, feed_ids, **kwargs):
                return [
                    {
                        "feed_id": "feed-1",
                        "feed_title": "Tom's Hardware",
                        "title": "RTX 6090 specs leak",
                        "summary": "Specs leaked.",
                        "link": "https://example.com/tom",
                        "published_at": "2026-03-28T00:00:00+00:00",
                    },
                    {
                        "feed_id": "feed-2",
                        "feed_title": "VideoCardz",
                        "title": "RTX 6090 specifications appear",
                        "summary": "The same specs appeared.",
                        "link": "https://example.com/vc",
                        "published_at": "2026-03-28T00:10:00+00:00",
                    },
                ]

            async def update_daily_digest_status(self, digest_id, **fields):
                self.status_updates.append((digest_id, fields))
                return fields

            async def get_daily_digest_status(self, digest_id):
                return {}

        class FakeDispatcher:
            def __init__(self):
                self.digests = []

            async def dispatch_daily_digest(self, digest):
                self.digests.append(digest)
                return DispatchResult(success_count=1)

        class FakePipeline:
            async def build_daily_digest_content(self, digest, items, unified_msg_origin=""):
                self.last_items = items
                return {"content": "1. [Tom's Hardware / VideoCardz] RTX 6090 specs", "engine": "llm", "llm_reason": "ok"}

        class FakeSemanticDeduper:
            async def merge_digest_items(self, digest, items, unified_msg_origin=""):
                return {
                    "items": [
                        {
                            "feed_title": "Tom's Hardware / VideoCardz",
                            "title": "RTX 6090 specs",
                            "summary": "Merged specs coverage.",
                            "link": "https://example.com/tom",
                            "source_items": items,
                            "merged_count": 2,
                        }
                    ],
                    "reason": "ok",
                    "merged_count": 1,
                    "input_count": 2,
                    "output_count": 1,
                }

        digest = DailyDigestConfig(
            id="digest-merge",
            title="芯片日报",
            feed_ids=["feed-1", "feed-2"],
            target_ids=["target-1"],
            semantic_merge_enabled=True,
            enabled=True,
        )
        storage = FakeStorage()
        scheduler = RSSScheduler(
            config=types.SimpleNamespace(
                jobs=[],
                daily_digests=[digest],
                poll_interval_seconds=300,
                startup_delay_seconds=0,
                timezone="Asia/Shanghai",
                targets=[
                    types.SimpleNamespace(
                        id="target-1",
                        enabled=True,
                        unified_msg_origin="qq:group:1",
                    )
                ],
            ),
            fetcher=types.SimpleNamespace(),
            parser=types.SimpleNamespace(),
            dispatcher=FakeDispatcher(),
            storage=storage,
            pipeline=FakePipeline(),
            semantic_deduper=FakeSemanticDeduper(),
        )

        await scheduler.run_daily_digest_once("digest-merge")

        self.assertEqual(len(scheduler._pipeline.last_items), 1)
        self.assertEqual(scheduler._pipeline.last_items[0]["title"], "RTX 6090 specs")
        self.assertEqual(scheduler._dispatcher.digests[0]["item_count"], 1)
        self.assertEqual(len(scheduler._dispatcher.digests[0]["links"]), 2)
        final_status = storage.status_updates[-1][1]
        self.assertEqual(final_status["last_raw_item_count"], 2)
        self.assertEqual(final_status["last_semantic_merge_reason"], "ok")
        self.assertEqual(final_status["last_semantic_merged_count"], 1)

    async def test_run_daily_digest_once_skips_empty_window(self):
        class FakeStorage:
            def __init__(self):
                self.status_updates = []

            async def list_digest_items(self, feed_ids, **kwargs):
                return []

            async def update_daily_digest_status(self, digest_id, **fields):
                self.status_updates.append((digest_id, fields))
                return fields

            async def get_daily_digest_status(self, digest_id):
                return {}

        class FakeDispatcher:
            def __init__(self):
                self.calls = 0

            async def dispatch_daily_digest(self, digest):
                self.calls += 1
                return DispatchResult(success_count=1)

        digest = DailyDigestConfig(
            id="digest-empty",
            title="空日报",
            feed_ids=["feed-1"],
            target_ids=["target-1"],
            enabled=True,
        )
        storage = FakeStorage()
        dispatcher = FakeDispatcher()
        scheduler = RSSScheduler(
            config=types.SimpleNamespace(
                jobs=[],
                daily_digests=[digest],
                poll_interval_seconds=300,
                startup_delay_seconds=0,
                timezone="Asia/Shanghai",
                targets=[],
            ),
            fetcher=types.SimpleNamespace(),
            parser=types.SimpleNamespace(),
            dispatcher=dispatcher,
            storage=storage,
            pipeline=None,
        )

        await scheduler.run_daily_digest_once("digest-empty")

        self.assertEqual(dispatcher.calls, 0)
        self.assertEqual(len(storage.status_updates), 1)
        self.assertEqual(storage.status_updates[0][1]["last_skipped_reason"], "empty_window")


class SchedulerBatchDedupTests(unittest.IsolatedAsyncioTestCase):
    async def test_duplicate_items_in_same_batch_are_dispatched_once(self):
        class FakeStorage:
            def __init__(self):
                self.marked = []

            def build_dedup_key(self, item):
                return item["guid"]

            def build_seen_keys(self, item):
                return [item["guid"]]

            async def has_seen(self, item_id):
                return False

            async def mark_seen(self, item_id, ttl_seconds=0):
                self.marked.append((item_id, ttl_seconds))

            async def get_feed_state(self, feed_id):
                return {"last_success_time": 0}

            async def update_feed_state(self, *args, **kwargs):
                return {}

        class FakeFetcher:
            async def fetch(self, job):
                return [{"feed_id": "feed-1"}]

        class FakeParser:
            def parse(self, raw_items, job):
                return [
                    {"feed_id": "feed-1", "guid": "dup-1", "title": "Same Item", "published_at": ""},
                    {"feed_id": "feed-1", "guid": "dup-1", "title": "Same Item", "published_at": ""},
                ]

        class FakeDispatcher:
            def __init__(self):
                self.calls = 0

            async def dispatch(self, item):
                self.calls += 1
                return DispatchResult(success_count=1)

        config = types.SimpleNamespace(
            jobs=[],
            dedup_ttl_seconds=123,
            poll_interval_seconds=300,
        )
        job = types.SimpleNamespace(id="job-1", feed_ids=["feed-1"], enabled=True, interval_seconds=300)
        storage = FakeStorage()
        dispatcher = FakeDispatcher()
        scheduler = RSSScheduler(
            config=config,
            fetcher=FakeFetcher(),
            parser=FakeParser(),
            dispatcher=dispatcher,
            storage=storage,
            pipeline=None,
        )

        await scheduler._run_job_once_guarded(job)

        self.assertEqual(dispatcher.calls, 1)
        self.assertEqual(storage.marked, [("dup-1", 123)])

    async def test_same_link_with_different_guid_is_dispatched_once(self):
        class FakeStorage:
            def __init__(self):
                self.marked = []
                self.seen = set()

            def build_dedup_key(self, item):
                return item["guid"]

            def build_seen_keys(self, item):
                return [item["guid"], f"link:{item['link']}"]

            async def has_seen(self, item_id):
                return item_id in self.seen

            async def mark_seen(self, item_id, ttl_seconds=0):
                self.seen.add(item_id)
                self.marked.append((item_id, ttl_seconds))

            async def get_feed_state(self, feed_id):
                return {"last_success_time": 0}

            async def update_feed_state(self, *args, **kwargs):
                return {}

        class FakeFetcher:
            async def fetch(self, job):
                return [{"feed_id": "feed-1"}]

        class FakeParser:
            def __init__(self):
                self._calls = 0

            def parse(self, raw_items, job):
                self._calls += 1
                if self._calls == 1:
                    return [
                        {
                            "feed_id": "feed-1",
                            "guid": "guid-1",
                            "link": "https://example.com/news/1",
                            "title": "Same Link",
                            "published_at": "",
                        }
                    ]
                return [
                    {
                        "feed_id": "feed-1",
                        "guid": "guid-2",
                        "link": "https://example.com/news/1",
                        "title": "Same Link",
                        "published_at": "",
                    }
                ]

        class FakeDispatcher:
            def __init__(self):
                self.calls = 0

            async def dispatch(self, item):
                self.calls += 1
                return DispatchResult(success_count=1)

        config = types.SimpleNamespace(
            jobs=[],
            dedup_ttl_seconds=123,
            poll_interval_seconds=300,
        )
        job = types.SimpleNamespace(id="job-1", feed_ids=["feed-1"], enabled=True, interval_seconds=300)
        storage = FakeStorage()
        dispatcher = FakeDispatcher()
        scheduler = RSSScheduler(
            config=config,
            fetcher=FakeFetcher(),
            parser=FakeParser(),
            dispatcher=dispatcher,
            storage=storage,
            pipeline=None,
        )

        await scheduler._run_job_once_guarded(job)
        await scheduler._run_job_once_guarded(job)

        self.assertEqual(dispatcher.calls, 1)
        self.assertEqual(
            storage.marked,
            [("guid-1", 123), ("link:https://example.com/news/1", 123)],
        )


class SchedulerSemanticDedupTests(unittest.IsolatedAsyncioTestCase):
    async def test_semantic_duplicate_marks_seen_and_skips_dispatch(self):
        class FakeStorage:
            def __init__(self):
                self.marked = []

            def build_dedup_key(self, item):
                return item["guid"]

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

        class FakeFetcher:
            async def fetch(self, job):
                return [{"feed_id": "feed-1"}]

        class FakeParser:
            def parse(self, raw_items, job):
                return [
                    {
                        "feed_id": "feed-1",
                        "guid": "guid-1",
                        "title": "NVIDIA unveils RTX 6090",
                        "summary": "NVIDIA introduced a new GPU.",
                        "published_at": "",
                    }
                ]

        class FakeDispatcher:
            def __init__(self):
                self.calls = 0

            async def dispatch(self, item):
                self.calls += 1
                return DispatchResult(success_count=1)

        class FakeSemanticDeduper:
            def __init__(self):
                self.remembered = []

            async def check(self, job, item, unified_msg_origin=""):
                return types.SimpleNamespace(
                    duplicate=True,
                    matched_record_id="record-1",
                    confidence=0.91,
                    reason="same_event",
                )

            async def remember(self, job, item, seen_keys, ttl_seconds):
                self.remembered.append((job.id, item.get("guid")))

        config = types.SimpleNamespace(
            jobs=[],
            dedup_ttl_seconds=123,
            poll_interval_seconds=300,
            targets=[],
        )
        job = types.SimpleNamespace(
            id="job-1",
            feed_ids=["feed-1"],
            target_ids=[],
            enabled=True,
            interval_seconds=300,
            dedup_ttl_seconds=123,
            semantic_dedup_enabled=True,
        )
        storage = FakeStorage()
        dispatcher = FakeDispatcher()
        semantic = FakeSemanticDeduper()
        scheduler = RSSScheduler(
            config=config,
            fetcher=FakeFetcher(),
            parser=FakeParser(),
            dispatcher=dispatcher,
            storage=storage,
            pipeline=None,
            semantic_deduper=semantic,
        )

        await scheduler._run_job_once_guarded(job)

        self.assertEqual(dispatcher.calls, 0)
        self.assertEqual(storage.marked, [("guid-1", 123)])
        self.assertEqual(semantic.remembered, [])

    async def test_successful_dispatch_records_semantic_candidate(self):
        class FakeStorage:
            def __init__(self):
                self.marked = []

            def build_dedup_key(self, item):
                return item["guid"]

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

        class FakeFetcher:
            async def fetch(self, job):
                return [{"feed_id": "feed-1"}]

        class FakeParser:
            def parse(self, raw_items, job):
                return [
                    {
                        "feed_id": "feed-1",
                        "guid": "guid-1",
                        "title": "NVIDIA unveils RTX 6090",
                        "summary": "NVIDIA introduced a new GPU.",
                        "published_at": "",
                    }
                ]

        class FakeDispatcher:
            async def dispatch(self, item):
                return DispatchResult(success_count=1)

        class FakeSemanticDeduper:
            def __init__(self):
                self.remembered = []

            async def check(self, job, item, unified_msg_origin=""):
                return types.SimpleNamespace(
                    duplicate=False,
                    matched_record_id="",
                    confidence=0.0,
                    reason="not_duplicate",
                )

            async def remember(self, job, item, seen_keys, ttl_seconds):
                self.remembered.append((job.id, item.get("guid"), tuple(seen_keys), ttl_seconds))

        config = types.SimpleNamespace(
            jobs=[],
            dedup_ttl_seconds=123,
            poll_interval_seconds=300,
            targets=[],
        )
        job = types.SimpleNamespace(
            id="job-1",
            feed_ids=["feed-1"],
            target_ids=[],
            enabled=True,
            interval_seconds=300,
            dedup_ttl_seconds=123,
            semantic_dedup_enabled=True,
        )
        semantic = FakeSemanticDeduper()
        scheduler = RSSScheduler(
            config=config,
            fetcher=FakeFetcher(),
            parser=FakeParser(),
            dispatcher=FakeDispatcher(),
            storage=FakeStorage(),
            pipeline=None,
            semantic_deduper=semantic,
        )

        await scheduler._run_job_once_guarded(job)

        self.assertEqual(semantic.remembered, [("job-1", "guid-1", ("guid-1",), 123)])


class SchedulerTranslationTest(unittest.IsolatedAsyncioTestCase):
    async def test_test_translation_returns_pipeline_error_when_missing(self):
        config = types.SimpleNamespace(jobs=[])
        scheduler = RSSScheduler(
            config=config,
            fetcher=types.SimpleNamespace(),
            parser=types.SimpleNamespace(),
            dispatcher=types.SimpleNamespace(),
            storage=types.SimpleNamespace(),
            pipeline=None,
        )

        result = await scheduler.test_translation(sample_text="hello")

        self.assertEqual(result.get("error"), "pipeline_not_configured")

    async def test_test_translation_returns_report_and_config_snapshot(self):
        class FakePipeline:
            async def diagnose_translation(self, entry):
                self.entry = entry
                return {
                    "input_chars": len(entry.get("summary", "")),
                    "llm": {"ok": True, "latency_ms": 10, "provider_id": "p"},
                    "github": {"ok": False, "latency_ms": 12, "error": "token_missing"},
                    "google": {"ok": False, "latency_ms": 20, "error": "no_key"},
                }

        pipeline = FakePipeline()
        config = types.SimpleNamespace(
            jobs=[],
            llm_enabled=True,
            llm_timeout_seconds=15,
            llm_proxy_mode="system",
            github_models_enabled=True,
            github_models_model="openai/gpt-4o-mini",
            github_models_timeout_seconds=18,
            github_models_proxy_mode="custom",
            google_translate_enabled=True,
            google_translate_target_lang="zh-CN",
            google_translate_timeout_seconds=15,
            google_translate_proxy_mode="off",
        )
        scheduler = RSSScheduler(
            config=config,
            fetcher=types.SimpleNamespace(),
            parser=types.SimpleNamespace(),
            dispatcher=types.SimpleNamespace(),
            storage=types.SimpleNamespace(),
            pipeline=pipeline,
        )

        result = await scheduler.test_translation(sample_text="sample")

        self.assertEqual(pipeline.entry.get("summary"), "sample")
        self.assertEqual(result.get("llm", {}).get("ok"), True)
        self.assertEqual(result.get("github", {}).get("error"), "token_missing")
        self.assertEqual(result.get("config", {}).get("github_models_model"), "openai/gpt-4o-mini")
        self.assertEqual(result.get("google", {}).get("error"), "no_key")
        self.assertEqual(result.get("config", {}).get("google_translate_proxy_mode"), "off")


class SchedulerTargetScopedDedupTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _target_seen_key(job_id: str, origin: str, item_key: str) -> str:
        digest = hashlib.sha256(origin.encode("utf-8")).hexdigest()
        return f"job:{job_id}:origin:{digest}:{item_key}"

    def _build_config(self):
        return RSSConfig.from_context(
            {
                "feeds": [{"id": "feed-1", "url": "https://example.com/rss", "enabled": True}],
                "targets": [
                    {
                        "id": "target-friend",
                        "platform": "qq",
                        "unified_msg_origin": "default:FriendMessage:562506516",
                        "enabled": True,
                    },
                    {
                        "id": "target-group",
                        "platform": "qq",
                        "unified_msg_origin": "default:GroupMessage:764968756",
                        "enabled": True,
                    },
                ],
                "jobs": [
                    {
                        "id": "job-1",
                        "feed_ids": ["feed-1"],
                        "target_ids": ["target-friend", "target-group"],
                        "interval_seconds": 300,
                        "enabled": True,
                    }
                ],
                "dedup_ttl_seconds": 123,
            }
        )

    async def test_seen_target_is_not_sent_again_when_another_target_is_pending(self):
        group_origin = "default:GroupMessage:764968756"
        friend_origin = "default:FriendMessage:562506516"

        class FakeStorage:
            def __init__(self, seen_key):
                self.seen = {seen_key}

            def build_dedup_key(self, item):
                return item["guid"]

            def build_seen_keys(self, item):
                return [item["guid"]]

            async def has_seen(self, item_id, ttl_seconds=None):
                return item_id in self.seen

            async def mark_seen(self, item_id, ttl_seconds=0):
                self.seen.add(item_id)

            async def get_feed_state(self, feed_id):
                return {"last_success_time": 0}

            async def update_feed_state(self, *args, **kwargs):
                return {}

        class FakeFetcher:
            async def fetch(self, job):
                return [{"feed_id": "feed-1"}]

        class FakeParser:
            def parse(self, raw_items, job):
                return [{"feed_id": "feed-1", "guid": "item-1", "published_at": ""}]

        class FakeDispatcher:
            def __init__(self):
                self.items = []

            async def dispatch(self, item):
                self.items.append(item)
                return DispatchResult(success_count=1, success_origins=[friend_origin])

        config = self._build_config()
        dispatcher = FakeDispatcher()
        scheduler = RSSScheduler(
            config=config,
            fetcher=FakeFetcher(),
            parser=FakeParser(),
            dispatcher=dispatcher,
            storage=FakeStorage(self._target_seen_key("job-1", group_origin, "item-1")),
            pipeline=None,
        )

        await scheduler._run_job_once_guarded(config.jobs[0])

        self.assertEqual(dispatcher.items[0]["_target_origins"], [friend_origin])

    async def test_partial_success_marks_only_successful_target_seen(self):
        group_origin = "default:GroupMessage:764968756"
        friend_origin = "default:FriendMessage:562506516"

        class FakeStorage:
            def __init__(self):
                self.seen = set()
                self.marked = []

            def build_dedup_key(self, item):
                return item["guid"]

            def build_seen_keys(self, item):
                return [item["guid"]]

            async def has_seen(self, item_id, ttl_seconds=None):
                return item_id in self.seen

            async def mark_seen(self, item_id, ttl_seconds=0):
                self.seen.add(item_id)
                self.marked.append((item_id, ttl_seconds))

            async def get_feed_state(self, feed_id):
                return {"last_success_time": 0}

            async def update_feed_state(self, *args, **kwargs):
                return {}

        class FakeFetcher:
            async def fetch(self, job):
                return [{"feed_id": "feed-1"}]

        class FakeParser:
            def parse(self, raw_items, job):
                return [{"feed_id": "feed-1", "guid": "item-1", "published_at": ""}]

        class FakeDispatcher:
            async def dispatch(self, item):
                return DispatchResult(
                    success_count=1,
                    transient_failure_count=1,
                    success_origins=[group_origin],
                )

        config = self._build_config()
        storage = FakeStorage()
        scheduler = RSSScheduler(
            config=config,
            fetcher=FakeFetcher(),
            parser=FakeParser(),
            dispatcher=FakeDispatcher(),
            storage=storage,
            pipeline=None,
        )

        await scheduler._run_job_once_guarded(config.jobs[0])

        self.assertEqual(
            storage.marked,
            [(self._target_seen_key("job-1", group_origin, "item-1"), 123)],
        )
        self.assertNotIn(
            self._target_seen_key("job-1", friend_origin, "item-1"),
            storage.seen,
        )


if __name__ == "__main__":
    unittest.main()
