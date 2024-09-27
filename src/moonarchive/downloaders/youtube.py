#!/usr/bin/python3

import asyncio
import collections
import dataclasses
import datetime
import html.parser
import io
import operator
import pathlib
import shutil
import string
import sys
import urllib.parse
import urllib.request
from contextvars import ContextVar
from http.cookiejar import MozillaCookieJar
from typing import AsyncIterator, Type

import av
import httpx
import msgspec

from ..models import messages as messages
from ..models.ffmpeg import FFMPEGProgress
from ..models.youtube_player import (
    YTPlayerAdaptiveFormats,
    YTPlayerMediaType,
    YTPlayerResponse,
    YTPlayerStreamingData,
)
from ..output import BaseMessageHandler

# table to remove illegal characters on Windows
# we use this to match ytarchive file output behavior
sanitize_table = str.maketrans({c: "_" for c in r'<>:"/\|?*'})


# status queue for downloading tasks; this is available in stream_downloader and frag_iterator
status_queue_ctx: ContextVar[asyncio.Queue] = ContextVar("status_queue")


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


async def extract_player_response(
    url: str, cookie_file: pathlib.Path | None
) -> YTPlayerResponse:
    response_extractor = PlayerResponseExtractor()
    cookies = _cookies_from_filepath(cookie_file)
    status_queue = status_queue_ctx.get()
    async with httpx.AsyncClient(follow_redirects=True, cookies=cookies) as client:
        max_retries = 10
        for n in range(10):
            try:
                r = await client.get(url)
                response_extractor.feed(r.text)
                break
            except httpx.HTTPError:
                status_queue.put_nowait(
                    messages.StringMessage(
                        "Failed to retrieve player response " f"(attempt {n} of {max_retries})"
                    )
                )
                await asyncio.sleep(6)

        if not response_extractor.result:  # type: ignore
            raise ValueError("Could not extract player response")
        return msgspec.json.decode(response_extractor.result, type=YTPlayerResponse)  # type: ignore


async def _get_streaming_data_from_android(video_id: str) -> YTPlayerStreamingData | None:
    # the DASH manifest via web client expires every 30 seconds as of 2024-08-08
    # so now we masquerade as the android client and extract the manifest there
    post_data = string.Template("""{
	'context': {
		'client': {
			'clientName': 'ANDROID',
			'clientVersion': '19.09.37',
			'hl': 'en'
		}
	},
	'videoId': '${videoid}',
	'params': 'CgIQBg==',
	'playbackContext': {
		'contentPlaybackContext': {
			'html5Preference': 'HTML5_PREF_WANTS'
		}
	},
	'contentCheckOk': true,
	'racyCheckOk': true
}
	""")

    status_queue = status_queue_ctx.get()
    async with httpx.AsyncClient() as client:
        headers = {
            "X-YouTube-Client-Name": "3",
            "X-YouTube-Client-Version": "19.09.37",
            "Origin": "https://www.youtube.com",
            "content-type": "application/json",
        }

        max_retries = 10
        for n in range(max_retries):
            try:
                result = await client.post(
                    "https://www.youtube.com/youtubei/v1/player?key=AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8",
                    content=post_data.substitute(videoid=video_id).encode("utf8"),
                    headers=headers,
                )
                result.raise_for_status()
                response = msgspec.json.decode(result.text, type=YTPlayerResponse)  # type: ignore
                if response.streaming_data:
                    return response.streaming_data
            except httpx.HTTPStatusError:
                status_queue.put_nowait(
                    messages.StringMessage(
                        "Failed to retrieve Android streaming data "
                        f"(attempt {n} of {max_retries})"
                    )
                )
            await asyncio.sleep(5)
        return None


def _cookies_from_filepath(cookie_file: pathlib.Path | None) -> httpx.Cookies:
    # since sessions refresh frequently, always grab cookies from file so they're
    # updated out-of-band
    jar = MozillaCookieJar()
    if cookie_file and cookie_file.is_file() and cookie_file.exists():
        jar.load(str(cookie_file))
    return httpx.Cookies(jar)


@dataclasses.dataclass
class FormatSelector:
    """
    Class to select a YouTube substream for downloading.

    This is invoked to determine an appropriate format for the user, as the availability of
    formats may change if the stream broadcasts under a new manifest ID.
    """

    major_type: YTPlayerMediaType
    codec: str | None = None
    max_video_resolution: int | None = None

    def select(self, formats: list[YTPlayerAdaptiveFormats]) -> list[YTPlayerAdaptiveFormats]:
        out_formats = list(filter(lambda x: x.media_type.type == self.major_type, formats))

        sort_key = None
        if self.major_type == YTPlayerMediaType.VIDEO:
            sort_key = operator.attrgetter("width")

            # note that the result of _preferred_codec_sorter is negative since this is reversed
            # at the end of the sort
            if self.codec:

                def _sort(fmt: YTPlayerAdaptiveFormats) -> tuple[int, int]:
                    assert fmt.width is not None
                    return (fmt.width, -FormatSelector._preferred_codec_sorter(self.codec)(fmt))

                # the typing is super clunky and we can't just return comparables, so we simply
                # disregard this for now
                sort_key = _sort  # type: ignore
            if self.max_video_resolution:
                out_formats = list(
                    fmt
                    for fmt in out_formats
                    if (fmt.resolution and fmt.resolution <= self.max_video_resolution)
                )
        else:
            sort_key = operator.attrgetter("bitrate")
        return sorted(out_formats, key=sort_key, reverse=True)

    @staticmethod
    def _preferred_codec_sorter(*codecs: str | None):
        # codecs should be specified with the highest value first
        if not codecs:
            codecs = tuple()

        def _sort(fmt: YTPlayerAdaptiveFormats) -> int:
            try:
                return codecs.index(fmt.media_type.codec_primary)
            except ValueError:
                # return value higher than given tuple
                return len(codecs)

        return _sort


class FragmentInfo(msgspec.Struct, kw_only=True):
    cur_seq: int
    max_seq: int
    itag: int
    manifest_id: str
    buffer: io.BytesIO


class WrittenFragmentInfo(msgspec.Struct):
    cur_seq: int
    length: int


async def frag_iterator(
    resp: YTPlayerResponse,
    selector: FormatSelector,
    cookie_file: pathlib.Path | None,
    num_parallel_downloads: int = 1,
) -> AsyncIterator[FragmentInfo]:
    if not resp.streaming_data or not resp.video_details:
        raise ValueError("Received a non-streamable player response")

    # yields fragment information
    # this should refresh the manifest as needed and yield the next available fragment
    android_streaming_data = await _get_streaming_data_from_android(resp.video_details.video_id)
    if not android_streaming_data:
        raise ValueError("Could not extract unthrottled player response")
    manifest = await android_streaming_data.get_dash_manifest()
    if not manifest:
        raise ValueError("Received a response with no DASH manfiest")

    selected_format, *_ = selector.select(android_streaming_data.adaptive_formats) or (None,)
    if not selected_format:
        raise ValueError(f"Could not meet criteria format for format selector {selector}")
    itag = selected_format.itag

    timeout = selected_format.target_duration_sec
    if not timeout:
        raise ValueError("itag does not have a target duration set")

    video_id = resp.video_details.video_id
    video_url = f"https://youtu.be/{resp.video_details.video_id}"
    cur_seq = 0
    max_seq = 0

    current_manifest_id = android_streaming_data.dash_manifest_id
    assert current_manifest_id

    status_queue = status_queue_ctx.get()

    if selector.major_type == YTPlayerMediaType.VIDEO and selected_format.quality_label:
        status_queue.put_nowait(
            messages.StreamVideoFormatMessage(
                selected_format.quality_label, selected_format.media_type.codec
            )
        )
    status_queue.put_nowait(messages.StringMessage(f"{itag=} {timeout=}"))

    client = httpx.AsyncClient(follow_redirects=True)

    while True:
        url = manifest.format_urls[itag]

        try:
            # allow for batching requests of fragments if we're behind
            # if we're caught up this only requests one fragment
            # if any request fails, the rest of the tasks will be wasted
            batch_count = max(1, min(max_seq - cur_seq, num_parallel_downloads))
            reqs = [
                # it doesn't look like we need cookies for fetching fragments
                (
                    s,
                    asyncio.create_task(
                        client.get(url.substitute(sequence=s), timeout=timeout * 2)
                    ),
                )
                for s in range(cur_seq, cur_seq + batch_count)
            ]
            for req_seq, req_task in reqs:
                fresp = await req_task
                fresp.raise_for_status()

                new_max_seq = int(fresp.headers.get("X-Head-Seqnum", -1))

                # copying to a buffer here ensures this function handles errors related to the request,
                # as opposed to passing the request errors to the downloading task
                buffer = io.BytesIO(fresp.content)

                if buffer.getbuffer().nbytes == 0:
                    # for some reason it's possible for us to get an empty result
                    # retry until we get a non-zero length or an error code (possible end of stream)
                    # there should be no way for us to get a success code and non-zero forever

                    # it looks like we may also hit this case if the live replay is unlisted at the end
                    status_queue.put_nowait(
                        messages.StringMessage(
                            f"Empty {selector.major_type} fragment at {cur_seq}; retrying"
                        )
                    )
                    await asyncio.sleep(min(timeout * 5, 20))
                    continue

                assert req_seq == cur_seq

                info = FragmentInfo(
                    cur_seq=cur_seq,
                    max_seq=new_max_seq,
                    itag=itag,
                    manifest_id=current_manifest_id,
                    buffer=buffer,
                )
                yield info
                max_seq = new_max_seq
                cur_seq += 1
            # no download issues, so move on to the next iteration
            continue
        except httpx.HTTPStatusError as exc:
            # this desperately needs refactoring...
            status_queue.put_nowait(
                messages.ExtractingPlayerResponseMessage(itag, exc.response.status_code)
            )

            # we need this call to be resilient to failures, otherwise we may have an incomplete download
            try_resp = await extract_player_response(video_url, cookie_file)
            if not try_resp:
                continue
            resp = try_resp

            if exc.response.status_code == 403:
                # stream access expired? retrieve a fresh manifest
                # the stream may have finished while we were mid-download, so don't check that here
                # FIXME: we need to check playability_status instead of bailing
                if not resp.streaming_data:
                    return
                android_streaming_data = await _get_streaming_data_from_android(video_id)
                if not android_streaming_data:
                    return
                manifest = await android_streaming_data.get_dash_manifest()
                if not manifest:
                    return
            elif exc.response.status_code in (404, 500, 503):
                if not resp.microformat or not resp.microformat.live_broadcast_details:
                    # video is private?
                    status_queue.put_nowait(
                        messages.StringMessage(
                            "Microformat or live broadcast details unavailable "
                            f"for type {selector.major_type}"
                        )
                    )
                    return

                # the server has indicated that no fragment is present
                # we're done if the stream is no longer live and a duration is rendered
                if not resp.microformat.live_broadcast_details.is_live_now and (
                    not resp.video_details or resp.video_details.num_length_seconds
                ):
                    # we need to handle this differenly during post-live
                    # the video server may return 503
                    if exc.response.status_code != 503:
                        return
                    elif cur_seq < max_seq - 2:
                        # post-live, X-Head-Seqnum tends to be two above the true number
                        pass

                if not resp.streaming_data:
                    # stream is offline; sleep and retry previous fragment
                    if resp.playability_status.status == "LIVE_STREAM_OFFLINE":
                        # this code path is hit if the streamer is disconnected; retry for frag
                        pass
                    if resp.playability_status.status == "UNPLAYABLE":
                        # this code path may be hit if the streamer opts to unlist the live
                        # replay once the stream ends:
                        # resp.playability_status.reason == "This live stream recording is not available."

                        # check resp.microformat.live_broadcast_details.{end_timestamp,is_live_now}
                        # end_timestamp is probably the best bet here

                        # this code path should also be hit if cookies are expired, in which case we are not logged in
                        pass
                    status_queue.put_nowait(
                        messages.StringMessage(
                            "No streaming data; status "
                            f"{resp.playability_status.status}; "
                            f"live now: {resp.microformat.live_broadcast_details.is_live_now}"
                        )
                    )

                    await asyncio.sleep(15)
                    continue

                if not resp.streaming_data.dash_manifest_id:
                    return

                android_streaming_data = await _get_streaming_data_from_android(video_id)
                if not android_streaming_data:
                    status_queue.put_nowait(
                        messages.StringMessage(
                            f"No android streaming data for type {selector.major_type}"
                        )
                    )
                    return
                manifest = await android_streaming_data.get_dash_manifest()
                if not manifest:
                    status_queue.put_nowait(
                        messages.StringMessage(
                            f"No android DASH manifest for type {selector.major_type}"
                        )
                    )
                    return
            elif exc.response.status_code == 401:
                await asyncio.sleep(10)
                continue
        except (httpx.HTTPError, httpx.StreamError):
            # for everything else we just retry
            continue

        assert android_streaming_data.dash_manifest_id
        if current_manifest_id != android_streaming_data.dash_manifest_id:
            # player response has a different manfifest ID than what we're aware of
            # reset the sequence counter
            cur_seq = 0
            max_seq = 0
            current_manifest_id = android_streaming_data.dash_manifest_id

            # update format to the best available
            # manifest.format_urls raises a key error
            preferred_format, *_ = selector.select(android_streaming_data.adaptive_formats)
            itag = preferred_format.itag

            status_queue.put_nowait(messages.StringMessage(f"{itag=} {timeout=}"))


@dataclasses.dataclass
class StatusManager:
    queue: asyncio.Queue

    # bind the lifetime of the manager to the task creating it
    parent_task: asyncio.Task

    def __init__(self):
        self.queue = asyncio.Queue()
        self.parent_task = asyncio.current_task()


async def status_handler(
    handlers: list[BaseMessageHandler],
    status: StatusManager,
) -> None:
    while not status.parent_task.done() or not status.queue.empty():
        try:
            message = await asyncio.wait_for(status.queue.get(), timeout=1.0)
            for handler in handlers:
                await handler.handle_message(message)
        except TimeoutError:
            pass


async def stream_downloader(
    resp: YTPlayerResponse,
    selector: FormatSelector,
    output_directory: pathlib.Path,
    cookie_file: pathlib.Path | None,
    num_parallel_downloads: int = 1,
) -> dict[str, set[pathlib.Path]]:
    # record the manifests and formats we're downloading
    # this is used later to determine which files to mux together
    manifest_outputs = collections.defaultdict(set)

    last_manifest_id = None
    last_frag_dimensions = (0, 0)
    outnum = 0

    status_queue = status_queue_ctx.get()

    async for frag in frag_iterator(resp, selector, cookie_file):
        output_prefix = f"{frag.manifest_id}.f{frag.itag}"

        if frag.manifest_id != last_manifest_id:
            last_manifest_id = frag.manifest_id
            last_frag_dimensions = (0, 0)
            outnum = 0
        if selector.major_type == YTPlayerMediaType.VIDEO:
            # analyze fragment for dimension change mid-manifest
            with av.open(frag.buffer, "r") as container:
                vf = next(container.decode(video=0))
                assert type(vf) == av.VideoFrame

                current_frag_dimensions = vf.width, vf.height
                if last_frag_dimensions != current_frag_dimensions:
                    if last_frag_dimensions != (0, 0):
                        outnum += 1
                        status_queue.put_nowait(
                            messages.StringMessage(
                                f"Resolution change {last_frag_dimensions} to {current_frag_dimensions}"
                            )
                        )
                    last_frag_dimensions = current_frag_dimensions
            output_prefix = f"{frag.manifest_id}#{outnum}.f{frag.itag}"

        output_stream_path = output_directory / f"{output_prefix}.ts"

        # we dump our fragment lengths in case we need to extract the raw segments
        with output_stream_path.with_suffix(".fragdata.txt").open("ab") as fragdata:
            payload = WrittenFragmentInfo(
                cur_seq=frag.cur_seq,
                length=frag.buffer.getbuffer().nbytes,
            )
            fragdata.write(msgspec.json.encode(payload) + b"\n")

        manifest_outputs[frag.manifest_id].add(output_stream_path)

        frag.buffer.seek(0)
        with output_stream_path.open("ab") as o:
            await asyncio.to_thread(shutil.copyfileobj, frag.buffer, o)

        status_queue.put_nowait(
            messages.FragmentMessage(
                frag.cur_seq,
                frag.max_seq,
                selector.major_type,
                frag.itag,
                frag.manifest_id,
                frag.buffer.getbuffer().nbytes,
            )
        )
    status_queue.put_nowait(messages.DownloadStreamJobEndedMessage(selector.major_type))
    return manifest_outputs


async def _run(args: "YouTubeDownloader") -> None:
    # prevent usage if we're running on an event loop that doesn't support the features we need
    if sys.platform == "win32" and isinstance(
        asyncio.get_event_loop(), asyncio.SelectorEventLoop
    ):
        raise RuntimeError(
            "Cannot use downloader with SelectorEventLoop as the "
            "running event loop on Windows as it does not support subprocesses"
        )

    # set up output handler
    status = StatusManager()
    status_queue_ctx.set(status.queue)

    # hold a reference to the output handler so it doesn't get GC'd until we're out of scope
    jobs = {asyncio.create_task(status_handler(args.handlers, status))}  # noqa: F841

    resp = await extract_player_response(args.url, args.cookie_file)

    if resp.playability_status.status in ("ERROR", "LOGIN_REQUIRED", "UNPLAYABLE"):
        status.queue.put_nowait(
            messages.StreamUnavailableMessage(
                resp.playability_status.status, resp.playability_status.reason
            )
        )
        return

    assert resp.video_details
    assert resp.microformat
    status.queue.put_nowait(
        messages.StreamInfoMessage(
            resp.video_details.author,
            resp.video_details.title,
            resp.microformat.live_broadcast_details.start_datetime,
        )
    )

    if args.dry_run:
        return

    while not resp.streaming_data:
        if not resp.microformat or resp.playability_status.status in ("LOGIN_REQUIRED",):
            # waiting room appears to be unavailable; either recheck or stop
            status.queue.put_nowait(
                messages.StreamUnavailableMessage(
                    resp.playability_status.status, resp.playability_status.reason
                )
            )

            if not args.poll_unavailable_interval:
                return
            await asyncio.sleep(args.poll_unavailable_interval)
            resp = await extract_player_response(args.url, args.cookie_file)
            continue

        # live_broadcast_details may not be available; LOGIN_REQUIRED should catch this
        timestamp = resp.microformat.live_broadcast_details.start_datetime

        # post-scheduled recheck interval
        seconds_wait = 20.0
        if timestamp:
            now = datetime.datetime.now(datetime.timezone.utc)

            seconds_remaining = (timestamp - now).total_seconds() - args.schedule_offset
            status.queue.put_nowait(
                messages.StringMessage(
                    f"No stream available (scheduled to start in {int(seconds_remaining)}s at {timestamp})"
                )
            )

            if seconds_remaining > 0:
                seconds_wait = seconds_remaining
                if args.poll_interval > 0 and seconds_wait > args.poll_interval:
                    seconds_wait = args.poll_interval
        else:
            status.queue.put_nowait(messages.StringMessage("No stream available, polling"))

        await asyncio.sleep(seconds_wait)

        resp = await extract_player_response(args.url, args.cookie_file)

    if args.list_formats:
        for format in resp.streaming_data.adaptive_formats:
            format_disp = format
            format_disp.url = None
            status.queue.put_nowait(messages.StringMessage(str(format_disp)))
        return

    assert resp.video_details
    video_id = resp.video_details.video_id

    workdir = args.staging_directory or pathlib.Path(".")
    workdir.mkdir(parents=True, exist_ok=True)

    outdir = args.output_directory or pathlib.Path()

    output_basename = resp.video_details.title.translate(sanitize_table)

    # output_paths[dest] = src
    output_paths = {}

    if args.write_description:
        desc_path = workdir / f"{video_id}.description"
        desc_path.write_text(
            f"https://www.youtube.com/watch?v={video_id}\n\n{resp.video_details.short_description}",
            encoding="utf8",
            newline="\n",
        )
        output_paths[outdir / f"{output_basename}-{video_id}{desc_path.suffix}"] = desc_path

    if args.write_thumbnail:
        if resp.microformat and resp.microformat.thumbnails:
            thumbnail_url = resp.microformat.thumbnails[0].url
            thumbnail_url_path = pathlib.Path(
                urllib.request.url2pathname(urllib.parse.urlparse(thumbnail_url).path)
            )

            thumb_dest_path = (workdir / video_id).with_suffix(thumbnail_url_path.suffix)
            r = httpx.get(thumbnail_url)
            thumb_dest_path.write_bytes(r.content)
            output_paths[outdir / f"{output_basename}-{video_id}{thumb_dest_path.suffix}"] = (
                thumb_dest_path
            )

    manifest_outputs: dict[str, set[pathlib.Path]] = collections.defaultdict(set)
    async with asyncio.TaskGroup() as tg:
        vidsel = FormatSelector(
            YTPlayerMediaType.VIDEO, max_video_resolution=args.max_video_resolution
        )
        if args.prioritize_vp9:
            vidsel.codec = "vp9"

        # tasks to write streams to file
        video_stream_dl = tg.create_task(
            stream_downloader(
                resp,
                vidsel,
                workdir,
                args.cookie_file,
                args.num_parallel_downloads,
            )
        )
        audio_stream_dl = tg.create_task(
            stream_downloader(
                resp,
                FormatSelector(YTPlayerMediaType.AUDIO),
                workdir,
                args.cookie_file,
                args.num_parallel_downloads,
            )
        )
        for task in asyncio.as_completed((video_stream_dl, audio_stream_dl)):
            try:
                result = await task
                for manifest_id, output_prefixes in result.items():
                    manifest_outputs[manifest_id] |= output_prefixes
            except Exception as exc:
                print(type(exc), exc)

    status.queue.put_nowait(messages.StreamMuxMessage(list(manifest_outputs)))
    status.queue.put_nowait(messages.StringMessage(str(manifest_outputs)))

    # output a file for each manifest we received fragments for
    for manifest_id, output_stream_paths in manifest_outputs.items():
        if len(output_stream_paths) != 2:
            # for YouTube, we expect one audio / video stream pair per manifest
            # we may have multiple video streams per manifest if the resolution changes
            # we can also handle audio-only flags here
            output_path_names = {p.name for p in output_stream_paths}
            status.queue.put_nowait(
                messages.StringMessage(
                    f"Manifest {manifest_id} produced outputs {output_path_names} (expected 2)"
                )
            )
            status.queue.put_nowait(
                messages.StringMessage("This will need to be manually processed")
            )
            # TODO: we need to dispatch a message on unsuccessful remuxes so other tools can
            # handle this case
            continue

        # raising the log level to 'fatal' instead of 'warning' suppresses MOOV atom warnings
        # and unknown webm:vp9 element errors
        # those warnings being dumped to stdout has a non-negligible performance impact
        program = str(args.ffmpeg_path) if args.ffmpeg_path else "ffmpeg"
        command = [
            "-v",
            "fatal",
            "-stats",
            "-progress",
            "-",
            "-nostdin",
            "-y",
        ]

        for output_stream_path in output_stream_paths:
            command += (
                "-seekable",
                "0",
                "-thread_queue_size",
                "1024",
                "-i",
                str(output_stream_path.absolute()),
            )

        command += (
            "-c",
            "copy",
            "-movflags",
            "faststart",
            "-fflags",
            "bitexact",
        )

        # we write this to workdir since ffmpeg will need to do a second pass to move the moov
        # atom - it's assumed that the output directory will be slower than the workdir
        output_mux_file = workdir / f"{manifest_id}.mp4"
        command += (str(output_mux_file.absolute()),)

        proc = await asyncio.create_subprocess_exec(
            program,
            *command,
            stdout=asyncio.subprocess.PIPE,
        )

        async for progress in FFMPEGProgress.from_process_stream(proc.stdout):
            # ffmpeg progress in remux provides bitrate, total size, out_time, speed
            status.queue.put_nowait(
                messages.StreamMuxProgressMessage(
                    manifest_id=manifest_id,
                    progress=progress,
                )
            )

        await proc.wait()

        mux_output_name = f"{output_basename}-{manifest_id}.mp4"
        if len(manifest_outputs) == 1:
            # unique manifest, so output video ID instead (matching ytarchive behavior)
            mux_output_name = f"{output_basename}-{video_id}.mp4"

        output_paths[outdir / mux_output_name] = output_mux_file

    # if we only have one manifest with an unexpected output count, the logs will never be
    # rendered in the CLI - yield to other tasks here just in case
    await asyncio.sleep(0)

    try:
        # bail if we fail to make the directory
        outdir.mkdir(parents=True, exist_ok=True)

        # move files to their final location
        # file paths may be too long on some filesystems like zfs, so we process the longest
        # first then bail if it throws
        for dest in sorted(output_paths, key=lambda p: len(str(p.resolve())), reverse=True):
            src = output_paths[dest]
            await asyncio.to_thread(shutil.move, src, dest)
        status.queue.put_nowait(messages.DownloadJobFinishedMessage(list(output_paths.keys())))
    except OSError:
        status.queue.put_nowait(messages.DownloadJobFailedOutputMoveMessage(output_paths))
    jobs.clear()


class YouTubeDownloader(msgspec.Struct, kw_only=True):
    url: str
    write_description: bool
    write_thumbnail: bool
    prioritize_vp9: bool
    max_video_resolution: int | None = None
    staging_directory: pathlib.Path | None
    output_directory: pathlib.Path | None
    poll_interval: int = 0
    poll_unavailable_interval: int = 0
    schedule_offset: int = 0
    dry_run: bool = False
    list_formats: bool = False
    ffmpeg_path: pathlib.Path | None = None
    cookie_file: pathlib.Path | None = None
    num_parallel_downloads: int = 1
    handlers: list[BaseMessageHandler] = msgspec.field(default_factory=list)

    async def async_run(self) -> None:
        await _run(self)

    def run(self) -> None:
        asyncio.run(_run(self))
