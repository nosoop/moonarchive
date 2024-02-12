#!/usr/bin/python3

import operator
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
    playable_in_embed: bool
    reason: Optional[str] = None


class YTPlayerAdaptiveFormats(YTJSONStruct):
    itag: int
    url: str
    mime_type: str
    target_duration_sec: float

    # video stream-specific fields
    width: Optional[int] = None
    height: Optional[int] = None
    quality_label: Optional[str] = None


class YTPlayerStreamingData(YTJSONStruct):
    expires_in_seconds: str
    adaptive_formats: list[YTPlayerAdaptiveFormats]
    dash_manifest_url: str

    @property
    def sorted_video_formats(self) -> list[YTPlayerAdaptiveFormats]:
        return sorted(
            (fmt for fmt in self.adaptive_formats if fmt.height),
            key=operator.attrgetter("width"),
            reverse=True,
        )

    def get_dash_manifest(self) -> YTDashManifest:
        with urllib.request.urlopen(self.dash_manifest_url) as resp:
            return YTDashManifest.from_manifest_text(resp.read().decode("utf8"))


class YTPlayerVideoDetails(YTJSONStruct):
    video_id: str
    title: str
    length_seconds: str
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
    start_timestamp: str

    @property
    def start_datetime(self):
        return datetime.datetime.fromisoformat(self.start_timestamp)


class YTPlayerMicroformatRenderer(YTJSONStruct):
    thumbnail: YTPlayerMicroformatRendererThumbnails
    live_broadcast_details: YTPlayerMicroformatRendererBroadcastDetails
    publish_date: str
    upload_date: str


class YTPlayerMicroformat(YTJSONStruct):
    player_microformat_renderer: YTPlayerMicroformatRenderer


class YTPlayerResponse(YTJSONStruct):
    response_context: YTPlayerResponseContext
    playability_status: YTPlayerPlayabilityStatus
    video_details: YTPlayerVideoDetails
    microformat: YTPlayerMicroformat
    streaming_data: Optional[YTPlayerStreamingData] = None
