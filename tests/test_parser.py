from parser import FeedParser


def test_parse_rss_prefers_enclosure_image_url():
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
    assert len(entries) == 1
    assert entries[0]["image_url"] == "https://example.com/full.jpg"


def test_parse_rss_fallback_to_description_img():
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
    assert len(entries) == 1
    assert entries[0]["image_url"] == "https://example.com/from-desc.jpg"


def test_parse_atom_enclosure_image_url():
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
    assert len(entries) == 1
    assert entries[0]["image_url"] == "https://example.com/c.jpg"
