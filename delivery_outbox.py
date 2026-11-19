import json
import sqlite3
import time
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime
from itertools import groupby
from pathlib import Path
from typing import Any, Iterator
from weakref import finalize


@dataclass(slots=True)
class DeliveryCandidate:
    item: dict[str, Any]
    seen_keys: tuple[str, ...]
    origins: tuple[str, ...]


@dataclass(slots=True)
class DeliveryEntry(DeliveryCandidate):
    row_id: int
    attempted: bool
    confirmed_origins: tuple[str, ...]


class DeliveryOutbox:
    """Job-local item snapshots and durable per-origin delivery receipts."""

    def __init__(self, path: Path | None) -> None:
        self._path = path
        # Scheduler operations stay on one loop, but finalization can run on another thread.
        self._memory = sqlite3.connect(":memory:", check_same_thread=False) if path is None else None
        if self._memory is not None:
            finalize(self, self._memory.close)
        self._initialized = False

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        if self._memory is not None:
            with self._memory:
                self._initialize(self._memory)
                yield self._memory
        else:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(self._path)) as connection, connection:
                connection.execute("PRAGMA foreign_keys = ON")
                self._initialize(connection)
                yield connection

    def _initialize(self, connection: sqlite3.Connection) -> None:
        if self._initialized:
            return
        connection.executescript("""
            PRAGMA foreign_keys = ON;
            CREATE TABLE IF NOT EXISTS outbox_item (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL,
                item_key TEXT NOT NULL,
                feed_id TEXT NOT NULL,
                seen_keys TEXT NOT NULL,
                payload TEXT NOT NULL,
                touched_ns INTEGER NOT NULL,
                attempted INTEGER NOT NULL DEFAULT 0,
                UNIQUE (job_id, item_key)
            );
            CREATE INDEX IF NOT EXISTS outbox_order ON outbox_item (job_id, touched_ns, id);
            CREATE TABLE IF NOT EXISTS outbox_key (
                job_id TEXT NOT NULL,
                seen_key TEXT NOT NULL,
                item_id INTEGER NOT NULL REFERENCES outbox_item(id) ON DELETE CASCADE,
                PRIMARY KEY (job_id, seen_key)
            );
            CREATE INDEX IF NOT EXISTS outbox_key_item ON outbox_key (item_id);
            CREATE TABLE IF NOT EXISTS outbox_target (
                item_id INTEGER NOT NULL REFERENCES outbox_item(id) ON DELETE CASCADE,
                origin TEXT NOT NULL,
                confirmed INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (item_id, origin)
            );
            CREATE TABLE IF NOT EXISTS outbox_job (
                job_id TEXT PRIMARY KEY,
                last_delivery_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS job_feed_cursor (
                job_id TEXT NOT NULL,
                feed_id TEXT NOT NULL,
                state TEXT NOT NULL,
                PRIMARY KEY (job_id, feed_id)
            );
            CREATE TABLE IF NOT EXISTS rejected_target (
                job_id TEXT NOT NULL,
                seen_key TEXT NOT NULL,
                origin TEXT NOT NULL,
                expires_at INTEGER NOT NULL,
                PRIMARY KEY (job_id, seen_key, origin)
            );
            CREATE INDEX IF NOT EXISTS rejected_expiry ON rejected_target (expires_at);
        """)
        self._initialized = True

    def enqueue(
        self, job_id: str, candidates: list[DeliveryCandidate], *, feed_updates: dict[str, dict] | None = None,
    ) -> None:
        if not candidates and not feed_updates:
            return
        touched_ns = time.time_ns()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for candidate in candidates:
                if not candidate.origins or self._matching_keys(connection, job_id, candidate.seen_keys):
                    continue
                payload = {key: value for key, value in candidate.item.items() if key != "proxy_url"}
                cursor = connection.execute(
                    """INSERT INTO outbox_item (job_id, item_key, feed_id, seen_keys, payload, touched_ns)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (job_id, candidate.seen_keys[0], str(payload.get("feed_id", "")).strip(),
                     json.dumps(candidate.seen_keys), json.dumps(payload, ensure_ascii=False), touched_ns),
                )
                connection.executemany(
                    "INSERT INTO outbox_target (item_id, origin) VALUES (?, ?)",
                    ((cursor.lastrowid, origin) for origin in dict.fromkeys(candidate.origins)),
                )
                connection.executemany(
                    "INSERT INTO outbox_key (job_id, seen_key, item_id) VALUES (?, ?, ?)",
                    ((job_id, key, cursor.lastrowid) for key in dict.fromkeys(candidate.seen_keys)),
                )
            if feed_updates:
                connection.executemany(
                    """INSERT INTO job_feed_cursor (job_id, feed_id, state) VALUES (?, ?, ?)
                       ON CONFLICT (job_id, feed_id) DO UPDATE SET state = excluded.state""",
                    ((job_id, feed_id, json.dumps(state)) for feed_id, state in feed_updates.items()),
                )

    def feed_states(self, job_id: str) -> dict[str, dict]:
        with self._connect() as connection:
            return {feed_id: json.loads(state) for feed_id, state in connection.execute(
                "SELECT feed_id, state FROM job_feed_cursor WHERE job_id = ?", (job_id,),
            )}

    def feed_scopes(self) -> set[tuple[str, str]]:
        with self._connect() as connection:
            return set(connection.execute("SELECT job_id, feed_id FROM job_feed_cursor"))

    def seed_feed_states(self, states: list[tuple[str, str, dict]]) -> None:
        with self._connect() as connection:
            connection.executemany(
                "INSERT OR IGNORE INTO job_feed_cursor (job_id, feed_id, state) VALUES (?, ?, ?)",
                ((job_id, feed_id, json.dumps(state)) for job_id, feed_id, state in states),
            )

    @staticmethod
    def _matching_keys(connection, job_id: str, keys: tuple[str, ...]) -> set[str]:
        matched = set()
        for offset in range(0, len(keys), 300):
            chunk = keys[offset:offset + 300]
            marks = ",".join("?" for _ in chunk)
            matched.update(row[0] for row in connection.execute(
                f"SELECT seen_key FROM outbox_key WHERE job_id = ? AND seen_key IN ({marks})",
                (job_id, *chunk),
            ))
        return matched

    def matching_keys(self, job_id: str, keys: tuple[str, ...]) -> set[str]:
        if not keys:
            return set()
        with self._connect() as connection:
            return self._matching_keys(connection, job_id, keys)

    def rejected_origins(self, job_id: str, keys: tuple[str, ...]) -> dict[str, set[str]]:
        if not keys:
            return {}
        rejected: dict[str, set[str]] = {}
        now = int(time.time())
        with self._connect() as connection:
            connection.execute(
                """DELETE FROM rejected_target WHERE rowid IN
                   (SELECT rowid FROM rejected_target WHERE expires_at < ? LIMIT 256)""",
                (now,),
            )
            for offset in range(0, len(keys), 300):
                chunk = keys[offset:offset + 300]
                marks = ",".join("?" for _ in chunk)
                for key, origin in connection.execute(
                    f"""SELECT seen_key, origin FROM rejected_target
                        WHERE job_id = ? AND expires_at >= ? AND seen_key IN ({marks})""",
                    (job_id, now, *chunk),
                ):
                    rejected.setdefault(key, set()).add(origin)
        return rejected

    def entries(
        self,
        job_id: str,
        limit: int,
        *,
        feed_ids: tuple[str, ...] = (),
        origins: tuple[str, ...] = (),
        confirmed: bool = False,
    ) -> list[DeliveryEntry]:
        if limit <= 0 or (not confirmed and (not feed_ids or not origins)):
            return []
        feed_marks = ",".join("?" for _ in feed_ids)
        origin_marks = ",".join("?" for _ in origins)
        feed_filter = "" if confirmed else f"AND i.feed_id IN ({feed_marks})"
        origin_filter = "" if confirmed else f"AND t.origin IN ({origin_marks})"
        with self._connect() as connection:
            rows = connection.execute(
                f"""WITH page AS (
                        SELECT i.id FROM outbox_item i WHERE i.job_id = ? {feed_filter}
                        AND EXISTS (SELECT 1 FROM outbox_target t WHERE t.item_id = i.id
                                    AND t.confirmed = ? {origin_filter})
                        ORDER BY i.touched_ns, i.id LIMIT ?
                    )
                    SELECT i.id, i.payload, i.seen_keys, i.attempted, t.origin, t.confirmed
                    FROM page JOIN outbox_item i ON i.id = page.id
                    JOIN outbox_target t ON t.item_id = i.id WHERE t.confirmed = ? {origin_filter}
                    ORDER BY i.touched_ns, i.id, t.origin""",
                (job_id, *feed_ids, int(confirmed), *origins, limit, int(confirmed), *origins),
            )
            entries = []
            for row_id, group in groupby(rows, key=lambda row: row[0]):
                records = list(group)
                _, payload, keys, attempted, _, _ = records[0]
                entries.append(DeliveryEntry(
                    item=json.loads(payload), seen_keys=tuple(json.loads(keys)), row_id=row_id,
                    attempted=bool(attempted), origins=tuple(row[4] for row in records if not row[5]),
                    confirmed_origins=tuple(row[4] for row in records if row[5]),
                ))
            return entries

    def prune_job(self, job_id: str, feed_ids: tuple[str, ...], origins: tuple[str, ...]) -> None:
        feed_marks = ",".join("?" for _ in feed_ids) or "NULL"
        origin_marks = ",".join("?" for _ in origins) or "NULL"
        with self._connect() as connection:
            connection.execute(
                f"""DELETE FROM outbox_target WHERE confirmed = 0
                    AND item_id IN (SELECT id FROM outbox_item WHERE job_id = ?)
                    AND (? = 0 OR origin NOT IN ({origin_marks}) OR item_id IN
                        (SELECT id FROM outbox_item WHERE job_id = ? AND
                         (? = 0 OR feed_id NOT IN ({feed_marks}))))""",
                (job_id, len(origins), *origins, job_id, len(feed_ids), *feed_ids),
            )
            connection.execute(
                """DELETE FROM outbox_item WHERE job_id = ? AND NOT EXISTS
                   (SELECT 1 FROM outbox_target WHERE item_id = outbox_item.id)""",
                (job_id,),
            )
            connection.execute(
                f"DELETE FROM job_feed_cursor WHERE job_id = ? AND (? = 0 OR feed_id NOT IN ({feed_marks}))",
                (job_id, len(feed_ids), *feed_ids),
            )
            connection.execute(
                f"DELETE FROM rejected_target WHERE job_id = ? AND (? = 0 OR origin NOT IN ({origin_marks}))",
                (job_id, len(origins), *origins),
            )

    def prune_removed_jobs(self, job_ids: tuple[str, ...]) -> None:
        marks = ",".join("?" for _ in job_ids) or "NULL"
        with self._connect() as connection:
            for table in ("outbox_item", "outbox_job", "job_feed_cursor", "rejected_target"):
                connection.execute(
                    f"DELETE FROM {table} WHERE ? = 0 OR job_id NOT IN ({marks})",
                    (len(job_ids), *job_ids),
                )

    def mark_attempted(self, entries: list[DeliveryEntry]) -> None:
        with self._connect() as connection:
            connection.executemany(
                "UPDATE outbox_item SET attempted = 1, touched_ns = ? WHERE id = ?",
                ((time.time_ns(), entry.row_id) for entry in entries),
            )

    def confirm(
        self,
        job_id: str,
        resolutions: list[tuple[int, str]],
        delivered_at: datetime | None = None,
        *,
        rejected: list[tuple[int, str]] | None = None,
        ttl_seconds: int = 0,
    ) -> None:
        if not resolutions and not rejected and delivered_at is None:
            return
        with self._connect() as connection:
            # Keep receipts until FeedStorage has accepted the corresponding seen keys.
            connection.executemany(
                "UPDATE outbox_target SET confirmed = 1 WHERE item_id = ? AND origin = ?",
                resolutions,
            )
            if rejected:
                expires_at = int(time.time()) + max(ttl_seconds, 0)
                connection.executemany(
                    """INSERT INTO rejected_target (job_id, seen_key, origin, expires_at)
                       SELECT job_id, seen_key, ?, ? FROM outbox_key WHERE item_id = ?
                       ON CONFLICT (job_id, seen_key, origin) DO UPDATE SET expires_at = excluded.expires_at""",
                    ((origin, expires_at, row_id) for row_id, origin in rejected),
                )
                self._remove(connection, rejected)
            if delivered_at is not None:
                connection.execute(
                    """INSERT INTO outbox_job (job_id, last_delivery_at) VALUES (?, ?)
                       ON CONFLICT (job_id) DO UPDATE SET last_delivery_at = excluded.last_delivery_at""",
                    (job_id, delivered_at.isoformat()),
                )

    def remove(self, resolutions: list[tuple[int, str]]) -> None:
        if not resolutions:
            return
        with self._connect() as connection:
            self._remove(connection, resolutions)

    @staticmethod
    def _remove(connection: sqlite3.Connection, resolutions: list[tuple[int, str]]) -> None:
        connection.executemany(
            "DELETE FROM outbox_target WHERE item_id = ? AND origin = ?", resolutions,
        )
        connection.executemany(
            """DELETE FROM outbox_item WHERE id = ? AND NOT EXISTS
               (SELECT 1 FROM outbox_target WHERE item_id = outbox_item.id)""",
            ((row_id,) for row_id in {row_id for row_id, _ in resolutions}),
        )

    def status(self, job_id: str) -> tuple[int, int, datetime | None]:
        with self._connect() as connection:
            items, targets = connection.execute(
                """SELECT COUNT(DISTINCT i.id), COUNT(*) FROM outbox_item i
                   JOIN outbox_target t ON t.item_id = i.id WHERE i.job_id = ? AND t.confirmed = 0""",
                (job_id,),
            ).fetchone()
            delivery = connection.execute(
                "SELECT last_delivery_at FROM outbox_job WHERE job_id = ?", (job_id,),
            ).fetchone()
            return items, targets, datetime.fromisoformat(delivery[0]) if delivery else None
