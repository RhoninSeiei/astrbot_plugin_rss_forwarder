import ssl
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError
from urllib.parse import (
    parse_qsl,
    urlencode,
    urljoin,
    urlparse,
    urlsplit,
    urlunparse,
    urlunsplit,
)
from urllib.request import (
    HTTPRedirectHandler,
    HTTPSHandler,
    ProxyHandler,
    Request,
    build_opener,
)


_SOCKS_SCHEMES = ("socks://", "socks4://", "socks5://", "socks5h://")
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_SENSITIVE_REDIRECT_HEADERS = {
    "authorization",
    "cookie",
    "proxy-authorization",
}
_DEFAULT_MAX_REDIRECTS = 20
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


def build_nitter_timeline_request(
    feed,
    default_nitter_url: str = "https://nitter.net",
) -> tuple[str, dict[str, str]]:
    base_url = (
        str(getattr(feed, "nitter_url", "") or "").strip()
        or str(getattr(feed, "url", "") or "").strip()
        or str(default_nitter_url or "").strip()
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


def _url_origin(url: str) -> tuple[str, str, int] | None:
    try:
        parsed = urlsplit(str(url or ""))
        scheme = parsed.scheme.lower()
        hostname = (
            (parsed.hostname or "").encode("idna").decode("ascii").lower()
        )
        port = parsed.port
    except (UnicodeError, ValueError):
        return None
    if scheme not in {"http", "https"} or not hostname:
        return None
    if port is None:
        port = 443 if scheme == "https" else 80
    return scheme, hostname, port


def _safe_error_url(url: str) -> str:
    try:
        parsed = urlsplit(str(url or ""))
        scheme = parsed.scheme.lower()
        hostname = parsed.hostname or ""
        port = parsed.port
    except ValueError:
        return "source://redacted"
    if scheme not in {"http", "https"} or not hostname:
        return "source://redacted"
    rendered_host = f"[{hostname}]" if ":" in hostname else hostname
    if port is not None:
        rendered_host = f"{rendered_host}:{port}"
    return urlunsplit((scheme, rendered_host, parsed.path, "", ""))


def _redirect_headers(
    headers: Mapping[str, str],
    source_url: str,
    target_url: str,
) -> dict[str, str]:
    copied = {str(name): str(value) for name, value in headers.items()}
    if _url_origin(source_url) == _url_origin(target_url):
        return copied
    return {
        name: value
        for name, value in copied.items()
        if name.lower() not in _SENSITIVE_REDIRECT_HEADERS
    }


class _SafeRedirectHandler(HTTPRedirectHandler):
    def __init__(self, max_redirects: int | None) -> None:
        super().__init__()
        self._max_redirects = max_redirects
        self._redirect_count = 0
        if max_redirects is not None:
            self.max_repeats = max_redirects + 1
            self.max_redirections = max_redirects + 1

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if self._max_redirects is not None:
            self._redirect_count += 1
        if (
            self._max_redirects is not None
            and self._redirect_count > self._max_redirects
        ):
            raise TooManyRedirects(
                f"source exceeded redirect limit ({self._max_redirects})"
            )
        if _url_origin(newurl) is None:
            raise HTTPError(
                _safe_error_url(req.full_url),
                code,
                "source redirect target is invalid or unsupported",
                headers,
                fp,
            )
        redirected = super().redirect_request(
            req,
            fp,
            code,
            msg,
            headers,
            newurl,
        )
        if redirected is None or _url_origin(req.full_url) == _url_origin(newurl):
            return redirected
        for header_store in (redirected.headers, redirected.unredirected_hdrs):
            for header_name in tuple(header_store):
                if header_name.lower() in _SENSITIVE_REDIRECT_HEADERS:
                    header_store.pop(header_name, None)
        return redirected


_LimitedRedirectHandler = _SafeRedirectHandler


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
    return "urllib3" if normalized_proxy.startswith(_SOCKS_SCHEMES) else "urllib"


def _normalize_socks_proxy_url(proxy_url: str) -> str:
    parsed = urlsplit(str(proxy_url or "").strip())
    if parsed.scheme.lower() != "socks":
        return str(proxy_url or "").strip()
    return urlunsplit(
        ("socks5", parsed.netloc, parsed.path, parsed.query, parsed.fragment)
    )


def _build_socks_proxy_manager(proxy_url: str, verify_ssl: bool):
    from urllib3.contrib.socks import SOCKSProxyManager

    return SOCKSProxyManager(
        _normalize_socks_proxy_url(proxy_url),
        ssl_context=build_ssl_context(verify_ssl),
    )


def _read_socks_body(response: Any, max_bytes: int | None) -> tuple[bytes, bool]:
    if max_bytes is None:
        return bytes(response.read()), False
    content = bytearray()
    for chunk in response.stream(max_bytes + 1, decode_content=True):
        remaining = max_bytes + 1 - len(content)
        content.extend(chunk[:remaining])
        if len(content) >= max_bytes + 1:
            break
    return bytes(content[:max_bytes]), len(content) > max_bytes


def _request_with_socks(
    *,
    url: str,
    headers: dict[str, str],
    proxy_url: str,
    timeout: int,
    verify_ssl: bool,
    max_bytes: int | None,
    max_redirects: int | None,
) -> SourceHttpResponse:
    manager = _build_socks_proxy_manager(proxy_url, verify_ssl)
    redirect_limit = (
        _DEFAULT_MAX_REDIRECTS if max_redirects is None else max_redirects
    )
    current_url = str(url)
    current_headers = dict(headers)
    redirect_count = 0
    try:
        while True:
            response = manager.request(
                "GET",
                current_url,
                headers=current_headers,
                timeout=timeout,
                redirect=False,
                preload_content=False,
                decode_content=True,
            )
            try:
                status = int(response.status)
                redirect_url = response.get_redirect_location()
                if status in _REDIRECT_STATUSES and redirect_url:
                    if redirect_count >= redirect_limit:
                        raise TooManyRedirects(
                            f"source exceeded redirect limit ({redirect_limit})"
                        )
                    next_url = urljoin(current_url, str(redirect_url))
                    if _url_origin(next_url) is None:
                        raise HTTPError(
                            _safe_error_url(current_url),
                            status,
                            "source redirect target is invalid or unsupported",
                            response.headers,
                            None,
                        )
                    current_headers = _redirect_headers(
                        current_headers,
                        current_url,
                        next_url,
                    )
                    current_url = next_url
                    redirect_count += 1
                    continue

                if not 200 <= status < 300:
                    raise HTTPError(
                        _safe_error_url(current_url),
                        status,
                        f"HTTP status {status}",
                        response.headers,
                        None,
                    )
                body, truncated = _read_socks_body(response, max_bytes)
                return SourceHttpResponse(
                    body=body,
                    status=status,
                    headers=normalize_response_headers(response.headers),
                    final_url=current_url,
                    truncated=truncated,
                )
            finally:
                try:
                    response.close()
                finally:
                    response.release_conn()
    finally:
        manager.clear()


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
    if source_http_client(normalized_proxy) == "urllib3":
        return _request_with_socks(
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
    opener_handlers.append(_SafeRedirectHandler(max_redirects))

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
