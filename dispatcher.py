import asyncio
import hashlib
import inspect
import json
from dataclasses import dataclass
from datetime import datetime
from html import escape
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen

from astrbot.api import logger

from .config import RSSConfig


@dataclass(slots=True)
class DispatchResult:
    success_count: int = 0
    permanent_failure_count: int = 0
    transient_failure_count: int = 0
    skipped_disabled_count: int = 0
    skipped_duplicate_count: int = 0


class FeedDispatcher:
    """分发层：负责把新内容推送到目标会话/渠道。"""

    _PENDING_DISPATCH_TTL_SECONDS = 120
    _IMAGE_HASH_TIMEOUT_SECONDS = 8
    _IMAGE_HASH_MAX_BYTES = 8 * 1024 * 1024

    def __init__(self, context, config: RSSConfig, storage=None, renderer=None) -> None:
        self.context = context
        self._renderer = renderer or context
        self._config = config
        self._storage = storage
        self._target_map = {
            target.id: target
            for target in config.targets
            if target.enabled and target.unified_msg_origin
        }
        self._job_target_origins = self._build_job_target_map(config)
        self._disabled_origins: set[str] = set()

    def _build_job_target_map(self, config: RSSConfig) -> dict[str, list[str]]:
        mapping: dict[str, list[str]] = {}
        for job in config.jobs:
            if not job.enabled:
                continue
            origins = [
                self._target_map[target_id].unified_msg_origin
                for target_id in job.target_ids
                if target_id in self._target_map
            ]
            if origins:
                mapping[job.id] = origins
        return mapping

    def _resolve_origins(self, item: dict[str, Any]) -> list[str]:
        origins: set[str] = set()

        job_ids = item.get("job_ids") or []
        if isinstance(job_ids, str):
            job_ids = [job_ids]
        for job_id in job_ids:
            origins.update(self._job_target_origins.get(str(job_id), []))

        job_id = str(item.get("job_id", "")).strip()
        if job_id:
            origins.update(self._job_target_origins.get(job_id, []))

        feed_id = str(item.get("feed_id", "")).strip()
        if feed_id:
            for job in self._config.jobs:
                if job.enabled and feed_id in job.feed_ids:
                    origins.update(self._job_target_origins.get(job.id, []))

        if not origins:
            for origin_list in self._job_target_origins.values():
                origins.update(origin_list)
        return sorted(origins)

    def _resolve_target_origins(self, target_ids: list[str]) -> list[str]:
        origins = {
            self._target_map[target_id].unified_msg_origin
            for target_id in target_ids
            if target_id in self._target_map
        }
        return sorted(origin for origin in origins if origin)

    def _format_time(self, item: dict[str, Any]) -> str:
        raw_time = (
            item.get("published")
            or item.get("published_at")
            or item.get("pub_date")
            or item.get("updated")
            or item.get("time")
            or ""
        )
        time_text = str(raw_time).strip()
        if time_text:
            return time_text
        return datetime.now().strftime("%Y-%m-%d %H:%M")

    def _truncate_summary(self, item: dict[str, Any]) -> tuple[str, bool]:
        summary = str(item.get("summary", "") or item.get("content", "")).strip()
        if not summary:
            return "", False

        max_chars = self._config.summary_max_chars
        if len(summary) <= max_chars:
            return summary, False

        truncated = summary[: max_chars - 1].rstrip()
        return f"{truncated}…", True

    def _build_render_data(self, item: dict[str, Any]) -> dict[str, str]:
        title = str(item.get("title", "")).strip() or "(无标题)"
        source = str(item.get("source", "") or item.get("feed_title", "")).strip() or "未知来源"
        published_at = self._format_time(item)
        summary, truncated = self._truncate_summary(item)
        link = str(item.get("link", "")).strip() if self._should_display_link(item) else ""

        return {
            "title": title,
            "source": source,
            "published_at": published_at,
            "summary": summary,
            "link": link,
            "truncated": "1" if truncated else "0",
        }

    @staticmethod
    def _normalize_text(value: Any) -> str:
        text = str(value or "").strip()
        return " ".join(text.split())

    def _should_display_source(self, item: dict[str, Any] | None = None) -> bool:
        return bool(getattr(self._config, "display_source", True))

    def _should_display_time(self, item: dict[str, Any] | None = None) -> bool:
        return bool(getattr(self._config, "display_time", True))

    def _should_display_link(self, item: dict[str, Any] | None = None) -> bool:
        if not bool(getattr(self._config, "display_link", True)):
            return False
        if item and str(item.get("source_type", "") or "").strip().lower() == "twitter":
            return bool(item.get("send_link", True))
        return True

    @staticmethod
    def _is_compact_item(item: dict[str, Any] | None = None) -> bool:
        return bool(item and item.get("compact_mode_enabled", False))

    @staticmethod
    def _normalize_url(url: str) -> str:
        text = str(url or "").strip()
        if not text:
            return ""
        parsed = urlsplit(text)
        if not any((parsed.scheme, parsed.netloc, parsed.path, parsed.query, parsed.fragment)):
            return text
        return urlunsplit(
            (
                parsed.scheme.lower(),
                parsed.netloc.lower(),
                parsed.path,
                parsed.query,
                "",
            )
        )

    async def _build_dispatch_fingerprint(self, item: dict[str, Any], origin: str) -> str:
        source_title = item.get("_source_title", "")
        source_summary = item.get("_source_summary", "")
        title = self._normalize_text(source_title or item.get("title", ""))
        summary = self._normalize_text(source_summary or item.get("summary", "") or item.get("content", ""))
        payload = {
            "origin": str(origin or "").strip(),
            "guid": self._normalize_text(item.get("guid", "") or item.get("id", "")),
            "link": self._normalize_url(str(item.get("link", "") or "")),
            "title": title,
            "published_at": self._normalize_text(item.get("published_at", "") or item.get("published", "")),
            "summary_sha256": (
                hashlib.sha256(summary.encode("utf-8")).hexdigest() if summary else ""
            ),
            "render_mode": str(self._config.render_mode or "text").strip(),
        }
        image_paths = self._item_image_paths(item)
        if image_paths:
            local_hashes = []
            for image_path in image_paths:
                image_digest = await self._hash_local_file(image_path)
                if image_digest:
                    local_hashes.append(image_digest)
            if local_hashes:
                payload["image_file_sha256"] = local_hashes
        has_local_image_hash = bool(payload.get("image_file_sha256"))
        image_url = "" if has_local_image_hash else str(item.get("image_url", "") or "").strip()
        if image_url:
            image_digest = await self._hash_image_bytes(image_url)
            if image_digest:
                payload["image_sha256"] = image_digest
            else:
                payload["image_url"] = self._normalize_url(image_url)
        image_urls = [] if has_local_image_hash else self._item_image_urls(item)
        if image_urls:
            payload["image_urls"] = [self._normalize_url(url) for url in image_urls]
        video_paths = self._item_video_paths(item)
        if video_paths:
            payload["video_paths"] = [hashlib.sha256(path.encode("utf-8")).hexdigest() for path in video_paths]
        video_urls = [] if video_paths else self._item_video_urls(item)
        if video_urls:
            payload["video_urls"] = [self._normalize_url(url) for url in video_urls]
        encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    async def _build_daily_digest_fingerprint(self, digest: dict[str, Any], origin: str) -> str:
        content = self._normalize_text(digest.get("content", ""))
        payload = {
            "origin": str(origin or "").strip(),
            "digest_id": self._normalize_text(digest.get("id", "")),
            "title": self._normalize_text(digest.get("title", "")),
            "window_start": self._normalize_text(digest.get("window_start_text", "")),
            "window_end": self._normalize_text(digest.get("window_end_text", "")),
            "item_count": int(digest.get("item_count", 0) or 0),
            "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest() if content else "",
            "render_mode": str(digest.get("render_mode", "text") or "text").strip(),
        }
        encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    async def _hash_image_bytes(self, image_url: str) -> str:
        normalized = self._normalize_url(image_url)
        if not normalized:
            return ""

        try:
            return await asyncio.to_thread(self._hash_image_bytes_sync, normalized)
        except (HTTPError, URLError, OSError, ValueError):
            return ""

    async def _hash_local_file(self, path: str) -> str:
        try:
            return await asyncio.to_thread(self._hash_local_file_sync, path)
        except (OSError, ValueError):
            return ""

    def _hash_local_file_sync(self, path: str) -> str:
        digest = hashlib.sha256()
        total = 0
        with open(path, "rb") as fp:
            while True:
                chunk = fp.read(65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > self._IMAGE_HASH_MAX_BYTES:
                    raise ValueError("local_image_too_large_for_hash")
                digest.update(chunk)
        if total <= 0:
            return ""
        return digest.hexdigest()

    def _hash_image_bytes_sync(self, image_url: str) -> str:
        request = Request(
            image_url,
            headers={"User-Agent": "AstrBotRSSForwarder/0.5.0"},
        )
        digest = hashlib.sha256()
        total = 0
        with urlopen(request, timeout=self._IMAGE_HASH_TIMEOUT_SECONDS) as response:
            while True:
                chunk = response.read(65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > self._IMAGE_HASH_MAX_BYTES:
                    raise ValueError("image_too_large_for_hash")
                digest.update(chunk)
        if total <= 0:
            return ""
        return digest.hexdigest()

    async def _claim_dispatch(self, fingerprint: str) -> bool:
        claim = getattr(self._storage, "claim_dispatch", None)
        if not callable(claim):
            return True
        try:
            return bool(
                await claim(
                    fingerprint,
                    ttl_seconds=self._PENDING_DISPATCH_TTL_SECONDS,
                )
            )
        except Exception as exc:
            logger.warning("claim dispatch fingerprint failed: %s", exc)
            return True

    async def _confirm_dispatch(self, fingerprint: str) -> None:
        confirm = getattr(self._storage, "confirm_dispatch", None)
        if not callable(confirm):
            return
        try:
            await confirm(
                fingerprint,
                ttl_seconds=max(int(getattr(self._config, "dedup_ttl_seconds", 0) or 0), 1),
            )
        except Exception as exc:
            logger.warning("confirm dispatch fingerprint failed: %s", exc)

    async def _release_dispatch(self, fingerprint: str) -> None:
        release = getattr(self._storage, "release_dispatch", None)
        if not callable(release):
            return
        try:
            await release(fingerprint)
        except Exception as exc:
            logger.warning("release dispatch fingerprint failed: %s", exc)

    @staticmethod
    def _safe_format(template: str, values: dict[str, str]) -> str:
        try:
            return template.format(**values)
        except Exception:
            return template

    @staticmethod
    def _resolve_messagechain_cls():
        """优先使用 core MessageChain，避免 API re-export 差异。"""
        try:
            from astrbot.core.message.message_event_result import MessageChain

            return MessageChain
        except Exception:
            from astrbot.api.message_components import MessageChain

            return MessageChain

    @staticmethod
    def _resolve_plain_cls():
        try:
            from astrbot.api.message_components import Plain

            return Plain
        except Exception:
            from astrbot.core.message.message_components import Plain

            return Plain

    @staticmethod
    def _resolve_image_cls():
        try:
            from astrbot.api.message_components import Image

            return Image
        except Exception:
            from astrbot.core.message.components import Image

            return Image

    @staticmethod
    def _resolve_video_cls():
        try:
            from astrbot.api.message_components import Video

            return Video
        except Exception:
            from astrbot.core.message.components import Video

            return Video

    def _create_message_chain(
        self,
        text_lines: list[str],
        link_line: str | None = None,
        image_url: str | None = None,
        image_urls: list[str] | None = None,
        video_urls: list[str] | None = None,
        image_paths: list[str] | None = None,
        video_paths: list[str] | None = None,
    ):
        MessageChain = self._resolve_messagechain_cls()
        Plain = self._resolve_plain_cls()

        lines = [line for line in text_lines if line]
        if link_line:
            lines.append(link_line)
        plain_text = "\n".join(lines)

        components: list[Any] = [Plain(plain_text)]

        local_image_added = False
        for image_path in self._dedupe_urls(image_paths or []):
            try:
                Image = self._resolve_image_cls()
                components.append(Image.fromFileSystem(image_path))
                local_image_added = True
            except Exception as exc:
                logger.warning("build local image component failed, fallback to url if any: %s", exc)

        normalized_image_urls = self._dedupe_urls(
            []
            if local_image_added
            else list(image_urls or []) + ([image_url] if image_url else [])
        )
        for current_image_url in normalized_image_urls:
            try:
                Image = self._resolve_image_cls()
                components.append(Image.fromURL(current_image_url))
            except Exception as exc:
                logger.warning("build image component failed, keep text only: %s", exc)

        local_video_added = False
        for video_path in self._dedupe_urls(video_paths or []):
            try:
                Video = self._resolve_video_cls()
                components.append(Video.fromFileSystem(video_path))
                local_video_added = True
            except Exception as exc:
                logger.warning("build local video component failed, fallback to url if any: %s", exc)

        for video_url in self._dedupe_urls([] if local_video_added else video_urls or []):
            try:
                Video = self._resolve_video_cls()
                components.append(Video.fromURL(video_url))
            except Exception as exc:
                logger.warning("build video component failed, fallback to link: %s", exc)
                components.append(Plain(f"视频：{video_url}"))

        try:
            return MessageChain(chain=components)
        except TypeError:
            chain = MessageChain()
            if hasattr(chain, "message"):
                return chain.message(plain_text)
            if hasattr(chain, "chain"):
                chain.chain = components
                return chain
            raise

    def _build_text_message_chain(self, item: dict[str, Any]):
        data = self._build_render_data(item)
        if self._is_compact_item(item):
            return self._create_message_chain([data["title"]])

        template = self._config.render_card_template

        title = self._safe_format(template.title, data)
        source = self._safe_format(template.source, data)
        published_at = self._safe_format(template.published_at, data)
        summary = self._safe_format(template.summary, data)
        link_text = self._safe_format(template.link_text, data)
        link = data["link"]
        image_url = str(item.get("image_url", "") or "").strip()
        image_urls = self._item_image_urls(item)
        video_urls = self._item_video_urls(item)
        image_paths = self._item_image_paths(item)
        video_paths = self._item_video_paths(item)

        text_lines = [
            line
            for line in [
                title,
                f"来源：{source}" if source and self._should_display_source(item) else "",
                f"时间：{published_at}" if published_at and self._should_display_time(item) else "",
                summary,
            ]
            if line
        ]

        try:
            link_line = ""
            if link:
                link_line = f"{link_text}: {link}" if data["truncated"] == "1" else link
            return self._create_message_chain(
                text_lines,
                link_line or None,
                image_url or None,
                image_urls=image_urls,
                video_urls=video_urls,
                image_paths=image_paths,
                video_paths=video_paths,
            )
        except Exception as exc:  # pragma: no cover - 依赖运行环境
            logger.error("build text MessageChain failed: %s", exc)
            raise

    def _build_card_html(self, item: dict[str, Any]) -> str:
        data = self._build_render_data(item)
        template = self._config.render_card_template
        title = escape(self._safe_format(template.title, data))
        if self._is_compact_item(item):
            return (
                "<html><head><meta charset='utf-8' /><style>"
                "body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f5f7fb;padding:16px;}"
                ".card{background:#fff;border-radius:12px;padding:16px;box-shadow:0 2px 12px rgba(30,55,90,.12);max-width:680px;}"
                ".title{font-size:22px;font-weight:700;line-height:1.4;color:#111827;}"
                "</style></head><body>"
                f"<div class='card'><div class='title'>{title}</div></div></body></html>"
            )

        source = escape(self._safe_format(template.source, data))
        published_at = escape(self._safe_format(template.published_at, data))
        summary = escape(self._safe_format(template.summary, data))
        link = escape(data["link"])
        link_text = escape(self._safe_format(template.link_text, data) or "查看全文")
        meta_parts = []
        if source and self._should_display_source(item):
            meta_parts.append(f"来源：{source}")
        if published_at and self._should_display_time(item):
            meta_parts.append(f"时间：{published_at}")
        meta = " · ".join(meta_parts)

        footer = ""
        if link and self._should_display_link(item):
            footer = f'<a class="link" href="{link}">{link_text}</a>'
        meta_html = f"<div class='meta'>{meta}</div>" if meta else ""

        return (
            "<html><head><meta charset='utf-8' /><style>"
            "body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f5f7fb;padding:16px;}"
            ".card{background:#fff;border-radius:12px;padding:16px;box-shadow:0 2px 12px rgba(30,55,90,.12);max-width:680px;}"
            ".title{font-size:22px;font-weight:700;line-height:1.4;margin-bottom:8px;color:#111827;}"
            ".meta{color:#6b7280;font-size:13px;margin-bottom:12px;}"
            ".summary{color:#1f2937;font-size:15px;line-height:1.7;white-space:pre-wrap;}"
            ".link{display:inline-block;margin-top:12px;color:#2563eb;text-decoration:none;font-weight:600;}"
            "</style></head><body>"
            f"<div class='card'><div class='title'>{title}</div>{meta_html}"
            f"<div class='summary'>{summary}</div>{footer}</div></body></html>"
        )

    async def _build_image_payload(self, item: dict[str, Any]) -> tuple[Any, bool]:
        html = self._build_card_html(item)

        try:
            image_result = await self.html_render(html)
            # 卡片渲染成功时，主 payload 不包含 RSS 原图。
            return self._as_image_result_if_possible(item, image_result), False
        except Exception as exc:  # pragma: no cover - 依赖运行环境
            logger.warning("image render failed, fallback to text mode: %s", exc)
            chain = self._build_text_message_chain(item)
            # 文本链路已包含 image_url，避免后续重复追加原图。
            return self._as_chain_result_if_possible(item, chain), True

    def _build_daily_digest_text_chain(self, digest: dict[str, Any]):
        title = str(digest.get("title", "")).strip() or "RSS 日报"
        window_start = str(digest.get("window_start_text", "")).strip()
        window_end = str(digest.get("window_end_text", "")).strip()
        item_count = int(digest.get("item_count", 0) or 0)
        content = str(digest.get("content", "")).strip()
        links = list(digest.get("links", []) or [])

        lines = [title]
        if window_start and window_end:
            lines.append(f"统计区间：{window_start} - {window_end}")
        lines.append(f"条目数：{item_count}")
        if content:
            lines.extend(["", content])
        if links:
            lines.append("")
            lines.append("链接：")
            for index, item in enumerate(links, start=1):
                link = str((item or {}).get("link", "")).strip()
                if not link:
                    continue
                source = str((item or {}).get("source", "")).strip()
                label = f"{index}. [{source}] {link}" if source else f"{index}. {link}"
                lines.append(label)
        return self._create_message_chain(lines)

    def _build_daily_digest_card_html(self, digest: dict[str, Any]) -> str:
        title = escape(str(digest.get("title", "")).strip() or "RSS 日报")
        window_start = escape(str(digest.get("window_start_text", "")).strip())
        window_end = escape(str(digest.get("window_end_text", "")).strip())
        item_count = int(digest.get("item_count", 0) or 0)
        content = escape(str(digest.get("content", "")).strip()).replace("\n", "<br/>")
        window_text = f"{window_start} - {window_end}" if window_start and window_end else ""

        return (
            "<html><head><meta charset='utf-8' /><style>"
            "body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#eef3f9;padding:18px;}"
            ".card{background:#fff;border-radius:14px;padding:18px;box-shadow:0 2px 16px rgba(30,55,90,.12);max-width:760px;}"
            ".title{font-size:24px;font-weight:700;line-height:1.4;margin-bottom:8px;color:#111827;}"
            ".meta{color:#6b7280;font-size:13px;margin-bottom:16px;}"
            ".content{color:#1f2937;font-size:15px;line-height:1.8;white-space:pre-wrap;}"
            "</style></head><body>"
            f"<div class='card'><div class='title'>{title}</div>"
            f"<div class='meta'>统计区间：{window_text} · 条目数：{item_count}</div>"
            f"<div class='content'>{content}</div></div></body></html>"
        )

    async def _build_daily_digest_image_payload(self, digest: dict[str, Any]):
        html = self._build_daily_digest_card_html(digest)
        return await self.html_render(html)

    def _build_image_only_chain(self, image_url: str):
        MessageChain = self._resolve_messagechain_cls()
        Image = self._resolve_image_cls()

        try:
            return MessageChain(chain=[Image.fromURL(image_url)])
        except TypeError:
            chain = MessageChain()
            if hasattr(chain, "chain"):
                chain.chain = [Image.fromURL(image_url)]
                return chain
            raise

    def _build_local_image_only_chain(self, image_path: str):
        MessageChain = self._resolve_messagechain_cls()
        Image = self._resolve_image_cls()

        try:
            return MessageChain(chain=[Image.fromFileSystem(image_path)])
        except TypeError:
            chain = MessageChain()
            if hasattr(chain, "chain"):
                chain.chain = [Image.fromFileSystem(image_path)]
                return chain
            raise

    def _build_video_only_chain(self, video_url: str):
        MessageChain = self._resolve_messagechain_cls()
        Plain = self._resolve_plain_cls()

        try:
            Video = self._resolve_video_cls()
            component = Video.fromURL(video_url)
        except Exception as exc:
            logger.warning("build video component failed, fallback to link: %s", exc)
            component = Plain(f"视频：{video_url}")

        try:
            return MessageChain(chain=[component])
        except TypeError:
            chain = MessageChain()
            if hasattr(chain, "chain"):
                chain.chain = [component]
                return chain
            raise

    def _build_local_video_only_chain(self, video_path: str):
        MessageChain = self._resolve_messagechain_cls()
        Plain = self._resolve_plain_cls()

        try:
            Video = self._resolve_video_cls()
            component = Video.fromFileSystem(video_path)
        except Exception as exc:
            logger.warning("build local video component failed, fallback to text: %s", exc)
            component = Plain(f"视频文件：{video_path}")

        try:
            return MessageChain(chain=[component])
        except TypeError:
            chain = MessageChain()
            if hasattr(chain, "chain"):
                chain.chain = [component]
                return chain
            raise

    def _build_source_media_payloads(self, item: dict[str, Any]) -> list[Any]:
        payloads: list[Any] = []
        image_paths = self._item_image_paths(item)
        video_paths = self._item_video_paths(item)
        local_image_added = False
        for image_path in image_paths:
            try:
                image_chain = self._build_local_image_only_chain(image_path)
                payloads.append(self._as_chain_result_if_possible(item, image_chain))
                local_image_added = True
            except Exception as exc:
                logger.warning("build local source image payload failed: %s", exc)
        for image_url in ([] if local_image_added else self._item_image_urls(item)):
            try:
                image_chain = self._build_image_only_chain(image_url)
                payloads.append(self._as_chain_result_if_possible(item, image_chain))
            except Exception as exc:
                logger.warning("build source image payload failed: %s", exc)
        local_video_added = False
        for video_path in video_paths:
            try:
                video_chain = self._build_local_video_only_chain(video_path)
                payloads.append(self._as_chain_result_if_possible(item, video_chain))
                local_video_added = True
            except Exception as exc:
                logger.warning("build local source video payload failed: %s", exc)
        for video_url in ([] if local_video_added else self._item_video_urls(item)):
            try:
                video_chain = self._build_video_only_chain(video_url)
                payloads.append(self._as_chain_result_if_possible(item, video_chain))
            except Exception as exc:
                logger.warning("build source video payload failed: %s", exc)
        return payloads

    @classmethod
    def _item_image_urls(cls, item: dict[str, Any]) -> list[str]:
        urls = item.get("image_urls", [])
        if isinstance(urls, list):
            return cls._dedupe_urls([str(url).strip() for url in urls if str(url).strip()])
        image_url = str(item.get("image_url", "") or "").strip()
        return [image_url] if image_url else []

    @classmethod
    def _item_image_paths(cls, item: dict[str, Any]) -> list[str]:
        paths = item.get("image_paths", [])
        if not isinstance(paths, list):
            return []
        return cls._dedupe_urls([str(path).strip() for path in paths if str(path).strip()])

    @classmethod
    def _item_video_urls(cls, item: dict[str, Any]) -> list[str]:
        urls = item.get("video_urls", [])
        if not isinstance(urls, list):
            return []
        return cls._dedupe_urls([str(url).strip() for url in urls if str(url).strip()])

    @classmethod
    def _item_video_paths(cls, item: dict[str, Any]) -> list[str]:
        paths = item.get("video_paths", [])
        if not isinstance(paths, list):
            return []
        return cls._dedupe_urls([str(path).strip() for path in paths if str(path).strip()])

    @staticmethod
    def _dedupe_urls(urls: list[str]) -> list[str]:
        seen: set[str] = set()
        result: list[str] = []
        for url in urls:
            text = str(url or "").strip()
            if not text or text in seen:
                continue
            seen.add(text)
            result.append(text)
        return result

    async def html_render(self, html: str):
        render_func = getattr(self._renderer, "html_render", None)
        if not callable(render_func) and self._renderer is not self.context:
            render_func = getattr(self.context, "html_render", None)
        if callable(render_func):
            try:
                return await self._call_html_render(render_func, html)
            except Exception:
                if await self._refresh_html_render_endpoints(render_func):
                    return await self._call_html_render(render_func, html)
                raise
        raise RuntimeError("context.html_render is not available")

    async def _call_html_render(self, render_func, html: str):
        if self._html_render_accepts_data_arg(render_func):
            return await render_func(html, {})
        return await render_func(html)

    @staticmethod
    async def _refresh_html_render_endpoints(render_func) -> bool:
        func = getattr(render_func, "__func__", render_func)
        func_globals = getattr(func, "__globals__", None)
        if not isinstance(func_globals, dict):
            return False

        html_renderer = func_globals.get("html_renderer")
        network_strategy = getattr(html_renderer, "network_strategy", None)
        refresh_func = getattr(network_strategy, "get_official_endpoints", None)
        if not callable(refresh_func):
            return False

        try:
            await refresh_func()
        except Exception as exc:  # pragma: no cover - 依赖 AstrBot 运行环境
            logger.warning("refresh t2i endpoints failed: %s", exc)
            return False
        return True

    @staticmethod
    def _html_render_accepts_data_arg(render_func) -> bool:
        try:
            signature = inspect.signature(render_func)
        except (TypeError, ValueError):
            return True
        positional_params = [
            param
            for param in signature.parameters.values()
            if param.kind
            in {
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.VAR_POSITIONAL,
            }
        ]
        if any(param.kind == inspect.Parameter.VAR_POSITIONAL for param in positional_params):
            return True
        return len(positional_params) >= 2

    @staticmethod
    def _as_chain_result_if_possible(item: dict[str, Any], message_chain):
        event = item.get("event")
        if event and hasattr(event, "chain_result"):
            try:
                return event.chain_result(message_chain)
            except Exception as exc:
                logger.warning("event.chain_result failed, fallback to message_chain: %s", exc)
        return message_chain

    def _as_image_result_if_possible(self, item: dict[str, Any], image_result):
        event = item.get("event")
        if event and hasattr(event, "image_result"):
            try:
                return event.image_result(image_result)
            except Exception as exc:
                logger.warning("event.image_result failed, fallback to image_result: %s", exc)
        if isinstance(image_result, str) and image_result.strip():
            try:
                return self._build_image_only_chain(image_result.strip())
            except Exception as exc:
                logger.warning("build rendered image chain failed, fallback to image_result: %s", exc)
        return image_result

    async def dispatch(self, item: dict) -> DispatchResult:
        origins = self._resolve_origins(item)
        if not origins:
            logger.warning("skip dispatch: no available targets for item=%s", item)
            return DispatchResult()

        extra_payloads: list[Any] = []
        if self._config.render_mode == "image":
            payload, source_image_already_included = await self._build_image_payload(item)
            if not source_image_already_included and not self._is_compact_item(item):
                extra_payloads.extend(self._build_source_media_payloads(item))
        else:
            try:
                chain = self._build_text_message_chain(item)
            except Exception:
                return DispatchResult(transient_failure_count=1)
            payload = self._as_chain_result_if_possible(item, chain)

        result = DispatchResult()
        for unified_msg_origin in origins:
            if unified_msg_origin in self._disabled_origins:
                result.skipped_disabled_count += 1
                continue
            fingerprint = await self._build_dispatch_fingerprint(item, unified_msg_origin)
            if not await self._claim_dispatch(fingerprint):
                result.skipped_duplicate_count += 1
                logger.warning(
                    "skip duplicate dispatch origin=%s item=%s fingerprint=%s",
                    unified_msg_origin,
                    str(item.get("guid", "") or item.get("title", "")).strip(),
                    fingerprint[:12],
                )
                continue
            try:
                await self.context.send_message(unified_msg_origin, payload)
                result.success_count += 1
                await self._confirm_dispatch(fingerprint)
                for extra_payload in extra_payloads:
                    try:
                        await self.context.send_message(unified_msg_origin, extra_payload)
                    except Exception as exc:
                        logger.warning(
                            "extra source media send failed origin=%s: %s",
                            unified_msg_origin,
                            exc,
                        )
            except Exception as exc:
                await self._release_dispatch(fingerprint)
                if self._is_permanent_target_error(exc):
                    self._disabled_origins.add(unified_msg_origin)
                    result.permanent_failure_count += 1
                    logger.error(
                        "主动消息发送失败 origin=%s: %s。已将该 target 标记为无效，本次运行内不再重试。",
                        unified_msg_origin,
                        exc or "unknown error",
                    )
                    continue
                result.transient_failure_count += 1
                logger.error(
                    "主动消息发送失败 origin=%s: %s。若当前平台不支持主动消息，请在支持的会话渠道配置 target。",
                    unified_msg_origin,
                    exc,
                )
        return result

    async def dispatch_daily_digest(self, digest: dict[str, Any]) -> DispatchResult:
        target_ids = list(digest.get("target_ids", []) or [])
        origins = self._resolve_target_origins(target_ids)
        if not origins:
            logger.warning("skip daily digest dispatch: no available targets for digest=%s", digest)
            return DispatchResult()

        render_mode = str(digest.get("render_mode", "text") or "text").strip().lower()
        try:
            if render_mode == "image":
                try:
                    payload = self._as_image_result_if_possible(
                        digest,
                        await self._build_daily_digest_image_payload(digest),
                    )
                except Exception as exc:
                    logger.warning(
                        "daily digest image render failed, fallback to text mode id=%s: %s",
                        digest.get("id", ""),
                        exc,
                    )
                    chain = self._build_daily_digest_text_chain(digest)
                    payload = self._as_chain_result_if_possible(digest, chain)
            else:
                chain = self._build_daily_digest_text_chain(digest)
                payload = self._as_chain_result_if_possible(digest, chain)
        except Exception as exc:
            logger.error("build daily digest payload failed id=%s: %s", digest.get("id", ""), exc)
            return DispatchResult(transient_failure_count=1)

        result = DispatchResult()
        for unified_msg_origin in origins:
            if unified_msg_origin in self._disabled_origins:
                result.skipped_disabled_count += 1
                continue
            fingerprint = await self._build_daily_digest_fingerprint(digest, unified_msg_origin)
            if not await self._claim_dispatch(fingerprint):
                result.skipped_duplicate_count += 1
                logger.warning(
                    "skip duplicate daily digest origin=%s digest=%s fingerprint=%s",
                    unified_msg_origin,
                    str(digest.get("id", "")).strip(),
                    fingerprint[:12],
                )
                continue
            try:
                await self.context.send_message(unified_msg_origin, payload)
                result.success_count += 1
                await self._confirm_dispatch(fingerprint)
            except Exception as exc:
                await self._release_dispatch(fingerprint)
                if self._is_permanent_target_error(exc):
                    self._disabled_origins.add(unified_msg_origin)
                    result.permanent_failure_count += 1
                    logger.error(
                        "日报发送失败 origin=%s: %s。已将该 target 标记为无效，本次运行内不再重试。",
                        unified_msg_origin,
                        exc or "unknown error",
                    )
                    continue
                result.transient_failure_count += 1
                logger.error("日报发送失败 origin=%s: %s", unified_msg_origin, exc)
        return result

    @staticmethod
    def _is_permanent_target_error(exc: Exception) -> bool:
        text = str(exc or "").strip().lower()
        if not text:
            return True
        permanent_markers = (
            "not support",
            "unsupported",
            "invalid",
            "not found",
            "no such",
            "无效",
            "不支持",
            "不存在",
            "找不到",
        )
        return any(marker in text for marker in permanent_markers)
