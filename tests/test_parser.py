import unittest
import sys
import types


astrbot_module = types.ModuleType("astrbot")
astrbot_api_module = types.ModuleType("astrbot.api")
astrbot_api_module.logger = types.SimpleNamespace(
    info=lambda *a, **k: None,
    warning=lambda *a, **k: None,
    error=lambda *a, **k: None,
)
sys.modules.setdefault("astrbot", astrbot_module)
sys.modules["astrbot.api"] = astrbot_api_module

from parser import FeedParser


class FeedParserTests(unittest.TestCase):
    def test_parse_rss_prefers_enclosure_image_url(self):
        xml = """
        <rss version="2.0">
          <channel>
            <title>TechPowerUp</title>
            <item>
              <title>News A</title>
              <link>https://example.com/a</link>
              <guid>a-1</guid>
              <pubDate>Mon, 16 Mar 2026 13:34:50 +0000</pubDate>
              <description><![CDATA[<img src="https://example.com/thumb.jpg"/>]]></description>
              <enclosure url="https://example.com/full.jpg" type="image/jpeg" />
            </item>
          </channel>
        </rss>
        """

        entries = FeedParser().parse([{"feed_id": "f1", "body": xml}])

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["image_url"], "https://example.com/full.jpg")

    def test_parse_rss_fallback_to_description_img(self):
        xml = """
        <rss version="2.0">
          <channel>
            <title>TechPowerUp</title>
            <item>
              <title>News B</title>
              <link>https://example.com/b</link>
              <guid>b-1</guid>
              <pubDate>Mon, 16 Mar 2026 13:34:50 +0000</pubDate>
              <description><![CDATA[<div><img src="https://example.com/from-desc.jpg"/></div>]]></description>
            </item>
          </channel>
        </rss>
        """

        entries = FeedParser().parse([{"feed_id": "f1", "body": xml}])

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["image_url"], "https://example.com/from-desc.jpg")

    def test_parse_atom_enclosure_image_url(self):
        xml = """
        <feed xmlns="http://www.w3.org/2005/Atom">
          <title>Atom Feed</title>
          <entry>
            <title>News C</title>
            <id>c-1</id>
            <updated>2026-03-16T13:34:50+00:00</updated>
            <link rel="alternate" href="https://example.com/c" />
            <link rel="enclosure" type="image/jpeg" href="https://example.com/c.jpg" />
            <summary><![CDATA[<img src="https://example.com/c-desc.jpg"/>]]></summary>
          </entry>
        </feed>
        """

        entries = FeedParser().parse([{"feed_id": "f1", "body": xml}])

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["image_url"], "https://example.com/c.jpg")

    def test_parse_twitter_raw_items_to_unified_entries(self):
        entries = FeedParser().parse(
            [
                {
                    "feed_id": "tw-1",
                    "source_type": "twitter",
                    "items": [
                        {
                            "tweet_id": "123",
                            "username": "alice",
                            "screen_name": "Alice",
                            "text": "hello world",
                            "images": ["https://nitter.net/pic/a.jpg"],
                            "videos": ["https://nitter.net/video/a.mp4"],
                            "image_paths": ["/tmp/a.jpg"],
                            "video_paths": ["/tmp/a.mp4"],
                            "send_link": False,
                        }
                    ],
                }
            ]
        )

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["guid"], "twitter:alice:123")
        self.assertEqual(entries[0]["link"], "https://x.com/alice/status/123")
        self.assertEqual(entries[0]["image_url"], "https://nitter.net/pic/a.jpg")
        self.assertEqual(entries[0]["image_urls"], ["https://nitter.net/pic/a.jpg"])
        self.assertEqual(entries[0]["video_urls"], ["https://nitter.net/video/a.mp4"])
        self.assertEqual(entries[0]["image_paths"], ["/tmp/a.jpg"])
        self.assertEqual(entries[0]["video_paths"], ["/tmp/a.mp4"])
        self.assertIs(entries[0]["send_link"], False)


if __name__ == "__main__":
    unittest.main()
