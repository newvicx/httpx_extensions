"""
Integration tests for ExClient with AsyncConnectionPoolMixin
"""
import typing
from datetime import timedelta

import httpx
import pytest

import httpx_extensions



# Additonal httpx_extensions tests
@pytest.mark.usefixtures("async_environment")
async def test_conn_id_in_response(server):
    url = server.url
    async with httpx_extensions.ExClient() as client:
        response = await client.get(url)
    assert response.extensions.get("conn_id") is not None


@pytest.mark.usefixtures("async_environment")
async def test_connection_is_released(server):
    url = server.url
    async with httpx_extensions.ExClient() as client:
        response = await client.get(url)
        conn_id = response.extensions.get("conn_id")
        assert conn_id is not None
        assert conn_id not in client._transport._pool._reserved_connections
        assert conn_id in client._transport._pool._idle_connections


@pytest.mark.usefixtures("async_environment")
async def test_connection_is_reserved(server):
    url = server.url
    async with httpx_extensions.ExClient() as client:
        request = client.build_request("GET", url)
        response = await client._send_single_request(request)
        conn_id = response.extensions.get("conn_id")
        await response.aread()
        assert conn_id is not None
        assert conn_id in client._transport._pool._reserved_connections
        await response.release()
        assert conn_id not in client._transport._pool._reserved_connections


@pytest.mark.usefixtures("async_environment")
async def test_reuse_reserved_connection(server):
    url = server.url
    async with httpx_extensions.ExClient() as client:
        request = client.build_request("GET", url)
        response_1 = await client._send_single_request(request)
        conn_id_1 = response_1.extensions.get("conn_id")
        await response_1.aread()
        request.extensions.update({"conn_id": conn_id_1})
        response_2 = await client._send_single_request(request)
        await response_2.aread()
        await response_2.release()
        conn_id_2 = response_2.extensions.get("conn_id")
        
    assert conn_id_1 is not None
    assert conn_id_2 is not None
    assert conn_id_1 == conn_id_2


@pytest.mark.usefixtures("async_environment")
async def test_implicit_release_stream_consumed(server):
    async with httpx_extensions.ExClient() as client:
        async with client.stream("GET", server.url) as response:
            conn_id = response.extensions.get("conn_id")
            assert conn_id is not None
            assert conn_id in client._transport._pool._active_connections
            body = await response.aread()
            assert conn_id not in client._transport._pool._reserved_connections


@pytest.mark.usefixtures("async_environment")
async def test_http2_raises_error(server):
    with pytest.raises(RuntimeError):
        async with httpx_extensions.ExClient(http2=True) as client:
            await client.get(url)