import hashlib
import json
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable
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
    DISPATCH_GUARD_PREFIX = "dispatch_guard:"
    SEMANTIC_DEDUP_SECTION = "semantic_dedup"
    DAILY_DIGEST_RETENTION_SECONDS = 30 * 24 * 60 * 60

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
        self._fallback_store: dict[str, str] = {}
        self._seen_ids: set[str] = set()
        self._dedup_version: int | None = None
        cache_root = Path(storage_dir) if storage_dir is not None else self.plugin_cache_dir()
        self._state_path = cache_root / "state.json"
        self._migrate_legacy_state(
            legacy_plugin_names=legacy_plugin_names or [],
            legacy_storage_dirs=[Path(path) for path in legacy_storage_dirs or []],
            explicit_storage_dir=storage_dir is not None,
        )
        self._state_loaded = False
        self._disk_state: dict[str, Any] = {"kv": {}}

    def _migrate_legacy_state(
        self,
        *,
        legacy_plugin_names: list[str],
        legacy_storage_dirs: list[Path],
        explicit_storage_dir: bool,
    ) -> None:
        if self._state_path.exists():
            return

        candidates = list(legacy_storage_dirs)
        if not explicit_storage_dir:
            for legacy_name in legacy_plugin_names:
                legacy_name = str(legacy_name or "").strip()
                if not legacy_name or legacy_name == self._plugin_name:
                    continue
                candidates.append(self._resolve_plugin_cache_dir(legacy_name))

        for legacy_dir in candidates:
            legacy_state = Path(legacy_dir) / "state.json"
            if not legacy_state.exists():
                continue
            try:
                self._state_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(legacy_state, self._state_path)
                return
            except OSError:
                continue

    async def get(self, key: str, default: Any = None) -> Any:
        """封装 KV 读取。"""
        await self._ensure_state_loaded()
        kv_store = self._disk_state.setdefault("kv", {})
        if key in kv_store:
            return kv_store[key]

        raw = await self._read_raw_from_backend(key)
        decoded = self._decode_value(raw)
        if decoded is None:
            return default
        kv_store[key] = decoded
        self._flush_state()
        return decoded

    async def put(self, key: str, value: Any) -> None:
        """封装 KV 写入。"""
        await self._ensure_state_loaded()
        self._disk_state.setdefault("kv", {})[key] = value
        self._flush_state()

        encoded = json.dumps(value, ensure_ascii=False)
        if self._put_kv_data is None:
            self._fallback_store[key] = encoded
            return
        await self._put_kv_data(key, encoded)

    async def delete(self, key: str) -> None:
        """封装 KV 删除。"""
        await self._ensure_state_loaded()
        self._disk_state.setdefault("kv", {}).pop(key, None)
        self._flush_state()

        if self._delete_kv_data is None:
            self._fallback_store.pop(key, None)
            return
        await self._delete_kv_data(key)

    async def has_seen(self, item_id: str, ttl_seconds: int | None = None) -> bool:
        await self._get_dedup_version()

        # NOTE:
        # _seen_ids is only an in-memory acceleration set and does not carry TTL.
        # We still need to validate persisted record expiration to avoid permanent
        # false positives after long-running processes.
        cached = item_id in self._seen_ids
        record = await self.get(self._content_key(item_id), default=None)
        if not record:
            record = await self._read_legacy_content_record(item_id)
        if not record:
            if cached:
                self._seen_ids.discard(item_id)
            return False

        now = int(time.time())
        expire_at = int(record.get("expire_at", 0))
        effective_expire_at = self._effective_expire_at(record, ttl_seconds)
        if expire_at and expire_at < now:
            if effective_expire_at and effective_expire_at >= now:
                record["expire_at"] = effective_expire_at
                await self.put(self._content_key(item_id), record)
                self._seen_ids.add(item_id)
                return True
            await self.delete(self._content_key(item_id))
            self._seen_ids.discard(item_id)
            return False

        if effective_expire_at and effective_expire_at > expire_at:
            record["expire_at"] = effective_expire_at
            await self.put(self._content_key(item_id), record)

        self._seen_ids.add(item_id)
        return True

    async def mark_seen(self, item_id: str, ttl_seconds: int = 86400) -> None:
        await self._get_dedup_version()
        self._seen_ids.add(item_id)
        expire_at = int(time.time()) + max(ttl_seconds, 0)
        await self.put(
            self._content_key(item_id),
            {
                "id": item_id,
                "expire_at": expire_at,
                "updated_at": int(time.time()),
            },
        )
        seen_index = await self.get(self.CONTENT_INDEX_KEY, default=[])
        if not isinstance(seen_index, list):
            seen_index = []
        if item_id not in seen_index:
            seen_index.append(item_id)
            await self.put(self.CONTENT_INDEX_KEY, seen_index)

    async def clear_seen(self) -> int:
        """清空已推送去重记录，返回删除数量。

        说明：在 KV 不支持按前缀枚举键时，采用去重版本号自增实现“逻辑清空”，
        旧版本键即使存在也不会再命中。
        """
        await self._get_dedup_version()
        seen_index = await self.get(self.CONTENT_INDEX_KEY, default=[])
        if not isinstance(seen_index, list):
            seen_index = []

        deleted = 0
        for item_id in seen_index:
            await self.delete(self._content_key(str(item_id)))
            deleted += 1

        await self.delete(self.CONTENT_INDEX_KEY)
        self._seen_ids.clear()
        version = await self._get_dedup_version()
        self._dedup_version = version + 1
        await self.put(self.DEDUP_VERSION_KEY, self._dedup_version)
        self._clear_semantic_dedup_state()
        return deleted

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

        def callback(state: dict[str, Any], now: int):
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

            self._write_disk_state(state)
            return updated

        return int(self._with_state_lock(callback))

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

        def callback(state: dict[str, Any], now: int):
            section = self._daily_digest_section(state)
            archive = section.setdefault("archive", {})
            self._prune_digest_archive(archive, now, effective_retention)

            matched: list[dict[str, Any]] = []
            for record in archive.values():
                if not isinstance(record, dict):
                    continue
                feed_id = str(record.get("feed_id", "")).strip()
                if feed_id not in selected_feed_ids:
                    continue
                record_ts = self._record_window_timestamp(record)
                if record_ts < window_start_ts or record_ts > window_end_ts:
                    continue
                matched.append(dict(record))

            matched.sort(
                key=lambda item: (
                    int(self._record_window_timestamp(item)),
                    int(item.get("collected_at", 0) or 0),
                ),
                reverse=True,
            )
            self._write_disk_state(state)
            return matched[:limit]

        return list(self._with_state_lock(callback) or [])

    async def get_daily_digest_status(self, digest_id: str) -> dict[str, Any]:
        digest_key = str(digest_id or "").strip()
        if not digest_key:
            return {}

        def callback(state: dict[str, Any], now: int):
            section = self._daily_digest_section(state)
            status = section.setdefault("status", {})
            record = status.get(digest_key)
            return dict(record) if isinstance(record, dict) else {}

        return dict(self._with_state_lock(callback) or {})

    async def update_daily_digest_status(self, digest_id: str, **fields: Any) -> dict[str, Any]:
        digest_key = str(digest_id or "").strip()
        if not digest_key:
            return {}

        def callback(state: dict[str, Any], now: int):
            section = self._daily_digest_section(state)
            status = section.setdefault("status", {})
            record = status.get(digest_key)
            if not isinstance(record, dict):
                record = {}
            for key, value in fields.items():
                if value is None:
                    continue
                record[key] = value
            record["updated_at"] = now
            status[digest_key] = record
            self._write_disk_state(state)
            return dict(record)

        return dict(self._with_state_lock(callback) or {})

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

        def callback(state: dict[str, Any], now: int):
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
            self._write_disk_state(state)
            return dict(record)

        return dict(self._with_state_lock(callback) or {})

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

        def callback(state: dict[str, Any], now: int):
            job_section = self._semantic_job_section(state, job_key)
            records = job_section.setdefault("records", {})
            self._prune_semantic_records(records, now, ttl)
            matched = [dict(record) for record in records.values() if isinstance(record, dict)]
            matched.sort(
                key=lambda record: (
                    int(record.get("updated_at", 0) or 0),
                    int(record.get("created_at", 0) or 0),
                ),
                reverse=True,
            )
            self._write_disk_state(state)
            return matched[:limit]

        return list(self._with_state_lock(callback) or [])

    async def touch_semantic_dedup_record(
        self,
        job_id: str,
        record_id: str,
    ) -> dict[str, Any]:
        job_key = str(job_id or "").strip()
        record_key = str(record_id or "").strip()
        if not job_key or not record_key:
            return {}

        def callback(state: dict[str, Any], now: int):
            job_section = self._semantic_job_section(state, job_key)
            records = job_section.setdefault("records", {})
            record = records.get(record_key)
            if not isinstance(record, dict):
                return {}
            record["updated_at"] = now
            record["duplicate_count"] = int(record.get("duplicate_count", 0) or 0) + 1
            self._write_disk_state(state)
            return dict(record)

        return dict(self._with_state_lock(callback) or {})

    async def claim_dispatch(self, fingerprint: str, ttl_seconds: int = 120) -> bool:
        """发送前占位，避免并发实例重复发送同一条消息。"""
        key = self._dispatch_guard_key(fingerprint)
        if not key:
            return True
        return bool(
            self._update_dispatch_guard(
                key,
                action="claim",
                ttl_seconds=max(int(ttl_seconds), 1),
            )
        )

    async def confirm_dispatch(self, fingerprint: str, ttl_seconds: int = 86400) -> None:
        key = self._dispatch_guard_key(fingerprint)
        if not key:
            return
        self._update_dispatch_guard(
            key,
            action="confirm",
            ttl_seconds=max(int(ttl_seconds), 1),
        )

    async def release_dispatch(self, fingerprint: str) -> None:
        key = self._dispatch_guard_key(fingerprint)
        if not key:
            return
        self._update_dispatch_guard(key, action="release")

    async def _get_dedup_version(self) -> int:
        if self._dedup_version is not None:
            return self._dedup_version
        raw = await self.get(self.DEDUP_VERSION_KEY, default=0)
        try:
            self._dedup_version = int(raw)
        except Exception:
            self._dedup_version = 0
        return self._dedup_version

    async def get_feed_state(self, feed_id: str) -> dict[str, Any]:
        return await self.get(self._feed_state_key(feed_id), default={})

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
        state = await self.get_feed_state(feed_id)
        if etag is not None:
            state["etag"] = etag
        if last_modified is not None:
            state["last_modified"] = last_modified
        if last_success_time is not None:
            state["last_success_time"] = last_success_time
        if bootstrap_done is not None:
            state["bootstrap_done"] = bool(bootstrap_done)
        if since_id is not None:
            state["since_id"] = str(since_id or "").strip()
        await self.put(self._feed_state_key(feed_id), state)
        return state

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

    async def _ensure_state_loaded(self) -> None:
        if self._state_loaded:
            return
        self._state_loaded = True
        try:
            if self._state_path.exists():
                with self._state_path.open("r", encoding="utf-8") as fp:
                    loaded = json.load(fp)
                if isinstance(loaded, dict):
                    self._disk_state = loaded
        except (OSError, json.JSONDecodeError):
            self._disk_state = {"kv": {}}
        self._disk_state.setdefault("kv", {})

    def _flush_state(self) -> None:
        self._write_disk_state(self._disk_state)

    def _write_disk_state(self, state: dict[str, Any]) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        with self._state_path.open("w", encoding="utf-8") as fp:
            json.dump(state, fp, ensure_ascii=False, sort_keys=True)

    @staticmethod
    def _daily_digest_section(state: dict[str, Any]) -> dict[str, Any]:
        section = state.setdefault("daily_digest", {})
        if not isinstance(section, dict):
            section = {}
            state["daily_digest"] = section
        section.setdefault("archive", {})
        section.setdefault("status", {})
        return section

    def _clear_semantic_dedup_state(self) -> None:
        def callback(state: dict[str, Any], now: int):
            state.pop(self.SEMANTIC_DEDUP_SECTION, None)
            self._write_disk_state(state)
            return None

        self._with_state_lock(callback)

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

    @staticmethod
    def _prune_semantic_records(records: dict[str, Any], now: int, ttl_seconds: int) -> None:
        cutoff = now - max(int(ttl_seconds), 1)
        expired_keys = []
        for key, record in records.items():
            if not isinstance(record, dict):
                expired_keys.append(key)
                continue
            expire_at = int(record.get("expires_at", 0) or 0)
            created_at = int(record.get("created_at", 0) or 0)
            if expire_at and expire_at < now:
                expired_keys.append(key)
            elif not expire_at and created_at and created_at < cutoff:
                expired_keys.append(key)
        for key in expired_keys:
            records.pop(key, None)

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
            if not isinstance(record, dict) or int(record.get("collected_at", 0) or 0) < cutoff
        ]
        for key in expired_keys:
            archive.pop(key, None)

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
            return self._fallback_store.get(key)
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

    async def _read_legacy_content_record(self, item_id: str) -> dict[str, Any] | None:
        legacy_keys = [f"{self.CONTENT_KEY_PREFIX}{item_id}"]
        if self._dedup_version not in (None, 0):
            legacy_keys.append(f"{self.CONTENT_KEY_PREFIX}v0:{item_id}")

        for legacy_key in legacy_keys:
            record = await self.get(legacy_key, default=None)
            if record:
                await self.put(self._content_key(item_id), record)
                return record
        return None

    @classmethod
    def _feed_state_key(cls, feed_id: str) -> str:
        return f"{cls.FEED_STATE_PREFIX}{feed_id}"

    def _content_key(self, item_id: str) -> str:
        # 仅依赖内存缓存；首次使用前由 _get_dedup_version 初始化。
        version = self._dedup_version if self._dedup_version is not None else 0
        return f"{self.CONTENT_KEY_PREFIX}v{version}:{item_id}"

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

    def _load_disk_state_from_file(self) -> dict[str, Any]:
        try:
            if self._state_path.exists():
                with self._state_path.open("r", encoding="utf-8") as fp:
                    loaded = json.load(fp)
                if isinstance(loaded, dict):
                    loaded.setdefault("kv", {})
                    return loaded
        except (OSError, json.JSONDecodeError):
            pass
        return {"kv": {}}

    def _with_state_lock(self, callback: Callable[[dict[str, Any], int], Any]) -> Any:
        lock_path = self._state_path.parent / "state.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+", encoding="utf-8") as lock_fp:
            if fcntl is not None:
                fcntl.flock(lock_fp.fileno(), fcntl.LOCK_EX)
            try:
                state = self._load_disk_state_from_file()
                now = int(time.time())
                result = callback(state, now)
                self._disk_state = state
                self._state_loaded = True
                return result
            finally:
                if fcntl is not None:
                    fcntl.flock(lock_fp.fileno(), fcntl.LOCK_UN)

    def _update_dispatch_guard(
        self,
        key: str,
        *,
        action: str,
        ttl_seconds: int = 0,
    ) -> bool | None:
        def callback(state: dict[str, Any], now: int):
            kv = state.setdefault("kv", {})
            record = kv.get(key)

            if action == "claim":
                if self._is_guard_active(record, now):
                    return False
                kv[key] = {
                    "state": "pending",
                    "expire_at": now + max(ttl_seconds, 1),
                    "updated_at": now,
                }
                self._write_disk_state(state)
                return True

            if action == "confirm":
                kv[key] = {
                    "state": "sent",
                    "expire_at": now + max(ttl_seconds, 1),
                    "updated_at": now,
                }
                self._write_disk_state(state)
                return None

            if action == "release":
                if isinstance(record, dict) and record.get("state") == "pending":
                    kv.pop(key, None)
                    self._write_disk_state(state)
                return None

            raise ValueError(f"unknown dispatch guard action: {action}")

        return self._with_state_lock(callback)
