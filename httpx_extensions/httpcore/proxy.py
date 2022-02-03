import ssl
from typing import Optional, Tuple, Union

from httpcore._models import (
    URL,
    Origin,
    enforce_bytes,
    enforce_headers,
    enforce_url,
)
from httpcore.backends.base import AsyncNetworkBackend
from httpcore._async.interfaces import AsyncConnectionInterface
from httpcore._async.http_proxy import (
    AsyncForwardHTTPConnection,
    AsyncTunnelHTTPConnection,
    HeadersAsMapping,
    HeadersAsSequence,
    build_auth_header
)

from .pool import AsyncConnectionPoolMixin


class AsyncHTTPProxyMixin(AsyncConnectionPoolMixin):
    """
    A connection pool that sends requests via an HTTP proxy.
    An unmodified version of httpcore._async.http_proxy.AsyncHTTPProxy
    which inherits from AsyncConnectionPoolMixin
    """

    def __init__(
        self,
        proxy_url: Union[URL, bytes, str],
        proxy_auth: Tuple[Union[bytes, str], Union[bytes, str]] = None,
        proxy_headers: Union[HeadersAsMapping, HeadersAsSequence] = None,
        ssl_context: ssl.SSLContext = None,
        max_connections: Optional[int] = 10,
        max_keepalive_connections: int = None,
        keepalive_expiry: float = None,
        http1: bool = True,
        http2: bool = False,
        retries: int = 0,
        local_address: str = None,
        uds: str = None,
        network_backend: AsyncNetworkBackend = None,
    ) -> None:
        
        super().__init__(
            ssl_context=ssl_context,
            max_connections=max_connections,
            max_keepalive_connections=max_keepalive_connections,
            keepalive_expiry=keepalive_expiry,
            http1=http1,
            http2=http2,
            network_backend=network_backend,
            retries=retries,
            local_address=local_address,
            uds=uds,
        )
        self._ssl_context = ssl_context
        self._proxy_url = enforce_url(proxy_url, name="proxy_url")
        self._proxy_headers = enforce_headers(proxy_headers, name="proxy_headers")
        if proxy_auth is not None:
            username = enforce_bytes(proxy_auth[0], name="proxy_auth")
            password = enforce_bytes(proxy_auth[1], name="proxy_auth")
            authorization = build_auth_header(username, password)
            self._proxy_headers = [
                (b"Proxy-Authorization", authorization)
            ] + self._proxy_headers

    def create_connection(self, origin: Origin) -> AsyncConnectionInterface:
        if origin.scheme == b"http":
            return AsyncForwardHTTPConnection(
                proxy_origin=self._proxy_url.origin,
                proxy_headers=self._proxy_headers,
                keepalive_expiry=self._keepalive_expiry,
                network_backend=self._network_backend,
            )
        return AsyncTunnelHTTPConnection(
            proxy_origin=self._proxy_url.origin,
            proxy_headers=self._proxy_headers,
            remote_origin=origin,
            ssl_context=self._ssl_context,
            keepalive_expiry=self._keepalive_expiry,
            http1=self._http1,
            http2=self._http2,
            network_backend=self._network_backend,
        )