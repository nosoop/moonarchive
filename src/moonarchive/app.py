#!/usr/bin/python3


import argparse
import contextlib
import pathlib
import shutil
import textwrap
import typing
from types import ModuleType

import colorama
import msgspec

from .downloaders.youtube import YouTubeDownloader
from .output import CLIMessageHandlers
from .util.paths import OutputPathTemplateCompat

wakepy: ModuleType | None = None
try:
    import wakepy
except ImportError:
    pass

colorama.just_fix_windows_console()

_tw = textwrap.TextWrapper(initial_indent="  ", subsequent_indent="  ")

_FORMAT_TEMPLATE_OPTIONS_EPILOG = """\
FORMAT TEMPLATE OPTIONS
Format template keys are loosely similar to those used in yt-dlp and ytarchive, but (despite being written in Python) do not support the full functionality of Python's string interpolation logic.

Only placeholders in the form '%(key)s' are accepted in moonarchive.

The following keys are available:

id: Video and possible broadcast identifier; identical to video ID if only one broadcast was seen
title: Video title (may be truncated to fit within length restrictions; only use as part of base name)
video_id: Video ID only (only use in directory names, not the base)
channel_id: Channel ID (UC...)
channel: Name of the channel containing the livestream
start_date: Date that the broadcast started in YYYYMMDD form
start_time: Time that the broadcast started in HHMMSS form
year: Year that the broadcast started in YYYY form.
month: Month that the broadcast started in MM form (00-12).
day: Day that the broadcast started in DD form (01-31).
hours: Hour that the broadcast started in HH form (00-24).
minutes: Minute that the broadcast started in MM form (00-60).
seconds: Second that the broadcast started in SS form (00-60).
"""


def _format_epilog_section(section: str) -> str:
    """
    Formats an epilog section such that lines following the header are indented and wrapped
    based on terminal width.
    """

    _tw.width = shutil.get_terminal_size().columns

    def _() -> typing.Iterable[str]:
        header, *rest = section.splitlines()
        yield header
        for line in rest:
            if not line:
                yield line  # keep the empty lines that are dropped by TextWrapper
            yield from _tw.wrap(line)

    return "\n".join(_())


def main() -> None:
    parser = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_format_epilog_section(_FORMAT_TEMPLATE_OPTIONS_EPILOG),
    )

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
        help="Base location for outputs (created if nonexistent; defaults to working directory)",
    )
    parser.add_argument(
        "--output-template",
        type=OutputPathTemplateCompat,
        help=(
            "Template string for output filename excluding extension "
            "(paths relative to --output-directory permitted; defaults to '%%(title)s-%%(id)s')"
        ),
    )
    parser.add_argument(
        "--progress-style",
        type=str,
        choices=[
            handler.tag
            for handler in msgspec.inspect.multi_type_info(typing.get_args(CLIMessageHandlers))
            if isinstance(handler, msgspec.inspect.StructType)
        ],
        default="ytarchive",
        help="Style to use for displaying progress results",
    )
    parser.add_argument(
        "-k",
        "--keep-ts-files",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Keep the raw downloaded files instead of deleting them on successful mux operations",
    )
    parser.add_argument(
        "--max-video-resolution",
        type=int,
        default=None,
        help="Maximum video resolution that the tool is permitted to download.  This filters "
        "on the lesser dimension (the value checked for 1920x1080 and 1080x1920 is 1080).  "
        "If no value is provided, the tool will attempt to download the highest resolution "
        "available.",
    )
    parser.add_argument(
        "--vp9",
        action=argparse.BooleanOptionalAction,
        dest="prioritize_vp9",
        help="Prioritizes vp9 over h264 when both codecs are present at a target resolution.  "
        "If h264 is not available at the resolution target, vp9 is always selected regardless "
        "of this option's presence.",
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
        help="Cookies file path.  If --cookies-from-browser is specified, this expects the "
        "browser-specific cookie store.  Otherwise, it expects a cookie file in Netscape "
        "format.",
    )
    parser.add_argument(
        "--cookies-from-browser",
        type=str,
        help="Specifies a browser to load cookies from.",
    )
    parser.add_argument(
        "--list-formats",
        action="store_true",
        help="Provide a list of currently available formats and exit without writing any files "
        "(note that formats availability may change throughout a broadcast under various "
        "conditions)",
    )
    parser.add_argument(
        "-j",
        "--num-parallel-downloads",
        type=int,
        help="Maximum number of requests allowed to be in flight for each stream",
        default=1,
    )
    parser.add_argument(
        "--po-token",
        type=str,
        help="Proof of origin token; optional, but prevents fast expirations of streams, "
        "causing frequent player refreshes and possibly triggering bot detection systems "
        "if multiple instances are running",
    )
    parser.add_argument(
        "--visitor-data",
        type=str,
        help="Visitor data to be used in place of cookies when not logged in",
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

        handler = msgspec.convert({"type": args.progress_style}, CLIMessageHandlers)

        downloader = msgspec.convert(vars(args), type=YouTubeDownloader)
        downloader.handlers.append(handler)
        downloader.run()
