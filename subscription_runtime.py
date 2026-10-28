from __future__ import annotations

import asyncio
import inspect
from copy import copy, deepcopy

from .parser import ET


class _BootstrapStorage:
    """Hide conditional HTTP state and timeline cursors without writing shared state."""

    def __init__(self, storage):
        self._storage = storage

    async def get_feed_state(self, feed_id):
        return {"since_id": "0"}

    def __getattr__(self, name):
        return getattr(self._storage, name)


def _check_current(plugin, raw, committed, old_scheduler, *, applied=None):
    if not plugin._management_ready:
        raise RuntimeError("subscription management is not ready")
    expected = committed if applied is None else applied
    if (plugin._raw_config is not raw or dict(raw) != expected
            or plugin._committed_raw != committed or plugin.scheduler is not old_scheduler):
        raise ValueError("configuration changed externally; refresh before retrying")


async def _save(raw):
    save_async = getattr(raw, "save_config_async", None)
    if callable(save_async):
        # Cancellation must not leave a background file write racing the rollback.
        task = asyncio.ensure_future(save_async())
        try:
            result = await asyncio.shield(task)
        except asyncio.CancelledError:
            try:
                await task
            except Exception:
                pass
            raise
    else:
        save = getattr(raw, "save_config", None)
        if not callable(save):
            raise RuntimeError("configuration persistence is unavailable")
        result = save()
        if inspect.isawaitable(result):
            result = await result
    if result is False:
        raise RuntimeError("configuration save was superseded and did not commit")


def _validate_raw(raw_items, job):
    if not isinstance(raw_items, list) or any(not isinstance(item, dict) for item in raw_items):
        raise ValueError("bootstrap did not return a complete feed response")
    received = [item.get("feed_id") for item in raw_items]
    if (len(received) != len(set(job.feed_ids))
            or any(feed_id not in job.feed_ids for feed_id in received)
            or len(set(received)) != len(received)):
        raise ValueError("bootstrap must fetch every job feed exactly once")
    for raw in raw_items:
        if raw.get("status") != 200:
            raise ValueError("bootstrap requires HTTP 200; cached or failed responses are unsafe")
        if str(raw.get("source_type", "rss")).lower() == "twitter":
            if not isinstance(raw.get("items"), list):
                raise ValueError("bootstrap timeline response is invalid")
            continue
        # FeedParser logs and skips malformed XML, so distinguish failure from a valid empty feed.
        try:
            root = ET.fromstring(raw.get("body", ""))
            tag = root.tag.rsplit("}", 1)[-1]
            valid = tag == "feed" or (tag == "rss" and root.find("channel") is not None)
        except Exception:
            valid = False
        if not valid:
            raise ValueError("bootstrap requires a complete RSS or Atom document")


async def _prepare_bootstrap(scheduler, old_scheduler):
    old_ids = {job.id for job in old_scheduler._config.jobs}
    jobs = [job for job in scheduler._config.jobs if job.enabled and job.id not in old_ids]
    if not jobs:
        return []

    original_fetcher = scheduler._fetcher
    bootstrap_fetcher = copy(original_fetcher)
    bootstrap_fetcher._storage = _BootstrapStorage(original_fetcher._storage)
    bootstrap_fetcher._config = deepcopy(original_fetcher._config)
    for feed in bootstrap_fetcher._config.feeds:
        feed.max_new_items = 0
    scheduler._fetcher = bootstrap_fetcher
    prepared = []
    try:
        for job in jobs:
            origins = scheduler._resolve_job_target_origins(job)
            if not origins:
                raise ValueError("new subscription job has no enabled target origins")
            raw_items = await scheduler._fetcher.fetch(job)
            _validate_raw(raw_items, job)
            unlimited = deepcopy(raw_items)
            for raw in unlimited:
                raw["max_new_items"] = 0
            items = scheduler._parser.parse(unlimited, job)
            if not isinstance(items, list):
                raise ValueError("bootstrap parser did not return a complete item list")
            keys = []
            for item in items:
                if not isinstance(item, dict) or item.get("feed_id") not in job.feed_ids:
                    raise ValueError("bootstrap parser returned an invalid feed item")
                seen_keys = scheduler._build_seen_keys(item)
                if not seen_keys:
                    raise ValueError("bootstrap item has no deduplication keys")
                keys.append(seen_keys)
            prepared.append((job, origins, keys, scheduler._job_dedup_ttl_seconds(job)))
    finally:
        scheduler._fetcher = original_fetcher
    return prepared


def _copy_runtime_state(old_scheduler, new_scheduler):
    job_ids = {job.id for job in new_scheduler._config.jobs}
    digest_ids = {digest.id for digest in new_scheduler._config.daily_digests}
    new_scheduler._paused_jobs = set(old_scheduler._paused_jobs) & job_ids
    new_scheduler._job_results = deepcopy({
        key: value for key, value in old_scheduler._job_results.items() if key in job_ids
    })
    new_scheduler._digest_results = deepcopy({
        key: value for key, value in old_scheduler._digest_results.items() if key in digest_ids
    })


async def _start_preserving_pauses(scheduler):
    # RSSScheduler.start registers all enabled jobs without consulting _paused_jobs.
    paused = [job for job in scheduler._config.jobs if job.enabled and job.id in scheduler._paused_jobs]
    for job in paused:
        job.enabled = False
    try:
        await scheduler.start()
    finally:
        for job in paused:
            job.enabled = True


async def apply_runtime(plugin, candidate: dict) -> None:
    """Switch runtime configuration; the management service serializes callers with its lock."""
    if not isinstance(candidate, dict):
        raise ValueError("candidate must be a configuration dictionary")
    raw = plugin._raw_config
    committed = getattr(plugin, "_committed_raw", None)
    if not isinstance(raw, dict) or not isinstance(committed, dict):
        raise RuntimeError("committed subscription configuration is unavailable")
    committed = deepcopy(committed)
    old_scheduler = plugin.scheduler
    _check_current(plugin, raw, committed, old_scheduler)
    desired = deepcopy(dict(candidate))
    if desired == committed:
        return
    if not callable(getattr(raw, "save_config_async", None)) and not callable(getattr(raw, "save_config", None)):
        raise RuntimeError("configuration persistence is unavailable")

    new_scheduler = plugin._make_scheduler(deepcopy(desired))
    if new_scheduler is old_scheduler:
        raise RuntimeError("scheduler factory must return a new scheduler")
    stop_attempted = False
    raw_replaced = False
    try:
        prepared = await _prepare_bootstrap(new_scheduler, old_scheduler)
        _check_current(plugin, raw, committed, old_scheduler)
        stop_attempted = True
        await old_scheduler.stop()
        _check_current(plugin, raw, committed, old_scheduler)
        for job, origins, keys, ttl in prepared:
            for seen_keys in keys:
                await new_scheduler._mark_seen_for_origins(job, origins, seen_keys, ttl_seconds=ttl)
        _check_current(plugin, raw, committed, old_scheduler)
        raw_replaced = True
        raw.clear()
        raw.update(deepcopy(desired))
        await _save(raw)
        _check_current(plugin, raw, committed, old_scheduler, applied=desired)
        _copy_runtime_state(old_scheduler, new_scheduler)
        await _start_preserving_pauses(new_scheduler)
        _check_current(plugin, raw, committed, old_scheduler, applied=desired)
        probe_api = getattr(plugin, "_source_probe_api", None)
        if probe_api is not None:
            probe_api._config = new_scheduler._config
        plugin.scheduler = new_scheduler
        plugin._committed_raw = deepcopy(desired)
    except BaseException as error:
        rollback_errors = []
        new_stopped = False
        try:
            await new_scheduler.stop()
            new_stopped = True
        except BaseException as exc:
            rollback_errors.append(("new scheduler stop", exc))
        if raw_replaced:
            if plugin._raw_config is raw and dict(raw) == desired:
                raw.clear()
                raw.update(deepcopy(committed))
                try:
                    await _save(raw)
                except BaseException as exc:
                    rollback_errors.append(("configuration persistence", exc))
            else:
                rollback_errors.append(("external configuration conflict", error))
        if stop_attempted and new_stopped:
            try:
                await _start_preserving_pauses(old_scheduler)
            except BaseException as exc:
                rollback_errors.append(("old scheduler restart", exc))
        if rollback_errors:
            plugin._management_ready = False
            failure = RuntimeError("runtime rollback incomplete: " + ", ".join(phase for phase, _ in rollback_errors))
            failure.rollback_errors = tuple(rollback_errors)
            raise failure from error
        raise
