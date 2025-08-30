#!/usr/bin/python3

"""
Module that handles API calls.  Includes auth.
"""

import asyncio
import datetime
import hashlib
import json
import pathlib
from contextvars import ContextVar
from http.cookiejar import MozillaCookieJar
from types import ModuleType
from typing import Protocol

import httpx
import msgspec

from ...models import messages as messages
from ._extract import PlayerResponseExtractor, YTCFGExtractor
from ._status import status_queue_ctx
from .config import YTCFG
from .player import (
    YTPlayerHeartbeatResponse,
    YTPlayerResponse,
)

browser_cookie3: ModuleType | None = None
try:
    import browser_cookie3  # type: ignore
except ImportError:
    pass

po_token_ctx: ContextVar[str | None] = ContextVar("po_token", default=None)
visitor_data_ctx: ContextVar[str | None] = ContextVar("visitor_data", default=None)

heartbeat_token_ctx: ContextVar[str | None] = ContextVar("heartbeat_token", default=None)

# optional cookie file for making authenticated requests
cookie_file_ctx: ContextVar[pathlib.Path | None] = ContextVar("cookie_file", default=None)


# browser method for retrieving cookies
class _Browser(Protocol):
    def __call__(self, cookie_file: pathlib.Path | None = None, domain_name: str = ""):
        pass


browser_ctx: ContextVar[_Browser | None] = ContextVar("browser", default=None)

ytcfg_ctx: ContextVar[YTCFG | None] = ContextVar("ytcfg", default=None)

# mapping of cookie names to their authorization keys
_AUTH_HASHES = {
    "SAPISID": "SAPISIDHASH",
    "__Secure-1PAPISID": "SAPISID1PHASH",
    "__Secure-3PAPISID": "SAPISID3PHASH",
}

# initial innertube data; these should be replaced by the results from extract_yt_cfg
_INITIAL_INNERTUBE_CLIENT_CONTEXT = {
    "clientName": "WEB",
    "clientVersion": "2.20250516.01.00",
    "hl": "en",
}
_INITIAL_INNERTUBE_CLIENT_HEADERS = {
    "X-YouTube-Client-Name": "1",
    "X-YouTube-Client-Version": "2.20250516.01.00",
    "Origin": "https://www.youtube.com",
    "content-type": "application/json",
}


async def extract_player_response(url: str) -> YTPlayerResponse:
    """
    Scrapes a given YouTube URL for the initial player response.
    """
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

    heartbeat_token = heartbeat_token_ctx.get()
    if heartbeat_token:
        post_dict["heartbeatToken"] = heartbeat_token

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
    """
    Obtains the player state for the given video ID.
    """
    post_dict: dict = {
        "context": {"client": _INITIAL_INNERTUBE_CLIENT_CONTEXT},
        "playbackContext": {"contentPlaybackContext": {"html5Preference": "HTML5_PREF_WANTS"}},
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


def _set_browser_ctx_by_name(browser_name: str) -> None:
    if not browser_cookie3:
        raise ValueError("Cannot set cookies from browser; missing browser-cookie3 dependency")
    _browser_fns: dict[str, _Browser] = {b.__name__: b for b in browser_cookie3.all_browsers}
    if browser_name not in _browser_fns:
        raise ValueError(f"Cannot set cookies from unknown browser {browser_name}")
    browser_ctx.set(_browser_fns[browser_name])


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

    sapisid_cookie = cookies.get("__Secure-3PAPISID", domain=".youtube.com") or cookies.get(
        "SAPISID", domain=".youtube.com"
    )
    if not sapisid_cookie:
        return None
    if not cookies.get("SAPISID", domain=".youtube.com"):
        cookies.set("SAPISID", sapisid_cookie, domain=".youtube.com")

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
