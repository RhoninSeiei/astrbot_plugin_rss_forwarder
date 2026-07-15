import asyncio
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
from urllib.request import ProxyHandler, Request, build_opener, getproxies

from astrbot.api import logger

from .config import RSSConfig
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
            if self._should_use_httpx(proxy_url):
                return self._request_with_httpx(
                    feed_id=feed.id,
                    url=url,
                    headers=headers,
                    proxy_url=proxy_url,
                    timeout=feed.timeout,
                )

            req = Request(url=url, headers=headers)
            opener = (
                build_opener(ProxyHandler({"http": proxy_url, "https": proxy_url}))
                if proxy_url
                else build_opener()
            )
            with opener.open(req, timeout=feed.timeout) as resp:  # noqa: S310
                body = resp.read().decode("utf-8", errors="ignore")
                return FetchedFeed(
                    feed_id=feed.id,
                    body=body,
                    etag=str(resp.headers.get("ETag", "")).strip(),
                    last_modified=str(resp.headers.get("Last-Modified", "")).strip(),
                    status=int(getattr(resp, "status", 200) or 200),
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
                "httpx" if self._should_use_httpx(proxy_url) else "urllib",
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
    def _request_with_httpx(
        *,
        feed_id: str,
        url: str,
        headers: dict[str, str],
        proxy_url: str,
        timeout: int,
    ) -> FetchedFeed:
        import httpx

        kwargs: dict[str, Any] = {
            "headers": headers,
            "timeout": timeout,
            "follow_redirects": True,
        }
        if proxy_url:
            kwargs["proxy"] = proxy_url
        with httpx.Client(**kwargs) as client:
            response = client.get(url)
            response.raise_for_status()
            return FetchedFeed(
                feed_id=feed_id,
                body=response.text,
                etag=str(response.headers.get("ETag", "")).strip(),
                last_modified=str(response.headers.get("Last-Modified", "")).strip(),
                status=int(response.status_code or 200),
            )

    @staticmethod
    def _should_use_httpx(proxy_url: str) -> bool:
        return str(proxy_url or "").strip().lower().startswith(
            ("socks://", "socks5://", "socks5h://", "socks4://")
        )

    @staticmethod
    def _proxy_state_for_log(proxy_url: str) -> str:
        if str(proxy_url or "").strip():
            return "feed"
        return "system" if getproxies() else "off"

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
