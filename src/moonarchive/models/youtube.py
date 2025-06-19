#!/usr/bin/python3

import warnings

# compatibility shim for tools using moonarchive
from ..downloaders.youtube.model import YTJSONStruct as YTJSONStruct

warnings.warn(
    "Module has been moved to moonarchive.downloaders.youtube.model", DeprecationWarning
)
