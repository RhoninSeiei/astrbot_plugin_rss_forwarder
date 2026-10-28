from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Awaitable, Callable
from copy import deepcopy
from urllib.parse import urlsplit, urlunsplit

from .config import ConfigValidationError, RSSConfig


_SAFE_FIELDS = {
    "feeds": ("id", "title", "url", "source_type", "enabled", "timeout", "verify_ssl",
              "send_images", "send_videos", "send_link", "max_new_items"),
    "targets": ("id", "platform", "enabled", "label", "name", "unified_msg_origin"),
    "jobs": ("id", "title", "feed_ids", "target_ids", "enabled", "cron", "interval_seconds"),
    "daily_digests": ("id", "title", "feed_ids", "target_ids", "enabled", "send_time",
                      "window_hours", "max_items", "render_mode"),
}
_URL_PATTERN = re.compile(r"[a-z][a-z0-9+.-]*://[^\s<>\"']+", re.IGNORECASE)
_CRON_RANGES = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))


def _hash(value) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _has_control(value: str) -> bool:
    return any(ord(char) < 32 or ord(char) == 127 for char in value)


def _subscription_url(value: str) -> str:
    if not isinstance(value, str) or _has_control(value):
        raise ConfigValidationError("url must be an HTTP(S) URL without userinfo")
    value = value.strip()
    try:
        parsed = urlsplit(value)
        valid = (parsed.scheme in {"http", "https"} and bool(parsed.hostname)
                 and parsed.username is None and parsed.password is None
                 and not any(char.isspace() for char in value) and "\\" not in value)
        parsed.port
    except ValueError:
        valid = False
    if not valid:
        raise ConfigValidationError("url must be an HTTP(S) URL without userinfo")
    return urlunsplit((parsed.scheme, parsed.netloc.lower(), parsed.path, parsed.query, parsed.fragment))


def _redact_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        if not host or parsed.scheme not in {"http", "https"}:
            return "[REDACTED]"
        if ":" in host:
            host = f"[{host}]"
        if parsed.port is not None:
            host += f":{parsed.port}"
        return urlunsplit((parsed.scheme, host, parsed.path,
                           "REDACTED" if parsed.query else "", ""))
    except ValueError:
        return "[REDACTED]"


def _safe_value(value):
    if isinstance(value, str):
        return _URL_PATTERN.sub(lambda match: _redact_url(match.group()), value)
    if isinstance(value, list):
        return [_safe_value(item) for item in value if isinstance(item, (str, bool, int, float))]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    # Never expose arbitrary nested configuration under a display field.
    return ""


def _parse_cron(value: str) -> tuple[tuple[int, ...], ...]:
    """Validate the scheduler's numeric five-field cron dialect without importing it."""
    fields = value.split()
    if len(fields) != 5:
        raise ConfigValidationError("cron must contain five valid numeric fields")
    parsed = []
    for field, (minimum, maximum) in zip(fields, _CRON_RANGES):
        values = set()
        for part in field.split(","):
            match = re.fullmatch(r"(\*|[0-9]+(?:-[0-9]+)?)(?:/([0-9]+))?", part)
            if match is None:
                raise ConfigValidationError("invalid cron field")
            base, step_text = match.groups()
            step = int(step_text) if step_text is not None else 1
            if base == "*":
                start, end = minimum, maximum
            elif "-" in base:
                start, end = map(int, base.split("-"))
            else:
                start = end = int(base)
            if not minimum <= start <= end <= maximum or step <= 0:
                raise ConfigValidationError("cron field is out of range")
            values.update(range(start, end + 1, step))
        if maximum == 7:
            values = {0 if item == 7 else item for item in values}
        parsed.append(tuple(sorted(values)))
    return tuple(parsed)


def _schedule_key(interval_seconds: int, cron: str):
    if cron:
        return ("cron", _parse_cron(cron))
    return ("interval", interval_seconds)


class RevisionConflictError(ValueError):
    """The supplied revision no longer describes the committed configuration."""


def safe_validation_error(error) -> str:
    if isinstance(error, ConfigValidationError) or "://" in str(error):
        return "配置校验失败，请检查订阅地址、推送目标和排期设置"
    return _safe_value(str(error))[:400]


class SubscriptionManagementService:
    """Manage configuration only; callers own authorization and persistence effects."""

    def __init__(self, raw_config: dict, apply_config: Callable[[dict], Awaitable[None]]):
        self._raw_config = deepcopy(dict(raw_config))
        self._apply_config = apply_config
        self._lock = asyncio.Lock()

    def snapshot(self) -> dict:
        raw = self._raw_config
        result = {"revision": _hash(raw)}
        for kind, fields in _SAFE_FIELDS.items():
            entries = []
            for item in RSSConfig._normalize_collection(raw.get(kind, [])):
                safe = {key: _safe_value(item[key]) for key in fields if key in item}
                safe["enabled"] = bool(item.get("enabled", kind != "daily_digests"))
                if kind == "feeds" and "url" in item:
                    safe["url"] = _redact_url(str(item["url"]))
                entries.append(safe)
            result[kind] = entries
        return result

    async def drain(self) -> None:
        """Wait for writes already admitted; the adapter must first stop admitting writes."""
        async with self._lock:
            pass

    def _check_revision(self, expected_revision: str) -> None:
        if not isinstance(expected_revision, str):
            raise ConfigValidationError("expected_revision must be a string")
        if expected_revision and expected_revision != _hash(self._raw_config):
            raise RevisionConflictError("configuration revision conflict")

    @staticmethod
    def _append(candidate: dict, kind: str, item: dict) -> None:
        collection = candidate.setdefault(kind, [])
        if isinstance(collection, list):
            collection.append(item)
        elif isinstance(collection, dict) and item["id"] not in collection:
            collection[item["id"]] = item
        else:
            raise ConfigValidationError(f"invalid {kind} collection or conflicting id")

    @staticmethod
    def _resolve_targets(candidate: dict, target_ids: list[str],
                         current_origin: str, current_platform: str) -> list[str]:
        if not isinstance(target_ids, list) or any(
            not isinstance(item, str) or not item.strip() for item in target_ids
        ):
            raise ConfigValidationError("target_ids must be a list of non-empty strings")
        targets = RSSConfig._normalize_collection(candidate.get("targets", []))
        if target_ids:
            ids = sorted({item.strip() for item in target_ids})
            enabled_ids = {str(item.get("id", "")).strip() for item in targets
                           if item.get("enabled", True)}
            if not set(ids) <= enabled_ids:
                raise ConfigValidationError("target_ids must reference existing enabled targets")
            return ids

        if (not isinstance(current_origin, str) or not isinstance(current_platform, str)
                or _has_control(current_origin) or _has_control(current_platform)
                or not current_platform.strip()
                or len(current_origin.split(":", 2)) != 3
                or not all(part.strip() for part in current_origin.split(":", 2))):
            raise ConfigValidationError("empty target_ids requires valid current session context")
        origin, platform = current_origin.strip(), current_platform.strip()
        for target in targets:
            if str(target.get("unified_msg_origin", "")).strip() == origin:
                target_id = str(target.get("id", "")).strip()
                if not target.get("enabled", True) or not target_id:
                    raise ConfigValidationError("current session target must be enabled and have an id")
                return [target_id]
        target_id = "target-" + _hash([platform, origin])[:24]
        SubscriptionManagementService._append(candidate, "targets", {
            "__template_key": "target", "id": target_id, "platform": platform,
            "unified_msg_origin": origin, "enabled": True,
        })
        return [target_id]

    async def _commit(self, candidate: dict) -> str:
        # The parser migrates panel template keys in place; validate a disposable copy.
        RSSConfig.from_context(deepcopy(candidate))
        revision = _hash(candidate)
        await self._apply_config(deepcopy(candidate))
        self._raw_config = candidate
        return revision

    async def apply_subscription(self, *, url: str, target_ids: list[str],
                                 interval_seconds: int = 0, cron: str = "", title: str = "",
                                 expected_revision: str = "", current_origin: str = "",
                                 current_platform: str = "") -> dict:
        async with self._lock:
            self._check_revision(expected_revision)
            candidate = deepcopy(self._raw_config)
            url = _subscription_url(url)
            if not isinstance(title, str) or not isinstance(cron, str):
                raise ConfigValidationError("title and cron must be strings")
            cron = " ".join(cron.split())
            if (type(interval_seconds) is not int
                    or (cron and interval_seconds != 0)
                    or (not cron and interval_seconds < 60)):
                raise ConfigValidationError("provide either interval_seconds >= 60 or a five-field cron")
            schedule = _schedule_key(interval_seconds, cron)
            ids = self._resolve_targets(candidate, target_ids, current_origin, current_platform)

            feed_id = ""
            for feed in RSSConfig._normalize_collection(candidate.get("feeds", [])):
                if RSSConfig._feed_source_type(feed) != "rss":
                    continue
                try:
                    same_url = _subscription_url(feed.get("url", "")) == url
                except ValueError:
                    continue
                if same_url and str(feed.get("id", "")).strip():
                    feed_id = str(feed["id"]).strip()
                    break
            if not feed_id:
                feed_id = "feed-" + _hash(url)[:24]
                self._append(candidate, "feeds", {
                    "__template_key": "rss_feed", "id": feed_id, "source_type": "rss",
                    "url": url, "title": title.strip(), "enabled": True,
                    "proxy_url": str(candidate.get("subscription_proxy_url", "") or ""),
                })

            job_id = ""
            for job in RSSConfig._normalize_collection(candidate.get("jobs", [])):
                if (RSSConfig._normalize_id_list(job.get("feed_ids", [])) != [feed_id]
                        or set(RSSConfig._normalize_id_list(job.get("target_ids", []))) != set(ids)):
                    continue
                try:
                    existing_cron = str(job.get("cron", "") or "").strip()
                    existing_interval = int(job.get("interval_seconds", 0) or 0)
                    same_schedule = _schedule_key(existing_interval, existing_cron) == schedule
                except ValueError:
                    continue
                if same_schedule and str(job.get("id", "")).strip():
                    job_id = str(job["id"]).strip()
                    break
            if not job_id:
                job_id = "job-" + _hash([feed_id, ids, schedule])[:24]
                self._append(candidate, "jobs", {
                    "__template_key": "job", "id": job_id, "feed_ids": [feed_id],
                    "target_ids": ids, "interval_seconds": interval_seconds,
                    "cron": cron, "enabled": True,
                })
            revision = await self._commit(candidate)
            return {"feed_id": feed_id, "job_id": job_id, "revision": revision}

    async def set_enabled(self, kind: str, id: str, enabled: bool,
                          expected_revision: str = "") -> dict:
        async with self._lock:
            self._check_revision(expected_revision)
            if not isinstance(kind, str) or kind not in {"feeds", "jobs", "daily_digests"}:
                raise ConfigValidationError("kind must be feeds, jobs or daily_digests")
            if not isinstance(id, str) or not id.strip() or type(enabled) is not bool:
                raise ConfigValidationError("id must be non-empty and enabled must be a bool")
            candidate = deepcopy(self._raw_config)
            for item in RSSConfig._normalize_collection(candidate.get(kind, [])):
                if str(item.get("id", "")).strip() == id.strip():
                    item["enabled"] = enabled
                    revision = await self._commit(candidate)
                    return {"kind": kind, "id": id.strip(), "enabled": enabled, "revision": revision}
            raise ConfigValidationError("subscription entry not found")
