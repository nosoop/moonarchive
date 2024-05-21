#!/usr/bin/python3

import datetime
import itertools
import operator
import urllib.parse
import urllib.request
from typing import Optional

from .dash_manifest import YTDashManifest
from .youtube import YTJSONStruct


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
    mime_type: str

    # this is not present in non-live streams
    target_duration_sec: Optional[float] = None
    url: Optional[str] = None

    # video stream-specific fields
    width: Optional[int] = None
    height: Optional[int] = None
    quality_label: Optional[str] = None

    bitrate: Optional[int] = None


class YTPlayerStreamingData(YTJSONStruct):
    expires_in_seconds: str
    adaptive_formats: list[YTPlayerAdaptiveFormats]
    dash_manifest_url: Optional[str] = None

    @property
    def sorted_video_formats(self) -> list[YTPlayerAdaptiveFormats]:
        return sorted(
            (fmt for fmt in self.adaptive_formats if fmt.height),
            key=operator.attrgetter("width"),
            reverse=True,
        )

    @property
    def sorted_audio_formats(self) -> list[YTPlayerAdaptiveFormats]:
        return sorted(
            (fmt for fmt in self.adaptive_formats if "audio" in fmt.mime_type),
            key=operator.attrgetter("bitrate"),
            reverse=True,
        )

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

    def __getattr__(self, name):
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
