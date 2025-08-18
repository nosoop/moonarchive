#!/usr/bin/python3

import xml.etree.ElementTree as ElementTree
from typing import Self

import msgspec


class YTDashManifest(msgspec.Struct):
    # container for XML MPEG-DASH information

    start_number: int = 0
    format_urls: dict[int, str] = msgspec.field(default_factory=dict)

    @classmethod
    def from_manifest_text(cls, text: str) -> Self | None:
        # convert manifest XML string to instance of class
        try:
            root = ElementTree.fromstring(text)
        except ElementTree.ParseError:
            return None

        segment_list = root.find(".//{*}Period/{*}SegmentList")
        if segment_list is None:
            return None

        manifest = cls()
        manifest.start_number = int(segment_list.get("startNumber") or 0)

        reps = root.findall(".//{*}Representation")

        for r in reps:
            itag = r.get("id")
            base_url_elem = r.find("{*}BaseURL")

            if (
                base_url_elem is None
                or base_url_elem.text is None
                or itag is None
                or not itag.isdigit()
            ):
                continue

            # ensure trailing slash for urljoin
            url = base_url_elem.text
            if not url.endswith("/"):
                url += "/"

            manifest.format_urls[int(itag)] = url

        return manifest
