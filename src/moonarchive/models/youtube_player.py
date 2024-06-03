#!/usr/bin/python3

import datetime
import enum
import itertools
import urllib.parse
import urllib.request
from typing import NamedTuple, Optional

from .dash_manifest import YTDashManifest
from .youtube import YTJSONStruct


class YTPlayerMediaType(enum.StrEnum):
    VIDEO = "video"
    AUDIO = "audio"

    @staticmethod
    def from_str(fmtstr: str) -> "YTPlayerMediaType":
        if fmtstr in ("video",):
            return YTPlayerMediaType.VIDEO
        elif fmtstr in ("audio",):
            return YTPlayerMediaType.AUDIO
        raise NotImplementedError(f"Unknown media type {fmtstr}")


class YTPlayerAdaptiveFormatType(NamedTuple):
    type: YTPlayerMediaType
    subtype: str
    codec: str | None = None


class YTPlayerMainAppWebResponseContext(YTJSONStruct):
    logged_out: bool


class YTPlayerResponseContext(YTJSONStruct):
    main_app_web_response_context: YTPlayerMainAppWebResponseContext


class YTPlayerPlayabilityStatus(YTJSONStruct):
    status: str
    playable_in_embed: bool = False
    reason: Optional[str] = None

    # may be present when status is LIVE_STREAM_OFFLINE
    # live_streamability: Optional[YTPlayerLiveStreamability] = None


class YTPlayerAdaptiveFormats(YTJSONStruct):
    itag: int

    # sample types:
    # video/mp4; codecs="avc1.4d402a" (itag 299)
    # video/webm; codecs="vp9" (itag 303)
    # audio/mp4; codecs="mp4a.40.2" (itag 140)
    mime_type: str

    # this is not present in non-live streams
    target_duration_sec: Optional[float] = None
    url: Optional[str] = None

    # video stream-specific fields
    width: Optional[int] = None
    height: Optional[int] = None
    quality_label: Optional[str] = None

    bitrate: Optional[int] = None

    @property
    def media_type(self) -> YTPlayerAdaptiveFormatType:
        fulltype, _, parameter = self.mime_type.partition(";")

        type, subtype = fulltype.split("/")
        param_key, param_value = parameter.strip().split("=")

        if param_key == "codecs":
            return YTPlayerAdaptiveFormatType(
                YTPlayerMediaType.from_str(type), subtype, param_value.strip('"')
            )
        return YTPlayerAdaptiveFormatType(YTPlayerMediaType.from_str(type), subtype, None)


class YTPlayerStreamingData(YTJSONStruct):
    expires_in_seconds: str
    adaptive_formats: list[YTPlayerAdaptiveFormats]
    dash_manifest_url: Optional[str] = None

    @property
    def dash_manifest_id(self) -> str | None:
        # youtube may create multiple manifests for a stream, see
        # https://github.com/Kethsar/ytarchive/issues/56
        # this causes downloads to stall
        #
        # this value is also present in the manifest itself; retrieve it the same way
        #
        # parameters in the manifest are slash-delimited
        # extract the subpath within 'id'
        if not self.dash_manifest_url:
            return None

        params = urllib.parse.urlparse(self.dash_manifest_url).path.split("/")
        _, id, *_ = itertools.dropwhile(lambda x: x != "id", params)

        # it's possible that this returns something in the form {video_id}.{id}~{unknown}
        # e.g. xOdgbNQ_lsE.1~45623198
        # we should be fine extracting the component preceding the tilde if it's present
        id_base, *_ = id.partition("~")
        return id_base

    def get_dash_manifest(self) -> YTDashManifest | None:
        if not self.dash_manifest_url:
            return None
        with urllib.request.urlopen(self.dash_manifest_url) as resp:
            return YTDashManifest.from_manifest_text(resp.read().decode("utf8"))


class YTPlayerVideoDetails(YTJSONStruct):
    video_id: str
    title: str
    length_seconds: str
    author: str
    channel_id: str
    is_owner_viewing: bool
    short_description: str
    is_live_content: bool
    is_live: bool = False

    @property
    def num_length_seconds(self):
        return int(self.length_seconds)


class YTPlayerThumbnail(YTJSONStruct):
    url: str
    width: int
    height: int


class YTPlayerMicroformatRendererThumbnails(YTJSONStruct):
    thumbnails: list[YTPlayerThumbnail]


class YTPlayerMicroformatRendererBroadcastDetails(YTJSONStruct):
    is_live_now: bool

    # this is not present on unstarted livestreams that have passed their scheduled time
    start_timestamp: Optional[str] = None

    @property
    def start_datetime(self) -> Optional[datetime.datetime]:
        if self.start_timestamp:
            return datetime.datetime.fromisoformat(self.start_timestamp)
        return None


class YTPlayerMicroformatRenderer(YTJSONStruct):
    thumbnail: YTPlayerMicroformatRendererThumbnails
    publish_date: str
    upload_date: str
    live_broadcast_details: Optional[YTPlayerMicroformatRendererBroadcastDetails] = None

    @property
    def thumbnails(self) -> list[YTPlayerThumbnail]:
        return self.thumbnail.thumbnails


class YTPlayerMicroformat(YTJSONStruct):
    player_microformat_renderer: YTPlayerMicroformatRenderer

    def __getattr__(self, name: str):
        # proxy attribute accesses to the renderer
        return getattr(self.player_microformat_renderer, name)


class YTPlayerResponse(YTJSONStruct):
    response_context: YTPlayerResponseContext
    playability_status: YTPlayerPlayabilityStatus

    # not present on streams that were made private
    video_details: Optional[YTPlayerVideoDetails] = None
    microformat: Optional[YTPlayerMicroformat] = None

    # this is not present on streams happening in the future
    streaming_data: Optional[YTPlayerStreamingData] = None
