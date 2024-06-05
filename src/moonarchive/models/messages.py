#!/usr/bin/python3

import datetime

import msgspec


class BaseMessage(msgspec.Struct, tag=True):
    pass


class StringMessage(BaseMessage, tag="string-message"):
    # other properly-typed message structs should be used over this
    text: str


class FragmentMessage(BaseMessage, tag="fragment"):
    current_fragment: int
    max_fragments: int
    itag: int
    manifest_id: str
    fragment_size: int


class StreamUnavailableMessage(BaseMessage, tag="stream-unavailable"):
    status: str
    reason: str | None


class StreamInfoMessage(BaseMessage, tag="stream-info"):
    channel_name: str
    video_title: str
    start_datetime: datetime.datetime


class StreamVideoFormatMessage(BaseMessage, tag="stream-video-format"):
    quality_label: str
    # TODO: write codec (vp9 / h264)


class ExtractingPlayerResponseMessage(BaseMessage, tag="extracting-player-response"):
    itag: int
    http_error_code: int


class DownloadJobEndedMessage(BaseMessage, tag="download-job-ended"):
    media_type: str
