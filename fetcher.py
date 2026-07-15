import asyncio
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
from urllib.request import getproxies

from astrbot.api import logger

from .config import RSSConfig
from .source_http import get_response_header, request_source, source_http_client
from .storage import FeedStorage
from .twitter_source import TwitterTimelineFetcher


@dataclass(slots=True)
class FetchedFeed:
    feed_id: str
    body: str
    etag: str
    last_modified: str
    status: int


class FeedFetcher:
    """抓取层：负责从远端源拉取原始 XML 数据。"""

    def __init__(self, config: RSSConfig, storage: FeedStorage) -> None:
        self._config = config
        self._storage = storage
        self._twitter_fetcher = TwitterTimelineFetcher()
        self._twitter_media_cache_dir = storage.plugin_cache_dir() / "twitter_media"
        self._relaxed_tls_warned_feed_ids: set[str] = set()

    async def fetch(self, job) -> list[dict[str, Any]]:
        feed_ids = list(getattr(job, "feed_ids", []) or [])
        job_id = str(getattr(job, "id", "") or "").strip()
        return await self.fetch_feed_ids(feed_ids, job_id=job_id)

    async def fetch_feed_ids(
        self,
        feed_ids: list[str],
        *,
        job_id: str = "",
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        feed_map = {feed.id: feed for feed in self._config.feeds if feed.enabled}
        for feed_id in feed_ids:
            feed = feed_map.get(feed_id)
            if feed is None:
                continue
            self._warn_if_relaxed_tls(feed)
            if getattr(feed, "source_type", "rss") == "twitter":
                fetched_twitter = await self._fetch_single_twitter_feed(feed)
                if fetched_twitter is None:
                    continue
                items.append(fetched_twitter)
                continue
            fetched = await self._fetch_single_feed(feed, job_id=job_id)
            if fetched is None:
                continue
            items.append(
                {
                    "feed_id": fetched.feed_id,
                    "body": fetched.body,
                    "etag": fetched.etag,
                    "last_modified": fetched.last_modified,
                    "status": fetched.status,
                    "max_new_items": int(getattr(feed, "max_new_items", 0) or 0),
                    "send_images": bool(getattr(feed, "send_images", True)),
                    "send_videos": bool(getattr(feed, "send_videos", True)),
                    "proxy_url": str(getattr(feed, "proxy_url", "") or "").strip(),
                }
            )
        return items

    async def _fetch_single_feed(self, feed, *, job_id: str = "") -> FetchedFeed | None:
        state = await self._storage.get_feed_state(feed.id)
        etag = str(state.get("etag", "")).strip()
        last_modified = str(state.get("last_modified", "")).strip()
        proxy_url = str(getattr(feed, "proxy_url", "") or "").strip()

        url, headers = self._build_url_and_headers(feed)
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified

        def _request_once():
            response = request_source(
                url=url,
                headers=headers,
                proxy_url=proxy_url,
                timeout=feed.timeout,
                verify_ssl=bool(getattr(feed, "verify_ssl", True)),
                max_bytes=None,
                max_redirects=None,
            )
            return FetchedFeed(
                feed_id=feed.id,
                body=response.body.decode("utf-8", errors="ignore"),
                etag=get_response_header(response.headers, "ETag").strip(),
                last_modified=str(
                    get_response_header(response.headers, "Last-Modified")
                ).strip(),
                status=int(response.status or 200),
            )

        try:
            return await asyncio.to_thread(_request_once)
        except Exception as exc:
            # urllib 对 304 也会抛异常，直接忽略
            if "304" in str(exc):
                logger.info("feed=%s not modified (304)", feed.id)
                return None
            logger.warning(
                "fetch job=%s feed=%s url=%s proxy=%s client=%s failed: %s",
                job_id or "-",
                feed.id,
                self._redact_url_for_log(url),
                self._proxy_state_for_log(proxy_url),
                source_http_client(proxy_url),
                self._exception_summary_for_log(exc),
            )
            return None

    async def _fetch_single_twitter_feed(self, feed) -> dict[str, Any] | None:
        state = await self._storage.get_feed_state(feed.id)
        fetched = await self._twitter_fetcher.fetch(
            feed,
            state,
            cache_dir=self._twitter_media_cache_dir,
        )
        if fetched is None:
            return None
        return {
            "feed_id": fetched.feed_id,
            "source_type": "twitter",
            "items": fetched.items,
            "since_id": fetched.since_id,
            "status": fetched.status,
        }

    @staticmethod
    def _build_url_and_headers(feed) -> tuple[str, dict[str, str]]:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0 Safari/537.36"
            ),
            "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml;q=0.9, */*;q=0.1",
            "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.7,zh;q=0.6",
        }
        url = feed.url

        if feed.auth_mode == "query" and feed.key:
            parsed = urlparse(url)
            q = dict(parse_qsl(parsed.query, keep_blank_values=True))
            q["key"] = feed.key
            url = urlunparse(parsed._replace(query=urlencode(q)))
        elif feed.auth_mode == "header" and feed.key:
            headers["Authorization"] = f"Bearer {feed.key}"

        return url, headers

    @staticmethod
    def _proxy_state_for_log(proxy_url: str) -> str:
        if str(proxy_url or "").strip():
            return "feed"
        return "system" if getproxies() else "off"

    def _warn_if_relaxed_tls(self, feed) -> None:
        if bool(getattr(feed, "verify_ssl", True)):
            return
        feed_id = str(getattr(feed, "id", "") or "")
        if feed_id in self._relaxed_tls_warned_feed_ids:
            return
        self._relaxed_tls_warned_feed_ids.add(feed_id)
        logger.warning(
            "feed=%s source TLS certificate verification is disabled",
            feed_id,
        )

    @staticmethod
    def _redact_url_for_log(url: str) -> str:
        parsed = urlparse(str(url or ""))
        query = "<redacted>" if parsed.query else ""
        netloc = parsed.netloc.rsplit("@", 1)[-1]
        return urlunparse(
            parsed._replace(netloc=netloc, query=query, fragment="")
        )

    @classmethod
    def _exception_summary_for_log(cls, exc: Exception) -> str:
        category = type(exc).__name__
        message = re.sub(
            r"(?im)\b(?:authorization|cookie)\s*[:=]\s*[^\r\n]*",
            "<redacted>",
            str(exc),
        )
        message = re.sub(
            r"(?i)\b[a-z][a-z0-9+.-]*://[^\s<>'\"]+",
            lambda match: cls._redact_url_for_log(match.group(0)),
            message,
        )
        message = " ".join(message.split())

        response = getattr(exc, "response", None)
        status_code = getattr(response, "status_code", None)
        status = f" HTTP {status_code};" if status_code is not None else ""
        summary = f"{category}:{status} {message}".strip()
        return summary if len(summary) <= 240 else f"{summary[:237]}..."
