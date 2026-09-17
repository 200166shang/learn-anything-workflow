"""Canonical cue locators without modifying the source transcript."""

import re


def cue_ranges(text: str) -> list[str]:
    return [f"{start.replace('.', ',')} --> {end.replace('.', ',')}"
            for start, end in re.findall(
                r"(?m)^(\d{2}:[0-5]\d:[0-5]\d[,.]\d{3})[ \t]+-->[ \t]+"
                r"(\d{2}:[0-5]\d:[0-5]\d[,.]\d{3})[ \t]*\r?$", text)]
