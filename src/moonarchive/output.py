#!/usr/bin/python3

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
    video_seq: int = 0
    audio_seq: int = 0
    max_seq: int = 0
    total_downloaded: int = 0
    current_manifest: str = ""

    def print_frag_status_update(self) -> None:
        # sequence numbers are offset by one to match ytarchive output
        print(
            f"\r{colorama.ansi.clear_line()}"
            f"Video Fragments: {self.video_seq + 1}; "
            f"Audio Fragments: {self.audio_seq + 1}; "
            f"Max Fragments: {self.max_seq + 1}; "
            f"Total Downloaded: {self.human_total_size}; "
            f"Manifest: {self.current_manifest}",
            end="",
            flush=True,
        )

    async def handle_message(self, msg: msgtypes.BaseMessage) -> None:
        match msg:
            case msg if isinstance(msg, msgtypes.StringMessage):
                print(msg.text)
            case msg if isinstance(msg, msgtypes.FragmentMessage):
                self.max_seq = max(self.max_seq, msg.max_fragments)
                if msg.media_type == "audio":
                    self.audio_seq = msg.current_fragment
                elif msg.media_type == "video":
                    self.video_seq = msg.current_fragment

                # this size matches ytarchive@1790a76 (0.4.0)
                # it probably diverges at ytarchive@3fb0ba0, which we currently don't implement
                # (both ffmpeg 5.0 and 7.0 have no fatal issues with such fmp4 files)
                self.total_downloaded += msg.fragment_size
                if self.current_manifest != msg.manifest_id:
                    self.max_seq = 0
                self.current_manifest = msg.manifest_id
                self.print_frag_status_update()
            case msg if isinstance(msg, msgtypes.DownloadStreamJobEndedMessage):
                print()
                print(f"Download job finished for type {msg.media_type}")
            case msg if isinstance(msg, msgtypes.StreamInfoMessage):
                print(f"Channel: {msg.channel_name}")
                print(f"Video Title: {msg.video_title}")
                print(f"Stream starts at {msg.start_datetime}")
            case msg if isinstance(msg, msgtypes.FormatSelectionMessage):
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
            case msg if isinstance(msg, msgtypes.ExtractingPlayerResponseMessage):
                print(
                    f"Extracting player response for itag {msg.itag}; segment error {msg.http_error_code}"
                )
            case msg if isinstance(msg, msgtypes.StreamUnavailableMessage):
                print(f"{msg.status}: {msg.reason}")
            case msg if isinstance(msg, msgtypes.DownloadJobFailedOutputMoveMessage):
                print("Failed to move output files to desired destination:")
                for dest, src in msg.path_mapping.items():
                    print(f"- '{dest}' (from '{src}')")
            case _:
                pass

    @property
    def human_total_size(self) -> str:
        return _sizeof_fmt(self.total_downloaded)
