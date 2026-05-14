import ast
import json
import sys
import types
import unittest
from pathlib import Path

astrbot_module = types.ModuleType("astrbot")
astrbot_api_module = types.ModuleType("astrbot.api")
astrbot_api_module.logger = types.SimpleNamespace(
    info=lambda *a, **k: None,
    warning=lambda *a, **k: None,
    error=lambda *a, **k: None,
)
sys.modules.setdefault("astrbot", astrbot_module)
sys.modules["astrbot.api"] = astrbot_api_module

from config import ConfigValidationError, RSSConfig


def _minimal_runtime_conf():
    return {
        "feeds": [{"id": "feed-1", "url": "https://example.com/rss", "enabled": True}],
        "targets": [
            {
                "id": "target-1",
                "platform": "qq",
                "unified_msg_origin": "qq:group:1",
                "enabled": True,
            }
        ],
        "jobs": [
            {
                "id": "job-1",
                "feed_ids": ["feed-1"],
                "target_ids": ["target-1"],
                "interval_seconds": 300,
                "enabled": True,
            }
        ],
    }


class ConfigTranslationTests(unittest.TestCase):
    def test_legacy_timeout_maps_to_llm_timeout(self):
        conf = _minimal_runtime_conf()
        conf.update(
            {
                "llm_enabled": True,
                "timeout": 21,
            }
        )

        cfg = RSSConfig.from_context(conf)

        self.assertEqual(cfg.llm_timeout_seconds, 21)
        self.assertEqual(cfg.timeout, 21)

    def test_translation_section_overrides_legacy_keys(self):
        conf = _minimal_runtime_conf()
        conf.update(
            {
                "timeout": 21,
                "translation": {
                    "llm_enabled": True,
                    "llm_timeout_seconds": 9,
                    "llm_provider_id": "provider-A",
                    "llm_proxy_mode": "custom",
                    "llm_proxy_url": "http://127.0.0.1:7891",
                    "github_models_enabled": True,
                    "github_models_model": "openai/gpt-4o-mini",
                    "github_models_timeout_seconds": 13,
                    "github_models_token_file": "tokens/github.token",
                    "github_models_proxy_mode": "custom",
                    "github_models_proxy_url": "http://127.0.0.1:7892",
                    "google_translate_enabled": True,
                    "google_translate_api_key": "k",
                    "google_translate_target_lang": "ja",
                    "google_translate_timeout_seconds": 11,
                    "google_translate_proxy_mode": "custom",
                    "google_translate_proxy_url": "http://127.0.0.1:7890",
                },
            }
        )

        cfg = RSSConfig.from_context(conf)

        self.assertTrue(cfg.llm_enabled)
        self.assertEqual(cfg.llm_timeout_seconds, 9)
        self.assertEqual(cfg.llm_provider_id, "provider-A")
        self.assertEqual(cfg.llm_proxy_mode, "custom")
        self.assertEqual(cfg.llm_proxy_url, "http://127.0.0.1:7891")
        self.assertTrue(cfg.github_models_enabled)
        self.assertEqual(cfg.github_models_model, "openai/gpt-4o-mini")
        self.assertEqual(cfg.github_models_timeout_seconds, 13)
        self.assertEqual(cfg.github_models_token_file, "tokens/github.token")
        self.assertEqual(cfg.github_models_proxy_mode, "custom")
        self.assertEqual(cfg.github_models_proxy_url, "http://127.0.0.1:7892")
        self.assertTrue(cfg.google_translate_enabled)
        self.assertEqual(cfg.google_translate_api_key, "k")
        self.assertEqual(cfg.google_translate_target_lang, "ja")
        self.assertEqual(cfg.google_translate_timeout_seconds, 11)
        self.assertEqual(cfg.google_translate_proxy_mode, "custom")
        self.assertEqual(cfg.google_translate_proxy_url, "http://127.0.0.1:7890")

    def test_daily_digest_parses_and_preserves_no_implicit_job(self):
        conf = {
            "feeds": [{"id": "feed-1", "url": "https://example.com/rss", "enabled": True}],
            "targets": [
                {
                    "id": "target-1",
                    "platform": "qq",
                    "unified_msg_origin": "qq:group:1",
                    "enabled": True,
                }
            ],
            "jobs": [],
            "daily_digests": [
                {
                    "id": "digest-1",
                    "feed_ids": ["feed-1"],
                    "target_ids": ["target-1"],
                    "send_time": "09:00",
                    "window_hours": 24,
                    "max_items": 20,
                    "llm_timeout_seconds": 90,
                    "semantic_merge_enabled": True,
                    "semantic_merge_provider_id": "provider-digest",
                    "semantic_merge_max_candidates": 12,
                    "semantic_merge_min_confidence": 0.76,
                    "render_mode": "image",
                    "enabled": True,
                }
            ],
        }

        cfg = RSSConfig.from_context(conf)

        self.assertEqual(len(cfg.jobs), 0)
        self.assertEqual(len(cfg.daily_digests), 1)
        digest = cfg.daily_digests[0]
        self.assertEqual(digest.id, "digest-1")
        self.assertEqual(digest.title, "digest-1")
        self.assertEqual(digest.render_mode, "image")
        self.assertEqual(digest.send_time, "09:00")
        self.assertEqual(digest.llm_timeout_seconds, 90)
        self.assertTrue(digest.semantic_merge_enabled)
        self.assertEqual(digest.semantic_merge_provider_id, "provider-digest")
        self.assertEqual(digest.semantic_merge_max_candidates, 12)
        self.assertEqual(digest.semantic_merge_min_confidence, 0.76)
        self.assertTrue(digest.enabled)

    def test_daily_digest_invalid_send_time_raises(self):
        conf = _minimal_runtime_conf()
        conf["daily_digests"] = [
            {
                "id": "digest-1",
                "feed_ids": ["feed-1"],
                "target_ids": ["target-1"],
                "send_time": "25:61",
                "enabled": True,
            }
        ]

        with self.assertRaises(ConfigValidationError):
            RSSConfig.from_context(conf)

    def test_job_dedup_ttl_seconds_can_override_global_default(self):
        conf = _minimal_runtime_conf()
        conf["jobs"][0]["dedup_ttl_seconds"] = 3888000

        cfg = RSSConfig.from_context(conf)

        self.assertEqual(cfg.jobs[0].dedup_ttl_seconds, 3888000)

    def test_job_dedup_ttl_seconds_must_be_non_negative(self):
        conf = _minimal_runtime_conf()
        conf["jobs"][0]["dedup_ttl_seconds"] = -1

        with self.assertRaises(ConfigValidationError):
            RSSConfig.from_context(conf)

    def test_job_semantic_dedup_config_parses(self):
        conf = _minimal_runtime_conf()
        conf["jobs"][0].update(
            {
                "compact_mode_enabled": True,
                "semantic_dedup_enabled": True,
                "semantic_dedup_provider_id": "provider-news",
                "semantic_dedup_ttl_seconds": 86400,
                "semantic_dedup_max_candidates": 8,
                "semantic_dedup_min_confidence": 0.75,
            }
        )

        cfg = RSSConfig.from_context(conf)

        job = cfg.jobs[0]
        self.assertTrue(job.compact_mode_enabled)
        self.assertTrue(job.semantic_dedup_enabled)
        self.assertEqual(job.semantic_dedup_provider_id, "provider-news")
        self.assertEqual(job.semantic_dedup_ttl_seconds, 86400)
        self.assertEqual(job.semantic_dedup_max_candidates, 8)
        self.assertEqual(job.semantic_dedup_min_confidence, 0.75)

    def test_target_compact_mode_parses(self):
        conf = _minimal_runtime_conf()
        conf["targets"][0]["compact_mode"] = "compact"

        cfg = RSSConfig.from_context(conf)

        self.assertEqual(cfg.targets[0].compact_mode, "compact")

    def test_target_compact_mode_validates_allowed_values(self):
        conf = _minimal_runtime_conf()
        conf["targets"][0]["compact_mode"] = "brief"

        with self.assertRaises(ConfigValidationError):
            RSSConfig.from_context(conf)

    def test_job_semantic_dedup_config_validates_positive_values(self):
        conf = _minimal_runtime_conf()
        conf["jobs"][0].update(
            {
                "semantic_dedup_enabled": True,
                "semantic_dedup_ttl_seconds": 0,
            }
        )

        with self.assertRaises(ConfigValidationError):
            RSSConfig.from_context(conf)

    def test_schema_exposes_job_semantic_dedup_provider_selector(self):
        schema = json.loads(Path("_conf_schema.json").read_text(encoding="utf-8"))

        job_items = schema["jobs"]["templates"]["job"]["items"]
        self.assertIn("compact_mode_enabled", job_items)
        self.assertIn("semantic_dedup_enabled", job_items)
        self.assertEqual(job_items["semantic_dedup_provider_id"]["_special"], "select_provider")
        self.assertEqual(job_items["semantic_dedup_ttl_seconds"]["default"], 86400)

    def test_schema_exposes_target_compact_mode(self):
        schema = json.loads(Path("_conf_schema.json").read_text(encoding="utf-8"))

        target_items = schema["targets"]["templates"]["target"]["items"]
        self.assertEqual(target_items["compact_mode"]["default"], "inherit")
        self.assertIn("compact", target_items["compact_mode"]["options"])
        self.assertIn("normal", target_items["compact_mode"]["options"])

    def test_schema_exposes_daily_digest_semantic_merge_provider_selector(self):
        schema = json.loads(Path("_conf_schema.json").read_text(encoding="utf-8"))

        digest_items = schema["daily_digests"]["templates"]["daily_digest"]["items"]
        self.assertIn("semantic_merge_enabled", digest_items)
        self.assertEqual(digest_items["semantic_merge_provider_id"]["_special"], "select_provider")
        self.assertEqual(digest_items["semantic_merge_max_candidates"]["default"], 20)

    def test_daily_digest_semantic_merge_config_validates_values(self):
        conf = _minimal_runtime_conf()
        conf["daily_digests"] = [
            {
                "id": "digest-1",
                "feed_ids": ["feed-1"],
                "target_ids": ["target-1"],
                "send_time": "09:00",
                "semantic_merge_enabled": True,
                "semantic_merge_max_candidates": 0,
                "enabled": True,
            }
        ]

        with self.assertRaises(ConfigValidationError):
            RSSConfig.from_context(conf)

    def test_twitter_feed_parses_media_switches(self):
        conf = _minimal_runtime_conf()
        conf["feeds"] = [
            {
                "id": "tw-1",
                "source_type": "twitter",
                "username": "@alice",
                "nitter_url": "https://nitter.example.com",
                "proxy_url": "http://127.0.0.1:7890",
                "send_images": False,
                "send_videos": True,
                "send_link": False,
                "max_new_items": 2,
                "enabled": True,
            }
        ]
        conf["jobs"][0]["feed_ids"] = ["tw-1"]

        cfg = RSSConfig.from_context(conf)

        feed = cfg.feeds[0]
        self.assertEqual(feed.source_type, "twitter")
        self.assertEqual(feed.username, "alice")
        self.assertEqual(feed.nitter_url, "https://nitter.example.com")
        self.assertEqual(feed.proxy_url, "http://127.0.0.1:7890")
        self.assertFalse(feed.send_images)
        self.assertTrue(feed.send_videos)
        self.assertFalse(feed.send_link)
        self.assertEqual(feed.max_new_items, 2)

    def test_legacy_feed_template_key_migrates_to_source_specific_template(self):
        conf = _minimal_runtime_conf()
        conf["feeds"] = [
            {
                "__template_key": "feed",
                "id": "rss-1",
                "url": "https://example.com/rss",
                "enabled": True,
            },
            {
                "__template_key": "feed",
                "id": "tw-1",
                "source_type": "twitter",
                "username": "alice",
                "enabled": True,
            },
        ]
        conf["jobs"][0]["feed_ids"] = ["rss-1", "tw-1"]

        RSSConfig.from_context(conf)

        self.assertEqual(conf["feeds"][0]["__template_key"], "rss_feed")
        self.assertEqual(conf["feeds"][1]["__template_key"], "twitter_feed")

    def test_legacy_feed_template_key_migration_saves_panel_config(self):
        class SavingConfig(dict):
            saved = False

            def save_config(self):
                self.saved = True

        conf = SavingConfig(_minimal_runtime_conf())
        conf["feeds"] = [
            {
                "__template_key": "feed",
                "id": "rss-1",
                "url": "https://example.com/rss",
                "enabled": True,
            }
        ]
        conf["jobs"][0]["feed_ids"] = ["rss-1"]

        RSSConfig.from_context(conf)

        self.assertTrue(conf.saved)

    def test_schema_uses_source_specific_feed_templates(self):
        schema = json.loads(Path("_conf_schema.json").read_text(encoding="utf-8"))

        self.assertEqual(schema["feeds"]["description"], "RSS/Twitter 源配置")
        templates = schema["feeds"]["templates"]
        self.assertIn("rss_feed", templates)
        self.assertIn("twitter_feed", templates)
        self.assertNotIn("feed", templates)
        self.assertEqual(templates["rss_feed"]["items"]["source_type"]["default"], "rss")
        self.assertTrue(templates["rss_feed"]["items"]["source_type"]["invisible"])
        self.assertIn("proxy_url", templates["rss_feed"]["items"])
        self.assertEqual(
            templates["twitter_feed"]["items"]["source_type"]["default"],
            "twitter",
        )
        self.assertTrue(templates["twitter_feed"]["items"]["source_type"]["invisible"])
        self.assertNotIn("username", templates["rss_feed"]["items"])
        self.assertNotIn("auth_mode", templates["twitter_feed"]["items"])
        self.assertIn("max_new_items", templates["twitter_feed"]["items"])

    def test_runtime_register_name_matches_package_name(self):
        tree = ast.parse(Path("main.py").read_text(encoding="utf-8"))
        register_call = None
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "register":
                register_call = node
                break

        self.assertIsNotNone(register_call)
        self.assertEqual(register_call.args[0].value, "astrbot_plugin_rss_forwarder")

    def test_display_flags_parse(self):
        conf = _minimal_runtime_conf()
        conf.update(
            {
                "display_source": False,
                "display_time": False,
                "display_link": False,
            }
        )

        cfg = RSSConfig.from_context(conf)

        self.assertFalse(cfg.display_source)
        self.assertFalse(cfg.display_time)
        self.assertFalse(cfg.display_link)

    def test_enabled_twitter_feed_requires_username(self):
        conf = _minimal_runtime_conf()
        conf["feeds"] = [
            {
                "id": "tw-1",
                "source_type": "twitter",
                "enabled": True,
            }
        ]
        conf["jobs"][0]["feed_ids"] = ["tw-1"]

        with self.assertRaises(ConfigValidationError):
            RSSConfig.from_context(conf)

    def test_twitter_feed_accepts_socks_proxy_url(self):
        conf = _minimal_runtime_conf()
        conf["feeds"] = [
            {
                "id": "tw-1",
                "source_type": "twitter",
                "username": "alice",
                "proxy_url": "socks5://127.0.0.1:7891",
                "enabled": True,
            }
        ]
        conf["jobs"][0]["feed_ids"] = ["tw-1"]

        cfg = RSSConfig.from_context(conf)

        self.assertEqual(cfg.feeds[0].proxy_url, "socks5://127.0.0.1:7891")

    def test_disabled_draft_entries_can_be_saved(self):
        conf = {
            "feeds": [
                {
                    "id": "",
                    "url": "",
                    "enabled": False,
                }
            ],
            "targets": [
                {
                    "id": "target-draft",
                    "platform": "qq",
                    "unified_msg_origin": "",
                    "enabled": False,
                }
            ],
            "jobs": [
                {
                    "id": "job-draft",
                    "feed_ids": [],
                    "target_ids": [],
                    "interval_seconds": 0,
                    "enabled": False,
                }
            ],
            "daily_digests": [
                {
                    "id": "digest-draft",
                    "feed_ids": [],
                    "target_ids": [],
                    "send_time": "09:00",
                    "enabled": False,
                }
            ],
        }

        cfg = RSSConfig.from_context(conf)

        self.assertFalse(cfg.feeds[0].enabled)
        self.assertFalse(cfg.targets[0].enabled)
        self.assertFalse(cfg.jobs[0].enabled)
        self.assertFalse(cfg.daily_digests[0].enabled)

    def test_enabled_target_requires_unified_msg_origin(self):
        conf = _minimal_runtime_conf()
        conf["targets"] = [
            {
                "id": "target-1",
                "platform": "qq",
                "unified_msg_origin": "",
                "enabled": True,
            }
        ]

        with self.assertRaises(ConfigValidationError):
            RSSConfig.from_context(conf)


if __name__ == "__main__":
    unittest.main()
