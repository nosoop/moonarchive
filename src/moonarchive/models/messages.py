#!/usr/bin/python3

import msgspec


class BaseMessage(msgspec.Struct, tag=True):
    pass


class FragmentMessage(BaseMessage, tag="fragment"):
    current_fragment: int
    max_fragments: int
    itag: int
    manifest_id: str
    fragment_size: int


class DownloadJobEndedMessage(BaseMessage, tag="download-job-ended"):
    itag: int
