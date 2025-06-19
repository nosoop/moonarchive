#!/usr/bin/python3

import warnings

# backwards compatibility shim for tools using moonarchive
# this may be removed at any version starting from 0.5.0 onwards
from ..downloaders.youtube.dash_manifest import YTDashManifest as YTDashManifest

warnings.warn(
    "Module has been moved to moonarchive.downloaders.youtube.dash_manifest", DeprecationWarning
)
