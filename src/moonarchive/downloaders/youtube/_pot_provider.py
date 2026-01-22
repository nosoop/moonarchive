#!/usr/bin/python3

import asyncio
import datetime
import itertools

import httpx
import msgspec

from ...models import messages as messages
from ._status import status_queue_ctx


class POTokenPingResponse(msgspec.Struct):
    server_uptime: float
    version: str


class POTokenProviderRequest(msgspec.Struct, omit_defaults=True):
    content_binding: str | None
    proxy: str | None = None
    bypass_cache: bool | None = None
    innertube_context: dict | None = None


class POTokenProviderResponse(msgspec.Struct, rename="camel"):
    content_binding: str
    po_token: str
    expires_at: datetime.datetime


async def get_provider_version(base_url: str | None) -> POTokenPingResponse | None:
    if base_url is None:
        return None
    for n in itertools.count(1):
        try:
            async with httpx.AsyncClient(base_url=base_url) as client:
                r = await client.get("/ping")
                r.raise_for_status()
                return msgspec.convert(r.json(), type=POTokenPingResponse)
        except (httpx.HTTPStatusError, httpx.TimeoutException):
            pass
        status_queue = status_queue_ctx.get()
        if status_queue:
            status_queue.put_nowait(
                messages.StringMessage(f"Failed to get POToken ping response (attempt {n})")
            )
        await asyncio.sleep(5)
    raise AssertionError  # unreachable


async def get_potoken(
    base_url: str | None, content_binding: str | None, innertube_context: dict | None = None
) -> POTokenProviderResponse | None:
    if base_url is None:
        return None
    request = POTokenProviderRequest(content_binding=content_binding)

    if innertube_context:
        request.innertube_context = innertube_context

    for n in itertools.count(1):
        try:
            async with httpx.AsyncClient(base_url=base_url) as client:
                r = await client.post("/get_pot", json=msgspec.structs.asdict(request))
                r.raise_for_status()
                return msgspec.convert(r.json(), type=POTokenProviderResponse)
        except (httpx.HTTPStatusError, httpx.TimeoutException):
            pass
        status_queue = status_queue_ctx.get()
        if status_queue:
            status_queue.put_nowait(
                messages.StringMessage(f"Failed to get POToken token response (attempt {n})")
            )
        await asyncio.sleep(5)
    raise AssertionError  # unreachable
