"""emit: writing a FieldSpec back out as TOML.

There used to be two writers -- the wizard's `render_task_card` and the
`[fields.X]` rewriter behind `task fix --write` -- and they disagreed with the
reader in `schema.py` and with each other. The rewriter dropped `window`,
`multiplier`, `percent`, `on_range` and `strip` entirely, so changing a rule
silently changed four others; and it wrote span bounds as `start`/`end` while
the reader only accepts `from`/`to`, which produced a card that could not be
read back at all. One writer, one set of names, taken from the reader.

Anything not in here is a parameter someone can set in a card and then have
removed by an unrelated edit, which is exactly the bug this module exists for.
"""
from __future__ import annotations

import re

_BARE = re.compile(r"^[A-Za-z0-9_-]+$")


def toml_key(name: str) -> str:
    """A TOML key: bare when it can be, quoted otherwise."""
    return name if _BARE.match(name) else toml_str(name)


def toml_str(value: str) -> str:
    """A TOML single-quoted literal, falling back to escaped double quotes."""
    if "'" not in value and "\n" not in value and "\r" not in value:
        return "'" + value + "'"
    out = value.replace("\\", "\\\\").replace('"', '\\"')
    out = out.replace("\n", "\\n").replace("\r", "\\r")
    return '"' + out + '"'


def _scalar(block: list[str], key: str, value: object) -> None:
    if isinstance(value, bool):
        block.append(f"{key} = {'true' if value else 'false'}")
    elif isinstance(value, (int, float)):
        block.append(f"{key} = {value}")
    elif isinstance(value, str):
        block.append(f"{key} = {toml_str(value)}")


def field_block(spec, *, head: bool = True) -> list[str]:
    """One field as `[fields.X]` + its parameters, in a stable order.

    Order is fixed so that re-running a fix on the same rule produces the same
    bytes: a file that churns on every write makes `git diff` useless for the
    one thing it is needed for here -- seeing what the rule change actually was.
    """
    out: list[str] = []
    if head:
        out.append(f"[fields.{toml_key(spec.name)}]")
    out.append(f'kind = "{spec.kind}"')

    # The primitive's own coordinates. Only the one that belongs to the kind is
    # written: leaving a stale `col` on a `line` field is how a later edit makes
    # the field read from the wrong place while still looking configured.
    if spec.kind == "cell":
        _scalar(out, "col", spec.col if spec.col is not None else 0)
    elif spec.kind == "line":
        _scalar(out, "index", spec.index or 0)
        if spec.pattern:
            _scalar(out, "pattern", spec.pattern)
    elif spec.kind == "search":
        if spec.pattern:
            _scalar(out, "pattern", spec.pattern)
    elif spec.kind == "span":
        # `schema.py` reads `from`/`to`; `FieldSpec` calls them `start`/`end`.
        # Writing the internal names here made every rewritten span field
        # unreadable ("字段 note 用 span 但 from/to 都没给").
        if spec.start:
            _scalar(out, "from", spec.start)
        if spec.end:
            _scalar(out, "to", spec.end)
    elif spec.kind == "const":
        _scalar(out, "value", "" if spec.value is None else str(spec.value))

    if spec.type != "text":
        out.append(f'type = "{spec.type}"')
    if spec.scope != "record":
        out.append(f'scope = "{spec.scope}"')
    if spec.window is not None:
        _scalar(out, "window", spec.window)
    if spec.unit:
        _scalar(out, "unit", spec.unit)
    if spec.multiplier is not None:
        _scalar(out, "multiplier", spec.multiplier)
    if spec.percent:
        out.append("percent = true")
    if spec.on_range != "reject":
        out.append(f'on_range = "{spec.on_range}"')
    if not spec.strip:
        out.append("strip = false")
    if spec.min_len:
        _scalar(out, "min_len", spec.min_len)
    if spec.exclude:
        out.append("exclude = [" + ", ".join(toml_str(x) for x in spec.exclude) + "]")
    # The dom/json primitives take selectors and paths. They are unimplemented
    # today, but a card can carry them, and dropping them on an unrelated edit
    # would quietly change which node the field was meant to read.
    for key in ("sel", "path"):
        value = getattr(spec, key, None)
        if value:
            _scalar(out, key, value)
    if getattr(spec, "attr", "text") != "text":
        _scalar(out, "attr", spec.attr)
    return out


def segment_block(card) -> list[str]:
    """Serialize the segmentation used by preview and saved-task execution."""
    out = ['[segment]', f'mode = "{card.segment_mode}"']
    if card.segment_mode == "periodic":
        _scalar(out, "period", card.segment_period)
        _scalar(out, "offset", card.segment_offset)
        if card.segment_count is not None:
            _scalar(out, "count", card.segment_count)
    elif card.segment_mode == "anchor":
        _scalar(out, "anchor", card.anchor)
        if card.anchor_next:
            _scalar(out, "anchor_next", card.anchor_next)
    if card.skip_header:
        out.append("skip_header = true")
    if card.min_lines != 1:
        _scalar(out, "min_lines", card.min_lines)
    if card.record_lines:
        out.append(f"record_lines = [{card.record_lines[0]}, {card.record_lines[1]}]")
    if card.required:
        out.append("required = [" + ", ".join(toml_str(f) for f in card.required) + "]")
    return out
