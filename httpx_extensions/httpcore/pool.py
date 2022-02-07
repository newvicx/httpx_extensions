import logging
import ssl
import uuid
import warnings
from contextlib import suppress
from types import TracebackType
from typing import AsyncIterable, AsyncIterator, Optional, Type

from httpcore import AsyncConnectionPool
from httpcore._exceptions import ConnectionNotAvailable, RemoteProtocolError, UnsupportedProtocol
from httpcore._models import Request, Response
from httpcore._synchronization import AsyncEvent
from httpcore.backends.base import AsyncNetworkBackend
from httpcore._async.interfaces import AsyncConnectionInterface


logger = logging.getLogger(__name__)


class RequestStatusMixin:
    def __init__(self, request: Request):
        self.request = request
        self.connection: Optional[AsyncConnectionInterface] = None
        self.conn_id = None
        self._connection_acquired = AsyncEvent()

    def set_connection(
        self,
        conn_id: str,
        connection: AsyncConnectionInterface
    ) -> None:
        assert self.connection is None
        assert self.conn_id is None
        self.connection = connection
        self.conn_id = conn_id
        self._connection_acquired.set()

    def unset_connection(self) -> None:
        assert self.connection is not None
        assert self.conn_id is not None
        self.connection = None
        self.conn_id = None
        self._connection_acquired = AsyncEvent()

    async def wait_for_connection(
        self, timeout: float = None
    ) -> AsyncConnectionInterface:
        await self._connection_acquired.wait(timeout=timeout)
        assert self.connection is not None
        assert self.conn_id is not None
        return self.connection


class AsyncConnectionPoolMixin(AsyncConnectionPool):
    """
    A connection pool for making HTTP requests. Extends
    httpcore implementation to enable connection orchestration
    for HTTP 1.1 connections. This allows requests in an HTTPX
    auth flow to control the connection used in the pool
    thus supporting connection based auth methods such as
    Negotiate and NTLM
    """

    def __init__(
        self,
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

        if http2:
            raise RuntimeError("HTTP 2 not supported, use stock versions of httpcore or httpx")
        if keepalive_expiry == 0:
            raise ValueError("keepalive_expiry cannot be 0")
        if max_keepalive_connections == 0:
            raise ValueError("max_keepalive_connections cannot be 0")

        super().__init__(
            ssl_context=ssl_context,
            max_connections=max_connections,
            max_keepalive_connections=max_keepalive_connections,
            keepalive_expiry=keepalive_expiry,
            http1=http1,
            http2=http2,
            retries=retries,
            local_address=local_address,
            uds=uds,
            network_backend=network_backend
        )
        self._active_connections = {}
        self._idle_connections = {}
        self._reserved_connections = {}
        self._connection_pool = {}
        self._check_http2 = http2

    async def _attempt_to_acquire_connection(self, status: RequestStatusMixin) -> bool:
        """
        Attempt to provide a connection that can handle the given origin.
        """
        origin = status.request.url.origin

        # If specified attempt to use a reserved connection
        conn_id = status.request.extensions.get("conn_id")
        if conn_id is not None:
            connection = self._reserved_connections.get(conn_id)
            if connection is not None:
                if not connection.is_available():
                    return False
                logger.debug(
                    "Re-using connection id %s for %r", conn_id, status.request
                )
                idx = self._pool.index(conn_id)
                self._pool.pop(idx)
                self._pool.insert(0, conn_id)
                status.set_connection(conn_id, connection)
                self._active_connections.update(
                    {conn_id: self._reserved_connections.pop(conn_id)}
                )
                return True
            else:
                logger.warning(
                    "Connection id '%s' was given in request extensions "
                    "but the connection is not reserved. %r is not guarenteed to "
                    "use the desired connection which could break auth flow.",
                    conn_id,
                    status.request
                )

        # Reuse an existing idle connection if one is currently available.
        # loop through pool rather than idle connections to ensure we grab
        # the oldest idle connection
        if len(self._idle_connections) > 0:
            for idx, conn_id in reversed(list(enumerate(self._pool))):
                connection = self._idle_connections.get(conn_id)
                if (
                    connection is not None and
                    connection.can_handle_request(origin) and
                    connection.is_available()
                ):
                    self._pool.pop(idx)
                    self._pool.insert(0, conn_id)
                    status.set_connection(conn_id, connection)
                    self._active_connections.update(
                        {conn_id: self._idle_connections.pop(conn_id)}
                    )
                    return True

        # If the pool is currently full, attempt to close one idle connection.
        # Loop through pool rather than idle connections to ensure we grab
        # the oldest idle connection
        if len(self._pool) >= self._max_connections:
            if len(self._idle_connections) > 0:
                for idx, conn_id in reversed(list(enumerate(self._pool))):
                    connection = self._idle_connections.get(conn_id)
                    if connection is not None and connection.is_idle():
                        await connection.aclose()
                        self._pool.remove(conn_id)
                        self._connection_pool.pop(conn_id)
                        self._idle_connections.pop(conn_id)
                        break

        # If the pool is still full, we cannot acquire a connection
        if len(self._pool) >= self._max_connections:
            return False

        # Otherwise create a new connection.
        conn_id = uuid.uuid4().hex
        connection = self.create_connection(origin)
        self._pool.insert(0, conn_id)
        self._connection_pool[conn_id] = connection
        status.set_connection(conn_id, connection)
        self._active_connections[conn_id] = connection
        return True

    async def _close_expired_connections(self) -> None:
        """
        Clean up the connection pool by closing off any connections that have expired.
        """
        # Close any connections that have expired their keep-alive time.
        for idx, conn_id in reversed(list(enumerate(self._pool))):
            connection = self._connection_pool[conn_id]
            if connection.has_expired():
                await connection.aclose()
                self._pool.pop(idx)
                self._connection_pool.pop(conn_id)
                self._active_connections.pop(conn_id, None)
                self._idle_connections.pop(conn_id, None)
                if conn_id in self._reserved_connections:
                    self._reserved_connections.pop(conn_id)
                    logger.warning(
                        "Reserved connection id '%s' has expired. "
                        "Additional requests attempting to use this connection "
                        "will be assigned a different connection which may break "
                        "auth flow",
                        conn_id
                    )


        # If the pool size exceeds the maximum number of allowed keep-alive connections,
        # then close off idle connections as required.
        pool_size = len(self._pool)
        # again loop through pool rather than idle connections directly to ensure
        # oldest connections are closed first
        for idx, conn_id in reversed(list(enumerate(self._pool))):
            connection = self._connection_pool[conn_id]
            if (
                connection.is_idle() and
                pool_size > self._max_keepalive_connections and
                conn_id not in self._reserved_connections
            ):
                await connection.aclose()
                # this would be a bug if connection was in active connections
                assert conn_id not in self._active_connections
                self._pool.pop(idx)
                self._connection_pool.pop(conn_id)
                # similar to above this would be a bug if connection was not
                # in idle connections and KeyError was raised
                self._idle_connections.pop(conn_id)
                pool_size -= 1

    async def handle_async_request(self, request: Request) -> Response:
        """
        Send an HTTP request, and return an HTTP response.
        This is the core implementation that is called into by `.request()` or `.stream()`.
        """
        scheme = request.url.scheme.decode()
        if scheme == "":
            raise UnsupportedProtocol(
                "Request URL is missing an 'http://' or 'https://' protocol."
            )
        if scheme not in ("http", "https"):
            raise UnsupportedProtocol(
                f"Request URL has an unsupported protocol '{scheme}://'."
            )

        status = RequestStatusMixin(request)

        async with self._pool_lock:
            self._requests.append(status)
            await self._close_expired_connections()
            acquired = await self._attempt_to_acquire_connection(status)

        while True:
            timeouts = request.extensions.get("timeout", {})
            timeout = timeouts.get("pool", None)
            connection = await status.wait_for_connection(timeout=timeout)
            try:
                response = await connection.handle_async_request(request)
            except ConnectionNotAvailable:
                # The ConnectionNotAvailable exception is a special case, that
                # indicates we need to retry the request on a new connection.
                #
                # The most common case where this can occur is when multiple
                # requests are queued waiting for a single connection, which
                # might end up as an HTTP/2 connection, but which actually ends
                # up as HTTP/1.1.
                async with self._pool_lock:
                    # Maintain our position in the request queue, but reset the
                    # status so that the request becomes queued again.
                    logger.warning(
                        "Connection id '%s' was unavailable. "
                        "%r must be sent on a different "
                        "connection which could break auth flow.",
                        status.conn_id,
                        status.request
                    )
                    status.unset_connection()
                    await self._attempt_to_acquire_connection(status)
            except BaseException as exc:
                await self.response_closed(status)
                await self.release_connection(status.conn_id)
                raise exc
            else:
                break

        # add conn_id to response object extensions
        response.extensions.update(
            {"conn_id": status.conn_id}
        )
        # When we return the response, we wrap the stream in a special class
        # that handles notifying the connection pool once the response
        # has been released.
        assert isinstance(response.stream, AsyncIterable)
        return Response(
            status=response.status,
            headers=response.headers,
            content=ConnectionPoolByteStreamMixin(response.stream, self, status),
            extensions=response.extensions,
        )

    async def response_closed(self, status: RequestStatusMixin) -> None:
        """
        This method acts as a callback once a request/response cycle is complete.
        It is called into from the `ConnectionPoolByteStreamMixin.aclose()` method.
        """
        assert status.connection is not None
        assert status.conn_id is not None
        connection = status.connection
        conn_id = status.conn_id

        async with self._pool_lock:
            # Update the state of the connection pool.
            if status in self._requests:
                self._requests.remove(status)

            if connection.is_closed() and conn_id in self._pool:
                self._pool.remove(conn_id)
                self._connection_pool.pop(conn_id)
                self._active_connections.pop(conn_id, None)
                
                warnings.warn(
                    f"Connection '{conn_id}' was closed after the "
                    f"the response for {repr(status.request)} was closed. Any "
                    "subsequent requests attempting to use this connection will "
                    "be assigned a new one which may break auth flow",
                    UserWarning,
                    stacklevel=2
                )
            else:
                try:
                    self._reserved_connections.update(
                        {conn_id: self._active_connections.pop(conn_id)}
                    )
                    logger.debug(f"{conn_id} added to reserved connections")
                except KeyError:
                    # if the connection is not in active_connections the pool
                    # might have been closed in which case the connection_pool
                    # would be empty. if it is, return None
                    if self._connection_pool == {}:
                        return
                    # if it isnt this is a bug
                    raise RuntimeError("Using an inactive connection")

            # Housekeeping.
            await self._close_expired_connections()

    async def release_connection(self, conn_id) -> None:
        """
        This method acts as a callback once a request is fully complete.
        The connection is released back to the pool. It is called into from the
        `ConnectionPoolByteStreamMixin.release()` method.
        """
        async with self._pool_lock:
            connection = self._reserved_connections.pop(conn_id, None)
            if connection is not None:
                self._idle_connections[conn_id] = connection
                logger.debug("Connection %s released to pool", conn_id)
            else:
                logger.warning(
                    "Attempted to release un-reserved connection the connection "
                    "might have been closed. Connection id: '%s'",
                    conn_id
                )

            # Since we've had a response closed, it's possible we'll now be able
            # to service one or more requests that are currently pending.
            for status in self._requests:
                if status.connection is None:
                    acquired = await self._attempt_to_acquire_connection(status)
                    # If we could not acquire a connection for a queued request
                    # then we don't need to check anymore requests that are
                    # queued later behind it.
                    if not acquired:
                        break

    async def aclose(self) -> None:
        """
        Close any connections in the pool.
        """
        async with self._pool_lock:
            requests_still_in_flight = len(self._requests)

            for conn_id in self._pool:
                connection = self._connection_pool[conn_id]
                await connection.aclose()
            self._pool = []
            self._requests = []
            self._connection_pool = {}
            self._active_connections = {}
            self._idle_connections = {}
            self._reserved_connections = {}

            if requests_still_in_flight:
                raise RuntimeError(
                    f"The connection pool was closed while {requests_still_in_flight} "
                    f"HTTP requests/responses were still in-flight."
                )

    async def __aenter__(self) -> "AsyncConnectionPool":
        return self

    async def __aexit__(
        self,
        exc_type: Type[BaseException] = None,
        exc_value: BaseException = None,
        traceback: TracebackType = None,
    ) -> None:
        await self.aclose()


class ConnectionPoolByteStreamMixin:
    """
    A wrapper around the response byte stream, that additionally handles
    notifying the connection pool when the response has been closed. Adds
    additional method for releasing the connection back to the pool.
    """

    def __init__(
        self,
        stream: AsyncIterable[bytes],
        pool: AsyncConnectionPoolMixin,
        status: RequestStatusMixin,
    ) -> None:
        self._stream = stream
        self._pool = pool
        self._status = status

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for part in self._stream:
            yield part

    async def aclose(self) -> None:
        try:
            if hasattr(self._stream, "aclose"):
                await self._stream.aclose()  # type: ignore
        finally:
            await self._pool.response_closed(self._status)

    async def release(self) -> None:
        await self._pool.release_connection(self._status.conn_id)