from __future__ import annotations

import asyncio
import hashlib
import mimetypes
import time
import re
from dataclasses import dataclass
from html import unescape
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urljoin, urlparse
from urllib.request import ProxyHandler, Request, build_opener

from astrbot.api import logger


@dataclass(slots=True)
class TwitterFetchResult:
    feed_id: str
    items: list[dict[str, Any]]
    since_id: str
    status: int = 200


class TwitterTimelineFetcher:
    """通过 Nitter HTML 拉取 Twitter 用户时间线并转换为原始推文记录。"""

    MEDIA_CACHE_TTL_SECONDS = 24 * 60 * 60
    MEDIA_CACHE_MAX_BYTES = 512 * 1024 * 1024
    MEDIA_MAX_FILE_BYTES = 64 * 1024 * 1024

    _STATUS_RE_TEMPLATE = r"/{username}/status/(\d+)"
    _TAG_RE = re.compile(r"<[^>]+>")
    _SPACE_RE = re.compile(r"\s+")
    _CLASS_ATTR_RE = re.compile(r"class=[\"']([^\"']*)[\"']", re.IGNORECASE)
    _HREF_RE = re.compile(r"href=[\"']([^\"']+)[\"']", re.IGNORECASE)
    _SRC_RE = re.compile(r"src=[\"']([^\"']+)[\"']", re.IGNORECASE)
    _DATA_URL_RE = re.compile(r"data-url=[\"']([^\"']+)[\"']", re.IGNORECASE)

    def __init__(self, default_nitter_url: str = "https://nitter.net") -> None:
        self._default_nitter_url = default_nitter_url.rstrip("/")

    async def fetch(
        self,
        feed,
        state: dict[str, Any],
        *,
        cache_dir: Path | None = None,
    ) -> TwitterFetchResult | None:
        username = str(getattr(feed, "username", "") or "").strip().lstrip("@")
        if not username:
            return None

        since_id = str(state.get("since_id", "") or "").strip()
        base_url = self._resolve_nitter_url(feed)
        proxy_url = str(getattr(feed, "proxy_url", "") or "").strip()
        timeout = max(int(getattr(feed, "timeout", 10) or 10), 1)
        max_new_items = max(int(getattr(feed, "max_new_items", 1) or 0), 0)

        try:
            timeline_html = await asyncio.to_thread(
                self._open_text,
                f"{base_url}/{username}",
                proxy_url,
                timeout,
            )
        except Exception as exc:
            logger.warning("fetch twitter feed=%s timeline failed: %s", feed.id, exc)
            return None

        timeline_ids = self._extract_timeline_ids(timeline_html, username)
        if not timeline_ids:
            return TwitterFetchResult(feed_id=feed.id, items=[], since_id=since_id)

        latest_id = timeline_ids[0]
        new_ids = self._select_new_ids(timeline_ids, since_id)
        if max_new_items > 0:
            new_ids = new_ids[:max_new_items]
        if not since_id:
            return TwitterFetchResult(feed_id=feed.id, items=[], since_id=latest_id)

        items: list[dict[str, Any]] = []
        advanced_since_id = since_id
        for tweet_id in reversed(new_ids):
            try:
                detail_html = await asyncio.to_thread(
                    self._open_text,
                    f"{base_url}/{username}/status/{tweet_id}",
                    proxy_url,
                    timeout,
                )
                item = self._parse_tweet_detail(feed, base_url, username, tweet_id, detail_html)
                if cache_dir is not None:
                    await self._cache_item_media(
                        item,
                        cache_dir=cache_dir,
                        proxy_url=proxy_url,
                        timeout=timeout,
                    )
                items.append(item)
                advanced_since_id = tweet_id
            except Exception as exc:
                logger.warning(
                    "fetch twitter feed=%s tweet=%s failed: %s",
                    feed.id,
                    tweet_id,
                    exc,
                )
                if self._is_permanent_detail_failure(exc):
                    advanced_since_id = tweet_id
                    continue
                break

        return TwitterFetchResult(
            feed_id=feed.id,
            items=items,
            since_id=advanced_since_id,
        )

    @staticmethod
    def _is_permanent_detail_failure(exc: Exception) -> bool:
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        return status_code in {404, 410}

    def _resolve_nitter_url(self, feed) -> str:
        configured = (
            str(getattr(feed, "nitter_url", "") or "").strip()
            or str(getattr(feed, "url", "") or "").strip()
            or self._default_nitter_url
        )
        return configured.rstrip("/")

    @staticmethod
    def _open_text(url: str, proxy_url: str, timeout: int) -> str:
        if TwitterTimelineFetcher._should_use_httpx(proxy_url):
            return TwitterTimelineFetcher._open_text_with_httpx(url, proxy_url, timeout)

        headers = {
            "User-Agent": "astrbot_plugin_rss_forwarder/0.5.2 (+https://github.com/RhoninSeiei/astrbot_plugin_rss_forwarder)",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7,ja;q=0.6",
        }
        opener = (
            build_opener(ProxyHandler({"http": proxy_url, "https": proxy_url}))
            if proxy_url
            else build_opener()
        )
        request = Request(url=url, headers=headers)
        with opener.open(request, timeout=timeout) as response:  # noqa: S310
            return response.read().decode("utf-8", errors="ignore")

    @staticmethod
    def _open_text_with_httpx(url: str, proxy_url: str, timeout: int) -> str:
        import httpx

        headers = {
            "User-Agent": "astrbot_plugin_rss_forwarder/0.5.0 (+https://github.com/RhoninSeiei/astrbot_plugin_rss_forwarder)",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7,ja;q=0.6",
        }
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
            return response.text

    @staticmethod
    def _should_use_httpx(proxy_url: str) -> bool:
        if str(proxy_url or "").strip().lower().startswith(("socks://", "socks5://", "socks5h://", "socks4://")):
            return True
        try:
            import httpx  # noqa: F401
        except Exception:
            return False
        return False

    def _extract_timeline_ids(self, html: str, username: str) -> list[str]:
        pattern = re.compile(
            self._STATUS_RE_TEMPLATE.format(username=re.escape(username)),
            re.IGNORECASE,
        )
        seen: set[str] = set()
        ids: list[str] = []

        blocks = self._extract_elements_by_class(html, "div", "timeline-item")
        candidates = blocks or [html]
        for block in candidates:
            if self._has_class(block, "pinned") or self._has_class(block, "icon-pin"):
                continue
            match = None
            for href_match in self._HREF_RE.finditer(block):
                match = pattern.search(unescape(href_match.group(1)))
                if match:
                    break
            if match is None and not blocks:
                match = pattern.search(block)
            if match is None:
                continue
            tweet_id = str(match.group(1) or "").strip()
            if not tweet_id or tweet_id in seen:
                continue
            seen.add(tweet_id)
            ids.append(tweet_id)
        return ids

    def _select_new_ids(self, timeline_ids: list[str], since_id: str) -> list[str]:
        if not since_id:
            return []

        selected: list[str] = []
        for tweet_id in timeline_ids:
            if self._tweet_id_is_newer(tweet_id, since_id):
                selected.append(tweet_id)
                continue
            break
        return selected

    @staticmethod
    def _tweet_id_is_newer(tweet_id: str, since_id: str) -> bool:
        try:
            return int(tweet_id) > int(since_id)
        except ValueError:
            return tweet_id > since_id

    def _parse_tweet_detail(
        self,
        feed,
        base_url: str,
        username: str,
        tweet_id: str,
        html: str,
    ) -> dict[str, Any]:
        main = self._extract_element_by_class(html, "div", "main-tweet") or html
        screen_name = self._clean_html(
            self._extract_element_by_class(main, "a", "fullname")
        ) or username
        content_html = self._extract_element_by_class(main, "div", "tweet-content")
        text = self._clean_html(content_html)

        image_urls = [
            self._absolute_url(base_url, src)
            for src in self._extract_still_image_sources(main)
            if src
        ]
        video_urls = [
            self._absolute_url(base_url, src)
            for src in self._extract_video_sources(main)
            if src
        ]

        quote = None
        quote_html = self._extract_element_by_class(main, "div", "quote")
        if quote_html:
            quote_author = self._clean_html(
                self._extract_element_by_class(quote_html, "a", "fullname")
            )
            quote_text = self._clean_html(
                self._extract_element_by_class(quote_html, "div", "tweet-content")
            )
            if quote_author or quote_text:
                quote = {"author": quote_author, "text": quote_text}

        if quote:
            quote_line = f"引用 @{quote.get('author', '')}: {quote.get('text', '')}".strip()
            text = f"{text}\n{quote_line}".strip() if text else quote_line

        return {
            "feed_id": str(getattr(feed, "id", "") or "").strip(),
            "source_type": "twitter",
            "tweet_id": tweet_id,
            "username": username,
            "screen_name": screen_name,
            "text": text,
            "images": image_urls if bool(getattr(feed, "send_images", True)) else [],
            "videos": video_urls if bool(getattr(feed, "send_videos", True)) else [],
            "all_images": image_urls,
            "all_videos": video_urls,
            "send_images": bool(getattr(feed, "send_images", True)),
            "send_videos": bool(getattr(feed, "send_videos", True)),
            "send_link": bool(getattr(feed, "send_link", True)),
            "quote": quote,
            "is_r18": self._has_class(main, "nsfw"),
            "link": f"https://x.com/{username}/status/{tweet_id}",
            "nitter_link": f"{base_url}/{username}/status/{tweet_id}",
            "published_at": "",
        }

    async def _cache_item_media(
        self,
        item: dict[str, Any],
        *,
        cache_dir: Path,
        proxy_url: str,
        timeout: int,
    ) -> None:
        cache_dir.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(self._cleanup_media_cache, cache_dir)

        image_paths: list[str] = []
        for url in item.get("images", []) or []:
            cached = await asyncio.to_thread(
                self._cache_media_url,
                str(url),
                cache_dir,
                proxy_url,
                timeout,
                "image",
            )
            if cached:
                image_paths.append(str(cached))

        video_paths: list[str] = []
        for url in item.get("videos", []) or []:
            cached = await asyncio.to_thread(
                self._cache_media_url,
                str(url),
                cache_dir,
                proxy_url,
                timeout,
                "video",
            )
            if cached:
                video_paths.append(str(cached))

        item["image_paths"] = image_paths
        item["video_paths"] = video_paths

    def _cache_media_url(
        self,
        url: str,
        cache_dir: Path,
        proxy_url: str,
        timeout: int,
        media_kind: str,
    ) -> Path | None:
        media_url = str(url or "").strip()
        if not media_url.startswith(("http://", "https://")):
            return None

        cache_key = hashlib.sha256(media_url.encode("utf-8")).hexdigest()
        for existing in sorted(cache_dir.glob(f"{cache_key}.*")):
            if existing.is_file() and not existing.name.endswith(".tmp"):
                try:
                    existing.touch()
                except OSError:
                    pass
                return existing

        headers = {
            "User-Agent": "astrbot_plugin_rss_forwarder/0.5.0 (+https://github.com/RhoninSeiei/astrbot_plugin_rss_forwarder)",
            "Accept": "*/*",
        }
        if self._should_use_httpx(proxy_url):
            return self._cache_media_url_with_httpx(
                media_url,
                cache_dir,
                proxy_url,
                timeout,
                media_kind,
                cache_key,
            )
        opener = (
            build_opener(ProxyHandler({"http": proxy_url, "https": proxy_url}))
            if proxy_url
            else build_opener()
        )
        request = Request(url=media_url, headers=headers)
        with opener.open(request, timeout=timeout) as response:  # noqa: S310
            content_type = str(response.headers.get("Content-Type", "") or "")
            content_length = str(response.headers.get("Content-Length", "") or "").strip()
            if content_length:
                try:
                    if int(content_length) > self.MEDIA_MAX_FILE_BYTES:
                        return None
                except ValueError:
                    pass
            data = response.read(self.MEDIA_MAX_FILE_BYTES + 1)

        if not data or len(data) > self.MEDIA_MAX_FILE_BYTES:
            return None

        ext = self._guess_media_extension(media_url, content_type, media_kind)
        target = cache_dir / f"{cache_key}{ext}"
        temp_target = target.with_name(f"{target.name}.tmp")
        temp_target.write_bytes(data)
        temp_target.replace(target)
        return target

    def _cache_media_url_with_httpx(
        self,
        media_url: str,
        cache_dir: Path,
        proxy_url: str,
        timeout: int,
        media_kind: str,
        cache_key: str,
    ) -> Path | None:
        import httpx

        headers = {
            "User-Agent": "astrbot_plugin_rss_forwarder/0.5.0 (+https://github.com/RhoninSeiei/astrbot_plugin_rss_forwarder)",
            "Accept": "*/*",
        }
        kwargs: dict[str, Any] = {
            "headers": headers,
            "timeout": timeout,
            "follow_redirects": True,
        }
        if proxy_url:
            kwargs["proxy"] = proxy_url
        with httpx.Client(**kwargs) as client:
            with client.stream("GET", media_url) as response:
                response.raise_for_status()
                content_type = str(response.headers.get("Content-Type", "") or "")
                content_length = str(response.headers.get("Content-Length", "") or "").strip()
                if content_length:
                    try:
                        if int(content_length) > self.MEDIA_MAX_FILE_BYTES:
                            return None
                    except ValueError:
                        pass
                chunks: list[bytes] = []
                total = 0
                for chunk in response.iter_bytes():
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > self.MEDIA_MAX_FILE_BYTES:
                        return None
                    chunks.append(chunk)

        data = b"".join(chunks)
        if not data:
            return None
        ext = self._guess_media_extension(media_url, content_type, media_kind)
        target = cache_dir / f"{cache_key}{ext}"
        temp_target = target.with_name(f"{target.name}.tmp")
        temp_target.write_bytes(data)
        temp_target.replace(target)
        return target

    def _cleanup_media_cache(self, cache_dir: Path) -> None:
        if not cache_dir.exists():
            return
        now = time.time()
        files: list[tuple[float, int, Path]] = []
        total_size = 0
        for path in cache_dir.iterdir():
            if not path.is_file() or path.name.endswith(".tmp"):
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            if stat.st_mtime < now - self.MEDIA_CACHE_TTL_SECONDS:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
                continue
            total_size += stat.st_size
            files.append((stat.st_mtime, stat.st_size, path))

        if total_size <= self.MEDIA_CACHE_MAX_BYTES:
            return

        for _mtime, size, path in sorted(files):
            try:
                path.unlink(missing_ok=True)
                total_size -= size
            except OSError:
                pass
            if total_size <= self.MEDIA_CACHE_MAX_BYTES:
                break

    @staticmethod
    def _guess_media_extension(url: str, content_type: str, media_kind: str) -> str:
        normalized_type = str(content_type or "").split(";", 1)[0].strip().lower()
        if normalized_type:
            guessed = mimetypes.guess_extension(normalized_type)
            if guessed:
                return guessed

        filename = unquote(urlparse(url).path.rsplit("/", 1)[-1])
        ext = Path(filename).suffix.lower()
        if ext:
            return ext
        return ".mp4" if media_kind == "video" else ".jpg"

    def _extract_still_image_sources(self, html: str) -> list[str]:
        sources: list[str] = []
        for block in self._extract_elements_by_class(html, "a", "still-image"):
            match = self._SRC_RE.search(block)
            if match:
                sources.append(unescape(match.group(1)))
        return self._dedupe(sources)

    def _extract_video_sources(self, html: str) -> list[str]:
        sources: list[str] = []
        for block in self._extract_elements_by_class(html, "video"):
            sources.extend(unescape(match.group(1)) for match in self._SRC_RE.finditer(block))
            sources.extend(unescape(match.group(1)) for match in self._DATA_URL_RE.finditer(block))
        return self._dedupe(sources)

    def _extract_element_by_class(self, html: str, tag: str, class_name: str) -> str:
        elements = self._extract_elements_by_class(html, tag, class_name, limit=1)
        return elements[0] if elements else ""

    def _extract_elements_by_class(
        self,
        html: str,
        tag: str,
        class_name: str | None = None,
        *,
        limit: int = 0,
    ) -> list[str]:
        tag_name = re.escape(tag)
        start_re = re.compile(rf"<{tag_name}\b[^>]*>", re.IGNORECASE)
        end_re = re.compile(rf"</{tag_name}>", re.IGNORECASE)
        results: list[str] = []
        pos = 0

        while True:
            start = start_re.search(html, pos)
            if not start:
                break
            start_tag = start.group(0)
            if class_name and not self._start_tag_has_class(start_tag, class_name):
                pos = start.end()
                continue

            depth = 1
            cursor = start.end()
            while depth > 0:
                next_start = start_re.search(html, cursor)
                next_end = end_re.search(html, cursor)
                if not next_end:
                    cursor = len(html)
                    break
                if next_start and next_start.start() < next_end.start():
                    depth += 1
                    cursor = next_start.end()
                else:
                    depth -= 1
                    cursor = next_end.end()

            results.append(html[start.start() : cursor])
            pos = cursor
            if limit > 0 and len(results) >= limit:
                break

        return results

    def _start_tag_has_class(self, start_tag: str, class_name: str) -> bool:
        match = self._CLASS_ATTR_RE.search(start_tag)
        if not match:
            return False
        classes = {part.strip() for part in match.group(1).split() if part.strip()}
        return class_name in classes

    def _has_class(self, html: str, class_name: str) -> bool:
        return any(
            class_name in {part.strip() for part in match.group(1).split() if part.strip()}
            for match in self._CLASS_ATTR_RE.finditer(html)
        )

    def _clean_html(self, html: str) -> str:
        if not html:
            return ""
        text = self._TAG_RE.sub(" ", html)
        text = unescape(text)
        return self._SPACE_RE.sub(" ", text).strip()

    @staticmethod
    def _absolute_url(base_url: str, url: str) -> str:
        text = str(url or "").strip()
        if not text:
            return ""
        if text.startswith(("http://", "https://")):
            return text
        return urljoin(f"{base_url}/", text.lstrip("/"))

    @staticmethod
    def _dedupe(values: list[str]) -> list[str]:
        seen: set[str] = set()
        result: list[str] = []
        for value in values:
            text = str(value or "").strip()
            if not text or text in seen:
                continue
            seen.add(text)
            result.append(text)
        return result
