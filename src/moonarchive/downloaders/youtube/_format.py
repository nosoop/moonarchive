#!/usr/bin/python3

import dataclasses
import operator

from .player import YTPlayerAdaptiveFormats, YTPlayerMediaType


@dataclasses.dataclass
class FormatSelector:
    """
    Class to select a YouTube substream for downloading.

    This is invoked to determine an appropriate format for the user, as the availability of
    formats may change between manifests.
    """

    major_type: YTPlayerMediaType
    codec: str | None = None
    max_video_resolution: int | None = None

    def select(self, formats: list[YTPlayerAdaptiveFormats]) -> list[YTPlayerAdaptiveFormats]:
        out_formats = list(filter(lambda x: x.media_type.type == self.major_type, formats))

        sort_key = None
        if self.major_type == YTPlayerMediaType.VIDEO:
            sort_key = operator.attrgetter("width")

            # note that the result of _preferred_codec_sorter is negative since this is reversed
            # at the end of the sort
            if self.codec:

                def _sort(fmt: YTPlayerAdaptiveFormats) -> tuple[int, int]:
                    assert fmt.width is not None
                    return (fmt.width, -FormatSelector._preferred_codec_sorter(self.codec)(fmt))

                # the typing is super clunky and we can't just return comparables, so we simply
                # disregard this for now
                sort_key = _sort  # type: ignore
            if self.max_video_resolution:
                out_formats = list(
                    fmt
                    for fmt in out_formats
                    if (fmt.resolution and fmt.resolution <= self.max_video_resolution)
                )
        else:
            sort_key = operator.attrgetter("bitrate")
        return sorted(out_formats, key=sort_key, reverse=True)

    @staticmethod
    def _preferred_codec_sorter(*codecs: str | None):
        # codecs should be specified with the highest value first
        if not codecs:
            codecs = tuple()

        def _sort(fmt: YTPlayerAdaptiveFormats) -> int:
            try:
                return codecs.index(fmt.media_type.codec_primary)
            except ValueError:
                # return value higher than given tuple
                return len(codecs)

        return _sort
