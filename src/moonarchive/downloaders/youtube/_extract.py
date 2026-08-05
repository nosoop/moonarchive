#!/usr/bin/python3

import contextlib
import html.parser
import json
import re
from typing import Type

INITIAL_ATTESTATION_PATTERN = re.compile("""window\.ytAtN\(\s*(?P<at_n>{[\s\S]*?})\s*\)""")


class JSONObjectExtractor(html.parser.HTMLParser):
    result: dict | None


def create_json_object_extractor(decl: str) -> Type[JSONObjectExtractor]:
    class InternalHTMLParser(JSONObjectExtractor):
        in_script: bool = False

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


# copied wholesale from yt_dlp/utils/_utils.py
# TODO move this somewhere else
def _js_to_json(code: str, vars: dict = {}, *, strict: bool = False) -> str:
    # vars is a dict of var, val pairs to substitute
    STRING_QUOTES = "'\"`"
    STRING_RE = "|".join(rf"{q}(?:\\.|[^\\{q}])*{q}" for q in STRING_QUOTES)
    COMMENT_RE = r"/\*(?:(?!\*/).)*?\*/|//[^\n]*\n"
    SKIP_RE = rf"\s*(?:{COMMENT_RE})?\s*"
    INTEGER_TABLE = (
        (rf"(?s)^(0[xX][0-9a-fA-F]+){SKIP_RE}:?$", 16),
        (rf"(?s)^(0+[0-7]+){SKIP_RE}:?$", 8),
    )

    def process_escape(match: re.Match) -> str:
        JSON_PASSTHROUGH_ESCAPES = R'"\bfnrtu'
        escape = match.group(1) or match.group(2)

        return (
            Rf"\{escape}"
            if escape in JSON_PASSTHROUGH_ESCAPES
            else R"\u00"
            if escape == "x"
            else ""
            if escape == "\n"
            else escape
        )

    def template_substitute(match: re.Match) -> str:
        evaluated = _js_to_json(match.group(1), vars, strict=strict)
        if evaluated[0] == '"':
            with contextlib.suppress(json.JSONDecodeError):
                return json.loads(evaluated)
        return evaluated

    def fix_kv(m: re.Match) -> str:
        v = m.group(0)
        if v in ("true", "false", "null"):
            return v
        elif v in ("undefined", "void 0"):
            return "null"
        elif v.startswith(("/*", "//", "!")) or v == ",":
            return ""

        if v[0] in STRING_QUOTES:
            v = (
                re.sub(r"(?s)\${([^}]+)}", template_substitute, v[1:-1])
                if v[0] == "`"
                else v[1:-1]
            )
            escaped = re.sub(r'(?s)(")|\\(.)', process_escape, v)
            return f'"{escaped}"'

        for regex, base in INTEGER_TABLE:
            im = re.match(regex, v)
            if im:
                i = int(im.group(1), base)
                return f'"{i}":' if v.endswith(":") else str(i)

        if v in vars:
            try:
                if not strict:
                    json.loads(vars[v])
            except json.JSONDecodeError:
                return json.dumps(vars[v])
            else:
                return vars[v]

        if not strict:
            return f'"{v}"'

        raise ValueError(f"Unknown value: {v}")

    def create_map(mobj: re.Match) -> str:
        return json.dumps(dict(json.loads(_js_to_json(mobj.group(1) or "[]", vars=vars))))

    code = re.sub(r"(?:new\s+)?Array\((.*?)\)", r"[\g<1>]", code)
    code = re.sub(r"new Map\((\[.*?\])?\)", create_map, code)
    if not strict:
        code = re.sub(rf"new Date\(({STRING_RE})\)", r"\g<1>", code)
        code = re.sub(r"new \w+\((.*?)\)", lambda m: json.dumps(m.group(0)), code)
        code = re.sub(r"parseInt\([^\d]+(\d+)[^\d]+\)", r"\1", code)
        code = re.sub(
            r'\(function\([^)]*\)\s*\{[^}]*\}\s*\)\s*\(\s*(["\'][^)]*["\'])\s*\)', r"\1", code
        )

    return re.sub(
        rf"""(?sx)
        {STRING_RE}|
        {COMMENT_RE}|,(?={SKIP_RE}[\]}}])|
        void\s0|(?:(?<![0-9])[eE]|[a-df-zA-DF-Z_$])[.a-zA-Z_$0-9]*|
        \b(?:0[xX][0-9a-fA-F]+|(?<!\.)0+[0-7]+)(?:{SKIP_RE}:)?|
        [0-9]+(?={SKIP_RE}:)|
        !+
        """,
        fix_kv,
        code,
    )


def extract_attestation_challenge(page: str) -> str | None:
    m = INITIAL_ATTESTATION_PATTERN.search(page)
    if m:
        n = json.loads(_js_to_json(m["at_n"]))
        if "R" in n:
            r = json.loads(n["R"])
            if "bgChallenge" in r:
                return r["bgChallenge"]
    return None
