#!/usr/bin/python3

import warnings

# backwards compatibility shim for tools using moonarchive
# this may be removed at any version starting from 0.5.0 onwards
from ..downloaders.youtube.config import YTCFG as YTCFG

warnings.warn(
    "Module has been moved to moonarchive.downloaders.youtube.config", DeprecationWarning
)
