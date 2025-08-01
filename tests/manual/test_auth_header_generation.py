#!/usr/bin/python3

"""
Verifies the correctness of moonarchive's authorization implementation against a given HAR file.

On Firefox, the HAR file can be generated by doing the following:

1. Navigate to YouTube on a logged-in account.
2. Open the web developer tools console.
3. Click on the YouTube logo to generate a POST request.
4. Right-click on the generated network request, then "Save as HAR".
5. Run this script, passing the HAR file as an argument.
"""

import argparse
import datetime
import pathlib
import json

import httpx
import msgspec

from moonarchive.downloaders.youtube import _build_auth_from_cookies


class HTTPArchiveCookie(msgspec.Struct, rename="camel"):
    name: str
    value: str


class HTTPArchiveHeader(msgspec.Struct, rename="camel"):
    name: str
    value: str


class HTTPArchiveLogEntryResponseContent(msgspec.Struct, rename="camel"):
    text: str


class HTTPArchiveLogEntryRequest(msgspec.Struct, rename="camel"):
    cookies: list[HTTPArchiveCookie]
    headers: list[HTTPArchiveHeader]

    def get_header(self, name: str) -> str | None:
        for h in self.headers:
            if h.name == name:
                return h.value
        return None


class HTTPArchiveLogEntryResponse(msgspec.Struct, rename="camel"):
    cookies: list[HTTPArchiveCookie]
    content: HTTPArchiveLogEntryResponseContent


class HTTPArchiveLogEntry(msgspec.Struct, rename="camel"):
    request: HTTPArchiveLogEntryRequest
    response: HTTPArchiveLogEntryResponse


class HTTPArchiveLog(msgspec.Struct, rename="camel"):
    entries: list[HTTPArchiveLogEntry]


class HTTPArchive(msgspec.Struct, rename="camel"):
    log: HTTPArchiveLog


class YouTubeAppWebResponseContext(msgspec.Struct, rename="camel"):
    datasync_id: str
    logged_out: bool


class YouTubeBrowseResponseContext(msgspec.Struct, rename="camel"):
    main_app_web_response_context: YouTubeAppWebResponseContext


class YouTubeBrowseResponse(msgspec.Struct, rename="camel"):
    response_context: YouTubeBrowseResponseContext


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument("file", help="An HTTP archive file to test against", type=pathlib.Path)

    args = parser.parse_args()

    archive_file = msgspec.json.decode(args.file.read_bytes(), type=HTTPArchive)
    log_entry = archive_file.log.entries[0]

    # extract the timestamp used for calculating the auth from the existing auth value
    _, auth_value, *_ = log_entry.request.get_header("Authorization").split(" ")
    auth_timestamp, *_ = auth_value.split("_")

    # extract the user session ID from the response datasync
    response_content = msgspec.json.decode(
        log_entry.response.content.text, type=YouTubeBrowseResponse
    )
    _, _, user_session_id = (
        response_content.response_context.main_app_web_response_context.datasync_id.partition(
            "||"
        )
    )

    cookies = httpx.Cookies()
    for c in log_entry.request.cookies:
        cookies.set(c.name, c.value)

    auth_header = _build_auth_from_cookies(
        cookies,
        user_session_id=user_session_id,
        current_dt=datetime.datetime.fromtimestamp(int(auth_timestamp)),
    )

    print(
        "Authorization implementation matches:",
        log_entry.request.get_header("Authorization") == auth_header,
    )


if __name__ == "__main__":
    main()
