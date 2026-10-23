"""xlsx by hand: zipfile + SpreadsheetML. No openpyxl, no dependency.

What it writes: one sheet, a bold frozen header row, typed numbers, inline
strings. What it does not write: styles beyond that, charts, formulas,
multiple sheets. That is the whole trade - about 150 lines instead of a
dependency, for the only thing the tool actually needs.

Two Excel limits are load-bearing here:
  - a cell holds at most 32767 characters; longer values are truncated
  - XML 1.0 forbids most control characters; they are stripped, not escaped
"""
from __future__ import annotations

import re
import zipfile
from pathlib import Path
from typing import Any

from ..models import Row
from .base import Change, RenderResult

MAX_CELL = 32767

# XML 1.0 valid:  #x9 | #xA | #xD | [#x20-#xD7FF] | [#xE000-#xFFFD] | [#x10000-#x10FFFF]
_ILLEGAL_XML = re.compile(
    "[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x84\x86-\x9f\ud800-\udfff\ufdd0-\ufdef\ufffe\uffff]"
)

_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
</Types>"""

_ROOT_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>"""

_WORKBOOK = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
<sheets><sheet name="{sheet}" sheetId="1" r:id="rId1"/></sheets>
</workbook>"""

_WB_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>"""

# Two cellXfs: 0 = plain, 1 = bold (header).
_STYLES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font>
<font><b/><sz val="11"/><name val="Calibri"/></font></fonts>
<fills count="2"><fill><patternFill patternType="none"/></fill>
<fill><patternFill patternType="gray125"/></fill></fills>
<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="2">
<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/>
</cellXfs>
</styleSheet>"""


def _esc(s: str) -> str:
    s = _ILLEGAL_XML.sub("", s)
    return (s.replace("&", "&amp;").replace("<", "&lt;")
             .replace(">", "&gt;").replace('"', "&quot;"))


def _col_letter(i: int) -> str:
    """0 -> A, 25 -> Z, 26 -> AA."""
    out = ""
    i += 1
    while i:
        i, rem = divmod(i - 1, 26)
        out = chr(65 + rem) + out
    return out


def _cell(ref: str, value: Any, style: int) -> str:
    st = f' s="{style}"' if style else ""
    if value is None or value == "":
        return f'<c r="{ref}"{st}/>'
    if isinstance(value, bool):
        return f'<c r="{ref}"{st} t="b"><v>{1 if value else 0}</v></c>'
    if isinstance(value, (int, float)):
        return f'<c r="{ref}"{st}><v>{value}</v></c>'
    text = str(value)
    return (f'<c r="{ref}"{st} t="inlineStr"><is><t xml:space="preserve">'
            f'{_esc(text)}</t></is></c>')


def _fit(value: Any, row_no: int, field: str,
         changes: list[Change]) -> str:
    """The value as Excel can hold it, recording anything that had to go.

    Two limits are unavoidable in the format: 32767 characters per cell, and
    XML 1.0 forbids most control characters. Both used to happen silently, so a
    long description lost its tail while the report said the export succeeded.
    """
    text = "" if value is None else str(value)
    if not text:
        return text
    kept = text
    if len(kept) > MAX_CELL:
        kept = kept[:MAX_CELL]
        changes.append(Change(
            row=row_no, field=field, kind="truncated",
            detail=f"超过 Excel 单元格上限 {MAX_CELL} 字符，只写入前 {MAX_CELL} 字符",
            original=text, kept_len=len(kept)))
    stripped = _ILLEGAL_XML.sub("", kept)
    if len(stripped) != len(kept):
        changes.append(Change(
            row=row_no, field=field, kind="stripped",
            detail=f"XML 不允许的字符 {len(kept) - len(stripped)} 个已删除",
            original=text, kept_len=len(stripped)))
    return stripped


def _sheet_xml(rows: list[Row], fields: list[str], sheet: str,
               changes: list[Change] | None = None) -> str:
    width = max(1, len(fields))
    last_col = _col_letter(width - 1)
    out = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">',
        # Freeze the header so a 10k-row sheet stays readable.
        '<sheetViews><sheetView workbookViewId="0">'
        '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
        '</sheetView></sheetViews>',
    ]
    if fields:
        widths = []
        for i, f in enumerate(fields):
            longest = max([len(str(f))] + [len(str(r.values.get(f) or "")[:60])
                                           for r in rows[:200]])
            widths.append(f'<col min="{i + 1}" max="{i + 1}" '
                          f'width="{min(60, max(8, longest + 2))}" customWidth="1"/>')
        out.append("<cols>" + "".join(widths) + "</cols>")

    out.append("<sheetData>")
    out.append('<row r="1">' + "".join(
        _cell(f"{_col_letter(i)}1", f, 1) for i, f in enumerate(fields)) + "</row>")
    for ri, row in enumerate(rows, start=2):
        cells = []
        for i, f in enumerate(fields):
            value = row.values.get(f)
            if changes is not None:
                value = _fit(value, ri, f, changes)
            cells.append(_cell(f"{_col_letter(i)}{ri}", value, 0))
        out.append(f'<row r="{ri}">' + "".join(cells) + "</row>")
    out.append("</sheetData>")
    if fields and rows:
        out.append(f'<autoFilter ref="A1:{last_col}{len(rows) + 1}"/>')
    out.append("</worksheet>")
    return "".join(out)


def _companion(path: Path, changes: list[Change]) -> Path | None:
    """Put every value the format could not hold next to the file, in full.

    Disclosing a truncation is not enough if the tail is gone: the user needs
    the value back, and needs to know which cell to put it in.
    """
    lost = [c for c in changes if c.original]
    if not lost:
        return None
    side = path.with_name(path.stem + ".full.jsonl")
    import json
    with side.open("w", encoding="utf-8") as f:
        for c in lost:
            f.write(json.dumps({"row": c.row, "field": c.field,
                                "kind": c.kind, "detail": c.detail,
                                "value": c.original},
                               ensure_ascii=False) + "\n")
    return side


def write_xlsx(rows: list[Row], fields: list[str], path: str | Path,
               sheet: str = "Sheet1") -> RenderResult:
    p = Path(path)
    changes: list[Change] = []
    p.parent.mkdir(parents=True, exist_ok=True)
    safe_sheet = re.sub(r"[\\/*?:\[\]]", "_", sheet)[:31] or "Sheet1"

    # ZIP_DEFLATED matters: a 10k-row sheet is ~2 MB uncompressed.
    with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _CONTENT_TYPES)
        z.writestr("_rels/.rels", _ROOT_RELS)
        z.writestr("xl/workbook.xml", _WORKBOOK.format(sheet=_esc(safe_sheet)))
        z.writestr("xl/_rels/workbook.xml.rels", _WB_RELS)
        z.writestr("xl/styles.xml", _STYLES)
        z.writestr("xl/worksheets/sheet1.xml",
                   _sheet_xml(rows, fields, safe_sheet, changes))
    return RenderResult(path=p, changes=changes,
                        companion=_companion(p, changes))


def write_any(fmt: str, rows: list[Row], fields: list[str],
              path: str | Path) -> RenderResult:
    from .text import WRITERS
    if fmt == "xlsx":
        return write_xlsx(rows, fields, path)
    if fmt in WRITERS:
        return WRITERS[fmt](rows, fields, path)
    raise ValueError(f"不支持的导出格式 {fmt!r}（支持 csv/md/xlsx/jsonl）")
