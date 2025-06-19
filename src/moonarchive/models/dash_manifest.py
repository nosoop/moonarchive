#!/usr/bin/python3

import warnings

# compatibility shim for tools using moonarchive
from ..downloaders.youtube.dash_manifest import YTDashManifest as YTDashManifest

warnings.warn(
    "Module has been moved to moonarchive.downloaders.youtube.dash_manifest", DeprecationWarning
)
