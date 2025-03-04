import logging
from functools import partial
from typing import Any, Awaitable, Callable, Optional, Union

from langchain.chains.base import Chain
from sse_starlette.sse import EventSourceResponse, ServerSentEvent, ensure_bytes
from starlette.background import BackgroundTask
from starlette.types import Receive, Send

from lanarky.callbacks import (
    AsyncLanarkyCallback,
    get_streaming_callback,
    get_streaming_json_callback,
)

logger = logging.getLogger(__name__)


class StreamingResponse(EventSourceResponse):
    """StreamingResponse class wrapper for langchain chains."""

    def __init__(
        self,
        chain_executor: Callable[[Send], Awaitable[Any]],
        background: Optional[BackgroundTask] = None,
        **kwargs: Any,
    ) -> None:
        """Constructor method.

        Args:
            chain_executor: function to execute ``chain.acall()``.
            background: A ``BackgroundTask`` object to run in the background.
        """
        super().__init__(content=iter(()), background=background, **kwargs)

        self.chain_executor = chain_executor

    async def listen_for_disconnect(self, receive: Receive) -> None:
        """Listen for client disconnect."""
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                print("Client disconnected")
                break

    async def stream_response(self, send: Send) -> None:
        """Streams the response."""
        await send(
            {
                "type": "http.response.start",
                "status": self.status_code,
                "headers": self.raw_headers,
            }
        )

        try:
            outputs = await self.chain_executor(send)
            if self.background is not None:
                self.background.kwargs.update({"outputs": outputs})
        except Exception as e:
            logger.error(f"chain execution error: {e}")
            if self.background is not None:
                self.background.kwargs.update({"outputs": {}, "error": e})
            # FIXME: use enum instead of hardcoding event name
            chunk = ServerSentEvent(
                data=dict(status_code=500, detail="Internal Server Error"),
                event="error",
            )
            await send(
                {
                    "type": "http.response.body",
                    "body": ensure_bytes(chunk, None),
                    "more_body": True,
                }
            )

        await send({"type": "http.response.body", "body": b"", "more_body": False})

    @staticmethod
    def _create_chain_executor(
        chain: Chain,
        inputs: Union[dict[str, Any], Any],
        as_json: bool = False,
        callback: Optional[AsyncLanarkyCallback] = None,
        **callback_kwargs,
    ) -> Callable[[Send], Awaitable[Any]]:
        """Creates a function to execute ``chain.acall()``.

        Args:
            chain: A ``Chain`` object.
            inputs: Inputs to pass to ``chain.acall()``.
            as_json: Whether to return the outputs as JSON.
            callback_kwargs: Keyword arguments to pass to the callback function.
        """
        if callback is None:
            get_callback_fn = (
                get_streaming_json_callback if as_json else get_streaming_callback
            )
            callback = partial(get_callback_fn, chain)

        async def wrapper(send: Send):
            return await chain.acall(
                inputs=inputs,
                callbacks=[callback(send=send, **callback_kwargs)],
            )

        return wrapper

    @classmethod
    def from_chain(
        cls,
        chain: Chain,
        inputs: Union[dict[str, Any], Any],
        as_json: bool = False,
        background: Optional[BackgroundTask] = None,
        callback: Optional[AsyncLanarkyCallback] = None,
        callback_kwargs: dict[str, Any] = {},
        **kwargs: Any,
    ) -> "StreamingResponse":
        """Creates a ``StreamingResponse`` object from a ``Chain`` object.

        Args:
            chain: A ``Chain`` object.
            inputs: Inputs to pass to ``chain.acall()``.
            as_json: Whether to return the outputs as JSON.
            background: A ``BackgroundTask`` object to run in the background.
            callback: custom callback function to use instead of using the registry.
            callback_kwargs: Keyword arguments to pass to the callback function.
        """
        chain_executor = cls._create_chain_executor(
            chain, inputs, as_json, callback, **callback_kwargs
        )

        return cls(
            chain_executor=chain_executor,
            background=background,
            **kwargs,
        )
