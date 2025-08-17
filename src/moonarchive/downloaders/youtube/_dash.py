#!/usr/bin/python3

import asyncio
import io
import string
from contextvars import ContextVar
from typing import AsyncIterator

import httpx
import msgspec

from ...models import messages as messages
from ._format import FormatSelector
from ._innertube import _get_web_player_response, po_token_ctx
from ._status import status_queue_ctx
from .player import (
    YTPlayerMediaType,
    YTPlayerResponse,
)

# for long running streams, YouTube allows retrieval of fragments from this far back
NUM_SECS_FRAG_RETENTION = 86_400 * 5

# number of downloads allowed for a substream
num_parallel_downloads_ctx: ContextVar[int] = ContextVar("num_parallel_downloads")


class FragmentInfo(msgspec.Struct, kw_only=True):
    cur_seq: int
    max_seq: int
    itag: int
    manifest_id: str
    buffer: io.BytesIO


class EmptyFragmentException(Exception):
    # exception indicating that we received a fragment response but it was empty
    # in this situation we should retry, as we should get a non-empty result next time if the
    # stream is still running
    pass


async def frag_iterator(
    resp: YTPlayerResponse, selector: FormatSelector, start_seq: int = 0
) -> AsyncIterator[FragmentInfo]:
    # yields fragment information
    # this should refresh the manifest as needed and yield the next available fragment

    assert resp.video_details
    video_id = resp.video_details.video_id

    # the player response is expected to initially be a valid stream to download from
    assert resp.streaming_data
    manifest = await resp.streaming_data.get_dash_manifest()
    if not manifest:
        raise ValueError("Received a response with no DASH manifest")

    selected_format, *_ = selector.select(resp.streaming_data.adaptive_formats) or (None,)
    if not selected_format:
        raise ValueError(f"Could not meet criteria format for format selector {selector}")
    itag = selected_format.itag

    timeout = selected_format.target_duration_sec
    if not timeout:
        raise ValueError("itag does not have a target duration set")

    cur_seq = start_seq
    max_seq = 0

    current_manifest_id = resp.streaming_data.dash_manifest_id
    assert current_manifest_id

    status_queue = status_queue_ctx.get()

    # there is a limit to the fragments that can be obtained in long-running streams
    earliest_seq_available = int(manifest.start_number - (NUM_SECS_FRAG_RETENTION // timeout))
    if earliest_seq_available > cur_seq:
        cur_seq = earliest_seq_available
        status_queue.put_nowait(
            messages.StringMessage(f"Starting from earliest available fragment {cur_seq}.")
        )

    if selector.major_type == YTPlayerMediaType.VIDEO and selected_format.quality_label:
        status_queue.put_nowait(
            messages.StreamVideoFormatMessage(
                selected_format.quality_label, selected_format.media_type.codec
            )
        )
    status_queue.put_nowait(
        messages.FormatSelectionMessage(
            current_manifest_id, selector.major_type, selected_format
        )
    )

    client = httpx.AsyncClient(follow_redirects=True)

    check_stream_status = False
    fragment_timeout_retries = 0
    last_seq_auth_expiry = 0

    num_parallel_downloads = num_parallel_downloads_ctx.get()

    reqs: list[tuple[int, asyncio.Task]] = []

    po_token = po_token_ctx.get()

    while True:
        # clear any outstanding requests from the previous iteration
        for req_seq, req_task in reqs:
            req_task.cancel()

        url = manifest.format_urls[itag]
        if po_token:
            url = string.Template(url.template + f"/pot/{po_token}")

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

                    # this should never happen while there are fragments available ahead of the current one,
                    # (at the very least I've never seen the assertion trip)
                    # but break out via exception just so we're assured we don't skip fragments here

                    # it looks like we may also hit this case if the live replay is unlisted at the end
                    raise EmptyFragmentException

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
            check_stream_status = False
            fragment_timeout_retries = 0
            continue
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 403:
                # stream access expired? retrieve a fresh manifest
                # the stream may have finished while we were mid-download, so don't check that here
                # FIXME: we need to check playability_status instead of bailing
                if last_seq_auth_expiry == max_seq:
                    status_queue.put_nowait(
                        messages.StringMessage(
                            f"Received HTTP 403 error on previous sequence {cur_seq=}, "
                            "stopping operation."
                        )
                    )
                    # we should probably error out of this
                    return
                # record the sequence we 403'd on - if we receive another one, then bail to
                # avoid further errors
                last_seq_auth_expiry = max_seq
                status_queue.put_nowait(
                    messages.StringMessage(
                        f"Received HTTP 403 error {cur_seq=}, getting new player response"
                    )
                )
                check_stream_status = True
            elif exc.response.status_code in (404, 500, 503):
                check_stream_status = True
            elif exc.response.status_code == 401:
                status_queue.put_nowait(
                    messages.StringMessage(f"Received HTTP 401 error {cur_seq=}, retrying")
                )
                # TODO: adjust our num_parallel_downloads down for a period of time
                await asyncio.sleep(10)
                continue
            else:
                status_queue.put_nowait(
                    messages.StringMessage(
                        f"Unhandled HTTP code {exc.response.status_code}, retrying"
                    )
                )
                await asyncio.sleep(10)
                continue
            if check_stream_status:
                status_queue.put_nowait(
                    messages.ExtractingPlayerResponseMessage(itag, exc.response.status_code)
                )
        except httpx.TimeoutException:
            status_queue.put_nowait(
                messages.StringMessage(
                    f"Fragment retrieval for type {selector.major_type} timed out: {cur_seq} of {max_seq}"
                )
            )
            fragment_timeout_retries += 1
            if fragment_timeout_retries < 5:
                continue
            check_stream_status = True
        except (httpx.HTTPError, httpx.StreamError) as exc:
            # for everything else we just retry
            status_queue.put_nowait(
                messages.StringMessage(
                    f"Unhandled httpx exception for type {selector.major_type}: {exc=}"
                )
            )
            check_stream_status = True
        except EmptyFragmentException:
            status_queue.put_nowait(
                messages.StringMessage(
                    f"Empty {selector.major_type} fragment at {cur_seq}; retrying"
                )
            )
            await asyncio.sleep(min(timeout * 5, 20))
            continue

        if check_stream_status:
            # we need this call to be resilient to failures, otherwise we may have an incomplete download
            try_resp = await _get_web_player_response(video_id)
            if not try_resp:
                continue
            resp = try_resp

            if not resp.microformat or not resp.microformat.live_broadcast_details:
                # video is private?
                status_queue.put_nowait(
                    messages.StringMessage(
                        "Microformat or live broadcast details unavailable "
                        f"for type {selector.major_type}"
                    )
                )
                return

            # if the broadcast has ended YouTube will increment the max sequence number by 2
            # without any fragments being present there
            probably_caught_up = cur_seq > 0 and cur_seq >= max_seq - 3

            # the server has indicated that no fragment is present
            # we're done if the stream is no longer live and a duration is rendered
            if not resp.microformat.live_broadcast_details.is_live_now and (
                not resp.video_details or resp.video_details.num_length_seconds
            ):
                # we need to handle this differenly during post-live
                # the video server may return 503
                if not probably_caught_up:
                    # post-live, X-Head-Seqnum tends to be two above the true number
                    pass
                else:
                    return

            if not resp.streaming_data:
                # stream is offline; sleep and retry previous fragment
                if resp.playability_status.status == "LIVE_STREAM_OFFLINE":
                    # this code path is hit if the streamer is disconnected; retry for frag
                    pass
                if resp.playability_status.status == "UNPLAYABLE":
                    # either member stream, possibly unlisted, or beyond the 12h limit
                    #   reason == "This live stream recording is not available."
                    #   reason == "This video is available to this channel's members on level..."

                    if (
                        probably_caught_up
                        and not resp.microformat.live_broadcast_details.is_live_now
                    ):
                        return

                    # this code path is also hit if cookies are expired, in which case we are
                    # not logged in.  this is fine; we'll retry fetching the frag on the current
                    # manifest and continue attempting authed player requests until the stream
                    # ends (user can update auth files out-of-band and will be reflected on
                    # next request); without auth we won't know how many fragments are available
                    #
                    # if the manifest isn't expired (members stream, temporarily offline)
                    # retrying the current manifest should be all that's necessary
                    #
                    # however, if the manifest *is* expired then a 403 result will hit the
                    # 'repeated bad auth' code path and the download will stop
                    # we could try setting last_seq_auth_expiry = 0
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
            manifest = await resp.streaming_data.get_dash_manifest()
            if not manifest:
                status_queue.put_nowait(
                    messages.StringMessage("Failed to retrieve DASH manifest")
                )
                return

        assert resp.streaming_data
        assert resp.streaming_data.dash_manifest_id
        if current_manifest_id != resp.streaming_data.dash_manifest_id:
            # manifest ID differs; we're done
            return

        # it is actually possible for a format to disappear from an updated manifest without
        # incrementing the ID - likely to be inconsistencies between servers
        # this may only apply to the android response
        if itag not in manifest.format_urls:
            # select a new format
            # IMPORTANT: we don't reset the fragment counter here to minimize the chance of the
            # stream going unavailable mid-download
            preferred_format, *_ = selector.select(resp.streaming_data.adaptive_formats)
            itag = preferred_format.itag

            status_queue.put_nowait(
                messages.FormatSelectionMessage(
                    current_manifest_id, selector.major_type, preferred_format
                )
            )
