#!/usr/bin/python3

import asyncio
import collections
import dataclasses
import datetime
import hashlib
import html.parser
import io
import itertools
import json
import operator
import pathlib
import shutil
import string
import sys
import urllib.parse
import urllib.request
from contextvars import ContextVar
from http.cookiejar import MozillaCookieJar
from types import ModuleType
from typing import AsyncIterator, Protocol, Type

import av
import httpx
import msgspec

from ..models import messages as messages
from ..models.ffmpeg import FFMPEGProgress
from ..models.youtube_config import YTCFG
from ..models.youtube_player import (
    YTPlayerAdaptiveFormats,
    YTPlayerHeartbeatResponse,
    YTPlayerMediaType,
    YTPlayerResponse,
)
from ..output import BaseMessageHandler

browser_cookie3: ModuleType | None = None
try:
    import browser_cookie3  # type: ignore
except ImportError:
    pass

# table to remove illegal characters on Windows
# we use this to match ytarchive file output behavior
sanitize_table = str.maketrans({c: "_" for c in r'<>:"/\|?*'})


# status queue for downloading tasks; this is available in stream_downloader and frag_iterator
status_queue_ctx: ContextVar[asyncio.Queue] = ContextVar("status_queue")


po_token_ctx: ContextVar[str | None] = ContextVar("po_token", default=None)
visitor_data_ctx: ContextVar[str | None] = ContextVar("visitor_data", default=None)

# optional cookie file for making authenticated requests
cookie_file_ctx: ContextVar[pathlib.Path | None] = ContextVar("cookie_file", default=None)


# browser method for retrieving cookies
class _Browser(Protocol):
    def __call__(self, cookie_file: pathlib.Path | None = None, domain_name: str = ""):
        pass


browser_ctx: ContextVar[_Browser | None] = ContextVar("browser", default=None)

ytcfg_ctx: ContextVar[YTCFG | None] = ContextVar("ytcfg", default=None)

# number of downloads allowed for a substream
num_parallel_downloads_ctx: ContextVar[int] = ContextVar("num_parallel_downloads")


# for long running streams, YouTube allows retrieval of fragments from this far back
NUM_SECS_FRAG_RETENTION = 86_400 * 5

# mapping of cookie names to their authorization keys
_AUTH_HASHES = {
    "SAPISID": "SAPISIDHASH",
    "__Secure-1PAPISID": "SAPISID1PHASH",
    "__Secure-3PAPISID": "SAPISID3PHASH",
}


def _string_byte_trim(input: str, length: int) -> str:
    """
    Trims a string using a byte limit, while ensuring that it is still valid Unicode.
    https://stackoverflow.com/a/70304695
    """
    bytes_ = input.encode()
    try:
        return bytes_[:length].decode()
    except UnicodeDecodeError as err:
        return bytes_[: err.start].decode()


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

            # we'll just let the decoder throw to determine where the data ends
            start_pos = data[decl_pos:].find("{") + decl_pos
            try:
                self.result = json.loads(data[start_pos:])
            except json.JSONDecodeError as e:
                self.result = json.loads(data[start_pos : start_pos + e.pos])

    return InternalHTMLParser


PlayerResponseExtractor = create_json_object_extractor("var ytInitialPlayerResponse =")
YTCFGExtractor = create_json_object_extractor('ytcfg.set({"CLIENT')

# initial innertube data; these should be replaced by the results from extract_yt_cfg
_INITIAL_INNERTUBE_CLIENT_CONTEXT = {
    "clientName": "WEB",
    "clientVersion": "2.20241121.01.00",
    "hl": "en",
}
_INITIAL_INNERTUBE_CLIENT_HEADERS = {
    "X-YouTube-Client-Name": "1",
    "X-YouTube-Client-Version": "2.20241121.01.00",
    "Origin": "https://www.youtube.com",
    "content-type": "application/json",
}


async def extract_player_response(url: str) -> YTPlayerResponse:
    response_extractor = PlayerResponseExtractor()
    cookies = _cookies_from_filepath()
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
        return msgspec.convert(response_extractor.result, type=YTPlayerResponse)  # type: ignore


async def extract_yt_cfg(url: str) -> YTCFG:
    # scrapes a page and returns a current YTCFG
    response_extractor = YTCFGExtractor()
    cookies = _cookies_from_filepath()
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
                        "Failed to retrieve YTCFG response " f"(attempt {n} of {max_retries})"
                    )
                )
                await asyncio.sleep(6)

        if not response_extractor.result:  # type: ignore
            raise ValueError("Could not extract YTCFG response")
        return msgspec.convert(response_extractor.result, type=YTCFG)  # type: ignore


async def _get_live_stream_status(video_id: str) -> YTPlayerHeartbeatResponse:
    post_dict: dict = {
        "context": {"client": _INITIAL_INNERTUBE_CLIENT_CONTEXT},
        "heartbeatRequestParams": {
            "heartbeatChecks": ["HEARTBEAT_CHECK_TYPE_LIVE_STREAM_STATUS"]
        },
    }
    post_dict["videoId"] = video_id

    ytcfg = ytcfg_ctx.get()
    if not ytcfg:
        # we assume a valid ytcfg at this point
        raise ValueError("No YTCFG available in context")
    visitor_data = visitor_data_ctx.get()

    status_queue = status_queue_ctx.get()
    cookies = _cookies_from_filepath()
    async with httpx.AsyncClient(cookies=cookies) as client:
        headers = _INITIAL_INNERTUBE_CLIENT_HEADERS

        auth = _build_auth_from_cookies(cookies, user_session_id=ytcfg.user_session_id)
        if auth:
            headers["Authorization"] = auth
            headers["X-Origin"] = "https://www.youtube.com"

        if visitor_data:
            ytcfg = msgspec.structs.replace(ytcfg, visitor_data=visitor_data)
        headers |= ytcfg.to_headers()
        post_dict["context"]["client"] |= ytcfg.to_post_context()

        max_retries = 10
        for n in range(max_retries):
            try:
                result = await client.post(
                    "https://www.youtube.com/youtubei/v1/player/heartbeat?alt=json",
                    content=json.dumps(post_dict).encode("utf8"),
                    headers=headers,
                )
                result.raise_for_status()
                return msgspec.json.decode(result.text, type=YTPlayerResponse)  # type: ignore
            except (httpx.HTTPStatusError, httpx.TransportError):
                status_queue.put_nowait(
                    messages.StringMessage(
                        "Failed to retrieve heartbeat response data "
                        f"(attempt {n} of {max_retries})"
                    )
                )
            await asyncio.sleep(5)
        raise RuntimeError("Failed to obtain heartbeat response")


async def _get_web_player_response(video_id: str) -> YTPlayerResponse | None:
    # the DASH manifest via web client expires every 30 seconds as of 2024-08-08
    # so now we masquerade as the android client and extract the manifest there
    post_dict: dict = {
        "context": {"client": _INITIAL_INNERTUBE_CLIENT_CONTEXT},
        "playbackContext": {"contentPlaybackContext": {"html5Preference": "HTML5_PREF_WANTS"}},
    }
    post_dict["videoId"] = video_id
    po_token = po_token_ctx.get()
    if po_token:
        post_dict["serviceIntegrityDimensions"] = {"poToken": po_token}

    ytcfg = ytcfg_ctx.get()
    if not ytcfg:
        # we assume a valid ytcfg at this point
        raise ValueError("No YTCFG available in context")
    visitor_data = visitor_data_ctx.get()

    status_queue = status_queue_ctx.get()
    cookies = _cookies_from_filepath()
    async with httpx.AsyncClient(cookies=cookies) as client:
        headers = _INITIAL_INNERTUBE_CLIENT_HEADERS

        auth = _build_auth_from_cookies(cookies, user_session_id=ytcfg.user_session_id)
        if auth:
            headers["Authorization"] = auth
            headers["X-Origin"] = "https://www.youtube.com"

        if visitor_data:
            ytcfg = msgspec.structs.replace(ytcfg, visitor_data=visitor_data)
        headers |= ytcfg.to_headers()
        post_dict["context"]["client"] |= ytcfg.to_post_context()

        max_retries = 10
        for n in range(max_retries):
            try:
                result = await client.post(
                    f"https://www.youtube.com/youtubei/v1/player?key={ytcfg.innertube_api_key}",
                    content=json.dumps(post_dict).encode("utf8"),
                    headers=headers,
                )
                result.raise_for_status()
                return msgspec.json.decode(result.text, type=YTPlayerResponse)  # type: ignore
            except (httpx.HTTPStatusError, httpx.TransportError):
                status_queue.put_nowait(
                    messages.StringMessage(
                        "Failed to retrieve web player response "
                        f"(attempt {n} of {max_retries})"
                    )
                )
            await asyncio.sleep(5)
        return None


def _cookies_from_filepath() -> httpx.Cookies:
    """
    Retrieves cookies from the given file.  This is called on-demand during normal operation,
    allowing cookies to be updated out-of-band.

    If browser_cookie3 is installed, this may also access cookies from a web browser installed
    on the system.
    """
    cookie_file = cookie_file_ctx.get()
    browser = browser_ctx.get()
    if browser is not None:
        jar = browser(cookie_file=cookie_file, domain_name="youtube.com")
        return httpx.Cookies(jar)
    jar = MozillaCookieJar()
    if cookie_file and cookie_file.is_file() and cookie_file.exists():
        jar.load(str(cookie_file))
    return httpx.Cookies(jar)


def _build_auth_from_cookies(
    cookies: httpx.Cookies | None,
    origin: str = "https://www.youtube.com",
    user_session_id: str | None = None,
    current_dt: datetime.datetime | None = None,
) -> str | None:
    # implementation adapted from yt-dlp (2025.01.12)
    # https://github.com/yt-dlp/yt-dlp/blob/75079f4e3f7dce49b61ef01da7adcd9876a0ca3b/yt_dlp/extractor/youtube.py#L652-L692
    if not cookies:
        return None
    if not current_dt:
        current_dt = datetime.datetime.now(tz=datetime.UTC)

    sapisid_cookie = cookies.get("__Secure-3PAPISID") or cookies.get("SAPISID")
    if not sapisid_cookie:
        return None
    if not cookies.get("SAPISID"):
        cookies.set("SAPISID", sapisid_cookie, ".youtube.com")

    extra_data = {}
    if user_session_id:
        extra_data["u"] = user_session_id

    current_timestamp = round(current_dt.timestamp())
    authorizations = []
    for cookie, auth_name in _AUTH_HASHES.items():
        cookie_value = cookies.get(cookie)
        if cookie_value is None:
            continue
        input_components = []
        if extra_data:
            input_components.append(":".join(extra_data.values()))
        input_components.extend([str(current_timestamp), cookie_value, origin])
        sidhash = hashlib.sha1(" ".join(input_components).encode("utf-8")).hexdigest()
        output_components = [str(current_timestamp), sidhash]
        if extra_data:
            output_components.append("".join(extra_data))
        authorizations.append(f"{auth_name} {'_'.join(output_components)}")
    return " ".join(authorizations)


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


class EmptyFragmentException(Exception):
    # exception indicating that we received a fragment response but it was empty
    # in this situation we should retry, as we should get a non-empty result next time if the
    # stream is still running
    pass


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

    cur_seq = 0
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
        # it is actually possible for a format to disappear from an updated manifest without
        # incrementing the ID - likely to be inconsistencies between servers
        # this may only apply to the android response
        if (
            itag not in manifest.format_urls
            and current_manifest_id == resp.streaming_data.dash_manifest_id
        ):
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

        assert resp.streaming_data.dash_manifest_id
        if current_manifest_id != resp.streaming_data.dash_manifest_id:
            # manifest ID differs; we're done
            return


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
    resp: YTPlayerResponse, selector: FormatSelector, output_directory: pathlib.Path
) -> dict[str, set[pathlib.Path]]:
    # record the manifests and formats we're downloading
    # this is used later to determine which files to mux together
    manifest_outputs = collections.defaultdict(set)

    last_manifest_id = None
    last_frag_dimensions = (0, 0)
    outnum = 0

    status_queue = status_queue_ctx.get()

    async for frag in frag_iterator(resp, selector):
        output_prefix = f"{frag.manifest_id}.f{frag.itag}"

        if frag.manifest_id != last_manifest_id:
            last_manifest_id = frag.manifest_id
            last_frag_dimensions = (0, 0)
            outnum = 0
        if selector.major_type == YTPlayerMediaType.VIDEO:
            # analyze fragment for dimension change mid-manifest
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

    num_parallel_downloads_ctx.set(args.num_parallel_downloads)
    po_token_ctx.set(args.po_token)
    visitor_data_ctx.set(args.visitor_data)
    cookie_file_ctx.set(args.cookie_file)
    if args.cookies_from_browser:
        if not browser_cookie3:
            raise ValueError(
                "Cannot set cookies from browser; missing browser-cookie3 dependency"
            )
        _browser_fns: dict[str, _Browser] = {
            b.__name__: b for b in browser_cookie3.all_browsers
        }
        if args.cookies_from_browser not in _browser_fns:
            raise ValueError(
                f"Cannot set cookies from unknown browser {args.cookies_from_browser}"
            )
        browser_ctx.set(_browser_fns[args.cookies_from_browser])
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
                heartbeat = await _get_live_stream_status(video_id)
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
            heartbeat = await _get_live_stream_status(video_id)
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
            # unique manifest, so output video ID instead (matching ytarchive behavior)
            mux_output_name = f"{output_basename}-{video_id}.mp4"

        output_paths[outdir / mux_output_name] = output_mux_file
        if proc.returncode == 0:
            intermediate_file_deletes.extend(output_stream_paths)

    # if we only have one manifest with an unexpected output count, the logs will never be
    # rendered in the CLI - yield to other tasks here just in case
    await asyncio.sleep(0)

    if not args.keep_ts_files:
        for f in intermediate_file_deletes:
            f.unlink()

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
