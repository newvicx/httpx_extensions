import typing

from httpx._models import Request, Response
from httpx._types import (
    AsyncByteStream,
    HeaderTypes,
    ResponseContent,
)


class ResponseMixin(Response):
    def __init__(
        self,
        status_code: int,
        *,
        headers: HeaderTypes = None,
        content: ResponseContent = None,
        text: str = None,
        html: str = None,
        json: typing.Any = None,
        stream: AsyncByteStream = None,
        request: Request = None,
        extensions: dict = None,
        history: typing.List["ResponseMixin"] = None,
    ):

        super().__init__(
            status_code,
            headers=headers,
            content=content,
            text=text,
            html=html,
            json=json,
            stream=stream,
            request=request,
            extensions=extensions,
            history=history
        )
        self.release_on_close = False
    
    async def aclose(self) -> None:
        await super().aclose()
        if self.release_on_close:
            await self.release()

    async def release(self) -> None:
        if not self.is_closed:
            # if the response is not closed it likely hasnt been read
            # so a call to aclose here will most likely close the
            # underlying TCP connection which will issue a warning that
            # the connection could not be reserved for subsequent requests.
            # Luckily the most likely times this situation is to occur will
            # be a user auth flow error, event hook error, or some critical
            # error in the connection. In any case that request is most
            # likely unrecoverable anyway so its okay if it cant be reserved
            await self.aclose()
        if hasattr(self, 'stream'):
            if hasattr(self.stream, 'release'):
                await self.stream.release()

    
