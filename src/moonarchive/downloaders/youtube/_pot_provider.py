#!/usr/bin/python3

import datetime

import httpx
import msgspec


class POTokenPingResponse(msgspec.Struct):
    server_uptime: float
    version: str


class POTokenProviderRequest(msgspec.Struct, omit_defaults=True):
    content_binding: str | None
    proxy: str | None = None
    bypass_cache: bool | None = None


class POTokenProviderResponse(msgspec.Struct, rename="camel"):
    content_binding: str
    po_token: str
    expires_at: datetime.datetime


async def get_provider_version(base_url: str | None) -> POTokenPingResponse | None:
    if base_url is None:
        return None
    try:
        async with httpx.AsyncClient(base_url=base_url) as client:
            r = await client.get("/ping")
            r.raise_for_status()
            return msgspec.convert(r.json(), type=POTokenPingResponse)
    except (httpx.HTTPStatusError, msgspec.ValidationError):
        pass
    return None


async def get_potoken(
    base_url: str | None, content_binding: str | None
) -> POTokenProviderResponse | None:
    if base_url is None:
        return None
    request = POTokenProviderRequest(content_binding=content_binding)
    try:
        async with httpx.AsyncClient(base_url=base_url) as client:
            r = await client.post("/get_pot", json=msgspec.structs.asdict(request))
            r.raise_for_status()
            return msgspec.convert(r.json(), type=POTokenProviderResponse)
    except (httpx.HTTPStatusError, msgspec.ValidationError):
        pass
    return None
