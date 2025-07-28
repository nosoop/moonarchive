#!/usr/bin/python3

import asyncio
import dataclasses
import pathlib
from typing import Collection


@dataclasses.dataclass
class FFMPEGRunner:
    """
    Wrapper to spawn an ffmpeg process with preset operations.
    """

    bin_path: pathlib.Path | None
    """ Path to the ffmpeg binary.  If None, lookup is performed on PATH. """

    stats: bool = dataclasses.field(default=True, kw_only=True)
    """ Whether or not stats are dumped to standard error. """

    async def create_remux_process(
        self, input_files: Collection[pathlib.Path], output_file: pathlib.Path
    ) -> asyncio.subprocess.Process:
        """
        Spawns a process to remux the given input file(s) to a single output file.
        """

        # raising the log level to 'fatal' instead of 'warning' suppresses MOOV atom warnings
        # and unknown webm:vp9 element errors
        # those warnings being dumped to stdout has a non-negligible performance impact
        program = str(self.bin_path.absolute()) if self.bin_path else "ffmpeg"
        command = [
            "-v",
            "fatal",
            "-progress",
            "-",
            "-nostdin",
            "-xerror",
            "-y",
        ]

        if self.stats:
            command += ("-stats",)

        for input_file in input_files:
            command += (
                "-seekable",
                "0",
                "-thread_queue_size",
                "1024",
                "-bsf",
                "setts=dts='max(PREV_OUTDTS+1,DTS)'",  # prevent non-monotonic DTS errors
                "-i",
                str(input_file.absolute()),
            )

        command += (
            "-c",
            "copy",
            "-movflags",
            "faststart",
            "-fflags",
            "bitexact",
            str(output_file.absolute()),
        )

        return await asyncio.create_subprocess_exec(
            program,
            *command,
            stdout=asyncio.subprocess.PIPE,
        )
