import asyncio
import hashlib
import json
import os
import shutil
import tempfile
import threading
import time
from collections.abc import Iterable
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal
from urllib.parse import urlsplit, urlunsplit

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback.
    fcntl = None

try:
    from astrbot.api.star import StarTools
except ImportError:  # pragma: no cover - unit tests may run without AstrBot runtime.
    StarTools = None


class FeedStorage:
    """存储层：负责去重、游标和持久化。"""

    FEED_STATE_PREFIX = "feed_state:"
    CONTENT_KEY_PREFIX = "content_seen:"
    CONTENT_INDEX_KEY = "content_seen_index"
    DEDUP_VERSION_KEY = "content_seen_version"
    SUBSCRIPTION_REGISTRY_KEY = "subscription_registry:v1"
    SUBSCRIPTION_STATE_PREFIX = "subscription_initialized:v1:"
    DISPATCH_GUARD_PREFIX = "dispatch_guard:"
    SEMANTIC_DEDUP_SECTION = "semantic_dedup"
    DAILY_DIGEST_RETENTION_SECONDS = 30 * 24 * 60 * 60
    _fallback_lock = threading.Lock()

    def __init__(
        self,
        plugin_name: str = "astrbot_plugin_rss_forwarder",
        get_kv_data: Callable[[str], Awaitable[Any]] | None = None,
        put_kv_data: Callable[[str, Any], Awaitable[Any]] | None = None,
        delete_kv_data: Callable[[str], Awaitable[Any]] | None = None,
        storage_dir: str | Path | None = None,
        legacy_plugin_names: list[str] | None = None,
        legacy_storage_dirs: list[str | Path] | None = None,
    ) -> None:
        self._plugin_name = plugin_name
        self._get_kv_data = get_kv_data
        self._put_kv_data = put_kv_data
        self._delete_kv_data = delete_kv_data
        cache_root = Path(storage_dir) if storage_dir is not None else self.plugin_cache_dir()
        self._state_path = cache_root / "state.json"
        candidates = []
        if not self._state_path.exists():
            candidates.extend(Path(path) for path in legacy_storage_dirs or [])
            if storage_dir is None:
                for legacy_name in legacy_plugin_names or []:
                    legacy_name = str(legacy_name or "").strip()
                    if not legacy_name or legacy_name == self._plugin_name:
                        continue
                    candidates.append(self._resolve_plugin_cache_dir(legacy_name))
        self._legacy_state_paths = [directory / "state.json" for directory in candidates]
        self._snapshot_id: tuple[int, ...] | None = None
        self._snapshot: dict[str, Any] = {"kv": {}}

    async def _migrate_legacy_state(self) -> None:
        if not self._legacy_state_paths:
            return
        if not self._state_path.exists():
            for legacy_state in self._legacy_state_paths:
                if not legacy_state.exists():
                    continue
                await self._replace_state_file(lambda path: shutil.copy2(legacy_state, path))
                break
        self._legacy_state_paths = []

    async def get(self, key: str, default: Any = None) -> Any:
        """封装 KV 读取。"""
        kv = (await self._read_state())["kv"]
        if key in kv:
            return deepcopy(kv[key])
        if await self._kv_value(kv, key) is None:
            return default
        async with self._locked_state() as state:
            kv = state["kv"]
            if key in kv:
                return kv[key]
            # A concurrent writer may have deleted the backend value since the probe.
            value = await self._kv_value(kv, key)
            if value is None:
                return default
            await self._commit_kv(state, {key: value}, mirror=False)
            return deepcopy(value)

    async def put(self, key: str, value: Any) -> None:
        """封装 KV 写入。"""
        async with self._locked_state() as state:
            await self._commit_kv(state, {key: deepcopy(value)})

    async def delete(self, key: str) -> None:
        """封装 KV 删除。"""
        async with self._locked_state() as state:
            await self._commit_kv(state, deletes=(key,))

    async def _kv_value(self, kv: dict[str, Any], key: str, default: Any = None) -> Any:
        if key in kv:
            return kv[key]
        value = self._decode_value(await self._read_raw_from_backend(key))
        return default if value is None else deepcopy(value)

    async def _commit_kv(
        self,
        state: dict[str, Any],
        updates: dict[str, Any] | None = None,
        *,
        deletes: Iterable[str] = (),
        mirror: bool = True,
    ) -> None:
        kv = state["kv"]
        updates = updates or {}
        deletes = tuple(deletes)
        changed = bool(updates)
        kv.update(updates)
        for key in deletes:
            if key in kv:
                del kv[key]
                changed = True
        if changed:
            await self._write_disk_state(state)
        if mirror:
            if self._delete_kv_data is not None:
                for key in deletes:
                    await self._delete_kv_data(key)
            if self._put_kv_data is not None:
                for key, value in updates.items():
                    await self._put_kv_data(key, json.dumps(value, ensure_ascii=False))

    async def has_seen(self, item_id: str, ttl_seconds: int | None = None) -> bool:
        kv = (await self._read_state())["kv"]
        raw_version = await self._kv_value(kv, self.DEDUP_VERSION_KEY)
        key, record, _ = await self._find_seen_record(kv, item_id, raw_version)
        if not record:
            return False
        expire_at = int(record.get("expire_at", 0))
        if kv.get(key) and (not expire_at or expire_at >= int(time.time())) and (
            self._effective_expire_at(record, ttl_seconds) <= expire_at
        ):
            return True

        async with self._locked_state() as state:
            kv = state["kv"]
            raw_version = await self._kv_value(kv, self.DEDUP_VERSION_KEY)
            key, record, from_legacy = await self._find_seen_record(kv, item_id, raw_version)
            if not record:
                return False
            updates = {}
            if raw_version is not None and self.DEDUP_VERSION_KEY not in kv:
                updates[self.DEDUP_VERSION_KEY] = raw_version
            now = int(time.time())
            expire_at = int(record.get("expire_at", 0))
            effective_expire_at = self._effective_expire_at(record, ttl_seconds)
            if expire_at and expire_at < now and effective_expire_at < now:
                index = await self._kv_value(kv, self.CONTENT_INDEX_KEY)
                if isinstance(index, list) and item_id in index:
                    updates[self.CONTENT_INDEX_KEY] = [entry for entry in index if entry != item_id]
                await self._commit_kv(state, updates, deletes=(key,))
                return False

            if effective_expire_at > expire_at:
                updates[key] = dict(record, expire_at=effective_expire_at)
                await self._commit_kv(state, updates)
            elif key not in kv:
                updates[key] = record
                await self._commit_kv(state, updates, mirror=from_legacy)
            else:
                await self._commit_kv(state, updates, mirror=False)
            return True

    async def _find_seen_record(
        self, kv: dict[str, Any], item_id: str, raw_version: Any
    ) -> tuple[str, Any, bool]:
        version = self._parse_dedup_version(raw_version)
        key = self._content_key(item_id, version)
        record = await self._kv_value(kv, key)
        if record:
            return key, record, False
        for legacy_key in self._legacy_content_keys(item_id, version):
            record = await self._kv_value(kv, legacy_key)
            if record:
                return key, record, True
        return key, None, False

    async def mark_seen(self, item_id: str, ttl_seconds: int = 86400) -> None:
        await self.mark_seen_many((item_id,), ttl_seconds)

    async def mark_seen_many(self, keys: Iterable[str], ttl_seconds: int = 86400) -> None:
        """在同一份状态中确认全部去重键并更新索引。"""
        item_ids = list(dict.fromkeys(keys))
        if not item_ids:
            return
        async with self._locked_state() as state:
            kv = state["kv"]
            raw_version = await self._kv_value(kv, self.DEDUP_VERSION_KEY, 0)
            version = self._parse_dedup_version(raw_version)
            index = await self._kv_value(kv, self.CONTENT_INDEX_KEY, [])
            if not isinstance(index, list):
                index = []
            indexed = {entry for entry in index if isinstance(entry, str)}
            now = int(time.time())
            updates: dict[str, Any] = {
                self._content_key(item_id, version): {
                    "id": item_id,
                    "expire_at": now + max(ttl_seconds, 0),
                    "updated_at": now,
                }
                for item_id in item_ids
            }
            new_ids = [item_id for item_id in item_ids if item_id not in indexed]
            if new_ids or self.CONTENT_INDEX_KEY not in kv:
                updates[self.CONTENT_INDEX_KEY] = index + new_ids
            if self.DEDUP_VERSION_KEY not in kv:
                updates[self.DEDUP_VERSION_KEY] = raw_version
            await self._commit_kv(state, updates)

    async def clear_seen(self) -> int:
        """清空已推送去重记录，返回删除数量。

        说明：在 KV 不支持按前缀枚举键时，采用去重版本号自增实现“逻辑清空”，
        旧版本键即使存在也不会再命中。
        """
        async with self._locked_state() as state:
            kv = state["kv"]
            version = self._parse_dedup_version(await self._kv_value(kv, self.DEDUP_VERSION_KEY))
            index = await self._kv_value(kv, self.CONTENT_INDEX_KEY, [])
            if not isinstance(index, list):
                index = []
            deletes = {self.CONTENT_INDEX_KEY: None}
            for raw_id in index:
                item_id = str(raw_id)
                deletes[self._content_key(item_id, version)] = None
                deletes.update(dict.fromkeys(self._legacy_content_keys(item_id, version)))
            state.pop(self.SEMANTIC_DEDUP_SECTION, None)
            await self._commit_kv(state, {self.DEDUP_VERSION_KEY: version + 1}, deletes=deletes)
            return len(index)

    async def archive_digest_items(
        self,
        items: list[dict[str, Any]],
        retention_seconds: int | None = None,
    ) -> int:
        if not items:
            return 0

        effective_retention = (
            int(retention_seconds)
            if retention_seconds is not None
            else self.DAILY_DIGEST_RETENTION_SECONDS
        )

        async with self._locked_state() as state:
            now = int(time.time())
            section = self._daily_digest_section(state)
            archive = section.setdefault("archive", {})
            self._prune_digest_archive(archive, now, effective_retention)

            updated = 0
            for item in items:
                record = self._build_digest_archive_record(item, now)
                if not record:
                    continue
                archive[record["archive_key"]] = record
                updated += 1

            await self._write_disk_state(state)
            return updated

    async def list_digest_items(
        self,
        feed_ids: list[str],
        *,
        window_start_ts: int,
        window_end_ts: int,
        limit: int,
        retention_seconds: int | None = None,
    ) -> list[dict[str, Any]]:
        selected_feed_ids = {str(feed_id).strip() for feed_id in feed_ids if str(feed_id).strip()}
        if not selected_feed_ids or limit <= 0:
            return []

        effective_retention = (
            int(retention_seconds)
            if retention_seconds is not None
            else self.DAILY_DIGEST_RETENTION_SECONDS
        )

        archive = self._nested_mapping(await self._read_state(), "daily_digest", "archive")
        cutoff = int(time.time()) - max(effective_retention, 1)
        matched: list[tuple[int, dict[str, Any]]] = []
        for record in archive.values():
            if self._digest_record_expired(record, cutoff):
                continue
            feed_id = str(record.get("feed_id", "")).strip()
            if feed_id not in selected_feed_ids:
                continue
            record_ts = self._record_window_timestamp(record)
            if window_start_ts <= record_ts <= window_end_ts:
                matched.append((record_ts, record))
        matched.sort(key=lambda item: (item[0], int(item[1].get("collected_at", 0) or 0)), reverse=True)
        return [deepcopy(record) for _, record in matched[:limit]]

    async def get_daily_digest_status(self, digest_id: str) -> dict[str, Any]:
        digest_key = str(digest_id or "").strip()
        if not digest_key:
            return {}

        return deepcopy(self._nested_mapping(await self._read_state(), "daily_digest", "status", digest_key))

    async def update_daily_digest_status(self, digest_id: str, **fields: Any) -> dict[str, Any]:
        digest_key = str(digest_id or "").strip()
        if not digest_key:
            return {}

        async with self._locked_state() as state:
            section = self._daily_digest_section(state)
            status = section.setdefault("status", {})
            record = status.get(digest_key)
            if not isinstance(record, dict):
                record = {}
            for key, value in fields.items():
                if value is None:
                    continue
                record[key] = deepcopy(value)
            record["updated_at"] = int(time.time())
            status[digest_key] = record
            await self._write_disk_state(state)
            return deepcopy(record)

    async def put_semantic_dedup_record(
        self,
        job_id: str,
        item: dict[str, Any],
        *,
        seen_keys: list[str],
        ttl_seconds: int,
    ) -> dict[str, Any]:
        job_key = str(job_id or "").strip()
        if not job_key:
            return {}

        ttl = max(int(ttl_seconds), 1)

        async with self._locked_state() as state:
            now = int(time.time())
            job_section = self._semantic_job_section(state, job_key)
            records = job_section.setdefault("records", {})
            self._prune_semantic_records(records, now, ttl)

            clean_seen_keys = [str(key).strip() for key in seen_keys if str(key).strip()]
            record_id = self._semantic_record_id(item, clean_seen_keys)
            if not record_id:
                return {}

            existing = records.get(record_id)
            created_at = int(existing.get("created_at", now)) if isinstance(existing, dict) else now
            record = {
                "record_id": record_id,
                "item_key": str(self.build_dedup_key(item)).strip(),
                "seen_keys": clean_seen_keys,
                "feed_id": str(item.get("feed_id", "")).strip(),
                "source": str(item.get("feed_title", "") or item.get("source", "") or "").strip(),
                "title": str(item.get("title", "")).strip(),
                "summary": str(item.get("summary", "") or item.get("content", "") or "").strip(),
                "link": str(item.get("link", "")).strip(),
                "published_at": str(item.get("published_at", "") or item.get("published", "") or "").strip(),
                "created_at": created_at,
                "updated_at": now,
                "expires_at": now + ttl,
                "duplicate_count": int(existing.get("duplicate_count", 0)) if isinstance(existing, dict) else 0,
            }
            records[record_id] = record
            await self._write_disk_state(state)
            return deepcopy(record)

    async def list_semantic_dedup_records(
        self,
        job_id: str,
        *,
        limit: int,
        ttl_seconds: int,
    ) -> list[dict[str, Any]]:
        job_key = str(job_id or "").strip()
        if not job_key or limit <= 0:
            return []

        ttl = max(int(ttl_seconds), 1)

        records = self._nested_mapping(
            await self._read_state(), self.SEMANTIC_DEDUP_SECTION, "jobs", job_key, "records"
        )
        now = int(time.time())
        matched = [
            record for record in records.values()
            if not self._semantic_record_expired(record, now, now - ttl)
        ]
        matched.sort(
            key=lambda record: (
                int(record.get("updated_at", 0) or 0),
                int(record.get("created_at", 0) or 0),
            ),
            reverse=True,
        )
        return deepcopy(matched[:limit])

    async def touch_semantic_dedup_record(
        self,
        job_id: str,
        record_id: str,
    ) -> dict[str, Any]:
        job_key = str(job_id or "").strip()
        record_key = str(record_id or "").strip()
        if not job_key or not record_key:
            return {}

        async with self._locked_state() as state:
            job_section = self._semantic_job_section(state, job_key)
            records = job_section.setdefault("records", {})
            record = records.get(record_key)
            if not isinstance(record, dict):
                return {}
            record["updated_at"] = int(time.time())
            record["duplicate_count"] = int(record.get("duplicate_count", 0) or 0) + 1
            await self._write_disk_state(state)
            return deepcopy(record)

    async def claim_dispatch(self, fingerprint: str, ttl_seconds: int = 120) -> bool:
        """发送前占位，避免并发实例重复发送同一条消息。"""
        return await self.claim_dispatch_status(fingerprint, ttl_seconds) == "claimed"

    async def claim_dispatch_status(
        self, fingerprint: str, ttl_seconds: int = 120
    ) -> Literal["claimed", "pending", "sent"]:
        """原子领取发送权，或返回仍有效的占位状态。"""
        key = self._dispatch_guard_key(fingerprint)
        if not key:
            return "claimed"
        ttl = max(int(ttl_seconds), 1)
        async with self._locked_state() as state:
            now = int(time.time())
            kv = state["kv"]
            record = kv.get(key)
            if self._is_guard_active(record, now):
                return "sent" if record.get("state") == "sent" else "pending"

            kv[key] = {
                "state": "pending",
                "expire_at": now + ttl,
                "updated_at": now,
            }
            await self._write_disk_state(state)
            return "claimed"

    async def confirm_dispatch(self, fingerprint: str, ttl_seconds: int = 86400) -> None:
        key = self._dispatch_guard_key(fingerprint)
        if not key:
            return
        await self._update_dispatch_guard(
            key,
            action="confirm",
            ttl_seconds=max(int(ttl_seconds), 1),
        )

    async def release_dispatch(self, fingerprint: str) -> None:
        key = self._dispatch_guard_key(fingerprint)
        if not key:
            return
        await self._update_dispatch_guard(key, action="release")

    async def get_feed_state(self, feed_id: str) -> dict[str, Any]:
        return await self.get(self._feed_state_key(feed_id), default={})

    @staticmethod
    def _subscription_scope(job_id: str, origin: str, feed_id: str) -> str:
        payload = json.dumps([job_id, origin, feed_id], ensure_ascii=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    async def initialize_subscription_registry(self, scopes: list[tuple[str, str, str]]) -> None:
        # Legacy state cannot identify newly added subscriptions. Adopt the first
        # configuration once, without consuming the existing jobs' next poll.
        async with self._locked_state() as state:
            kv = state["kv"]
            key = self.SUBSCRIPTION_REGISTRY_KEY
            adopted = await self._kv_value(kv, key)
            if adopted is None:
                await self._commit_kv(state, {key: {
                    self._subscription_scope(*scope): True for scope in scopes
                }})
            elif key not in kv:
                await self._commit_kv(state, {key: adopted}, mirror=False)

    async def is_subscription_initialized(self, job_id: str, origin: str, feed_id: str) -> bool:
        scope = self._subscription_scope(job_id, origin, feed_id)
        adopted = await self.get(self.SUBSCRIPTION_REGISTRY_KEY, default={})
        if isinstance(adopted, dict) and adopted.get(scope) is True:
            return True
        return await self.get(self.SUBSCRIPTION_STATE_PREFIX + scope) is True

    async def mark_subscription_initialized(self, job_id: str, origin: str, feed_id: str) -> None:
        await self.put(self.SUBSCRIPTION_STATE_PREFIX + self._subscription_scope(job_id, origin, feed_id), True)

    async def reset_subscription_initialization(self, job_id: str, origin: str, feed_id: str) -> None:
        await self.delete(self.SUBSCRIPTION_STATE_PREFIX + self._subscription_scope(job_id, origin, feed_id))

    async def update_feed_state(
        self,
        feed_id: str,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
        last_success_time: int | None = None,
        bootstrap_done: bool | None = None,
        since_id: str | None = None,
    ) -> dict[str, Any]:
        key = self._feed_state_key(feed_id)
        async with self._locked_state() as state:
            feed_state = dict(await self._kv_value(state["kv"], key, {}))
            if etag is not None:
                feed_state["etag"] = etag
            if last_modified is not None:
                feed_state["last_modified"] = last_modified
            if last_success_time is not None:
                feed_state["last_success_time"] = last_success_time
            if bootstrap_done is not None:
                feed_state["bootstrap_done"] = bool(bootstrap_done)
            if since_id is not None:
                feed_state["since_id"] = str(since_id or "").strip()
            await self._commit_kv(state, {key: feed_state})
            return deepcopy(feed_state)

    def build_dedup_key(self, item: dict[str, Any]) -> str:
        """优先使用 guid/id，其次使用 link 哈希。"""
        for field in ("guid", "id"):
            value = str(item.get(field, "")).strip()
            if value:
                return value

        link = str(item.get("link", "")).strip()
        if link:
            return hashlib.sha256(link.encode("utf-8")).hexdigest()

        # 兜底，避免空键导致重复推送。
        payload = json.dumps(item, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def build_link_fingerprint(self, item: dict[str, Any]) -> str:
        """基于规范化 link 生成第二层去重键。"""
        link = self._normalize_link(str(item.get("link", "")).strip())
        if not link:
            return ""
        digest = hashlib.sha256(link.encode("utf-8")).hexdigest()
        return f"link:{digest}"

    def build_seen_keys(self, item: dict[str, Any]) -> list[str]:
        """返回需要同时参与去重的键。"""
        keys: list[str] = []
        primary_key = str(self.build_dedup_key(item)).strip()
        if primary_key:
            keys.append(primary_key)

        link_fingerprint = self.build_link_fingerprint(item)
        if link_fingerprint and link_fingerprint not in keys:
            keys.append(link_fingerprint)
        return keys

    def build_digest_archive_key(self, item: dict[str, Any]) -> str:
        link_fingerprint = self.build_link_fingerprint(item)
        if link_fingerprint:
            return link_fingerprint
        seen_keys = self.build_seen_keys(item)
        return seen_keys[0] if seen_keys else ""

    def plugin_cache_dir(self) -> Path:
        """如需大文件缓存，请按规范写入 data/plugin_data/{plugin_name}/。"""
        return self._resolve_plugin_cache_dir(self._plugin_name)

    @staticmethod
    def _resolve_plugin_cache_dir(plugin_name: str) -> Path:
        if StarTools is not None:
            try:
                return Path(StarTools.get_data_dir(plugin_name))
            except Exception:
                pass
        return Path("data") / "plugin_data" / plugin_name

    async def _write_disk_state(self, state: dict[str, Any]) -> None:
        def write(path: Path) -> None:
            with path.open("w", encoding="utf-8") as fp:
                json.dump(state, fp, ensure_ascii=False, sort_keys=True)

        # The locked transaction owns this object until the worker exits.
        self._snapshot_id = await self._replace_state_file(write)
        self._snapshot = state

    async def _replace_state_file(self, write: Callable[[Path], Any]) -> tuple[int, ...] | None:
        worker = asyncio.get_running_loop().run_in_executor(None, self._replace_state_file_sync, write)
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError as cancelled:
            # A cancelled caller must not release state.lock while the worker can
            # still replace the file, even if cancellation is requested again.
            while not worker.done():
                try:
                    await asyncio.shield(worker)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            try:
                worker.result()
            except Exception as error:
                raise cancelled from error
            raise

    def _replace_state_file_sync(self, write: Callable[[Path], Any]) -> tuple[int, ...] | None:
        fd, name = tempfile.mkstemp(prefix=".state-", suffix=".tmp", dir=self._state_path.parent)
        os.close(fd)
        temporary = Path(name)
        try:
            write(temporary)
            with temporary.open("rb") as fp:
                os.fsync(fp.fileno())
            os.replace(temporary, self._state_path)
            try:
                return self._file_identity(self._state_path.stat())
            except OSError:
                return None
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _nested_mapping(state: dict[str, Any], *keys: str) -> dict[str, Any]:
        value: Any = state
        for key in keys:
            if not isinstance(value, dict):
                return {}
            value = value.get(key)
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _daily_digest_section(state: dict[str, Any]) -> dict[str, Any]:
        section = state.setdefault("daily_digest", {})
        if not isinstance(section, dict):
            section = {}
            state["daily_digest"] = section
        section.setdefault("archive", {})
        section.setdefault("status", {})
        return section

    @classmethod
    def _semantic_dedup_section(cls, state: dict[str, Any]) -> dict[str, Any]:
        section = state.setdefault(cls.SEMANTIC_DEDUP_SECTION, {})
        if not isinstance(section, dict):
            section = {}
            state[cls.SEMANTIC_DEDUP_SECTION] = section
        section.setdefault("jobs", {})
        return section

    @classmethod
    def _semantic_job_section(cls, state: dict[str, Any], job_id: str) -> dict[str, Any]:
        section = cls._semantic_dedup_section(state)
        jobs = section.setdefault("jobs", {})
        if not isinstance(jobs, dict):
            jobs = {}
            section["jobs"] = jobs
        job_section = jobs.get(job_id)
        if not isinstance(job_section, dict):
            job_section = {}
            jobs[job_id] = job_section
        job_section.setdefault("records", {})
        return job_section

    @classmethod
    def _prune_semantic_records(cls, records: dict[str, Any], now: int, ttl_seconds: int) -> None:
        cutoff = now - max(int(ttl_seconds), 1)
        expired_keys = [
            key for key, record in records.items()
            if cls._semantic_record_expired(record, now, cutoff)
        ]
        for key in expired_keys:
            del records[key]

    @staticmethod
    def _semantic_record_expired(record: Any, now: int, cutoff: int) -> bool:
        if not isinstance(record, dict):
            return True
        expire_at = int(record.get("expires_at", 0) or 0)
        created_at = int(record.get("created_at", 0) or 0)
        return expire_at < now if expire_at else bool(created_at and created_at < cutoff)

    def _semantic_record_id(self, item: dict[str, Any], seen_keys: list[str]) -> str:
        seed = next((str(key).strip() for key in seen_keys if str(key).strip()), "")
        if not seed:
            seed = str(self.build_dedup_key(item)).strip()
        if not seed:
            return ""
        digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
        return f"semantic:{digest}"

    def _build_digest_archive_record(self, item: dict[str, Any], now: int) -> dict[str, Any] | None:
        archive_key = self.build_digest_archive_key(item)
        if not archive_key:
            return None
        seen_keys = self.build_seen_keys(item)
        return {
            "archive_key": archive_key,
            "item_key": str(self.build_dedup_key(item)).strip(),
            "seen_keys": seen_keys,
            "feed_id": str(item.get("feed_id", "")).strip(),
            "feed_title": str(item.get("feed_title", "") or item.get("source", "")).strip(),
            "title": str(item.get("title", "")).strip(),
            "summary": str(item.get("summary", "") or item.get("content", "") or "").strip(),
            "link": str(item.get("link", "")).strip(),
            "image_url": str(item.get("image_url", "")).strip(),
            "image_urls": [
                str(url).strip()
                for url in item.get("image_urls", [])
                if str(url).strip()
            ]
            if isinstance(item.get("image_urls", []), list)
            else [],
            "video_urls": [
                str(url).strip()
                for url in item.get("video_urls", [])
                if str(url).strip()
            ]
            if isinstance(item.get("video_urls", []), list)
            else [],
            "published_at": str(item.get("published_at", "")).strip(),
            "collected_at": now,
        }

    def _prune_digest_archive(
        self,
        archive: dict[str, Any],
        now: int,
        retention_seconds: int,
    ) -> None:
        cutoff = now - max(int(retention_seconds), 1)
        expired_keys = [
            key
            for key, record in archive.items()
            if self._digest_record_expired(record, cutoff)
        ]
        for key in expired_keys:
            archive.pop(key, None)

    @staticmethod
    def _digest_record_expired(record: Any, cutoff: int) -> bool:
        return not isinstance(record, dict) or int(record.get("collected_at", 0) or 0) < cutoff

    @classmethod
    def _record_window_timestamp(cls, record: dict[str, Any]) -> int:
        published_ts = cls._parse_iso_timestamp(str(record.get("published_at", "")).strip())
        if published_ts is not None:
            return published_ts
        return int(record.get("collected_at", 0) or 0)

    @staticmethod
    def _parse_iso_timestamp(raw_value: str) -> int | None:
        text = str(raw_value or "").strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.astimezone(timezone.utc).timestamp())

    async def _read_raw_from_backend(self, key: str) -> Any:
        if self._get_kv_data is None:
            return None
        try:
            # AstrBot PluginKVStoreMixin.get_kv_data(key, default)
            return await self._get_kv_data(key, None)
        except TypeError:
            # 兼容仅接收 key 的实现
            return await self._get_kv_data(key)

    def _decode_value(self, raw: Any) -> Any:
        if raw in (None, ""):
            return None
        if isinstance(raw, dict) and set(raw.keys()) == {"val"}:
            return self._decode_value(raw.get("val"))
        if isinstance(raw, str):
            try:
                decoded = json.loads(raw)
            except json.JSONDecodeError:
                return raw
            return self._decode_value(decoded)
        return raw

    def _legacy_content_keys(self, item_id: str, version: int) -> list[str]:
        legacy_keys = [f"{self.CONTENT_KEY_PREFIX}{item_id}"]
        if version != 0:
            legacy_keys.append(self._content_key(item_id, 0))
        return legacy_keys

    @classmethod
    def _feed_state_key(cls, feed_id: str) -> str:
        return f"{cls.FEED_STATE_PREFIX}{feed_id}"

    def _content_key(self, item_id: str, version: int | None = None) -> str:
        if version is None:
            kv = self._load_disk_state_from_file(snapshot=True)["kv"]
            version = self._parse_dedup_version(kv.get(self.DEDUP_VERSION_KEY))
        return f"{self.CONTENT_KEY_PREFIX}v{version}:{item_id}"

    @staticmethod
    def _parse_dedup_version(value: Any) -> int:
        try:
            return int(value)
        except (TypeError, ValueError, OverflowError):
            return 0

    @staticmethod
    def _effective_expire_at(record: dict[str, Any], ttl_seconds: int | None) -> int:
        if ttl_seconds is None or ttl_seconds <= 0:
            return 0
        updated_at = int(record.get("updated_at", 0) or 0)
        if updated_at <= 0:
            return 0
        return updated_at + int(ttl_seconds)

    def _dispatch_guard_key(self, fingerprint: str) -> str:
        value = str(fingerprint or "").strip()
        if not value:
            return ""
        return f"{self.DISPATCH_GUARD_PREFIX}{value}"

    @staticmethod
    def _normalize_link(link: str) -> str:
        if not link:
            return ""
        parsed = urlsplit(link)
        if not any((parsed.scheme, parsed.netloc, parsed.path, parsed.query, parsed.fragment)):
            return link
        return urlunsplit(
            (
                parsed.scheme.lower(),
                parsed.netloc.lower(),
                parsed.path,
                parsed.query,
                "",
            )
        )

    @staticmethod
    def _is_guard_active(record: Any, now: int) -> bool:
        if not isinstance(record, dict):
            return False
        expire_at = int(record.get("expire_at", 0) or 0)
        return expire_at <= 0 or expire_at >= now

    @staticmethod
    def _file_identity(stat: os.stat_result) -> tuple[int, ...]:
        return stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size

    def _load_disk_state_from_file(self, *, snapshot: bool = False) -> dict[str, Any]:
        try:
            if snapshot and self._file_identity(self._state_path.stat()) == self._snapshot_id:
                return self._snapshot
            with self._state_path.open("r", encoding="utf-8") as fp:
                loaded = json.load(fp)
                identity = self._file_identity(os.fstat(fp.fileno()))
        except FileNotFoundError:
            return {"kv": {}}
        if not isinstance(loaded, dict) or not isinstance(loaded.setdefault("kv", {}), dict):
            raise ValueError(f"Invalid state structure: {self._state_path}")
        if snapshot:
            self._snapshot_id = identity
            self._snapshot = loaded
        return loaded

    async def _read_state(self) -> dict[str, Any]:
        if self._legacy_state_paths:
            async with self._state_lock():
                await self._migrate_legacy_state()
        return self._load_disk_state_from_file(snapshot=True)

    @asynccontextmanager
    async def _state_lock(self):
        lock_path = self._state_path.parent / "state.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+", encoding="utf-8") as lock_fp:
            # KV callbacks can await while holding this lock. Wait without blocking
            # the event loop, including when another instance lives on that loop.
            while True:
                if fcntl is None:
                    if self._fallback_lock.acquire(blocking=False):
                        break
                else:
                    try:
                        fcntl.flock(lock_fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        pass
                await asyncio.sleep(0.01)
            try:
                yield
            finally:
                if fcntl is None:
                    self._fallback_lock.release()
                else:
                    fcntl.flock(lock_fp.fileno(), fcntl.LOCK_UN)

    @asynccontextmanager
    async def _locked_state(self):
        async with self._state_lock():
            await self._migrate_legacy_state()
            yield self._load_disk_state_from_file()

    async def _update_dispatch_guard(
        self,
        key: str,
        *,
        action: str,
        ttl_seconds: int = 0,
    ) -> None:
        async with self._locked_state() as state:
            now = int(time.time())
            kv = state["kv"]
            record = kv.get(key)

            if action == "confirm":
                kv[key] = {
                    "state": "sent",
                    "expire_at": now + max(ttl_seconds, 1),
                    "updated_at": now,
                }
                await self._write_disk_state(state)
                return None

            if action == "release":
                if isinstance(record, dict) and record.get("state") == "pending":
                    kv.pop(key, None)
                    await self._write_disk_state(state)
                return None

            raise ValueError(f"unknown dispatch guard action: {action}")
