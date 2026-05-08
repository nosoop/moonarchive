#!/usr/bin/python3

import dataclasses
import http.cookiejar
import pathlib
from http.cookiejar import LWPCookieJar, MozillaCookieJar

from ._base import BaseCookieSource


@dataclasses.dataclass
class CookieTextFileSource(BaseCookieSource):
    """
    A class that creates a cookie jar from a `cookies.txt` file.
    """

    cookie_file: pathlib.Path

    async def get_cookies(self) -> http.cookiejar.CookieJar:
        if not self.cookie_file.is_file():
            raise ValueError(f"Cookie file {self.cookie_file} does not exist")
        jar = MozillaCookieJar()
        jar.load(str(self.cookie_file))
        return jar


@dataclasses.dataclass
class LWPCookieFileSource(BaseCookieSource):
    """
    A class that creates a cookie jar from a libwww-perl compatible file.
    """

    cookie_file: pathlib.Path

    async def get_cookies(self) -> http.cookiejar.CookieJar:
        if not self.cookie_file.is_file():
            raise ValueError(f"Cookie file {self.cookie_file} does not exist")
        jar = LWPCookieJar()
        jar.load(str(self.cookie_file))
        return jar
