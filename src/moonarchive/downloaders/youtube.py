#!/usr/bin/python3

import collections
import concurrent.futures
import dataclasses
import datetime
import html.parser
import io
import json
import multiprocessing as mp
import operator
import pathlib
import queue
import shutil
import subprocess
import time
import urllib.parse
import urllib.request
from multiprocessing.synchronize import Event as SyncEvent
from typing import Iterator, Type

import httpx
import msgspec

from ..models import messages as messages
from ..models.youtube_player import YTPlayerAdaptiveFormats, YTPlayerMediaType, YTPlayerResponse
from ..output import BaseMessageHandler, YTArchiveMessageHandler

# table to remove illegal characters on Windows
# we currently don't use this, but we may optionally match ytarchive file output behavior later
sanitize_table = str.maketrans({c: "_" for c in r'<>:"/\|?*'})


def create_json_object_extractor(decl: str) -> Type[html.parser.HTMLParser]:
    class InternalHTMLParser(html.parser.HTMLParser):
        in_script: bool = False
        result = None

        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            self.in_script = tag == "script"

        def handle_endtag(self, tag: str) -> None:
            self.in_script = False

        def handle_data(self, data: str) -> None:
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


def extract_player_response(url: str) -> YTPlayerResponse:
    response_extractor = PlayerResponseExtractor()

    transport = httpx.HTTPTransport(retries=10)
    with httpx.Client(transport=transport, follow_redirects=True) as client:
        r = client.get(url)
        response_extractor.feed(r.text)

        if not response_extractor.result:  # type: ignore
            raise ValueError("Could not extract player response")
        return msgspec.json.decode(response_extractor.result, type=YTPlayerResponse)  # type: ignore


@dataclasses.dataclass
class FormatSelector:
    """
    Class to select a YouTube substream for downloading.
    """

    major_type: YTPlayerMediaType
    codec: str | None = None

    def select(self, formats: list[YTPlayerAdaptiveFormats]) -> list[YTPlayerAdaptiveFormats]:
        out_formats = filter(lambda x: x.media_type.type == self.major_type, formats)
        if self.codec is not None and any(self.codec == x.media_type.codec for x in formats):
            out_formats = filter(lambda x: self.codec == x.media_type.codec, out_formats)

        # TODO: allow for prioritizing VP9 over AVC1 at the same resolution
        sort_key = None
        if self.major_type == YTPlayerMediaType.VIDEO:
            sort_key = operator.attrgetter("width")
        else:
            sort_key = operator.attrgetter("bitrate")
        return sorted(out_formats, key=sort_key, reverse=True)


class FragmentInfo(msgspec.Struct, kw_only=True):
    cur_seq: int
    max_seq: int
    itag: int
    manifest_id: str
    buffer: io.BytesIO


def frag_iterator(
    resp: YTPlayerResponse, selector: FormatSelector, status_queue: queue.Queue
) -> Iterator[FragmentInfo]:
    if not resp.streaming_data or not resp.video_details:
        raise ValueError("Received a non-streamable player response")

    # yields fragment information
    # this should refresh the manifest as needed and yield the next available fragment
    manifest = resp.streaming_data.get_dash_manifest()
    if not manifest:
        raise ValueError("Received a response with no DASH manfiest")

    selected_format, *_ = selector.select(resp.streaming_data.adaptive_formats)
    itag = selected_format.itag

    timeout = selected_format.target_duration_sec
    if not timeout:
        raise ValueError("itag does not have a target duration set")

    video_url = f"https://youtu.be/{resp.video_details.video_id}"
    cur_seq = 0

    current_manifest_id = resp.streaming_data.dash_manifest_id
    assert current_manifest_id

    if selector.major_type == YTPlayerMediaType.VIDEO and selected_format.quality_label:
        status_queue.put(messages.StreamVideoFormatMessage(selected_format.quality_label))
    status_queue.put(messages.StringMessage(f"{itag=} {timeout=}"))

    client = httpx.Client(follow_redirects=True)

    while True:
        url = manifest.format_urls[itag]

        try:
            fresp = client.get(url.substitute(sequence=cur_seq), timeout=timeout * 2)
            fresp.raise_for_status()

            new_max_seq = int(fresp.headers.get("X-Head-Seqnum", -1))

            # copying to a buffer here ensures this function handles errors related to the request,
            # as opposed to passing the request errors to the downloading task
            buffer = io.BytesIO(fresp.content)

            info = FragmentInfo(
                cur_seq=cur_seq,
                max_seq=new_max_seq,
                itag=itag,
                manifest_id=current_manifest_id,
                buffer=buffer,
            )
            yield info
            cur_seq += 1
        except httpx.HTTPStatusError as exc:
            status_queue.put(
                messages.ExtractingPlayerResponseMessage(itag, exc.response.status_code)
            )

            # we need this call to be resilient to failures, otherwise we may have an incomplete download
            try_resp = extract_player_response(video_url)
            if not try_resp:
                continue
            resp = try_resp

            if exc.response.status_code == 403:
                # retrieve a fresh manifest
                if not resp.streaming_data:
                    return
                manifest = resp.streaming_data.get_dash_manifest()
                if not manifest:
                    return
            elif exc.response.status_code in (404, 500, 503):
                if not resp.microformat or not resp.microformat.live_broadcast_details:
                    # video is private?
                    return

                # the server has indicated that no fragment is present
                # we're done if the stream is no longer live and a duration is rendered
                if not resp.microformat.live_broadcast_details.is_live_now and (
                    not resp.video_details or resp.video_details.num_length_seconds
                ):
                    return

                if not resp.streaming_data:
                    # stream is offline; sleep and retry previous fragment
                    time.sleep(15)
                    continue

                if not resp.streaming_data.dash_manifest_id:
                    return

                manifest = resp.streaming_data.get_dash_manifest()
                if not manifest:
                    return

                if current_manifest_id != resp.streaming_data.dash_manifest_id:
                    # player response has a different manfifest ID than what we're aware of
                    # reset the sequence counters
                    cur_seq = 0
                    current_manifest_id = resp.streaming_data.dash_manifest_id

                    # update format to the best available
                    # manifest.format_urls raises a key error
                    preferred_format, *_ = selector.select(resp.streaming_data.adaptive_formats)
                    itag = preferred_format.itag

                    status_queue.put(messages.StringMessage(f"{itag=} {timeout=}"))
        except (httpx.HTTPError, httpx.StreamError):
            # for everything else we just retry
            continue


@dataclasses.dataclass
class StatusManager:
    queue: queue.Queue
    finished: SyncEvent

    def __init__(self):
        status_manager = mp.Manager()
        self.queue = status_manager.Queue()
        self.finished = status_manager.Event()


def status_handler(
    handler: BaseMessageHandler,
    status: StatusManager,
) -> None:
    while not status.finished.is_set() or not status.queue.empty():
        try:
            message = status.queue.get(timeout=1.0)
            handler.handle_message(message)
        except queue.Empty:
            pass


def stream_downloader(
    resp: YTPlayerResponse,
    selector: FormatSelector,
    output_directory: pathlib.Path,
    status_queue: queue.Queue,
) -> dict[str, set[pathlib.Path]]:
    # record the manifests and formats we're downloading
    # this is used later to determine which files to mux together
    manifest_outputs = collections.defaultdict(set)

    last_itag = 0
    for frag in frag_iterator(resp, selector, status_queue):
        output_prefix = f"{frag.manifest_id}.f{frag.itag}"
        output_stream_path = output_directory / f"{output_prefix}.ts"

        # we dump our fragment lengths in case we need to extract the raw segments
        #
        # this should help in situations where the stream is rotated between portrait and
        # landscape; that maintains the same manifest, but ffmpeg ends up applying the initial
        # dimensions for the entire video - that causes a broken image for the rest of the stream
        with output_stream_path.with_suffix(".fragdata.txt").open(
            "at", encoding="utf8"
        ) as fragdata:
            payload = {
                "cur_seq": frag.cur_seq,
                "length": frag.buffer.getbuffer().nbytes,
            }
            fragdata.write(json.dumps(payload) + "\n")

        manifest_outputs[frag.manifest_id].add(output_stream_path)

        frag.buffer.seek(0)
        with output_stream_path.open("ab") as o:
            shutil.copyfileobj(frag.buffer, o)

        status_queue.put(
            messages.FragmentMessage(
                frag.cur_seq,
                frag.max_seq,
                frag.itag,
                frag.manifest_id,
                frag.buffer.getbuffer().nbytes,
            )
        )
        last_itag = frag.itag
    status_queue.put(messages.DownloadJobEndedMessage(last_itag))
    return manifest_outputs


def _run(args: "YouTubeDownloader") -> None:
    # set up output handler
    handler = YTArchiveMessageHandler()
    status = StatusManager()

    status_proc = mp.Process(target=status_handler, args=(handler, status))
    status_proc.start()

    resp = extract_player_response(args.url)

    if resp.playability_status.status in ("ERROR", "LOGIN_REQUIRED"):
        print(f"{resp.playability_status.status}: {resp.playability_status.reason}")
        status.finished.set()
        status_proc.join()
        return

    assert resp.video_details
    assert resp.microformat
    status.queue.put(
        messages.StreamInfoMessage(
            resp.video_details.author,
            resp.video_details.title,
            resp.microformat.live_broadcast_details.start_datetime,
        )
    )

    if args.dry_run:
        status.finished.set()
        status_proc.join()
        return

    while not resp.streaming_data:
        timestamp = resp.microformat.live_broadcast_details.start_datetime

        # post-scheduled recheck interval
        seconds_wait = 20.0
        if timestamp:
            now = datetime.datetime.now(datetime.timezone.utc)

            seconds_remaining = (timestamp - now).total_seconds()
            status.queue.put(
                messages.StringMessage(
                    f"No stream available (scheduled to start in {int(seconds_remaining)}s at {timestamp})"
                )
            )

            if seconds_remaining > 0:
                seconds_wait = seconds_remaining
                if args.poll_interval > 0 and seconds_wait > args.poll_interval:
                    seconds_wait = args.poll_interval
        else:
            status.queue.put(messages.StringMessage("No stream available, polling"))

        time.sleep(seconds_wait)

        resp = extract_player_response(args.url)

    assert resp.video_details
    video_id = resp.video_details.video_id

    if args.write_description:
        desc_path = pathlib.Path(f"{video_id}.description")
        desc_path.write_text(
            f"https://www.youtube.com/watch?v={video_id}\n\n{resp.video_details.short_description}",
            encoding="utf8",
            newline="\n",
        )

    workdir = pathlib.Path(".")
    outdir = workdir

    if args.write_thumbnail:
        if resp.microformat and resp.microformat.thumbnails:
            thumbnail_url = resp.microformat.thumbnails[0].url
            thumbnail_url_path = pathlib.Path(
                urllib.request.url2pathname(urllib.parse.urlparse(thumbnail_url).path)
            )

            thumb_dest_path = (workdir / video_id).with_suffix(thumbnail_url_path.suffix)
            r = httpx.get(thumbnail_url)
            thumb_dest_path.write_bytes(r.content)

    manifest_outputs: dict[str, set[pathlib.Path]] = collections.defaultdict(set)
    with concurrent.futures.ProcessPoolExecutor() as executor:
        # tasks to write streams to file
        video_stream_dl = executor.submit(
            stream_downloader,
            resp,
            FormatSelector(YTPlayerMediaType.VIDEO),
            workdir,
            status.queue,
        )
        audio_stream_dl = executor.submit(
            stream_downloader,
            resp,
            FormatSelector(YTPlayerMediaType.AUDIO),
            workdir,
            status.queue,
        )
        for future in concurrent.futures.as_completed((video_stream_dl, audio_stream_dl)):
            exc = future.exception()
            if exc:
                print(type(exc), exc)
            else:
                for manifest_id, output_prefixes in future.result().items():
                    manifest_outputs[manifest_id] |= output_prefixes

        status.finished.set()
    status_proc.join()

    print()
    print(manifest_outputs)

    # output a file for each manifest we received fragments for
    for manifest_id, output_stream_paths in manifest_outputs.items():
        if len(output_stream_paths) != 2:
            # for YouTube, we expect one audio / video stream pair per manifest
            output_path_names = {p.name for p in output_stream_paths}
            print(f"Manifest {manifest_id} produced outputs {output_path_names} (expected 2)")
            print("This will need to be manually processed")
            continue

        # raising the log level to 'error' instead of 'warning' suppresses MOOV atom warnings
        # those warnings being dumped to stdout has a non-negligible performance impact
        command = [
            "ffmpeg",
            "-v",
            "error",
            "-stats",
            "-y",
        ]

        for output_stream_path in output_stream_paths:
            command.extend(
                (
                    "-seekable",
                    "0",
                    "-thread_queue_size",
                    "1024",
                    "-i",
                    str(output_stream_path.absolute()),
                )
            )

        command.extend(
            (
                "-c",
                "copy",
                "-movflags",
                "faststart",
                "-fflags",
                "bitexact",
            )
        )

        output_mux_file = outdir / f"{manifest_id}.mp4"
        command.extend((str(output_mux_file.absolute()),))

        subprocess.run(command)


class YouTubeDownloader(msgspec.Struct):
    url: str
    poll_interval: int
    dry_run: bool
    write_description: bool
    write_thumbnail: bool

    def run(self) -> None:
        _run(self)
