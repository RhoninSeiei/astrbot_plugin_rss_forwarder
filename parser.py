from __future__ import annotations

import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from hashlib import sha256
from html import unescape
from typing import Any
from urllib.parse import urlparse

try:
    from defusedxml import ElementTree as ET
except ImportError:  # pragma: no cover - fallback for environments without defusedxml.
    from xml.etree import ElementTree as ET

from astrbot.api import logger


class FeedParser:
    """解析层：将 RSS/Atom 转换为统一条目结构。"""

    _IMG_SRC_RE = re.compile(r"<img\b[^>]*\bsrc=['\"]([^'\"]+)['\"]", re.IGNORECASE)

    def parse(self, raw_items: list[dict], job=None) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        for raw in raw_items:
            feed_id = str(raw.get("feed_id", "")).strip()
            if str(raw.get("source_type", "")).strip().lower() == "twitter":
                parsed_twitter = self._parse_twitter(feed_id, raw)
                for item in parsed_twitter:
                    if job is not None:
                        item.setdefault("job_id", getattr(job, "id", ""))
                    entries.append(item)
                continue
            body = str(raw.get("body", "") or "")
            if not body:
                continue
            try:
                parsed = self._parse_xml(feed_id, body)
                for item in parsed:
                    if job is not None:
                        item.setdefault("job_id", getattr(job, "id", ""))
                    entries.append(item)
            except Exception as exc:
                logger.warning("parse feed=%s failed: %s", feed_id, exc)
        return entries

    def _parse_twitter(self, feed_id: str, raw: dict[str, Any]) -> list[dict[str, Any]]:
        feed_title = str(raw.get("feed_title", "") or "").strip()
        result: list[dict[str, Any]] = []
        raw_items = raw.get("items", []) or []
        if not isinstance(raw_items, list):
            return []

        for item in raw_items:
            if not isinstance(item, dict):
                continue
            username = str(item.get("username", "") or "").strip().lstrip("@")
            tweet_id = str(item.get("tweet_id", "") or "").strip()
            if not username or not tweet_id:
                continue
            screen_name = str(item.get("screen_name", "") or "").strip() or username
            images = self._normalize_string_list(item.get("images", []))
            videos = self._normalize_string_list(item.get("videos", []))
            image_paths = self._normalize_string_list(item.get("image_paths", []))
            video_paths = self._normalize_string_list(item.get("video_paths", []))
            text = str(item.get("text", "") or "").strip()
            source = feed_title or f"Twitter @{username}"
            title = f"@{username}"
            if screen_name and screen_name != username:
                title = f"@{username} ({screen_name})"
            link = str(item.get("link", "") or "").strip() or f"https://x.com/{username}/status/{tweet_id}"
            result.append(
                {
                    "feed_id": feed_id,
                    "feed_title": source,
                    "source_type": "twitter",
                    "title": title,
                    "link": link,
                    "guid": f"twitter:{username}:{tweet_id}",
                    "summary": text,
                    "published_at": str(item.get("published_at", "") or "").strip(),
                    "source": source,
                    "image_url": images[0] if images else "",
                    "image_urls": images,
                    "video_urls": videos,
                    "image_paths": image_paths,
                    "video_paths": video_paths,
                    "username": username,
                    "tweet_id": tweet_id,
                    "screen_name": screen_name,
                    "nitter_link": str(item.get("nitter_link", "") or "").strip(),
                    "is_r18": bool(item.get("is_r18", False)),
                    "send_link": bool(item.get("send_link", True)),
                }
            )
        return result

    def _parse_xml(self, feed_id: str, xml_text: str) -> list[dict[str, Any]]:
        root = ET.fromstring(xml_text)
        tag = self._strip_ns(root.tag)
        if tag == "rss":
            return self._parse_rss(feed_id, root)
        if tag == "feed":
            return self._parse_atom(feed_id, root)
        return []

    def _parse_rss(self, feed_id: str, root) -> list[dict[str, Any]]:
        channel = root.find("channel")
        if channel is None:
            return []
        feed_title = self._text(channel.find("title"))
        result: list[dict[str, Any]] = []
        for item in channel.findall("item"):
            title = self._text(item.find("title"))
            link = self._text(item.find("link"))
            guid = self._text(item.find("guid"))
            summary = self._text(item.find("description"))
            published = self._normalize_time(self._text(item.find("pubDate")))
            image_url = self._extract_rss_image_url(item, summary)
            item_id = guid or link or sha256(f"{title}|{published}".encode("utf-8")).hexdigest()
            result.append(
                {
                    "feed_id": feed_id,
                    "feed_title": feed_title,
                    "title": title,
                    "link": link,
                    "guid": item_id,
                    "summary": summary,
                    "published_at": published,
                    "source": feed_title,
                    "image_url": image_url,
                }
            )
        return result

    def _parse_atom(self, feed_id: str, root) -> list[dict[str, Any]]:
        ns = self._namespace(root.tag)
        feed_title = self._text(root.find(self._tag(ns, "title")))
        result: list[dict[str, Any]] = []
        for entry in root.findall(self._tag(ns, "entry")):
            title = self._text(entry.find(self._tag(ns, "title")))
            id_text = self._text(entry.find(self._tag(ns, "id")))
            summary = self._text(entry.find(self._tag(ns, "summary"))) or self._text(
                entry.find(self._tag(ns, "content"))
            )
            published = self._normalize_time(
                self._text(entry.find(self._tag(ns, "published")))
                or self._text(entry.find(self._tag(ns, "updated")))
            )
            link = ""
            image_url = ""
            for link_node in entry.findall(self._tag(ns, "link")):
                href = (link_node.attrib.get("href") or "").strip()
                rel = (link_node.attrib.get("rel") or "alternate").strip().lower()
                link_type = (link_node.attrib.get("type") or "").strip().lower()
                if href and rel in {"alternate", ""} and not link:
                    link = href
                if href and rel == "enclosure" and link_type.startswith("image/") and not image_url:
                    image_url = href

            if not image_url:
                for child in entry.iter():
                    if child is entry:
                        continue
                    local = self._strip_ns(child.tag).lower()
                    if local in {"content", "thumbnail", "image"}:
                        url = (child.attrib.get("url") or child.attrib.get("href") or "").strip()
                        if self._is_http_url(url):
                            image_url = url
                            break

            if not image_url:
                image_url = self._extract_image_from_html(summary)

            item_id = id_text or link or sha256(f"{title}|{published}".encode("utf-8")).hexdigest()
            result.append(
                {
                    "feed_id": feed_id,
                    "feed_title": feed_title,
                    "title": title,
                    "link": link,
                    "guid": item_id,
                    "summary": summary,
                    "published_at": published,
                    "source": feed_title,
                    "image_url": image_url,
                }
            )
        return result

    def _extract_rss_image_url(self, item, summary: str) -> str:
        enclosure = item.find("enclosure")
        if enclosure is not None:
            url = (enclosure.attrib.get("url") or "").strip()
            mime = (enclosure.attrib.get("type") or "").strip().lower()
            if self._is_http_url(url) and (not mime or mime.startswith("image/")):
                return url

        for child in item:
            local = self._strip_ns(child.tag).lower()
            if local in {"enclosure", "content", "thumbnail", "image"}:
                url = (child.attrib.get("url") or child.attrib.get("href") or "").strip()
                mime = (child.attrib.get("type") or "").strip().lower()
                if self._is_http_url(url) and (local != "enclosure" or not mime or mime.startswith("image/")):
                    return url

        return self._extract_image_from_html(summary)

    def _extract_image_from_html(self, html_text: str) -> str:
        if not html_text:
            return ""
        match = self._IMG_SRC_RE.search(html_text)
        if not match:
            return ""
        src = unescape((match.group(1) or "").strip())
        if self._is_http_url(src):
            return src
        return ""

    @staticmethod
    def _is_http_url(url: str) -> bool:
        if not url:
            return False
        parsed = urlparse(url)
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)

    @staticmethod
    def _normalize_string_list(value) -> list[str]:
        if not isinstance(value, list):
            return []
        result: list[str] = []
        for item in value:
            text = str(item or "").strip()
            if text:
                result.append(text)
        return result

    @staticmethod
    def _strip_ns(tag: str) -> str:
        return tag.split("}", 1)[-1] if "}" in tag else tag

    @staticmethod
    def _namespace(tag: str) -> str:
        if tag.startswith("{") and "}" in tag:
            return tag[1 : tag.index("}")]
        return ""

    @staticmethod
    def _tag(ns: str, name: str) -> str:
        return f"{{{ns}}}{name}" if ns else name

    @staticmethod
    def _text(node) -> str:
        if node is None:
            return ""
        return "".join(node.itertext()).strip()

    @staticmethod
    def _normalize_time(raw: str) -> str:
        text = str(raw or "").strip()
        if not text:
            return ""
        try:
            dt = parsedate_to_datetime(text)
        except Exception:
            try:
                dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except Exception:
                return text
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
