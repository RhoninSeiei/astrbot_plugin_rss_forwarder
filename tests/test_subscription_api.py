import contextvars
import copy
import sys
import types
import unittest
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch


class _RequestStub:
    def __init__(self):
        self._body = contextvars.ContextVar("subscription_api_body", default=None)
        self._username = contextvars.ContextVar("subscription_api_username", default=None)
        self.json_calls = Mock()

    @property
    def username(self):
        return self._username.get()

    async def json(self, default=None):
        self.json_calls(default=default)
        return self._body.get()

    async def invoke(self, handler, body, username):
        body_token = self._body.set(copy.deepcopy(body))
        username_token = self._username.set(username)
        try:
            return await handler()
        finally:
            self._username.reset(username_token)
            self._body.reset(body_token)


request_stub = _RequestStub()


def _json_response(data, status_code=200):
    return {"status_code": status_code, "data": data}


def _error_response(message, status_code=400):
    return {"status_code": status_code, "message": message}


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "astrbot_subscription_api_testpkg"
package_module = types.ModuleType(PACKAGE_NAME)
package_module.__path__ = [str(ROOT)]
astrbot_module = types.ModuleType("astrbot")
astrbot_api_module = types.ModuleType("astrbot.api")
astrbot_api_module.logger = types.SimpleNamespace(info=lambda *args: None, warning=lambda *args: None)
astrbot_web_module = types.ModuleType("astrbot.api.web")
astrbot_web_module.request = request_stub
astrbot_web_module.json_response = _json_response
astrbot_web_module.error_response = _error_response


def _load_module(name):
    full_name = f"{PACKAGE_NAME}.{name}"
    spec = spec_from_file_location(full_name, ROOT / f"{name}.py")
    module = module_from_spec(spec)
    sys.modules[full_name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


# Keep the API's stub bindings without replacing other tests' AstrBot modules.
with patch.dict(sys.modules, {
    "astrbot": astrbot_module,
    "astrbot.api": astrbot_api_module,
    "astrbot.api.web": astrbot_web_module,
    PACKAGE_NAME: package_module,
}):
    subscription_api_module = _load_module("subscription_api")

SubscriptionApi = subscription_api_module.SubscriptionApi


class SubscriptionApiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        request_stub.json_calls.reset_mock()
        self.snapshot = {
            "revision": "revision-1",
            "feeds": [{"id": "feed-1", "url": "https://example.com/feed"}],
            "targets": [{"id": "group", "enabled": True}],
            "jobs": [{"id": "job-1", "feed_ids": ["feed-1"]}],
            "daily_digests": [],
        }
        self.created = {"feed_id": "feed-2", "job_id": "job-2", "revision": "revision-2"}
        self.service = types.SimpleNamespace(
            snapshot=Mock(return_value=self.snapshot),
            apply_subscription=AsyncMock(return_value=self.created),
            set_enabled=AsyncMock(return_value={"revision": "revision-3"}),
        )
        self.api = SubscriptionApi(self.service, proxy_url="http://proxy.example:8080")
        self.discovered = {
            "url": "https://example.com/feed.xml",
            "title": "Discovered News",
            "sample_titles": ["First item"],
        }
        self.discovery = AsyncMock(return_value=self.discovered)
        patcher = patch.object(subscription_api_module, "discover_feed", self.discovery)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _create_body(self, **overrides):
        body = {
            "url": "https://example.com/",
            "target_ids": ["group", "private"],
            "interval_seconds": 300,
            "cron": "",
            "expected_revision": "revision-1",
        }
        body.update(overrides)
        return body

    async def _invoke(self, endpoint, body=None, username="operator"):
        return await request_stub.invoke(getattr(self.api, endpoint), body, username)

    def _assert_no_service_calls(self):
        self.service.snapshot.assert_not_called()
        self._assert_no_mutation_calls()

    def _assert_no_mutation_calls(self):
        self.service.apply_subscription.assert_not_called()
        self.service.set_enabled.assert_not_called()

    def test_register_exposes_expected_routes_methods_and_handlers(self):
        context = Mock()

        self.api.register(context)

        self.assertEqual(context.register_web_api.call_count, 4)
        self.assertEqual(
            [call.args[:3] for call in context.register_web_api.call_args_list],
            [
                (f"/astrbot_plugin_rss_forwarder/subscriptions/{name}",
                 getattr(self.api, name), [method])
                for name, method in (
                    ("state", "GET"), ("discover", "POST"),
                    ("create", "POST"), ("toggle", "POST"),
                )
            ],
        )

    async def test_all_endpoints_require_login_before_reading_body_or_calling_services(self):
        for endpoint in ("state", "discover", "create", "toggle"):
            for username in (None, ""):
                with self.subTest(endpoint=endpoint, username=username):
                    response = await self._invoke(endpoint, ["invalid body"], username)

                    self.assertEqual(response, {
                        "status_code": 403, "message": "需要登录 AstrBot 管理面板",
                    })
                    request_stub.json_calls.assert_not_called()
                    self.discovery.assert_not_called()
                    self._assert_no_service_calls()

    async def test_state_returns_authenticated_service_snapshot(self):
        response = await self._invoke("state")

        self.assertEqual(response, {"status_code": 200, "data": self.snapshot})
        self.service.snapshot.assert_called_once_with()
        request_stub.json_calls.assert_not_called()
        self.discovery.assert_not_called()
        self.service.apply_subscription.assert_not_called()
        self.service.set_enabled.assert_not_called()

    async def test_discover_returns_metadata_and_uses_configured_proxy(self):
        response = await self._invoke("discover", {"url": "https://example.com/"})

        self.assertEqual(response, {"status_code": 200, "data": self.discovered})
        self.discovery.assert_awaited_once_with(
            "https://example.com/", proxy_url="http://proxy.example:8080",
        )
        self._assert_no_service_calls()

    async def test_post_endpoints_reject_non_object_json_before_side_effects(self):
        for endpoint in ("discover", "create", "toggle"):
            for body in (None, [], ["url"], "url", 1, False):
                with self.subTest(endpoint=endpoint, body=body):
                    response = await self._invoke(endpoint, body)

                    self.assertEqual(response["status_code"], 400)
                    self.assertTrue(response["message"])
                    self.discovery.assert_not_called()
                    self._assert_no_service_calls()

    async def test_post_endpoints_reject_fields_outside_each_whitelist(self):
        requests = {
            "discover": {"url": "https://example.com/"},
            "create": self._create_body(),
            "toggle": {"kind": "jobs", "id": "job-1", "enabled": False,
                       "expected_revision": "revision-1"},
        }
        for endpoint, body in requests.items():
            for field in ("proxy_url", "username", "current_origin"):
                with self.subTest(endpoint=endpoint, field=field):
                    response = await self._invoke(endpoint, {**body, field: "injected"})

                    self.assertEqual(response["status_code"], 400)
                    self.discovery.assert_not_called()
                    self._assert_no_service_calls()

    async def test_whitelists_do_not_accept_fields_from_other_endpoints(self):
        for endpoint, body in (
            ("discover", {"url": "https://example.com/", "expected_revision": "revision-1"}),
            ("create", self._create_body(enabled=True)),
            ("toggle", {"kind": "feeds", "id": "feed-1", "enabled": True, "url": "https://example.com/"}),
        ):
            with self.subTest(endpoint=endpoint):
                response = await self._invoke(endpoint, body)

                self.assertEqual(response["status_code"], 400)
                self.discovery.assert_not_called()
                self._assert_no_service_calls()

    async def test_create_and_toggle_require_nonempty_string_revision_before_side_effects(self):
        requests = {
            "create": self._create_body(),
            "toggle": {
                "kind": "jobs", "id": "job-1", "enabled": False,
                "expected_revision": "revision-1",
            },
        }
        revisions = [{}] + [
            {"expected_revision": value}
            for value in (None, "", " \t ", 0, 1, False, True, [], {}, ["revision-1"])
        ]
        for endpoint, body in requests.items():
            body = dict(body)
            body.pop("expected_revision")
            for revision in revisions:
                with self.subTest(endpoint=endpoint, revision=revision):
                    response = await self._invoke(endpoint, {**body, **revision})

                    self.assertEqual(response["status_code"], 400)
                    self.assertIsInstance(response["message"], str)
                    self.assertTrue(response["message"])
                    self.discovery.assert_not_called()
                    self._assert_no_mutation_calls()

    async def test_create_requires_explicit_nonempty_target_list_before_discovery(self):
        body = self._create_body()
        body.pop("target_ids")
        targets = [{}] + [
            {"target_ids": value}
            for value in (None, [], "", "group", {}, {"group": True}, False, True, 0, 1)
        ]
        for target in targets:
            with self.subTest(target=target):
                response = await self._invoke("create", {**body, **target})

                self.assertEqual(response["status_code"], 400)
                self.assertIsInstance(response["message"], str)
                self.assertTrue(response["message"])
                self.discovery.assert_not_called()
                self._assert_no_mutation_calls()

    async def test_web_create_cannot_use_current_session_instead_of_explicit_targets(self):
        for session in (
            {"current_origin": "qq:group:1"},
            {"current_platform": "qq"},
            {"current_origin": "qq:group:1", "current_platform": "qq"},
        ):
            with self.subTest(session=session):
                response = await self._invoke("create", {
                    **self._create_body(target_ids=[]), **session,
                })

                self.assertEqual(response["status_code"], 400)
                self.assertTrue(response["message"])
                self.discovery.assert_not_called()
                self._assert_no_service_calls()

    async def test_stale_revision_rejects_create_and_toggle_before_side_effects(self):
        for endpoint, body in (
            ("create", self._create_body(expected_revision="old-revision")),
            ("toggle", {
                "kind": "feeds", "id": "feed-1", "enabled": True,
                "expected_revision": "old-revision",
            }),
        ):
            with self.subTest(endpoint=endpoint):
                self.service.snapshot.reset_mock()

                response = await self._invoke(endpoint, body)

                self.assertEqual(response["status_code"], 400)
                self.assertTrue(response["message"])
                self.service.snapshot.assert_called_once_with()
                self.discovery.assert_not_called()
                self._assert_no_mutation_calls()

    async def test_create_accepts_single_explicit_target(self):
        response = await self._invoke("create", self._create_body(target_ids=["group"]))

        self.assertEqual(response, {"status_code": 200, "data": self.created})
        self.service.apply_subscription.assert_awaited_once_with(
            url="https://example.com/feed.xml", title="Discovered News",
            target_ids=["group"], interval_seconds=300, cron="",
            expected_revision="revision-1",
        )

    async def test_create_resolves_feed_url_and_defaults_title_before_saving(self):
        body = self._create_body()

        response = await self._invoke("create", body)

        self.assertEqual(response, {"status_code": 200, "data": self.created})
        self.discovery.assert_awaited_once_with(
            "https://example.com/", proxy_url="http://proxy.example:8080",
        )
        self.service.apply_subscription.assert_awaited_once_with(
            url="https://example.com/feed.xml",
            title="Discovered News",
            target_ids=["group", "private"],
            interval_seconds=300,
            cron="",
            expected_revision="revision-1",
        )
        self.service.set_enabled.assert_not_called()
        self.service.snapshot.assert_called_once_with()

    async def test_create_preserves_custom_title_and_defaults_empty_title(self):
        for title, expected_title in (("Custom title", "Custom title"), ("", "Discovered News")):
            with self.subTest(title=title):
                self.service.apply_subscription.reset_mock()

                response = await self._invoke("create", self._create_body(title=title))

                self.assertEqual(response, {"status_code": 200, "data": self.created})
                self.service.apply_subscription.assert_awaited_once_with(
                    url="https://example.com/feed.xml", title=expected_title,
                    target_ids=["group", "private"], interval_seconds=300, cron="",
                    expected_revision="revision-1",
                )

    async def test_create_forwards_cron_schedule_and_revision_unchanged(self):
        self.snapshot["revision"] = "cron-revision"
        response = await self._invoke("create", self._create_body(
            interval_seconds=0, cron="0 9 * * 1-5", expected_revision="cron-revision",
        ))

        self.assertEqual(response, {"status_code": 200, "data": self.created})
        self.service.apply_subscription.assert_awaited_once_with(
            url="https://example.com/feed.xml", title="Discovered News",
            target_ids=["group", "private"], interval_seconds=0, cron="0 9 * * 1-5",
            expected_revision="cron-revision",
        )

    async def test_create_does_not_save_when_discovery_fails(self):
        for error, message in (
            (ValueError("feed not found"), "feed not found"),
            (TypeError("invalid address type"), "invalid address type"),
            (TimeoutError("private timeout details"), "订阅保存失败，请刷新后重试"),
            (RuntimeError("private transport details"), "订阅保存失败，请刷新后重试"),
        ):
            with self.subTest(error=type(error).__name__):
                self.discovery.reset_mock()
                self.discovery.side_effect = error

                response = await self._invoke("create", self._create_body())

                self.assertEqual(response, {"status_code": 400, "message": message})
                self.discovery.assert_awaited_once_with(
                    "https://example.com/", proxy_url="http://proxy.example:8080",
                )
                self._assert_no_mutation_calls()

    async def test_create_returns_service_validation_errors_and_revision_conflicts(self):
        for error in (ValueError("configuration revision conflict"), TypeError("invalid interval")):
            with self.subTest(error=type(error).__name__):
                self.service.apply_subscription.reset_mock()
                self.service.apply_subscription.side_effect = error

                response = await self._invoke("create", self._create_body())

                self.assertEqual(response, {"status_code": 400, "message": str(error)})
                self.service.apply_subscription.assert_awaited_once()
                self.service.set_enabled.assert_not_called()

    async def test_create_hides_unexpected_service_failure_details(self):
        self.service.apply_subscription.side_effect = RuntimeError("database password=secret")

        response = await self._invoke("create", self._create_body())

        self.assertEqual(response, {"status_code": 400, "message": "订阅保存失败，请刷新后重试"})
        self.service.apply_subscription.assert_awaited_once()
        self.assertNotIn("secret", str(response))

    async def test_toggle_forwards_all_supported_kinds_and_boolean_states(self):
        for kind, item_id in (("feeds", "feed-1"), ("jobs", "job-1"), ("daily_digests", "digest-1")):
            for enabled in (False, True):
                with self.subTest(kind=kind, enabled=enabled):
                    self.service.set_enabled.reset_mock()
                    body = {"kind": kind, "id": item_id, "enabled": enabled,
                            "expected_revision": "revision-1"}

                    response = await self._invoke("toggle", body)

                    self.assertEqual(response, {"status_code": 200, "data": {"revision": "revision-3"}})
                    self.service.set_enabled.assert_awaited_once_with(**body)
                    self.discovery.assert_not_called()
                    self.service.apply_subscription.assert_not_called()

    async def test_toggle_returns_service_validation_errors_and_revision_conflicts(self):
        self.snapshot["revision"] = "old-revision"
        for error in (ValueError("configuration revision conflict"), TypeError("enabled must be a boolean")):
            with self.subTest(error=type(error).__name__):
                self.service.set_enabled.reset_mock()
                self.service.set_enabled.side_effect = error

                response = await self._invoke("toggle", {
                    "kind": "jobs", "id": "job-1", "enabled": False,
                    "expected_revision": "old-revision",
                })

                self.assertEqual(response, {"status_code": 400, "message": str(error)})
                self.service.set_enabled.assert_awaited_once_with(
                    kind="jobs", id="job-1", enabled=False, expected_revision="old-revision",
                )
                self.discovery.assert_not_called()

    async def test_toggle_hides_unexpected_service_failure_details(self):
        self.service.set_enabled.side_effect = RuntimeError("private configuration path")

        response = await self._invoke("toggle", {
            "kind": "feeds", "id": "feed-1", "enabled": True,
            "expected_revision": "revision-1",
        })

        self.assertEqual(response, {"status_code": 400, "message": "状态更新失败，请刷新后重试"})
        self.service.set_enabled.assert_awaited_once()

    async def test_invalid_saved_url_is_redacted_in_validation_error(self):
        self.service.set_enabled.side_effect = ValueError('invalid URL ftp://user:password@example.com/feed ?token=secret')
        response = await self._invoke("toggle", {
            "kind": "feeds", "id": "feed-1", "enabled": True,
            "expected_revision": "revision-1",
        })
        self.assertEqual(response["status_code"], 400)
        self.assertNotIn("password", response["message"])
        self.assertNotIn("secret", response["message"])

    async def test_shutdown_rejects_before_discovery_or_mutation(self):
        self.api.ready = lambda: False
        response = await self._invoke("create", self._create_body())
        self.assertEqual(response["status_code"], 400)
        self.discovery.assert_not_called()
        self._assert_no_mutation_calls()

    async def test_discover_hides_discovery_errors_and_timeout_details(self):
        for error in (ValueError("internal DNS details"), TypeError("internal URL details"),
                      TimeoutError("private timeout details"), RuntimeError("private proxy details")):
            with self.subTest(error=type(error).__name__):
                self.discovery.reset_mock()
                self.discovery.side_effect = error

                response = await self._invoke("discover", {"url": "https://example.com/"})

                self.assertEqual(response, {
                    "status_code": 400, "message": "无法识别订阅源，请检查地址和网络设置",
                })
                self.discovery.assert_awaited_once()
                self._assert_no_service_calls()

    async def test_post_endpoints_return_permission_errors_as_forbidden(self):
        for endpoint, dependency, body in (
            ("discover", self.discovery, {"url": "https://example.com/"}),
            ("create", self.service.apply_subscription, self._create_body()),
            ("toggle", self.service.set_enabled, {
                "kind": "jobs", "id": "job-1", "enabled": False,
                "expected_revision": "revision-1",
            }),
        ):
            with self.subTest(endpoint=endpoint):
                dependency.reset_mock()
                dependency.side_effect = PermissionError("operation forbidden")
                try:
                    response = await self._invoke(endpoint, body)

                    self.assertEqual(response, {"status_code": 403, "message": "operation forbidden"})
                    dependency.assert_awaited_once()
                finally:
                    dependency.side_effect = None

    async def test_invalid_json_returns_error_without_discovery_or_service_calls(self):
        for endpoint in ("discover", "create", "toggle"):
            with self.subTest(endpoint=endpoint):
                with patch.object(request_stub, "json", AsyncMock(side_effect=ValueError("invalid JSON"))):
                    response = await self._invoke(endpoint)

                self.assertEqual(response["status_code"], 400)
                self.assertEqual(response["message"],
                                 "无法识别订阅源，请检查地址和网络设置" if endpoint == "discover" else "invalid JSON")
                self.discovery.assert_not_called()
                self._assert_no_service_calls()


if __name__ == "__main__":
    unittest.main()
