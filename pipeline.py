import asyncio
import json
import re
from html import unescape
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener

from astrbot.api import logger

from .config import RSSConfig


class FeedPipeline:
    """处理层：负责在分发前对条目进行可选增强。"""

    GOOGLE_TRANSLATE_ENDPOINT = "https://translation.googleapis.com/language/translate/v2"
    _TAG_RE = re.compile(r"<[^>]+>")
    _SPACE_RE = re.compile(r"\s+")

    def __init__(self, context, config: RSSConfig) -> None:
        self.context = context
        self._config = config

    async def process(self, entry: dict[str, Any]) -> dict[str, Any]:
        """执行分发前处理，失败时始终回退到原始条目。"""
        if not self._config.llm_enabled and not self._config.google_translate_enabled:
            return entry

        input_text = self._build_llm_input(entry)
        if not input_text:
            return entry

        summary = ""
        if self._config.llm_enabled:
            summary = await self._try_llm_summary(entry, input_text)

        if not summary and self._config.google_translate_enabled:
            summary = await self._try_google_translate(input_text)

        if not summary:
            summary = self._build_fallback_summary(entry)

        if not summary:
            return entry

        enriched = dict(entry)
        enriched["summary"] = summary
        return enriched


    async def diagnose_translation(self, entry: dict[str, Any] | None = None) -> dict[str, Any]:
        """执行翻译链路自检，不触发消息分发。"""
        sample = dict(entry or {})
        sample.setdefault("title", "RSS translation diagnostic title")
        sample.setdefault("summary", "This is a translation diagnostics message from RSS forwarder.")

        input_text = self._build_llm_input(sample)
        report: dict[str, Any] = {
            "input_chars": len(input_text),
            "llm": {
                "enabled": bool(self._config.llm_enabled),
                "timeout_seconds": int(self._config.llm_timeout_seconds),
                "provider_id": "",
                "ok": False,
                "latency_ms": 0,
                "error": "",
                "preview": "",
            },
            "google": {
                "enabled": bool(self._config.google_translate_enabled),
                "timeout_seconds": int(self._config.google_translate_timeout_seconds),
                "target_lang": str(self._config.google_translate_target_lang),
                "ok": False,
                "latency_ms": 0,
                "error": "",
                "preview": "",
            },
        }

        if not input_text:
            report["error"] = "empty_input"
            return report

        provider_id = await self._resolve_provider_id(sample)
        report["llm"]["provider_id"] = provider_id

        if self._config.llm_enabled:
            loop = asyncio.get_running_loop()
            start = loop.time()
            llm_summary = await self._try_llm_summary(sample, input_text)
            report["llm"]["latency_ms"] = int((loop.time() - start) * 1000)
            if llm_summary:
                report["llm"]["ok"] = True
                report["llm"]["preview"] = self._preview(llm_summary)
            else:
                report["llm"]["error"] = "empty_result_or_failed"
        elif provider_id:
            report["llm"]["error"] = "llm_disabled"
        else:
            report["llm"]["error"] = "llm_disabled_or_provider_missing"

        if self._config.google_translate_enabled:
            loop = asyncio.get_running_loop()
            start = loop.time()
            google_summary = await self._try_google_translate(input_text)
            report["google"]["latency_ms"] = int((loop.time() - start) * 1000)
            if google_summary:
                report["google"]["ok"] = True
                report["google"]["preview"] = self._preview(google_summary)
            else:
                report["google"]["error"] = "empty_result_or_failed"
        else:
            report["google"]["error"] = "google_disabled"

        return report

    async def _try_llm_summary(self, entry: dict[str, Any], input_text: str) -> str:
        provider_id = await self._resolve_provider_id(entry)
        if not provider_id:
            logger.warning("llm enabled but no available provider id, skip llm enrich")
            return ""

        prompt = self._build_prompt(input_text)
        llm_kwargs: dict[str, Any] = {
            "chat_provider_id": provider_id,
            "prompt": prompt,
        }
        profile = str(self._config.llm_profile or "").strip()
        if profile:
            llm_kwargs["profile"] = profile

        # 尝试透传代理参数（具体是否生效取决于 provider 实现）。
        llm_kwargs.update(self._build_llm_proxy_kwargs())

        llm_call = self.context.llm_generate(**llm_kwargs)
        try:
            result = await asyncio.wait_for(llm_call, timeout=self._config.llm_timeout_seconds)
        except asyncio.TimeoutError:
            logger.warning(
                "pipeline llm enrich timeout after %ss, fallback to next translator",
                self._config.llm_timeout_seconds,
            )
            return ""
        except Exception as exc:
            logger.warning("pipeline llm enrich failed, fallback to next translator: %s", exc)
            return ""

        generated_text = self._extract_generated_text(result)
        return self._sanitize_text(generated_text)

    async def _try_google_translate(self, input_text: str) -> str:
        api_key = str(self._config.google_translate_api_key or "").strip()
        if not api_key:
            logger.warning("google_translate_enabled=true but api key is empty, skip google translate")
            return ""

        try:
            translated = await asyncio.wait_for(
                asyncio.to_thread(self._google_translate_blocking, input_text),
                timeout=self._config.google_translate_timeout_seconds,
            )
            return self._sanitize_text(translated)
        except asyncio.TimeoutError:
            logger.warning(
                "google translate timeout after %ss",
                self._config.google_translate_timeout_seconds,
            )
            return ""
        except Exception as exc:
            logger.warning("google translate failed: %s", exc)
            return ""

    def _google_translate_blocking(self, input_text: str) -> str:
        payload = {
            "q": input_text,
            "target": self._config.google_translate_target_lang,
            "format": "text",
            "key": self._config.google_translate_api_key,
        }
        body = urlencode(payload, doseq=True).encode("utf-8")

        req = Request(
            url=self.GOOGLE_TRANSLATE_ENDPOINT,
            data=body,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "astrbot_plugin_rss_forwarder/0.2 (+https://github.com/RhoninSeiei/astrbot_plugin_rss_forwarder)",
            },
            method="POST",
        )

        opener = self._build_google_opener()
        timeout = self._config.google_translate_timeout_seconds
        try:
            with opener.open(req, timeout=timeout) as resp:  # noqa: S310
                raw = resp.read().decode("utf-8", errors="ignore")
        except HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="ignore")
            except Exception:
                detail = str(exc)
            raise RuntimeError(f"google translate http error {exc.code}: {detail}") from exc
        except URLError as exc:
            raise RuntimeError(f"google translate network error: {exc}") from exc

        data = json.loads(raw)
        if isinstance(data, dict) and data.get("error"):
            message = str((data.get("error") or {}).get("message", "unknown error"))
            raise RuntimeError(f"google translate api error: {message}")

        translations = ((data.get("data") or {}).get("translations") or []) if isinstance(data, dict) else []
        if not translations:
            return ""

        translated = str((translations[0] or {}).get("translatedText", "")).strip()
        return unescape(translated)

    def _build_google_opener(self):
        mode = str(self._config.google_translate_proxy_mode or "system").strip().lower()
        proxy_url = str(self._config.google_translate_proxy_url or "").strip()

        if mode == "off":
            return build_opener(ProxyHandler({}))

        if mode == "custom":
            if not proxy_url:
                # 用户选择 custom 但未填地址时，回退直连。
                return build_opener(ProxyHandler({}))
            return build_opener(ProxyHandler({"http": proxy_url, "https": proxy_url}))

        # system: 使用容器/系统环境变量中的 HTTP(S)_PROXY。
        return build_opener()

    def _build_llm_proxy_kwargs(self) -> dict[str, Any]:
        mode = str(self._config.llm_proxy_mode or "system").strip().lower()
        proxy_url = str(self._config.llm_proxy_url or "").strip()

        if mode == "custom" and proxy_url:
            return {"proxy": proxy_url, "trust_env": False}
        if mode == "off":
            return {"trust_env": False}
        return {}

    async def _resolve_provider_id(self, entry: dict[str, Any]) -> str:
        provider_id = str(self._config.llm_provider_id or "").strip()
        if provider_id:
            return provider_id

        origin = str(entry.get("unified_msg_origin", "")).strip()
        event = entry.get("event")
        if not origin and event is not None:
            origin = str(getattr(event, "unified_msg_origin", "")).strip()

        if not origin:
            return ""

        try:
            provider_id = await self.context.get_current_chat_provider_id(umo=origin)
        except Exception as exc:
            logger.warning("get_current_chat_provider_id failed: %s", exc)
            return ""
        return str(provider_id or "").strip()

    def _build_llm_input(self, entry: dict[str, Any]) -> str:
        title = self._sanitize_text(str(entry.get("title", "") or ""))
        summary = self._sanitize_text(str(entry.get("summary", "") or ""))
        content = self._sanitize_text(str(entry.get("content", "") or ""))
        parts = [part for part in [title, summary, content] if part]
        merged = "\n\n".join(parts)
        if not merged:
            return ""
        return merged[: self._config.max_input_chars]

    def _build_fallback_summary(self, entry: dict[str, Any]) -> str:
        summary = self._sanitize_text(str(entry.get("summary", "") or entry.get("content", "") or ""))
        if summary:
            return summary
        title = self._sanitize_text(str(entry.get("title", "") or ""))
        return title

    @classmethod
    def _sanitize_text(cls, text: str) -> str:
        value = unescape(str(text or ""))
        if not value:
            return ""
        value = cls._TAG_RE.sub(" ", value)
        value = value.replace("\u00a0", " ")
        value = cls._SPACE_RE.sub(" ", value).strip()
        return value

    @staticmethod
    def _build_prompt(input_text: str) -> str:
        return (
            "请对以下 RSS 内容执行处理：\n"
            "1. 输出一段中文摘要；\n"
            "2. 若原文不是中文，额外给出中文翻译；\n"
            "3. 总长度不超过 180 字。\n\n"
            f"内容：\n{input_text}"
        )

    @staticmethod
    def _preview(text: str, limit: int = 120) -> str:
        value = str(text or "").strip()
        if len(value) <= limit:
            return value
        return value[:limit] + "..."

    @staticmethod
    def _extract_generated_text(result: Any) -> str:
        if result is None:
            return ""
        if isinstance(result, str):
            return result.strip()
        completion_text = getattr(result, "completion_text", None)
        if isinstance(completion_text, str) and completion_text.strip():
            return completion_text.strip()
        if isinstance(result, dict):
            for key in ("text", "content", "message", "result"):
                value = result.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return str(result).strip()
