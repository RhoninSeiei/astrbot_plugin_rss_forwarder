from __future__ import annotations

import asyncio
import ipaddress
import re
import unicodedata
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from astrbot.api.star import Context
from astrbot.api.web import error_response, json_response, request

from .config import FeedConfig, RSSConfig
from .source_probe import SourceProbeService


PLUGIN_NAME = "astrbot_plugin_rss_forwarder"
_ANONYMOUS_LOCK_KEY = "anonymous"
_SOURCE_SCHEMES = {"http", "https"}
_PROXY_SCHEMES = {"http", "https", "socks4", "socks5", "socks5h"}


class SourceProbeApi:
    def __init__(self, config: RSSConfig, service: SourceProbeService) -> None:
        self._config = config
        self._service = service
        self._probe_locks: dict[str, asyncio.Lock] = {}

    def register(self, context: Context) -> None:
        context.register_web_api(
            f"/{PLUGIN_NAME}/source-probe/feeds",
            self.list_feeds,
            ["GET"],
            "List RSS Forwarder sources for diagnostics",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/source-probe/run",
            self.run_probe,
            ["POST"],
            "Probe RSS Forwarder source connectivity",
        )

    async def list_feeds(self):
        feeds = []
        for feed in self._config.feeds:
            source_type = str(feed.source_type or "rss").lower()
            source_url = feed.url
            if source_type == "twitter":
                source_url = feed.nitter_url or feed.url or "https://nitter.net"
            feeds.append(
                {
                    "id": str(feed.id),
                    "source_type": source_type,
                    "enabled": bool(feed.enabled),
                    "display_url": _redact_url(source_url),
                    "proxy_configured": bool(str(feed.proxy_url or "").strip()),
                    "timeout": int(feed.timeout),
                    "verify_ssl": bool(feed.verify_ssl),
                }
            )
        return json_response(feeds)

    async def run_probe(self):
        body = await request.json(default={})
        if not isinstance(body, dict):
            return error_response("JSON body must be an object", status_code=400)

        has_feed_id = "feed_id" in body
        has_draft = "draft" in body
        if has_feed_id == has_draft:
            return error_response(
                "Provide exactly one of feed_id or draft",
                status_code=400,
            )
        allowed_body_fields = {"full_check", "feed_id" if has_feed_id else "draft"}
        if set(body) - allowed_body_fields:
            return error_response("JSON body contains unsupported fields", status_code=400)

        full_check = body.get("full_check", False)
        if not isinstance(full_check, bool):
            return error_response("full_check must be a boolean", status_code=400)

        if has_feed_id:
            raw_feed_id = body["feed_id"]
            if not isinstance(raw_feed_id, str) or not raw_feed_id.strip():
                return error_response("feed_id must be a non-empty string", status_code=400)
            feed = self._find_feed(raw_feed_id.strip())
            if feed is None:
                return error_response("Source not found", status_code=404)
        else:
            try:
                feed = _draft_feed(body["draft"])
            except ValueError as exc:
                return error_response(str(exc), status_code=400)

        lock_key = _request_lock_key(request.username, request.client_host)
        lock = self._probe_locks.get(lock_key)
        if lock is None:
            lock = asyncio.Lock()
            self._probe_locks[lock_key] = lock
        if lock.locked():
            return error_response(
                "A source probe is already running for this user",
                status_code=429,
            )

        await lock.acquire()
        try:
            report = await self._service.probe(feed, full_check=full_check)
            payload = _redact_report_secrets(
                report.as_dict(),
                feed,
            )
            return json_response(payload)
        finally:
            lock.release()
            if self._probe_locks.get(lock_key) is lock and not lock.locked():
                self._probe_locks.pop(lock_key, None)

    def _find_feed(self, feed_id: str) -> FeedConfig | None:
        return next((feed for feed in self._config.feeds if feed.id == feed_id), None)


def _request_lock_key(username: str | None, client_host: str | None) -> str:
    for value in (username, client_host):
        if isinstance(value, str) and value.strip():
            return value.strip()
    return _ANONYMOUS_LOCK_KEY


def _draft_feed(raw_draft: Any) -> FeedConfig:
    if not isinstance(raw_draft, dict):
        raise ValueError("draft must be an object")

    source_type = _string_field(raw_draft, "source_type", "rss").lower()
    if source_type not in {"rss", "twitter"}:
        raise ValueError("draft.source_type must be rss or twitter")
    common_fields = {
        "source_type",
        "proxy_url",
        "timeout",
        "verify_ssl",
    }
    source_fields = (
        {"username", "nitter_url"}
        if source_type == "twitter"
        else {"url", "auth_mode", "key"}
    )
    if set(raw_draft) - common_fields - source_fields:
        raise ValueError("draft contains unsupported fields")

    proxy_url = _raw_string_field(raw_draft, "proxy_url", "")
    if proxy_url:
        _validate_url(proxy_url, "draft.proxy_url", _PROXY_SCHEMES)

    timeout = raw_draft.get("timeout", 10)
    if isinstance(timeout, bool) or not isinstance(timeout, int):
        raise ValueError("draft.timeout must be an integer")
    if not 3 <= timeout <= 30:
        raise ValueError("draft.timeout must be between 3 and 30")

    verify_ssl = raw_draft.get("verify_ssl", True)
    if not isinstance(verify_ssl, bool):
        raise ValueError("draft.verify_ssl must be a boolean")

    if source_type == "twitter":
        username = _string_field(raw_draft, "username", "").lstrip("@")
        if not username:
            raise ValueError("draft.username must be a non-empty string")
        nitter_url = _raw_string_field(raw_draft, "nitter_url", "")
        if nitter_url:
            _validate_url(nitter_url, "draft.nitter_url", _SOURCE_SCHEMES)
        return FeedConfig(
            id="draft",
            url="",
            source_type="twitter",
            username=username,
            nitter_url=nitter_url,
            proxy_url=proxy_url,
            timeout=timeout,
            verify_ssl=verify_ssl,
        )

    url = _raw_string_field(raw_draft, "url", "")
    _validate_url(url, "draft.url", _SOURCE_SCHEMES)
    auth_mode = _string_field(raw_draft, "auth_mode", "none").lower()
    if auth_mode not in {"none", "query", "header"}:
        raise ValueError("draft.auth_mode must be none, query or header")
    key = _string_field(raw_draft, "key", "")
    return FeedConfig(
        id="draft",
        url=url,
        source_type="rss",
        proxy_url=proxy_url,
        auth_mode=auth_mode,
        key=key,
        timeout=timeout,
        verify_ssl=verify_ssl,
    )


def _string_field(values: dict[str, Any], name: str, default: str) -> str:
    return _raw_string_field(values, name, default).strip()


def _raw_string_field(values: dict[str, Any], name: str, default: str) -> str:
    value = values.get(name, default)
    if not isinstance(value, str):
        raise ValueError(f"draft.{name} must be a string")
    return value


def _validate_url(value: str, field_name: str, schemes: set[str]) -> None:
    if _has_forbidden_url_character(value):
        raise ValueError(f"{field_name} must be a valid URL")
    authority = _raw_url_authority(value)
    if not authority or _has_forbidden_url_character(authority):
        raise ValueError(f"{field_name} must be a valid URL")
    try:
        parsed = urlsplit(value)
        scheme = parsed.scheme.lower()
        hostname = parsed.hostname
        parsed.port
    except ValueError:
        raise ValueError(f"{field_name} must be a valid URL") from None
    if (
        scheme not in schemes
        or not hostname
        or _has_forbidden_url_character(hostname)
    ):
        raise ValueError(f"{field_name} must be a valid URL")


def _raw_url_authority(value: str) -> str:
    scheme_end = value.find("://")
    if scheme_end < 0:
        return ""
    authority_start = scheme_end + 3
    authority_end = len(value)
    for delimiter in "/?#":
        delimiter_index = value.find(delimiter, authority_start)
        if delimiter_index >= 0:
            authority_end = min(authority_end, delimiter_index)
    return value[authority_start:authority_end]


def _has_forbidden_url_character(value: str) -> bool:
    return any(
        character == "\\"
        or character.isspace()
        or unicodedata.category(character) == "Cc"
        for character in value
    )


def _redact_url(value: str) -> str:
    try:
        parsed = urlsplit(str(value or ""))
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return ""
    if parsed.scheme.lower() not in _SOURCE_SCHEMES or not hostname:
        return ""
    safe_host = f"[{hostname}]" if ":" in hostname else hostname
    netloc = f"{safe_host}:{port}" if port is not None else safe_host
    return urlunsplit((parsed.scheme.lower(), netloc, parsed.path, "", ""))


def _feed_error_secrets(feed: FeedConfig) -> tuple[str, ...]:
    secrets = [str(feed.key or "")]
    for value in (feed.url, feed.nitter_url):
        try:
            parsed = urlsplit(str(value or ""))
        except ValueError:
            continue
        secrets.extend((parsed.username or "", parsed.password or ""))
    proxy_values, _, _ = _proxy_error_values(feed.proxy_url)
    secrets.extend(proxy_values)
    return tuple(sorted(set(filter(None, secrets)), key=len, reverse=True))


def _proxy_error_values(
    proxy_url: str,
) -> tuple[tuple[str, ...], tuple[str, ...], int | None]:
    raw_url = str(proxy_url or "")
    if not raw_url:
        return (), (), None
    try:
        parsed = urlsplit(raw_url)
        hostname = parsed.hostname or ""
        port = parsed.port
    except ValueError:
        return (raw_url,), (), None

    safe_hostname = f"[{hostname}]" if ":" in hostname else hostname
    host_port = f"{safe_hostname}:{port}" if port is not None else safe_hostname
    without_userinfo = urlunsplit(
        (parsed.scheme, host_port, parsed.path, parsed.query, parsed.fragment)
    )
    without_userinfo_and_query = urlunsplit(
        (parsed.scheme, host_port, parsed.path, "", "")
    )
    return (
        (
            raw_url,
            without_userinfo,
            without_userinfo_and_query,
            host_port,
            parsed.username or "",
            parsed.password or "",
        ),
        _proxy_hostname_variants(hostname),
        port,
    )


def _proxy_hostname_variants(hostname: str) -> tuple[str, ...]:
    variants = {hostname}
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        try:
            variants.add(hostname.encode("idna").decode("ascii"))
        except UnicodeError:
            pass
        try:
            variants.add(hostname.encode("ascii").decode("idna"))
        except UnicodeError:
            pass
    else:
        variants.update((str(address), address.compressed, address.exploded))
    return tuple(sorted(filter(None, variants), key=len, reverse=True))


def _proxy_hostname_pattern(hostnames: tuple[str, ...]) -> re.Pattern[str] | None:
    if not hostnames:
        return None
    alternatives = "|".join(re.escape(hostname) for hostname in hostnames)
    return re.compile(
        rf"(?<![\w.-])(?:{alternatives})(?![\w.-])",
        re.IGNORECASE,
    )


def _redact_report_secrets(value: Any, feed: FeedConfig) -> Any:
    if not isinstance(value, dict):
        return value
    redacted = dict(value)
    attempts = value.get("attempts")
    if isinstance(attempts, list):
        redacted_attempts = []
        for attempt in attempts:
            if not isinstance(attempt, dict):
                redacted_attempts.append(attempt)
                continue
            redacted_attempt = dict(attempt)
            error_message = attempt.get("error_message")
            if isinstance(error_message, str):
                error_type = attempt.get("error_type")
                redacted_attempt["error_message"] = _redact_error_message(
                    error_message,
                    feed,
                    error_type=error_type if isinstance(error_type, str) else "",
                )
            redacted_attempts.append(redacted_attempt)
        redacted["attempts"] = redacted_attempts

    recommendation = value.get("recommendation")
    if isinstance(recommendation, dict):
        redacted_recommendation = dict(recommendation)
        message = recommendation.get("message")
        if isinstance(message, str):
            redacted_recommendation["message"] = _redact_error_message(
                message,
                feed,
            )
        redacted["recommendation"] = redacted_recommendation
    return redacted


def _redact_error_message(
    value: str,
    feed: FeedConfig,
    *,
    error_type: str = "",
) -> str:
    redact_proxy_port = (
        error_type.lower() == "proxy"
        or _has_proxy_address_context(value, feed)
    )
    for secret in _feed_error_secrets(feed):
        value = value.replace(secret, "<redacted>")

    _, proxy_hostnames, proxy_port = _proxy_error_values(feed.proxy_url)
    hostname_pattern = _proxy_hostname_pattern(proxy_hostnames)
    if hostname_pattern is not None:
        value = hostname_pattern.sub("<redacted>", value)
    if proxy_port is not None and redact_proxy_port:
        port_pattern = re.compile(rf"(?<!\d){proxy_port}(?!\d)")
        value = port_pattern.sub("<redacted>", value)
    return value


def _has_proxy_address_context(value: str, feed: FeedConfig) -> bool:
    _, proxy_hostnames, _ = _proxy_error_values(feed.proxy_url)
    hostname_pattern = _proxy_hostname_pattern(proxy_hostnames)
    return hostname_pattern is not None and hostname_pattern.search(value) is not None
