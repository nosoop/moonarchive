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

    args = parser.parse_args()

    resp = extract_player_response(args.url)
    manifest = resp.streaming_data.get_dash_manifest()

    target_duration = resp.streaming_data.adaptive_formats[0].target_duration_sec

    # if you start late you must reset the PTS
    max_seq = 0
    for fragnum in range(10):
        url = manifest.format_urls[299]

        output_path = pathlib.Path("entry.f299.ts")
        print(f"downloading video chunk {fragnum}")
        with urllib.request.urlopen(
            url.substitute(sequence=fragnum), timeout=target_duration * 2
        ) as resp, output_path.open("ab") as o:
            max_seq = int(resp.getheader("X-Head-Seqnum", -1))
            shutil.copyfileobj(resp, o)

    max_seq = 0
    for fragnum in range(10):
        url = manifest.format_urls[140]
        output_path = pathlib.Path("entry.f140.ts")
        print(f"downloading audio chunk {fragnum}")
        with urllib.request.urlopen(
            url.substitute(sequence=fragnum), timeout=target_duration * 2
        ) as resp, output_path.open("ab") as o:
            max_seq = int(resp.getheader("X-Head-Seqnum", -1))
            shutil.copyfileobj(resp, o)

    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "warning",
            "-stats",
            "-y",
            "-i",
            "entry.f299.ts",
            "-i",
            "entry.f140.ts",
            "-c",
            "copy",
            "-movflags",
            "faststart",
            "entry.mp4",
        ]
    )
