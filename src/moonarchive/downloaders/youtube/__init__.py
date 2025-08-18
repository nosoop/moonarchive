#!/usr/bin/python3

import asyncio
import collections
import datetime
import io
import itertools
import json
import pathlib
import shutil
import sys
import urllib.parse
import urllib.request
from typing import Iterable, TypeVar

import av
import httpx
import msgspec

from ...models import messages as messages
from ...models.ffmpeg import FFMPEGProgress
from ...output import BaseMessageHandler
from ...util.paths import _string_byte_trim, sanitize_table
from ._dash import frag_iterator, num_parallel_downloads_ctx
from ._format import FormatSelector
from ._innertube import _build_auth_from_cookies as _build_auth_from_cookies
from ._innertube import (
    _get_live_stream_status,
    _get_web_player_response,
    _set_browser_ctx_by_name,
    cookie_file_ctx,
    extract_player_response,
    extract_yt_cfg,
    po_token_ctx,
    visitor_data_ctx,
    ytcfg_ctx,
)
from ._status import StatusManager, status_handler, status_queue_ctx
from .player import (
    YTPlayerHeartbeatResponse,
    YTPlayerMediaType,
    YTPlayerResponse,
)


class WrittenFragmentInfo(msgspec.Struct):
    cur_seq: int
    length: int


class ResumeState(msgspec.Struct):
    start_seq: int = 0
    outnum: int = 0
    last_frag_dimensions: tuple[int, int] = (0, 0)


T = TypeVar("T")


def _decode_possibly_malformed_fragdata(s: str) -> Iterable[WrittenFragmentInfo]:
    """
    Decodes a potentially-malformed fragment list file.  The fragment list file may be malformed
    if the system dies while in the process of appending to the file.
    """
    jdec = json.JSONDecoder()
    try:
        yield from (
            msgspec.convert(jdec.decode(line), WrittenFragmentInfo) for line in s.splitlines()
        )
    except json.decoder.JSONDecodeError:
        pass


async def _check_resume_state(
    output_directory: pathlib.Path, manifest_id: str, media_type: YTPlayerMediaType
) -> ResumeState:
    """
    Attempts to resume an existing download.
    This function also restores the raw stream and fragment information to a known good state.
    """

    # get the last stream that was touched, in the event that we have multiple files for a given
    # manifest ID
    streams = sorted(
        output_directory.glob(
            f"{manifest_id}#*.f*.ts"
            if media_type == YTPlayerMediaType.VIDEO
            else f"{manifest_id}.f*.ts"
        ),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not streams:
        return ResumeState()

    raw_stream, *_ = streams

    fragdata_f = raw_stream.with_suffix(".fragdata.txt")
    if not fragdata_f:
        return ResumeState()

    fraglist = list(_decode_possibly_malformed_fragdata(fragdata_f.read_text()))
    if not fraglist:
        return ResumeState()

    file_size = raw_stream.stat().st_size
    total_frag_size = sum(frag.length for frag in fraglist)

    if file_size > total_frag_size:
        # we always write to the raw stream file before appending to the fragment data file,
        # so we expect that the computed total fragment size is always less than or equal to the
        # raw stream size; as such we can truncate
        with raw_stream.open("ab") as rs:
            rs.truncate(total_frag_size)
    elif file_size < total_frag_size:
        # this case would be hit if the raw stream was truncated somehow
        # this is unlikely to happen without the stream being modified externally
        raise RuntimeError("Raw stream is smaller than recorded written fragment total")

    with fragdata_f.open("wb") as fragdata:
        # fix up fragment data in case the file is malformed
        for payload in fraglist:
            fragdata.write(msgspec.json.encode(payload) + b"\n")

    last_fragment = fraglist[-1]
    resume_seq = last_fragment.cur_seq + 1

    if media_type != YTPlayerMediaType.VIDEO:
        return ResumeState(resume_seq)

    # for videos we need additional information
    # TODO: maybe write this information during the download?
    last_frag_dimensions = (0, 0)
    with raw_stream.open("rb") as rs:
        rs.seek(total_frag_size - last_fragment.length)
        last_frag_buffer = io.BytesIO(rs.read())
    with av.open(last_frag_buffer, "r") as container:
        vf = next(await asyncio.to_thread(container.decode, video=0))
        assert type(vf) == av.VideoFrame
        last_frag_dimensions = vf.width, vf.height

    # given N output streams, the last outnum should be (N - 1)
    return ResumeState(resume_seq, len(streams) - 1, last_frag_dimensions)


async def stream_downloader(
    resp: YTPlayerResponse, selector: FormatSelector, output_directory: pathlib.Path
) -> dict[str, set[pathlib.Path]]:
    # record the manifests and formats we're downloading
    # this is used later to determine which files to mux together
    manifest_outputs = collections.defaultdict(set)

    if not resp.streaming_data:
        raise RuntimeError("Missing streaming data in player response")
    if not resp.streaming_data.dash_manifest_id:
        raise RuntimeError("Missing manifest ID in player response")

    last_manifest_id = resp.streaming_data.dash_manifest_id

    resume = await _check_resume_state(output_directory, last_manifest_id, selector.major_type)

    last_frag_dimensions = resume.last_frag_dimensions
    outnum = resume.outnum

    status_queue = status_queue_ctx.get()

    if resume.start_seq:
        status_queue.put_nowait(
            messages.StringMessage(
                f"Resuming existing {selector.major_type} download for {last_manifest_id} "
                f"from sequence {resume.start_seq}"
            )
        )

    async for frag in frag_iterator(resp, selector, resume.start_seq):
        output_prefix = f"{frag.manifest_id}.f{frag.itag}"

        if frag.manifest_id != last_manifest_id:
            last_manifest_id = frag.manifest_id
            last_frag_dimensions = (0, 0)
            outnum = 0
        if selector.major_type == YTPlayerMediaType.VIDEO:
            # analyze fragment for dimension change mid-broadcast
            with av.open(frag.buffer, "r") as container:
                vf = next(await asyncio.to_thread(container.decode, video=0))
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
    for broadcast_id in manifest_outputs.keys():
        status_queue.put_nowait(
            messages.DownloadStreamJobEndedMessage(selector.major_type, broadcast_id)
        )
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

    num_parallel_downloads_ctx.set(args.num_parallel_downloads)
    po_token_ctx.set(args.po_token)
    visitor_data_ctx.set(args.visitor_data)
    cookie_file_ctx.set(args.cookie_file)
    if args.cookies_from_browser:
        _set_browser_ctx_by_name(args.cookies_from_browser)
    ytcfg_ctx.set(await extract_yt_cfg(args.url))

    # hold a reference to the output handler so it doesn't get GC'd until we're out of scope
    jobs = {asyncio.create_task(status_handler(args.handlers, status))}  # noqa: F841

    resp = await extract_player_response(args.url)

    if resp.playability_status.status in ("ERROR", "LOGIN_REQUIRED", "UNPLAYABLE"):
        status.queue.put_nowait(
            messages.StreamUnavailableMessage(
                resp.playability_status.status, resp.playability_status.reason
            )
        )
        return

    assert resp.video_details
    assert resp.microformat
    if not resp.microformat.live_broadcast_details:
        # return unavailable for non-live content
        status.queue.put_nowait(
            messages.StreamUnavailableMessage(
                "NOT_LIVE_CONTENT", "This video does not have live streaming data available."
            )
        )
        return
    status.queue.put_nowait(
        messages.StreamInfoMessage(
            resp.video_details.author,
            resp.video_details.title,
            resp.microformat.live_broadcast_details.start_datetime,
        )
    )

    if args.dry_run:
        return

    video_id = resp.video_details.video_id if resp.video_details else None
    heartbeat = YTPlayerHeartbeatResponse(playability_status=resp.playability_status)

    heartbeat_token: str | None = (
        resp.heartbeat_params.heartbeat_token if resp.heartbeat_params else None
    )

    while not resp.streaming_data:
        if heartbeat.playability_status.status == "OK":
            resp = await extract_player_response(args.url)
            if resp.streaming_data:
                continue

        # if LIVE_STREAM_OFFLINE then stream may have finished
        if heartbeat.playability_status.status in (
            "UNPLAYABLE",
            "LOGIN_REQUIRED",
        ):
            # waiting room appears to be unavailable; either recheck or stop
            # "UNPLAYABLE" is used for members-only streams without auth
            # it is also used when the stream is private post-live
            status.queue.put_nowait(
                messages.StreamUnavailableMessage(
                    heartbeat.playability_status.status, heartbeat.playability_status.reason
                )
            )

            if not args.poll_unavailable_interval:
                return
            await asyncio.sleep(args.poll_unavailable_interval)
            resp = await extract_player_response(args.url)
            heartbeat = YTPlayerHeartbeatResponse(playability_status=resp.playability_status)
            continue

        seconds_wait = 20.0
        if (
            heartbeat.playability_status.live_streamability
            and heartbeat.playability_status.live_streamability.offline_slate
        ):
            timestamp = heartbeat.playability_status.live_streamability.offline_slate.scheduled_start_datetime
            if timestamp:
                now = datetime.datetime.now(datetime.timezone.utc)

                seconds_remaining = (timestamp - now).total_seconds() - args.schedule_offset
                if seconds_remaining > 0:
                    status.queue.put_nowait(
                        messages.StringMessage(
                            f"No stream available (scheduled to start in {int(seconds_remaining)}s at {timestamp})"
                        )
                    )
                else:
                    status.queue.put_nowait(
                        messages.StringMessage(
                            f"No stream available (should have started {int(-seconds_remaining)}s ago at {timestamp})"
                        )
                    )
                if seconds_remaining > 0:
                    seconds_wait = seconds_remaining
                    if args.poll_interval > 0 and seconds_wait > args.poll_interval:
                        seconds_wait = args.poll_interval
        else:
            status.queue.put_nowait(messages.StringMessage("No stream available, polling"))

        await asyncio.sleep(seconds_wait)
        if video_id:
            try:
                heartbeat = await _get_live_stream_status(
                    video_id, heartbeat_token=heartbeat_token
                )
                continue
            except RuntimeError:
                pass
        resp = await extract_player_response(args.url)
        heartbeat = YTPlayerHeartbeatResponse(playability_status=resp.playability_status)

    if args.list_formats:
        for format in resp.streaming_data.adaptive_formats:
            format_disp = format
            format_disp.url = None
            status.queue.put_nowait(messages.StringMessage(str(format_disp)))
        return

    assert resp.video_details
    assert resp.microformat
    assert resp.microformat.live_broadcast_details
    video_id = resp.video_details.video_id
    status.queue.put_nowait(
        messages.StreamInfoMessage(
            resp.video_details.author,
            resp.video_details.title,
            resp.microformat.live_broadcast_details.start_datetime,
        )
    )

    workdir = args.staging_directory or pathlib.Path(".")
    workdir.mkdir(parents=True, exist_ok=True)

    outdir = args.output_directory or pathlib.Path()

    # derive filename from stream title at time of starting the download
    # unlike ytarchive we use absolute paths when invoking ffmpeg,
    # so we do not need to check for a '-' prefix
    output_basename = resp.video_details.title.translate(sanitize_table)

    # https://en.wikipedia.org/wiki/Comparison_of_file_systems#Limits
    # modern filesystems commonly have a maximum filename length of 255 bytes with higher limits on paths
    # our default filename convention is also constrained by the following:
    # - 12 bytes for video ID and separator '-'
    # - 3 bytes for multi-broadcast stream extension '.00'; assume possibility of two-digit broadcast IDs
    # - 12 bytes for longest possible file extension '.description'
    #
    # given this, 228 bytes seems like the absolute hard limit, but have a margin of error
    #
    # however upon further testing, it's observed that some applications have even shorter
    # filename (basename?) length constraints like 240, so the revised hard limit is 213
    trunc_basename = _string_byte_trim(output_basename, 192)

    # output_paths[dest] = src
    output_paths = {}

    if output_basename != trunc_basename:
        # save original title
        output_basename = trunc_basename
        title_path = workdir / f"{video_id}.title.txt"
        title_path.write_text(
            resp.video_details.title,
            encoding="utf8",
            newline="\n",
        )
        output_paths[outdir / f"{output_basename}-{video_id}.title.txt"] = title_path
        status.queue.put_nowait(messages.StringMessage("Output filename will be truncated"))

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

    broadcast_tasks: dict[str, list[asyncio.Task]] = {}
    manifest_outputs: dict[str, set[pathlib.Path]] = collections.defaultdict(set)
    async with asyncio.TaskGroup() as tg:
        vidsel = FormatSelector(
            YTPlayerMediaType.VIDEO, max_video_resolution=args.max_video_resolution
        )
        if args.prioritize_vp9:
            vidsel.codec = "vp9"

        while True:
            heartbeat = await _get_live_stream_status(video_id, heartbeat_token=heartbeat_token)
            playability_status = heartbeat.playability_status

            # spin up tasks for any new broadcasts seen
            # we do this first so we have at least one broadcast if the stream has finished
            if playability_status.live_streamability:
                live_streamability = playability_status.live_streamability

                broadcast_key = live_streamability.broadcast_id
                if broadcast_key not in broadcast_tasks:
                    broadcast_resp = await _get_web_player_response(video_id)
                    if broadcast_resp:
                        resp_broadcast_key = playability_status.live_streamability.broadcast_id
                        # ensure broadcast didn't change again since the heartbeat response
                        if resp_broadcast_key == broadcast_key:
                            video_stream_dl = tg.create_task(
                                stream_downloader(broadcast_resp, vidsel, workdir)
                            )
                            audio_stream_dl = tg.create_task(
                                stream_downloader(
                                    broadcast_resp,
                                    FormatSelector(YTPlayerMediaType.AUDIO),
                                    workdir,
                                )
                            )
                            broadcast_tasks[broadcast_key] = [video_stream_dl, audio_stream_dl]
                        status.queue.put_nowait(
                            messages.StringMessage(
                                f"Queued broadcast {resp_broadcast_key} for download"
                            )
                        )

            if playability_status.status == "OK":
                if heartbeat.stop_heartbeat:
                    break
                pass
            elif playability_status.status == "LIVE_STREAM_OFFLINE":
                if not playability_status.live_streamability:
                    # no auth + member stream?
                    # ideally we block here until we get valid auth
                    break
                elif playability_status.live_streamability.display_endscreen:
                    break  # stream is over
            elif playability_status.status == "UNPLAYABLE":
                # privated stream
                break
            else:
                status.queue.put_nowait(
                    messages.StringMessage(
                        f"Unexpected heartbeat status response {playability_status.status}"
                    )
                )

            await asyncio.sleep(20)
        status.queue.put_nowait(
            messages.StringMessage(
                "Done with heartbeat checks; waiting for broadcast download to finish"
            )
        )

    for task in itertools.chain.from_iterable(broadcast_tasks.values()):
        try:
            result = await task
            for manifest_id, output_prefixes in result.items():
                manifest_outputs[manifest_id] |= output_prefixes
        except Exception as exc:
            print(type(exc), exc)

    status.queue.put_nowait(messages.StreamMuxMessage(list(manifest_outputs)))
    status.queue.put_nowait(messages.StringMessage(str(manifest_outputs)))

    intermediate_file_deletes: list[pathlib.Path] = []

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
            # single broadcast, so output video ID instead (matching ytarchive behavior)
            mux_output_name = f"{output_basename}-{video_id}.mp4"

        output_paths[outdir / mux_output_name] = output_mux_file
        if proc.returncode == 0:
            intermediate_file_deletes.extend(output_stream_paths)

    # if we only have one broadcast with an unexpected output count, the logs will never be
    # rendered in the CLI - yield to other tasks here just in case
    await asyncio.sleep(0)

    if not args.keep_ts_files:
        for f in intermediate_file_deletes:
            f.unlink()

    try:
        # bail if we fail to make the directory
        outdir.mkdir(parents=True, exist_ok=True)

        # move files to their final location
        #
        # file paths may be too long on some filesystems; to reduce the possibility of
        # partially-complete moves we process the longest first then bail if it throws
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
    keep_ts_files: bool = True  # for backwards compatibility
    poll_interval: int = 0
    poll_unavailable_interval: int = 0
    schedule_offset: int = 0
    dry_run: bool = False
    list_formats: bool = False
    ffmpeg_path: pathlib.Path | None = None
    cookie_file: pathlib.Path | None = None
    cookies_from_browser: str | None = None
    num_parallel_downloads: int = 1
    po_token: str | None = None
    visitor_data: str | None = None
    handlers: list[BaseMessageHandler] = msgspec.field(default_factory=list)

    async def async_run(self) -> None:
        await _run(self)

    def run(self) -> None:
        asyncio.run(_run(self))
