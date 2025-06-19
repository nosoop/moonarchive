#!/usr/bin/python3

import collections
import functools

import colorama.ansi
import msgspec

from .models import messages as msgtypes
from .models.youtube_player import YTPlayerMediaType


class BaseMessageHandler(msgspec.Struct):
    async def handle_message(self, msg: msgtypes.BaseMessage) -> None:
        raise NotImplementedError()


class JSONLMessageHandler(BaseMessageHandler, tag="jsonl"):
    # outputs messages as newline-delimited JSON
    # this is intended for applications that read this tool's standard output
    async def handle_message(self, msg: msgtypes.BaseMessage) -> None:
        print(msgspec.json.encode(msg).decode("utf8"))


def _sizeof_fmt(num: int | float, suffix: str = "B") -> str:
    # https://stackoverflow.com/a/1094933
    for unit in ("", "Ki", "Mi", "Gi", "Ti", "Pi", "Ei", "Zi"):
        if abs(num) < 1024.0:
            return f"{num:3.2f}{unit}{suffix}"
        num /= 1024.0
    return f"{num:.2f}Yi{suffix}"


class YTArchiveMessageHandler(BaseMessageHandler, tag="ytarchive"):
    # outputs a ytarchive-style message

    class BroadcastState(msgspec.Struct):
        video_seq: int = 0
        audio_seq: int = 0
        max_seq: int = 0
        total_downloaded: int = 0

        @property
        def human_total_size(self) -> str:
            return _sizeof_fmt(self.total_downloaded)

    current_manifest: str = ""
    last_stream_info: msgtypes.StreamInfoMessage | None = None
    broadcasts: dict[str, BroadcastState] = msgspec.field(
        default_factory=functools.partial(collections.defaultdict, BroadcastState)
    )

    def print_frag_status_update(self) -> None:
        # sequence numbers are offset by one to match ytarchive output
        broadcast = self.broadcasts[self.current_manifest]
        print(
            f"\r{colorama.ansi.clear_line()}"
            f"Video Fragments: {broadcast.video_seq + 1}; "
            f"Audio Fragments: {broadcast.audio_seq + 1}; "
            f"Max Fragments: {broadcast.max_seq + 1}; "
            f"Total Downloaded: {broadcast.human_total_size}; "
            f"Manifest: {self.current_manifest}",
            end="",
            flush=True,
        )

    async def handle_message(self, msg: msgtypes.BaseMessage) -> None:
        match msg:
            case msgtypes.StringMessage():
                print(msg.text)
            case msgtypes.FragmentMessage():
                broadcast = self.broadcasts[msg.manifest_id]
                broadcast.max_seq = max(broadcast.max_seq, msg.max_fragments)
                if msg.media_type == "audio":
                    broadcast.audio_seq = msg.current_fragment
                elif msg.media_type == "video":
                    broadcast.video_seq = msg.current_fragment

                # this size matches ytarchive@1790a76 (0.4.0)
                # it probably diverges at ytarchive@3fb0ba0, which we currently don't implement
                # (both ffmpeg 5.0 and 7.0 have no fatal issues with such fmp4 files)
                broadcast.total_downloaded += msg.fragment_size
                self.current_manifest = msg.manifest_id
                self.print_frag_status_update()
            case msgtypes.DownloadStreamJobEndedMessage():
                print()
                print(f"Download job finished for type {msg.media_type}")
            case msgtypes.StreamInfoMessage():
                if not self.last_stream_info:
                    print(f"Channel: {msg.channel_name}")
                    print(f"Video Title: {msg.video_title}")
                if (
                    not self.last_stream_info
                    or self.last_stream_info.start_datetime != msg.start_datetime
                ):
                    print(f"Stream starts at {msg.start_datetime}")
                self.last_stream_info = msg
            case msgtypes.FormatSelectionMessage():
                major_type_str = str(msg.major_type).capitalize()
                display_media_type = msg.format.media_type.codec_primary or "unknown codec"
                if msg.major_type == YTPlayerMediaType.VIDEO:
                    if display_media_type.startswith("avc1"):
                        display_media_type = "h264"
                    print(
                        f"{major_type_str} format: {msg.format.quality_label} "
                        f"{display_media_type} (itag {msg.format.itag}, manifest "
                        f"{msg.manifest_id}, duration {msg.format.target_duration_sec})"
                    )
                elif msg.format.bitrate:
                    print(
                        f"{major_type_str} format: {msg.format.bitrate // 1000}k "
                        f"{display_media_type} (itag {msg.format.itag}, manifest "
                        f"{msg.manifest_id}, duration {msg.format.target_duration_sec})"
                    )
                else:
                    print(
                        f"{major_type_str} format selected (manifest "
                        f"{msg.manifest_id}, duration {msg.format.target_duration_sec})"
                    )
            case msgtypes.ExtractingPlayerResponseMessage():
                print(
                    f"Extracting player response for itag {msg.itag}; segment error {msg.http_error_code}"
                )
            case msgtypes.StreamUnavailableMessage():
                print(f"{msg.status}: {msg.reason}")
            case msgtypes.DownloadJobFailedOutputMoveMessage():
                print("Failed to move output files to desired destination:")
                for dest, src in msg.path_mapping.items():
                    print(f"- '{dest}' (from '{src}')")
            case _:
                pass


CLIMessageHandlers = JSONLMessageHandler | YTArchiveMessageHandler
