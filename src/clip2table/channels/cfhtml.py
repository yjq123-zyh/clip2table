"""CF_HTML byte-offset parsing.

The CF_HTML header carries offsets that are BYTE offsets into a UTF-8 byte
stream, written as zero-padded 10-digit strings. Slicing the decoded string
at those numbers produces a shifted, mid-character slice - verified to differ
from the byte slice the moment non-ASCII appears earlier in the document.
"""
from __future__ import annotations

import re

_HEADER_KEYS = ("Version", "StartHTML", "EndHTML", "StartFragment",
                "EndFragment", "SourceURL", "StartSelection", "EndSelection")


def parse_header(raw: bytes) -> dict[str, object]:
    """Parse the ASCII header. Returns ints for the offsets, None if absent."""
    head = raw[:2048].decode("utf-8", errors="replace")
    out: dict[str, object] = {}
    for key in _HEADER_KEYS:
        m = re.search(rf"{key}:(\S*)", head)
        val = m.group(1) if m else None
        if key != "Version" and key != "SourceURL" and val and val.isdigit():
            out[key] = int(val)
        else:
            out[key] = val
    return out


def extract_html_fragment(raw: bytes) -> str:
    """Slice the HTML fragment out of a CF_HTML payload, byte-correctly.

    Prefers StartFragment..EndFragment (the canonical fragment). Falls back to
    StartHTML..EndHTML, then to the whole payload. Decoding is strict so a
    misaligned slice raises instead of silently producing U+FFFD soup.
    """
    info = parse_header(raw)
    sf, ef = info.get("StartFragment"), info.get("EndFragment")
    sh, eh = info.get("StartHTML"), info.get("EndHTML")

    for lo, hi in ((sf, ef), (sh, eh)):
        if isinstance(lo, int) and isinstance(hi, int) and 0 <= lo < hi <= len(raw):
            chunk = raw[lo:hi]
            try:
                return chunk.decode("utf-8")
            except UnicodeDecodeError:
                # Offsets are broken (some apps emit char offsets). Degrade to
                # lenient decoding rather than throwing away the payload.
                return chunk.decode("utf-8", errors="replace")

    text = raw.decode("utf-8", errors="replace")
    m = re.search(r"<!--\s*StartFragment\s*-->(.*?)<!--\s*EndFragment\s*-->", text, re.S)
    return m.group(1) if m else text


# ---------------------------------------------------------------- RTF

_RTF_SKIP_DESTINATIONS = {
    "fonttbl", "colortbl", "stylesheet", "info", "pict", "object",
    "header", "footer", "footnote", "annotation", "listtable", "listoverridetable",
    "rsidtbl", "generator", "themedata", "colorschememapping", "latentstyles",
    "datastore", "xmlnstbl", "filetbl", "revtbl", "pgdsctbl", "mmathPr",
}


def rtf_to_text(raw: bytes) -> str:
    """Strip RTF control words down to plain text.

    Deliberately not a table reconstructor: RTF tables carry no reliable
    record boundary, so the output is text and the pipeline treats it as such.
    """
    src = raw.decode("latin-1", errors="replace")
    out: list[str] = []
    i, n = 0, len(src)
    depth_skip = 0
    stack: list[bool] = []
    while i < n:
        ch = src[i]
        if ch == "\\":
            i += 1
            if i >= n:
                break
            c = src[i]
            if c == "'":                       # \'hh hex byte
                hexs = src[i + 1:i + 3]
                try:
                    out.append(bytes([int(hexs, 16)]).decode("cp1252", errors="replace"))
                except ValueError:
                    pass
                i += 3
                continue
            if c == "*":                       # \* marks a skippable destination
                depth_skip += 1
                i += 1
                continue
            if not c.isalpha():
                mapping = {"\\": "\\", "{": "{", "}": "}", "~": "\u00a0",
                           "-": "", "_": "-", "\n": "\n"}
                if c in mapping:
                    if not depth_skip:
                        out.append(mapping[c])
                i += 1
                continue
            j = i
            while j < n and src[j].isalpha():
                j += 1
            word = src[i:j]
            num = ""
            while j < n and (src[j].isdigit() or src[j] == "-"):
                num += src[j]
                j += 1
            if j < n and src[j] == " ":
                j += 1
            if word in _RTF_SKIP_DESTINATIONS:
                depth_skip += 1
            elif word == "par" or word == "line":
                if not depth_skip:
                    out.append("\n")
            elif word == "tab":
                if not depth_skip:
                    out.append("\t")
            elif word == "u" and num:
                if not depth_skip:
                    code = int(num)
                    out.append(chr(code + 65536 if code < 0 else code))
            i = j
            continue
        if ch == "{":
            stack.append(depth_skip > 0)
            i += 1
            continue
        if ch == "}":
            was_skipping = stack.pop() if stack else False
            if depth_skip and not was_skipping:
                depth_skip -= 1
            i += 1
            continue
        if ch in "\r\n":
            i += 1
            continue
        if not depth_skip:
            out.append(ch)
        i += 1
    return "".join(out)
