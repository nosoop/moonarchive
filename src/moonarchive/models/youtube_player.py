#!/usr/bin/python3

import asyncio
import datetime
import enum
import itertools
import urllib.parse
from typing import NamedTuple, Optional

import httpx

from .dash_manifest import YTDashManifest
from .youtube import YTJSONStruct


class YTPlayerMediaType(enum.StrEnum):
    VIDEO = "video"
    AUDIO = "audio"
    TEXT = "text"  # TODO: code raised on this in a premiere but I didn't get the exact type

    @staticmethod
    def from_str(fmtstr: str) -> "YTPlayerMediaType":
        if fmtstr in ("video",):
            return YTPlayerMediaType.VIDEO
        elif fmtstr in ("audio",):
            return YTPlayerMediaType.AUDIO
        elif fmtstr in ("text",):
            return YTPlayerMediaType.TEXT
        raise NotImplementedError(f"Unknown media type {fmtstr}")


class YTPlayerAdaptiveFormatType(NamedTuple):
    type: YTPlayerMediaType
    subtype: str
    codec: str | None = None

    @property
    def codec_primary(self) -> str | None:
        # returns the codec primary component
        # (all avc1 / mp4a profiles with trailing options removed)
        if self.codec is None:
            return None
        fourcc, *_ = self.codec.partition(".")
        return fourcc


class YTPlayerMainAppWebResponseContext(YTJSONStruct):
    logged_out: bool


class YTPlayerResponseContext(YTJSONStruct):
    main_app_web_response_context: YTPlayerMainAppWebResponseContext | None = None


class YTPlayerLiveStreamabilityOfflineSlateRenderer(YTJSONStruct):
    scheduled_start_time: str | None = None

    @property
    def scheduled_start_datetime(self) -> datetime.datetime | None:
        if not self.scheduled_start_time:
            return None
        return datetime.datetime.fromtimestamp(int(self.scheduled_start_time), tz=datetime.UTC)


class YTPlayerLiveStreamabilityOfflineSlate(YTJSONStruct):
    live_stream_offline_slate_renderer: YTPlayerLiveStreamabilityOfflineSlateRenderer

    def __getattr__(self, name: str):
        # proxy attribute accesses to the renderer
        return getattr(self.live_stream_offline_slate_renderer, name)


class YTPlayerLiveStreamabilityRenderer(YTJSONStruct):
    video_id: str
    broadcast_id: str | None = None
    display_endscreen: bool | None = False

    # only if status = LIVE_STREAM_OFFLINE
    offline_slate: YTPlayerLiveStreamabilityOfflineSlate | None = None


class YTPlayerLiveStreamability(YTJSONStruct):
    live_streamability_renderer: YTPlayerLiveStreamabilityRenderer

    def __getattr__(self, name: str):
        # proxy attribute accesses to the renderer
        return getattr(self.live_streamability_renderer, name)


class YTPlayerPlayabilityStatus(YTJSONStruct):
    status: str
    playable_in_embed: bool = False
    reason: Optional[str] = None

    # may be present when status is LIVE_STREAM_OFFLINE
    live_streamability: YTPlayerLiveStreamability | None = None


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

    @property
    def resolution(self) -> int | None:
        """
        Returns a video stream's minimum of its width and height.
        This should approximately line up with the friendly resolution name (e.g. 1080p, 720p).
        """
        if self.width and self.height:
            return min(self.width, self.height)
        return None


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

    async def get_dash_manifest(self) -> YTDashManifest | None:
        if not self.dash_manifest_url:
            return None
        async with httpx.AsyncClient() as client:
            for _ in range(6):
                try:
                    resp = await client.get(self.dash_manifest_url, timeout=5)
                    return YTDashManifest.from_manifest_text(resp.text)
                except httpx.RequestError:
                    await asyncio.sleep(5.0)
        return None


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
    def num_length_seconds(self) -> int:
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


class YTPlayerHeartbeatParams(YTJSONStruct):
    # passed in whenever a heartbeat request is made
    heartbeat_token: str | None = None


class YTPlayerHeartbeatResponse(YTJSONStruct):
    playability_status: YTPlayerPlayabilityStatus
    stop_heartbeat: bool | None = False


class YTPlayerResponse(YTJSONStruct):
    playability_status: YTPlayerPlayabilityStatus
    response_context: YTPlayerResponseContext | None = None

    # not present on streams that were made private
    video_details: Optional[YTPlayerVideoDetails] = None
    microformat: Optional[YTPlayerMicroformat] = None

    # this is not present on streams happening in the future
    streaming_data: Optional[YTPlayerStreamingData] = None

    # present on membership-only streams
    heartbeat_params: YTPlayerHeartbeatParams | None = None

    stop_heartbeat: bool | None = False
