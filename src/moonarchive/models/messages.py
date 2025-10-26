#!/usr/bin/python3

import datetime
import pathlib

import msgspec

from .ffmpeg import FFMPEGProgress


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
    from ..downloaders.youtube.player import YTPlayerAdaptiveFormats, YTPlayerMediaType

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


class StreamMuxFailureMessage(BaseMessage, tag="stream-mux-failure"):
    manifest_id: str
    staging_files: set[pathlib.Path]
    reason: str | None = None
    ffmpeg_exit_code: int | None = None
    """
    Error code produced by ffmpeg.
    https://github.com/FFmpeg/FFmpeg/blob/a218cafe4d3be005ab0c61130f90db4d21afb5db/libavutil/error.c#L37-L107
    """


class StreamMuxProgressMessage(BaseMessage, tag="stream-mux-progress"):
    manifest_id: str
    progress: FFMPEGProgress


class DownloadJobFailedOutputMoveMessage(BaseMessage, tag="download-failed-output"):
    # mapping between destination and source
    path_mapping: dict[pathlib.Path, pathlib.Path]


class DownloadJobFinishedMessage(BaseMessage, tag="download-finished"):
    input_paths: set[pathlib.Path]
    """
    Full paths to unprocessed files in the 'staging' directory (including thumbnail /
    description).  Paths may not necessarily point to existing files at the time this message is
    emitted.
    """

    output_paths: set[pathlib.Path]
    """
    Full paths to processed files in the 'output' directory.
    """

    multi_broadcast: bool
    """
    Whether or not the livestream was known to be split across multiple broadcasts.
    Note that it is possible for a stream to start with a broadcast ID higher than 1.
    """

    unmuxed_broadcasts: bool
    """ Whether or not any broadcast had a mux failure. """


class StreamWaitingMessage(BaseMessage, tag="stream-waiting"):
    """
    Used while waiting for stream.
    Allows consumers to update the visible start time if it is rescheduled.
    """

    scheduled_start_datetime: datetime.datetime | None
