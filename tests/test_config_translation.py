import unittest

from config import RSSConfig


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
        self.assertTrue(cfg.google_translate_enabled)
        self.assertEqual(cfg.google_translate_api_key, "k")
        self.assertEqual(cfg.google_translate_target_lang, "ja")
        self.assertEqual(cfg.google_translate_timeout_seconds, 11)
        self.assertEqual(cfg.google_translate_proxy_mode, "custom")
        self.assertEqual(cfg.google_translate_proxy_url, "http://127.0.0.1:7890")


if __name__ == "__main__":
    unittest.main()
