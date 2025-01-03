#!/usr/bin/python3

import string
import xml.etree.ElementTree as ElementTree
from typing import Self

import msgspec


class YTDashManifest(msgspec.Struct):
    # container for XML MPEG-DASH information

    start_number: int = 0
    format_urls: dict[int, string.Template] = msgspec.field(default_factory=dict)

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

            if base_url_elem is None:
                continue

            assert base_url_elem.text
            url_template = string.Template(base_url_elem.text + "sq/${sequence}")

            try:
                int(itag)  # type: ignore[arg-type]
            except Exception:
                continue

            if itag and url_template:
                manifest.format_urls[int(itag)] = url_template

        return manifest
