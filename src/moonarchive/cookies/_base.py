#!/usr/bin/python3

import abc
from http.cookiejar import CookieJar


class BaseCookieSource(abc.ABC):
    @abc.abstractmethod
    async def get_cookies(self) -> CookieJar:
        """
        Returns cookies from the source in a `CookieJar`.
        """
        pass
