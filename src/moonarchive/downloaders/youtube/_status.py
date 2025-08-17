#!/usr/bin/python3

import asyncio
from contextvars import ContextVar

# status queue for downloading tasks; this is available in stream_downloader and frag_iterator
status_queue_ctx: ContextVar[asyncio.Queue] = ContextVar("status_queue")
