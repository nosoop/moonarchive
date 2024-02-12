#!/usr/bin/python3

import argparse
import html.parser
import pathlib
import shutil
import subprocess
import urllib.request
from typing import Optional

import msgspec
import requests

from moonarchive.models.dash_manifest import YTDashManifest
from moonarchive.models.youtube_player import YTPlayerResponse


def create_json_object_extractor(decl: str):
    class InternalHTMLParser(html.parser.HTMLParser):
        in_script: bool = False
        result = None

        def handle_starttag(self, tag, attrs):
            self.in_script = tag == "script"

        def handle_endtag(self, tag):
            self.in_script = False

        def handle_data(self, data):
            if not self.in_script:
                return

            decl_pos = data.find(decl)
            if decl_pos == -1:
                return

            start_pos = data[decl_pos:].find("{") + decl_pos
            end_pos = data[start_pos:].rfind("};") + 1 + start_pos

            self.result = data[start_pos:end_pos]

    return InternalHTMLParser


PlayerResponseExtractor = create_json_object_extractor("var ytInitialPlayerResponse =")


def extract_player_response(url: str):
    response_extractor = PlayerResponseExtractor()

    r = requests.get(url)
    response_extractor.feed(r.text)

    if not response_extractor.result:
        return None
    return msgspec.json.decode(response_extractor.result, type=YTPlayerResponse)


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument("url", type=str)
    parser.add_argument("-n", "--dry-run", action="store_true")

    args = parser.parse_args()

    resp = extract_player_response(args.url)

    if not resp.streaming_data:
        timestamp = (
            resp.microformat.player_microformat_renderer.live_broadcast_details.start_timestamp
        )
        print(f"No stream available (scheduled to start at {timestamp})")
        return

    manifest = resp.streaming_data.get_dash_manifest()

    preferred_format, *_ = resp.streaming_data.sorted_video_formats
    timeout = resp.streaming_data.adaptive_formats[0].target_duration_sec

    if args.dry_run:
        return

    video_id = resp.video_details.video_id
    output_video_path = pathlib.Path(f"{video_id}.f{preferred_format.itag}.ts")
    output_audio_path = pathlib.Path(f"{video_id}.f140.ts")

    # if you're dealing with a partial stream you should reset the PTS
    max_seq = 0
    for fragnum in range(10):
        url = manifest.format_urls[preferred_format.itag]

        # formats may change throughout the stream, and this should coincide with a sequence reset
        # in that case we would need to restart the download on a second file

        print(f"downloading video chunk {fragnum}")

        with urllib.request.urlopen(
            url.substitute(sequence=fragnum), timeout=timeout * 2
        ) as fresp, output_video_path.open("ab") as o:
            max_seq = int(fresp.getheader("X-Head-Seqnum", -1))
            shutil.copyfileobj(fresp, o)

    max_seq = 0
    for fragnum in range(10):
        url = manifest.format_urls[140]
        print(f"downloading audio chunk {fragnum}")
        with urllib.request.urlopen(
            url.substitute(sequence=fragnum), timeout=timeout * 2
        ) as fresp, output_audio_path.open("ab") as o:
            max_seq = int(fresp.getheader("X-Head-Seqnum", -1))
            shutil.copyfileobj(fresp, o)

    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "warning",
            "-stats",
            "-y",
            "-i",
            str(output_video_path),
            "-i",
            str(output_audio_path),
            "-c",
            "copy",
            "-movflags",
            "faststart",
            f"{video_id}.mp4",
        ]
    )
