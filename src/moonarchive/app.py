#!/usr/bin/python3


import argparse
import contextlib
import pathlib
from types import ModuleType

import colorama
import msgspec

from .downloaders.youtube import YouTubeDownloader
from .output import YTArchiveMessageHandler

wakepy: ModuleType | None = None
try:
    import wakepy
except ImportError:
    pass

colorama.just_fix_windows_console()


def main() -> None:
    parser = argparse.ArgumentParser()

    parser.add_argument("url", type=str)
    parser.add_argument("-n", "--dry-run", action="store_true")
    parser.add_argument(
        "--poll-interval",
        type=int,
        help="Rechecks the stream at an interval prior to its scheduled start time",
        default=0,
    )
    parser.add_argument(
        "--poll-unavailable-interval",
        type=int,
        help="Rechecks the stream at this interval in seconds if it went private "
        "before going live; 0 to exit instead",
        default=0,
    )
    parser.add_argument(
        "--schedule-offset",
        type=int,
        help="Number of seconds ahead of the scheduled start time for rechecks "
        "(e.g. 300 if a given streamer goes online 5 minutes early)",
        default=0,
    )
    parser.add_argument(
        "--write-description",
        action="store_true",
        help="Writes the stream description to a text file",
    )
    parser.add_argument(
        "--write-thumbnail", action="store_true", help="Writes the thumbnail to an image file"
    )
    parser.add_argument(
        "--keep-awake",
        action=argparse.BooleanOptionalAction,
        help="Ensures the system stays awake while the process is running",
        default=True,
    )
    parser.add_argument(
        "--staging-directory",
        type=pathlib.Path,
        help="Location for intermediary files (created if nonexistent; defaults to working directory)",
    )
    parser.add_argument(
        "--output-directory",
        type=pathlib.Path,
        help="Location for outputs (created if nonexistent; defaults to working directory)",
    )
    parser.add_argument(
        "--max-video-resolution",
        type=int,
        default=None,
        help="Maximum resolution for video streams (best resolution if omitted)",
    )
    parser.add_argument(
        "--vp9",
        action=argparse.BooleanOptionalAction,
        dest="prioritize_vp9",
        help="Prioritizes vp9 over h264 when both codecs are present at a given resolution",
        default=False,
    )
    parser.add_argument(
        "--ffmpeg-path",
        type=pathlib.Path,
        help="Path to ffmpeg binary, if there isn't one you want to use in your PATH",
    )
    parser.add_argument(
        "-c",
        "--cookies",
        type=pathlib.Path,
        dest="cookie_file",
        help="Cookies file in Netscape format",
    )
    parser.add_argument(
        "--list-formats",
        action="store_true",
        help="Provide a list of currently available formats and exit without writing any files",
    )
    parser.add_argument(
        "-j",
        "--num-parallel-downloads",
        type=int,
        help="Maximum number of requests allowed to be in flight for each stream",
        default=1,
    )

    args = parser.parse_args()

    with contextlib.ExitStack() as context:
        if args.keep_awake:
            if not wakepy:
                # right now wakepy is completely optional, but at the same time we want to
                # remind users that their session may sleep when it's not available
                raise ValueError(
                    "wakepy is not installed; pass --no-keep-awake to suppress this error "
                    "or install the 'keepawake' optional dependency set"
                )
            context.enter_context(wakepy.keep.running())

        handler = YTArchiveMessageHandler()

        downloader = msgspec.convert(vars(args), type=YouTubeDownloader)
        downloader.handlers.append(handler)
        downloader.run()
