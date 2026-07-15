import ssl
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
from urllib.request import (
    HTTPRedirectHandler,
    HTTPSHandler,
    ProxyHandler,
    Request,
    build_opener,
)


_SOCKS_SCHEMES = ("socks://", "socks4://", "socks5://", "socks5h://")
_RSS_REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml;q=0.9, */*;q=0.1",
    "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.7,zh;q=0.6",
}
NITTER_REQUEST_HEADERS = {
    "User-Agent": "astrbot_plugin_rss_forwarder/0.5.2 (+https://github.com/RhoninSeiei/astrbot_plugin_rss_forwarder)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7,ja;q=0.6",
}


class TooManyRedirects(Exception):
    """Raised when a source exceeds an explicit redirect limit."""


@dataclass(slots=True)
class SourceHttpResponse:
    body: bytes
    status: int
    headers: dict[str, str]
    final_url: str
    truncated: bool = False


def build_rss_request(
    feed,
    etag: str = "",
    last_modified: str = "",
) -> tuple[str, dict[str, str]]:
    headers = dict(_RSS_REQUEST_HEADERS)
    url = str(getattr(feed, "url", "") or "")
    auth_mode = str(getattr(feed, "auth_mode", "none") or "none")
    key = str(getattr(feed, "key", "") or "")
    if auth_mode == "query" and key:
        parsed = urlparse(url)
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        query["key"] = key
        url = urlunparse(parsed._replace(query=urlencode(query)))
    elif auth_mode == "header" and key:
        headers["Authorization"] = f"Bearer {key}"
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    return url, headers


def build_nitter_timeline_request(feed) -> tuple[str, dict[str, str]]:
    base_url = (
        str(getattr(feed, "nitter_url", "") or "").strip()
        or str(getattr(feed, "url", "") or "").strip()
        or "https://nitter.net"
    ).rstrip("/")
    username = str(getattr(feed, "username", "") or "").strip().lstrip("@")
    return f"{base_url}/{username}", dict(NITTER_REQUEST_HEADERS)


def build_ssl_context(verify_ssl: bool) -> ssl.SSLContext:
    context = ssl.create_default_context()
    if not verify_ssl:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return context


class _LimitedRedirectHandler(HTTPRedirectHandler):
    def __init__(self, max_redirects: int) -> None:
        super().__init__()
        self._max_redirects = max_redirects
        self._redirect_count = 0
        self.max_repeats = max_redirects + 1
        self.max_redirections = max_redirects + 1

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self._redirect_count += 1
        if self._redirect_count > self._max_redirects:
            raise TooManyRedirects(
                f"source exceeded redirect limit ({self._max_redirects})"
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _read_body(response: Any, max_bytes: int | None) -> tuple[bytes, bool]:
    if max_bytes is None:
        return response.read(), False
    body = response.read(max_bytes + 1)
    return body[:max_bytes], len(body) > max_bytes


def normalize_response_headers(headers: Any) -> dict[str, str]:
    return {str(name).lower(): str(value) for name, value in headers.items()}


def get_response_header(
    headers: Mapping[str, str],
    name: str,
    default: str = "",
) -> str:
    normalized_name = str(name).lower()
    for header_name, value in headers.items():
        if str(header_name).lower() == normalized_name:
            return str(value)
    return default


def source_http_client(proxy_url: str) -> str:
    normalized_proxy = str(proxy_url or "").strip().lower()
    return "httpx" if normalized_proxy.startswith(_SOCKS_SCHEMES) else "urllib"


def _request_with_httpx(
    *,
    url: str,
    headers: dict[str, str],
    proxy_url: str,
    timeout: int,
    verify_ssl: bool,
    max_bytes: int | None,
    max_redirects: int | None,
) -> SourceHttpResponse:
    import httpx

    client_options: dict[str, Any] = {
        "headers": headers,
        "timeout": timeout,
        "follow_redirects": True,
        "verify": verify_ssl,
        "proxy": proxy_url,
    }
    if max_redirects is not None:
        client_options["max_redirects"] = max_redirects

    with httpx.Client(**client_options) as client:
        if max_bytes is not None:
            with client.stream("GET", url) as response:
                response.raise_for_status()
                content = bytearray()
                for chunk in response.iter_bytes(chunk_size=max_bytes + 1):
                    remaining = max_bytes + 1 - len(content)
                    content.extend(chunk[:remaining])
                    if len(content) >= max_bytes + 1:
                        break
                truncated = len(content) > max_bytes
                return SourceHttpResponse(
                    body=bytes(content[:max_bytes]),
                    status=int(response.status_code),
                    headers=normalize_response_headers(response.headers),
                    final_url=str(response.url),
                    truncated=truncated,
                )

        response = client.get(url)
        response.raise_for_status()
        content = bytes(response.content)
        return SourceHttpResponse(
            body=content,
            status=int(response.status_code),
            headers=normalize_response_headers(response.headers),
            final_url=str(response.url),
            truncated=False,
        )


def request_source(
    *,
    url: str,
    headers: dict[str, str],
    proxy_url: str,
    timeout: int,
    verify_ssl: bool,
    max_bytes: int | None = None,
    max_redirects: int | None = None,
    use_environment_proxy: bool = True,
) -> SourceHttpResponse:
    if max_bytes is not None and max_bytes <= 0:
        raise ValueError("max_bytes must be greater than zero")

    normalized_proxy = str(proxy_url or "").strip()
    if source_http_client(normalized_proxy) == "httpx":
        return _request_with_httpx(
            url=url,
            headers=headers,
            proxy_url=normalized_proxy,
            timeout=timeout,
            verify_ssl=verify_ssl,
            max_bytes=max_bytes,
            max_redirects=max_redirects,
        )

    if normalized_proxy:
        proxy_handler = ProxyHandler(
            {"http": normalized_proxy, "https": normalized_proxy}
        )
    elif use_environment_proxy:
        proxy_handler = ProxyHandler()
    else:
        proxy_handler = ProxyHandler({})

    opener_handlers: list[Any] = [
        proxy_handler,
        HTTPSHandler(context=build_ssl_context(verify_ssl)),
    ]
    if max_redirects is not None:
        opener_handlers.append(_LimitedRedirectHandler(max_redirects))

    opener = build_opener(*opener_handlers)
    request = Request(url=url, headers=headers)
    with opener.open(request, timeout=timeout) as response:  # noqa: S310
        body, truncated = _read_body(response, max_bytes)
        return SourceHttpResponse(
            body=body,
            status=int(getattr(response, "status", 200) or 200),
            headers=normalize_response_headers(response.headers),
            final_url=str(response.geturl()),
            truncated=truncated,
        )
