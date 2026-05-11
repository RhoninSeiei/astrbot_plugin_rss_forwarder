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
