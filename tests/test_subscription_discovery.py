import unittest
import socket
import sys
import types
from unittest.mock import patch
from subscription_discovery import discover_feed, validate_public_url, feed_metadata, fetch_public


class DiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_html_discovers_relative_atom_feed(self):
        async def fetch(url):
            if url.endswith('/feed'):
                return url, b'<feed xmlns="http://www.w3.org/2005/Atom"><title>News</title><entry><title>Item</title></entry></feed>'
            return url, b'<html><link rel="alternate" type="application/atom+xml" href="/feed"></html>'
        result = await discover_feed('https://example.com/', fetch=fetch)
        self.assertEqual(result['url'], 'https://example.com/feed')
        self.assertEqual(result['sample_titles'], ['Item'])

    def test_non_feed_xml_is_rejected(self):
        self.assertIsNone(feed_metadata(b'<html><title>Verify</title></html>'))

    def test_credential_and_non_web_urls_rejected(self):
        for url in ['file:///etc/passwd', 'https://name:secret@example.com/', 'https://@example.com/', 'http://example.com:22/', None, 'https://example.com/\x7f']:
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_public_url(url)

    def test_xml_entities_are_rejected(self):
        self.assertIsNone(feed_metadata(b'<!DOCTYPE rss [<!ENTITY x "value">]><rss><title>&x;</title></rss>'))

    async def test_private_and_mixed_dns_are_rejected_before_connect(self):
        for ips in [('127.0.0.1',), ('169.254.169.254',), ('8.8.8.8', '10.0.0.1'), ('::1',)]:
            records = [(socket.AF_INET, socket.SOCK_STREAM, 0, '', (ip, 443)) for ip in ips]
            with self.subTest(ips=ips), patch.dict(sys.modules, {'aiohttp': types.ModuleType('aiohttp')}), patch('subscription_discovery.socket.getaddrinfo', return_value=records):
                with self.assertRaisesRegex(ValueError, '公网'):
                    await fetch_public('https://example.com/feed')
