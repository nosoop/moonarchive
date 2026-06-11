#!/usr/bin/python3

from typing import Mapping


def update_qsl(
    qsl: list[tuple[str, str | int | float | bool | None]], new_values: Mapping[str, str]
) -> list[tuple[str, str | int | float | bool | None]]:
    """
    Returns a copy of the query string key/value pairs with changes and additions.
    """
    new_values_copy = dict(new_values)
    q_new = []
    for name, value in qsl:
        if name in new_values_copy:
            value = new_values_copy.pop(name)
        q_new.append((name, value))
    for name, value in new_values_copy.items():
        q_new.append((name, value))
    return q_new
