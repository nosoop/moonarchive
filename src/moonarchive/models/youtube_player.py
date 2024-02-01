#!/usr/bin/python3

from typing import Optional

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

    # audio streams do not have a quality label specified
    quality_label: Optional[str] = None


class YTPlayerStreamingData(YTJSONStruct):
    expires_in_seconds: str
    adaptive_formats: list[YTPlayerAdaptiveFormats]
    dash_manifest_url: str


class YTPlayerVideoDetails(YTJSONStruct):
    video_id: str
    title: str
    length_seconds: str
    is_live: bool
    channel_id: str
    is_owner_viewing: bool
    short_description: str
    is_live_content: bool


class YTPlayerThumbnail(YTJSONStruct):
    url: str
    width: int
    height: int


class YTPlayerMicroformatRendererThumbnails(YTJSONStruct):
    thumbnails: list[YTPlayerThumbnail]


class YTPlayerMicroformatRendererBroadcastDetails(YTJSONStruct):
    is_live_now: bool
    start_timestamp: str


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
    streaming_data: YTPlayerStreamingData
    video_details: YTPlayerVideoDetails
    microformat: YTPlayerMicroformat
