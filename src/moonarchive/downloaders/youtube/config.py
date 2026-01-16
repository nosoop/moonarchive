#!/usr/bin/python3

import urllib.parse

import msgspec

from .model import YTJSONStruct


class YTWebPlayerContextConfig(YTJSONStruct, kw_only=True):
    serialized_experiment_flags: str | None = None

    @property
    def experiment_flags(self) -> dict[str, str]:
        return (
            {
                k: v[0]
                for k, v in urllib.parse.parse_qs(self.serialized_experiment_flags).items()
            }
            if self.serialized_experiment_flags
            else {}
        )


class YTCFG(YTJSONStruct, kw_only=True):
    delegated_session_id: str | None = msgspec.field(name="DELEGATED_SESSION_ID", default=None)
    id_token: str | None = msgspec.field(name="ID_TOKEN", default=None)
    hl: str = msgspec.field(name="HL")
    innertube_api_key: str = msgspec.field(name="INNERTUBE_API_KEY")
    innertube_client_name: str = msgspec.field(name="INNERTUBE_CLIENT_NAME")
    innertube_client_version: str = msgspec.field(name="INNERTUBE_CLIENT_VERSION")
    innertube_ctx_client_name: int = msgspec.field(name="INNERTUBE_CONTEXT_CLIENT_NAME")
    innertube_ctx_client_version: str = msgspec.field(name="INNERTUBE_CONTEXT_CLIENT_VERSION")
    session_index: str | None = msgspec.field(name="SESSION_INDEX", default=None)
    visitor_data: str = msgspec.field(name="VISITOR_DATA")
    user_session_id: str | None = msgspec.field(name="USER_SESSION_ID", default=None)
    player_js_url: str = msgspec.field(name="PLAYER_JS_URL")

    # DATASYNC_ID appears to be f"{DELEGATED_SESSION_ID}||{USER_SESSION_ID}"

    web_player_context_configs: dict[str, YTWebPlayerContextConfig] = msgspec.field(
        name="WEB_PLAYER_CONTEXT_CONFIGS", default_factory=dict
    )

    def to_headers(self) -> dict[str, str]:
        headers = {
            "X-YouTube-Client-Name": str(self.innertube_ctx_client_name),
            "X-YouTube-Client-Version": self.innertube_client_version,
        }
        if self.visitor_data:
            headers["X-Goog-Visitor-Id"] = self.visitor_data
        if self.session_index:
            headers["X-Goog-AuthUser"] = self.session_index
        if self.delegated_session_id:
            headers["X-Goog-PageId"] = self.delegated_session_id
        if self.id_token:
            headers["X-Youtube-Identity-Token"] = self.id_token
        return headers

    def to_post_context(self) -> dict[str, str]:
        post_context = {
            "clientName": self.innertube_client_name,
            "clientVersion": self.innertube_client_version,
        }
        if self.visitor_data:
            post_context["visitorData"] = self.visitor_data
        return post_context
