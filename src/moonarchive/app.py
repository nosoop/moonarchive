#!/usr/bin/python3


import argparse

import colorama
import msgspec

from .downloaders.youtube import YouTubeDownloader

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
        "--write-description",
        action="store_true",
        help="Writes the stream description to a text file",
    )
    parser.add_argument(
        "--write-thumbnail", action="store_true", help="Writes the thumbnail to an image file"
    )

    args = parser.parse_args()

    downloader = msgspec.convert(vars(args), type=YouTubeDownloader)
    downloader.run()
