"""Card regression: run a card over its own samples and diff against expectations.

Why this exists
---------------
`validate` answers "is this card well-formed". It cannot answer "does this card
still parse correctly", because a rule can be perfectly legal and completely
wrong. The only way to catch that is to have an answer key that the card does
not produce itself. So the key lives beside the sample as a separate file:

    samples/
      ecom.txt            <- a capture, exactly what Ctrl+A/Ctrl+C gave
      ecom.expect.tsv     <- the answer key: header row of field names, then values

If the key were generated from the card at test time, breaking a rule would
also break the key and every run would pass. That is the whole point of the
separate file.

TSV escaping
------------
A cell holding a newline would break the row layout, so newlines are stored as
the two characters `\\n` and an empty value as the empty cell. Both sides are
normalised before comparison, so `None` and `""` are the same thing (they are
the same thing to a spreadsheet too).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..core import parse_capture
from ..errors import InputError
from ..models import Captured
from .schema import Card

INPUT_SUFFIXES = (".txt", ".html", ".htm", ".json")
EXPECT_SUFFIX = ".expect.tsv"


@dataclass
class RecordFailure:
    index: int          # 1-based, the record number the user would count to
    field: str
    expected: str
    actual: str


@dataclass
class SampleResult:
    path: Path
    ok: bool
    rows: int = 0
    expected_rows: int = 0
    failures: list[RecordFailure] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


@dataclass
class CardTestReport:
    card: str
    samples_dir: str
    results: list[SampleResult] = field(default_factory=list)

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.ok)

    @property
    def failed(self) -> int:
        return len(self.results) - self.passed

    @property
    def ok(self) -> bool:
        return bool(self.results) and self.failed == 0

    def render(self) -> str:
        out = [f"卡回归：{self.card}",
               f"样本目录：{self.samples_dir}",
               f"  {len(self.results)} 个样本，通过 {self.passed}，失败 {self.failed}"]
        for r in self.results:
            if r.ok:
                out.append(f"  ✓ {r.path.name}  {r.rows} 条记录")
                continue
            out.append(f"  ✗ {r.path.name}  期望 {r.expected_rows} 条，实际 {r.rows} 条")
            for p in r.problems:
                out.append(f"     ! {p}")
            for f in r.failures[:20]:
                exp = f.expected if f.expected else "(空)"
                act = f.actual if f.actual else "(空)"
                out.append(f"     记录 #{f.index} {f.field}: 期望 {exp!r} 实际 {act!r}")
            if len(r.failures) > 20:
                out.append(f"     …另有 {len(r.failures) - 20} 处不一致")
        return "\n".join(out)


def _cell(v: object) -> str:
    if v is None:
        return ""
    s = str(v)
    s = s.replace("\r\n", "\n").replace("\r", "\n").strip()
    return s.replace("\n", "\\n")


def _read_expect(path: Path) -> tuple[list[str], list[list[str]]]:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as e:
        raise InputError(f"读不了期望文件 {path}：{e}") from None
    lines = [ln for ln in text.replace("\r\n", "\n").split("\n") if ln.strip()]
    if not lines:
        raise InputError(f"期望文件是空的：{path}",
                         hint="第一行必须是字段名，之后每行一条记录")
    header = [h.strip() for h in lines[0].split("\t")]
    # Cells stay in their escaped form: comparison happens against _cell(),
    # which escapes the same way. Unescaping one side only would make a value
    # containing a real newline impossible to express.
    rows = [[c.strip() for c in ln.split("\t")] for ln in lines[1:]]
    return header, rows


def _samples_dir(card: Card) -> Path:
    if not card.samples:
        raise InputError(
            f"卡 {card.name} 没有 samples，没法做回归",
            hint='在卡里加一行 samples = "samples/<名字>"（相对卡文件所在目录）')
    base = Path(card.path).parent if card.path else Path.cwd()
    d = (base / card.samples).resolve()
    if not d.is_dir():
        raise InputError(f"卡 {card.name} 的 samples 目录不存在：{d}",
                         hint=f"卡里写的是 samples = {card.samples!r}")
    return d


def test_card(card: Card) -> CardTestReport:
    """Run every sample of a card and diff each against its answer key."""
    d = _samples_dir(card)
    inputs = sorted(p for p in d.iterdir()
                    if p.is_file() and p.suffix.lower() in INPUT_SUFFIXES)
    if not inputs:
        raise InputError(f"{d} 里没有样本（{ ' / '.join(INPUT_SUFFIXES) }）",
                         hint="每个样本是一份原样保存的复制结果")

    rep = CardTestReport(card=card.name, samples_dir=str(d))
    for src in inputs:
        expect = src.with_name(src.stem + EXPECT_SUFFIX)
        res = SampleResult(path=src, ok=True)
        rep.results.append(res)
        if not expect.is_file():
            res.ok = False
            res.problems.append(f"缺期望文件 {expect.name}")
            continue

        kind = "json" if src.suffix.lower() == ".json" else "html" if src.suffix.lower() in (".html", ".htm") else "text"
        cap = Captured(kind=kind,
                       payload=src.read_text(encoding="utf-8", errors="replace"),
                       channel="file", formats=["file"])
        parsed, _ = parse_capture(cap, card)
        res.rows = len(parsed.rows)

        header, expected = _read_expect(expect)
        res.expected_rows = len(expected)

        want = card.field_names
        if header != want:
            res.problems.append(
                f"期望文件的表头 {header} 和卡里的字段 {want} 不一致")

        if res.rows != res.expected_rows:
            res.problems.append(
                f"记录数不一致：期望 {res.expected_rows}，实际 {res.rows}")
            res.ok = False

        for i in range(min(res.rows, len(expected))):
            row = parsed.rows[i]
            for j, name in enumerate(header):
                got = _cell(row.values.get(name))
                exp = expected[i][j] if j < len(expected[i]) else ""
                if got != exp:
                    res.ok = False
                    res.failures.append(
                        RecordFailure(index=i + 1, field=name,
                                      expected=exp, actual=got))
        if res.failures or res.problems:
            res.ok = False
    return rep


def scene_path(name):
    import sysconfig
    if name.startswith("scene:"):
        relative = Path(name[6:]) / "scene.toml"
        bases = [Path(__file__).resolve().parents[3] / "scenes",
                 Path(sysconfig.get_path("data")) / "share" / "clip2table" / "scenes"]
        for base in bases:
            if (base / relative).is_file():
                return base / relative
        raise InputError(f"找不到场景包：{name}")
    return Path(name)


def test_scene(path):
    import csv
    import tempfile
    import tomllib
    from .. import __version__, api
    from ..capabilities import CAPABILITIES
    from .loader import resolve_card
    path = scene_path(str(path)).resolve()
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    required = {"schema", "id", "version", "maintainer", "min_clip2table", "card_schema", "capabilities", "cards", "samples"}
    if required - data.keys() or data["schema"] != 1:
        raise InputError("场景清单需要完整的标识、兼容信息、卡片和样本")
    version = lambda value: tuple(int(part) for part in value.split("."))
    if version(__version__) < version(data["min_clip2table"]):
        raise InputError(f"场景包需要 clip2table ≥{data['min_clip2table']}")
    if set(data["capabilities"]) - CAPABILITIES.keys():
        raise InputError("场景包声明了未知字段能力")
    def local(name):
        target = (path.parent / name).resolve()
        if not target.is_relative_to(path.parent) or not target.is_file():
            raise InputError(f"场景文件不可用：{name}")
        return target
    for name in data["cards"]:
        card = resolve_card(str(local(name)))
        if card.schema not in data["card_schema"]:
            raise InputError(f"场景卡片格式未声明：{name}")
        if {field.kind for field in card.fields} - set(data["capabilities"]):
            raise InputError(f"场景卡片使用了未声明的字段能力：{name}")
    report = CardTestReport(data["id"], str(path.parent))
    for sample in data["samples"]:
        if sample["card"] not in data["cards"]:
            raise InputError(f"样本卡片未列入场景清单：{sample['card']}")
        result = SampleResult(path=Path(sample["name"]), ok=True)
        report.results.append(result)
        card = resolve_card(str(local(sample["card"])))
        with tempfile.TemporaryDirectory(prefix="c2t-scene-") as directory:
            dataset = Path(directory) / "dataset"
            for n, name in enumerate(sample["inputs"]):
                request = api.ExecuteRequest(dataset=str(dataset), request_id=f"sample-{n}",
                    capture={"kind": sample["kind"], "payload": local(name).read_text(encoding="utf-8")})
                answer = api.execute(request, card)
                if not answer.ok:
                    result.problems.append(answer.message)
                    result.ok = False
                    break
            if not result.ok:
                continue
            rows, fields, _ = api.ledger_view(dataset, view=sample["view"])
            with local(sample["expected"]).open(encoding="utf-8-sig", newline="") as stream:
                expected = list(csv.reader(stream))
            result.rows, result.expected_rows = len(rows), len(expected) - 1
            if sample.get("pending_format") == "boolean":
                rows = [dict(row, _pending="是" if row.get("_pending") else "") for row in rows]
            actual = [fields] + [[str(row.get(name, "")) for name in fields] for row in rows]
            if actual != expected:
                result.ok = False
                result.problems.append("结果字段、顺序或取值与独立答案不同")
    return report
