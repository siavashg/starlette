from typing import AsyncGenerator, Callable, Optional, Union

from .._compat import aclosing
from ..datastructures import MutableHeaders
from ..responses import Response
from ..types import ASGIApp, Message, Receive, Scope, Send

_HTTPDispatchFlow = Union[
    AsyncGenerator[None, Response],
    AsyncGenerator[Response, Response],
    AsyncGenerator[Optional[Response], Response],
]


class HTTPMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        dispatch: Optional[Callable[[Scope], _HTTPDispatchFlow]] = None,
    ) -> None:
        self.app = app
        self.dispatch_func = self.dispatch if dispatch is None else dispatch

    def dispatch(self, scope: Scope) -> _HTTPDispatchFlow:
        raise NotImplementedError  # pragma: no cover

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async with aclosing(self.dispatch(scope)) as flow:
            # Kick the flow until the first `yield`.
            # Might respond early before we call into the app.
            maybe_early_response = await flow.__anext__()

            if maybe_early_response is not None:
                await maybe_early_response(scope, receive, send)
                return

            response_started: set = set()

            async def wrapped_send(message: Message) -> None:
                if message["type"] == "http.response.start":
                    response_started.add(True)

                    response = Response(status_code=message["status"])
                    response.raw_headers.clear()

                    try:
                        await flow.asend(response)
                    except StopAsyncIteration:
                        pass
                    else:
                        raise RuntimeError("dispatch() should yield exactly once")

                    headers = MutableHeaders(raw=message["headers"])
                    headers.update(response.headers)

                await send(message)

            try:
                await self.app(scope, receive, wrapped_send)
            except Exception as exc:
                if response_started:
                    raise

                try:
                    response = await flow.athrow(exc)
                except StopAsyncIteration:
                    response = None
                except Exception:
                    # Exception was not handled, or they raised another one.
                    raise

                if response is None:
                    raise RuntimeError(
                        f"dispatch() handled exception {exc!r}, "
                        "but no response was returned"
                    )

                await response(scope, receive, send)
                return

            if not response_started:
                raise RuntimeError("No response returned.")
