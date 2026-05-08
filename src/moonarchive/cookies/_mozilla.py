#!/usr/bin/python3

"""
Code for async extraction of cookies on Firefox and derived browsers.

Make sure to occasionally review `CookiePersistentStorage::TryInitDB` to account for any
migrations:
https://github.com/mozilla-firefox/firefox/blob/main/netwerk/cookie/CookiePersistentStorage.cpp
"""

import dataclasses
import functools
import http.cookiejar
import pathlib
import sqlite3
import typing
import urllib.parse
from typing import Self

import aiosqlite
import msgspec

from ._base import BaseCookieSource


class MozillaContainerIdentities(msgspec.Struct, rename="camel"):
    """
    Representation of a container identity in Firefox's container file.
    """

    user_context_id: int
    name: str | None = None
    public: bool = True
    """
    Whether or not the identity is visible.  Some identities are marked as internal, which
    should be ignored.
    """


class MozillaContainerFile(msgspec.Struct, rename="camel"):
    """
    Representation of `containers.json`, Firefox's per-profile account container definitions.
    """

    version: int
    last_user_context_id: int
    identities: list[MozillaContainerIdentities] = msgspec.field(default_factory=list)


class MozillaBrowserCookieRow(msgspec.Struct, rename="camel", dict=True):
    """
    Represents a row and relevant subset of columns in Firefox's cookie database.
    """

    name: str
    value: str
    host: str
    path: str
    expiry: float | None
    """Cookie expiry time as a number of seconds since epoch."""

    is_secure: bool
    is_http_only: bool
    origin_attributes: str = ""
    """A urlencoded string of browser-specific attributes for the cookie"""

    def to_cookie(self) -> http.cookiejar.Cookie:
        return http.cookiejar.Cookie(
            version=0,
            name=self.name,
            value=self.value,
            port=None,
            port_specified=False,
            domain=self.host,
            domain_specified=bool(self.host),
            domain_initial_dot=self.host.startswith("."),
            path=self.path,
            path_specified=True,
            secure=bool(self.is_secure),
            expires=int(self.expiry) if self.expiry is not None else None,
            discard=False,
            comment=None,
            comment_url=None,
            rest={"HTTPOnly": ""} if self.is_http_only else {},
        )

    @functools.cached_property
    def origin_attribute(self) -> dict[str, str]:
        return {
            field: value for field, value in urllib.parse.parse_qsl(self.origin_attributes[1:])
        }

    @classmethod
    def factory(cls, cursor: sqlite3.Cursor, row: tuple) -> Self:
        """
        A sqlite3 row factory to return structured cookie instances.
        """
        return msgspec.convert(sqlite3.Row(cursor, row), cls, strict=False)

    @classmethod
    def get_query(cls) -> str:
        """
        Returns the query string used to query cookies.  Subclasses may return a different query
        string as the schema evolves.

        Note that the selected row names must line up with the fields in
        MozillaBrowserCookieRow.
        """
        return (
            "SELECT name, value, host, path, expiry, isSecure, isHttpOnly, originAttributes "
            "FROM moz_cookies"
        )


class MozillaBrowserCookieRowV16(MozillaBrowserCookieRow):
    @classmethod
    def get_query(cls) -> str:
        """
        Returns the query string used to query cookies.
        For schema v16 and newer, the expiry is specified as milliseconds since epoch; convert
        it to seconds.
        """
        return (
            "SELECT name, value, host, path, expiry / 1000 AS expiry, "
            "isSecure, isHttpOnly, originAttributes FROM moz_cookies"
        )


@dataclasses.dataclass(frozen=True)
class MozillaBrowserCookieSource(BaseCookieSource):
    """
    A class that creates a `CookieJar` from a Firefox-based browser.
    """

    cookie_db: pathlib.Path
    """Cookie database (commonly a `cookies.sqlite` file in the profile directory)."""

    container: str | None = None
    """A container name to use.  Specify `None` to use the default container."""

    def __post_init__(self):
        # attempt to generate the context ID so we can throw if it fails
        self.container_context_id

    async def get_cookies(self) -> http.cookiejar.CookieJar:
        async with aiosqlite.connect(f"file:/{self.cookie_db}?mode=ro", uri=True) as db:
            async with db.execute("PRAGMA user_version;") as cursor:
                version, *_ = await cursor.fetchone() or (0,)

            schema_class: type[MozillaBrowserCookieRow] = MozillaBrowserCookieRow
            if version >= 16:
                schema_class = MozillaBrowserCookieRowV16

            db.row_factory = schema_class.factory  # type: ignore

            async with db.execute(schema_class.get_query()) as cursor:
                jar = http.cookiejar.CookieJar()
                async for row in cursor:
                    cookie_row = typing.cast(MozillaBrowserCookieRow, row)
                    context_id = cookie_row.origin_attribute.get("userContextId")
                    if (
                        context_id is not None
                        if self.container_context_id is None
                        else context_id != str(self.container_context_id)
                    ):
                        continue
                    jar.set_cookie(cookie_row.to_cookie())
                return jar

    @functools.cached_property
    def container_context_id(self) -> int | None:
        """
        Returns the context ID associated with the given container name, or None if no container
        name was specified.  Raises an error if a container with the specified name could not be
        found.
        """
        if self.container is None:
            return None
        container_info = msgspec.json.decode(
            (self.cookie_db.parent / "containers.json").read_bytes(), type=MozillaContainerFile
        )
        result = next(
            (
                identity.user_context_id
                for identity in container_info.identities
                if identity.name == self.container and identity.public
            ),
            None,
        )
        if result is not None:
            return result

        identities = {
            identity.name for identity in container_info.identities if identity.public
        }
        raise ValueError(
            f"Failed to find context ID for container '{self.container}' "
            f"(expected one of {identities}, or no container specified)"
        )
