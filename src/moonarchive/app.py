#!/usr/bin/python3

import argparse
import concurrent.futures
import html.parser
import pathlib
import shutil
import socket
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


def frag_iterator(resp: YTPlayerResponse, itag: int):
    # yields fragment information
    # this should refresh the manifest as needed and yield the next available fragment
    # if the format changes, signal a reset on the consumer somehow
    # maybe create a FormatChangedException?
    manifest = resp.streaming_data.get_dash_manifest()
    timeout = resp.streaming_data.adaptive_formats[0].target_duration_sec

    video_url = f"https://youtu.be/{resp.video_details.video_id}"
    cur_seq = 0
    max_seq = 0
    num_empty_results = 0
    while True:
        url = manifest.format_urls[itag]

        # formats may change throughout the stream, and this should coincide with a sequence reset
        # in that case we would need to restart the download on a second file

        # TODO: gracefully handle the following
        #       - end of stream
        #       - extended stream dropouts (we should refresh manifest / player)
        #       - unavailable formats
        #       - sequence resets

        try:
            with urllib.request.urlopen(
                url.substitute(sequence=cur_seq), timeout=timeout * 2
            ) as fresp:
                new_max_seq = int(fresp.getheader("X-Head-Seqnum", -1))
                yield fresp

                if new_max_seq < max_seq:
                    # this would be considered very bad
                    # it's currently not known if the stream would actually report this, or if we
                    # would need to fetch a new manifest / player response
                    pass
                max_seq = new_max_seq
            print(f"fragments {itag}: {cur_seq}/{max_seq}")
            cur_seq += 1
        except socket.timeout:
            continue
        except urllib.error.HTTPError as err:
            resp = extract_player_response(video_url)
            if err.code == 403:
                # retrieve a fresh manifest
                manifest = resp.streaming_data.get_dash_manifest()
            elif err.code == 404:
                # we're done if the stream is no longer live and a duration is rendered
                if (
                    not resp.microformat.live_broadcast_details.is_live_now
                    and resp.video_details.num_length_seconds
                ):
                    return
        except urllib.error.URLError:
            continue


def stream_downloader(resp: YTPlayerResponse, format_itag: int, output_path: pathlib.Path):
    # thread for managing the download of a specific format
    for fresp in frag_iterator(resp, format_itag):
        # formats may change throughout the stream, and this should coincide with a sequence reset
        # in that case we would need to restart the download on a second file

        with output_path.open("ab") as o:
            shutil.copyfileobj(fresp, o)


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument("url", type=str)
    parser.add_argument("-n", "--dry-run", action="store_true")

    args = parser.parse_args()
    resp = extract_player_response(args.url)

    if not resp.streaming_data:
        timestamp = resp.microformat.live_broadcast_details.start_timestamp
        print(f"No stream available (scheduled to start at {timestamp})")
        return

    manifest = resp.streaming_data.get_dash_manifest()

    preferred_format, *_ = resp.streaming_data.sorted_video_formats
    timeout = resp.streaming_data.adaptive_formats[0].target_duration_sec

    print(f"Video title: {resp.video_details.title}")
    print(f"Selected format: {preferred_format.quality_label}")

    if args.dry_run:
        return

    video_id = resp.video_details.video_id
    output_video_path = pathlib.Path(f"{video_id}.f{preferred_format.itag}.ts")
    output_audio_path = pathlib.Path(f"{video_id}.f140.ts")

    with concurrent.futures.ProcessPoolExecutor() as executor:
        # tasks to write streams to file
        video_stream_dl = executor.submit(
            stream_downloader, resp, preferred_format.itag, output_video_path
        )
        audio_stream_dl = executor.submit(stream_downloader, resp, 140, output_audio_path)
        for future in concurrent.futures.as_completed((video_stream_dl, audio_stream_dl)):
            exc = future.exception()
            if exc:
                print(type(exc), exc)

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
