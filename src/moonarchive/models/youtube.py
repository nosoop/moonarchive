#!/usr/bin/python3

import msgspec


class YTJSONStruct(msgspec.Struct, rename="camel"):
    # class to handle renaming fields to match those present in the struct
    pass
