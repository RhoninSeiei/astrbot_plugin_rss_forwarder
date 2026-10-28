"""Bounded public RSS discovery for subscription management."""

import asyncio
import ipaddress
import socket
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, urlunsplit
from xml.etree import ElementTree


_dns_slots = asyncio.Semaphore(2)
_discovery_slots = asyncio.Semaphore(2)


async def _resolve_addresses(host, port):
    await _dns_slots.acquire()
    task = asyncio.create_task(asyncio.to_thread(socket.getaddrinfo, host, port, type=socket.SOCK_STREAM))
    try:
        return await asyncio.shield(task)
    finally:
        # Cancellation cannot stop getaddrinfo; retain its slot until the thread exits.
        if task.done():
            _dns_slots.release()
        else:
            def release(completed):
                if not completed.cancelled():
                    completed.exception()
                _dns_slots.release()
            task.add_done_callback(release)


class _FeedLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag.lower() == "link" and "alternate" in values.get("rel", "").lower().split():
            if values.get("type", "").lower() in {"application/rss+xml", "application/atom+xml"}:
                if values.get("href"):
                    self.links.append(values["href"])


def validate_public_url(url):
    if not isinstance(url, str) or len(url) > 4096:
        raise ValueError("订阅地址无效或过长")
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("需要完整的 HTTP 或 HTTPS 地址")
    if parsed.username is not None or parsed.password is not None or parsed.port not in {None, 80, 443}:
        raise ValueError("订阅发现仅支持无凭据的标准网页地址")
    if any(ord(char) < 33 or ord(char) == 127 or char == "\\" for char in url):
        raise ValueError("地址包含非法字符")
    return parsed


async def fetch_public(url, proxy_url=""):
    import aiohttp

    for _ in range(4):
        parsed = validate_public_url(url)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        addresses = await _resolve_addresses(parsed.hostname, port)
        ips = list(dict.fromkeys(entry[4][0] for entry in addresses))
        if not ips or any(not ipaddress.ip_address(ip).is_global for ip in ips):
            raise ValueError("订阅发现仅允许公网地址")
        ip = ips[0]
        netloc = f"[{ip}]" if ":" in ip else ip
        pinned = urlunsplit((parsed.scheme, f"{netloc}:{port}", parsed.path or "/", parsed.query, ""))
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12), trust_env=False) as session:
            async with session.get(
                pinned, headers={"Host": parsed.netloc, "User-Agent": "AstrBot-RSS-Forwarder/1.0"},
                server_hostname=parsed.hostname if parsed.scheme == "https" else None,
                proxy=proxy_url or None, allow_redirects=False,
            ) as response:
                if response.status in {301, 302, 303, 307, 308}:
                    url = urljoin(url, response.headers.get("Location", ""))
                    continue
                response.raise_for_status()
                body = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    body.extend(chunk)
                    if len(body) > 1024 * 1024:
                        raise ValueError("页面超过订阅发现大小限制")
                return url, bytes(body)
    raise ValueError("页面重定向次数过多")


def feed_metadata(body):
    if b"<!DOCTYPE" in body.upper() or b"<!ENTITY" in body.upper():
        return None
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError:
        return None
    local = root.tag.rsplit("}", 1)[-1].lower()
    if local not in {"rss", "feed"}:
        return None
    if local == "rss" and root.find("channel") is None:
        return None
    titles = [node.text.strip() for node in root.iter() if node.tag.rsplit("}", 1)[-1] == "title" and node.text]
    return {"title": titles[0] if titles else "RSS", "sample_titles": titles[1:4]}


async def discover_feed(url, *, proxy_url="", fetch=None):
    if _discovery_slots.locked():
        raise ValueError("订阅发现正在处理其他请求，请稍后重试")
    async with _discovery_slots:
        return await _discover_feed(url, proxy_url=proxy_url, fetch=fetch)


async def _discover_feed(url, *, proxy_url="", fetch=None):
    validate_public_url(url)
    async def read(address):
        return await fetch(address) if fetch else await fetch_public(address, proxy_url)
    final_url, body = await read(url)
    metadata = feed_metadata(body)
    if metadata:
        return {"url": final_url, **metadata}
    parser = _FeedLinks()
    parser.feed(body.decode("utf-8", errors="replace"))
    for link in list(dict.fromkeys(parser.links))[:4]:
        candidate = urljoin(final_url, link)
        try:
            validate_public_url(candidate)
            address, data = await read(candidate)
            metadata = feed_metadata(data)
            if metadata:
                return {"url": address, **metadata}
        except (ValueError, OSError):
            continue
    raise ValueError("页面没有可验证的 RSS 或 Atom 订阅源，请提供订阅地址")
