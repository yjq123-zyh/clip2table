"""Extract records from saved JSON and HTML snapshots."""
from __future__ import annotations

import json
import hashlib
import re
from dataclasses import asdict, dataclass, field, replace
from html.parser import HTMLParser

from ..errors import ChannelError, InputError
from ..models import FieldTrace, ParseResult, Row
from .extract import _coerce, _iso_date


class Number(str):
    """A JSON number's original spelling."""


def _invalid_constant(value):
    raise ValueError(value)


def json_document(text):
    try:
        return json.loads(text, parse_int=Number, parse_float=Number,
                          parse_constant=_invalid_constant)
    except (ValueError, TypeError) as error:
        raise InputError(f"JSON 内容无效：{error}") from None


def pointer_parts(path):
    if not isinstance(path, str) or (path and not path.startswith("/")) or re.search(r"~(?![01])", path):
        raise InputError(f"JSON 路径无效：{path!r}")
    return [p.replace("~1", "/").replace("~0", "~") for p in path.split("/")[1:]]


_MISSING = object()


def lookup(obj, path):
    for part in pointer_parts(path):
        if isinstance(obj, dict):
            obj = obj.get(part, _MISSING)
        elif isinstance(obj, list) and re.fullmatch(r"0|[1-9][0-9]*", part):
            index = int(part)
            obj = obj[index] if index < len(obj) else _MISSING
        else:
            return _MISSING
    return obj


@dataclass(eq=False)
class Node:
    tag: str
    attrs: dict = field(default_factory=dict)
    children: list = field(default_factory=list)
    parent: Node | None = None
    path: str = ""

    def descendants(self):
        for child in self.children:
            if isinstance(child, Node):
                yield child
                yield from child.descendants()

    def ancestors(self):
        node = self.parent
        while node is not None:
            yield node
            node = node.parent

    def text(self):
        return "".join(c.text() if isinstance(c, Node) else c for c in self.children)


class Document(HTMLParser):
    VOID = set("area base br col embed hr img input link meta param source track wbr".split())

    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.root = Node("document")
        self.stack = [self.root]
        self.feed(text)
        self.close()

    def handle_starttag(self, tag, attrs):
        parent = self.stack[-1]
        index = sum(isinstance(c, Node) for c in parent.children)
        node = Node(tag, dict(attrs), parent=parent, path=f"{parent.path}/{index}")
        parent.children.append(node)
        if tag not in self.VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if self.stack[-1].tag == tag and tag not in self.VOID:
            self.stack.pop()

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)


_ATOM = re.compile(r"([a-zA-Z][\w-]*|\*)|([.#])([\w-]+)|\[([\w:-]+)(?:\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([\w-]+)))?\]|:nth-of-type\(([1-9][0-9]*)\)")


def selector_parts(selector):
    if not isinstance(selector, str) or not selector.strip():
        raise InputError("DOM 选择器需要指定标签、类、ID 或属性")
    tokens = re.findall(r"(?:\[[^\]]*\]|[^\s>\[])+|>", selector)
    if not tokens or tokens[0] == ">" or tokens[-1] == ">" or "> >" in " ".join(tokens):
        raise InputError(f"DOM 选择器无效：{selector!r}")
    parts = []
    relation = " "
    for token in tokens:
        if token == ">":
            relation = ">"
            continue
        atoms, pos = [], 0
        while pos < len(token):
            match = _ATOM.match(token, pos)
            if match is None:
                raise InputError(f"DOM 选择器语法无效：{selector!r}")
            atoms.append(match.groups())
            pos = match.end()
        parts.append((relation, atoms))
        relation = " "
    return parts


def _matches(node, atoms):
    for tag, prefix, name, attr, double, single, bare, nth in atoms:
        if tag and tag != "*" and node.tag != tag.lower():
            return False
        if prefix == "#" and node.attrs.get("id") != name:
            return False
        if prefix == "." and name not in (node.attrs.get("class") or "").split():
            return False
        if nth:
            siblings = [c for c in node.parent.children if isinstance(c, Node) and c.tag == node.tag]
            if siblings.index(node) + 1 != int(nth):
                return False
        if attr:
            value = next((v for v in (double, single, bare) if v is not None), None)
            if attr not in node.attrs or (value is not None and node.attrs[attr] != value):
                return False
    return True


def select(root, selector):
    scope = [root]
    for relation, atoms in selector_parts(selector):
        found = {}
        for parent in scope:
            candidates = (c for c in parent.children if isinstance(c, Node)) if relation == ">" else parent.descendants()
            for node in candidates:
                if _matches(node, atoms):
                    found[node.path] = node
        scope = list(found.values())
    order = {node.path: i for i, node in enumerate(root.descendants())}
    return sorted(scope, key=lambda node: order[node.path])


def source_hash(cap):
    text = cap.payload if cap.payload is not None else json.dumps(cap.json_payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _node_selector(root, node):
    identity = node.attrs.get("id", "")
    if re.fullmatch(r"[\w-]+", identity) and len(select(root, "#" + identity)) == 1:
        return "#" + identity
    parts = []
    while node.parent is not None:
        siblings = [c for c in node.parent.children if isinstance(c, Node) and c.tag == node.tag]
        parts.append(f"{node.tag}:nth-of-type({siblings.index(node) + 1})")
        node = node.parent
    return " > ".join(reversed(parts))


def structure_candidates(cap, card):
    """Return deterministic record scopes and snapshot-bound selection IDs."""
    scopes = []
    if card.segment_mode == "json":
        document = json_document(cap.payload) if cap.payload is not None else cap.json_payload
        def visit(value, path):
            if isinstance(value, list):
                if not value or any(isinstance(item, dict) for item in value):
                    scopes.append(({"path": path}, len(value)))
                for i, item in enumerate(value):
                    visit(item, f"{path}/{i}")
            elif isinstance(value, dict):
                for key, item in value.items():
                    visit(item, path + "/" + key.replace("~", "~0").replace("/", "~1"))
        visit(document, "")
    else:
        root = Document(cap.payload or "").root
        regions = select(root, card.segment_region)
        parents = [node.parent for node in select(root, card.segment_records)]
        seen = set()
        for region in regions + parents:
            if region.path in seen or region is root:
                continue
            seen.add(region.path)
            records = select(region, card.segment_records)
            members = set(records)
            if not records or any(parent in members for node in records for parent in node.ancestors()):
                continue
            selector = card.segment_region if len(regions) == 1 and region is regions[0] else _node_selector(root, region)
            scopes.append(({"region": selector, "records": card.segment_records}, len(records)))
    snapshot = source_hash(cap)
    candidates = []
    for segment, count in scopes:
        digest = hashlib.sha256(json.dumps([snapshot, asdict(card), segment], sort_keys=True,
                                          ensure_ascii=False).encode()).hexdigest()[:24]
        candidates.append({"index": len(candidates) + 1, "candidate_id": "structure-" + digest,
                           "segment": {"mode": card.segment_mode, **segment}, "records": count})
    return candidates


def choose_structure(card, candidates, choice):
    candidate = next((c for c in candidates if
                      (type(choice) is int and c["index"] == choice) or c["candidate_id"] == choice), None)
    if candidate is None:
        raise InputError("请选择列表中的结构候选", code="invalid_action")
    segment = candidate["segment"]
    return replace(card, **{"segment_" + k: v for k, v in segment.items() if k != "mode"})


def _field(raw, spec, locator, matches=1):
    status = "missing" if raw is _MISSING else "null" if raw is None else "ok"
    if isinstance(raw, (dict, list, Node)):
        status = "type-mismatch"
    if matches > 1 and spec.match == "unique":
        status = "ambiguous"
    text = None if raw is _MISSING or raw is None or isinstance(raw, (dict, list, Node)) else (
        "true" if raw is True else "false" if raw is False else str(raw))
    if status == "ok" and ((spec.min_len and len(text) < spec.min_len) or any(x in text for x in spec.exclude)):
        status = "no-match"
    value = _coerce(text, spec) if status == "ok" else None
    if status == "ok" and spec.type in ("text", "str") and (text == "" or not spec.strip):
        value = text
    if status == "ok" and value is None:
        status = "coerce-failed"
    trace = FieldTrace(spec.name, spec.kind, status=status, raw=text,
                       rule=spec.describe_rule(), locator=locator, matches=matches,
                       normalized=_iso_date(text) if text is not None and spec.type == "date" else None)
    return value, trace


def parse_structured(cap, card):
    mode = card.segment_mode
    if cap.kind != ("html" if mode == "dom" else "json"):
        raise InputError(f"{mode} 规则需要 {'HTML' if mode == 'dom' else 'JSON'} 内容")
    if mode == "json":
        obj = json_document(cap.payload) if cap.payload is not None else cap.json_payload
        records = lookup(obj, card.segment_path)
        if not isinstance(records, list):
            raise ChannelError("记录路径需要定位到 JSON 数组", reason="structure_selection",
                               details={"path": card.segment_path})
    else:
        regions = select(Document(cap.payload or "").root, card.segment_region)
        if len(regions) != 1:
            raise ChannelError("请选择一个 DOM 目标区域", reason="structure_selection",
                               details={"selector": card.segment_region, "matches": len(regions)})
        records = select(regions[0], card.segment_records)
        selected = set(records)
        for record in records:
            ancestor = record.parent
            while ancestor:
                if ancestor in selected:
                    raise ChannelError("记录容器存在嵌套，请调整记录选择器", reason="structure_selection")
                ancestor = ancestor.parent
    result = ParseResult(rows=[], fields=card.field_names, level="card")
    seen = {}
    pending = []
    mapping = []
    for index, record in enumerate(records):
        path = f"{card.segment_path}/{index}" if mode == "json" else record.path
        locator = {"kind": mode, "path": path}
        if mode == "json" and not isinstance(record, dict):
            pending.append({"locator": locator, "reason": "record_type"})
            result.unparsed.append(json.dumps(record, ensure_ascii=False))
            continue
        row = Row(values={}, locator=locator)
        for spec in card.fields:
            if spec.kind == "const":
                raw, where, count = spec.value, locator, 1
            elif mode == "json":
                raw = lookup(record, spec.path)
                where, count = {"kind": mode, "path": path + spec.path}, int(raw is not _MISSING)
            else:
                nodes = select(record, spec.sel) if spec.sel else [record]
                raw = (nodes[0].text() if spec.attr == "text" else nodes[0].attrs.get(spec.attr, _MISSING)) if nodes else _MISSING
                where = {"kind": mode, "path": nodes[0].path if nodes else record.path,
                         "selector": spec.sel, "attr": spec.attr}
                count = len(nodes)
                if count > 1:
                    where["candidates"] = [node.path for node in nodes]
            row.values[spec.name], row.traces[spec.name] = _field(raw, spec, where, count)
        key = tuple(row.values.get(k) for k in card.dedupe_key)
        if card.dedupe_key and any(v not in (None, "") for v in key) and key in seen:
            mapping.append({"locator": locator, "row": seen[key], "deduplicated": True})
        else:
            seen[key] = len(result.rows)
            mapping.append({"locator": locator, "row": len(result.rows), "deduplicated": False})
            result.rows.append(row)
    result.coverage = {"candidates": len(records), "parsed": len(mapping), "pending": pending,
                       "deduplicated": sum(m["deduplicated"] for m in mapping), "mapping": mapping}
    if pending:
        result.deviations.append(f"{len(pending)} 条记录的对象类型不符合规则")
    return result
