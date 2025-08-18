#!/usr/bin/python3

import dataclasses
import datetime
import pathlib
import string
from typing import ClassVar

# table to remove illegal characters on Windows
# we use this to match ytarchive file output behavior
sanitize_table = str.maketrans({c: "_" for c in r'<>:"/\|?*'})


def _string_byte_trim(input: str, length: int) -> str:
    """
    Trims a string using a byte limit, while ensuring that it is still valid Unicode.
    https://stackoverflow.com/a/70304695
    """
    bytes_ = input.encode()
    try:
        return bytes_[:length].decode()
    except UnicodeDecodeError as err:
        return bytes_[: err.start].decode()


@dataclasses.dataclass(kw_only=True)
class OutputPathTemplateVars:
    """
    Definition of available values for use in output path templates.
    Note that values MUST be sanitized before template usage.
    """

    title: str = ""
    """ Stream title. """

    id: str = "xxxxxxxxxxx"
    """
    Video / broadcast ID.
    Broadcast IDs are the video ID plus a ".NN" broadcast number, used to disambiguate outputs
    for multi-broadcast livestreams.  This should only be used for filenames.
    """

    video_id: str
    """
    Video ID only.  This may be used as part of any intermediate directory names.
    """

    channel_id: str
    channel: str
    start_date: str = dataclasses.field(init=False)
    start_time: str = dataclasses.field(init=False)
    year: str = dataclasses.field(init=False)
    month: str = dataclasses.field(init=False)
    day: str = dataclasses.field(init=False)
    hours: str = dataclasses.field(init=False)
    minutes: str = dataclasses.field(init=False)
    seconds: str = dataclasses.field(init=False)
    _start_datetime: datetime.datetime = dataclasses.field(
        default_factory=datetime.datetime.now
    )

    def __post_init__(self):
        self.start_datetime = self._start_datetime

    @property
    def start_datetime(self) -> datetime.datetime:
        return self._start_datetime

    @start_datetime.setter
    def start_datetime(self, value: datetime.datetime) -> None:
        self._start_datetime = value
        (
            self.start_date,
            self.start_time,
            self.year,
            self.month,
            self.day,
            self.hours,
            self.minutes,
            self.seconds,
        ) = (
            self._start_datetime.strftime(df)
            for df in ("%Y%m%d", "%H%M%S", "%Y", "%m", "%d", "%H", "%M", "%S")
        )


class OutputPathTemplate(string.Template):
    """
    Class that adds functionality to format a path using a template string and known keys.
    """

    def to_path(self, outvars: OutputPathTemplateVars, /, suffix: str, **kwds) -> pathlib.Path:
        return pathlib.Path(self.substitute(dataclasses.asdict(outvars), **kwds) + suffix)

    def get_max_title_byte_length(self, outvars: OutputPathTemplateVars) -> int:
        """
        Calculates the maximum allowed title byte length for a filename, using a simulated large
        broadcast ID and the file extension used by the application.
        """
        # https://en.wikipedia.org/wiki/Comparison_of_file_systems#Limits
        # modern filesystems commonly have a maximum filename length of 255 bytes with higher limits on paths
        # our default filename convention is also constrained by the following:
        # - 12 bytes for video ID and separator '-'
        # - 3 bytes for multi-broadcast stream extension '.00'; assume possibility of two-digit broadcast IDs
        # - 12 bytes for longest possible file extension '.description'
        #
        # given this, 228 bytes seems like the absolute hard limit, but have a margin of error
        #
        # however upon further testing, it's observed that some applications have even shorter
        # filename (basename?) length constraints like 240, so the revised hard limit is 213
        test_path = self.to_path(outvars, title="", id="xxxxxxxxxxx.00", suffix=".description")
        return 236 - len(test_path.name.encode())


class OutputPathTemplateCompat(OutputPathTemplate):
    """
    String template using the interpolation pattern style used by yt-dlp (and in turn,
    ytarchive).  This only supports named values in the form %(value)s.
    This is implemented primarily for CLI compatibility purposes.
    """

    pattern: ClassVar = r"\%(?:\((?P<named>[_a-z][_a-z0-9]*)\)|(?P<invalid>))s"


_DEFAULT_OUTPUT_FORMAT = OutputPathTemplate("${title}-${id}")
