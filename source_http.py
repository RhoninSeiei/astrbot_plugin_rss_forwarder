import ssl
from dataclasses import dataclass
from typing import Any
from urllib.request import (
    HTTPRedirectHandler,
    HTTPSHandler,
    ProxyHandler,
    Request,
    build_opener,
)


_SOCKS_SCHEMES = ("socks://", "socks4://", "socks5://", "socks5h://")


class TooManyRedirects(Exception):
    """Raised when a source exceeds an explicit redirect limit."""


@dataclass(slots=True)
class SourceHttpResponse:
    body: bytes
    status: int
    headers: dict[str, str]
    final_url: str
    truncated: bool = False


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


def _response_headers(headers: Any) -> dict[str, str]:
    return {str(name): str(value) for name, value in headers.items()}


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
        response = client.get(url)
        response.raise_for_status()
        content = bytes(response.content)
        truncated = max_bytes is not None and len(content) > max_bytes
        if max_bytes is not None:
            content = content[:max_bytes]
        return SourceHttpResponse(
            body=content,
            status=int(response.status_code),
            headers=_response_headers(response.headers),
            final_url=str(response.url),
            truncated=truncated,
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
    if normalized_proxy.lower().startswith(_SOCKS_SCHEMES):
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
            headers=_response_headers(response.headers),
            final_url=str(response.geturl()),
            truncated=truncated,
        )
