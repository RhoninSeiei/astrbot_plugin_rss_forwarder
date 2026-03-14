import sys
import types
import unittest
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


astrbot_module = types.ModuleType("astrbot")
astrbot_api_module = types.ModuleType("astrbot.api")
astrbot_api_module.logger = types.SimpleNamespace(info=lambda *a, **k: None)
sys.modules.setdefault("astrbot", astrbot_module)
sys.modules["astrbot.api"] = astrbot_api_module

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "astrbot_rss_testpkg"
package_module = types.ModuleType(PACKAGE_NAME)
package_module.__path__ = [str(ROOT)]
sys.modules[PACKAGE_NAME] = package_module


def _load_module(module_name: str):
    full_name = f"{PACKAGE_NAME}.{module_name}"
    spec = spec_from_file_location(full_name, ROOT / f"{module_name}.py")
    module = module_from_spec(spec)
    sys.modules[full_name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


_load_module("config")
_load_module("dispatcher")
_load_module("fetcher")
_load_module("parser")
_load_module("pipeline")
_load_module("storage")
RSSScheduler = _load_module("scheduler").RSSScheduler


class RSSSchedulerTests(unittest.TestCase):
    def test_history_items_are_suppressed_when_older_than_last_success(self):
        item = {
            "feed_id": "feed-1",
            "published_at": "2026-03-15T00:00:00+00:00",
        }
        feed_state_map = {"feed-1": {"last_success_time": 1773536400}}

        self.assertTrue(RSSScheduler._should_mark_history_only(item, feed_state_map))

    def test_newer_items_are_not_suppressed(self):
        item = {
            "feed_id": "feed-1",
            "published_at": "2026-03-15T03:00:01+00:00",
        }
        feed_state_map = {"feed-1": {"last_success_time": 1773536400}}

        self.assertFalse(RSSScheduler._should_mark_history_only(item, feed_state_map))

    def test_items_without_timestamp_are_not_suppressed(self):
        item = {"feed_id": "feed-1", "published_at": ""}
        feed_state_map = {"feed-1": {"last_success_time": 1773536400}}

        self.assertFalse(RSSScheduler._should_mark_history_only(item, feed_state_map))


if __name__ == "__main__":
    unittest.main()
