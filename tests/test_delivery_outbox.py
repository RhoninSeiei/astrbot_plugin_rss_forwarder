import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from delivery_outbox import DeliveryCandidate, DeliveryOutbox


class DeliveryOutboxTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "queue.sqlite3"
        self.outbox = DeliveryOutbox(self.path)

    @staticmethod
    def candidate(key, *, feed="feed", origins=("first", "second"), alias=""):
        return DeliveryCandidate({"feed_id": feed, "guid": key}, (key, alias) if alias else (key,), origins)

    def page(self, limit, *, job="job", origins=("first", "second"), feeds=("feed",)):
        return self.outbox.entries(job, limit, feed_ids=feeds, origins=origins)

    def test_bounded_fifo_page_rotates_failed_items_behind_unattempted_items(self):
        self.outbox.enqueue("job", [self.candidate(str(index)) for index in range(50)])
        page = self.page(3)
        self.assertEqual([entry.item["guid"] for entry in page], ["0", "1", "2"])
        self.outbox.mark_attempted(page)
        self.outbox = DeliveryOutbox(self.path)
        self.assertEqual([entry.item["guid"] for entry in self.page(3)], ["3", "4", "5"])
        self.assertEqual(self.outbox.status("job"), (50, 100, None))

    def test_duplicate_alias_does_not_expand_targets_and_rejection_is_not_a_receipt(self):
        self.outbox.enqueue("job", [self.candidate("original", alias="same-link", origins=("first",))])
        self.outbox.enqueue("job", [self.candidate("replacement", alias="same-link", origins=("second",))])
        entry, = self.page(2)
        self.assertEqual(entry.origins, ("first",))
        self.outbox.confirm("job", [], rejected=[(entry.row_id, "first")], ttl_seconds=60)
        restored = DeliveryOutbox(self.path)
        self.assertEqual(restored.status("job"), (0, 0, None))
        self.assertEqual(restored.entries("job", 2, confirmed=True), [])
        self.assertEqual(restored.rejected_origins("job", ("same-link",)), {"same-link": {"first"}})

    def test_pruning_drops_removed_scopes_without_discarding_receipts(self):
        self.outbox.enqueue("job", [self.candidate("current"), self.candidate("retired", feed="old-feed")],
                            feed_updates={"feed": {"etag": "new"}, "old-feed": {"etag": "old"}})
        self.outbox.enqueue("old-job", [self.candidate("old-job-item")], feed_updates={"feed": {}})
        kept, removed = self.page(2, feeds=("feed", "old-feed"))
        stamp = datetime(2026, 1, 1, 12, 0)
        self.outbox.confirm("job", [(removed.row_id, "first")], stamp)
        self.outbox.prune_job("job", ("feed",), ("second",))
        self.outbox.prune_removed_jobs(("job",))
        self.assertEqual(self.outbox.feed_states("job"), {"feed": {"etag": "new"}})
        self.assertEqual(self.outbox.feed_states("old-job"), {})
        self.assertEqual(self.outbox.status("old-job"), (0, 0, None))
        self.assertEqual(self.outbox.status("job"), (1, 1, stamp))
        self.assertEqual(self.page(2)[0].row_id, kept.row_id)
        receipt, = self.outbox.entries("job", 2, confirmed=True)
        self.assertEqual(receipt.confirmed_origins, ("first",))
        self.assertEqual(receipt.row_id, removed.row_id)


if __name__ == "__main__":
    unittest.main()
