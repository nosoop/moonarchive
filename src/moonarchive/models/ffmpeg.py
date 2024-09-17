#!/usr/bin/python3

import asyncio
from typing import AsyncIterator

import msgspec


class FFMPEGProgress(msgspec.Struct, kw_only=True):
    """
    Structured representation of ffmpeg progress output.
    Available fields are listed under fftools/ffmpeg.c::print_report()
    """

    # stream encode quality is formatted under "stream_%d_%d_q="
    frame: int | None = None
    fps: float | None = None

    # represented as a numeric value with suffix "kbits/s"
    bitrate: str | None = None

    # output size in bytes, if output is non-null
    total_size: int | None = None

    # output stream duration in microseconds
    # this may be not available if AV_NOPTS_VALUE is set
    # we don't parse out_time_ms because it's equal to out_time_us (mislabeled?)
    out_time_us: int | None = None
    out_time: str | None = None

    dup_frames: int | None = None
    drop_frames: int | None = None

    # represented as a numeric string (in scientific notation if >10e3) with a trailing 'x'
    # possible to be None -- ffmpeg reports this as "N/A" if speed is a negative value
    speed: str | None = None

    @classmethod
    async def from_process_stream(
        cls, stdout: asyncio.StreamReader | None
    ) -> AsyncIterator["FFMPEGProgress"]:
        """
        Yields instances from an open asyncio stream.

        The application must be launched with ("-progress", "-") for ffmpeg to report
        machine-parseable progress information back to the caller.  "-stats" may also be
        omitted to suppress ffmpeg's usual reporting to standard output.

        ffmpeg reports progress as a series of line-delimited key / value pairs with a
        "progress" key marking the end of a given progress update.
        """
        if not stdout:
            return
        state: dict[str, str] = {}
        while True:
            raw_line = await stdout.readline()
            if not raw_line:
                return
            key, value = raw_line.decode().strip().split("=", 1)
            if key == "progress":
                yield msgspec.convert(state, type=cls, strict=False)
                state.clear()
                continue
            elif value == "N/A":
                # omit fields that are represented as 'N/A' in ffmpeg output
                continue
            state[key] = value
