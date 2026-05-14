import asyncio
import json
import re
from dataclasses import dataclass
from html import unescape
from typing import Any

from astrbot.api import logger

from .config import RSSConfig


@dataclass(slots=True)
class SemanticDedupResult:
    duplicate: bool = False
    matched_record_id: str = ""
    confidence: float = 0.0
    reason: str = ""


class SemanticDedupService:
    """任务组级语义重复判定。"""

    _TAG_RE = re.compile(r"<[^>]+>")
    _SPACE_RE = re.compile(r"\s+")

    def __init__(self, context, config: RSSConfig, storage) -> None:
        self.context = context
        self._config = config
        self._storage = storage

    async def check(
        self,
        job,
        item: dict[str, Any],
        *,
        unified_msg_origin: str = "",
    ) -> SemanticDedupResult:
        if not bool(getattr(job, "semantic_dedup_enabled", False)):
            return SemanticDedupResult(reason="disabled")

        current = self._prepare_item(item)
        if not current["title"] and not current["summary"]:
            return SemanticDedupResult(reason="empty_input")

        ttl_seconds = self._ttl_seconds(job)
        max_candidates = self._max_candidates(job)
        records = await self._list_records(job, ttl_seconds=ttl_seconds, limit=max_candidates)
        if not records:
            return SemanticDedupResult(reason="no_candidates")

        provider_id = await self._resolve_provider_id(job, unified_msg_origin=unified_msg_origin)
        if not provider_id:
            logger.warning("semantic dedup enabled but no provider id is available, skip job=%s", job.id)
            return SemanticDedupResult(reason="provider_missing")

        prompt = self._build_prompt(current, records)
        llm_kwargs: dict[str, Any] = {
            "chat_provider_id": provider_id,
            "prompt": prompt,
        }
        profile = str(getattr(self._config, "llm_profile", "") or "").strip()
        if profile:
            llm_kwargs["profile"] = profile
        llm_kwargs.update(self._build_llm_proxy_kwargs())

        llm_call = self.context.llm_generate(**llm_kwargs)
        try:
            result = await asyncio.wait_for(llm_call, timeout=self._timeout_seconds())
        except asyncio.TimeoutError:
            return SemanticDedupResult(reason="timeout")
        except Exception as exc:
            logger.warning("semantic dedup llm call failed job=%s: %s", job.id, exc)
            return SemanticDedupResult(reason=f"exception:{type(exc).__name__}")

        parsed = self._parse_result(self._extract_generated_text(result))
        if parsed is None:
            return SemanticDedupResult(reason="invalid_payload")

        min_confidence = self._min_confidence(job)
        if parsed.duplicate and not parsed.matched_record_id:
            return SemanticDedupResult(
                duplicate=False,
                confidence=parsed.confidence,
                reason="missing_match_id",
            )
        if parsed.duplicate and parsed.confidence < min_confidence:
            return SemanticDedupResult(
                duplicate=False,
                matched_record_id=parsed.matched_record_id,
                confidence=parsed.confidence,
                reason="below_confidence",
            )
        return parsed

    async def remember(
        self,
        job,
        item: dict[str, Any],
        seen_keys: list[str],
        ttl_seconds: int | None = None,
    ) -> dict[str, Any]:
        if not bool(getattr(job, "semantic_dedup_enabled", False)):
            return {}

        current = self._prepare_item(item)
        if not current["title"] and not current["summary"]:
            return {}

        putter = getattr(self._storage, "put_semantic_dedup_record", None)
        if not callable(putter):
            return {}
        return await putter(
            str(getattr(job, "id", "")).strip(),
            item,
            seen_keys=seen_keys,
            ttl_seconds=int(ttl_seconds or self._ttl_seconds(job)),
        )

    async def record_duplicate_match(self, job, matched_record_id: str) -> None:
        toucher = getattr(self._storage, "touch_semantic_dedup_record", None)
        if not callable(toucher):
            return
        await toucher(str(getattr(job, "id", "")).strip(), str(matched_record_id or "").strip())

    async def merge_digest_items(
        self,
        digest,
        items: list[dict[str, Any]],
        *,
        unified_msg_origin: str = "",
    ) -> dict[str, Any]:
        source_items = [dict(item) for item in items]
        if not bool(getattr(digest, "semantic_merge_enabled", False)):
            return {
                "items": source_items,
                "reason": "disabled",
                "merged_count": 0,
                "input_count": len(source_items),
                "output_count": len(source_items),
            }

        prepared_items = []
        for index, item in enumerate(source_items, start=1):
            prepared = self._prepare_item(item)
            if not prepared["title"] and not prepared["summary"]:
                continue
            prepared_items.append((index, item, prepared))

        max_candidates = self._digest_merge_max_candidates(digest)
        candidates = prepared_items[:max_candidates]
        if len(candidates) < 2:
            return {
                "items": source_items,
                "reason": "insufficient_candidates",
                "merged_count": 0,
                "input_count": len(source_items),
                "output_count": len(source_items),
            }

        provider_id = await self._resolve_digest_merge_provider_id(
            digest,
            unified_msg_origin=unified_msg_origin,
        )
        if not provider_id:
            return {
                "items": source_items,
                "reason": "provider_missing",
                "merged_count": 0,
                "input_count": len(source_items),
                "output_count": len(source_items),
            }

        prompt = self._build_digest_merge_prompt(digest, candidates)
        llm_kwargs: dict[str, Any] = {
            "chat_provider_id": provider_id,
            "prompt": prompt,
        }
        profile = str(getattr(self._config, "llm_profile", "") or "").strip()
        if profile:
            llm_kwargs["profile"] = profile
        llm_kwargs.update(self._build_llm_proxy_kwargs())

        llm_call = self.context.llm_generate(**llm_kwargs)
        try:
            result = await asyncio.wait_for(
                llm_call,
                timeout=self._digest_merge_timeout_seconds(digest),
            )
        except asyncio.TimeoutError:
            return {
                "items": source_items,
                "reason": "timeout",
                "merged_count": 0,
                "input_count": len(source_items),
                "output_count": len(source_items),
            }
        except Exception as exc:
            logger.warning("daily digest semantic merge llm call failed digest=%s: %s", getattr(digest, "id", ""), exc)
            return {
                "items": source_items,
                "reason": f"exception:{type(exc).__name__}",
                "merged_count": 0,
                "input_count": len(source_items),
                "output_count": len(source_items),
            }

        groups = self._parse_digest_merge_groups(self._extract_generated_text(result))
        if groups is None:
            return {
                "items": source_items,
                "reason": "invalid_payload",
                "merged_count": 0,
                "input_count": len(source_items),
                "output_count": len(source_items),
            }

        merged_items, merged_count = self._apply_digest_merge_groups(digest, source_items, groups)
        return {
            "items": merged_items,
            "reason": "ok",
            "merged_count": merged_count,
            "input_count": len(source_items),
            "output_count": len(merged_items),
        }

    async def _list_records(self, job, *, ttl_seconds: int, limit: int) -> list[dict[str, Any]]:
        lister = getattr(self._storage, "list_semantic_dedup_records", None)
        if not callable(lister):
            return []
        records = await lister(
            str(getattr(job, "id", "")).strip(),
            limit=limit,
            ttl_seconds=ttl_seconds,
        )
        return [record for record in records if isinstance(record, dict)]

    async def _resolve_provider_id(self, job, *, unified_msg_origin: str = "") -> str:
        provider_id = str(getattr(job, "semantic_dedup_provider_id", "") or "").strip()
        if provider_id:
            return provider_id

        provider_id = str(getattr(self._config, "llm_provider_id", "") or "").strip()
        if provider_id:
            return provider_id

        origin = str(unified_msg_origin or "").strip()
        if not origin:
            return ""
        try:
            provider_id = await self.context.get_current_chat_provider_id(umo=origin)
        except Exception as exc:
            logger.warning("get_current_chat_provider_id for semantic dedup failed: %s", exc)
            return ""
        return str(provider_id or "").strip()

    async def _resolve_digest_merge_provider_id(self, digest, *, unified_msg_origin: str = "") -> str:
        provider_id = str(getattr(digest, "semantic_merge_provider_id", "") or "").strip()
        if provider_id:
            return provider_id

        provider_id = str(getattr(self._config, "llm_provider_id", "") or "").strip()
        if provider_id:
            return provider_id

        origin = str(unified_msg_origin or "").strip()
        if not origin:
            return ""
        try:
            provider_id = await self.context.get_current_chat_provider_id(umo=origin)
        except Exception as exc:
            logger.warning("get_current_chat_provider_id for daily digest semantic merge failed: %s", exc)
            return ""
        return str(provider_id or "").strip()

    def _build_prompt(self, current: dict[str, str], records: list[dict[str, Any]]) -> str:
        candidates = []
        for record in records:
            prepared = self._prepare_item(record)
            if not prepared["title"] and not prepared["summary"]:
                continue
            candidates.append(
                {
                    "record_id": str(record.get("record_id", "") or "").strip(),
                    "source": self._sanitize_text(str(record.get("source", "") or ""))[:80],
                    "title": prepared["title"][:180],
                    "summary": prepared["summary"][:260],
                    "link": str(record.get("link", "") or "").strip()[:240],
                    "published_at": str(record.get("published_at", "") or "").strip()[:80],
                }
            )

        payload = {
            "current": {
                "source": current["source"][:80],
                "title": current["title"][:180],
                "summary": current["summary"][:300],
                "link": current["link"][:240],
                "published_at": current["published_at"][:80],
            },
            "candidates": candidates,
        }
        serialized = json.dumps(payload, ensure_ascii=False, indent=2)
        return (
            "判断 current 是否与 candidates 中某条新闻属于同一事实事件，并严格只返回 JSON。\n"
            "同一事实事件包括同一产品发布、同一漏洞公告、同一财报消息、同一爆料或同一官方声明。\n"
            "仅主题相近、公司相同、产品线相同、行业相同，不能判为重复。\n"
            "无法确定时返回 duplicate=false。\n"
            "输出格式：{\"duplicate\":true,\"matched_record_id\":\"...\",\"confidence\":0.0,\"reason\":\"...\"}\n\n"
            f"数据：\n{serialized}"
        )

    def _build_digest_merge_prompt(self, digest, items: list[tuple[int, dict[str, Any], dict[str, str]]]) -> str:
        payload = {
            "digest_id": str(getattr(digest, "id", "") or "").strip(),
            "items": [
                {
                    "index": index,
                    "source": prepared["source"][:80],
                    "title": prepared["title"][:180],
                    "summary": prepared["summary"][:300],
                    "link": prepared["link"][:240],
                    "published_at": prepared["published_at"][:80],
                }
                for index, _item, prepared in items
            ],
        }
        serialized = json.dumps(payload, ensure_ascii=False, indent=2)
        return (
            "把 items 中属于同一事实事件的 RSS 条目合并分组，并严格只返回 JSON。\n"
            "同一事实事件包括同一产品发布、同一漏洞公告、同一财报消息、同一爆料或同一官方声明。\n"
            "仅主题相近、公司相同、产品线相同、行业相同，不能合并。\n"
            "无法确定时不要分组。只输出包含 2 个或以上条目的分组。\n"
            "输出格式：{\"groups\":[{\"item_indices\":[1,2],\"title\":\"合并后的标题\","
            "\"summary\":\"合并后的事实摘要\",\"confidence\":0.0,\"reason\":\"...\"}]}\n\n"
            f"数据：\n{serialized}"
        )

    @classmethod
    def _parse_result(cls, text: str) -> SemanticDedupResult | None:
        raw = str(text or "").strip()
        if not raw:
            return None

        stripped = cls._strip_code_fence(raw)
        candidates = [stripped]
        first = stripped.find("{")
        last = stripped.rfind("}")
        if first != -1 and last != -1 and first < last:
            candidates.append(stripped[first : last + 1])

        for candidate in candidates:
            try:
                data = json.loads(candidate)
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            try:
                confidence = float(data.get("confidence", 0) or 0)
            except (TypeError, ValueError):
                confidence = 0.0
            return SemanticDedupResult(
                duplicate=bool(data.get("duplicate", False)),
                matched_record_id=str(data.get("matched_record_id", "") or "").strip(),
                confidence=max(0.0, min(confidence, 1.0)),
                reason=str(data.get("reason", "") or "").strip() or "ok",
            )
        return None

    @classmethod
    def _parse_digest_merge_groups(cls, text: str) -> list[dict[str, Any]] | None:
        raw = str(text or "").strip()
        if not raw:
            return None

        stripped = cls._strip_code_fence(raw)
        candidates = [stripped]
        first = stripped.find("{")
        last = stripped.rfind("}")
        if first != -1 and last != -1 and first < last:
            candidates.append(stripped[first : last + 1])

        for candidate in candidates:
            try:
                data = json.loads(candidate)
            except Exception:
                continue
            groups = data.get("groups") if isinstance(data, dict) else data
            if not isinstance(groups, list):
                continue
            normalized = []
            for group in groups:
                if isinstance(group, dict):
                    normalized.append(group)
            return normalized
        return None

    @staticmethod
    def _extract_generated_text(result: Any) -> str:
        for attr in ("completion_text", "text", "content"):
            value = getattr(result, attr, None)
            if value:
                return str(value).strip()
        if isinstance(result, dict):
            for key in ("completion_text", "text", "content"):
                value = result.get(key)
                if value:
                    return str(value).strip()
        return str(result or "").strip()

    def _prepare_item(self, item: dict[str, Any]) -> dict[str, str]:
        return {
            "source": self._sanitize_text(str(item.get("feed_title", "") or item.get("source", "") or "")),
            "title": self._sanitize_text(str(item.get("_source_title", "") or item.get("title", "") or "")),
            "summary": self._sanitize_text(
                str(
                    item.get("_source_summary", "")
                    or item.get("summary", "")
                    or item.get("content", "")
                    or ""
                )
            ),
            "link": str(item.get("link", "") or "").strip(),
            "published_at": str(item.get("published_at", "") or item.get("published", "") or "").strip(),
        }

    def _apply_digest_merge_groups(
        self,
        digest,
        items: list[dict[str, Any]],
        groups: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], int]:
        min_confidence = self._digest_merge_min_confidence(digest)
        consumed: set[int] = set()
        group_by_start: dict[int, dict[str, Any]] = {}
        merged_count = 0

        for group in groups:
            indices = self._group_item_indices(group, max_index=len(items))
            if len(indices) < 2 or any(index in consumed for index in indices):
                continue
            try:
                confidence = float(group.get("confidence", 0) or 0)
            except (TypeError, ValueError):
                confidence = 0.0
            confidence = max(0.0, min(confidence, 1.0))
            if confidence < min_confidence:
                continue
            start_index = min(indices)
            source_group_items = [items[index - 1] for index in indices]
            merged_item = self._build_digest_merged_item(
                digest,
                group,
                source_group_items,
                group_number=merged_count + 1,
                confidence=confidence,
            )
            group_by_start[start_index] = merged_item
            consumed.update(indices)
            merged_count += 1

        merged_items: list[dict[str, Any]] = []
        for index, item in enumerate(items, start=1):
            if index in group_by_start:
                merged_items.append(group_by_start[index])
                continue
            if index in consumed:
                continue
            merged_items.append(dict(item))
        return merged_items, merged_count

    def _build_digest_merged_item(
        self,
        digest,
        group: dict[str, Any],
        items: list[dict[str, Any]],
        *,
        group_number: int,
        confidence: float,
    ) -> dict[str, Any]:
        first = dict(items[0])
        prepared_items = [self._prepare_item(item) for item in items]
        sources = self._unique_texts([prepared["source"] for prepared in prepared_items])
        title = self._sanitize_text(str(group.get("title", "") or ""))
        summary = self._sanitize_text(str(group.get("summary", "") or ""))
        if not title:
            title = prepared_items[0]["title"] or str(first.get("title", "") or "").strip()
        if not summary:
            summary = prepared_items[0]["summary"] or str(first.get("summary", "") or "").strip()

        source_text = " / ".join(sources)
        first.update(
            {
                "title": title,
                "summary": summary,
                "feed_title": source_text or str(first.get("feed_title", "") or first.get("source", "") or "").strip(),
                "source": source_text or str(first.get("source", "") or first.get("feed_title", "") or "").strip(),
                "source_items": [dict(item) for item in items],
                "merged_count": len(items),
                "semantic_merge_group_id": f"{str(getattr(digest, 'id', '') or '').strip()}:group:{group_number}",
                "semantic_merge_confidence": confidence,
                "semantic_merge_reason": str(group.get("reason", "") or "").strip(),
            }
        )
        return first

    @staticmethod
    def _group_item_indices(group: dict[str, Any], *, max_index: int) -> list[int]:
        raw_indices = group.get("item_indices", group.get("indices", []))
        if not isinstance(raw_indices, list):
            return []
        indices: list[int] = []
        seen: set[int] = set()
        for raw_index in raw_indices:
            try:
                index = int(raw_index)
            except (TypeError, ValueError):
                continue
            if index < 1 or index > max_index or index in seen:
                continue
            indices.append(index)
            seen.add(index)
        return indices

    @staticmethod
    def _unique_texts(values: list[str]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for value in values:
            text = str(value or "").strip()
            if not text or text in seen:
                continue
            seen.add(text)
            result.append(text)
        return result

    def _build_llm_proxy_kwargs(self) -> dict[str, Any]:
        mode = str(getattr(self._config, "llm_proxy_mode", "system") or "system").strip().lower()
        proxy_url = str(getattr(self._config, "llm_proxy_url", "") or "").strip()
        if mode == "custom" and proxy_url:
            return {"proxy": proxy_url, "trust_env": False}
        if mode == "off":
            return {"trust_env": False}
        return {}

    def _timeout_seconds(self) -> int:
        return max(int(getattr(self._config, "llm_timeout_seconds", 15) or 15), 1)

    def _digest_merge_timeout_seconds(self, digest) -> float:
        try:
            value = float(getattr(digest, "llm_timeout_seconds", 0) or 0)
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            return value
        return float(self._timeout_seconds())

    @staticmethod
    def _ttl_seconds(job) -> int:
        return max(int(getattr(job, "semantic_dedup_ttl_seconds", 24 * 60 * 60) or 24 * 60 * 60), 1)

    @staticmethod
    def _max_candidates(job) -> int:
        return max(int(getattr(job, "semantic_dedup_max_candidates", 20) or 20), 1)

    @staticmethod
    def _min_confidence(job) -> float:
        try:
            confidence = float(getattr(job, "semantic_dedup_min_confidence", 0.82) or 0.82)
        except (TypeError, ValueError):
            confidence = 0.82
        return max(0.0, min(confidence, 1.0))

    @staticmethod
    def _digest_merge_max_candidates(digest) -> int:
        return max(int(getattr(digest, "semantic_merge_max_candidates", 20) or 20), 1)

    @staticmethod
    def _digest_merge_min_confidence(digest) -> float:
        try:
            confidence = float(getattr(digest, "semantic_merge_min_confidence", 0.82) or 0.82)
        except (TypeError, ValueError):
            confidence = 0.82
        return max(0.0, min(confidence, 1.0))

    @staticmethod
    def _strip_code_fence(text: str) -> str:
        value = str(text or "").strip()
        if value.startswith("```"):
            value = re.sub(r"^```[a-zA-Z0-9_-]*\n", "", value)
            if value.endswith("```"):
                value = value[:-3]
        return value.strip()

    @classmethod
    def _sanitize_text(cls, text: str) -> str:
        value = unescape(str(text or ""))
        if not value:
            return ""
        value = cls._TAG_RE.sub(" ", value)
        value = value.replace("\u00a0", " ")
        value = cls._SPACE_RE.sub(" ", value).strip()
        return value
