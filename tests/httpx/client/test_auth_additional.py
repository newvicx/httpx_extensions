import pytest
import typing

import httpx

import httpx_extensions
from tests.httpx.client.test_auth import App



class ReuseConnFlow(httpx.Auth):

    def auth_flow(self, request: httpx.Request) -> typing.Generator[httpx.Request, httpx_extensions.ResponseMixin, None]:
        response_1 = yield request
        conn_id = response_1.extensions.get("conn_id")
        request.extensions.update({"conn_id": conn_id})
        yield request


@pytest.mark.asyncio
async def test_reuse_connection_in_auth_flow() -> None:
    url = "https://example.org/"
    auth = ReuseConnFlow()
    app = App()

    async with httpx_extensions.ExClient(transport=httpx_extensions.mock.MockTransport(app)) as client:
        response = await client.get(url, auth=auth)

    conn_id = response.extensions.get("conn_id")
    assert conn_id is not None
    compare = response.history[0].extensions.get("conn_id")
    assert conn_id == compare