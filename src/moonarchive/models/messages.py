#!/usr/bin/python3

import datetime
import pathlib

import msgspec

from .ffmpeg import FFMPEGProgress
from .youtube_player import YTPlayerAdaptiveFormats, YTPlayerMediaType


class BaseMessage(msgspec.Struct, tag=True):
    pass


class StringMessage(BaseMessage, tag="string-message"):
    # other properly-typed message structs should be used over this
    text: str


class FragmentMessage(BaseMessage, tag="fragment"):
    current_fragment: int
    max_fragments: int
    media_type: str
    itag: int
    manifest_id: str
    fragment_size: int


class StreamUnavailableMessage(BaseMessage, tag="stream-unavailable"):
    status: str
    reason: str | None


class StreamInfoMessage(BaseMessage, tag="stream-info"):
    channel_name: str
    video_title: str
    start_datetime: datetime.datetime | None


class StreamVideoFormatMessage(BaseMessage, tag="stream-video-format"):
    quality_label: str
    codec: str | None


class FormatSelectionMessage(BaseMessage, tag="format-selection"):
    manifest_id: str
    major_type: YTPlayerMediaType
    format: YTPlayerAdaptiveFormats


class ExtractingPlayerResponseMessage(BaseMessage, tag="extracting-player-response"):
    itag: int
    http_error_code: int


class DownloadStreamJobEndedMessage(BaseMessage, tag="download-stream-ended"):
    media_type: str
    manifest_id: str | None = None


class StreamMuxMessage(BaseMessage, tag="stream-mux"):
    manifests: list[str]


class StreamMuxProgressMessage(BaseMessage, tag="stream-mux-progress"):
    manifest_id: str
    progress: FFMPEGProgress


class DownloadJobFailedOutputMoveMessage(BaseMessage, tag="download-failed-output"):
    # mapping between destination and source
    path_mapping: dict[pathlib.Path, pathlib.Path]


class DownloadJobFinishedMessage(BaseMessage, tag="download-finished"):
    output_paths: list[pathlib.Path]
