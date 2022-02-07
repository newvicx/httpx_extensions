import datetime
import typing

from httpx import AsyncClient, Request
from httpx.__version__ import __version__
from httpx._auth import Auth
from httpx._client import (
    ClientState,
    USE_CLIENT_DEFAULT,
    UseClientDefault
)
from httpx._config import (
    DEFAULT_LIMITS,
    DEFAULT_MAX_REDIRECTS,
    DEFAULT_TIMEOUT_CONFIG,
    Limits,
    Proxy,
)
from httpx._exceptions import TooManyRedirects, request_context
from httpx._transports.base import AsyncBaseTransport
from httpx._types import (
    AsyncByteStream,
    AuthTypes,
    CertTypes,
    CookieTypes,
    HeaderTypes,
    ProxiesTypes,
    QueryParamTypes,
    TimeoutTypes,
    URLTypes,
    VerifyTypes,
)
from httpx._utils import Timer, get_logger

from .transport import AsyncHTTPTransportMixin
from .models import ResponseMixin


logger = get_logger(__name__)


class BoundAsyncStreamMixin(AsyncByteStream):
    """
    An async byte stream that is bound to a given response instance, and that
    ensures the `response.elapsed` is set once the response is closed.
    """

    def __init__(
        self, stream: AsyncByteStream, response: ResponseMixin, timer: Timer
    ) -> None:
        self._stream = stream
        self._response = response
        self._timer = timer

    async def __aiter__(self) -> typing.AsyncIterator[bytes]:
        async for chunk in self._stream:
            yield chunk

    async def aclose(self) -> None:
        seconds = await self._timer.async_elapsed()
        self._response.elapsed = datetime.timedelta(seconds=seconds)
        await self._stream.aclose()

    async def release(self) -> None:
        if hasattr(self._stream, "release"):
            await self._stream.release()


class ExClient(AsyncClient):

    """
    A slightly modified version of AsyncClient. Nearly all methods are
    identical except in the handling of a request additional calls were
    added to release the underlying connection back to the pool where
    applicable
    """
    
    def __init__(
        self,
        *,
        auth: AuthTypes = None,
        params: QueryParamTypes = None,
        headers: HeaderTypes = None,
        cookies: CookieTypes = None,
        verify: VerifyTypes = True,
        cert: CertTypes = None,
        http1: bool = True,
        http2: bool = False,
        proxies: ProxiesTypes = None,
        mounts: typing.Mapping[str, AsyncBaseTransport] = None,
        timeout: TimeoutTypes = DEFAULT_TIMEOUT_CONFIG,
        follow_redirects: bool = False,
        limits: Limits = DEFAULT_LIMITS,
        max_redirects: int = DEFAULT_MAX_REDIRECTS,
        event_hooks: typing.Mapping[str, typing.List[typing.Callable]] = None,
        base_url: URLTypes = "",
        transport: AsyncBaseTransport = None,
        app: typing.Callable = None,
        trust_env: bool = True,
    ):
        super().__init__(
            auth=auth,
            params=params,
            headers=headers,
            cookies=cookies,
            verify=verify,
            cert=cert,
            http1=http1,
            http2=http2,
            proxies=proxies,
            mounts=mounts,
            timeout=timeout,
            follow_redirects=follow_redirects,
            limits=limits,
            max_redirects=max_redirects,
            event_hooks=event_hooks,
            base_url=base_url,
            transport=transport,
            app=None,
            trust_env=trust_env,
        )

    def _init_transport(
        self,
        verify: VerifyTypes = True,
        cert: CertTypes = None,
        http1: bool = True,
        http2: bool = False,
        limits: Limits = DEFAULT_LIMITS,
        transport: AsyncBaseTransport = None,
        app: typing.Callable = None,
        trust_env: bool = True,
    ) -> AsyncBaseTransport:

        if transport is not None:
            if not isinstance(transport, AsyncHTTPTransportMixin):
                raise TypeError(
                    f"Unsupported transport {type(transport)}. Use {AsyncHTTPTransportMixin.__name__}"
                )
            return transport

        return AsyncHTTPTransportMixin(
            verify=verify,
            cert=cert,
            http1=http1,
            http2=http2,
            limits=limits,
            trust_env=trust_env,
        )

    def _init_proxy_transport(
        self,
        proxy: Proxy,
        verify: VerifyTypes = True,
        cert: CertTypes = None,
        http1: bool = True,
        http2: bool = False,
        limits: Limits = DEFAULT_LIMITS,
        trust_env: bool = True,
    ) -> AsyncBaseTransport:

        return AsyncHTTPTransportMixin(
            verify=verify,
            cert=cert,
            http1=http1,
            http2=http2,
            limits=limits,
            trust_env=trust_env,
            proxy=proxy,
        )
    
    async def send(
        self,
        request: Request,
        *,
        stream: bool = False,
        auth: typing.Union[AuthTypes, UseClientDefault] = USE_CLIENT_DEFAULT,
        follow_redirects: typing.Union[bool, UseClientDefault] = USE_CLIENT_DEFAULT,
    ) -> ResponseMixin:
        """
        Send a request.
        The request is sent as-is, unmodified.
        Typically you'll want to build one with `AsyncClient.build_request()`
        so that any client-level configuration is merged into the request,
        but passing an explicit `httpx.Request()` is supported as well.
        See also: [Request instances][0]
        [0]: /advanced/#request-instances
        """
        if self._state == ClientState.CLOSED:
            raise RuntimeError("Cannot send a request, as the client has been closed.")

        self._state = ClientState.OPENED
        follow_redirects = (
            self.follow_redirects
            if isinstance(follow_redirects, UseClientDefault)
            else follow_redirects
        )

        auth = self._build_request_auth(request, auth)

        response = await self._send_handling_auth(
            request,
            auth=auth,
            follow_redirects=follow_redirects,
            history=[],
        )
        try:
            if not stream:
                await response.aread()
                if hasattr(response, 'release'):
                    await response.release()
            else:
                response.release_on_close = True
            return response

        except Exception as exc:  # pragma: no cover
            await response.aclose()
            if hasattr(response, 'release'):
                await response.release()
            raise exc

    async def _send_handling_auth(
        self,
        request: Request,
        auth: Auth,
        follow_redirects: bool,
        history: typing.List[ResponseMixin],
    ) -> ResponseMixin:
        auth_flow = auth.async_auth_flow(request)
        try:
            request = await auth_flow.__anext__()

            while True:
                response = await self._send_handling_redirects(
                    request,
                    follow_redirects=follow_redirects,
                    history=history,
                )
                try:
                    try:
                        next_request = await auth_flow.asend(response)
                    except StopAsyncIteration:
                        return response

                    response.history = list(history)
                    await response.aread()
                    reserved_connection = next_request.extensions.get("conn_id")
                    if not reserved_connection:
                        if hasattr(response, 'release'):
                            await response.release()
                    request = next_request
                    history.append(response)

                except Exception as exc:
                    await response.aclose()
                    if hasattr(response, 'release'):
                        await response.release()
                    raise exc
        finally:
            await auth_flow.aclose()

    async def _send_handling_redirects(
        self,
        request: Request,
        follow_redirects: bool,
        history: typing.List[ResponseMixin],
    ) -> ResponseMixin:
        while True:
            if len(history) > self.max_redirects:
                raise TooManyRedirects(
                    "Exceeded maximum allowed redirects.", request=request
                )

            for hook in self._event_hooks["request"]:
                await hook(request)

            response = await self._send_single_request(request)
            try:
                for hook in self._event_hooks["response"]:
                    await hook(response)

                response.history = list(history)

                if not response.has_redirect_location:
                    return response

                request = self._build_redirect_request(request, response)
                history = history + [response]

                if follow_redirects:
                    await response.aread()
                    conn_id = response.extensions.get("conn_id")
                    request.extensions.update({"conn_id": conn_id})
                else:
                    response.next_request = request
                    return response

            except Exception as exc:
                await response.aclose()
                if hasattr(response, 'release'):
                    await response.release()
                raise exc

    async def _send_single_request(self, request: Request) -> ResponseMixin:
        """
        Sends a single request, without handling any redirections.
        """
        transport = self._transport_for_url(request.url)
        timer = Timer()
        await timer.async_start()

        if not isinstance(request.stream, AsyncByteStream):
            raise RuntimeError(
                "Attempted to send an sync request with an AsyncClient instance."
            )

        with request_context(request=request):
            response = await transport.handle_async_request(request)

        assert isinstance(response.stream, AsyncByteStream)
        response.request = request
        response.stream = BoundAsyncStreamMixin(
            response.stream, response=response, timer=timer
        )
        self.cookies.extract_cookies(response)

        status = f"{response.status_code} {response.reason_phrase}"
        response_line = f"{response.http_version} {status}"
        logger.debug(
            'HTTP Request: %s %s "%s"', request.method, request.url, response_line
        )

        return response
    