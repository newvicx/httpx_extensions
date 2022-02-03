import ssl
import typing

from httpcore._models import URL, Origin, enforce_bytes, enforce_url
from httpcore.backends.base import AsyncNetworkBackend
from httpcore._async.interfaces import AsyncConnectionInterface
from httpcore._async.socks_proxy import AsyncSocks5Connection

from .pool import AsyncConnectionPoolMixin


class AsyncSOCKSProxyMixin(AsyncConnectionPoolMixin):
    """
    A connection pool that sends requests via an HTTP proxy.
    An unmodified version of httpcore._async.socks_proxy.AsyncSOCKSProxy
    which inherits from AsyncConnectionPoolMixin
    """

    def __init__(
        self,
        proxy_url: typing.Union[URL, bytes, str],
        proxy_auth: typing.Tuple[
            typing.Union[bytes, str], typing.Union[bytes, str]
        ] = None,
        ssl_context: ssl.SSLContext = None,
        max_connections: typing.Optional[int] = 10,
        max_keepalive_connections: int = None,
        keepalive_expiry: float = None,
        http1: bool = True,
        http2: bool = False,
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
        )
        self._ssl_context = ssl_context
        self._proxy_url = enforce_url(proxy_url, name="proxy_url")
        if proxy_auth is not None:
            username, password = proxy_auth
            username_bytes = enforce_bytes(username, name="proxy_auth")
            password_bytes = enforce_bytes(password, name="proxy_auth")
            self._proxy_auth: typing.Optional[typing.Tuple[bytes, bytes]] = (
                username_bytes,
                password_bytes,
            )
        else:
            self._proxy_auth = None

    def create_connection(self, origin: Origin) -> AsyncConnectionInterface:
        return AsyncSocks5Connection(
            proxy_origin=self._proxy_url.origin,
            remote_origin=origin,
            proxy_auth=self._proxy_auth,
            ssl_context=self._ssl_context,
            keepalive_expiry=self._keepalive_expiry,
            http1=self._http1,
            http2=self._http2,
            network_backend=self._network_backend,
        )