#!/usr/bin/python3

import msgspec

from .youtube import YTJSONStruct


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

    def to_headers(self) -> dict[str, str]:
        headers = {
            "X-YouTube-Client-Name": str(self.innertube_ctx_client_name),
            "X-YouTube-Client-Version": self.innertube_client_version,
        }
        if self.visitor_data:
            headers["X-Goog-Visitor-Id"] = self.visitor_data
        if self.session_index:
            headers["X-Goog-AuthUser"] = self.visitor_data
        if self.delegated_session_id:
            headers["X-Goog-PageId"] = self.delegated_session_id
        if self.id_token:
            headers["X-Youtube-Identity-Token"] = self.id_token
        return headers

    def to_post_context(self) -> dict[str, str]:
        return {
            "clientName": self.innertube_client_name,
            "clientVersion": self.innertube_client_version,
        }
