import asyncio

from astrbot.api.web import error_response, json_response, request

from .subscription_discovery import discover_feed
from .subscription_management import safe_validation_error


class SubscriptionApi:
    def __init__(self, service, *, proxy_url="", ready=None):
        self.service = service
        self.proxy_url = proxy_url
        self.ready = ready or (lambda: True)

    def register(self, context):
        for route, handler, method in [
            ("state", self.state, "GET"),
            ("discover", self.discover, "POST"),
            ("create", self.create, "POST"),
            ("toggle", self.toggle, "POST"),
        ]:
            context.register_web_api(
                f"/astrbot_plugin_rss_forwarder/subscriptions/{route}",
                handler, [method], f"RSS subscription {route}",
            )

    async def state(self):
        if not request.username:
            return error_response("需要登录 AstrBot 管理面板", status_code=403)
        return json_response(self.service.snapshot())

    async def _body(self, allowed):
        if not request.username:
            raise PermissionError("需要登录 AstrBot 管理面板")
        if not self.ready():
            raise ValueError("订阅管理尚未就绪，请稍后重试")
        body = await request.json(default={})
        if not self.ready():
            raise ValueError("订阅管理尚未就绪，请稍后重试")
        if not isinstance(body, dict) or set(body) - allowed:
            raise ValueError("请求字段无效")
        return body

    async def discover(self):
        try:
            body = await self._body({"url"})
            result = await asyncio.wait_for(discover_feed(body.get("url", ""), proxy_url=self.proxy_url), 45)
            return json_response(result)
        except PermissionError as exc:
            return error_response(str(exc), status_code=403)
        except Exception:
            return error_response("无法识别订阅源，请检查地址和网络设置", status_code=400)

    async def create(self):
        try:
            body = await self._body({"url", "target_ids", "interval_seconds", "cron", "title", "expected_revision"})
            self._require_revision(body)
            if not isinstance(body.get("target_ids"), list) or not body["target_ids"]:
                raise ValueError("请至少选择一个已配置的推送目标")
            result = await asyncio.wait_for(discover_feed(body.get("url", ""), proxy_url=self.proxy_url), 45)
            body["url"] = result["url"]
            if not body.get("title"):
                body["title"] = result["title"]
            return json_response(await self.service.apply_subscription(**body))
        except PermissionError as exc:
            return error_response(str(exc), status_code=403)
        except (ValueError, TypeError) as exc:
            return error_response(safe_validation_error(exc), status_code=400)
        except Exception:
            return error_response("订阅保存失败，请刷新后重试", status_code=400)

    async def toggle(self):
        try:
            body = await self._body({"kind", "id", "enabled", "expected_revision"})
            self._require_revision(body)
            return json_response(await self.service.set_enabled(**body))
        except PermissionError as exc:
            return error_response(str(exc), status_code=403)
        except (ValueError, TypeError) as exc:
            return error_response(safe_validation_error(exc), status_code=400)
        except Exception:
            return error_response("状态更新失败，请刷新后重试", status_code=400)

    def _require_revision(self, body):
        revision = body.get("expected_revision")
        if not isinstance(revision, str) or not revision:
            raise ValueError("缺少配置修订号，请刷新后重试")
        if revision != self.service.snapshot()["revision"]:
            raise ValueError("配置已发生变化，请刷新后重试")
