#!/usr/bin/python3

# table to remove illegal characters on Windows
# we use this to match ytarchive file output behavior
sanitize_table = str.maketrans({c: "_" for c in r'<>:"/\|?*'})


def _string_byte_trim(input: str, length: int) -> str:
    """
    Trims a string using a byte limit, while ensuring that it is still valid Unicode.
    https://stackoverflow.com/a/70304695
    """
    bytes_ = input.encode()
    try:
        return bytes_[:length].decode()
    except UnicodeDecodeError as err:
        return bytes_[: err.start].decode()
