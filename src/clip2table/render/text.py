"""CSV / Markdown / JSONL. Deliberately tiny; xlsx lives in its own module.

Every writer takes (rows, fields, path) and returns the path written. They all
escape correctly and none of them drops a value: an empty field is an empty
field, never "N/A".
"""
from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from ..models import Row
from .base import RenderResult, lossless


def _text(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def write_csv(rows: list[Row], fields: list[str],
              path: str | Path) -> RenderResult:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    # utf-8-sig: Excel on Windows opens plain UTF-8 as mojibake, and this is
    # the single most common "the tool gave me garbage" report.
    with p.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, quoting=csv.QUOTE_MINIMAL)
        w.writerow(fields)
        for r in rows:
            w.writerow([_text(r.values.get(k)) for k in fields])
    # CSV is lossless: quoting handles every character the format can hold.
    return lossless(p)


def _md_cell(v: Any) -> str:
    s = _text(v).replace("\r\n", "\n").replace("\r", "\n")
    s = s.replace("|", "\\|")
    return s.replace("\n", "<br>")


def write_md(rows: list[Row], fields: list[str],
             path: str | Path) -> RenderResult:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    # The header goes through the same escaping as the body. A field named
    # `a|b` used to shift every column one cell to the right: the data was
    # fine, the table was not.
    lines = ["| " + " | ".join(_md_cell(f) for f in fields) + " |",
             "| " + " | ".join("---" for _ in fields) + " |"]
    for r in rows:
        lines.append("| " + " | ".join(_md_cell(r.values.get(k)) for k in fields) + " |")
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return lossless(p)


def write_jsonl(rows: list[Row], fields: list[str],
                path: str | Path) -> RenderResult:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as f:
        for r in rows:
            obj = {k: r.values.get(k) for k in fields}
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")
    return p


WRITERS = {
    "csv": write_csv,
    "md": write_md,
    "jsonl": write_jsonl,
}
