import asyncio
import hashlib
import inspect
import time
from contextlib import suppress
from copy import copy, deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from astrbot.api import logger

from .config import DailyDigestConfig, JobConfig, RSSConfig
from .delivery_outbox import DeliveryCandidate, DeliveryEntry, DeliveryOutbox
from .dispatcher import FeedDispatcher
from .fetcher import FeedFetcher
from .parser import FeedParser
from .pipeline import FeedPipeline
from .semantic_dedup import SemanticDedupResult, SemanticDedupService
from .storage import FeedStorage
from .subscription_runtime import _bootstrap_fetcher, _validate_raw


@dataclass(slots=True)
class JobExecutionResult:
    """任务执行结果快照。"""

    started_at: datetime
    duration_ms: int
    fetched_count: int
    pushed_count: int
    error_summary: str = ""
    failed_feed_ids: list[str] = field(default_factory=list)
    pending_item_count: int = 0
    pending_target_count: int = 0
    last_delivery_at: datetime | None = None


@dataclass(slots=True)
class DigestExecutionResult:
    """日报执行结果快照。"""

    started_at: datetime
    duration_ms: int
    item_count: int
    pushed_count: int
    error_summary: str = ""


class _JobFeedStorage:
    def __init__(self, storage: FeedStorage, states: dict[str, dict]) -> None:
        self._storage = storage
        self._states = states

    async def get_feed_state(self, feed_id: str) -> dict:
        if feed_id in self._states:
            return dict(self._states[feed_id])
        return await self._storage.get_feed_state(feed_id)

    def __getattr__(self, name: str):
        return getattr(self._storage, name)


class RSSScheduler:
    """调度层：按 job 独立周期执行 fetch -> parse -> dedup -> dispatch。"""

    def __init__(
        self,
        config: RSSConfig,
        fetcher: FeedFetcher,
        parser: FeedParser,
        dispatcher: FeedDispatcher,
        storage: FeedStorage,
        pipeline: FeedPipeline | None = None,
        semantic_deduper: SemanticDedupService | None = None,
    ) -> None:
        self._config = config
        self._fetcher = fetcher
        self._parser = parser
        self._dispatcher = dispatcher
        self._storage = storage
        state_path = getattr(storage, "_state_path", None) or getattr(storage, "state_path", None)
        cache_dir = getattr(storage, "plugin_cache_dir", None)
        if isinstance(state_path, (str, Path)):
            outbox_dir = Path(state_path).parent
        else:
            outbox_dir = cache_dir() if callable(cache_dir) else cache_dir
        self._outbox = DeliveryOutbox(
            Path(outbox_dir) / "delivery_outbox.sqlite3" if isinstance(outbox_dir, (str, Path)) else None,
        )
        self._cursor_seed_lock = asyncio.Lock()
        self._seeded_cursor_scopes: set[tuple[str, str]] = set()
        self._outbox_scopes: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {}
        self._pipeline = pipeline
        self._semantic_deduper = semantic_deduper
        self._job_tasks: dict[str, asyncio.Task] = {}
        self._job_locks: dict[str, asyncio.Lock] = {}
        self._job_results: dict[str, JobExecutionResult] = {}
        self._paused_jobs: set[str] = set()
        self._digest_feed_tasks: dict[str, asyncio.Task] = {}
        self._digest_feed_locks: dict[str, asyncio.Lock] = {}
        self._digest_send_tasks: dict[str, asyncio.Task] = {}
        self._digest_send_locks: dict[str, asyncio.Lock] = {}
        self._digest_results: dict[str, DigestExecutionResult] = {}

    @property
    def running(self) -> bool:
        all_tasks = [
            *self._job_tasks.values(),
            *self._digest_feed_tasks.values(),
            *self._digest_send_tasks.values(),
        ]
        return any(not task.done() for task in all_tasks)

    @property
    def last_results(self) -> dict[str, JobExecutionResult]:
        return dict(self._job_results)

    @property
    def digest_results(self) -> dict[str, DigestExecutionResult]:
        return dict(self._digest_results)

    @property
    def config(self) -> RSSConfig:
        return self._config

    @property
    def storage(self) -> FeedStorage:
        return self._storage

    async def start(self) -> None:
        if self.running:
            return

        await self._cancel_stale_job_tasks()
        await self._ensure_subscription_registry()
        jobs = self._jobs()
        self._outbox.prune_removed_jobs(tuple(job.id for job in jobs))
        for job in jobs:
            self._prune_job_outbox(job)
            if not job.enabled:
                continue
            self._register_job(job)
        for feed_id in self._digest_only_feed_ids():
            self._register_digest_feed(feed_id)
        for digest in self._daily_digests():
            if not digest.enabled:
                continue
            self._register_daily_digest(digest)

        logger.info(
            "RSS scheduler started, registered jobs=%s digest_feeds=%s daily_digests=%s",
            list(self._job_tasks.keys()),
            list(self._digest_feed_tasks.keys()),
            list(self._digest_send_tasks.keys()),
        )

    async def stop(self) -> None:
        all_tasks = [
            *self._job_tasks.values(),
            *self._digest_feed_tasks.values(),
            *self._digest_send_tasks.values(),
        ]
        if not all_tasks:
            return

        for task in all_tasks:
            task.cancel()

        for task in all_tasks:
            with suppress(asyncio.CancelledError):
                await task

        self._job_tasks.clear()
        self._job_locks.clear()
        self._digest_feed_tasks.clear()
        self._digest_feed_locks.clear()
        self._digest_send_tasks.clear()
        self._digest_send_locks.clear()
        logger.info("RSS scheduler stopped")

    async def _cancel_stale_job_tasks(self) -> None:
        current_task = asyncio.current_task()
        stale_tasks: list[asyncio.Task] = []
        for task in asyncio.all_tasks():
            if task is current_task or task.done():
                continue
            task_name = task.get_name()
            if not task_name.startswith(
                (
                    "rss-job-",
                    "rss-digest-feed-",
                    "rss-digest-send-",
                )
            ):
                continue
            stale_tasks.append(task)

        if not stale_tasks:
            return

        for task in stale_tasks:
            task.cancel()

        for task in stale_tasks:
            with suppress(asyncio.CancelledError):
                await task

        logger.warning(
            "cancelled stale rss scheduler tasks=%s",
            [task.get_name() for task in stale_tasks],
        )

    async def run_once(self) -> None:
        """手动触发：并发执行所有启用 job。"""
        await self.run_job_once()

    async def run_job_once(self, job_id: str | None = None) -> bool:
        """手动触发任务，job_id 为空时触发所有启用且未暂停任务。"""
        if job_id:
            job = self.get_job(job_id)
            if job is None or not job.enabled or job.id in self._paused_jobs:
                return False
            await self._run_job_once_guarded(job)
            return True

        await asyncio.gather(
            *(
                self._run_job_once_guarded(job)
                for job in self._jobs()
                if job.enabled and job.id not in self._paused_jobs
            )
        )
        return True

    async def run_daily_digest_once(self, digest_id: str) -> bool:
        digest = self.get_daily_digest(digest_id)
        if digest is None or not digest.enabled:
            return False
        await self._run_daily_digest_once_guarded(digest, scheduled_date=None)
        return True

    def get_job(self, job_id: str) -> JobConfig | None:
        return next((job for job in self._jobs() if job.id == job_id), None)

    def get_daily_digest(self, digest_id: str) -> DailyDigestConfig | None:
        return next(
            (
                digest
                for digest in self._daily_digests()
                if digest.id == digest_id
            ),
            None,
        )

    async def pause_job(self, job_id: str) -> bool:
        """暂停指定任务。"""
        job = self.get_job(job_id)
        if job is None or not job.enabled:
            return False

        self._paused_jobs.add(job_id)
        task = self._job_tasks.get(job_id)
        if task and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        self._job_tasks.pop(job_id, None)
        return True

    def resume_job(self, job_id: str) -> bool:
        """恢复指定任务。"""
        job = self.get_job(job_id)
        if job is None or not job.enabled:
            return False

        self._paused_jobs.discard(job_id)
        self._register_job(job)
        return True

    @property
    def paused_jobs(self) -> set[str]:
        return set(self._paused_jobs)

    async def test_translation(self, sample_text: str = "") -> dict:
        """执行翻译链路测试，不触发分发与去重。"""
        if self._pipeline is None:
            return {"error": "pipeline_not_configured"}

        diagnose_func = getattr(self._pipeline, "diagnose_translation", None)
        if not callable(diagnose_func):
            return {"error": "pipeline_diagnose_not_supported"}

        sample_entry = {
            "title": "RSS Translation Test",
            "summary": sample_text or "This is a translation diagnostics message from RSS forwarder.",
            "content": sample_text or "",
        }
        report = await diagnose_func(sample_entry)
        report["config"] = {
            "llm_enabled": bool(getattr(self._config, "llm_enabled", False)),
            "llm_timeout_seconds": int(getattr(self._config, "llm_timeout_seconds", 0) or 0),
            "llm_proxy_mode": str(getattr(self._config, "llm_proxy_mode", "system") or "system"),
            "google_translate_enabled": bool(
                getattr(self._config, "google_translate_enabled", False)
            ),
            "google_translate_target_lang": str(
                getattr(self._config, "google_translate_target_lang", "zh-CN") or "zh-CN"
            ),
            "google_translate_timeout_seconds": int(
                getattr(self._config, "google_translate_timeout_seconds", 0) or 0
            ),
            "google_translate_proxy_mode": str(
                getattr(self._config, "google_translate_proxy_mode", "system") or "system"
            ),
            "github_models_enabled": bool(
                getattr(self._config, "github_models_enabled", False)
            ),
            "github_models_model": str(
                getattr(self._config, "github_models_model", "openai/gpt-4o-mini")
                or "openai/gpt-4o-mini"
            ),
            "github_models_timeout_seconds": int(
                getattr(self._config, "github_models_timeout_seconds", 0) or 0
            ),
            "github_models_proxy_mode": str(
                getattr(self._config, "github_models_proxy_mode", "system") or "system"
            ),
        }
        return report

    def _digest_only_feed_ids(self) -> list[str]:
        realtime_feed_ids = {
            feed_id
            for job in self._jobs()
            if job.enabled
            for feed_id in job.feed_ids
        }
        digest_feed_ids = {
            feed_id
            for digest in self._daily_digests()
            if digest.enabled
            for feed_id in digest.feed_ids
        }
        return sorted(digest_feed_ids - realtime_feed_ids)

    def _register_digest_feed(self, feed_id: str) -> None:
        if feed_id in self._digest_feed_tasks and not self._digest_feed_tasks[feed_id].done():
            return
        self._digest_feed_locks.setdefault(feed_id, asyncio.Lock())
        self._digest_feed_tasks[feed_id] = asyncio.create_task(
            self._digest_feed_loop(feed_id),
            name=f"rss-digest-feed-{feed_id}",
        )

    def _register_daily_digest(self, digest: DailyDigestConfig) -> None:
        if digest.id in self._digest_send_tasks and not self._digest_send_tasks[digest.id].done():
            return
        self._digest_send_locks.setdefault(digest.id, asyncio.Lock())
        self._digest_send_tasks[digest.id] = asyncio.create_task(
            self._daily_digest_loop(digest),
            name=f"rss-digest-send-{digest.id}",
        )

    def _register_job(self, job: JobConfig) -> None:
        if job.id in self._job_tasks and not self._job_tasks[job.id].done():
            return

        self._job_locks.setdefault(job.id, asyncio.Lock())
        self._job_tasks[job.id] = asyncio.create_task(
            self._job_loop(job),
            name=f"rss-job-{job.id}",
        )

    async def _digest_feed_loop(self, feed_id: str) -> None:
        initial_delay = self._startup_delay_seconds()
        if initial_delay > 0:
            logger.info(
                "digest-feed=%s waiting startup delay=%ss before first poll",
                feed_id,
                initial_delay,
            )
            await asyncio.sleep(initial_delay)
        while True:
            await self._collect_digest_feed_once(feed_id)
            await asyncio.sleep(self._poll_interval_seconds())

    async def _daily_digest_loop(self, digest: DailyDigestConfig) -> None:
        initial_delay = self._startup_delay_seconds()
        if initial_delay > 0:
            logger.info(
                "daily-digest=%s waiting startup delay=%ss before first check",
                digest.id,
                initial_delay,
            )
            await asyncio.sleep(initial_delay)
        while True:
            if await self._should_run_daily_digest(digest):
                await self._run_daily_digest_once_guarded(
                    digest,
                    scheduled_date=self._local_now().date().isoformat(),
                )
            await asyncio.sleep(30)

    async def _job_loop(self, job: JobConfig) -> None:
        initial_delay = self._startup_delay_seconds()
        if initial_delay > 0:
            logger.info("job=%s waiting startup delay=%ss before first poll", job.id, initial_delay)
            await asyncio.sleep(initial_delay)
        while True:
            if str(getattr(job, "cron", "") or "").strip():
                delay = self._cron_next_delay_seconds(job)
                logger.info("job=%s waiting cron delay=%ss before next poll", job.id, delay)
                await asyncio.sleep(delay)
                await self._run_job_once_guarded(job)
            else:
                await self._run_job_once_guarded(job)
                await asyncio.sleep(self._resolve_interval(job))

    def _resolve_interval(self, job: JobConfig) -> int:
        if getattr(job, "interval_seconds", 0) > 0:
            return int(job.interval_seconds)
        if str(getattr(job, "cron", "") or "").strip():
            if self._parse_cron_expression(str(getattr(job, "cron", "") or "")) is not None:
                return self._poll_interval_seconds()
            logger.warning(
                "job=%s configured with invalid cron=%s, fallback interval=%s",
                job.id,
                job.cron,
                self._poll_interval_seconds(),
            )
        return self._poll_interval_seconds()

    def _jobs(self) -> list[JobConfig]:
        return list(getattr(self._config, "jobs", []) or [])

    def _daily_digests(self) -> list[DailyDigestConfig]:
        return list(getattr(self._config, "daily_digests", []) or [])

    def _targets(self) -> list[Any]:
        return list(getattr(self._config, "targets", []) or [])

    def _startup_delay_seconds(self) -> int:
        return max(int(getattr(self._config, "startup_delay_seconds", 0) or 0), 0)

    def _poll_interval_seconds(self) -> int:
        return max(int(getattr(self._config, "poll_interval_seconds", 300) or 300), 1)


    def _cron_next_delay_seconds(self, job: JobConfig, *, now: datetime | None = None) -> int:
        cron = str(getattr(job, "cron", "") or "").strip()
        parsed = self._parse_cron_expression(cron)
        if parsed is None:
            return self._resolve_interval(job)

        base = now or self._local_now()
        if base.tzinfo is None:
            base = base.replace(tzinfo=self._local_timezone())
        candidate = (base + timedelta(minutes=1)).replace(second=0, microsecond=0)
        for _ in range(366 * 24 * 60):
            if self._cron_datetime_matches(candidate, parsed):
                return max(int((candidate - base).total_seconds()), 1)
            candidate += timedelta(minutes=1)
        return self._poll_interval_seconds()

    @classmethod
    def _parse_cron_expression(cls, cron: str) -> tuple[set[int], set[int], set[int], set[int], set[int]] | None:
        parts = str(cron or "").split()
        if len(parts) != 5:
            return None
        ranges = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 7)]
        parsed = []
        for field, (minimum, maximum) in zip(parts, ranges, strict=True):
            values = cls._parse_cron_field(field, minimum=minimum, maximum=maximum)
            if not values:
                return None
            if maximum == 7:
                values = {0 if value == 7 else value for value in values}
            parsed.append(values)
        return tuple(parsed)  # type: ignore[return-value]

    @staticmethod
    def _parse_cron_field(field: str, *, minimum: int, maximum: int) -> set[int]:
        values: set[int] = set()
        for part in str(field or "").split(","):
            part = part.strip()
            if not part:
                return set()
            step = 1
            if "/" in part:
                base, step_text = part.split("/", 1)
                try:
                    step = int(step_text)
                except ValueError:
                    return set()
                if step <= 0:
                    return set()
            else:
                base = part
            if base == "*":
                start, end = minimum, maximum
            elif "-" in base:
                start_text, end_text = base.split("-", 1)
                try:
                    start, end = int(start_text), int(end_text)
                except ValueError:
                    return set()
            else:
                try:
                    start = end = int(base)
                except ValueError:
                    return set()
            if start < minimum or end > maximum or start > end:
                return set()
            values.update(range(start, end + 1, step))
        return values

    @staticmethod
    def _cron_datetime_matches(
        value: datetime,
        parsed: tuple[set[int], set[int], set[int], set[int], set[int]],
    ) -> bool:
        minutes, hours, days, months, weekdays = parsed
        cron_weekday = (value.weekday() + 1) % 7
        return (
            value.minute in minutes
            and value.hour in hours
            and value.day in days
            and value.month in months
            and cron_weekday in weekdays
        )

    def _local_timezone(self):
        timezone_name = str(getattr(self._config, "timezone", "Asia/Shanghai") or "Asia/Shanghai")
        try:
            return ZoneInfo(timezone_name)
        except Exception:
            return timezone.utc

    def _local_now(self) -> datetime:
        return datetime.now(self._local_timezone())

    def _format_local_timestamp(self, timestamp_value: int) -> str:
        parsed = datetime.fromtimestamp(int(timestamp_value), tz=timezone.utc)
        return parsed.astimezone(self._local_timezone()).strftime("%Y-%m-%d %H:%M")

    async def _should_run_daily_digest(self, digest: DailyDigestConfig) -> bool:
        now_local = self._local_now()
        if now_local.strftime("%H:%M") < digest.send_time:
            return False
        status = await self._get_daily_digest_status(digest.id)
        return str(status.get("last_schedule_date", "") or "") != now_local.date().isoformat()

    async def _collect_digest_feed_once(self, feed_id: str) -> None:
        feed_lock = self._digest_feed_locks.setdefault(feed_id, asyncio.Lock())
        if feed_lock.locked():
            logger.warning("skip digest-feed=%s: previous collection still in progress", feed_id)
            return

        async with feed_lock:
            try:
                raw_items = await self._call_fetch_feed_ids([feed_id])
                if not raw_items:
                    return
                feed_meta = self._extract_feed_meta(raw_items)
                parse_failed: set[str] = set()
                parseable = [raw for raw in raw_items if 200 <= self._feed_response_status(raw) < 300]
                items = self._call_parse(parseable, None, failed_feed_ids=parse_failed) if parseable else []
                await self._archive_items(items)
                if feed_id not in feed_meta or feed_id in parse_failed:
                    return
                now_ts = int(time.time())
                meta = feed_meta[feed_id]
                await self._storage.update_feed_state(
                    feed_id,
                    etag=meta.get("etag"),
                    last_modified=meta.get("last_modified"),
                    last_success_time=now_ts,
                    bootstrap_done=True,
                    since_id=meta.get("since_id"),
                )
                logger.info(
                    "digest-feed=%s collected: fetched=%s parsed=%s",
                    feed_id,
                    len(raw_items),
                    len(items),
                )
            except Exception:
                logger.exception("digest-feed=%s collection failed", feed_id)

    async def _archive_items(self, items: list[dict]) -> None:
        archive_func = getattr(self._storage, "archive_digest_items", None)
        if not callable(archive_func):
            return
        try:
            await archive_func(items)
        except Exception:
            logger.exception("archive digest items failed")

    async def _call_fetch_feed_ids(self, feed_ids: list[str]) -> list[dict]:
        fetch_feed_ids = getattr(self._fetcher, "fetch_feed_ids", None)
        if callable(fetch_feed_ids):
            return await fetch_feed_ids(feed_ids)

        temp_job = type("TempJob", (), {"feed_ids": feed_ids})()
        return await self._call_fetch(temp_job)

    async def _run_daily_digest_once_guarded(
        self,
        digest: DailyDigestConfig,
        *,
        scheduled_date: str | None,
    ) -> None:
        digest_lock = self._digest_send_locks.setdefault(digest.id, asyncio.Lock())
        if digest_lock.locked():
            logger.warning("skip daily-digest=%s: previous run still in progress", digest.id)
            return

        async with digest_lock:
            started_at = datetime.now()
            started_perf = time.perf_counter()
            item_count = 0
            pushed_count = 0
            error_summary = ""
            status_fields: dict[str, Any] = {}

            try:
                window_end_ts = int(time.time())
                window_start_ts = window_end_ts - max(int(digest.window_hours), 1) * 3600
                items = await self._list_daily_digest_items(
                    digest.feed_ids,
                    window_start_ts=window_start_ts,
                    window_end_ts=window_end_ts,
                    limit=digest.max_items,
                )
                raw_item_count = len(items)
                item_count = len(items)
                status_fields.update(
                    {
                        "last_run_at": window_end_ts,
                        "last_window_start_ts": window_start_ts,
                        "last_window_end_ts": window_end_ts,
                        "last_raw_item_count": raw_item_count,
                        "last_item_count": item_count,
                    }
                )
                if scheduled_date:
                    status_fields["last_schedule_date"] = scheduled_date

                if not items:
                    status_fields["last_error"] = ""
                    status_fields["last_skipped_reason"] = "empty_window"
                    await self._update_daily_digest_status(digest.id, **status_fields)
                    logger.info("daily-digest=%s skipped: empty window", digest.id)
                    return

                target_origins = self._resolve_digest_target_origins(digest.target_ids)
                first_origin = target_origins[0] if target_origins else ""
                merge_result = await self._merge_daily_digest_items(
                    digest,
                    items,
                    unified_msg_origin=first_origin,
                )
                items = list(merge_result.get("items", items) or items)
                item_count = len(items)
                status_fields.update(
                    {
                        "last_item_count": item_count,
                        "last_semantic_merge_reason": str(merge_result.get("reason", "") or ""),
                        "last_semantic_merged_count": int(merge_result.get("merged_count", 0) or 0),
                    }
                )
                digest_context = {
                    "id": digest.id,
                    "title": digest.title,
                    "prompt_template": digest.prompt_template,
                    "max_items": digest.max_items,
                    "llm_timeout_seconds": digest.llm_timeout_seconds,
                    "window_start_text": self._format_local_timestamp(window_start_ts),
                    "window_end_text": self._format_local_timestamp(window_end_ts),
                }
                if self._pipeline is not None:
                    content_result = await self._pipeline.build_daily_digest_content(
                        digest_context,
                        items,
                        unified_msg_origin=first_origin,
                    )
                    content = str(content_result.get("content", "")).strip()
                    content_engine = str(content_result.get("engine", "fallback") or "fallback")
                    llm_reason = str(content_result.get("llm_reason", "") or "")
                else:
                    content = "\n".join(
                        f"{index}. [{str(item.get('feed_title', '') or item.get('source', '') or '未知来源').strip() or '未知来源'}] {str(item.get('title', '')).strip() or '(无标题)'}"
                        for index, item in enumerate(items, start=1)
                    )
                    content_engine = "fallback"
                    llm_reason = "pipeline_not_configured"

                digest_payload = {
                    "id": digest.id,
                    "title": digest.title or digest.id,
                    "target_ids": digest.target_ids,
                    "render_mode": digest.render_mode,
                    "window_start_text": self._format_local_timestamp(window_start_ts),
                    "window_end_text": self._format_local_timestamp(window_end_ts),
                    "item_count": item_count,
                    "content": content,
                    "links": self._daily_digest_links(items),
                }
                dispatch_result = await self._dispatcher.dispatch_daily_digest(digest_payload)
                pushed_count = dispatch_result.success_count
                status_fields["last_content_engine"] = content_engine
                status_fields["last_llm_reason"] = llm_reason

                if pushed_count > 0:
                    status_fields["last_sent_at"] = int(time.time())
                    status_fields["last_error"] = ""
                    status_fields["last_skipped_reason"] = ""
                elif (
                    dispatch_result.skipped_duplicate_count > 0
                    and dispatch_result.permanent_failure_count == 0
                    and dispatch_result.transient_failure_count == 0
                ):
                    status_fields["last_error"] = ""
                    status_fields["last_skipped_reason"] = "duplicate"
                else:
                    error_summary = "dispatch_failed"
                    status_fields["last_error"] = error_summary
                    status_fields["last_skipped_reason"] = ""

                await self._update_daily_digest_status(digest.id, **status_fields)
                logger.info(
                    "daily-digest=%s finished: items=%s pushed=%s engine=%s error=%s",
                    digest.id,
                    item_count,
                    pushed_count,
                    status_fields.get("last_content_engine", ""),
                    error_summary,
                )
            except Exception as exc:
                error_summary = f"{type(exc).__name__}: {exc}"
                await self._update_daily_digest_status(
                    digest.id,
                    **status_fields,
                    last_error=error_summary,
                    last_skipped_reason="",
                )
                logger.exception("daily-digest=%s execution failed", digest.id)
            finally:
                duration_ms = int((time.perf_counter() - started_perf) * 1000)
                self._digest_results[digest.id] = DigestExecutionResult(
                    started_at=started_at,
                    duration_ms=duration_ms,
                    item_count=item_count,
                    pushed_count=pushed_count,
                    error_summary=error_summary,
                )

    def _resolve_digest_target_origins(self, target_ids: list[str]) -> list[str]:
        enabled_targets = {
            target.id: target
            for target in self._targets()
            if target.enabled and target.unified_msg_origin
        }
        return sorted(
            {
                enabled_targets[target_id].unified_msg_origin
                for target_id in target_ids
                if target_id in enabled_targets
            }
        )

    def _build_seen_keys(self, item: dict) -> list[str]:
        build_keys = getattr(self._storage, "build_seen_keys", None)
        if callable(build_keys):
            keys = [str(key).strip() for key in build_keys(item) if str(key).strip()]
            if keys:
                return keys

        item_id = str(self._storage.build_dedup_key(item)).strip()
        return [item_id] if item_id else []

    async def _has_seen_any(self, keys: list[str], ttl_seconds: int | None = None) -> bool:
        has_seen = self._storage.has_seen
        accepts_ttl = self._accepts_keyword(has_seen, "ttl_seconds")
        for key in keys:
            if accepts_ttl:
                seen = await has_seen(key, ttl_seconds=ttl_seconds)
            else:
                seen = await has_seen(key)
            if seen:
                return True
        return False

    async def _mark_seen_all(self, keys: list[str], ttl_seconds: int) -> None:
        if not keys:
            return
        mark_seen_many = getattr(self._storage, "mark_seen_many", None)
        if callable(mark_seen_many):
            await mark_seen_many(list(dict.fromkeys(keys)), ttl_seconds=ttl_seconds)
            return
        for key in keys:
            await self._storage.mark_seen(key, ttl_seconds=ttl_seconds)

    def _resolve_job_target_origins(self, job: JobConfig, *, include_disabled: bool = False) -> list[str]:
        target_ids = [str(target_id).strip() for target_id in (getattr(job, "target_ids", []) or [])]
        if not target_ids:
            return []
        targets = {
            target.id: target
            for target in self._targets()
            if (include_disabled or getattr(target, "enabled", False)) and getattr(target, "unified_msg_origin", "")
        }
        origins = {
            str(targets[target_id].unified_msg_origin).strip()
            for target_id in target_ids
            if target_id in targets
        }
        return sorted(origin for origin in origins if origin)

    @staticmethod
    def _target_seen_key(job_id: str, origin: str, item_key: str) -> str:
        origin_digest = hashlib.sha256(str(origin).encode("utf-8")).hexdigest()
        return f"job:{job_id}:origin:{origin_digest}:{item_key}"

    def _target_seen_keys(self, job: JobConfig, origin: str, seen_keys: list[str]) -> list[str]:
        job_id = str(getattr(job, "id", "")).strip()
        if not job_id:
            return seen_keys
        return [self._target_seen_key(job_id, origin, key) for key in seen_keys]

    @staticmethod
    def _bootstrap_seen_keys(job: JobConfig, origin: str, feed_id: str, seen_keys: list[str]) -> list[str]:
        origin_digest = hashlib.sha256(origin.encode("utf-8")).hexdigest()
        feed_digest = hashlib.sha256(feed_id.encode("utf-8")).hexdigest()
        prefix = f"bootstrap:job:{job.id}:origin:{origin_digest}:feed:{feed_digest}:"
        return [prefix + key for key in seen_keys]

    async def _mark_bootstrap_seen(self, job, origins, feed_id, seen_keys, ttl_seconds):
        await self._mark_seen_all([
            key for origin in origins for key in self._bootstrap_seen_keys(job, origin, feed_id, seen_keys)
        ], ttl_seconds)

    def _subscription_scopes(self) -> list[tuple[str, str, str]]:
        return [
            (job.id, origin, feed_id)
            for job in self._jobs()
            for origin in self._resolve_job_target_origins(job)
            for feed_id in job.feed_ids
        ]

    async def _ensure_subscription_registry(self) -> None:
        initialize = getattr(self._storage, "initialize_subscription_registry", None)
        if callable(initialize):
            await initialize(self._subscription_scopes())

    async def _uninitialized_subscriptions(self, job: JobConfig) -> dict[str, list[str]]:
        check = getattr(self._storage, "is_subscription_initialized", None)
        if not callable(check):
            return {}
        pending = {}
        for feed_id in job.feed_ids:
            origins = []
            for origin in self._resolve_job_target_origins(job):
                if not await check(job.id, origin, feed_id):
                    origins.append(origin)
            if origins:
                pending[feed_id] = origins
        return pending

    async def _initialize_job_subscriptions(self, job, pending, raw_items, ttl_seconds):
        for feed_id, origins in pending.items():
            scoped_job = copy(job)
            scoped_job.feed_ids = [feed_id]
            snapshot = deepcopy([raw for raw in raw_items if raw.get("feed_id") == feed_id])
            try:
                _validate_raw(snapshot, scoped_job)
            except ValueError as exc:
                logger.warning("job=%s feed=%s subscription bootstrap deferred: %s", job.id, feed_id, exc)
                continue
            for raw in snapshot:
                raw["max_new_items"] = 0
            items = self._call_parse(snapshot, scoped_job)
            keys = [self._build_seen_keys(item) for item in items]
            if any(not item_keys for item_keys in keys):
                raise ValueError("bootstrap item has no deduplication keys")
            await self._mark_bootstrap_seen(job, origins, feed_id, [key for group in keys for key in group], ttl_seconds)
            for origin in origins:
                await self._storage.mark_subscription_initialized(job.id, origin, feed_id)

    async def _pending_target_origins(
        self, job: JobConfig, origins: tuple[str, ...], seen_keys: tuple[str, ...],
        ttl_seconds: int, feed_id: str,
    ) -> tuple[str, ...]:
        # Legacy global keys and subscription baselines still gate fresh admissions.
        if await self._has_seen_any(list(seen_keys), ttl_seconds=ttl_seconds):
            return ()
        if origins == ("",):
            return origins

        pending = []
        check = getattr(self._storage, "is_subscription_initialized", None)
        for origin in origins:
            if feed_id and callable(check) and not await check(job.id, origin, feed_id):
                continue
            scoped_keys = self._target_seen_keys(job, origin, list(seen_keys))
            if feed_id:
                scoped_keys += self._bootstrap_seen_keys(job, origin, feed_id, list(seen_keys))
            if not await self._has_seen_any(scoped_keys, ttl_seconds=ttl_seconds):
                pending.append(origin)
        return tuple(pending)

    def _delivery_seen_keys(
        self, job: JobConfig, origins: tuple[str, ...], seen_keys: tuple[str, ...],
    ) -> list[str]:
        return [
            key for origin in origins
            for key in (self._target_seen_keys(job, origin, list(seen_keys)) if origin else seen_keys)
        ]

    def _job_delivery_origins(self, job: JobConfig, *, include_disabled: bool = False) -> tuple[str, ...]:
        if not getattr(job, "target_ids", None):
            # Target-less scheduler stubs exercise the legacy unscoped dispatch interface.
            return ("",)
        return tuple(self._resolve_job_target_origins(job, include_disabled=include_disabled))

    def _job_feed_ids(self, job: JobConfig, *, include_disabled: bool = False) -> tuple[str, ...]:
        feeds = getattr(self._config, "feeds", None)
        if feeds is None:
            return tuple(job.feed_ids)
        allowed = {feed.id for feed in feeds if include_disabled or feed.enabled}
        return tuple(feed_id for feed_id in job.feed_ids if feed_id in allowed)

    def _prune_job_outbox(self, job: JobConfig) -> None:
        scope = (
            self._job_feed_ids(job, include_disabled=True),
            self._job_delivery_origins(job, include_disabled=True),
        )
        if self._outbox_scopes.get(job.id) != scope:
            self._outbox.prune_job(job.id, *scope)
            self._outbox_scopes[job.id] = scope

    async def _job_feed_states(self, job: JobConfig) -> dict[str, dict]:
        scopes = {
            (configured_job.id, feed_id)
            for configured_job in [*self._jobs(), job]
            for feed_id in self._job_feed_ids(configured_job, include_disabled=True)
        }
        if scopes - self._seeded_cursor_scopes:
            async with self._cursor_seed_lock:
                if scopes - self._seeded_cursor_scopes:
                    # Seed every configured job before any one job changes shared feed state.
                    missing = scopes - self._outbox.feed_scopes()
                    baselines = {}
                    for feed_id in sorted({feed_id for _, feed_id in missing}):
                        state = await self._storage.get_feed_state(feed_id)
                        baselines[feed_id] = {
                            key: state[key] for key in
                            ("etag", "last_modified", "since_id", "last_success_time", "bootstrap_done") if key in state
                        }
                    self._outbox.seed_feed_states([
                        (job_id, feed_id, baselines[feed_id]) for job_id, feed_id in sorted(missing)
                    ])
                    self._seeded_cursor_scopes = scopes
        return self._outbox.feed_states(job.id)

    @staticmethod
    def _result_origins(result: Any, attr_name: str) -> set[str]:
        values = getattr(result, attr_name, []) or []
        if isinstance(values, str):
            values = [values]
        return {str(value).strip() for value in values if str(value).strip()}

    def _job_dedup_ttl_seconds(self, job: JobConfig) -> int:
        job_ttl = int(getattr(job, "dedup_ttl_seconds", 0) or 0)
        if job_ttl > 0:
            return job_ttl
        return int(getattr(self._config, "dedup_ttl_seconds", 0) or 0)

    def _job_semantic_dedup_ttl_seconds(self, job: JobConfig, fallback_ttl_seconds: int) -> int:
        semantic_ttl = int(getattr(job, "semantic_dedup_ttl_seconds", 0) or 0)
        if semantic_ttl > 0:
            return semantic_ttl
        return max(int(fallback_ttl_seconds), 1)

    async def _check_semantic_duplicate(
        self,
        job: JobConfig,
        item: dict[str, Any],
    ) -> SemanticDedupResult:
        if self._semantic_deduper is None:
            return SemanticDedupResult(reason="not_configured")
        if not bool(getattr(job, "semantic_dedup_enabled", False)):
            return SemanticDedupResult(reason="disabled")

        origins = self._resolve_digest_target_origins(getattr(job, "target_ids", []) or [])
        unified_msg_origin = origins[0] if origins else ""
        try:
            return await self._semantic_deduper.check(
                job,
                item,
                unified_msg_origin=unified_msg_origin,
            )
        except Exception as exc:
            logger.warning("semantic dedup check failed job=%s: %s", job.id, exc)
            return SemanticDedupResult(reason=f"exception:{type(exc).__name__}")

    async def _remember_semantic_candidate(
        self,
        job: JobConfig,
        item: dict[str, Any],
        seen_keys: list[str],
        *,
        ttl_seconds: int,
    ) -> None:
        if self._semantic_deduper is None:
            return
        if not bool(getattr(job, "semantic_dedup_enabled", False)):
            return
        remember = getattr(self._semantic_deduper, "remember", None)
        if not callable(remember):
            return
        try:
            await remember(job, item, seen_keys, ttl_seconds=ttl_seconds)
        except Exception as exc:
            logger.warning("semantic dedup remember failed job=%s: %s", job.id, exc)

    async def _record_semantic_duplicate_match(
        self,
        job: JobConfig,
        result: SemanticDedupResult,
    ) -> None:
        if self._semantic_deduper is None or not result.matched_record_id:
            return
        marker = getattr(self._semantic_deduper, "record_duplicate_match", None)
        if not callable(marker):
            return
        try:
            await marker(job, result.matched_record_id)
        except Exception as exc:
            logger.warning("semantic dedup duplicate marker failed job=%s: %s", job.id, exc)

    async def _get_daily_digest_status(self, digest_id: str) -> dict[str, Any]:
        getter = getattr(self._storage, "get_daily_digest_status", None)
        if not callable(getter):
            return {}
        result = await getter(digest_id)
        return dict(result) if isinstance(result, dict) else {}

    async def _update_daily_digest_status(self, digest_id: str, **fields: Any) -> dict[str, Any]:
        updater = getattr(self._storage, "update_daily_digest_status", None)
        if not callable(updater):
            return {}
        result = await updater(digest_id, **fields)
        return dict(result) if isinstance(result, dict) else {}

    async def _list_daily_digest_items(
        self,
        feed_ids: list[str],
        *,
        window_start_ts: int,
        window_end_ts: int,
        limit: int,
    ) -> list[dict[str, Any]]:
        lister = getattr(self._storage, "list_digest_items", None)
        if not callable(lister):
            return []
        result = await lister(
            feed_ids,
            window_start_ts=window_start_ts,
            window_end_ts=window_end_ts,
            limit=limit,
        )
        return list(result or [])

    async def _merge_daily_digest_items(
        self,
        digest: DailyDigestConfig,
        items: list[dict[str, Any]],
        *,
        unified_msg_origin: str = "",
    ) -> dict[str, Any]:
        if self._semantic_deduper is None:
            return {
                "items": items,
                "reason": "not_configured",
                "merged_count": 0,
            }
        if not bool(getattr(digest, "semantic_merge_enabled", False)):
            return {
                "items": items,
                "reason": "disabled",
                "merged_count": 0,
            }
        merger = getattr(self._semantic_deduper, "merge_digest_items", None)
        if not callable(merger):
            return {
                "items": items,
                "reason": "not_supported",
                "merged_count": 0,
            }
        try:
            result = await merger(
                digest,
                items,
                unified_msg_origin=unified_msg_origin,
            )
        except Exception as exc:
            logger.warning("daily digest semantic merge failed digest=%s: %s", digest.id, exc)
            return {
                "items": items,
                "reason": f"exception:{type(exc).__name__}",
                "merged_count": 0,
            }
        if not isinstance(result, dict):
            return {
                "items": items,
                "reason": "invalid_result",
                "merged_count": 0,
            }
        return result

    @staticmethod
    def _daily_digest_links(items: list[dict[str, Any]]) -> list[dict[str, str]]:
        links: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for item in items:
            source_items = item.get("source_items")
            candidates = source_items if isinstance(source_items, list) else [item]
            for candidate in candidates:
                if not isinstance(candidate, dict):
                    continue
                link = str(candidate.get("link", "") or "").strip()
                if not link:
                    continue
                source = str(
                    candidate.get("feed_title", "")
                    or candidate.get("source", "")
                    or "未知来源"
                ).strip() or "未知来源"
                key = (source, link)
                if key in seen:
                    continue
                seen.add(key)
                links.append({"source": source, "link": link})
        return links

    async def _run_job_once_guarded(self, job: JobConfig) -> None:
        job_lock = self._job_locks.setdefault(job.id, asyncio.Lock())
        if job_lock.locked():
            logger.warning("skip job=%s: previous run still in progress", job.id)
            return

        async with job_lock:
            started_at = datetime.now()
            started_perf = time.perf_counter()
            ttl_seconds = self._job_dedup_ttl_seconds(job)
            failed_feed_ids = list(self._job_feed_ids(job))
            fetched_count = pushed_count = 0
            errors = []
            try:
                await self._ensure_subscription_registry()
                self._prune_job_outbox(job)
                feed_state_map = await self._job_feed_states(job)
                pending_subscriptions = await self._uninitialized_subscriptions(job)
                raw_items = await self._call_fetch(job, feed_states=feed_state_map)
                fetched_count = len(raw_items)
                feed_meta = self._extract_feed_meta(raw_items)
                parse_failed: set[str] = set()
                parseable = [raw for raw in raw_items if 200 <= self._feed_response_status(raw) < 300]
                items = self._call_parse(parseable, job, failed_feed_ids=parse_failed) if parseable else []
                for feed_id in parse_failed:
                    feed_meta.pop(feed_id, None)
                failed_feed_ids = [feed_id for feed_id in failed_feed_ids if feed_id not in feed_meta]
                if pending_subscriptions:
                    snapshot_job = copy(job)
                    snapshot_job.feed_ids = list(pending_subscriptions)
                    snapshot = await self._call_fetch(snapshot_job, pending_subscriptions)
                    await self._initialize_job_subscriptions(job, pending_subscriptions, snapshot, ttl_seconds)
                await self._archive_items(items)
                candidates = await self._collect_delivery_candidates(job, items, feed_state_map, ttl_seconds)
                now_ts = int(time.time())
                origins = set(self._resolve_job_target_origins(job))
                feed_updates = {}
                for feed_id in self._job_feed_ids(job):
                    if feed_id not in feed_meta:
                        continue
                    new_origins = pending_subscriptions.get(feed_id)
                    if new_origins and set(new_origins) == origins:
                        continue
                    feed_updates[feed_id] = dict(feed_meta[feed_id], last_success_time=now_ts, bootstrap_done=True)
                # Candidates and job-local cursors commit together; the shared status follows.
                self._outbox.enqueue(job.id, candidates, feed_updates=feed_updates)
                for feed_id, state in feed_updates.items():
                    await self._storage.update_feed_state(feed_id, **state)
            except Exception as exc:
                errors.append(f"{type(exc).__name__}: {exc}")
                logger.exception("job=%s poll failed", job.id)

            try:
                self._prune_job_outbox(job)
                pushed_count = await self._deliver_job_batch(job, ttl_seconds)
                await self._flush_delivery_receipts(job, ttl_seconds)
            except Exception as exc:
                errors.append(f"{type(exc).__name__}: {exc}")
                logger.exception("job=%s delivery failed", job.id)

            try:
                pending_items, pending_targets, last_delivery_at = self._outbox.status(job.id)
            except Exception as exc:
                errors.append(f"{type(exc).__name__}: {exc}")
                previous = self._job_results.get(job.id)
                pending_items = previous.pending_item_count if previous else 0
                pending_targets = previous.pending_target_count if previous else 0
                last_delivery_at = previous.last_delivery_at if previous else None
                logger.exception("job=%s outbox status unavailable", job.id)

            duration_ms = int((time.perf_counter() - started_perf) * 1000)
            self._job_results[job.id] = JobExecutionResult(
                started_at=started_at, duration_ms=duration_ms, fetched_count=fetched_count,
                pushed_count=pushed_count, error_summary="; ".join(errors), failed_feed_ids=failed_feed_ids,
                pending_item_count=pending_items, pending_target_count=pending_targets,
                last_delivery_at=last_delivery_at,
            )
            logger.info(
                "job=%s finished: fetched=%s pushed=%s failed_feeds=%s pending_items=%s pending_targets=%s duration_ms=%s error=%s",
                job.id, fetched_count, pushed_count, failed_feed_ids, pending_items,
                pending_targets, duration_ms, "; ".join(errors),
            )

    async def _collect_delivery_candidates(
        self, job: JobConfig, items: list[dict[str, Any]], feed_state_map: dict[str, dict],
        ttl_seconds: int,
    ) -> list[DeliveryCandidate]:
        origins = self._job_delivery_origins(job)
        active_feeds = set(self._job_feed_ids(job))
        if not origins or not items:
            return []

        keyed_items = [
            (item, tuple(self._build_seen_keys(item))) for item in items
            if str(item.get("feed_id", "")).strip() in active_feeds
        ]
        all_keys = tuple(dict.fromkeys(key for _, keys in keyed_items for key in keys))
        seen_in_run = self._outbox.matching_keys(job.id, all_keys)
        rejected = self._outbox.rejected_origins(job.id, all_keys)
        candidates = []
        history_keys = []
        for item, keys in keyed_items:
            if not keys or any(key in seen_in_run for key in keys):
                continue
            feed_id = str(item.get("feed_id", "")).strip()
            admissible = tuple(origin for origin in origins if not any(origin in rejected.get(key, ()) for key in keys))
            if not admissible:
                continue
            pending = await self._pending_target_origins(job, admissible, keys, ttl_seconds, feed_id)
            if not pending:
                continue
            seen_in_run.update(keys)
            if self._should_mark_history_only(item, feed_state_map):
                history_keys.extend(self._delivery_seen_keys(job, pending, keys))
            else:
                candidates.append(DeliveryCandidate(item=item, seen_keys=keys, origins=pending))
        await self._mark_seen_all(history_keys, ttl_seconds)
        return candidates

    def _delivery_item(self, job: JobConfig, entry: DeliveryEntry) -> dict[str, Any]:
        item = dict(entry.item)
        item["job_id"] = job.id
        item.pop("_target_origins", None)
        if entry.origins != ("",):
            item["_target_origins"] = list(entry.origins)
        feed_id = str(item.get("feed_id", "")).strip()
        item["proxy_url"] = next((
            str(getattr(feed, "proxy_url", "") or "").strip() for feed in getattr(self._config, "feeds", [])
            if feed.id == feed_id
        ), "")
        item["compact_mode_enabled"] = bool(getattr(job, "compact_mode_enabled", False))
        item["compact_mode_send_images"] = bool(getattr(job, "compact_mode_send_images", False))
        return item

    async def _deliver_job_batch(self, job: JobConfig, ttl_seconds: int) -> int:
        batch_size = max(int(getattr(job, "batch_size", 10) or 10), 1)
        aggregate = bool(getattr(job, "aggregate_enabled", False))
        if aggregate:
            aggregate_limit = int(getattr(job, "aggregate_max_items", batch_size) or batch_size)
            batch_size = min(batch_size, max(aggregate_limit, 1))
        entries = self._outbox.entries(
            job.id, batch_size, feed_ids=self._job_feed_ids(job), origins=self._job_delivery_origins(job),
        )
        pushed_count = 0
        buckets: dict[tuple[str, ...], list[tuple[DeliveryEntry, dict]]] = {}
        for entry in entries:
            event_item = self._delivery_item(job, entry)
            if not entry.attempted:
                semantic = await self._check_semantic_duplicate(job, event_item)
                if semantic.duplicate:
                    self._outbox.confirm(job.id, [(entry.row_id, origin) for origin in entry.origins])
                    await self._record_semantic_duplicate_match(job, semantic)
                    continue
            if aggregate:
                buckets.setdefault(entry.origins, []).append((entry, event_item))
                continue

            self._outbox.mark_attempted([entry])
            if self._pipeline is not None:
                event_item = await self._pipeline.process(event_item)
                event_item["job_id"] = job.id
                if entry.origins != ("",):
                    event_item["_target_origins"] = list(entry.origins)
            result = await self._dispatcher.dispatch(event_item)
            pushed_count += await self._confirm_job_dispatch(job, [(entry, event_item)], result, ttl_seconds)

        for origins, bucket in buckets.items():
            self._outbox.mark_attempted([entry for entry, _ in bucket])
            result = await self._dispatch_aggregate_bucket(
                job, [item for _, item in bucket], [origin for origin in origins if origin],
            )
            pushed_count += await self._confirm_job_dispatch(job, bucket, result, ttl_seconds)
        return pushed_count

    async def _confirm_job_dispatch(
        self, job: JobConfig, bucket: list[tuple[DeliveryEntry, dict]], result: Any, ttl_seconds: int,
    ) -> int:
        requested = set(bucket[0][0].origins)
        success_count = result.success_count
        duplicate_count = result.skipped_duplicate_count
        permanent_count = result.permanent_failure_count
        delivered_at = datetime.now() if success_count > 0 else None
        retryable = result.transient_failure_count > 0 or result.skipped_disabled_count > 0
        confirmed = (
            self._result_origins(result, "success_origins")
            | self._result_origins(result, "skipped_duplicate_origins")
        ) & requested
        rejected = (self._result_origins(result, "permanent_failure_origins") & requested) - confirmed
        if not retryable:
            # Count-only stubs can identify all origins only when the outcome is uniform.
            if not permanent_count and success_count + duplicate_count >= len(requested):
                confirmed = requested
            elif not (success_count or duplicate_count) and permanent_count >= len(requested):
                rejected = requested
        self._outbox.confirm(
            job.id, [(entry.row_id, origin) for entry, _ in bucket for origin in sorted(confirmed)],
            delivered_at,
            rejected=[(entry.row_id, origin) for entry, _ in bucket for origin in sorted(rejected)],
            ttl_seconds=ttl_seconds,
        )
        if success_count > 0 and confirmed | rejected == requested:
            for entry, item in bucket:
                await self._remember_semantic_candidate(
                    job, item, list(entry.seen_keys),
                    ttl_seconds=self._job_semantic_dedup_ttl_seconds(job, ttl_seconds),
                )
        return len(bucket) if success_count > 0 else 0

    async def _flush_delivery_receipts(self, job: JobConfig, ttl_seconds: int) -> None:
        while entries := self._outbox.entries(job.id, 64, confirmed=True):
            await self._mark_seen_all([
                key for entry in entries
                for key in self._delivery_seen_keys(job, entry.confirmed_origins, entry.seen_keys)
            ], ttl_seconds)
            self._outbox.remove([
                (entry.row_id, origin) for entry in entries for origin in entry.confirmed_origins
            ])

    async def _dispatch_aggregate_bucket(
        self,
        job: JobConfig,
        items: list[dict[str, Any]],
        pending_origins: list[str],
    ) -> Any:
        first_origin = pending_origins[0] if pending_origins else ""
        job_context = self._build_aggregate_context(job)
        if self._pipeline is not None and callable(getattr(self._pipeline, "build_aggregate_content", None)):
            content_result = await self._pipeline.build_aggregate_content(
                job_context,
                items,
                unified_msg_origin=first_origin,
            )
        else:
            content_result = self._build_aggregate_fallback_content(items)
        logger.info(
            "job=%s aggregate content built: items=%s engine=%s llm=%s title=%s",
            job.id,
            len(items),
            str(content_result.get("engine", "fallback") or "fallback"),
            str(content_result.get("llm_reason", "") or ""),
            str(content_result.get("title", "") or "")[:80],
        )
        aggregate_payload = self._build_aggregate_payload(
            job,
            items,
            content_result,
            pending_origins=pending_origins,
        )
        dispatcher_func = getattr(self._dispatcher, "dispatch_aggregate_digest", None)
        if callable(dispatcher_func):
            return await dispatcher_func(aggregate_payload)
        return await self._dispatcher.dispatch_daily_digest(aggregate_payload)

    def _build_aggregate_context(self, job: JobConfig) -> dict[str, Any]:
        return {
            "id": str(getattr(job, "id", "") or "").strip(),
            "aggregate_provider_id": str(getattr(job, "aggregate_provider_id", "") or "").strip(),
            "aggregate_max_items": int(getattr(job, "aggregate_max_items", 12) or 12),
            "aggregate_llm_timeout_seconds": int(getattr(job, "aggregate_llm_timeout_seconds", 0) or 0),
            "aggregate_prompt_template": str(getattr(job, "aggregate_prompt_template", "") or "").strip(),
        }

    def _build_aggregate_payload(
        self,
        job: JobConfig,
        items: list[dict[str, Any]],
        content_result: dict[str, Any],
        *,
        pending_origins: list[str],
    ) -> dict[str, Any]:
        now_ts = int(time.time())
        title = str(content_result.get("title", "") or "").strip() or f"RSS 聚合：{len(items)} 条更新"
        sections = list(content_result.get("sections", []) or [])
        return {
            "id": f"{job.id}:{now_ts}",
            "job_id": job.id,
            "target_ids": list(getattr(job, "target_ids", []) or []),
            "_target_origins": list(pending_origins),
            "title": title,
            "content": str(content_result.get("content", "") or "").strip(),
            "sections": sections,
            "items": items,
            "render_mode": str(getattr(job, "aggregate_render_mode", "text") or "text").strip().lower(),
            "include_images": bool(getattr(job, "aggregate_include_images", True)),
            "item_count": len(items),
            "links": self._aggregate_links(items),
            "source_text": self._aggregate_source_text(items),
            "engine": str(content_result.get("engine", "fallback") or "fallback"),
            "llm_reason": str(content_result.get("llm_reason", "") or ""),
        }

    @staticmethod
    def _build_aggregate_fallback_content(items: list[dict[str, Any]]) -> dict[str, Any]:
        sections = []
        for item in items:
            title = str(item.get("title", "") or "(无标题)").strip()
            summary = str(item.get("summary", "") or item.get("content", "") or title).strip()
            sections.append(
                {
                    "title": title,
                    "summary": summary,
                    "source": str(item.get("feed_title", "") or item.get("source", "") or "未知来源").strip(),
                    "link": str(item.get("link", "") or "").strip(),
                    "proxy_url": str(item.get("proxy_url", "") or "").strip(),
                    "image_url": str(item.get("image_url", "") or "").strip(),
                    "image_path": str(item.get("image_path", "") or "").strip(),
                }
            )
        content = "\n".join(
            f"{index}. {section['title']}"
            for index, section in enumerate(sections, start=1)
        )
        first_title = sections[0]["title"] if sections else "RSS 聚合"
        title = first_title if len(sections) == 1 else "RSS 聚合"
        return {
            "title": title,
            "content": content,
            "sections": sections,
            "engine": "fallback",
        }

    @staticmethod
    def _aggregate_links(items: list[dict[str, Any]]) -> list[dict[str, str]]:
        links: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for item in items:
            link = str(item.get("link", "") or "").strip()
            if not link:
                continue
            source = str(item.get("feed_title", "") or item.get("source", "") or "未知来源").strip()
            key = (source, link)
            if key in seen:
                continue
            seen.add(key)
            links.append({"source": source, "link": link})
        return links

    @staticmethod
    def _aggregate_source_text(items: list[dict[str, Any]]) -> str:
        sources: list[str] = []
        for item in items:
            source = str(item.get("feed_title", "") or item.get("source", "") or "").strip()
            if source and source not in sources:
                sources.append(source)
        return " / ".join(sources)

    async def _call_fetch(
        self, job: JobConfig, pending_feeds=None, *, feed_states: dict[str, dict] | None = None,
    ) -> list[dict]:
        fetcher = self._fetcher
        if pending_feeds:
            fetcher = _bootstrap_fetcher(fetcher, pending_feeds)
        elif feed_states is not None and getattr(fetcher, "_storage", None) is not None:
            fetcher = copy(fetcher)
            fetcher._storage = _JobFeedStorage(self._fetcher._storage, feed_states)
        fetch_func = fetcher.fetch
        if self._accepts_argument(fetch_func):
            raw_items = await fetch_func(job)
        else:
            raw_items = await fetch_func()
        return raw_items

    def _call_parse(
        self, raw_items: list[dict], job: JobConfig | None, *, failed_feed_ids: set[str] | None = None,
    ) -> list[dict]:
        parse_func = self._parser.parse
        kwargs = {}
        if failed_feed_ids is not None and self._accepts_keyword(parse_func, "failed_feed_ids"):
            kwargs["failed_feed_ids"] = failed_feed_ids
        if self._accepts_argument(parse_func, expected_count=2):
            return parse_func(raw_items, job, **kwargs)
        return parse_func(raw_items, **kwargs)

    @staticmethod
    def _feed_response_status(raw_item: Any) -> int:
        if not isinstance(raw_item, dict):
            return 0
        try:
            return int(raw_item.get("status", 200))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _extract_feed_meta(raw_items: list[dict]) -> dict[str, dict[str, str]]:
        """提取成功抓取的来源及其可选元信息，兼容未提供状态码的旧结果。"""
        meta_by_feed: dict[str, dict[str, str]] = {}
        for raw_item in raw_items:
            status = RSSScheduler._feed_response_status(raw_item)
            if not (200 <= status < 300 or status == 304):
                continue
            feed_id = str(raw_item.get("feed_id", "")).strip()
            if not feed_id:
                continue
            etag = str(raw_item.get("etag", "")).strip()
            last_modified = str(raw_item.get("last_modified", "")).strip()
            since_id = str(raw_item.get("since_id", "")).strip()
            meta_by_feed[feed_id] = {
                "etag": etag,
                "last_modified": last_modified,
                "since_id": since_id,
            }
        return meta_by_feed

    @staticmethod
    def _accepts_argument(func, expected_count: int = 1) -> bool:
        signature = inspect.signature(func)
        positional_or_keyword_params = [
            param
            for param in signature.parameters.values()
            if param.kind
            in {inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD}
        ]
        return len(positional_or_keyword_params) >= expected_count

    @staticmethod
    def _accepts_keyword(func, name: str) -> bool:
        signature = inspect.signature(func)
        return any(
            param.name == name or param.kind == inspect.Parameter.VAR_KEYWORD
            for param in signature.parameters.values()
        )

    @classmethod
    def _should_mark_history_only(
        cls,
        item: dict,
        feed_state_map: dict[str, dict[str, int | str]],
        *,
        bootstrap_only: bool = True,
    ) -> bool:
        feed_id = str(item.get("feed_id", "")).strip()
        if not feed_id:
            return False

        feed_state = feed_state_map.get(feed_id) or {}
        if bootstrap_only and bool(feed_state.get("bootstrap_done", False)):
            return False

        try:
            last_success_time = int(feed_state.get("last_success_time", 0) or 0)
        except (TypeError, ValueError):
            return False
        if last_success_time <= 0:
            return False

        published_at = cls._parse_item_timestamp(item.get("published_at"))
        if published_at is None:
            return False
        return int(published_at.timestamp()) <= last_success_time

    @staticmethod
    def _parse_item_timestamp(raw_value) -> datetime | None:
        text = str(raw_value or "").strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
