"""cli: six flags, three exit codes, commands that say what they did.

Learning a tool's whole surface should take one screen. So: `run` is the
default, `harvest` is the one that puts a whole round together, and everything
else either inspects (`cards`, `doctor`, `selftest`) or re-emits (`export`).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

from . import __version__
from . import preview
from .cards import emit
from .cards.loader import list_builtin, load_card, resolve_card
from .cards.schema import Card
from .core import _text_of as _text_of_cap, parse_capture
from .errors import BAD_INPUT, OK, ChannelError, Clip2TableError, InputError
from .flow import Totals, open_dataset, run_report_fields
from .models import Captured
from .pipeline.normalize import normalize_text, to_lines
from .report import Report, sample_block
from .render.text import write_md


class _ClipboardEmpty(Exception):
    """Local sentinel: the clipboard API works, it just had nothing in it."""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="clip2table",
        description="把屏幕上看得到、选得中的任何内容，变成一张结构化表",
    )
    p.add_argument("-V", "--version", action="version", version=f"clip2table {__version__}")
    sub = p.add_subparsers(dest="cmd")

    def common(sp: argparse.ArgumentParser, out: bool = True) -> None:
        sp.add_argument("-p", "--card", default=None, help="站点卡 .toml 路径或内置卡名")
        sp.add_argument("-d", "--dataset", default=None,
                        help="把结果累积进这个目录（断点续采、去重、冲突审计）")
        sp.add_argument("--channel", default="auto",
                        choices=("auto", "http", "browser", "desktop"),
                        help="自动选（默认）或钉死某一条通道；钉死时不降级")
        if out:
            sp.add_argument("-o", "--out", default=None, help="输出文件；省略则只打印报告")
            sp.add_argument("-f", "--format", action="append", default=None,
                            help="导出格式：csv/md/jsonl/xlsx（可重复）")
            sp.add_argument("--dir", default=None, help="导出目录，文件名按卡名生成")
        sp.add_argument("--json", action="store_true", help="报告以 JSON 输出")
        sp.add_argument("--dry-run", action="store_true",
                        help="只解析并打印，不写任何文件")

    run_p = sub.add_parser("run", help="一次性：取一份 → 解析 → 出表（无参默认）")
    common(run_p)
    run_p.add_argument("--url", default=None,
                       help="改为按地址取：自动按 A→B→C 找一条能用的通道")
    run_p.add_argument("--text-file", default=None, help="改为读文本文件，不读剪贴板")
    run_p.add_argument("--html-file", default=None, help="改为读 HTML 文件")
    run_p.add_argument("--show", type=int, default=3, help="打印前 N 条解析结果")

    har_p = sub.add_parser(
        "harvest", help="主角：自动按键复制一整轮（含翻页），无人值守")
    common(har_p)
    har_p.add_argument("--max-pages", type=int, default=None,
                       help="最多翻几页（默认用卡里的 paging.max_pages）")
    har_p.add_argument("--port", type=int, default=None,
                       help="浏览器调试端口（默认自动探测 9222/9223/9333）")
    har_p.add_argument("--force", action="store_true",
                       help="目标窗口有未保存修改时仍然继续（desktop 通道）")
    har_p.add_argument("--yes", action="store_true",
                       help="跳过放量前的确认（多页时必须先看过单页报告）")
    har_p.add_argument("--append-from-here", action="store_true",
                       dest="append_from_here",
                       help="桌面通道没有可恢复的界面位置：明确声明「接着当前屏幕继续采」，"
                            "新的一段会以单独段号接在后面，而不是假装页号能还原界面")
    har_p.add_argument("--allow-card-change", action="store_true",
                       dest="allow_card_change",
                       help="卡的定义改过之后，仍然往同一个 dataset 里追加"
                            "（默认拒绝：同名列在不同规则下含义不同，混在一起"
                            "看起来齐整、实际上不是一种数据）")
    har_p.add_argument("--show", type=int, default=3)

    inf_p = sub.add_parser("infer", help="零配置：从剪贴板/文件反推结构与卡草案")
    common(inf_p)
    inf_p.add_argument("--text-file", default=None)
    inf_p.add_argument("--html-file", default=None)
    inf_p.add_argument("--show", type=int, default=3)
    inf_p.add_argument("--write-card", default=None,
                       help="把推断出的卡草案写到这个路径")

    exp_p = sub.add_parser("export", help="从已有 texts/ 目录或 dataset 重出任意格式")
    exp_p.add_argument("source", nargs="?", default=None,
                       help="待导出的文本/HTML 文件或目录")
    exp_p.add_argument("-d", "--dataset", default=None,
                       help="改为从 dataset 目录导出（不重采，只重排版）")
    exp_p.add_argument("-p", "--card", default=None)
    exp_p.add_argument("-f", "--format", action="append", default=None)
    exp_p.add_argument("-o", "--out", default=None)
    exp_p.add_argument("--dir", default=None,
                       help="输出目录（与 run/harvest 一致；省略则写进 dataset 目录或当前目录）")
    exp_p.add_argument("--json", action="store_true")

    cards_p = sub.add_parser("cards", help="卡的查看与校验")
    cards_sub = cards_p.add_subparsers(dest="cards_cmd")
    cards_sub.add_parser("list", help="列出内置卡")
    show_p = cards_sub.add_parser("show", help="打印一张卡的解析结果")
    show_p.add_argument("card")
    val_p = cards_sub.add_parser("validate", help="校验一张卡")
    val_p.add_argument("card")

    sub.add_parser("doctor", help="自检：剪贴板 / Python / 卡目录 / 浏览器调试端口")
    sub.add_parser("selftest", help="对账 SKILL.md 里的命令与真实 argparser")

    test_p = sub.add_parser(
        "test", help="用卡自带的 samples 做回归，列出不一致的记录号")
    test_p.add_argument("card")
    test_p.add_argument("--json", action="store_true", help="结果以 JSON 输出")

    task_p = sub.add_parser(
        "task", help="任务：向导建卡、预览纠错、改一次应用到整批、再次执行")
    task_sub = task_p.add_subparsers(dest="task_cmd")

    new_p = task_sub.add_parser("new", help="从一份样本建一张以后能直接跑的任务卡")
    new_p.add_argument("-p", "--card", default=None,
                       help="已有的卡：改成任务（补 [task] 段）而不是重新推断")
    new_p.add_argument("--text-file", default=None, help="样本文本文件")
    new_p.add_argument("--html-file", default=None, help="样本 HTML 文件")
    new_p.add_argument("--url", default=None, help="样本来源地址（会真去取一次）")
    new_p.add_argument("--source", default=None,
                       help="再次采集的 URL（可含 {page}）或桌面窗口标题正则")
    new_p.add_argument("--next", default=None,
                       help="浏览器下一页按钮 CSS，或桌面翻页按键")
    new_p.add_argument("--max-pages", type=int, default=None, help="任务默认采集页数")
    new_p.add_argument("--channel", default="auto",
                       choices=("auto", "http", "browser", "desktop"))
    new_p.add_argument("--out", default=None,
                       help="写到这个路径；默认 ./<任务名>.toml")
    new_p.add_argument("--yes", action="store_true",
                       help="全部用推断值，不问问题（agent 用这条）")
    new_p.add_argument("--rows", type=int, default=5, help="预览打印几条")
    new_p.add_argument("--record-start", dest="record_start", type=int, default=None,
                       help="首条完整记录的原文起始行号，从 1 开始，与 --record-end 一起使用")
    new_p.add_argument("--record-end", dest="record_end", type=int, default=None,
                       help="首条完整记录的原文结束行号（包含该行）")
    new_p.add_argument("--force", action="store_true", help="覆盖已存在的卡")

    show_p = task_sub.add_parser("show", help="看一个任务：它是什么、字段、规则、跑过几次")
    show_p.add_argument("card")
    show_p.add_argument("--json", action="store_true")

    chk_p = task_sub.add_parser(
        "check", help="预览：表格 + 每个字段来自原文哪一行 + 候选切法")
    chk_p.add_argument("card")
    chk_p.add_argument("--text-file", default=None, help="改看这个样本，不重新取数")
    chk_p.add_argument("--html-file", default=None)
    chk_p.add_argument("--rows", type=int, default=8, help="打印几条记录")
    chk_p.add_argument("--lines", type=int, default=0,
                       help="另外打印原文的前 N 行（0 = 不打印）")
    chk_p.add_argument("--field", default="", help="重点看这个字段的来源")
    chk_p.add_argument("--pick", type=int, default=0,
                       help="换成第 N 种候选切法重看（列表里的编号）")
    chk_p.add_argument("--write", action="store_true", help="保存 --pick 选择的分段")
    chk_p.add_argument("--confirm", action="store_true", help="记录当前样本结果已由用户确认")
    chk_p.add_argument("--json", action="store_true")

    fix_p = task_sub.add_parser(
        "fix", help="改一个字段的规则，立刻在整批样本上预览效果，并保存")
    fix_p.add_argument("card")
    fix_p.add_argument("--field", required=True, help="要改的字段名")
    fix_p.add_argument("--kind", default=None,
                       choices=("line", "cell", "search", "span", "const"))
    fix_p.add_argument("--index", type=int, default=None, help="line 原语：记录内第几行")
    fix_p.add_argument("--col", type=int, default=None, help="cell 原语：第几列")
    fix_p.add_argument("--pattern", default=None, help="search 原语：正则，用 (?P<v>…) 标出值")
    fix_p.add_argument("--type", default=None,
                       choices=("text", "date", "int", "float", "str", "bool"))
    mark_p = fix_p.add_mutually_exclusive_group()
    mark_p.add_argument("--from-line", dest="from_line", type=int, default=None,
                       help="读取原文指定行在所属记录中的位置，应用到每条记录")
    mark_p.add_argument("--value", default=None,
                       help="标出第 1 条记录中的值，读取它所在的整行")
    fix_p.add_argument("--text-file", default=None, help="在这份样本上验证")
    fix_p.add_argument("--html-file", default=None)
    fix_p.add_argument("--write", action="store_true",
                       help="确认改动无误后真正写回卡文件")
    fix_p.add_argument("--rows", type=int, default=5)
    fix_p.add_argument("--json", action="store_true")

    run2_p = task_sub.add_parser(
        "run", help="再次执行一个任务：取数 → 解析 → 出表（可累积）")
    common(run2_p)
    run2_p.add_argument("--url", default=None)
    run2_p.add_argument("--text-file", default=None)
    run2_p.add_argument("--html-file", default=None)
    run2_p.add_argument("--max-pages", type=int, default=None)
    run2_p.add_argument("--port", type=int, default=None)
    run2_p.add_argument("--yes", action="store_true")
    run2_p.add_argument("--force", action="store_true",
                        help="窗口标题变了也照样发键（desktop 通道）")
    run2_p.add_argument("--append-from-here", dest="append_from_here",
                        action="store_true",
                        help="从当前屏幕接着采，不尝试回到第 N 页")
    run2_p.add_argument("--allow-card-change", dest="allow_card_change",
                        action="store_true",
                        help="卡换过了也往这个 dataset 里写")
    run2_p.add_argument("--show", type=int, default=3)

    return p


# ------------------------------------------------------------------ helpers

def _capture_from_args(args) -> tuple[Captured, str]:
    """(capture, ladder decision line or "")."""
    if getattr(args, "url", None):
        from . import ladder
        cap, decision = ladder.capture(args.url, getattr(args, "channel", "auto"))
        return cap, decision.line()
    if getattr(args, "text_file", None):
        p = Path(args.text_file)
        if not p.exists():
            raise InputError(f"找不到文件：{p}")
        return Captured(kind="text", payload=p.read_text(encoding="utf-8", errors="replace"),
                        channel="file", engine=None, formats=["file"]), ""
    if getattr(args, "html_file", None):
        p = Path(args.html_file)
        if not p.exists():
            raise InputError(f"找不到文件：{p}")
        return Captured(kind="html", payload=p.read_text(encoding="utf-8", errors="replace"),
                        channel="file", engine=None, formats=["file"]), ""
    from .channels.clipboard import read_clipboard
    return read_clipboard(), ""


def _emit(report: Report, args, sample: list[str] | None = None) -> None:
    if getattr(args, "json", False):
        print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
        return
    print(report.render())
    if sample:
        print()
        print("\n".join(sample))


def _write_unparsed(res, target: Path) -> Path | None:
    """Keep the lines that no record claimed, next to the table.

    The report says "未识别的 N 行不会被丢弃". That sentence is only true if
    they land somewhere on disk: the in-memory list dies with the process, and
    a re-run on a changed page cannot reproduce them. So every export drops a
    sidecar `.unparsed.txt` beside the table. It is deliberately *not* a column
    (one 32767-char cell limit per field, and a quoted blob of navigation text
    destroys the shape of the table it is attached to).
    """
    if not res.unparsed:
        return None
    side = target.with_suffix(target.suffix + ".unparsed.txt") \
        if target.suffix else target.with_name(target.name + ".unparsed.txt")
    side.parent.mkdir(parents=True, exist_ok=True)
    side.write_text("\n".join(res.unparsed) + "\n", encoding="utf-8")
    return side


def _report_render(results, out=print) -> None:
    """Say what each format did to the values, not just where it went.

    An export that silently shortened a cell looked exactly like one that did
    not, so the disclosure has to travel with the file.
    """
    for r in results:
        if not getattr(r, "changes", None):
            continue
        out(f"⚠ {r.path.name}：{len(r.changes)} 处内容被格式改动")
        for c in r.changes[:5]:
            out(f"    第 {c.row} 行 {c.field}：{c.detail}")
        if len(r.changes) > 5:
            out(f"    …另有 {len(r.changes) - 5} 处")
        if getattr(r, "companion", None):
            out(f"    完整值已另存：{r.companion}")


def _export(res, fields: list[str], args, stem: str) -> list[Path]:
    fmts = args.format or ([] if args.out else [])
    if args.out and not fmts:
        fmts = [Path(args.out).suffix.lstrip(".").lower() or "csv"]
    if not fmts:
        return []
    written: list[Path] = []
    from .render.xlsx import write_any
    for fmt in fmts:
        if fmt not in ("csv", "md", "jsonl", "xlsx"):
            raise InputError(f"不支持的格式 {fmt!r}（csv/md/jsonl/xlsx）")
        if args.out and len(fmts) == 1:
            target = Path(args.out)
        elif args.out:
            # Plenty of people write `-o out/report.csv -f csv -f xlsx` and
            # mean "both, named report". Ignoring -o in that case silently
            # dropped files into the current directory instead.
            out = Path(args.out)
            target = out.with_suffix(f".{fmt}")
        else:
            base = Path(args.dir) if args.dir else Path.cwd()
            target = base / f"{stem}.{fmt}"
        result = write_any(fmt, res.rows, fields, target)
        written.append(result)
    _report_render([w for w in written if hasattr(w, "changes")])
    for target in [Path(str(w)) for w in list(written)]:
        side = _write_unparsed(res, target)
        if side is not None:
            written.append(side)
    return [Path(str(w)) for w in written]


# ------------------------------------------------------------------ commands

def _engine_label(cap: Captured) -> str:
    """Name the engine honestly in reports that did not come from harvest.

    `run --text-file x.txt` used to report `file ｜ HTTP`, which is nonsense: no
    HTTP happened. Saying which path produced the text is the whole point of
    the headline, so a wrong label there is worse than a vague one.
    """
    if cap.engine:
        return cap.engine
    return {"http": "HTTP", "browser": "E1", "desktop": "CLIP",
            "file": "FILE"}.get(cap.channel or "", "-")


def cmd_run(args) -> int:
    cap, why = _capture_from_args(args)
    card = resolve_card(args.card) if args.card else None
    if args.dataset and card is None:
        # A dataset binds a card snapshot, a field definition and a key rule.
        # Writing into one from a capture nobody described is how a directory
        # ends up holding two different tables under one name.
        raise InputError(
            "run -d 需要同时给 -p 一张卡",
            hint="先 `clip2table infer --write-card x.toml` 定住字段，"
                 "再 `run -p x.toml -d 目录`")
    res, inf = parse_capture(cap, card)
    fields = card.field_names if card else (inf.result.fields if inf else res.fields)

    rep = Report(captured=cap, result=res, card=card.name if card else "",
                 engine=cap.engine or _engine_label(cap))
    if card is not None:
        rep.extra["field_specs"] = list(card.fields)
    if why:
        rep.extra["channel_decision"] = why

    # One capture is page 1 of the same commit pipeline `harvest` uses. It is
    # not a second write path: accepting -d and then dropping the rows was the
    # exact defect this replaces.
    written: list[Path] = []
    committer, start_page, append_from_here = open_dataset(
        args, card, cap.channel if card else None, raw=cap.payload or "")
    ds = committer.ds if committer else None
    totals = committer.totals if committer else Totals()
    totals.fetched(res)
    if committer is not None:
        committer.page(start_page, cap.payload or "",
                       dict(cap.provenance(), why=why or "run", card=card.name),
                       res, append_from_here=append_from_here)
        rep.result = ds.as_result()
        res = rep.result
    rep.extra.update(run_report_fields(ds, totals, args.dry_run))

    if not args.dry_run and ds is None:
        stem = card.name if card else "clip2table"
        written = _export(res, fields, args, stem)

    if getattr(args, "json", False):
        rep.extra["written"] = [str(w) for w in written]
        _emit(rep, args)
    else:
        _emit(rep, args, sample_block(res, getattr(args, "show", 3)))
        if ds is not None:
            print()
            print(f"本轮取到 {totals.fetched_rows} 行；"
                  f"提交进 dataset {totals.added} 行")
            print(f"dataset：{ds.path}（累计 {len(ds.rows)} 行，"
                  f"已提交页 {len(ds.committed_pages())}）")
        if written:
            print()
            for w in written:
                print(f"已写出：{w}")
        elif not args.dry_run and ds is None:
            print()
            print("（没写文件：加 -f csv 或 -o 结果.csv 才会落盘）")
    return OK


def cmd_harvest(args) -> int:
    """One round,无人值守: focus -> copy -> read -> parse -> dedupe -> next."""
    from .dataset import dataset_from_card, PAGE_COL
    from .harvest import harvest_pages
    from .pipeline.assemble import merge_pages

    card = resolve_card(args.card) if args.card else None
    if card is None:
        raise InputError("harvest 必须给一张卡：-p 卡片.toml",
                         hint="先用 `clip2table infer --write-card x.toml` 生成草案")
    if card.channel not in ("http", "browser", "desktop"):
        raise InputError(
            f"卡 {card.name!r} 的 channel 是 {card.channel!r}，harvest 只支持 "
            f"http / browser / desktop",
            hint='在卡里加 channel = "http"、"browser" 或 "desktop"')

    max_pages = args.max_pages if args.max_pages is not None else card.max_pages
    max_pages = max(1, int(max_pages))

    # Where to write is decided once, here, and a preview gets no committer at
    # all -- so no callback further down can write "just this one file".
    committer, start_page, append_from_here = open_dataset(args, card, card.channel)
    ds = committer.ds if committer else None
    totals = Totals()

    if max_pages > 1 and not args.yes:
        # Deliberate friction: 50 unverified pages is 50 pages of garbage and
        # 50x the load on someone else's server. One page first, always.
        print(f"要翻 {max_pages} 页。先跑一页看过报告再放量是规矩。", file=sys.stderr)
        if not sys.stdin.isatty():
            raise InputError(
                "多页采集需要先确认，而当前不是交互终端",
                hint="先跑 --max-pages 1 --dry-run 看一页，确认无误后加 --yes")
        try:
            ans = input(f"确认按 {max_pages} 页采集？[y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = ""
        if ans not in ("y", "yes"):
            print("已取消", file=sys.stderr)
            return BAD_INPUT

    def land(rnd) -> None:
        """Commit this page before the next page is attempted.

        There is no `dry_run` check here on purpose: a preview never gets a
        committer, so this callback cannot be the one place that forgets.
        """
        if committer is None or rnd.result is None:
            return
        cap = rnd.captured
        committer.page(
            rnd.page, cap.payload or "",
            dict(cap.provenance(), why=rnd.why, card=card.name),
            rnd.result, append_from_here=append_from_here)

    multi = harvest_pages(card, max_pages=max_pages, port=args.port,
                          allow_force=args.force, start_page=start_page,
                          on_page=land)
    totals = committer.totals if committer else Totals()
    for rnd in multi.rounds:
        if rnd.result is not None:
            totals.fetched(rnd.result)

    results = []
    for rnd in multi.rounds:
        res = rnd.result if rnd.result is not None else parse_capture(rnd.captured, card)[0]
        for note in rnd.notes:
            res.notes.append(note)
        res.notes.append(f"第 {rnd.page} 页：{rnd.why}")
        results.append(res)

    merged = merge_pages(results, card.dedupe_key or None)
    first = multi.first
    cap0 = first.captured if first else Captured(kind="text", payload="",
                                                 channel=card.channel)
    rep = Report(captured=cap0, result=merged, card=card.name,
                 engine=first.engine if first else "",
                 pages=multi.pages_done)
    rep.extra["field_specs"] = list(card.fields)
    rep.extra["stopped_because"] = multi.stopped_because
    rep.extra["new_per_page"] = multi.new_per_page
    if multi.repeat_of is not None:
        rep.extra["repeat_of"] = multi.repeat_of
        rep.result.notes.append(
            f"第 {multi.pages_done} 页和第 {multi.repeat_of} 页内容相同，"
            f"翻页没有真的前进，这一页没有计入")

    # With a dataset, the report describes the table on disk, not the pages
    # just fetched. "36 rows" from a 3-page run says nothing about a directory
    # that already held 200 -- and a user who asks "how much have I got" is
    # asking about the directory.
    final = merged
    if ds is not None:
        if multi.rounds:
            # The resume position advanced page-by-page inside the committer,
            # which is the only place that runs after a page is published.
            # Recomputing it from `multi.pages_done` here would move it forward
            # for pages that were fetched but never committed -- e.g. when the
            # last page errored mid-parse.
            ds.log_run({
                "card": card.name, "channel": card.channel,
                "engine": first.engine if first else "",
                "start_page": start_page, "pages": multi.pages_done,
                "stopped_because": multi.stopped_because,
                "append_from_here": append_from_here,
                "provenance": cap0.provenance(),
                **totals.as_dict(), "rows_in_dataset": len(ds.rows),
            })
        final = ds.as_result()
        rep.result = final
    rep.extra.update(run_report_fields(ds, totals, args.dry_run))

    written: list[Path] = []
    if not args.dry_run and not ds:
        written = _export(merged, card.field_names, args, card.name)

    if args.json:
        rep.extra["written"] = [str(w) for w in written]
        _emit(rep, args)
        return OK

    _emit(rep, args, sample_block(final, args.show))
    print()
    print(f"停止原因：{multi.stopped_because}")
    print(f"每页新增：{multi.new_per_page}")
    print(f"本轮取到 {totals.fetched_rows} 行；"
          + (f"提交进 dataset {totals.added} 行"
             if ds is not None else "没有 dataset，未提交任何行"))
    if ds is not None:
        print(f"本轮：{_dataset_summary(totals.as_dict(), ds.on_collision)}")
        print(f"dataset：{ds.path}（累计 {len(ds.rows)} 行，"
              f"已提交页 {len(ds.committed_pages())}，原文在 {ds.pages_dir}）")
        print("  注意：报告里的行数是这个目录的累计值，不是本轮页数")
    for w in written:
        print(f"已写出：{w}")
    if not written and not args.dry_run and ds is None:
        print("（没写文件：加 -f csv 或 -o 结果.csv 才会落盘）")
    return OK


def _dataset_summary(totals: dict, on_collision: str) -> str:
    bits = [f"新增 {totals['added']} 行"]
    if totals["duplicates"]:
        bits.append(f"重复 {totals['duplicates']} 行已跳过")
    if totals.get("changed"):
        how = {"report_only": "只报告，保留先前那条",
               "keep_first": "保留先前那条",
               "keep_all": "两条都留，新的标 _dup_of"}.get(on_collision, on_collision)
        bits.append(f"内容变更 {totals['changed']} 行（{how}）")
    if totals.get("identity_conflicts"):
        how = {"report_only": "只报告，保留先前那条",
               "keep_first": "保留先前那条",
               "keep_all": "两条都留，新的标 _dup_of"}.get(on_collision, on_collision)
        bits.append(f"身份冲突 {totals['identity_conflicts']} 行"
                    f"（身份字段变了，{how}）")
    if totals["empty_keys"]:
        bits.append(f"{totals['empty_keys']} 行去重键不完整，身份未确定（已保留）")
    return "，".join(bits)


def cmd_infer(args) -> int:
    cap, why = _capture_from_args(args)
    res, inf = parse_capture(cap, None)
    assert inf is not None
    rep = Report(captured=cap, result=res,
                 engine=cap.engine or _engine_label(cap))
    if why:
        rep.extra["channel_decision"] = why

    written: list[Path] = []
    if not args.dry_run:
        written = _export(res, inf.result.fields, args, "inferred")

    card_path = getattr(args, "write_card", None)
    if card_path and not args.dry_run:
        p = Path(card_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(draft_card_toml(inf, cap), encoding="utf-8")
        written.append(p)

    if getattr(args, "json", False):
        rep.extra.update({"written": [str(w) for w in written],
                          "level": inf.level,
                          "sample": inf.sample,
                          "evidence": inf.evidence,
                          "ambiguous": inf.ambiguous,
                          "alternates": [
                              {"why": a.why, "period": a.period,
                               "fields": a.field_count,
                               "records": a.record_count,
                               "consumed": a.consumed,
                               "unparsed": a.unparsed,
                               "sample": a.sample[:2]}
                              for a in inf.alternates]})
        _emit(rep, args)
    else:
        _emit(rep, args, _infer_block(inf, args))
        if written:
            print()
            for w in written:
                print(f"已写出：{w}")
        if card_path:
            print()
            print(f"卡草案：{card_path}（改掉字段名就能长期复用）")
    return OK


def _export_dataset(args) -> int:
    """Re-emit an existing dataset. Never re-collects: same input, same table."""
    from .dataset import Dataset, require_dir

    path = require_dir(args.dataset)
    ds = Dataset(path).open()
    if not ds.rows:
        raise InputError(f"{path} 里一行都没有",
                         hint="先跑 harvest -d 这个目录，或检查 rows.csv 是否为空")
    res = ds.as_result()
    cap0 = Captured(kind="text", payload="", channel="dataset", formats=["dataset"])
    rep = Report(captured=cap0, result=res, card=ds.state.get("card") or "",
                 engine="DATASET", pages=int(ds.state.get("pages_done") or 0) or 1)
    rep.extra["dataset"] = str(path)
    rep.extra["dataset_rows"] = len(ds.rows)

    fmts = args.format or ["csv"]
    out_dir = Path(args.dir) if getattr(args, "dir", None) else path
    written: list[Path] = []
    for fmt in fmts:
        if args.out and len(fmts) == 1:
            target = Path(args.out)
        elif args.out:
            target = Path(args.out).with_suffix(f".{fmt}")
        else:
            target = out_dir / f"export.{fmt}"
        from .render.xlsx import write_any
        written.append(write_any(fmt, res.rows, res.fields, target))
    _report_render([w for w in written if hasattr(w, "changes")])
    for target in [Path(str(w)) for w in list(written)]:
        side = _write_unparsed(res, target)
        if side is not None:
            written.append(side)
    if args.json:
        rep.extra["written"] = [str(w) for w in written]
        _emit(rep, args)
    else:
        _emit(rep, args)
        for w in written:
            print(f"已写出：{w}")
    return OK


def cmd_export(args) -> int:
    if getattr(args, "dataset", None):
        return _export_dataset(args)

    if not args.source:
        raise InputError("export 要么给一个文件/目录，要么用 -d 指定 dataset",
                         hint="export 样本.txt -f csv  或者  export -d out/orders -f csv")

    src = Path(args.source)
    card = resolve_card(args.card) if args.card else None
    if not src.exists():
        raise InputError(f"找不到：{src}")

    files = sorted(src.glob("*")) if src.is_dir() else [src]
    files = [f for f in files if f.suffix.lower() in
             (".txt", ".md", ".html", ".htm", ".csv", ".tsv")]
    if not files:
        raise InputError(f"{src} 里没有可导出的文本文件")

    from .pipeline.assemble import merge_pages
    results = []
    for f in files:
        text = f.read_text(encoding="utf-8", errors="replace")
        kind = "html" if f.suffix.lower() in (".html", ".htm") else "text"
        cap = Captured(kind=kind, payload=text, channel="file", formats=["file"])
        res, _ = parse_capture(cap, card)
        results.append(res)

    merged = merge_pages(results, card.dedupe_key or None if card else None)
    fields = card.field_names if card else merged.fields
    cap0 = Captured(kind="text", payload="", channel="file", formats=["file"])
    rep = Report(captured=cap0, result=merged, card=card.name if card else "",
                 engine="FILE", pages=len(files))

    written = _export(merged, fields, args, card.name if card else "export")
    if args.json:
        rep.extra["written"] = [str(w) for w in written]
        _emit(rep, args)
    else:
        _emit(rep, args)
        for w in written:
            print(f"已写出：{w}")
    return OK


def cmd_task(args) -> int:
    """`task` family: new / show / check / fix / run."""
    from . import taskflow, tasks

    sub = getattr(args, "task_cmd", None)
    if sub == "new":
        return _task_new(args, taskflow, tasks)
    if sub == "show":
        return _task_show(args, tasks)
    if sub == "check":
        return _task_check(args, taskflow)
    if sub == "fix":
        return _task_fix(args, taskflow, tasks)
    if sub == "run":
        return _task_run(args, tasks)
    raise InputError("task 后面要跟 new / show / check / fix / run",
                     hint="task new 从一个样本建任务；task check 看它对不对")


def _task_new(args, taskflow, tasks) -> int:
    """Wizard: sample in, runnable card out."""
    from . import marks
    from .pipeline.normalize import normalize_text, to_lines
    started_at = tasks._now()

    if (args.record_start is None) != (args.record_end is None):
        raise InputError("标记记录边界需要同时给出 --record-start 和 --record-end")
    cap, _why = _capture_from_args(args)
    card = None
    if args.card:
        # Converting an existing card: keep its rules, ask for the task metadata.
        card = resolve_card(args.card)
        res, inf = parse_capture(cap, card)
        print(f"用现有卡「{card.name}」重跑这份样本："
              f"{len(res.rows)} 条 × {len(res.fields)} 个字段")
    else:
        res, inf = parse_capture(cap, None)

    # Marking the boundary by hand is the answer to "inference cut it wrong and
    # I cannot express why in a regex". Both ends are 1-based line numbers of
    # the sample the user is looking at.
    override = None
    if args.record_start is not None:
        from .cards import emit as _emit
        from .pipeline.assemble import AssembleOptions, assemble
        from .pipeline.segment import segment

        seg_lines = to_lines(normalize_text(_text_of_cap(cap)))
        total = len(seg_lines)
        s = marks.line_no(str(args.record_start), total)
        e = marks.line_no(str(args.record_end), total)
        marks.period_of(s, e)
        period = sum(bool(ln.strip()) for ln in seg_lines[s:e + 1])
        anchor, hits = marks.anchor_from(seg_lines, s, period)
        print(f"按你标的边界重切：一条记录 {period} 行，起点在第 {s + 1} 行")
        print(f"  由此推出的 anchor = {_emit.toml_str(anchor)}，匹配 {len(hits)} 处"
              f"（第 {[h + 1 for h in hits[:6]]}{' …' if len(hits) > 6 else ''} 行）")
        seg = segment(seg_lines, anchor=anchor)
        specs0 = (list(card.fields) if card is not None
                  else list(inf.specs or []) if inf is not None else [])
        if not specs0:
            specs0 = [taskflow.FieldSpec(name=f, kind="line", index=i)
                      for i, f in enumerate(res.fields)]
        res = assemble(seg, specs0,
                       AssembleOptions(dedupe_by=(card.dedupe_key or None)
                                       if card is not None else None))
        override = ('mode = "anchor"', anchor, "")

    ans = taskflow.build_answers(res, inf, cap, args, card, seg_override=override)
    if override is not None:
        ans.record_lines = (period, period)
        ans.min_lines = 1
        ans.skip_header = False
    text = taskflow.render_task_card(ans)

    out = Path(args.out) if args.out else Path(f"{ans.name}.toml")
    if out.exists() and not args.force:
        raise InputError(f"{out} 已经存在",
                         hint="换一个 --out，或者加 --force 覆盖")
    # Refuse to write a card this tool cannot read back. A draft that fails its
    # own parser is the one failure mode that wastes the most of a user's time.
    import tomllib
    from .cards.schema import parse_card
    try:
        saved_card = parse_card(tomllib.loads(text), str(out))
        saved_result, _ = parse_capture(cap, saved_card)
    except Clip2TableError as e:
        raise InputError(f"生成的任务卡自己校验不过：{e.message}",
                         hint="这是工具的缺陷，不是你的输入问题，请报告")

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")

    # The first look at the finished task is part of creating it: writing a file
    # and saying "done" is how a task ends up run once and abandoned.
    #
    # These two lines are meant to be pasted. `task run` takes the card as `-p`
    # (it shares the flags every other command uses), while `check`/`fix`/`show`
    # take it positionally -- one card in the wrong shape turns a copy-paste into
    # `unrecognized arguments`, which is a bad first minute.
    print(f"\n已写好任务卡：{out}")
    print(f"  字段：{'、'.join(f.name for f in ans.fields)}")
    print(f"  下一步：clip2table task check {out}")
    sample_arg = (f' --text-file "{args.text_file}"' if args.text_file else
                  f' --html-file "{args.html_file}"' if args.html_file else "")
    live = bool(ans.fetch.get("url") or any(ans.target.get(k)
                for k in ("url", "title", "exe", "window_class")))
    print(f'          clip2table task run -p "{out}" -d out/{ans.name}'
          + ("" if live else sample_arg))
    if not live:
        print("  当前卡用于样本导入；创建自动采集任务时用 --source 填写来源。")
    _task_record_created(out, ans, tasks, rows=len(saved_result.rows), started_at=started_at)
    return OK


def _task_record_created(out: Path, ans, tasks, *, rows: int = 0,
                         started_at: str | None = None) -> None:
    """Mark the task as created, with no runs yet.

    The wizard's preview is not a run. Recording it as one would make "首次任务
    耗时" mean the wizard instead of the first harvest, which is the one number a
    new user actually feels.
    """
    p = tasks.stats_path(out)
    data = {"runs": [], "fixes": []}
    data["created"] = {"at": started_at or tasks._now(), "fields": [f.name for f in ans.fields],
                       "channel": ans.channel, "sample_rows": rows}
    tasks.save(p, data)


def _task_show(args, tasks) -> int:
    card = resolve_card(args.card)
    p = tasks.stats_path(_card_path_for_stats(card, args.card))
    summary = tasks.summarise(tasks.load(p))
    if args.json:
        print(json.dumps({
            "name": card.name, "title": card.title, "path": card.path,
            "channel": card.channel, "task": card.task,
            "fields": [{"name": f.name, "kind": f.kind, "type": f.type,
                        "rule": f.describe_rule()} for f in card.fields],
            "segment": {"mode": card.segment_mode, "anchor": card.anchor,
                        "anchor_next": card.anchor_next,
                        "record_lines": list(card.record_lines or ())},
            "paging": {"style": card.paging_style, "max_pages": card.max_pages},
            "dedupe": card.dedupe_key, "stats": summary,
        }, ensure_ascii=False, indent=2))
        return OK

    print(f"任务：{card.title}（{card.name}）")
    if card.task.get("purpose"):
        print(f"  要产出：{card.task['purpose']}")
    if card.task.get("source"):
        print(f"  数据来源：{card.task['source']}")
    print(f"  通道：{card.channel}")
    print(f"  切分：{card.segment_mode}"
          + (f"（anchor={card.anchor}）" if card.anchor else ""))
    if card.record_lines:
        print(f"  一条记录 {card.record_lines[0]}–{card.record_lines[1]} 行")
    print(f"  字段（{len(card.fields)}）：")
    for f in card.fields:
        print(f"    {f.name:<12} {f.kind:<7} {f.describe_rule()}")
    if card.dedupe_key:
        print(f"  去重主键：{'、'.join(card.dedupe_key)}")
    if card.task.get("expected"):
        print("  一条正确记录：" + "；".join(card.task["expected"]))
    notes = tasks.describe(summary)
    for line in notes:
        print(f"  {line}")
    return OK


def _task_check(args, taskflow) -> int:
    """Preview: the table, where each value came from, and the raw text."""
    from dataclasses import replace
    from . import tasks
    from .preview import numbered_source

    card = resolve_card(args.card)
    if args.write and not args.pick:
        raise InputError("--write 需要与 --pick 一起使用")
    if args.confirm and args.pick and not args.write:
        raise InputError("确认候选前需要加 --write 保存该规则")
    cap, _why = _capture_from_args(args)
    lines = to_lines(normalize_text(_text_of_cap(cap)))
    res, inf = parse_capture(cap, card)
    if inf is None:
        # A card decides its own boundary, so `parse_capture` returns no infer
        # result -- and the candidate list then had nothing to show exactly
        # where a user needs it most, on a card whose boundary is wrong. Ask
        # inference the same question anyway; its answer stays advisory and
        # never overrides the card unless `--pick` names one.
        from .pipeline.infer import infer as _infer
        try:
            inf = _infer(cap)
        except Clip2TableError:
            inf = None
    picked: dict[str, object] = {}
    if getattr(args, "pick", 0):
        # "Try the second reading" has to be one flag, not a new rule the user
        # writes: the alternates are already numbered in the text above.
        alts = (inf.alternates if inf is not None else [])
        if not (1 <= args.pick <= len(alts)):
            raise InputError(
                f"没有第 {args.pick} 种切法",
                hint=f"这份样本有 {len(alts)} 种候选" if alts else
                     "这张卡是手定的切法，推断没有给出候选")
        alt = alts[args.pick - 1]
        card = replace(card, segment_mode="periodic", segment_period=alt.period,
                       segment_offset=alt.offset, segment_count=alt.record_count,
                       anchor=None, anchor_next=None, skip_header=False)
        res, _ = parse_capture(cap, card)
        picked = {"pick": args.pick, "period": alt.period,
                  "offset": alt.offset, "rows": len(res.rows)}
        if not args.json:
            print(f"按第 {args.pick} 种切法重看：每 {alt.period} 行一条，"
                  f"从第 {alt.offset + 1} 个非空行起算 → {len(res.rows)} 条")
            print()

    if args.confirm and (not res.rows or res.check_conservation() or res.deviations):
        raise InputError("当前样本无记录或存在规则偏离，修正后再确认",
                         hint="；".join(res.deviations) or res.check_conservation() or "没有记录")
    if args.write:
        _rewrite_section(args.card, lambda line: bool(re.match(r"^\s*\[\s*segment\s*\]", line)),
                         emit.segment_block(card)[1:], cap=cap, expected=res)
        tasks.record_fix(tasks.stats_path(_card_path_for_stats(card, args.card)),
                         field="[segment]", kind="periodic", detail=str(picked))
    import hashlib
    from .dataset import _card_fingerprint
    card_hash = hashlib.sha256(json.dumps(_card_fingerprint(card), sort_keys=True,
                                         ensure_ascii=False).encode()).hexdigest()
    sample_hash = hashlib.sha256(normalize_text(_text_of_cap(cap)).encode()).hexdigest()
    stats_file = tasks.stats_path(_card_path_for_stats(card, args.card))
    if args.confirm:
        confirmation = tasks.record_confirm(stats_file, rows=len(res.rows),
                                             card_hash=card_hash, sample_hash=sample_hash)
        if not confirmation["recorded"]:
            raise InputError(f"确认记录无法保存到 {stats_file}")
    confirmed = any(c.get("card_hash") == card_hash and c.get("sample_hash") == sample_hash
                    for c in tasks.load(stats_file).get("confirmations", []) if isinstance(c, dict))

    if args.json:
        sources = []
        for r in res.rows[:args.rows]:
            per: dict[str, object] = {}
            for f in res.fields:
                t = r.traces.get(f)
                per[f] = {"value": r.values.get(f),
                          "line": preview.source_of(r, t)}
            sources.append(per)
        print(json.dumps({
            "card": card.name, "rows": len(res.rows), "fields": res.fields,
            "unparsed": len(res.unparsed),
            "conservation": res.check_conservation(),
            "samples": [{f: r.values.get(f) for f in res.fields}
                        for r in res.rows[:args.rows]],
            "sources": sources,
            "fill_rates": res.fill_rates, "deviations": res.deviations,
            "deviation_rows": res.deviation_rows,
            "confirmed": confirmed, "saved": bool(args.write),
            "alternates": [a.line() for a in (inf.alternates if inf else [])],
            **picked,
        }, ensure_ascii=False, indent=2))
        return OK

    body = taskflow.preview_text(res, lines, rows=args.rows,
                                source_lines=args.lines, field=args.field)
    for line in body:
        print(line)
    print("人工确认：" + ("当前规则和样本已确认" if confirmed else "尚未确认（--confirm）"))
    if args.write:
        print(f"已保存候选分段：{args.card}")
    if inf is not None and inf.alternates and not picked:
        print()
        print("还有这几种切法，如果上面那条不对，可以换（--pick N）：")
        for i, alt in enumerate(inf.alternates, 1):
            print(f"  {i}. {alt.line()}")
    cons = res.check_conservation()
    if cons:
        print(f"\n✗ {cons}")
    else:
        print(f"\n✓ 行数守恒：{res.total_lines} 行全部有归属"
              f"（记录 {len(res.rows)} + 未识别 {len(res.unparsed)}）")
    return OK


def _task_fix(args, taskflow, tasks) -> int:
    """Change one rule, show the effect on the whole batch, then save if asked."""
    from . import marks
    from .cards import schema as schema_mod

    card = resolve_card(args.card)
    if args.field not in card.field_names:
        raise InputError(f"卡里没有字段「{args.field}」",
                         hint=f"有这些：{card.field_names}")
    before = next(f for f in card.fields if f.name == args.field)

    cap, _why = _capture_from_args(args)
    res_before, _ = parse_capture(cap, card)

    # Two ways to say "the value is on the fourth line of the record": type the
    # number, or paste the value. Both end up as the same `line` rule, because
    # the point of the fix command is that nobody has to learn a syntax to
    # correct a rule they can already see is wrong.
    index = args.index
    marked = args.value is not None or args.from_line is not None
    if marked and (args.index is not None or args.col is not None
                   or args.pattern is not None or args.kind not in (None, "line")):
        raise InputError("按原文标记字段时，请使用 --from-line 或 --value 指定位置，"
                         "不要同时指定其他取值位置或原语")
    if args.value is not None:
        if not res_before.rows:
            raise InputError("这份样本没解析出记录，没法按值定位",
                             hint="先用 --from-line 直接给行号")
        rec = res_before.rows[0]
        rel = marks.find_in_record(list(rec.raw_lines), args.value)
        print(f"「{args.value}」在第 1 条记录的第 {rel + 1} 行"
              f"（原文第 {(rec.raw_at[rel] + 1) if rec.raw_at and rel < len(rec.raw_at) else '?'} 行）")
        index = rel
    elif args.from_line is not None:
        seg_lines = to_lines(normalize_text(_text_of_cap(cap)))
        abs_i = marks.line_no(str(args.from_line), len(seg_lines))
        if not res_before.rows:
            raise InputError("这份样本没解析出记录，没法把行号换算成记录内位置")
        for record_no, rec in enumerate(res_before.rows, 1):
            at = rec.raw_at or ([rec.source_line + k for k in range(len(rec.raw_lines))]
                                if rec.source_line is not None else [])
            if abs_i in at:
                index = at.index(abs_i)
                break
        else:
            raise InputError(
                f"第 {abs_i + 1} 行没有归属任何已解析记录",
                hint="先看 task check --lines 20 的归属列，选择记录中的非空行")
        print(f"第 {abs_i + 1} 行 → 第 {record_no} 条记录的第 {index + 1} 行")

    fix = taskflow.Fix(field=args.field, kind="line" if marked else args.kind, index=index,
                       col=args.col, pattern=args.pattern, type=args.type)
    after = fix.apply(before)

    new_fields = [after if f.name == args.field else f for f in card.fields]
    patched = Card(**{**card.__dict__, "fields": new_fields})
    res_after, _ = parse_capture(cap, patched)

    print(f"改「{args.field}」：{taskflow.describe_fix(before, after)}")
    print(f"在这份样本上的效果（{len(res_before.rows)} 条）：")
    for line in taskflow.show_effect(res_before, res_after, args.field,
                                     limit=args.rows):
        print(line)
    cons = res_after.check_conservation()
    print(f"  {'✗ ' + cons if cons else '✓ 行数守恒没破'}")

    if not args.write:
        print("\n没有写回卡文件。看完确认无误，加 --write 保存这一次修正。")
        return OK

    _rewrite_field(args.card, after, cap=cap, expected=res_after)
    tasks.record_fix(tasks.stats_path(_card_path_for_stats(card, args.card)),
                     field=args.field, kind=args.kind or "rule",
                     detail=taskflow.describe_fix(before, after))
    print(f"\n已写回：{args.card}")
    print("  下次 task run / harvest 就会用这条新规则。")
    return OK


def _rewrite_field(card_path: str, spec, *, cap=None, expected=None) -> None:
    """Replace exactly one `[fields.X]` block, leaving every other byte alone.

    Rewriting the whole file from a parsed structure would drop the comments and
    the user's formatting - and the file being edited by hand is the normal
    case, not the exception.
    """
    # Find the block by the *name it carries*, not by the spelling this tool
    # would have chosen. `[fields.'金额']` and `[fields."金额"]` name the same
    # field, and matching a canonical form meant a hand-written card using
    # double quotes could not be edited at all.
    head_re = re.compile(
        r"""^\s*\[\s*fields\s*\.\s*(?:"((?:[^"\\]|\\.)*)"|'([^']*)'|([A-Za-z0-9_-]+))\s*\]""")
    def matches(line):
        import tomllib
        if not head_re.match(line):
            return False
        try:
            return spec.name in tomllib.loads(line).get("fields", {})
        except tomllib.TOMLDecodeError:
            return False
    _rewrite_section(card_path, matches, emit.field_block(spec, head=False),
                     cap=cap, expected=expected)


def _rewrite_section(card_path, matches, content, *, cap=None, expected=None):
    """Rewrite one section; validate both syntax and the sample's parsed result."""
    p = Path(resolve_card(card_path).path or card_path)
    # Bytes, not text mode: `read_text` normalises \r\n to \n and `write_text`
    # turns every \n back into \r\n, so a card that was LF-only came back fully
    # CRLF after one fix -- every line "changed" in git, and the promise above
    # ("leaving every other byte alone") was false. The line ending is part of
    # the file too.
    raw = p.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        raise InputError(f"{p}: 不是 UTF-8 文本，不能安全改写",
                         hint="任务卡必须是 UTF-8") from e
    eol = "\r\n" if "\r\n" in text else "\n"
    if "\r\n" in text and text.replace("\r\n", "").count("\n"):
        # Mixed endings: rewriting one block would have to guess, and a guess
        # here silently reformats the rest of the file.
        raise InputError(f"{p}: 卡里同时有 CRLF 和 LF 两种换行，不能安全改写",
                         hint="先把整个文件的换行统一，再改字段")
    lines = text.splitlines()
    start = None
    for i, ln in enumerate(lines):
        if matches(ln):
            start = i
            break
    if start is None:
        # Renamed field, or the block is spelled differently. Appending is
        # wrong (it would duplicate the field), so refuse.
        raise InputError("在文件里没找到要修改的段落")

    end = len(lines)
    for j in range(start + 1, len(lines)):
        if lines[j].lstrip().startswith("["):
            end = j
            break

    out = (lines[:start]
           + [lines[start]]          # keep the user's own spelling of the key
           + content
           + [""] + lines[end:])
    body = eol.join(out) + (eol if text.endswith(("\n", "\r")) else "")
    new_bytes = body.encode("utf-8")

    # Read it back before keeping it. A fix that produces a card this tool
    # cannot parse is worse than no fix: the user's next command fails, and the
    # file they were told "已写回" no longer opens.
    import tomllib
    from .cards.schema import parse_card
    try:
        saved = parse_card(tomllib.loads(body))
        if cap is not None and expected is not None:
            actual, _ = parse_capture(cap, saved)
            for key in ("fields", "rows", "unparsed", "deviations", "deviation_rows",
                        "total_lines", "consumed_lines"):
                if getattr(actual, key) != getattr(expected, key):
                    raise InputError(f"保存后 {key} 与预览不一致")
    except (tomllib.TOMLDecodeError, Clip2TableError) as e:
        raise InputError(f"改完之后这张卡读不回来了，所以没有写：{e}",
                         hint="这是工具的缺陷，不是你的输入问题，请报告")

    # Replace in one step. A half-written card (disk full, process killed) would
    # lose rules the user cannot recover -- the old bytes are all they have.
    tmp = p.with_name(p.name + ".tmp")
    try:
        tmp.write_bytes(new_bytes)
        os.replace(tmp, p)
    except OSError as e:
        tmp.unlink(missing_ok=True)
        raise InputError(f"{p}: 写不进去（{e.strerror or e}）",
                         hint="检查文件是否只读、磁盘是否已满") from e


def _card_path_for_stats(card, as_given: str) -> Path:
    """The path a task's record file is derived from.

    Always the card. `card.path` is empty for a built-in card, which has no
    writeable home; falling back to what the user typed keeps the record next to
    whatever they named. Four call sites need to agree on this or the record is
    written somewhere nobody reads it.
    """
    return Path(card.path) if getattr(card, "path", "") else Path(as_given)


def _task_run(args, tasks) -> int:
    """Run a saved task again.

    A task that can reach its own source harvests on its own. Reading whatever
    happens to be on the clipboard is a different thing wearing the same name:
    the card says where its data lives, so a run that ignores that is a run the
    user has to babysit -- copy, switch, run, repeat for every page.

    Three inputs, in order of how much of the task they use:

    - `-d` and nothing else: harvest every page the card and `--max-pages` ask
      for, committing each page as it lands. This is the real run.
    - `-d` plus an explicit file/URL: import that one page into the dataset and
      stop. Useful for a backlog of saved pages.
    - an explicit file/URL with no `-d`: parse one sample and print it. A
      preview is not a run, so nothing is recorded.
    """
    import time

    card = resolve_card(args.card)
    started = time.monotonic()
    dataset = getattr(args, "dataset", None)

    if dataset:
        import io
        from contextlib import redirect_stdout, nullcontext
        output = io.StringIO()
        try:
            with redirect_stdout(output) if args.json else nullcontext():
                rc = (cmd_run(args) if args.text_file or args.html_file or args.url
                      else cmd_harvest(args))
        except Clip2TableError as exc:
            if not args.dry_run:
                tasks.record_run(tasks.stats_path(_card_path_for_stats(card, args.card)),
                                 rows=0, pages=0, ok=False,
                                 seconds=time.monotonic() - started,
                                 channel=card.channel, note=exc.message)
            raise
        # A preview must not touch the record. `--dry-run` returned 0 and left a
        # "this is run 1, it took no time" line behind, which made the next real
        # run run number 2 and put a figure nobody measured into the report.
        if args.dry_run:
            if args.json:
                print(output.getvalue(), end="")
            return rc
        p = tasks.stats_path(_card_path_for_stats(card, args.card))
        rows = pages = 0
        if rc == OK:
            try:
                from .dataset import Dataset
                ds = Dataset(Path(dataset)).open()
                rows = len(ds.as_rows())
                pages = len(ds.committed_pages())
            except Clip2TableError:
                pass
        summary = tasks.record_run(
            p, rows=rows, pages=pages, ok=(rc == OK),
            seconds=time.monotonic() - started, channel=card.channel)
        if args.json:
            report = json.loads(output.getvalue()) if output.getvalue().strip() else {}
            report["task_stats"] = summary
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return rc
        for line in tasks.describe(summary):
            print(line)
        if not summary.get("recorded", True):
            # The pages are already committed; the note about them is not worth
            # a non-zero exit, but silence would be a lie.
            print(f"· 任务记录没能写入 {p}（目录只读或被占用），这次运行没有记进台账")
        return rc

    if args.text_file or args.html_file or args.url:
        # One sample, no dataset: parse it, but do not record it as a run - the
        # track record would then mix previews with real harvests.
        return _task_run_sample(args, card, tasks, started)

    raise InputError(
        "task run 需要 -d 指定累积目录（不给文件就从卡片自己声明的来源采），"
        "或者给出 --text-file 看一份样本",
        hint=f"clip2table task run -p {args.card} -d out/{card.name}")


def _task_run_sample(args, card, tasks, started) -> int:
    """Parse one sample and print the report. Deliberately records nothing.

    A sample is what `task check` is for. Counting it as a run would put a
    "first run took 0 seconds" line into the record that `task show` prints,
    and the one number this project promised to report -- how long the *first
    real* run took -- would be a lie from the day the task was created.
    """
    cap, why = _capture_from_args(args)
    res, inf = parse_capture(cap, card)
    rep = Report(captured=cap, result=res, card=card.name,
                 engine=_engine_label(cap), extra={"field_specs": list(card.fields)})
    if why:
        rep.extra["channel_decision"] = why
    _emit(rep, args)
    return OK


def cmd_test(args) -> int:
    from .cards.regress import test_card
    card = resolve_card(args.card)
    rep = test_card(card)
    if args.json:
        print(json.dumps({
            "card": rep.card,
            "samples_dir": rep.samples_dir,
            "passed": rep.passed,
            "failed": rep.failed,
            "ok": rep.ok,
            "samples": [{
                "file": r.path.name,
                "ok": r.ok,
                "rows": r.rows,
                "expected_rows": r.expected_rows,
                "problems": r.problems,
                "failures": [{"record": f.index, "field": f.field,
                              "expected": f.expected, "actual": f.actual}
                             for f in r.failures],
            } for r in rep.results],
        }, ensure_ascii=False, indent=2))
    else:
        print(rep.render())
    return OK if rep.ok else BAD_INPUT


def cmd_cards(args) -> int:
    if args.cards_cmd == "list":
        builtin = list_builtin()
        if not builtin:
            print("没有内置卡。")
            return OK
        print(f"内置卡（{len(builtin)} 张，目录 {builtin[0].parent}）：")
        for p in builtin:
            try:
                c = load_card(p)
                print(f"  {c.name:<24} {c.channel or '自动':<8} "
                      f"{len(c.fields)} 字段  {p.name}")
            except InputError as e:
                print(f"  ! {p.name}: {e}")
        return OK

    if args.cards_cmd == "show":
        card = resolve_card(args.card)
        print(f"# {card.name}  ({card.path})")
        print(f"channel   = {card.channel or '自动'}")
        print(f"segment   = {card.segment_mode}", end="")
        if card.segment_mode == "anchor":
            print(f"  anchor={card.anchor!r}  anchor_next={card.anchor_next!r}")
        else:
            print()
        print(f"paging    = {card.paging_style}"
              + (f"  next={card.paging_next!r}" if card.paging_next else ""))
        print(f"dedupe    = {card.dedupe_key or '（不去重）'}")
        print("fields    :")
        for f in card.fields:
            print(f"  - {f.name:<16} kind={f.kind:<7} type={f.type}")
        return OK

    if args.cards_cmd == "validate":
        card = resolve_card(args.card)
        print(f"✓ {card.name} 合法（{len(card.fields)} 字段，"
              f"切分方式 {card.segment_mode}）")
        # A card that points at samples it cannot test is only half valid:
        # `validate` says fine, `test` immediately errors. Say it here.
        if card.samples:
            from pathlib import Path as _P
            base = _P(card.path).parent if card.path else _P.cwd()
            d = (base / card.samples).resolve()
            if not d.is_dir():
                raise InputError(
                    f"{card.path}: samples 指向的目录不存在：{d}",
                    hint='要么建这个目录并放样本，要么删掉 samples 这一行')
            inps = [p for p in d.iterdir()
                    if p.suffix.lower() in (".txt", ".html", ".htm")]
            if not inps:
                print(f"  ⚠ samples 目录里没有样本：{d}")
            else:
                missing = [p.name for p in inps
                           if not p.with_name(p.stem + ".expect.tsv").is_file()]
                print(f"  samples = {card.samples}（{len(inps)} 个样本）")
                if missing:
                    print(f"  ⚠ 缺期望文件：{', '.join(missing)}"
                          f" —— 这些样本会被 `test` 判为失败")
        return OK

    print("用法：clip2table cards list|show|validate", file=sys.stderr)
    return BAD_INPUT


def cmd_doctor(args) -> int:
    ok = True
    print(f"clip2table {__version__}")
    print(f"Python      {sys.version.split()[0]}")
    if sys.version_info < (3, 11):
        print("  ✗ 需要 Python ≥ 3.11（tomllib 在标准库里）")
        ok = False
    else:
        print("  ✓ 版本满足 ≥3.11")

    try:
        from .channels.clipboard import available_formats, read_clipboard, sequence_number
        seq = sequence_number()
        fmts = available_formats()
        names = [n for _, n in fmts]
        print(f"剪贴板      序列号 {seq}，{len(fmts)} 种格式：{', '.join(names[:8])}"
              + (" …" if len(names) > 8 else ""))
        try:
            cap = read_clipboard()
        except ChannelError:
            # An empty clipboard is a normal state, not a broken install. doctor
            # answers "can this machine run the tool" -- and it can: the API
            # answered, it just had nothing in it. Treating this as a failure
            # made `doctor` exit 2 at random, depending on what the user last
            # copied, which is the worst kind of flaky check.
            print("  · 可读，但剪贴板里现在没有文字（正常状态，先复制点什么再跑 run）")
            raise _ClipboardEmpty()
        print(f"  ✓ 可读，kind={cap.kind}，{len(cap.payload or '')} 字符")
        # The format table carries Windows' names ("HTML Format"), not our
        # constant names (CF_HTML). Comparing the wrong one made doctor print
        # "no HTML here" directly under a line reporting kind=html.
        if cap.kind != "html":
            print("  · 当前剪贴板没有 HTML（不是问题：纯文本同样能入表）")
    except _ClipboardEmpty:
        pass
    except Clip2TableError as e:
        print(f"  ✗ {e.message}")
        if e.hint:
            print(f"    提示：{e.hint}")
        ok = False

    builtin = list_builtin()
    print(f"内置卡      {len(builtin)} 张", end="")
    if builtin:
        bad = 0
        for p in builtin:
            try:
                load_card(p)
            except InputError as e:
                bad += 1
                print(f"\n  ✗ {p.name}: {e}", end="")
        print("（全部合法）" if not bad else "（有非法卡）")
    else:
        print()

    try:
        import tomllib  # noqa: F401
        print("TOML        ✓ 标准库 tomllib")
    except ImportError:
        print("TOML        ✗ 缺 tomllib")
        ok = False

    # The same table the schema validates against, printed so that "the card
    # declared dom" and "the validator rejected dom" are visibly the same rule
    # rather than two independently maintained opinions.
    from .capabilities import describe, unsupported_kinds
    dead = unsupported_kinds()
    print(f"取数原语    {len(describe())} 种，未实现 {len(dead)} 种（{', '.join(dead) or '无'}）")
    for line in describe():
        print(f"  {line}")

    ok = _doctor_debug_port() and ok

    return OK if ok else BAD_INPUT


def _doctor_debug_port() -> bool:
    """Is there a browser we can drive? Say which, or say what to paste.

    This is the difference between "harvest doesn't work" and "my browser wasn't
    started with a debugging port". Without this line the user's only clue is a
    connection timeout, which suggests a bug where there is a missing flag.
    """
    from .channels import cdp

    port = cdp.find_debug_port()
    if port is None:
        print("调试端口    未发现（浏览器没带 --remote-debugging-port 启动）")
        for line in cdp.hint_for_port().splitlines():
            print(f"  {line}")
        print("  · 只读剪贴板的话不受影响：run 只用剪贴板，不需要端口")
        return True          # not a defect in the tool, and clipboard still works

    ver = cdp.browser_version(port)
    pages = cdp.usable_pages(port)
    print(f"调试端口    {port} ✓ {ver or '(版本未知)'}，{len(pages)} 个可采页面")
    if not pages:
        print("  · 端口开着但没有普通网页标签，打开目标页面后重试")
        return True
    for t in pages[:6]:
        title = str(t.get("title") or "(无标题)")[:44]
        print(f"    · {title}  —  {str(t.get('url'))[:70]}")
    if len(pages) > 1:
        print("  · 多个标签时 harvest 不猜，用卡里 [harvest] url_contains 指定")
    return True


def _selftest_doc_paths() -> list[Path]:
    """Every file whose command examples must match the real parser.

    SKILL.md alone was not enough: the usage doc told people to run
    `clip2table cards test`, which has never existed. A command in a doc is a
    promise, and a promise nobody checks rots.

    `docs/verification.md` is deliberately NOT in this list. It is the record of
    what was wrong, so it has to be able to quote a command that never existed
    ("usage.md used to say `clip2table cards test`"). Scanning it would make the
    record of the bug fail the check for the bug.
    """
    root = Path(__file__).resolve().parents[2]
    return [root / "SKILL.md", root / "README.md",
            root / "docs" / "usage.md", root / "docs" / "architecture.md",
            root / "docs" / "design-notes.md"]


def cmd_selftest(args) -> int:
    """Every `clip2table <sub>` in the docs must exist in the parser.

    Two-word forms are checked too: `clip2table cards test` was in usage.md for
    a while, and `cards` alone is real, so a first-word-only check would have
    passed it. The doc was telling people to run a command that never existed.

    Finding no docs at all is a failure. It happens whenever this runs from an
    installed copy, since a wheel carries code and not documentation -- and the
    old behaviour there was to print "跳过对账" and exit 0, i.e. to report
    success for having checked nothing.
    """
    parser = build_parser()
    known: set[str] = set()
    subsub: dict[str, set[str]] = {}
    for action in parser._actions:
        if not isinstance(action, argparse._SubParsersAction):
            continue
        known = set(action.choices)
        for name, sub in action.choices.items():
            for inner in sub._actions:
                if isinstance(inner, argparse._SubParsersAction):
                    subsub[name] = set(inner.choices)

    import re
    scanned: list[str] = []
    used: set[str] = set()
    missing: dict[str, list[str]] = {}
    for path in _selftest_doc_paths():
        if not path.exists():
            continue
        scanned.append(path.name)
        text = path.read_text(encoding="utf-8", errors="replace")
        # Drop the YAML frontmatter first. It is metadata, not instructions, and
        # it ends with "name: clip2table" -- which the command regex below
        # happily reads as "clip2table description" because \s+ crosses the
        # newline.
        if text.startswith("---"):
            end = text.find("\n---", 3)
            if end != -1:
                text = text[end + 4:]
        bad: set[str] = set()
        for m in re.finditer(r"clip2table[ \t]+([a-z][a-z0-9-]*)(?:[ \t]+([a-z][a-z0-9-]*))?", text):
            first, second = m.group(1), m.group(2)
            if first in ("csv", "md", "jsonl", "xlsx"):
                continue                     # bare filenames, not commands
            used.add(first)
            if first not in known:
                bad.add(first)
                continue
            # Only judge the second word when this subcommand has its own
            # sub-actions; otherwise "clip2table run --card x.toml" would have
            # its flag value read as a command name.
            if second and first in subsub and second not in subsub[first]:
                bad.add(f"{first} {second}")
        if bad:
            missing[path.name] = sorted(bad)

    if missing:
        for name, bad in missing.items():
            print(f"✗ {name} 提到不存在的子命令：{bad}")
        return BAD_INPUT
    if not scanned:
        # Not a pass. This used to print "· 没找到任何文档，跳过对账" and return 0,
        # which meant `clip2table selftest` on an installed copy (the wheel ships
        # code, not docs) cheerfully reported success for having checked nothing.
        # A check that skips itself when it cannot see its inputs is the exact
        # failure mode this command exists to catch.
        looked = "、".join(p.as_posix() for p in _selftest_doc_paths())
        print(f"✗ 没有找到任何可以核对的文档，selftest 什么也没验证")
        print(f"  找过：{looked}")
        print("  · selftest 校验的是仓库里的文档，请在源码仓库根目录运行")
        print("  · 装好的副本里没有这些文档，所以这里不能算通过")
        return BAD_INPUT
    print(f"✓ {'、'.join(scanned)} 里的 {len(used)} 个子命令全部存在：{sorted(used)}")
    return OK


def _infer_block(inf, args) -> list[str]:
    """What infer saw, and what else it could have seen.

    Order matters here. The sample comes first because it is what the user can
    judge, then the evidence for the chosen reading, then the competing one.
    A tool that prints only the winner is claiming a certainty that a heuristic
    over pasted text does not have.
    """
    out = sample_block(inf.result, getattr(args, "show", 3))
    if inf.evidence:
        out.append("")
        out.append("依据：")
        out.extend("  · " + e for e in inf.evidence)
    if inf.alternates:
        out.append("")
        n = len(inf.alternates)
        out.append(f"这段文本还能切出另外 {n} 种结果，工具选了其中一种，"
                   f"但没证据说另外 {n} 种是错的：")
        for a in inf.alternates:
            out.append(f"  · {a.line()}")
            if a.sample:
                first = " ／ ".join(x for x in a.sample[0] if x)[:100]
                out.append(f"      第 1 条会是这样：{first}")
        out.append("  看见更像的那种，用 -p 写一张卡把边界固定下来，"
                   "别让每次跑都重新猜。")
    return out


def draft_card_toml(inf, cap: Captured) -> str:
    """The card draft infer hands back: a runnable card, not a sketch.

    Two things here are load-bearing and were both wrong in the first cut:

    Field names must be emitted as bare keys only when they are legal bare
    keys. `[fields.日期]` is invalid TOML, so an inferred Chinese column name
    produced a file that could not be re-read by the tool that wrote it.

    `anchor_next` must be a real second-line rule. `'.+'` matches any line,
    which reduces the two-line agreement back to a single-line anchor - the
    exact rule this project proved wrong. When infer cannot name a precise
    second-line shape it emits the empty string and says why, rather than
    shipping a rule that looks stricter than it is.
    """
    lines = [
        "schema = 1",
        f'name   = "inferred-{inf.level.lower()}"',
        "",
        "[segment]",
    ]
    if cap.formats and "CF_HTML" in cap.formats and inf.level != "S0":
        lines.append('mode = "blank"')
    elif inf.level == "S0":
        # A table row is already a record. `single` would glue every row of
        # the pasted table into one record whose first cell is the header.
        lines.append('mode = "lines"')
    else:
        second = _second_line_rule(inf)
        lines.append('mode = "anchor"')
        lines.append(f"anchor      = {_toml_key_str(_anchor_rule(inf))}")
        if second:
            lines.append(f"anchor_next = {_toml_key_str(second)}")
        else:
            lines.append("# anchor_next 推断不出来：这版先用单条规则切，"
                         "切分结果务必用 --show 看过再落盘")
    lines.append("")
    for f in inf.specs:
        lines.append(f"[fields.{_toml_key(f.name)}]")
        lines.append(f'kind = "{f.kind}"')
        if f.kind == "cell":
            lines.append(f"col  = {f.col}")
        elif f.kind == "line":
            lines.append(f"index = {f.index}")
        elif f.pattern:
            lines.append(f"pattern = {_toml_key_str(f.pattern)}")
        lines.append("")

    named = [s.name for s in inf.specs]
    lines.append("[dedupe]")
    lines.append("key = [" + ", ".join(_toml_key_str(n) for n in named) + "]")
    lines.append('on_collision = "report_only"')
    lines.append("")
    return "\n".join(lines)


_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


def _toml_key(name: str) -> str:
    """Bare when legal, quoted when not. `fields.日期` is not valid TOML."""
    if _BARE_KEY.match(name):
        return name
    return _toml_key_str(name)


def _toml_key_str(s: str) -> str:
    """TOML literal (single-quoted) string: no escape processing at all.

    Every value here is a regex, and regexes are full of backslashes. A
    basic (double-quoted) string would need every one of them doubled.
    """
    if "'" in s:
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return "'" + s + "'"


def _anchor_rule(inf) -> str:
    """The first-line rule, derived from what the records actually look like."""
    width = len(inf.result.fields)
    if width <= 1:
        return r"^\s*\S"
    return r"^(?!\s).{1,40}$"


def _second_line_rule(inf) -> str:
    """A rule for line two that is true of THIS data, not true of everything.

    The draft is generated from records infer already segmented, so the second
    line of every record is known. Anchoring on that shared shape is a real
    constraint; falling back to '.+' is not one.
    """
    lines = _second_lines(inf)
    if len(lines) < 3:
        return ""
    sigs = []
    for probe, rule in (
        (r"^\d{4}\s*[-/.年]\s*\d{1,2}\s*(?:[-/.月]\s*\d{1,2}\s*[日号]?)?"
         r"(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?\s*$", "date-only"),
        (r"^[^:：]{1,18}[:：]", "kv"),
        (r"^\s*(?:[·•*\-]|\d{1,3}[.、])", "bullet"),
        (r"^.{1,24}$", "short"),
    ):
        if all(re.search(probe, ln) for ln in lines):
            sigs.append(rule)
            break
    return {
        "date-only": (r"^\d{4}\s*[-/.年]\s*\d{1,2}\s*"
                      r"(?:[-/.月]\s*\d{1,2}\s*[日号]?)?"
                      r"(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?\s*$"),
        "kv": r"^[^:：]{1,18}[:：]",
        "bullet": r"^\s*(?:[·•*\-]|\d{1,3}[.、])",
        "short": r"^.{1,24}$",
    }.get(sigs[0] if sigs else "", "")


def _second_lines(inf) -> list[str]:
    out = []
    for r in inf.result.rows:
        if len(r.raw_lines) >= 2:
            out.append(r.raw_lines[1].strip())
    return out


# ------------------------------------------------------------------ main

def _force_utf8_stdout() -> None:
    """Windows consoles default to GBK/CP936, which cannot encode ✓ ✗ ∅.

    Without this, the tool crashes on its own report - the most embarrassing
    possible failure mode: data parsed correctly, then printing it throws.
    """
    for stream in (sys.stdout, sys.stderr):
        enc = (getattr(stream, "encoding", "") or "").lower()
        if enc.replace("-", "").replace("_", "") in ("utf8", "utf8sig"):
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError, OSError):
            pass


def main(argv: list[str] | None = None) -> int:
    _force_utf8_stdout()
    argv = sys.argv[1:] if argv is None else argv
    parser = build_parser()

    # No arguments at all is the common case: read the clipboard, print a table.
    if not argv:
        argv = ["run"]

    args = parser.parse_args(argv)
    try:
        if args.cmd in (None, "run"):
            if args.cmd is None:
                args = parser.parse_args(["run"])
            return cmd_run(args)
        if args.cmd == "harvest":
            return cmd_harvest(args)
        if args.cmd == "infer":
            return cmd_infer(args)
        if args.cmd == "export":
            return cmd_export(args)
        if args.cmd == "cards":
            return cmd_cards(args)
        if args.cmd == "test":
            return cmd_test(args)
        if args.cmd == "task":
            return cmd_task(args)
        if args.cmd == "doctor":
            return cmd_doctor(args)
        if args.cmd == "selftest":
            return cmd_selftest(args)
        parser.print_help()
        return BAD_INPUT
    except Clip2TableError as e:
        print(f"✗ {e.message}", file=sys.stderr)
        if e.hint:
            print(f"  {e.hint}", file=sys.stderr)
        return e.exit_code
    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        return BAD_INPUT


if __name__ == "__main__":
    raise SystemExit(main())
