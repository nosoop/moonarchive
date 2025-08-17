#!/usr/bin/python3

import html.parser
import json
from typing import Type


def create_json_object_extractor(decl: str) -> Type[html.parser.HTMLParser]:
    class InternalHTMLParser(html.parser.HTMLParser):
        in_script: bool = False
        result = None

        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            self.in_script = tag == "script"

        def handle_endtag(self, tag: str) -> None:
            self.in_script = False

        def handle_data(self, data: str) -> None:
            if not self.in_script:
                return

            decl_pos = data.find(decl)
            if decl_pos == -1:
                return

            # we'll just let the decoder throw to determine where the data ends
            start_pos = data[decl_pos:].find("{") + decl_pos
            try:
                self.result = json.loads(data[start_pos:])
            except json.JSONDecodeError as e:
                self.result = json.loads(data[start_pos : start_pos + e.pos])

    return InternalHTMLParser


PlayerResponseExtractor = create_json_object_extractor("var ytInitialPlayerResponse =")
YTCFGExtractor = create_json_object_extractor('ytcfg.set({"CLIENT')
