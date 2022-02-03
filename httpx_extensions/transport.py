import typing
from types import TracebackType

import httpcore

from httpx._config import DEFAULT_LIMITS, Limits, Proxy, create_ssl_context
from httpx._models import Request
from httpx._types import AsyncByteStream, CertTypes, VerifyTypes
from httpx._transports.base import AsyncBaseTransport
from httpx._transports.default import map_httpcore_exceptions

from .httpcore.pool import AsyncConnectionPoolMixin
from .httpcore.proxy import AsyncHTTPProxyMixin
from .models import ResponseMixin

A = typing.TypeVar("A", bound="AsyncHTTPTransportMixin")


class AsyncResponseStreamMixin(AsyncByteStream):
    def __init__(self, httpcore_stream: typing.AsyncIterable[bytes]):
        self._httpcore_stream = httpcore_stream

    async def __aiter__(self) -> typing.AsyncIterator[bytes]:
        with map_httpcore_exceptions():
            async for part in self._httpcore_stream:
                yield part

    async def aclose(self) -> None:
        if hasattr(self._httpcore_stream, "aclose"):
            await self._httpcore_stream.aclose()  # type: ignore

    async def release(self) -> None:
        if hasattr(self._httpcore_stream, "release"):
            await self._httpcore_stream.release()


class AsyncHTTPTransportMixin(AsyncBaseTransport):
    def __init__(
        self,
        verify: VerifyTypes = True,
        cert: CertTypes = None,
        http1: bool = True,
        http2: bool = False,
        limits: Limits = DEFAULT_LIMITS,
        trust_env: bool = True,
        proxy: Proxy = None,
        uds: str = None,
        local_address: str = None,
        retries: int = 0,
    ) -> None:
        ssl_context = create_ssl_context(verify=verify, cert=cert, trust_env=trust_env)

        if proxy is None:
            self._pool = AsyncConnectionPoolMixin(
                ssl_context=ssl_context,
                max_connections=limits.max_connections,
                max_keepalive_connections=limits.max_keepalive_connections,
                keepalive_expiry=limits.keepalive_expiry,
                http1=http1,
                http2=http2,
                uds=uds,
                local_address=local_address,
                retries=retries,
            )
        elif proxy.url.scheme in ("http", "https"):
            self._pool = AsyncHTTPProxyMixin(
                proxy_url=httpcore.URL(
                    scheme=proxy.url.raw_scheme,
                    host=proxy.url.raw_host,
                    port=proxy.url.port,
                    target=proxy.url.raw_path,
                ),
                proxy_auth=proxy.raw_auth,
                proxy_headers=proxy.headers.raw,
                ssl_context=ssl_context,
                max_connections=limits.max_connections,
                max_keepalive_connections=limits.max_keepalive_connections,
                keepalive_expiry=limits.keepalive_expiry,
                http1=http1,
                http2=http2,
            )
        elif proxy.url.scheme == "socks5":
            try:
                import socksio  # noqa
                from .httpcore.socks import AsyncSOCKSProxyMixin
            except ImportError:  # pragma: nocover
                raise ImportError(
                    "Using SOCKS proxy, but the 'socksio' package is not installed. "
                    "Make sure to install httpx using `pip install httpx[socks]`."
                ) from None

            self._pool = AsyncSOCKSProxyMixin(
                proxy_url=httpcore.URL(
                    scheme=proxy.url.raw_scheme,
                    host=proxy.url.raw_host,
                    port=proxy.url.port,
                    target=proxy.url.raw_path,
                ),
                proxy_auth=proxy.raw_auth,
                ssl_context=ssl_context,
                max_connections=limits.max_connections,
                max_keepalive_connections=limits.max_keepalive_connections,
                keepalive_expiry=limits.keepalive_expiry,
                http1=http1,
                http2=http2,
            )
        else:  # pragma: nocover
            raise ValueError(
                f"Proxy protocol must be either 'http', 'https', or 'socks5', but got {proxy.url.scheme!r}."
            )

    async def __aenter__(self: A) -> A:  # Use generics for subclass support.
        await self._pool.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: typing.Type[BaseException] = None,
        exc_value: BaseException = None,
        traceback: TracebackType = None,
    ) -> None:
        with map_httpcore_exceptions():
            await self._pool.__aexit__(exc_type, exc_value, traceback)

    async def handle_async_request(
        self,
        request: Request,
    ) -> ResponseMixin:
        assert isinstance(request.stream, AsyncByteStream)

        req = httpcore.Request(
            method=request.method,
            url=httpcore.URL(
                scheme=request.url.raw_scheme,
                host=request.url.raw_host,
                port=request.url.port,
                target=request.url.raw_path,
            ),
            headers=request.headers.raw,
            content=request.stream,
            extensions=request.extensions,
        )
        with map_httpcore_exceptions():
            resp = await self._pool.handle_async_request(req)

        assert isinstance(resp.stream, typing.AsyncIterable)

        return ResponseMixin(
            status_code=resp.status,
            headers=resp.headers,
            stream=AsyncResponseStreamMixin(resp.stream),
            extensions=resp.extensions,
        )

    async def aclose(self) -> None:
        await self._pool.aclose()