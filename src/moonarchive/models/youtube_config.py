#!/usr/bin/python3

import warnings

# compatibility shim for tools using moonarchive
from ..downloaders.youtube.config import YTCFG as YTCFG

warnings.warn(
    "Module has been moved to moonarchive.downloaders.youtube.config", DeprecationWarning
)
