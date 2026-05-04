import sys
import types
import unittest


astrbot_module = types.ModuleType("astrbot")
astrbot_api_module = types.ModuleType("astrbot.api")
astrbot_api_module.logger = types.SimpleNamespace(
    info=lambda *a, **k: None,
    warning=lambda *a, **k: None,
    error=lambda *a, **k: None,
)
sys.modules.setdefault("astrbot", astrbot_module)
sys.modules["astrbot.api"] = astrbot_api_module

from twitter_source import TwitterTimelineFetcher


class TwitterTimelineFetcherTests(unittest.IsolatedAsyncioTestCase):
    def test_extract_timeline_ids_dedupes_status_links(self):
        html = """
        <div class="timeline-item"><a class="tweet-link" href="/alice/status/200"></a></div>
        <div class="timeline-item"><a class="tweet-link" href="/alice/status/199"></a></div>
        <div class="timeline-item"><a class="tweet-link" href="/alice/status/200"></a></div>
        """

        ids = TwitterTimelineFetcher()._extract_timeline_ids(html, "alice")

        self.assertEqual(ids, ["200", "199"])

    def test_parse_tweet_detail_respects_media_switches(self):
        class Feed:
            id = "tw-1"
            send_images = False
            send_videos = True
            send_link = False

        html = """
        <div class="main-tweet">
          <a class="fullname">Alice</a>
          <div class="tweet-content media-body">hello <b>world</b></div>
          <a class="still-image"><img src="/pic/a.jpg"/></a>
          <div class="attachment"><video><source src="/video/a.mp4"/></video></div>
        </div>
        """

        item = TwitterTimelineFetcher()._parse_tweet_detail(
            Feed(),
            "https://nitter.example.com",
            "alice",
            "200",
            html,
        )

        self.assertEqual(item["text"], "hello world")
        self.assertEqual(item["images"], [])
        self.assertEqual(item["all_images"], ["https://nitter.example.com/pic/a.jpg"])
        self.assertEqual(item["videos"], ["https://nitter.example.com/video/a.mp4"])
        self.assertEqual(item["link"], "https://x.com/alice/status/200")
        self.assertIs(item["send_link"], False)

    async def test_fetch_keeps_since_id_at_last_success_when_detail_fails(self):
        class Feed:
            id = "tw-1"
            username = "alice"
            nitter_url = "https://nitter.example.com"
            url = ""
            proxy_url = ""
            timeout = 10
            send_images = True
            send_videos = True
            send_link = True

        fetcher = TwitterTimelineFetcher()

        def fake_open_text(url, proxy_url, timeout):
            if url.endswith("/alice"):
                return """
                <div class="timeline-item"><a class="tweet-link" href="/alice/status/300"></a></div>
                <div class="timeline-item"><a class="tweet-link" href="/alice/status/200"></a></div>
                """
            if url.endswith("/status/200"):
                return """
                <div class="main-tweet">
                  <a class="fullname">Alice</a>
                  <div class="tweet-content media-body">old item</div>
                </div>
                """
            raise RuntimeError("temporary failure")

        original_open_text = TwitterTimelineFetcher._open_text
        TwitterTimelineFetcher._open_text = staticmethod(fake_open_text)
        try:
            result = await fetcher.fetch(Feed(), {"since_id": "100"})
        finally:
            TwitterTimelineFetcher._open_text = original_open_text

        self.assertIsNotNone(result)
        self.assertEqual(result.since_id, "200")
        self.assertEqual([item["tweet_id"] for item in result.items], ["200"])


if __name__ == "__main__":
    unittest.main()
