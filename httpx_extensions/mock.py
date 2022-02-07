import asyncio
import typing
import uuid
from types import TracebackType

from httpcore.backends.mock import AsyncMockBackend
from httpx import Request

from .models import ResponseMixin
from .transport import AsyncHTTPTransportMixin

A = typing.TypeVar("A", bound="MockTransport")

class MockTransport(AsyncHTTPTransportMixin):
    def __init__(self, handler: typing.Callable) -> None:
        self.handler = handler

    async def handle_async_request(
        self,
        request: Request,
    ) -> ResponseMixin:
        await request.aread()
        response = self.handler(request)

        # Allow handler to *optionally* be an `async` function.
        # If it is, then the `response` variable need to be awaited to actually
        # return the result.

        # https://simonwillison.net/2020/Sep/2/await-me-maybe/
        if asyncio.iscoroutine(response):
            response = await response
        if request.extensions.get("conn_id") is not None:
            response.extensions.update(
                {"conn_id": request.extensions.get("conn_id")}
            )
        else:
            response.extensions.update({"conn_id": uuid.uuid4().hex})
        return response

    async def aclose(self) -> None:
        pass
    
    async def __aenter__(self: A) -> A:
        return self

    async def __aexit__(
        self,
        exc_type: typing.Type[BaseException] = None,
        exc_value: BaseException = None,
        traceback: TracebackType = None,
    ) -> None:
        await self.aclose()
