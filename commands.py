from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from astrbot.api.event import AstrMessageEvent

if TYPE_CHECKING:
    from .source_probe import SourceProbeService


class RSSCommands:
    """命令入口。"""

    scheduler = None

    def __init__(
        self,
        source_probe_service: SourceProbeService | None = None,
    ) -> None:
        self.source_probe_service = source_probe_service
        self._rss_probe_lock = asyncio.Lock()

    async def rss_router(self, event: AstrMessageEvent):
        """兜底消息路由：在未命中 wake/at 指令条件时，仍可处理 /rss 子命令。"""
        message_text = self._get_message_text(event)
        tokens = message_text.strip().split()
        if not tokens:
            return

        head = tokens[0].lstrip("/").lower()
        if head != "rss":
            return

        sub = tokens[1].lower() if len(tokens) >= 2 else ""
        if sub == "digest":
            digest_sub = tokens[2].lower() if len(tokens) >= 3 else ""
            if digest_sub == "run":
                async for result in self.rss_digest_run(event):
                    yield result
                return
            yield event.plain_result("用法：/rss digest run [digest_id]")
            return

        route_map = {
            "help": self.rss_help,
            "list": self.rss_list,
            "status": self.rss_status,
            "run": self.rss_run,
            "pause": self.rss_pause,
            "resume": self.rss_resume,
            "reset": self.rss_reset,
            "probe": self.rss_probe,
            "test": self.rss_test,
            "test_translate": self.rss_test,
        }

        handler = route_map.get(sub)
        if handler is None:
            yield event.plain_result(self._help_text())
            return

        async for result in handler(event):
            yield result

    async def rss_help(self, event: AstrMessageEvent):
        yield event.plain_result(self._help_text())

    async def rss_probe(self, event: AstrMessageEvent):
        feed_id = self._extract_param(event)
        if not feed_id:
            yield event.plain_result("用法：/rss probe <feed_id>")
            return

        feed = next(
            (
                feed
                for feed in self.scheduler.config.feeds
                if str(feed.id) == feed_id
            ),
            None,
        )
        if feed is None:
            yield event.plain_result("未找到指定来源。")
            return

        if self.source_probe_service is None:
            yield event.plain_result("来源探测服务不可用。")
            return

        async with self._rss_probe_lock:
            report = await self.source_probe_service.probe(feed)
        yield event.plain_result(self._format_probe_report(report.as_dict()))

    async def rss_list(self, event: AstrMessageEvent):
        scheduler = self.scheduler
        config = scheduler.config
        last_results = scheduler.last_results
        paused_jobs = scheduler.paused_jobs

        lines = [
            "RSS 概览：",
            f"- feeds={len(config.feeds)} jobs={len(config.jobs)} targets={len(config.targets)}",
            f"- 日报任务：{len(config.daily_digests)}",
            f"- 运行状态：{'运行中' if scheduler.running else '未运行'}",
            f"- 暂停任务：{', '.join(sorted(paused_jobs)) if paused_jobs else '无'}",
            "",
            "任务列表：",
        ]

        for job in config.jobs:
            result = last_results.get(job.id)
            recent_success = self._format_success_time(result)
            recent_error = self._format_last_error(result)
            job_status = "已暂停" if job.id in paused_jobs else ("启用" if job.enabled else "禁用")
            semantic_status = "开" if getattr(job, "semantic_dedup_enabled", False) else "关"
            lines.append(
                f"- {job.id} [{job_status}] feeds={len(job.feed_ids)} targets={len(job.target_ids)} "
                f"语义判重={semantic_status} 最近成功={recent_success} 最近错误={recent_error}"
            )

        if config.daily_digests:
            lines.extend(["", "日报任务列表："])
            for digest in config.daily_digests:
                status = await scheduler.storage.get_daily_digest_status(digest.id)
                digest_status = "启用" if digest.enabled else "禁用"
                recent_sent = self._format_unix_time(status.get("last_sent_at"))
                recent_error = str(status.get("last_error", "") or "无")
                lines.append(
                    f"- {digest.id} [{digest_status}] feeds={len(digest.feed_ids)} targets={len(digest.target_ids)} "
                    f"send={digest.send_time} 最近发送={recent_sent} 最近错误={recent_error}"
                )

        yield event.plain_result("\n".join(lines))

    async def rss_run(self, event: AstrMessageEvent):
        job_id = self._extract_param(event)
        ok = await self.scheduler.run_job_once(job_id=job_id or None)
        if not ok:
            target = job_id or "全部任务"
            yield event.plain_result(f"手动触发失败：未找到或不可执行任务（{target}）")
            return

        if job_id:
            result = self.scheduler.last_results.get(job_id)
            yield event.plain_result(
                f"已触发任务 {job_id}。最近成功={self._format_success_time(result)} "
                f"最近错误={self._format_last_error(result)}"
            )
            return

        yield event.plain_result("已触发全部启用且未暂停任务。")

    async def rss_test(self, event: AstrMessageEvent):
        sample_text = self._extract_tail_text(event)
        report = await self.scheduler.test_translation(sample_text=sample_text)

        if report.get("error"):
            yield event.plain_result(f"翻译测试失败：{report['error']}")
            return

        config = report.get("config", {})
        llm = report.get("llm", {})
        github = report.get("github", {})
        google = report.get("google", {})

        lines = [
            "翻译链路测试：",
            f"- 输入字符数：{report.get('input_chars', 0)}",
            (
                "- LLM："
                f"enabled={self._bool_text(bool(config.get('llm_enabled', llm.get('enabled', False))))} "
                f"provider={llm.get('provider_id', '') or '(自动/未解析)'} "
                f"timeout={llm.get('timeout_seconds', config.get('llm_timeout_seconds', 0))}s "
                f"proxy={config.get('llm_proxy_mode', 'system')} "
                f"ok={self._bool_text(bool(llm.get('ok', False)))} "
                f"latency={llm.get('latency_ms', 0)}ms "
                f"error={llm.get('error', '') or '-'}"
            ),
            (
                "- Google："
                f"enabled={self._bool_text(bool(config.get('google_translate_enabled', google.get('enabled', False))))} "
                f"target={google.get('target_lang', config.get('google_translate_target_lang', 'zh-CN'))} "
                f"timeout={google.get('timeout_seconds', config.get('google_translate_timeout_seconds', 0))}s "
                f"proxy={config.get('google_translate_proxy_mode', 'system')} "
                f"ok={self._bool_text(bool(google.get('ok', False)))} "
                f"latency={google.get('latency_ms', 0)}ms "
                f"error={google.get('error', '') or '-'}"
            ),
            (
                "- GitHub Models："
                f"enabled={self._bool_text(bool(config.get('github_models_enabled', github.get('enabled', False))))} "
                f"model={github.get('model', config.get('github_models_model', 'openai/gpt-4o-mini'))} "
                f"timeout={github.get('timeout_seconds', config.get('github_models_timeout_seconds', 0))}s "
                f"proxy={config.get('github_models_proxy_mode', 'system')} "
                f"ok={self._bool_text(bool(github.get('ok', False)))} "
                f"latency={github.get('latency_ms', 0)}ms "
                f"error={github.get('error', '') or '-'}"
            ),
        ]

        llm_preview = str(llm.get("preview", "")).strip()
        google_preview = str(google.get("preview", "")).strip()
        github_preview = str(github.get("preview", "")).strip()
        if llm_preview:
            lines.append(f"- LLM结果预览：{llm_preview}")
        if google_preview:
            lines.append(f"- Google结果预览：{google_preview}")
        if github_preview:
            lines.append(f"- GitHub结果预览：{github_preview}")

        yield event.plain_result("\n".join(lines))

    async def rss_reset(self, event: AstrMessageEvent):
        """清空已推送去重记录，便于调试或重新全量推送。"""
        scheduler = self.scheduler
        deleted = await scheduler.storage.clear_seen()
        yield event.plain_result(f"已清空去重记录：{deleted} 条。")

    async def rss_digest_run(self, event: AstrMessageEvent):
        digest_id = self._extract_param_at(event, index=3)
        if not digest_id:
            yield event.plain_result("用法：/rss digest run [digest_id]")
            return

        ok = await self.scheduler.run_daily_digest_once(digest_id)
        if not ok:
            yield event.plain_result(f"日报触发失败：未找到或未启用任务（{digest_id}）")
            return

        result = self.scheduler.digest_results.get(digest_id)
        yield event.plain_result(
            f"已触发日报 {digest_id}。最近执行={self._format_success_time(result)} 最近错误={self._format_last_error(result)}"
        )

    async def rss_status(self, event: AstrMessageEvent):
        scheduler = self.scheduler
        config = scheduler.config
        last_results = scheduler.last_results

        success_times = [
            result.started_at for result in last_results.values() if not result.error_summary
        ]
        recent_success = (
            max(success_times).strftime("%Y-%m-%d %H:%M:%S") if success_times else "暂无"
        )

        errors = [result.error_summary for result in last_results.values() if result.error_summary]
        recent_error = errors[-1] if errors else "无"

        lines = [
            "RSS 状态：",
            f"- 调度器：{'运行中' if scheduler.running else '未运行'}",
            f"- feeds={len(config.feeds)} jobs={len(config.jobs)} targets={len(config.targets)}",
            f"- 日报任务={len(config.daily_digests)}",
            f"- 最近成功：{recent_success}",
            f"- 最近错误：{recent_error}",
        ]
        if config.daily_digests:
            latest_digest_sent = "暂无"
            latest_digest_error = "无"
            digest_sent_times = []
            digest_errors = []
            for digest in config.daily_digests:
                status = await scheduler.storage.get_daily_digest_status(digest.id)
                if status.get("last_sent_at"):
                    digest_sent_times.append(int(status["last_sent_at"]))
                if status.get("last_error"):
                    digest_errors.append(str(status["last_error"]))
            if digest_sent_times:
                latest_digest_sent = self._format_unix_time(max(digest_sent_times))
            if digest_errors:
                latest_digest_error = digest_errors[-1]
            lines.append(f"- 日报最近发送：{latest_digest_sent}")
            lines.append(f"- 日报最近错误：{latest_digest_error}")
        yield event.plain_result("\n".join(lines))

    async def rss_pause(self, event: AstrMessageEvent):
        job_id = self._extract_param(event)
        if not job_id:
            yield event.plain_result("用法：/rss pause [job_id]")
            return

        ok = await self.scheduler.pause_job(job_id)
        if not ok:
            yield event.plain_result(f"暂停失败：任务不存在或未启用（{job_id}）")
            return

        result = self.scheduler.last_results.get(job_id)
        yield event.plain_result(
            f"任务已暂停：{job_id}。最近成功={self._format_success_time(result)} 最近错误={self._format_last_error(result)}"
        )

    async def rss_resume(self, event: AstrMessageEvent):
        job_id = self._extract_param(event)
        if not job_id:
            yield event.plain_result("用法：/rss resume [job_id]")
            return

        ok = self.scheduler.resume_job(job_id)
        if not ok:
            yield event.plain_result(f"恢复失败：任务不存在或未启用（{job_id}）")
            return

        result = self.scheduler.last_results.get(job_id)
        yield event.plain_result(
            f"任务已恢复：{job_id}。最近成功={self._format_success_time(result)} 最近错误={self._format_last_error(result)}"
        )

    @staticmethod
    def _extract_param(event: AstrMessageEvent) -> str:
        message_text = RSSCommands._get_message_text(event)
        tokens = message_text.strip().split()
        return tokens[2].strip() if len(tokens) >= 3 else ""

    @staticmethod
    def _extract_param_at(event: AstrMessageEvent, index: int) -> str:
        message_text = RSSCommands._get_message_text(event)
        tokens = message_text.strip().split()
        return tokens[index].strip() if len(tokens) > index else ""

    @staticmethod
    def _extract_tail_text(event: AstrMessageEvent) -> str:
        message_text = RSSCommands._get_message_text(event)
        parts = message_text.strip().split(maxsplit=2)
        return parts[2].strip() if len(parts) >= 3 else ""

    @staticmethod
    def _get_message_text(event: AstrMessageEvent) -> str:
        if hasattr(event, "message_str"):
            return str(getattr(event, "message_str") or "")
        if hasattr(event, "get_message_str"):
            getter = getattr(event, "get_message_str")
            return str(getter() if callable(getter) else getter or "")
        return ""

    @staticmethod
    def _bool_text(value: bool) -> str:
        return "on" if value else "off"

    @staticmethod
    def _help_text() -> str:
        return (
            "用法：/rss [list|status|run [job_id]|pause [job_id]|resume [job_id]|"
            "reset|probe <feed_id>|test [sample text]|digest run [digest_id]]\n"
            "/rss probe <feed_id>：仅检查连接，不保存设置。"
        )

    @staticmethod
    def _format_probe_report(report: dict[str, Any]) -> str:
        mode_names = {
            "direct_strict",
            "proxy_strict",
            "direct_relaxed",
            "proxy_relaxed",
        }
        feed_kinds = {"rss", "atom", "rdf", "nitter"}
        error_types = {
            "tls_certificate",
            "proxy",
            "dns",
            "connect",
            "timeout",
            "http_status",
            "invalid_feed",
            "unknown",
        }
        recommendation_codes = mode_names | {"invalid_feed", "unreachable"}

        lines = ["来源探测结果："]
        attempts = report.get("attempts", [])
        if not isinstance(attempts, list):
            attempts = []
        for raw_attempt in attempts:
            if not isinstance(raw_attempt, dict):
                continue
            mode = str(raw_attempt.get("mode", ""))
            if mode not in mode_names:
                mode = "unknown"
            ok = raw_attempt.get("ok") is True
            latency = raw_attempt.get("latency_ms")
            if isinstance(latency, bool) or not isinstance(latency, int):
                latency = 0
            latency = max(0, latency)
            http_status = raw_attempt.get("http_status")
            if isinstance(http_status, bool) or not isinstance(http_status, int):
                http_status_text = "-"
            else:
                http_status_text = str(http_status)
            feed_kind = str(raw_attempt.get("feed_kind", ""))
            is_feed = raw_attempt.get("is_feed") is True and feed_kind in feed_kinds
            content_text = f"已识别({feed_kind})" if is_feed else "未识别"
            error_type = str(raw_attempt.get("error_type", ""))
            if ok:
                error_type = "-"
            elif error_type not in error_types:
                error_type = "unknown"
            lines.append(
                f"{mode}：{'成功' if ok else '失败'} "
                f"延迟={latency}ms HTTP={http_status_text} "
                f"内容={content_text} 分类错误={error_type}"
            )

        recommendation = report.get("recommendation", {})
        if not isinstance(recommendation, dict):
            recommendation = {}
        code = str(recommendation.get("code", ""))
        if code not in recommendation_codes:
            code = "unknown"
        verify_ssl = recommendation.get("verify_ssl")
        use_proxy = recommendation.get("use_proxy")
        recommendation_messages = {
            "direct_strict": "默认网络与严格证书校验可用。",
            "proxy_strict": "来源代理与严格证书校验可用。",
            "direct_relaxed": "默认网络仅在关闭证书校验时可用，存在证书安全隐患。",
            "proxy_relaxed": "来源代理仅在关闭证书校验时可用，存在证书安全隐患。",
            "invalid_feed": "来源可访问，但响应内容无法识别为订阅源。",
            "unreachable": "所有探测模式均失败，请根据分类错误检查来源配置。",
            "unknown": "无法生成建议。",
        }
        if code in {"direct_strict", "proxy_strict"} and verify_ssl is None:
            recommendation_message = "HTTP 来源可用，TLS 不适用。"
        else:
            recommendation_message = recommendation_messages[code]
        lines.append(
            f"建议：{recommendation_message} "
            f"code={code} "
            f"verify_ssl={RSSCommands._optional_bool_text(verify_ssl)} "
            f"use_proxy={RSSCommands._optional_bool_text(use_proxy)}"
        )
        return "\n".join(lines)

    @staticmethod
    def _optional_bool_text(value: Any) -> str:
        if value is True:
            return "true"
        if value is False:
            return "false"
        return "n/a"

    @staticmethod
    def _format_success_time(result) -> str:
        if result is None or result.error_summary:
            return "暂无"
        return result.started_at.strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def _format_last_error(result) -> str:
        if result is None or not result.error_summary:
            return "无"
        return result.error_summary

    @staticmethod
    def _format_unix_time(raw_value) -> str:
        try:
            timestamp_value = int(raw_value or 0)
        except (TypeError, ValueError):
            return "暂无"
        if timestamp_value <= 0:
            return "暂无"
        from datetime import datetime

        return datetime.fromtimestamp(timestamp_value).strftime("%Y-%m-%d %H:%M:%S")
