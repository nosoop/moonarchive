#!/usr/bin/python3

import argparse
import concurrent.futures
import datetime
import html.parser
import http.client
import io
import multiprocessing as mp
import pathlib
import queue
import shutil
import socket
import subprocess
import time
import urllib.request

import colorama
import colorama.ansi
import requests

colorama.just_fix_windows_console()

from typing import Optional

import msgspec

import moonarchive.models.messages as messages
import moonarchive.output
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


class FragmentInfo(msgspec.Struct, kw_only=True):
    cur_seq: int
    max_seq: int
    manifest_id: str
    buffer: io.BytesIO


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

    current_manifest_id = resp.streaming_data.dash_manifest_id

    # this is supposed to increment on failure before we refetch the player response
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

                # copying to a buffer here ensures this function handles errors related to the request
                buffer = io.BytesIO(fresp.read())

                info = FragmentInfo(
                    cur_seq=cur_seq,
                    max_seq=new_max_seq,
                    manifest_id=current_manifest_id,
                    buffer=buffer,
                )
                yield info

                max_seq = new_max_seq
            cur_seq += 1
        except socket.timeout:
            continue
        except urllib.error.HTTPError as err:
            resp = extract_player_response(video_url)
            if err.code == 403:
                # retrieve a fresh manifest
                manifest = resp.streaming_data.get_dash_manifest()
            elif err.code == 404:
                if not resp.microformat or not resp.microformat.live_broadcast_details:
                    # video is private?
                    return

                # the server has indicated that no fragment is present
                # we're done if the stream is no longer live and a duration is rendered
                if (
                    not resp.microformat.live_broadcast_details.is_live_now
                    and resp.video_details.num_length_seconds
                ):
                    return

                if not resp.streaming_data:
                    # stream is offline
                    time.sleep(15)
                    continue

                if current_manifest_id != resp.streaming_data.dash_manifest_id:
                    print(
                        "manifest ID changed from",
                        current_manifest_id,
                        "to",
                        resp.streaming_data.dash_manifest_id,
                        "- manually issue a redownload for now",
                    )
                    cur_seq = 0
                    max_seq = 0
                    current_manifest_id = resp.streaming_data.dash_manifest_id
        except urllib.error.URLError:
            continue
        except http.client.IncompleteRead:
            continue


def status_handler(
    handler: moonarchive.output.BaseMessageHandler,
    status_queue: mp.Queue,
    handler_stop: mp.Event,
):
    while not handler_stop.is_set() or not status_queue.empty():
        try:
            message = status_queue.get(timeout=1.0)
            handler.handle_message(message)
        except queue.Empty:
            pass


def stream_downloader(
    resp: YTPlayerResponse,
    format_itag: int,
    output_path: pathlib.Path,
    status_queue: mp.Queue,
):
    # thread for managing the download of a specific format
    # we need this to be more robust against available format changes mid-stream
    for frag in frag_iterator(resp, format_itag):
        # formats may change throughout the stream, and this should coincide with a sequence reset
        # in that case we would need to restart the download on a second file

        with output_path.open("ab") as o:
            shutil.copyfileobj(frag.buffer, o)

        frag.buffer.seek(0)
        with pathlib.Path(f"{frag.manifest_id}.f{format_itag}.ts").open("ab") as o:
            shutil.copyfileobj(frag.buffer, o)

        status_queue.put(
            messages.FragmentMessage(
                frag.cur_seq,
                frag.max_seq,
                format_itag,
                frag.manifest_id,
                frag.buffer.getbuffer().nbytes,
            )
        )
    print(f"download process for format {format_itag} complete")


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument("url", type=str)
    parser.add_argument("-n", "--dry-run", action="store_true")
    parser.add_argument(
        "--poll-interval",
        type=int,
        help="Rechecks the stream at an interval prior to its scheduled start time",
        default=0,
    )
    parser.add_argument("--write-description", action="store_true")

    args = parser.parse_args()
    resp = extract_player_response(args.url)

    if resp.playability_status.status in ("ERROR", "LOGIN_REQUIRED"):
        print(f"{resp.playability_status.status}: {resp.playability_status.reason}")
        return

    while not resp.streaming_data:
        timestamp = resp.microformat.live_broadcast_details.start_datetime

        # post-scheduled recheck interval
        seconds_wait = 20.0
        if timestamp:
            now = datetime.datetime.now(datetime.timezone.utc)

            seconds_remaining = (timestamp - now).total_seconds()
            print(
                f"No stream available (scheduled to start in {int(seconds_remaining)}s at {timestamp})"
            )

            if seconds_remaining > 0:
                seconds_wait = seconds_remaining
                if args.poll_interval > 0 and seconds_wait > args.poll_interval:
                    seconds_wait = args.poll_interval
        else:
            print(f"No stream available, polling")

        time.sleep(seconds_wait)

        resp = extract_player_response(args.url)

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

    if args.write_description:
        desc_path = pathlib.Path(f"{video_id}.description")
        desc_path.write_text(resp.video_details.short_description, encoding="utf8")

    with concurrent.futures.ProcessPoolExecutor() as executor:
        handler = moonarchive.output.YTArchiveMessageHandler()
        status_manager = mp.Manager()

        status_queue = status_manager.Queue()
        handler_stop = status_manager.Event()

        # tasks to receive events from stream downloading jobs
        status_proc = executor.submit(status_handler, handler, status_queue, handler_stop)

        # tasks to write streams to file
        video_stream_dl = executor.submit(
            stream_downloader,
            resp,
            preferred_format.itag,
            output_video_path,
            status_queue,
        )
        audio_stream_dl = executor.submit(
            stream_downloader, resp, 140, output_audio_path, status_queue
        )
        for future in concurrent.futures.as_completed((video_stream_dl, audio_stream_dl)):
            exc = future.exception()
            if exc:
                print(type(exc), exc)

        handler_stop.set()

    print()

    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "warning",
            "-stats",
            "-y",
            "-seekable",
            "0",
            "-thread_queue_size",
            "1024",
            "-i",
            str(output_video_path),
            "-seekable",
            "0",
            "-thread_queue_size",
            "1024",
            "-i",
            str(output_audio_path),
            "-c",
            "copy",
            "-movflags",
            "faststart",
            "-fflags",
            "bitexact",
            f"{video_id}.mp4",
        ]
    )
