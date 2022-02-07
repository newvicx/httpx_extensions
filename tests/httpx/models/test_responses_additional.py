import pytest

import httpx_extensions
from .test_responses import async_streaming_body

# Additonal httpx_extensions tests
class Response(httpx_extensions.ResponseMixin):
    def __init__(self, name, events, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.name = name
        self.events = events

    async def aread(self):
        self.events.append(f"{self.name}.aread (Called)")
        self.events.append(f"{self.name}.is_closed = {self.is_closed}")
        await super().aread()

    async def aclose(self):
        self.events.append(f"{self.name}.aclose (Called)")
        self.events.append(f"{self.name}.is_closed = {self.is_closed}")
        await super().aclose()

    async def release(self):
        self.events.append(f"{self.name}.release (Called)")
        self.events.append(f"{self.name}.is_closed = {self.is_closed}")
        await super().release()
        self.events.append(f"{self.name} released")
        self.events.append(f"{self.name}.is_closed = {self.is_closed}")


@pytest.mark.asyncio
async def test_release_on_close():
    events = []
    response = Response(
        "response",
        events,
        200,
        content=async_streaming_body(),
    )
    response.release_on_close = True
    content = await response.aread()
    assert events == [
        "response.aread (Called)",
        "response.is_closed = False",
        "response.aclose (Called)",
        "response.is_closed = False",
        "response.release (Called)",
        "response.is_closed = True",
        "response released",
        "response.is_closed = True",
    ]


@pytest.mark.asyncio
async def test_release():
    events = []
    response = Response(
        "response",
        events,
        200,
        content=async_streaming_body(),
    )
    content = await response.aread()
    await response.release()
    assert events == [
        "response.aread (Called)",
        "response.is_closed = False",
        "response.aclose (Called)",
        "response.is_closed = False",
        "response.release (Called)",
        "response.is_closed = True",
        "response released",
        "response.is_closed = True",
    ]

@pytest.mark.asyncio
async def test_close_on_release():
    events = []
    response = Response(
        "response",
        events,
        200,
        content=async_streaming_body(),
    )
    await response.release()
    assert events == [
        "response.release (Called)",
        "response.is_closed = False",
        "response.aclose (Called)",
        "response.is_closed = False",
        "response released",
        "response.is_closed = True",
    ]
