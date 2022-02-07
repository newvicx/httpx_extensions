from typing import List

import pytest
import trio as concurrency

from httpcore import ConnectError, PoolTimeout, RemoteProtocolError, UnsupportedProtocol
from httpcore.backends.mock import AsyncMockBackend

from httpx_extensions.httpcore.pool import AsyncConnectionPoolMixin


@pytest.mark.anyio
async def test_connection_pool_with_keepalive():
    """
    By default HTTP/1.1 requests should be reserved until explicitely released.
    """
    network_backend = AsyncMockBackend(
        [
            b"HTTP/1.1 200 OK\r\n",
            b"Content-Type: plain/text\r\n",
            b"Content-Length: 13\r\n",
            b"\r\n",
            b"Hello, world!",
            b"HTTP/1.1 200 OK\r\n",
            b"Content-Type: plain/text\r\n",
            b"Content-Length: 13\r\n",
            b"\r\n",
            b"Hello, world!",
            b"HTTP/1.1 200 OK\r\n",
            b"Content-Type: plain/text\r\n",
            b"Content-Length: 13\r\n",
            b"\r\n",
            b"Hello, world!",
            b"HTTP/1.1 200 OK\r\n",
            b"Content-Type: plain/text\r\n",
            b"Content-Length: 13\r\n",
            b"\r\n",
            b"Hello, world!",
        ]
    )

    async with AsyncConnectionPoolMixin(
        network_backend=network_backend,
    ) as pool:
        async with pool.stream("GET", "https://example.com/") as response:
            info = [repr(pool._connection_pool[c]) for c in pool.connections]
            assert info == [
                "<AsyncHTTPConnection ['https://example.com:443', HTTP/1.1, ACTIVE, Request Count: 1]>"
            ]
            # connection should be considered active until response is closed
            conn_id = response.extensions.get("conn_id")
            assert conn_id is not None
            assert conn_id in pool._active_connections
            await response.aread()
        assert response.status == 200
        assert response.content == b"Hello, world!"
        
        # once the response is closed, connection should be considered reserved
        assert conn_id in pool._reserved_connections
        info = [repr(pool._connection_pool[conn_id])]
        
        # The underlying connection state should be idle
        assert info == [
            "<AsyncHTTPConnection ['https://example.com:443', HTTP/1.1, IDLE, Request Count: 1]>"
        ]
        # but the pool should not have any connections considered idle
        assert not pool._idle_connections

        # sending a second request referencing the conn_id should reuse the reserved connection
        async with pool.stream("GET", "https://example.com/", extensions={"conn_id": conn_id}) as response:
            info = [repr(pool._connection_pool[c]) for c in pool.connections]
            assert info == [
                "<AsyncHTTPConnection ['https://example.com:443', HTTP/1.1, ACTIVE, Request Count: 2]>"
            ]
            assert len(pool._pool) == 1
            assert conn_id == response.extensions.get("conn_id")
            await response.aread()
        assert response.status == 200
        assert response.content == b"Hello, world!"
        
        # releasing the stream should set the connection to idle in the pool
        await response.stream.release()
        assert conn_id in pool._idle_connections

        # Sending a third request to the same origin will reuse the existing IDLE connection.
        async with pool.stream("GET", "https://example.com/") as response:
            info = [repr(pool._connection_pool[c]) for c in pool.connections]
            assert info == [
                "<AsyncHTTPConnection ['https://example.com:443', HTTP/1.1, ACTIVE, Request Count: 3]>"
            ]
            assert conn_id == response.extensions.get("conn_id")
            await response.aread()
        assert response.status == 200
        assert response.content == b"Hello, world!"
        info = [repr(pool._connection_pool[c]) for c in pool.connections]
        assert info == [
            "<AsyncHTTPConnection ['https://example.com:443', HTTP/1.1, IDLE, Request Count: 3]>"
        ]
        await response.stream.release()

        # Sending a request to a different origin will not reuse the existing IDLE connection.
        # It will create a new connection with a new conn_id
        async with pool.stream("GET", "http://example.com/") as response_new:
            info = [repr(pool._connection_pool[c]) for c in pool.connections]
            assert info == [
                "<AsyncHTTPConnection ['http://example.com:80', HTTP/1.1, ACTIVE, Request Count: 1]>",
                "<AsyncHTTPConnection ['https://example.com:443', HTTP/1.1, IDLE, Request Count: 3]>",
            ]
            conn_id_new = response_new.extensions.get("conn_id")
            assert conn_id_new is not None
            assert conn_id_new != conn_id
            await response_new.aread()

        assert response.status == 200
        assert response.content == b"Hello, world!"
        info = [repr(pool._connection_pool[c]) for c in pool.connections]
        assert info == [
            "<AsyncHTTPConnection ['http://example.com:80', HTTP/1.1, IDLE, Request Count: 1]>",
            "<AsyncHTTPConnection ['https://example.com:443', HTTP/1.1, IDLE, Request Count: 3]>",
        ]

        # the new connection should be reserved while the old connection is still idle
        assert conn_id_new in pool._reserved_connections and conn_id not in pool._reserved_connections
        await response_new.stream.release()
        # both connections should now be idle
        assert conn_id_new in pool._idle_connections and conn_id in pool._idle_connections


@pytest.mark.anyio
async def test_connection_pool_with_close():
    """
    HTTP/1.1 requests that include a 'Connection: Close' header should
    not be returned to the connection pool. The response will still have
    a conn_id. A warning will be issued
    """
    network_backend = AsyncMockBackend(
        [
            b"HTTP/1.1 200 OK\r\n",
            b"Content-Type: plain/text\r\n",
            b"Content-Length: 13\r\n",
            b"Connection: close\r\n",
            b"\r\n",
            b"Hello, world!",
        ]
    )

    async with AsyncConnectionPoolMixin(network_backend=network_backend) as pool:
        # Sending an intial request, which once complete will not return to the pool.
        with pytest.warns(UserWarning):
            async with pool.stream(
                "GET", "https://example.com/", headers={"Connection": "close"}
            ) as response:
                info = [repr(pool._connection_pool[c]) for c in pool.connections]
                assert info == [
                    "<AsyncHTTPConnection ['https://example.com:443', HTTP/1.1, ACTIVE, Request Count: 1]>"
                ]
                conn_id = response.extensions.get("conn_id")
                assert conn_id is not None
                await response.aread()
        assert response.status == 200
        assert response.content == b"Hello, world!"
        assert (
            conn_id not in pool._connection_pool and
            conn_id not in pool._active_connections and
            conn_id not in pool._idle_connections and
            conn_id not in pool._reserved_connections and
            conn_id not in pool._pool
        )


@pytest.mark.anyio
async def test_trace_request():
    """
    The 'trace' request extension allows for a callback function to inspect the
    internal events that occur while sending a request.
    """
    network_backend = AsyncMockBackend(
        [
            b"HTTP/1.1 200 OK\r\n",
            b"Content-Type: plain/text\r\n",
            b"Content-Length: 13\r\n",
            b"\r\n",
            b"Hello, world!",
        ]
    )

    called = []

    async def trace(name, kwargs):
        called.append(name)

    async with AsyncConnectionPoolMixin(network_backend=network_backend) as pool:
        await pool.request("GET", "https://example.com/", extensions={"trace": trace})

    assert called == [
        "connection.connect_tcp.started",
        "connection.connect_tcp.complete",
        "connection.start_tls.started",
        "connection.start_tls.complete",
        "http11.send_request_headers.started",
        "http11.send_request_headers.complete",
        "http11.send_request_body.started",
        "http11.send_request_body.complete",
        "http11.receive_response_headers.started",
        "http11.receive_response_headers.complete",
        "http11.receive_response_body.started",
        "http11.receive_response_body.complete",
        "http11.response_closed.started",
        "http11.response_closed.complete",
    ]


@pytest.mark.anyio
async def test_connection_pool_with_http_exception():
    """
    HTTP/1.1 requests that result in an exception during the connection should
    not be returned to the connection pool.
    """
    network_backend = AsyncMockBackend([b"Wait, this isn't valid HTTP!"])

    called = []

    async def trace(name, kwargs):
        called.append(name)

    async with AsyncConnectionPoolMixin(network_backend=network_backend) as pool:
        # Sending an initial request, which once complete will not return to the pool.
        with pytest.raises(Exception):
            with pytest.warns(UserWarning):
                await pool.request(
                    "GET", "https://example.com/", extensions={"trace": trace}
                )
        # all connection management objects should be empty
        assert (
            not pool._connection_pool and
            not pool._active_connections and
            not pool._idle_connections and
            not pool._reserved_connections and
            not pool._pool
        )

    assert called == [
        "connection.connect_tcp.started",
        "connection.connect_tcp.complete",
        "connection.start_tls.started",
        "connection.start_tls.complete",
        "http11.send_request_headers.started",
        "http11.send_request_headers.complete",
        "http11.send_request_body.started",
        "http11.send_request_body.complete",
        "http11.receive_response_headers.started",
        "http11.receive_response_headers.failed",
        "http11.response_closed.started",
        "http11.response_closed.complete",
    ]


@pytest.mark.anyio
async def test_connection_pool_with_connect_exception():
    """
    HTTP/1.1 requests that result in an exception during connection should not
    be returned to the connection pool.
    """

    class FailedConnectBackend(AsyncMockBackend):
        async def connect_tcp(
            self, host: str, port: int, timeout: float = None, local_address: str = None
        ):
            raise ConnectError("Could not connect")

    network_backend = FailedConnectBackend([])

    called = []

    async def trace(name, kwargs):
        called.append(name)

    async with AsyncConnectionPoolMixin(network_backend=network_backend) as pool:
        # Sending an initial request, which once complete will not return to the pool.
        with pytest.raises(Exception):
            await pool.request(
                "GET", "https://example.com/", extensions={"trace": trace}
            )

        assert (
            not pool._connection_pool and
            not pool._active_connections and
            not pool._idle_connections and
            not pool._reserved_connections and
            not pool._pool
        )

    assert called == [
        "connection.connect_tcp.started",
        "connection.connect_tcp.failed",
    ]


@pytest.mark.anyio
async def test_connection_pool_with_immediate_expiry():
    """
    Connection pools with keepalive_expiry=0.0 should raise a RuntimeError
    """

    with pytest.raises(ValueError):
        AsyncConnectionPoolMixin(keepalive_expiry=0.0)


@pytest.mark.anyio
async def test_connection_pool_with_no_keepalive_connections_allowed():
    """
    When 'max_keepalive_connections=0' is used, IDLE connections should not
    be returned to the pool.
    """
    with pytest.raises(ValueError):
        AsyncConnectionPoolMixin(max_keepalive_connections=0.0)


@pytest.mark.trio
async def test_connection_pool_concurrency():
    """
    HTTP/1.1 requests made in concurrency must not ever exceed the maximum number
    of allowable connection in the pool.
    """
    network_backend = AsyncMockBackend(
        [
            b"HTTP/1.1 200 OK\r\n",
            b"Content-Type: plain/text\r\n",
            b"Content-Length: 13\r\n",
            b"\r\n",
            b"Hello, world!",
        ]
    )
    
    async def fetch(pool, domain, info_list):
        async with pool.stream("GET", f"http://{domain}/") as response:
            info = [repr(pool._connection_pool[c]) for c in pool.connections]
            info_list.append(info)
            await response.aread()
        # connection must be released outside of context manager otherwise deadlock will occur
        await response.stream.release()

    async with AsyncConnectionPoolMixin(
        max_connections=1, network_backend=network_backend
    ) as pool:
        info_list: List[str] = []
        async with concurrency.open_nursery() as nursery:
            for domain in ["a.com", "b.com", "c.com", "d.com", "e.com"]:
                nursery.start_soon(fetch, pool, domain, info_list)

        for item in info_list:
            # Check that each time we inspected the connection pool, only a
            # single connection was established at any one time.
            assert len(item) == 1
            # Each connection was to a different host, and only sent a single
            # request on that connection.
            assert item[0] in [
                "<AsyncHTTPConnection ['http://a.com:80', HTTP/1.1, ACTIVE, Request Count: 1]>",
                "<AsyncHTTPConnection ['http://b.com:80', HTTP/1.1, ACTIVE, Request Count: 1]>",
                "<AsyncHTTPConnection ['http://c.com:80', HTTP/1.1, ACTIVE, Request Count: 1]>",
                "<AsyncHTTPConnection ['http://d.com:80', HTTP/1.1, ACTIVE, Request Count: 1]>",
                "<AsyncHTTPConnection ['http://e.com:80', HTTP/1.1, ACTIVE, Request Count: 1]>",
            ]


@pytest.mark.trio
async def test_connection_pool_concurrency_same_domain_closing():
    """
    HTTP/1.1 requests made in concurrency must not ever exceed the maximum number
    of allowable connection in the pool.
    """
    network_backend = AsyncMockBackend(
        [
            b"HTTP/1.1 200 OK\r\n",
            b"Content-Type: plain/text\r\n",
            b"Content-Length: 13\r\n",
            b"Connection: close\r\n",
            b"\r\n",
            b"Hello, world!",
        ]
    )

    
    async def fetch(pool, domain, info_list):
        async with pool.stream("GET", f"https://{domain}/") as response:
            info = [repr(pool._connection_pool[c]) for c in pool.connections]
            info_list.append(info)
            await response.aread()
        # connection must be released outside of context manager otherwise deadlock will occur
        await response.stream.release()

    with pytest.warns(UserWarning):
        async with AsyncConnectionPoolMixin(
            max_connections=1, network_backend=network_backend
        ) as pool:
            info_list: List[str] = []
            async with concurrency.open_nursery() as nursery:
                for domain in ["a.com", "a.com", "a.com", "a.com", "a.com"]:
                    nursery.start_soon(fetch, pool, domain, info_list)

            for item in info_list:
                # Check that each time we inspected the connection pool, only a
                # single connection was established at any one time.
                assert len(item) == 1
                # Only a single request was sent on each connection.
                assert (
                    item[0]
                    == "<AsyncHTTPConnection ['https://a.com:443', HTTP/1.1, ACTIVE, Request Count: 1]>"
                )


@pytest.mark.trio
async def test_connection_pool_concurrency_same_domain_keepalive():
    """
    HTTP/1.1 requests made in concurrency must not ever exceed the maximum number
    of allowable connection in the pool.
    """
    network_backend = AsyncMockBackend(
        [
            b"HTTP/1.1 200 OK\r\n",
            b"Content-Type: plain/text\r\n",
            b"Content-Length: 13\r\n",
            b"\r\n",
            b"Hello, world!",
        ]
        * 5
    )

    async def fetch(pool, domain, info_list):
        async with pool.stream("GET", f"https://{domain}/") as response:
            info = [repr(pool._connection_pool[c]) for c in pool.connections]
            info_list.append(info)
            await response.aread()
        # connection must be released outside of context manager otherwise deadlock will occur
        await response.stream.release()

    async with AsyncConnectionPoolMixin(
        max_connections=1, network_backend=network_backend
    ) as pool:
        info_list: List[str] = []
        async with concurrency.open_nursery() as nursery:
            for domain in ["a.com", "a.com", "a.com", "a.com", "a.com"]:
                nursery.start_soon(fetch, pool, domain, info_list)

        for item in info_list:
            # Check that each time we inspected the connection pool, only a
            # single connection was established at any one time.
            assert len(item) == 1
            # The connection sent multiple requests.
            assert item[0] in [
                "<AsyncHTTPConnection ['https://a.com:443', HTTP/1.1, ACTIVE, Request Count: 1]>",
                "<AsyncHTTPConnection ['https://a.com:443', HTTP/1.1, ACTIVE, Request Count: 2]>",
                "<AsyncHTTPConnection ['https://a.com:443', HTTP/1.1, ACTIVE, Request Count: 3]>",
                "<AsyncHTTPConnection ['https://a.com:443', HTTP/1.1, ACTIVE, Request Count: 4]>",
                "<AsyncHTTPConnection ['https://a.com:443', HTTP/1.1, ACTIVE, Request Count: 5]>",
            ]


@pytest.mark.anyio
async def test_unsupported_protocol():
    async with AsyncConnectionPoolMixin() as pool:
        with pytest.raises(UnsupportedProtocol):
            await pool.request("GET", "ftp://www.example.com/")

        with pytest.raises(UnsupportedProtocol):
            await pool.request("GET", "://www.example.com/")


@pytest.mark.anyio
async def test_connection_pool_closed_while_request_in_flight():
    """
    Closing a connection pool while a request/response is still in-flight
    should raise an error.
    """
    network_backend = AsyncMockBackend(
        [
            b"HTTP/1.1 200 OK\r\n",
            b"Content-Type: plain/text\r\n",
            b"Content-Length: 13\r\n",
            b"\r\n",
            b"Hello, world!",
        ]
    )

    async with AsyncConnectionPoolMixin(
        network_backend=network_backend,
    ) as pool:
        # Send a request, and then close the connection pool while the
        # response has not yet been streamed.
        async with pool.stream("GET", "https://example.com/"):
            with pytest.raises(RuntimeError):
                await pool.aclose()


@pytest.mark.anyio
async def test_connection_pool_timeout():
    """
    Ensure that exceeding max_connections can cause a request to timeout.
    """
    network_backend = AsyncMockBackend(
        [
            b"HTTP/1.1 200 OK\r\n",
            b"Content-Type: plain/text\r\n",
            b"Content-Length: 13\r\n",
            b"\r\n",
            b"Hello, world!",
        ]
    )
    with pytest.raises(RuntimeError): # request in flight
        async with AsyncConnectionPoolMixin(
            network_backend=network_backend, max_connections=1
        ) as pool:
            # Send a request to a pool that is configured to only support a single
            # connection, and then ensure that a second concurrent request
            # fails with a timeout.
            with pytest.raises(UserWarning): # reserved connection closed
                async with pool.stream("GET", "https://example.com/"):
                    with pytest.raises(PoolTimeout):
                        extensions = {"timeout": {"pool": 0.0001}}
                        await pool.request("GET", "https://example.com/", extensions=extensions)


@pytest.mark.anyio
async def test_http2_connection_raises_error():
    """
    RuntimeError raised if an http2 connection is established
    """
    
    network_backend = AsyncMockBackend(
        [
            b"HTTP/2 200 OK\r\n",
            b"Content-Type: plain/text\r\n",
            b"Content-Length: 13\r\n",
            b"\r\n",
            b"Hello, world!"
        ],
        http2=True
    )

    with pytest.raises(RuntimeError):
        async with AsyncConnectionPoolMixin(network_backend=network_backend, http2=True) as pool:
            async with pool.stream("GET", "https://example.com/") as response:
                pass

