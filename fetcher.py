import asyncio
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
from urllib.request import ProxyHandler, Request, build_opener

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
        return await self.fetch_feed_ids(feed_ids)

    async def fetch_feed_ids(self, feed_ids: list[str]) -> list[dict[str, Any]]:
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
            fetched = await self._fetch_single_feed(feed)
            if fetched is None:
                continue
            items.append(
                {
                    "feed_id": fetched.feed_id,
                    "body": fetched.body,
                    "etag": fetched.etag,
                    "last_modified": fetched.last_modified,
                    "status": fetched.status,
                }
            )
        return items

    async def _fetch_single_feed(self, feed) -> FetchedFeed | None:
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
            logger.warning("fetch feed=%s failed: %s", feed.id, exc)
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
