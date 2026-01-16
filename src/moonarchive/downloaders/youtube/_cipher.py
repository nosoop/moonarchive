#!/usr/bin/python3

import asyncio
import itertools
from contextvars import ContextVar

import httpx

from ...models import messages as messages
from ._status import status_queue_ctx

cipher_solver_url_ctx: ContextVar[str | None] = ContextVar("cipher_solver_url")
"""
Solver base URL.  Note that this is only used so other tasks can coordinate the context; the
functions themselves take the URL as an argument.
"""


async def decode_n_param_via_cipher_server(
    server_base_url: str, player_url: str, n_param: str | None
) -> str | None:
    if not n_param:
        return None

    status_queue = status_queue_ctx.get()

    # TODO: support more solvers such as yt-dlp/ejs
    headers = {"user-agent": "moonarchive (https://github.com/nosoop/moonarchive)"}
    async with httpx.AsyncClient(headers=headers, base_url=server_base_url) as client:
        for n in itertools.count(1):
            try:
                sig_r = await client.post(
                    "decrypt_signature", json={"n_param": n_param, "player_url": player_url}
                )
                sig_r_data = sig_r.json()
                return sig_r_data.get("decrypted_n_sig")
            except (httpx.TimeoutException, httpx.ConnectError):
                pass

            if status_queue:
                status_queue.put_nowait(
                    messages.StringMessage(f"Failed to get decoded 'n' param (attempt {n})")
                )

            await asyncio.sleep(5)
    raise AssertionError  # unreachable
