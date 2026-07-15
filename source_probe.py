from __future__ import annotations

import asyncio
import re
import socket
import ssl
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from html.parser import HTMLParser
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from xml.etree import ElementTree

from .config import FeedConfig
from .source_http import (
    SourceHttpResponse,
    build_nitter_timeline_request,
    build_rss_request,
    request_source,
)


class InvalidFeedError(ValueError):
    """Raised when a successful response is not a recognized source format."""


_ATOM_NAMESPACE = "http://www.w3.org/2005/Atom"
_RDF_NAMESPACE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"


class _NitterContentParser(HTMLParser):
    _VOID_ELEMENTS = {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }

    def __init__(self, username: str) -> None:
        super().__init__(convert_charrefs=True)
        self._username = str(username or "").strip().lstrip("@")
        self._stack: list[tuple[str, bool]] = []
        self.matched = False

    def handle_starttag(self, tag: str, attrs) -> None:
        in_timeline = self._inspect_element(tag, attrs)
        normalized_tag = tag.lower()
        if normalized_tag not in self._VOID_ELEMENTS:
            self._stack.append((normalized_tag, in_timeline))

    def handle_startendtag(self, tag: str, attrs) -> None:
        self._inspect_element(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        normalized_tag = tag.lower()
        for index in range(len(self._stack) - 1, -1, -1):
            if self._stack[index][0] == normalized_tag:
                del self._stack[index:]
                return

    def _inspect_element(self, tag: str, attrs) -> bool:
        normalized_tag = tag.lower()
        parent_in_timeline = self._stack[-1][1] if self._stack else False
        attributes = {str(name).lower(): str(value or "") for name, value in attrs}
        classes = set(attributes.get("class", "").split())
        is_content_element = normalized_tag not in {"script", "style"}
        in_timeline = parent_in_timeline or (
            is_content_element and "timeline-item" in classes
        )
        if not in_timeline or not is_content_element:
            return in_timeline
        if "tweet-content" in classes:
            self.matched = True
        href = attributes.get("href", "")
        if normalized_tag == "a" and self._username and re.search(
            rf"/{re.escape(self._username)}/status/\d+(?:\b|/)",
            href,
            re.IGNORECASE,
        ):
            self.matched = True
        return in_timeline


def _sanitize_url(match: re.Match[str]) -> str:
    raw_url = match.group(0)
    trailing = ""
    while raw_url and raw_url[-1] in ".,;)]}":
        trailing = raw_url[-1] + trailing
        raw_url = raw_url[:-1]
    try:
        parsed = urlparse(raw_url)
        host = parsed.hostname or ""
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        netloc = host
        port = parsed.port
        if port is not None:
            netloc = f"{netloc}:{port}"
        sanitized = parsed._replace(
            netloc=netloc,
            query="",
            fragment="",
        ).geturl()
    except ValueError:
        sanitized = "<invalid-url>"
    return sanitized + trailing


def sanitize_error_message(exc: BaseException, *, secrets) -> str:
    message = str(exc)
    message = re.sub(
        r"(?i)'(?:authorization|cookie)'\s*:\s*'[^']*'",
        "<redacted-header>",
        message,
    )
    message = re.sub(
        r'(?i)"(?:authorization|cookie)"\s*:\s*"[^"]*"',
        "<redacted-header>",
        message,
    )
    message = re.sub(
        r"(?im)\b(?:authorization|cookie)\s*[:=]\s*[^\r\n]*",
        "",
        message,
    )
    message = re.sub(
        r"(?i)\b[a-z][a-z0-9+.-]*://[^\s<>'\"]+",
        _sanitize_url,
        message,
    )
    message = re.sub(
        r"(?<![:\w])(/[^\s?#<>'\"]+)[?#][^\s<>'\"]+",
        r"\1",
        message,
    )
    for secret in secrets:
        value = str(secret or "")
        if value:
            message = message.replace(value, "<redacted>")
    return " ".join(message.split())[:500]


def classify_probe_error(
    exc: BaseException,
    *,
    secrets,
) -> tuple[str, str, int | None]:
    message = sanitize_error_message(exc, secrets=secrets)
    raw_message = str(exc).lower()
    class_name = type(exc).__name__.lower()
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if status is None:
        status = getattr(exc, "code", None)
    status = int(status) if isinstance(status, int) else None

    if isinstance(exc, HTTPError) or class_name == "httpstatuserror" or status:
        return "http_status", message, status
    if isinstance(exc, InvalidFeedError):
        return "invalid_feed", message, None
    if class_name == "proxyerror" or any(
        marker in raw_message
        for marker in (
            "proxy error",
            "proxy connection",
            "proxy authentication",
            "proxy protocol",
            "tunnel connection",
        )
    ):
        return "proxy", message, None

    reason = exc.reason if isinstance(exc, URLError) else exc
    reason_message = str(reason).lower()
    reason_name = type(reason).__name__.lower()
    combined = f"{raw_message} {reason_message}"
    if isinstance(reason, (TimeoutError, socket.timeout)) or "timeout" in reason_name or "timed out" in combined:
        return "timeout", message, None
    if isinstance(reason, ssl.SSLError) or any(
        marker in combined
        for marker in (
            "certificate verify failed",
            "certificate_verify_failed",
            "unable to get local issuer",
            "self-signed certificate",
            "hostname mismatch",
        )
    ):
        return "tls_certificate", message, None
    if isinstance(reason, socket.gaierror) or any(
        marker in combined
        for marker in (
            "getaddrinfo",
            "name or service not known",
            "name not known",
            "nodename nor servname",
            "temporary failure in name resolution",
            "no such host",
        )
    ):
        return "dns", message, None
    if isinstance(reason, (ConnectionError, OSError)) or class_name == "connecterror" or any(
        marker in combined
        for marker in (
            "connection refused",
            "connection reset",
            "network is unreachable",
            "connect failed",
        )
    ):
        return "connect", message, None
    return "unknown", message, None


@dataclass(slots=True)
class ProbeAttempt:
    mode: str
    ok: bool
    http_status: int | None
    content_type: str
    latency_ms: int
    is_feed: bool
    feed_kind: str
    truncated: bool
    error_type: str
    error_message: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ProbeReport:
    feed_id: str
    source_type: str
    attempts: list[ProbeAttempt]
    recommendation: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "feed_id": self.feed_id,
            "source_type": self.source_type,
            "attempts": [attempt.as_dict() for attempt in self.attempts],
            "recommendation": dict(self.recommendation),
        }


class SourceProbeService:
    MAX_RESPONSE_BYTES = 256 * 1024
    MIN_TIMEOUT_SECONDS = 3
    MAX_TIMEOUT_SECONDS = 30

    def __init__(
        self,
        requester: Callable[..., SourceHttpResponse] = request_source,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._requester = requester
        self._clock = clock

    async def probe(
        self,
        feed: FeedConfig,
        *,
        full_check: bool = False,
    ) -> ProbeReport:
        proxy_url = str(getattr(feed, "proxy_url", "") or "").strip()
        username = str(getattr(feed, "username", "") or "")
        proxy_password = urlparse(proxy_url).password or ""
        secrets = (str(getattr(feed, "key", "") or ""), proxy_password)
        source_url, headers = self._build_request(feed)
        is_https = urlparse(source_url).scheme.lower() == "https"
        attempts: list[ProbeAttempt] = []

        strict_modes = [("direct_strict", "")]
        if proxy_url:
            strict_modes.append(("proxy_strict", proxy_url))
        strict_results: dict[str, ProbeAttempt] = {}
        for mode, mode_proxy in strict_modes:
            attempt = await self._attempt(
                mode,
                source_url,
                headers,
                mode_proxy,
                self._bounded_timeout(feed),
                True,
                username,
                secrets,
            )
            attempts.append(attempt)
            strict_results[mode] = attempt

        if is_https:
            relaxed_modes = [("direct_relaxed", "", "direct_strict")]
            if proxy_url:
                relaxed_modes.append(("proxy_relaxed", proxy_url, "proxy_strict"))
            for mode, mode_proxy, strict_mode in relaxed_modes:
                if full_check or not strict_results[strict_mode].ok:
                    attempts.append(
                        await self._attempt(
                            mode,
                            source_url,
                            headers,
                            mode_proxy,
                            self._bounded_timeout(feed),
                            False,
                            username,
                            secrets,
                        )
                    )

        return ProbeReport(
            feed_id=str(getattr(feed, "id", "") or ""),
            source_type=str(getattr(feed, "source_type", "rss") or "rss"),
            attempts=attempts,
            recommendation=self._recommend(attempts, is_https=is_https),
        )

    @staticmethod
    def _build_request(feed: FeedConfig) -> tuple[str, dict[str, str]]:
        if str(getattr(feed, "source_type", "rss") or "rss") == "twitter":
            return build_nitter_timeline_request(feed)
        return build_rss_request(feed)

    def _bounded_timeout(self, feed: FeedConfig) -> int:
        timeout = int(getattr(feed, "timeout", 10) or 10)
        return min(max(timeout, self.MIN_TIMEOUT_SECONDS), self.MAX_TIMEOUT_SECONDS)

    async def _attempt(
        self,
        mode: str,
        url: str,
        headers: dict[str, str],
        proxy_url: str,
        timeout: int,
        verify_ssl: bool,
        username: str,
        secrets: tuple[str, str],
    ) -> ProbeAttempt:
        started = self._clock()
        try:
            response = await asyncio.to_thread(
                self._requester,
                url=url,
                headers=dict(headers),
                proxy_url=proxy_url,
                timeout=timeout,
                verify_ssl=verify_ssl,
                max_bytes=self.MAX_RESPONSE_BYTES,
                max_redirects=5,
                use_environment_proxy=False,
            )
            if not 200 <= int(response.status) < 300:
                error_type, error_message, status = classify_probe_error(
                    HTTPError(
                        response.final_url,
                        int(response.status),
                        f"HTTP status {response.status}",
                        response.headers,
                        None,
                    ),
                    secrets=secrets,
                )
                return ProbeAttempt(
                    mode=mode,
                    ok=False,
                    http_status=status,
                    content_type=str(response.headers.get("content-type", "")),
                    latency_ms=max(0, round((self._clock() - started) * 1000)),
                    is_feed=False,
                    feed_kind="unknown",
                    truncated=bool(response.truncated),
                    error_type=error_type,
                    error_message=error_message,
                )
            feed_kind = self._recognize_content(
                response.body,
                username=username,
            )
            is_feed = feed_kind != "unknown"
            error_type = ""
            error_message = ""
            if not is_feed:
                error_type, error_message, _status = classify_probe_error(
                    InvalidFeedError("响应内容无法识别为订阅源"),
                    secrets=secrets,
                )
            return ProbeAttempt(
                mode=mode,
                ok=is_feed,
                http_status=int(response.status),
                content_type=str(response.headers.get("content-type", "")),
                latency_ms=max(0, round((self._clock() - started) * 1000)),
                is_feed=is_feed,
                feed_kind=feed_kind,
                truncated=bool(response.truncated),
                error_type=error_type,
                error_message=error_message,
            )
        except Exception as exc:
            error_type, error_message, status = classify_probe_error(
                exc,
                secrets=secrets,
            )
            return ProbeAttempt(
                mode=mode,
                ok=False,
                http_status=status,
                content_type="",
                latency_ms=max(0, round((self._clock() - started) * 1000)),
                is_feed=False,
                feed_kind="unknown",
                truncated=False,
                error_type=error_type,
                error_message=error_message,
            )

    @staticmethod
    def _recognize_content(body: bytes, *, username: str = "") -> str:
        text = body.decode("utf-8-sig", errors="ignore").lstrip()
        text = re.sub(
            r"^<\?xml\s+[^?]*\?>\s*",
            "",
            text,
            count=1,
            flags=re.IGNORECASE,
        )
        try:
            root = ElementTree.fromstring(text)
        except ElementTree.ParseError:
            root = None
        if root is not None and isinstance(root.tag, str):
            if root.tag.startswith("{") and "}" in root.tag:
                namespace, local_name = root.tag[1:].split("}", 1)
            else:
                namespace, local_name = "", root.tag
            local_name = local_name.lower()
            if local_name == "rss":
                return "rss"
            if local_name == "feed" and namespace == _ATOM_NAMESPACE:
                return "atom"
            if local_name == "rdf" and namespace == _RDF_NAMESPACE:
                return "rdf"

        nitter_parser = _NitterContentParser(username)
        try:
            nitter_parser.feed(text)
            nitter_parser.close()
        except Exception:
            return "unknown"
        if nitter_parser.matched:
            return "nitter"
        return "unknown"

    @staticmethod
    def _recommend(attempts: list[ProbeAttempt], *, is_https: bool) -> dict[str, Any]:
        successful = next((attempt for attempt in attempts if attempt.ok), None)
        if successful is not None:
            use_proxy = successful.mode.startswith("proxy_")
            if not is_https:
                return {
                    "code": successful.mode,
                    "verify_ssl": None,
                    "use_proxy": use_proxy,
                    "message": "HTTP 来源可用，TLS 不适用。",
                }
            if successful.mode == "direct_strict":
                message = "默认网络与严格证书校验可用。"
            elif successful.mode == "proxy_strict":
                message = "来源代理与严格证书校验可用。"
            elif successful.mode == "direct_relaxed":
                message = "默认网络仅在关闭证书校验时可用，存在证书安全隐患。"
            else:
                message = "来源代理仅在关闭证书校验时可用，存在证书安全隐患。"
            return {
                "code": successful.mode,
                "verify_ssl": successful.mode.endswith("strict"),
                "use_proxy": use_proxy,
                "message": message,
            }

        invalid_attempt = next(
            (attempt for attempt in attempts if attempt.error_type == "invalid_feed"),
            None,
        )
        if invalid_attempt is not None:
            return {
                "code": "invalid_feed",
                "verify_ssl": None,
                "use_proxy": None,
                "message": "来源可访问，但响应内容无法识别为 RSS、Atom、RDF 或 Nitter。",
            }

        priorities = {
            "tls_certificate": 0,
            "proxy": 1,
            "dns": 2,
            "connect": 3,
            "timeout": 4,
            "http_status": 5,
            "unknown": 6,
        }
        failure = min(
            attempts,
            key=lambda attempt: priorities.get(attempt.error_type, 99),
            default=None,
        )
        labels = {
            "tls_certificate": "TLS 证书校验失败",
            "proxy": "来源代理访问失败",
            "dns": "域名解析失败",
            "connect": "连接失败",
            "timeout": "访问超时",
            "http_status": "HTTP 状态异常",
            "unknown": "来源访问失败",
        }
        if failure is None:
            message = "来源无法访问。"
        else:
            label = labels.get(failure.error_type, "来源访问失败")
            detail = failure.error_message or label
            message = f"{label}：{detail}"[:500]
        return {
            "code": "unreachable",
            "verify_ssl": None,
            "use_proxy": None,
            "message": message,
        }
