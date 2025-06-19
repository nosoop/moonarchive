#!/usr/bin/python3

import warnings

# backwards compatibility shim for tools using moonarchive
# this may be removed at any version starting from 0.5.0 onwards
from ..downloaders.youtube.player import YTPlayerAdaptiveFormats as YTPlayerAdaptiveFormats
from ..downloaders.youtube.player import (
    YTPlayerAdaptiveFormatType as YTPlayerAdaptiveFormatType,
)
from ..downloaders.youtube.player import YTPlayerHeartbeatParams as YTPlayerHeartbeatParams
from ..downloaders.youtube.player import YTPlayerHeartbeatResponse as YTPlayerHeartbeatResponse
from ..downloaders.youtube.player import YTPlayerLiveStreamability as YTPlayerLiveStreamability
from ..downloaders.youtube.player import (
    YTPlayerLiveStreamabilityOfflineSlate as YTPlayerLiveStreamabilityOfflineSlate,
)
from ..downloaders.youtube.player import (
    YTPlayerLiveStreamabilityOfflineSlateRenderer as YTPlayerLiveStreamabilityOfflineSlateRenderer,
)
from ..downloaders.youtube.player import (
    YTPlayerLiveStreamabilityRenderer as YTPlayerLiveStreamabilityRenderer,
)
from ..downloaders.youtube.player import (
    YTPlayerMainAppWebResponseContext as YTPlayerMainAppWebResponseContext,
)
from ..downloaders.youtube.player import YTPlayerMediaType as YTPlayerMediaType
from ..downloaders.youtube.player import YTPlayerMicroformat as YTPlayerMicroformat
from ..downloaders.youtube.player import (
    YTPlayerMicroformatRenderer as YTPlayerMicroformatRenderer,
)
from ..downloaders.youtube.player import (
    YTPlayerMicroformatRendererBroadcastDetails as YTPlayerMicroformatRendererBroadcastDetails,
)
from ..downloaders.youtube.player import (
    YTPlayerMicroformatRendererThumbnails as YTPlayerMicroformatRendererThumbnails,
)
from ..downloaders.youtube.player import YTPlayerPlayabilityStatus as YTPlayerPlayabilityStatus
from ..downloaders.youtube.player import YTPlayerResponse as YTPlayerResponse
from ..downloaders.youtube.player import YTPlayerResponseContext as YTPlayerResponseContext
from ..downloaders.youtube.player import YTPlayerStreamingData as YTPlayerStreamingData
from ..downloaders.youtube.player import YTPlayerThumbnail as YTPlayerThumbnail
from ..downloaders.youtube.player import YTPlayerVideoDetails as YTPlayerVideoDetails

warnings.warn(
    "Module has been moved to moonarchive.downloaders.youtube.player", DeprecationWarning
)
