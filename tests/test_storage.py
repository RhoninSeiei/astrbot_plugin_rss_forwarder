import json
import tempfile
import time
import unittest
from pathlib import Path

from storage import FeedStorage


class FeedStorageTests(unittest.IsolatedAsyncioTestCase):
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
            storage._dedup_version = 1

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
