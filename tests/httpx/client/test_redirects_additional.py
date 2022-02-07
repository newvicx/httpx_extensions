import pytest

import httpx

import httpx_extensions

from tests.httpx.client.test_redirects import redirects


# Additonal httpx_extensions tests
@pytest.mark.usefixtures("async_environment")
async def test_redirect_uses_existing_connection():
    async with httpx_extensions.ExClient(transport=httpx_extensions.mock.MockTransport(redirects)) as client:
        response = await client.get(
            "https://example.org/multiple_redirects?count=20", follow_redirects=True
        )
    assert response.status_code == httpx.codes.OK
    assert response.url == "https://example.org/multiple_redirects"
    assert len(response.history) == 20
    conn_id = response.extensions.get("conn_id")
    assert conn_id is not None
    assert all([conn_id == previous_response.extensions.get("conn_id") for previous_response in response.history])
