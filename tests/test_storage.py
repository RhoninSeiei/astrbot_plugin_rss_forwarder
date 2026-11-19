import asyncio
import json
import os
import tempfile
import threading
import time
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

from storage import FeedStorage, fcntl


class FeedStorageTests(unittest.IsolatedAsyncioTestCase):
    async def test_subscription_registry_adopts_legacy_once_and_persists_isolated_scopes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = FeedStorage(storage_dir=tmpdir)
            await storage.initialize_subscription_registry([("job", "old", "feed")])
            await storage.initialize_subscription_registry([("job", "new", "feed")])
            self.assertTrue(await storage.is_subscription_initialized("job", "old", "feed"))
            self.assertFalse(await storage.is_subscription_initialized("job", "new", "feed"))
            await storage.mark_subscription_initialized("job", "new", "feed")
            restored = FeedStorage(storage_dir=tmpdir)
            self.assertTrue(await restored.is_subscription_initialized("job", "new", "feed"))
            self.assertFalse(await restored.is_subscription_initialized("other", "new", "feed"))
            self.assertFalse(await restored.is_subscription_initialized("job", "new", "other"))
            await restored.clear_seen()
            self.assertTrue(await restored.is_subscription_initialized("job", "new", "feed"))

    def test_default_plugin_name_matches_package_name(self):
        storage = FeedStorage(storage_dir=".")

        self.assertEqual(storage._plugin_name, "astrbot_plugin_rss_forwarder")

    async def test_persists_seen_records_to_disk(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = FeedStorage(storage_dir=tmpdir)
            await storage.mark_seen("item-1", ttl_seconds=3600)

            restored = FeedStorage(storage_dir=tmpdir)
            self.assertTrue(await restored.has_seen("item-1"))

            state_path = Path(tmpdir) / "state.json"
            self.assertTrue(state_path.exists())
            payload = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertIn("content_seen:v0:item-1", payload["kv"])

    async def test_stale_instances_preserve_unrelated_state_and_current_dedup_version(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            first = FeedStorage(storage_dir=tmpdir)
            second = FeedStorage(storage_dir=tmpdir)
            await first.mark_seen("before-clear")
            self.assertTrue(await second.has_seen("before-clear"))
            self.assertEqual(await first.clear_seen(), 1)
            await first.update_daily_digest_status("digest", last_item_count=5)
            await first.confirm_dispatch("fingerprint")
            await second.update_feed_state("feed", etag="updated")
            await second.mark_seen("after-clear")

            self.assertFalse(await second.has_seen("before-clear"))
            self.assertTrue(await first.has_seen("after-clear"))
            self.assertFalse(await second.claim_dispatch("fingerprint"))
            self.assertEqual(
                (await second.get_daily_digest_status("digest"))["last_item_count"], 5
            )
            self.assertEqual(await first.get_feed_state("feed"), {"etag": "updated"})
            payload = json.loads((Path(tmpdir) / "state.json").read_text(encoding="utf-8"))
            self.assertIn("content_seen:v1:after-clear", payload["kv"])
            self.assertNotIn("content_seen:v0:after-clear", payload["kv"])

    async def test_concurrent_feed_and_digest_updates_do_not_block_on_held_kv_lock(self):
        async def get_kv_data(key, default=None):
            await asyncio.sleep(0)
            return {"val": '{"bootstrap_done":true}'} if key == "feed_state:feed" else default

        with tempfile.TemporaryDirectory() as tmpdir:
            first = FeedStorage(storage_dir=tmpdir, get_kv_data=get_kv_data)
            second = FeedStorage(storage_dir=tmpdir, get_kv_data=get_kv_data)
            if fcntl is not None:
                flock = fcntl.flock

                def check_flock(fd, operation):
                    if operation & fcntl.LOCK_EX:
                        self.assertTrue(operation & fcntl.LOCK_NB, "blocking flock on the event loop")
                    return flock(fd, operation)

                lock_check = patch.object(fcntl, "flock", side_effect=check_flock)
            else:
                lock_check = nullcontext()
            with lock_check:
                await asyncio.wait_for(
                    asyncio.gather(
                        first.update_feed_state("feed", etag="v1"),
                        second.update_daily_digest_status("digest", last_item_count=1),
                        second.update_feed_state("feed", since_id="123"),
                    ),
                    timeout=2,
                )
            expected = {"etag": "v1", "since_id": "123", "bootstrap_done": True}
            self.assertEqual(await first.get_feed_state("feed"), expected)
            self.assertEqual(await second.get_feed_state("feed"), expected)
            self.assertEqual((await first.get_daily_digest_status("digest"))["last_item_count"], 1)

    async def test_backend_lookup_does_not_reimport_a_concurrently_deleted_value(self):
        for key, lookup in (
            ("key", lambda storage: storage.get("key")),
            ("content_seen:v0:item", lambda storage: storage.has_seen("item")),
        ):
            with self.subTest(key=key), tempfile.TemporaryDirectory() as tmpdir:
                started = asyncio.Event()
                resume = asyncio.Event()
                stored = {key: '{"id":"item","expire_at":9999999999}'}

                async def get_kv_data(current_key, default=None):
                    value = stored.get(current_key, default)
                    if current_key == key and not started.is_set():
                        started.set()
                        await resume.wait()
                    return value

                async def delete_kv_data(current_key):
                    stored.pop(current_key, None)

                reader = FeedStorage(storage_dir=tmpdir, get_kv_data=get_kv_data)
                writer = FeedStorage(storage_dir=tmpdir, delete_kv_data=delete_kv_data)
                pending = asyncio.create_task(lookup(reader))
                try:
                    await asyncio.wait_for(started.wait(), timeout=2)
                    await asyncio.wait_for(writer.delete(key), timeout=2)
                finally:
                    resume.set()
                    result = await asyncio.wait_for(pending, timeout=2)
                self.assertFalse(result)
                self.assertFalse((Path(tmpdir) / "state.json").exists())

    async def test_failed_atomic_replace_preserves_previous_state_and_allows_retry(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = FeedStorage(storage_dir=tmpdir)
            await storage.mark_seen("existing")
            state_path = Path(tmpdir) / "state.json"
            original = state_path.read_bytes()

            def partial_dump(state, fp, **kwargs):
                fp.write('{"partial":')
                raise OSError("partial write")

            for failure in (
                patch("storage.json.dump", side_effect=partial_dump),
                patch("storage.os.replace", side_effect=OSError("replace failed")),
            ):
                with failure, self.assertRaises(OSError):
                    await storage.mark_seen("new")
                self.assertEqual(state_path.read_bytes(), original)
                self.assertFalse(await storage.has_seen("new"))
                self.assertEqual(
                    {entry.name for entry in Path(tmpdir).iterdir()}, {"state.json", "state.lock"}
                )
            await storage.mark_seen("new")
            restored = FeedStorage(storage_dir=tmpdir)
            self.assertTrue(await restored.has_seen("existing"))
            self.assertTrue(await restored.has_seen("new"))

    async def test_invalid_state_is_not_overwritten_by_any_write_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = FeedStorage(storage_dir=tmpdir)
            await storage.put("original", True)
            self.assertTrue(await storage.get("original"))
            state_path = Path(tmpdir) / "state.json"
            original = state_path.read_bytes()
            for damaged in (b'{"kv": {"original": true', b'{"kv": []}', b'[]'):
                state_path.write_bytes(damaged)
                with self.subTest(damaged=damaged):
                    for operation in (
                        lambda: storage.put("new", True),
                        lambda: storage.confirm_dispatch("fingerprint"),
                        lambda: storage.mark_seen_many(["new"]),
                        lambda: storage.update_daily_digest_status("digest", last_item_count=1),
                    ):
                        with self.assertRaises(ValueError):
                            await operation()
                        self.assertEqual(state_path.read_bytes(), damaged)
            state_path.write_bytes(original)
            await storage.put("new", True)
            self.assertTrue(await storage.get("original"))
            self.assertTrue(await storage.get("new"))

    async def test_cancelled_atomic_write_keeps_lock_until_background_replace_finishes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            first = FeedStorage(storage_dir=tmpdir)
            second = FeedStorage(storage_dir=tmpdir)
            await first.put("original", True)
            self.assertIsNone(await first.get("first"))
            loop = asyncio.get_running_loop()
            entered_replace = asyncio.Event()
            allow_replace = threading.Event()
            replace = os.replace
            paused = threading.Event()

            def pause_first_replace(source, destination):
                if not paused.is_set():
                    paused.set()
                    loop.call_soon_threadsafe(entered_replace.set)
                    if not allow_replace.wait(timeout=3):
                        raise TimeoutError("replace blocked the event loop")
                replace(source, destination)

            with patch("storage.os.replace", side_effect=pause_first_replace):
                writer = asyncio.create_task(first.put("first", True))
                follower = None
                try:
                    await asyncio.wait_for(entered_replace.wait(), timeout=2)
                    self.assertIsNone(await first.get("first"))
                    writer.cancel()
                    await asyncio.sleep(0)
                    writer.cancel()
                    await asyncio.sleep(0)
                    self.assertFalse(writer.done())
                    if fcntl is not None:
                        with (Path(tmpdir) / "state.lock").open() as fp, self.assertRaises(BlockingIOError):
                            fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    follower = asyncio.create_task(second.put("second", True))
                    await asyncio.sleep(0)
                    self.assertFalse(follower.done())
                finally:
                    allow_replace.set()
                    await asyncio.gather(
                        writer, *([follower] if follower is not None else []), return_exceptions=True
                    )

            self.assertTrue(writer.cancelled())
            follower.result()
            restored = FeedStorage(storage_dir=tmpdir)
            for key in ("original", "first", "second"):
                self.assertTrue(await restored.get(key))

    async def test_large_state_misses_and_reads_after_commit_reuse_snapshot(self):
        lookups = set()

        async def get_kv_data(key, default=None):
            lookups.add(key)
            return default

        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "state.json"
            with state_path.open("w", encoding="utf-8") as fp:
                json.dump({"kv": {f"existing:{i}": "x" * 580 for i in range(52_798)}}, fp)
            self.assertGreater(state_path.stat().st_size, 31_000_000)
            storage = FeedStorage(storage_dir=tmpdir, get_kv_data=get_kv_data)
            with patch("storage.json.load", wraps=json.load) as load:
                for i in range(100):
                    self.assertFalse(await storage.has_seen(f"item-{i}"))
                    self.assertIsNone(await storage.get(f"missing:{i}"))
                self.assertEqual(load.call_count, 1)
                self.assertGreaterEqual(len(lookups), 200)
                self.assertFalse((Path(tmpdir) / "state.lock").exists())

                saved_value = {"field": "committed"}
                await storage.put("saved", saved_value)
                saved_value["field"] = "caller-side mutation"
                loaded_value = await storage.get("saved")
                self.assertEqual(loaded_value, {"field": "committed"})
                loaded_value["field"] = "read-side mutation"
                self.assertEqual(await storage.get("saved"), {"field": "committed"})
                self.assertEqual(await storage.get("existing:0"), "x" * 580)
                self.assertEqual(load.call_count, 2)

    async def test_mark_seen_many_commits_100_keys_in_one_write(self):
        keys = [f"item-{i}" for i in range(100)]
        with tempfile.TemporaryDirectory() as single_dir, tempfile.TemporaryDirectory() as batch_dir:
            single = FeedStorage(storage_dir=single_dir)
            batch = FeedStorage(storage_dir=batch_dir)
            with patch("storage.time.time", return_value=1_000_000):
                with patch.object(single, "_write_disk_state", wraps=single._write_disk_state) as write:
                    for key in keys:
                        await single.mark_seen(key, ttl_seconds=60)
                    single_writes = write.call_count

                with patch.object(batch, "_write_disk_state", wraps=batch._write_disk_state) as write:
                    await batch.mark_seen_many([], ttl_seconds=60)
                    await batch.mark_seen_many(keys + [keys[0]], ttl_seconds=60)
                    self.assertEqual(write.call_count, 1)

                self.assertEqual(single_writes, 100)
                restored = FeedStorage(storage_dir=batch_dir)
                self.assertTrue(all([await restored.has_seen(key) for key in keys]))
                self.assertEqual(await restored.get(FeedStorage.CONTENT_INDEX_KEY), keys)

            self.assertEqual(
                (Path(single_dir) / "state.json").read_bytes(),
                (Path(batch_dir) / "state.json").read_bytes(),
            )

    async def test_expired_seen_record_is_removed_from_index_and_backend(self):
        stored = {}

        async def get_kv_data(key, default=None):
            return stored.get(key, default)

        async def put_kv_data(key, value):
            stored[key] = value

        async def delete_kv_data(key):
            stored.pop(key, None)

        with tempfile.TemporaryDirectory() as tmpdir:
            storage = FeedStorage(
                get_kv_data=get_kv_data,
                put_kv_data=put_kv_data,
                delete_kv_data=delete_kv_data,
                storage_dir=tmpdir,
            )
            with patch("storage.time.time", return_value=1_000_000):
                await storage.mark_seen("expired", ttl_seconds=5)
                await storage.mark_seen("active", ttl_seconds=50)
            with patch("storage.time.time", return_value=1_000_006):
                self.assertFalse(await storage.has_seen("expired"))
                self.assertTrue(await storage.has_seen("active"))
                self.assertEqual(await storage.get(FeedStorage.CONTENT_INDEX_KEY), ["active"])
                self.assertEqual(json.loads(stored[FeedStorage.CONTENT_INDEX_KEY]), ["active"])
                self.assertNotIn("content_seen:v0:expired", stored)
                self.assertEqual(await storage.clear_seen(), 1)

    async def test_candidate_and_digest_reads_filter_expired_records_without_writing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = FeedStorage(storage_dir=tmpdir)
            item = {"guid": "item", "title": "Title", "feed_id": "feed"}
            state_path = Path(tmpdir) / "state.json"

            async def list_digest():
                return await storage.list_digest_items(
                    ["feed"], window_start_ts=0, window_end_ts=2_000_000,
                    limit=10, retention_seconds=60,
                )

            async def list_semantic():
                return await storage.list_semantic_dedup_records("job", limit=10, ttl_seconds=60)

            with patch("storage.time.time", return_value=1_000_000):
                await storage.archive_digest_items([item])
                await storage.put_semantic_dedup_record(
                    "job", item, seen_keys=["item"], ttl_seconds=60
                )
                original = state_path.read_bytes()
                with patch.object(storage, "_write_disk_state", wraps=storage._write_disk_state) as write:
                    self.assertEqual(len(await list_digest()), 1)
                    self.assertEqual(len(await list_semantic()), 1)
                    self.assertEqual(await storage.get_daily_digest_status("missing"), {})
                    write.assert_not_called()

            with patch("storage.time.time", return_value=1_000_061):
                self.assertEqual(await list_digest(), [])
                self.assertEqual(await list_semantic(), [])
                self.assertEqual(state_path.read_bytes(), original)

    async def test_migrates_legacy_plugin_state_directory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            legacy_dir = root / "astrbot_rss"
            current_dir = root / "astrbot_plugin_rss_forwarder"
            legacy_dir.mkdir(parents=True)
            (legacy_dir / "state.json").write_text(
                json.dumps(
                    {
                        "kv": {
                            "content_seen:v0:item-1": {
                                "id": "item-1",
                                "expire_at": int(time.time()) + 3600,
                                "updated_at": int(time.time()),
                            }
                        }
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            storage = FeedStorage(
                plugin_name="astrbot_plugin_rss_forwarder",
                storage_dir=current_dir,
                legacy_storage_dirs=[legacy_dir],
            )

            self.assertTrue(await storage.has_seen("item-1"))
            self.assertTrue((current_dir / "state.json").exists())

    async def test_expired_record_can_use_longer_effective_ttl(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = FeedStorage(storage_dir=tmpdir)
            original_time = time.time
            try:
                time.time = lambda: 1_000_000
                await storage.mark_seen("item-1", ttl_seconds=7 * 24 * 60 * 60)

                time.time = lambda: 1_000_000 + 8 * 24 * 60 * 60
                self.assertTrue(
                    await storage.has_seen("item-1", ttl_seconds=50 * 24 * 60 * 60)
                )
            finally:
                time.time = original_time

            state_path = Path(tmpdir) / "state.json"
            payload = json.loads(state_path.read_text(encoding="utf-8"))
            record = payload["kv"]["content_seen:v0:item-1"]
            self.assertEqual(record["expire_at"], 1_000_000 + 50 * 24 * 60 * 60)

    def test_build_seen_keys_include_normalized_link_fingerprint(self):
        storage = FeedStorage(storage_dir=".")

        keys = storage.build_seen_keys(
            {
                "guid": "guid-1",
                "link": "HTTPS://Example.com/path?a=1#fragment",
            }
        )

        self.assertEqual(keys[0], "guid-1")
        self.assertEqual(len(keys), 2)
        self.assertEqual(keys[1], storage.build_link_fingerprint({"link": "https://example.com/path?a=1"}))

    def test_build_link_fingerprint_returns_empty_for_missing_link(self):
        storage = FeedStorage(storage_dir=".")

        self.assertEqual(storage.build_link_fingerprint({"link": ""}), "")
        self.assertEqual(storage.build_seen_keys({"guid": "guid-1"}), ["guid-1"])

    async def test_reads_legacy_backend_keys_and_migrates_them(self):
        stored: dict[str, str] = {
            "content_seen_version": "1",
            "content_seen:legacy-item": json.dumps(
                {"id": "legacy-item", "expire_at": 9999999999, "updated_at": 1},
                ensure_ascii=False,
            )
        }

        async def get_kv_data(key: str, default=None):
            return stored.get(key, default)

        async def put_kv_data(key: str, value):
            stored[key] = value

        with tempfile.TemporaryDirectory() as tmpdir:
            storage = FeedStorage(
                get_kv_data=get_kv_data,
                put_kv_data=put_kv_data,
                storage_dir=tmpdir,
            )
            self.assertTrue(await storage.has_seen("legacy-item"))
            self.assertIn("content_seen:v1:legacy-item", stored)

    async def test_dispatch_guard_claim_confirm_and_release(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = FeedStorage(storage_dir=tmpdir)

            self.assertTrue(await storage.claim_dispatch("fingerprint-1", ttl_seconds=30))
            self.assertFalse(await storage.claim_dispatch("fingerprint-1", ttl_seconds=30))

            await storage.release_dispatch("fingerprint-1")
            self.assertTrue(await storage.claim_dispatch("fingerprint-1", ttl_seconds=30))

            await storage.confirm_dispatch("fingerprint-1", ttl_seconds=3600)
            self.assertFalse(await storage.claim_dispatch("fingerprint-1", ttl_seconds=30))

    async def test_dispatch_guard_claim_status_distinguishes_pending_sent_and_expired(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            first = FeedStorage(storage_dir=tmpdir)
            second = FeedStorage(storage_dir=tmpdir)
            fingerprint = "fingerprint"
            with patch("storage.time.time", return_value=1_000_000):
                results = await asyncio.gather(
                    first.claim_dispatch_status(fingerprint, ttl_seconds=2),
                    second.claim_dispatch_status(fingerprint, ttl_seconds=2),
                )
                self.assertCountEqual(results, ["claimed", "pending"])

            with patch("storage.time.time", return_value=1_000_002):
                self.assertEqual(await second.claim_dispatch_status(fingerprint), "pending")

            with patch("storage.time.time", return_value=1_000_003):
                restarted = FeedStorage(storage_dir=tmpdir)
                self.assertEqual(await restarted.claim_dispatch_status(fingerprint), "claimed")
                await restarted.confirm_dispatch(fingerprint, ttl_seconds=5)

            with patch("storage.time.time", return_value=1_000_008):
                await first.release_dispatch(fingerprint)
                self.assertEqual(await first.claim_dispatch_status(fingerprint), "sent")

            with patch("storage.time.time", return_value=1_000_009):
                self.assertEqual(await second.claim_dispatch_status(fingerprint), "claimed")
                self.assertEqual(await first.claim_dispatch_status(fingerprint), "pending")

    async def test_dispatch_guard_claim_status_requires_explicit_send_confirmation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = FeedStorage(storage_dir=tmpdir)
            with patch("storage.time.time", return_value=1_000_000):
                await storage.put("dispatch_guard:legacy", {"expire_at": 1_000_005})
                self.assertEqual(await storage.claim_dispatch_status("legacy"), "pending")
                self.assertFalse(await storage.claim_dispatch("legacy"))
                self.assertEqual(await storage.claim_dispatch_status(" "), "claimed")
                self.assertTrue(await storage.claim_dispatch(" "))
                self.assertIsNone(await storage.get("dispatch_guard:"))

    async def test_archive_digest_items_and_query_window(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = FeedStorage(storage_dir=tmpdir)
            now_ts = 1774699200
            item = {
                "feed_id": "feed-1",
                "feed_title": "Feed",
                "guid": "guid-1",
                "title": "Title",
                "summary": "Summary",
                "link": "https://example.com/post/1",
                "published_at": "2026-03-28T06:00:00+00:00",
                "image_url": "https://example.com/a.jpg",
            }

            original_time = time.time
            try:
                time.time = lambda: now_ts
                await storage.archive_digest_items([item])
                await storage.archive_digest_items([dict(item, title="Updated Title")])
            finally:
                time.time = original_time

            try:
                time.time = lambda: now_ts
                items = await storage.list_digest_items(
                    ["feed-1"],
                    window_start_ts=1774656000,
                    window_end_ts=1774742400,
                    limit=10,
                )
            finally:
                time.time = original_time

            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["title"], "Updated Title")
            self.assertEqual(items[0]["item_key"], "guid-1")

    async def test_list_digest_items_falls_back_to_collected_at_when_published_at_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = FeedStorage(storage_dir=tmpdir)
            now_ts = 1774699200
            item = {
                "feed_id": "feed-1",
                "feed_title": "Feed",
                "guid": "guid-2",
                "title": "Collected Title",
                "summary": "Summary",
                "link": "https://example.com/post/2",
                "published_at": "",
            }

            original_time = time.time
            try:
                time.time = lambda: now_ts
                await storage.archive_digest_items([item])
            finally:
                time.time = original_time

            try:
                time.time = lambda: now_ts
                items = await storage.list_digest_items(
                    ["feed-1"],
                    window_start_ts=1774695600,
                    window_end_ts=1774702800,
                    limit=10,
                )
            finally:
                time.time = original_time

            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["title"], "Collected Title")

    async def test_daily_digest_status_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = FeedStorage(storage_dir=tmpdir)

            await storage.update_daily_digest_status(
                "digest-1",
                last_schedule_date="2026-03-27",
                last_item_count=5,
                last_error="",
            )
            status = await storage.get_daily_digest_status("digest-1")

            self.assertEqual(status["last_schedule_date"], "2026-03-27")
            self.assertEqual(status["last_item_count"], 5)

    async def test_semantic_dedup_records_are_pruned_by_ttl(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = FeedStorage(storage_dir=tmpdir)
            original_time = time.time
            try:
                time.time = lambda: 1_000_000
                await storage.put_semantic_dedup_record(
                    "job-1",
                    {
                        "feed_id": "feed-1",
                        "feed_title": "Tom's Hardware",
                        "guid": "guid-1",
                        "title": "NVIDIA launches RTX 6090",
                        "summary": "NVIDIA announced a new GPU.",
                        "link": "https://example.com/a",
                        "published_at": "2026-05-11T00:00:00+00:00",
                    },
                    seen_keys=["guid-1"],
                    ttl_seconds=60,
                )

                records = await storage.list_semantic_dedup_records(
                    "job-1",
                    limit=10,
                    ttl_seconds=60,
                )
                self.assertEqual(len(records), 1)
                self.assertEqual(records[0]["title"], "NVIDIA launches RTX 6090")

                time.time = lambda: 1_000_061
                records = await storage.list_semantic_dedup_records(
                    "job-1",
                    limit=10,
                    ttl_seconds=60,
                )
                self.assertEqual(records, [])
            finally:
                time.time = original_time

    async def test_clear_seen_also_clears_semantic_dedup_records(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = FeedStorage(storage_dir=tmpdir)
            await storage.put_semantic_dedup_record(
                "job-1",
                {"guid": "guid-1", "title": "Title", "summary": "Summary"},
                seen_keys=["guid-1"],
                ttl_seconds=3600,
            )

            await storage.clear_seen()

            self.assertEqual(
                await storage.list_semantic_dedup_records("job-1", limit=10, ttl_seconds=3600),
                [],
            )


if __name__ == "__main__":
    unittest.main()
