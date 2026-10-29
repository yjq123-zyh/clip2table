"""Compute the order-ledger answers from the input files, without clip2table.

This is the independent half of the S2-08 answer files. It reads the three
sample pages and applies the rules the card states in plain words --

  * one row per 订单号
  * the current row is the observation with the newest 更新时间
  * if two observations share the newest 更新时间 but disagree on a field,
    the order is left *pending* instead of picking one

-- and nothing else. It deliberately does not import clip2table, so a bug in
the ledger cannot make the answer agree with it.

The answer files hold **business facts**, not explanations. Strings such as
"按观察时间选了 2026-10-26T23:16:23.322898 这次" name a timestamp that changes
on every run; putting them in the answer file would make the comparison test
the clock. So `_pending` is a yes/no flag here and the pending table is just
entity + field, and the test projects the tool's output into that same shape.

On a tie the last accepted value stays current and the conflicting observation
is reported separately for a decision.

Usage:  py -3 scenes/order-ledger/make-answers.py            # write output/expected-*.csv
        py -3 scenes/order-ledger/make-answers.py --check    # compare, do not write
"""
from __future__ import annotations

import csv
import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FIELDS = ["订单号", "客户", "金额", "状态", "下单时间", "更新时间"]
TRACKED = ["状态", "更新时间"]          # declared mutable
SORT = "更新时间"
CURRENT_COLS = FIELDS + ["_pending"]
CHANGE_COLS = ["类型", "实体", "字段", "之前", "之后"]
PENDING_COLS = ["实体", "字段"]


def read(page: str) -> list[dict[str, str]]:
    with (ROOT / "input" / page).open(encoding="utf-8") as fh:
        rows = list(csv.reader(fh, delimiter="\t"))
    head = rows[0]
    return [dict(zip(head, r)) for r in rows[1:] if r]


def pick(observations: list[dict[str, str]]) -> tuple[dict[str, str], bool]:
    """Newest 更新时间 wins; a tie with different content is pending."""
    newest = max(o[SORT] for o in observations)
    top = [o for o in observations if o[SORT] == newest]
    first = top[0]
    clash = any(any(o[f] != first[f] for f in FIELDS if f != "订单号")
                for o in top[1:])
    return first, clash


def current(pages: list[str]) -> dict[str, tuple[dict[str, str], bool]]:
    seen: dict[str, list[dict[str, str]]] = {}
    for page in pages:
        for row in read(page):
            seen.setdefault(row["订单号"], []).append(row)
    return {k: pick(v) for k, v in seen.items()}


def csv_bytes(header: list[str], rows: list[list[str]]) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(header)
    w.writerows(rows)
    return ("\ufeff" + buf.getvalue()).encode("utf-8")


def answers() -> dict[str, bytes]:
    two = current(["page-1.txt", "page-2.txt"])
    three = current(["page-1.txt", "page-2.txt", "page-3.txt"])

    cur_rows, pending_rows = [], []
    for key in sorted(three):
        row, is_pending = three[key]
        cur_rows.append([row[f] for f in FIELDS] + ["是" if is_pending else ""])
        if is_pending:
            pending_rows.append([key, "状态"])

    chg_rows = []
    for key in sorted(three):
        now, is_pending = three[key]
        before_row = two.get(key, ({}, False))[0]
        if not before_row:
            chg_rows.append(["new", key, "（整条）", "", "；".join(
                f"{f}={now[f]}" for f in FIELDS)])
            continue
        # A tie is not a change we can vouch for: two observations share the
        # business timestamp and disagree, so "已发货 → 已完成" would be a claim
        # the data does not support. It is reported as pending instead.
        kind = "pending" if is_pending else "updated"
        if is_pending:
            now = next(r for r in read("page-3.txt") if r["订单号"] == key)
        for field in TRACKED:
            if before_row[field] != now[field]:
                chg_rows.append([kind, key, field,
                                 before_row[field], now[field]])

    return {
        "expected-current.csv": csv_bytes(CURRENT_COLS, cur_rows),
        "expected-changes.csv": csv_bytes(CHANGE_COLS, chg_rows),
        "expected-pending.csv": csv_bytes(PENDING_COLS, pending_rows),
    }


def main(argv: list[str]) -> int:
    got = answers()
    check = "--check" in argv
    bad = 0
    for name, data in got.items():
        target = ROOT / "output" / name
        if check:
            same = target.is_file() and target.read_bytes() == data
            print(f"{'OK' if same else 'FAIL'} {name}")
            bad += 0 if same else 1
        else:
            target.write_bytes(data)
            print(f"写入 {target.relative_to(ROOT.parent.parent)}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
