import importlib.util
import sys
import types
import unittest
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


class _Logger:
    def __init__(self) -> None:
        self.warnings = []

    def info(self, *args, **kwargs):
        return None

    def warning(self, message, *args, **kwargs):
        self.warnings.append(message % args if args else str(message))

    def error(self, *args, **kwargs):
        return None


class _Context:
    def __init__(self, config) -> None:
        self.config = config
        self.registered_web_apis = []

    def get_config(self):
        return {}

    def register_web_api(self, route, handler, methods, desc):
        self.registered_web_apis.append((route, handler, methods, desc))


def _load_main(package_name: str):
    logger = _Logger()
    active_config = types.SimpleNamespace(timezone="Asia/Shanghai", feeds=[])

    astrbot_module = types.ModuleType("astrbot")
    astrbot_api_module = types.ModuleType("astrbot.api")
    astrbot_api_module.logger = logger
    event_module = types.ModuleType("astrbot.api.event")
    event_module.AstrMessageEvent = object
    event_module.filter = types.SimpleNamespace(
        regex=lambda _pattern: (lambda handler: handler)
    )
    star_module = types.ModuleType("astrbot.api.star")

    class Star:
        def __init__(self, context, config=None):
            self.context = context
            self.config = config

    star_module.Context = _Context
    star_module.Star = Star
    star_module.register = lambda *_args, **_kwargs: (lambda cls: cls)
    sys.modules["astrbot"] = astrbot_module
    sys.modules["astrbot.api"] = astrbot_api_module
    sys.modules["astrbot.api.event"] = event_module
    sys.modules["astrbot.api.star"] = star_module

    package = types.ModuleType(package_name)
    package.__path__ = [str(ROOT)]
    sys.modules[package_name] = package

    class RSSCommands:
        def __init__(self, source_probe_service=None):
            self.source_probe_service = source_probe_service

        async def rss_router(self, _event):
            if False:
                yield None

    class RSSConfig:
        @classmethod
        def from_context(cls, _source):
            return active_config

    class DummyComponent:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

    class RSSScheduler(DummyComponent):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.config = kwargs["config"]
            self.started = False
            self.stopped = False

        async def start(self):
            self.started = True

        async def stop(self):
            self.stopped = True

    class SourceProbeService:
        pass

    stubs = {
        "commands": {"RSSCommands": RSSCommands},
        "config": {"RSSConfig": RSSConfig},
        "dispatcher": {"FeedDispatcher": DummyComponent},
        "fetcher": {"FeedFetcher": DummyComponent},
        "parser": {"FeedParser": DummyComponent},
        "pipeline": {"FeedPipeline": DummyComponent},
        "scheduler": {"RSSScheduler": RSSScheduler},
        "semantic_dedup": {"SemanticDedupService": DummyComponent},
        "storage": {"FeedStorage": DummyComponent},
        "source_probe": {"SourceProbeService": SourceProbeService},
    }
    for name, attributes in stubs.items():
        module = types.ModuleType(f"{package_name}.{name}")
        for attribute, value in attributes.items():
            setattr(module, attribute, value)
        sys.modules[module.__name__] = module

    spec = spec_from_file_location(f"{package_name}.main", ROOT / "main.py")
    module = module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module, logger, active_config, SourceProbeService


class SourceProbeCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_supported_web_api_registers_and_keeps_shared_service(self):
        package_name = "astrbot_source_probe_compat_present"
        main_module, _logger, config, service_type = _load_main(package_name)
        registrations = []

        class SourceProbeApi:
            def __init__(self, api_config, service):
                self.config = api_config
                self.service = service

            def register(self, context):
                registrations.append((self, context))

        api_module = types.ModuleType(f"{package_name}.source_probe_api")
        api_module.SourceProbeApi = SourceProbeApi
        sys.modules[api_module.__name__] = api_module
        context = _Context(config)

        with mock.patch.object(importlib.util, "find_spec", return_value=object()) as find_spec:
            plugin = main_module.RSSPlugin(context, config={})
        await plugin.initialize()

        find_spec.assert_called_once_with("astrbot.api.web")
        self.assertIsInstance(plugin.source_probe_service, service_type)
        self.assertIs(plugin._source_probe_api.service, plugin.source_probe_service)
        self.assertIs(plugin._source_probe_api.config, plugin.scheduler.config)
        self.assertEqual(registrations, [(plugin._source_probe_api, context)])
        self.assertTrue(plugin.scheduler.started)
        self.assertIsInstance(plugin, main_module.RSSCommands)

    async def test_missing_web_api_warns_once_and_keeps_shared_service(self):
        package_name = "astrbot_source_probe_compat_missing"
        main_module, logger, config, service_type = _load_main(package_name)
        context = _Context(config)

        with mock.patch.object(importlib.util, "find_spec", return_value=None) as find_spec:
            plugin = main_module.RSSPlugin(context, config={})
        await plugin.initialize()

        find_spec.assert_called_once_with("astrbot.api.web")
        self.assertIsNone(plugin._source_probe_api)
        self.assertIsInstance(plugin.source_probe_service, service_type)
        self.assertTrue(plugin.scheduler.started)
        self.assertIsInstance(plugin, main_module.RSSCommands)
        self.assertEqual(len(logger.warnings), 1)
        self.assertIn("astrbot.api.web", logger.warnings[0])

    async def test_supported_web_api_does_not_hide_adapter_import_errors(self):
        package_name = "astrbot_source_probe_compat_broken"
        main_module, _logger, config, _service_type = _load_main(package_name)
        broken_module = types.ModuleType(f"{package_name}.source_probe_api")

        def broken_attribute(_name):
            raise ImportError("source probe adapter import failed")

        broken_module.__getattr__ = broken_attribute
        sys.modules[broken_module.__name__] = broken_module

        with mock.patch.object(importlib.util, "find_spec", return_value=object()):
            with self.assertRaisesRegex(ImportError, "adapter import failed"):
                main_module.RSSPlugin(_Context(config), config={})


if __name__ == "__main__":
    unittest.main()
