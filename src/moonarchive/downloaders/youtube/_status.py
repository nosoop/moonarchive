#!/usr/bin/python3

import asyncio
import dataclasses
from contextvars import ContextVar

from ...output import BaseMessageHandler

# status queue for downloading tasks; this is available in stream_downloader and frag_iterator
status_queue_ctx: ContextVar[asyncio.Queue] = ContextVar("status_queue")


@dataclasses.dataclass
class StatusManager:
    queue: asyncio.Queue

    # bind the lifetime of the manager to the task creating it
    parent_task: asyncio.Task

    def __init__(self):
        self.queue = asyncio.Queue()
        self.parent_task = asyncio.current_task()


async def status_handler(
    handlers: list[BaseMessageHandler],
    status: StatusManager,
) -> None:
    while not status.parent_task.done() or not status.queue.empty():
        try:
            message = await asyncio.wait_for(status.queue.get(), timeout=1.0)
            for handler in handlers:
                await handler.handle_message(message)
        except TimeoutError:
            pass
