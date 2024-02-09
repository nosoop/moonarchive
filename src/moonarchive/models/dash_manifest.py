#!/usr/bin/python3

import string
import xml.etree.ElementTree as ElementTree

import msgspec


class YTDashManifest(msgspec.Struct):
    # container for XML MPEG-DASH information

    start_number: int = 0
    format_urls: dict[int, string.Template] = msgspec.field(default_factory=dict)

    @classmethod
    def from_manifest_text(cls, text: str):
        # convert manifest XML string to instance of class
        root = ElementTree.fromstring(text)

        manifest = cls()
        manifest.start_number = int(root.find(".//{*}Period/{*}SegmentList").get("startNumber"))

        reps = root.findall(".//{*}Representation")

        for r in reps:
            itag = r.get("id")
            url_template = string.Template(r.find("{*}BaseURL").text + "sq/${sequence}")

            try:
                int(itag)
            except Exception:
                continue

            if itag and url_template:
                manifest.format_urls[int(itag)] = url_template

        return manifest
