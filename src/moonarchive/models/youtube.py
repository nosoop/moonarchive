#!/usr/bin/python3

import warnings

# backwards compatibility shim for tools using moonarchive
# this may be removed at any version starting from 0.5.0 onwards
from ..downloaders.youtube.model import YTJSONStruct as YTJSONStruct

warnings.warn(
    "Module has been moved to moonarchive.downloaders.youtube.model", DeprecationWarning
)
