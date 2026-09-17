"""Resolve adopted inline Markdown links into portable content-addressed links.

Adopted files use inline image/link destinations (bare or angle-wrapped, with
optional titles). Reference-style and HTML attachment embeds are not accepted;
callers receive a validation error rather than publishing an unresolved file.
"""

import hashlib
import re
from pathlib import Path
from urllib.parse import unquote


def _links(markdown):
    # Preserve offsets while excluding non-rendered Markdown code and comments.
    masked = markdown
    patterns = [r"<!--.*?(?:-->|\Z)",
                r"(?m)^ {0,3}(`{3,})[^\n]*\n.*?(?:^ {0,3}\1`*[ \t]*(?:\n|\Z)|\Z)",
                r"(?m)^ {0,3}(~{3,})[^\n]*\n.*?(?:^ {0,3}\1~*[ \t]*(?:\n|\Z)|\Z)",
                r"(?m)^(?: {4}|\t)[^\n]*(?:\n|\Z)",
                r"(`+)(?!`).*?(?<!`)\1(?!`)"]
    for pattern in patterns:
        masked = re.sub(pattern, lambda match: "".join("\n" if char == "\n" else " "
                                                     for char in match.group()), masked, flags=re.DOTALL)
    pattern = re.compile(
        r"(?<!\\)(?P<image>!?)\[(?:\\.|[^\]\\\n])*\]\(\s*"
        r"(?P<target><[^>\n]+>|(?:\\.|[^\s()\\]|\([^()\n]*\))+)"
        r"(?:[ \t]+(?:\"[^\"\n]*\"|'[^'\n]*'|\([^()\n]*\)))?\s*\)")
    return list(pattern.finditer(masked))


def portable_markdown(markdown, attachments):
    """Return rewritten body and exactly the attachment bytes used to name it."""
    blobs = [(Path(raw).expanduser().resolve(), Path(raw).expanduser().resolve().read_bytes())
             for raw in attachments]
    names = [path.name for path, _ in blobs]
    if len(names) != len(set(names)):
        raise ValueError("adopted attachments must have distinct file names")
    destinations = {}
    required = set()
    for path, body in blobs:
        digest = hashlib.sha256(body).hexdigest()
        target = f"../{digest[:2]}/{digest}"
        required.add(target)
        for alias in (path.name, str(path), target):
            destinations[alias] = target
    used = set()
    replacements = []
    for match in _links(markdown):
        raw = match.group("target")
        destination = unquote(raw[1:-1] if raw.startswith("<") else raw)
        destination = re.sub(r"\\([!\"#$%&'()*+,\-./:;<=>?@\[\]\\^_`{|}~])", r"\1", destination)
        target = destinations.get(destination)
        if target:
            used.add(target)
            replacements.append((*match.span("target"), target))
        elif match.group("image") and not re.match(r"https?://", destination):
            raise ValueError("local note image must identify an adopted attachment")
    if used != required:
        raise ValueError("adopted attachment is not used by the note body as a rendered inline Markdown link")
    for start, end, target in reversed(replacements):
        markdown = markdown[:start] + target + markdown[end:]
    return markdown, blobs


def validate_published_links(markdown, digests):
    expected = {f"../{digest[:2]}/{digest}" for digest in digests}
    linked = {match.group("target").strip("<>") for match in _links(markdown)}
    if not expected.issubset(linked):
        raise ValueError("published note does not link every adopted attachment object")
