#!/usr/bin/python3

import colorama.ansi
import msgspec

import moonarchive.models.messages as msgtypes


class BaseMessageHandler(msgspec.Struct):
    def handle_message(self, msg: msgtypes.BaseMessage):
        raise NotImplementedError()


class JSONLMessageHandler(BaseMessageHandler, tag="jsonl"):
    # outputs messages as newline-delimited JSON
    # this is intended for applications that read this tool's standard output
    def handle_message(self, msg: msgtypes.BaseMessage):
        print(msgspec.json.encode(msg).decode("utf8"))


def _sizeof_fmt(num, suffix="B"):
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

    def print_frag_status_update(self):
        print(
            f"\r{colorama.ansi.clear_line()}Video Fragments: {self.video_seq + 1}; Audio Fragments: {self.audio_seq + 1}; Max Fragments: {self.max_seq + 1}; Total Downloaded: {self.human_total_size}; Manifest: {self.current_manifest}",
            end="",
            flush=True,
        )

    def handle_message(self, msg: msgtypes.BaseMessage):
        match type(msg):
            case msgtypes.FragmentMessage:
                self.max_seq = max(self.max_seq, msg.max_fragments)
                if msg.itag == 140:
                    self.audio_seq = msg.current_fragment
                else:
                    self.video_seq = msg.current_fragment
                self.total_downloaded += msg.fragment_size
                self.current_manifest = msg.manifest_id
                self.print_frag_status_update()
            case msgtypes.DownloadJobEndedMessage:
                print()
                print(f"Download job finished for format {msg.itag}")
            case msgtypes.StreamInfoMessage:
                print(f"Channel: {msg.channel_name}")
                print(f"Video Title: {msg.video_title}")
            case _:
                pass

    @property
    def human_total_size(self):
        return _sizeof_fmt(self.total_downloaded)
