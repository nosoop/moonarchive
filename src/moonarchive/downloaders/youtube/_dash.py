#!/usr/bin/python3

import asyncio
import datetime
import io
import urllib
from contextvars import ContextVar
from typing import AsyncIterator

import httpx
import msgspec

from ...models import messages as messages
from ._format import FormatSelector
from ._innertube import _get_live_stream_status, _get_web_player_response, po_token_ctx
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
    """
    Exception indicating that we received a fragment response without any content.
    In this situation we should retry, as we should get a non-empty result next time if the
    stream is still running.

    This is only used for control flow within the iterator.
    """


class RepeatedFragmentExpiryException(Exception):
    """
    Exception indicating that access to the fragment failed even after fetching a new player
    instance.  This likely indicates a bad proof-of-origin token.
    """


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

    # some formats are not present in the DASH manifest; just exclude them for the time being
    # nosoop/moonarchive#16
    available_formats = [
        format
        for format in resp.streaming_data.adaptive_formats
        if format.itag in manifest.format_urls
    ]

    selected_format, *_ = selector.select(available_formats) or (None,)
    if not selected_format:
        raise ValueError(f"Could not meet criteria format for format selector {selector}")
    itag = selected_format.itag

    timeout = selected_format.target_duration_sec
    if not timeout:
        raise ValueError("itag does not have a target duration set")

    cur_seq = start_seq
    max_seq = 0

    # current broadcast ID may be None if the stream is done
    # this is fine, since we only use it to check for new broadcasts
    current_broadcast_id = (
        resp.playability_status.live_streamability.broadcast_id
        if resp.playability_status.live_streamability
        else None
    )
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

    fragment_timeout_retries = 0
    last_seq_auth_expiry = 0
    last_heartbeat_check = None

    num_parallel_downloads = num_parallel_downloads_ctx.get()

    reqs: list[tuple[int, asyncio.Task]] = []

    po_token = po_token_ctx.get()

    while True:
        # clear any outstanding requests from the previous iteration
        for req_seq, req_task in reqs:
            req_task.cancel()

        url = manifest.format_urls[itag]
        if po_token:
            url = urllib.parse.urljoin(url, f"pot/{po_token}/")

        fragment_access_expired = False

        try:
            # allow for batching requests of fragments if we're behind
            # if we're caught up this only requests one fragment
            # if any request fails, the rest of the tasks will be wasted
            batch_count = max(1, min(max_seq - cur_seq, num_parallel_downloads))
            if last_heartbeat_check and batch_count > 1:
                # dynamically throttle batches if we ran into issues recently
                time_since_check = datetime.datetime.now(tz=datetime.UTC) - last_heartbeat_check
                batch_count = min(1 + int(time_since_check.total_seconds() / 10), batch_count)
            reqs = [
                # it doesn't look like we need cookies for fetching fragments
                (
                    s,
                    asyncio.create_task(
                        client.get(urllib.parse.urljoin(url, f"sq/{s}/"), timeout=timeout * 2)
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
            fragment_timeout_retries = 0
            continue
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 403:
                # stream access expired? retrieve a fresh manifest
                # the stream may have finished while we were mid-download, so don't check that here
                # FIXME: we need to check playability_status instead of bailing
                if last_seq_auth_expiry == cur_seq:
                    if max_seq - 2 > cur_seq:
                        # It's very rare, but there are instances when YouTube repeatedly
                        # responds with a 403 on a fragment that should be valid (even
                        # unplayable when rewinding is enabled).  Skip it.
                        status_queue.put_nowait(
                            messages.StringMessage(
                                f"Skipping fragment {cur_seq} on "
                                f"{current_manifest_id} {selector.major_type} "
                                "due to repeated 403 results"
                            )
                        )
                        cur_seq += 1
                        # Increment sequence auth to avoid multiple player requests on
                        # sequential 403s.
                        last_seq_auth_expiry = cur_seq
                        continue
                    status_queue.put_nowait(
                        messages.StringMessage(
                            f"Received HTTP 403 error on previous sequence {cur_seq=}, "
                            "stopping operation."
                        )
                    )
                    raise RepeatedFragmentExpiryException
                # record the sequence we 403'd on - if we receive another one, then bail to
                # avoid further errors
                last_seq_auth_expiry = cur_seq
                fragment_access_expired = True
            status_queue.put_nowait(
                messages.StringMessage(
                    f"Received HTTP {exc.response.status_code} error on "
                    f"{current_manifest_id} {selector.major_type} ({cur_seq} of {max_seq})"
                )
            )
        except httpx.ReadTimeout:
            status_queue.put_nowait(
                messages.StringMessage(
                    f"Fragment retrieval timed out on "
                    f"{current_manifest_id} {selector.major_type} ({cur_seq} of {max_seq})"
                )
            )
            # timeouts can happen when going between sleep and waking states, so the max_seq we
            # have may not reflect the live broadcast; if this happens on repeated requests then
            # it probably is normal stream ending / downtime
            fragment_timeout_retries += 1
            if fragment_timeout_retries < 5:
                continue
        except (httpx.HTTPError, httpx.StreamError) as exc:
            # for everything else we just retry
            status_queue.put_nowait(
                messages.StringMessage(
                    f"Unhandled httpx exception for type {selector.major_type}: {exc=}"
                )
            )
        except EmptyFragmentException:
            status_queue.put_nowait(
                messages.StringMessage(
                    f"Empty {selector.major_type} fragment at {cur_seq}; retrying"
                )
            )

        # if the broadcast has ended YouTube will increment the max sequence number by 2
        # without any fragments being present there
        probably_caught_up = cur_seq > 0 and cur_seq >= max_seq - 3

        # verify that the stream is still available, blocking new fragment requests if offline
        if not fragment_access_expired:
            while True:
                if last_heartbeat_check:
                    # require at least 15 seconds between heartbeat checks
                    pause_for = (
                        last_heartbeat_check
                        + datetime.timedelta(seconds=15)
                        - datetime.datetime.now(tz=datetime.UTC)
                    )
                    await asyncio.sleep(pause_for.total_seconds())
                last_heartbeat_check = datetime.datetime.now(tz=datetime.UTC)

                # `return` means we are done; stop downloading
                # `break` means start downloading fragments again
                # `pass` means continue polling for a change in state
                heartbeat = await _get_live_stream_status(video_id)
                playability = heartbeat.playability_status
                streamability = playability.live_streamability
                match playability.status:
                    case "OK":
                        broadcast_id = streamability.broadcast_id if streamability else None
                        if broadcast_id != current_broadcast_id and probably_caught_up:
                            return
                        # intermittent issue, should be resolved
                        break
                    case "LIVE_STREAM_OFFLINE":
                        # stream is temporarily offline or ending
                        if not probably_caught_up:
                            break
                        elif streamability and streamability.display_endscreen:
                            return
                        pass
                    case "UNPLAYABLE":
                        # stream went private or something
                        # since hitting this code path meant we didn't get 403 on the fragment
                        # (yet), just retry and hope the playlist hasn't expired
                        if not probably_caught_up:
                            break
                        return
            # we may want to fall through to getting a new response in the future, so instead
            # of `elif`ing this we'll just continue
            continue

        # make new player request if the playlist expired or stream went private
        # we need this call to be resilient to failures, otherwise we may have an incomplete download
        resp = await _get_web_player_response(video_id)

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
            if not probably_caught_up:
                # post-live, X-Head-Seqnum tends to be two above the true number
                pass
            else:
                return

        if not resp.streaming_data:
            # stream is offline; sleep and retry previous fragment
            if resp.playability_status.status == "LIVE_STREAM_OFFLINE":
                # this code path is hit if the streamer is disconnected; retry for frag
                #
                # note that if the stream goes unavailable for 6+ hours it might be possible
                # that we start hitting this code path repeatedly
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
            status_queue.put_nowait(messages.StringMessage("Failed to retrieve DASH manifest"))
            return

        if current_manifest_id != resp.streaming_data.dash_manifest_id:
            # manifest ID differs; we no longer have access to the broadcast this task is for
            return

        # it is actually possible for a format to disappear from an updated manifest without
        # incrementing the ID - likely to be inconsistencies between servers
        # this may only apply to the android response
        if itag not in manifest.format_urls:
            # select a new format
            # IMPORTANT: we don't reset the fragment counter here to minimize the chance of the
            # stream going unavailable mid-download
            available_formats = [
                format
                for format in resp.streaming_data.adaptive_formats
                if format.itag in manifest.format_urls
            ]

            preferred_format, *_ = selector.select(available_formats)
            itag = preferred_format.itag

            status_queue.put_nowait(
                messages.FormatSelectionMessage(
                    current_manifest_id, selector.major_type, preferred_format
                )
            )
