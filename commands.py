from astrbot.api.event import AstrMessageEvent


class RSSCommands:
    """命令入口。"""

    scheduler = None

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
        route_map = {
            "list": self.rss_list,
            "status": self.rss_status,
            "run": self.rss_run,
            "pause": self.rss_pause,
            "resume": self.rss_resume,
            "reset": self.rss_reset,
            "test": self.rss_test,
            "test_translate": self.rss_test,
        }

        handler = route_map.get(sub)
        if handler is None:
            yield event.plain_result(
                "用法：/rss [list|status|run [job_id]|pause [job_id]|resume [job_id]|reset|test [sample text]]"
            )
            return

        async for result in handler(event):
            yield result

    async def rss_list(self, event: AstrMessageEvent):
        scheduler = self.scheduler
        config = scheduler.config
        last_results = scheduler.last_results
        paused_jobs = scheduler.paused_jobs

        lines = [
            "RSS 概览：",
            f"- feeds={len(config.feeds)} jobs={len(config.jobs)} targets={len(config.targets)}",
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
            lines.append(
                f"- {job.id} [{job_status}] feeds={len(job.feed_ids)} targets={len(job.target_ids)} "
                f"最近成功={recent_success} 最近错误={recent_error}"
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
            f"- 最近成功：{recent_success}",
            f"- 最近错误：{recent_error}",
        ]
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
    def _format_success_time(result) -> str:
        if result is None or result.error_summary:
            return "暂无"
        return result.started_at.strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def _format_last_error(result) -> str:
        if result is None or not result.error_summary:
            return "无"
        return result.error_summary
