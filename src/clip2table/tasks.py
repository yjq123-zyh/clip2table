"""tasks: what makes a card a *task*, and what a task remembers about itself.

A card is rules. A task is rules plus a reason and a track record: what the
user wanted, which sample proved it, how long the first run took, how many
times they had to fix a rule, and whether the second run needed them at all.

The record lives in a JSON file, never in the card. The card is hand-edited
TOML with comments; rewriting it on every run would destroy that, and a tool
that quietly reformats the file you just edited is a tool you stop editing.

Nothing here is required for parsing. A card with no task block and no stats
file behaves exactly as it did before this module existed.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STATS_NAME = "task-stats.json"


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def stats_path(where: str | Path) -> Path:
    """Where a task's record file goes: beside the card, named after it.

    One task, one record. It used to be that a run into a dataset directory was
    recorded *in that directory*, while fixes were recorded beside the card --
    so `task show` read an empty file, and "how many reruns needed no fix" could
    never be computed because the fixes were in the other file. The record is
    about the task, not about one output directory, so it lives with the card.

    `is_dir()` alone is not enough: a dataset directory usually does not exist
    yet on the first run. The tell is the suffix -- a card is `orders.toml`, a
    directory is `out/orders`.
    """
    p = Path(where)
    if p.is_dir() or not p.suffix:
        return p / STATS_NAME
    return p.with_name(p.stem + ".stats.json")


def load(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    if not p.is_file():
        return {"runs": [], "fixes": []}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # A corrupt stats file must never stop a harvest. The run is the
        # valuable thing; the bookkeeping is not worth failing over.
        return {"runs": [], "fixes": [], "unreadable": True}
    if not isinstance(data, dict):
        return {"runs": [], "fixes": []}
    data.setdefault("runs", [])
    data.setdefault("fixes", [])
    return data


def save(path: str | Path, data: dict[str, Any]) -> bool:
    """Write the record. False when it could not be written -- never raises.

    A failed write used to escape as `PermissionError` from `os.replace` and take
    the whole command down, after the harvest had already committed its pages.
    The pages are the product; this file is a note about them. Losing the note
    is worth a line of warning, not a non-zero exit.
    """
    p = Path(path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                       encoding="utf-8")
        os.replace(tmp, p)
        return True
    except OSError:
        return False


def record_run(path: str | Path, *, rows: int, pages: int, ok: bool,
               seconds: float | None = None, channel: str = "",
               note: str = "", run_id: str = "", request_id: str = "") -> dict[str, Any]:
    """Append one run. Returns the summary that gets printed after it.

    `seconds` is how long *this* run took, and it is kept for every run: later
    runs get slower as the table grows, and a single first-run figure would
    describe a table that no longer exists. That is a different number from the
    one the project promises ("首次配置到确认正确的耗时"), which cannot be known
    until the user says the output is right -- `record_confirm` records that.
    """
    data = load(path)
    previous = next((r for r in data["runs"] if run_id and r.get("run_id") == run_id), None)
    if previous and request_id in previous.get("requests", []):
        summary = summarise(data)
        summary["recorded"] = True
        return summary
    entry = {"at": _now(), "rows": int(rows), "pages": int(pages),
             "ok": bool(ok), "channel": channel,
             # How many fixes existed when this run started. Comparing this
             # number between runs is how "used again without a fix" is
             # counted -- comparing timestamps instead would break whenever a
             # fix and a run land in the same second, which they usually do.
             "fixes": len(data.get("fixes") or [])}
    if seconds is not None:
        entry["seconds"] = round(float(seconds), 2)
    if note:
        entry["note"] = note
    if run_id:
        entry.update(run_id=run_id, requests=list((previous or {}).get("requests", [])) + ([request_id] if request_id else []))
    if previous:
        if not note:
            previous.pop("note", None)
        entry["at"] = previous["at"]
        entry["fixes"] = previous["fixes"]
        entry["seconds"] = round(float(previous.get("seconds", 0)) + float(seconds or 0), 2)
        previous.update(entry)
    else:
        data["runs"].append(entry)
    summary = summarise(data)
    summary["recorded"] = save(path, data)
    return summary


def record_confirm(path: str | Path, *, rows: int = 0,
                   run: int | None = None, card_hash: str = "",
                   sample_hash: str = "") -> dict[str, Any]:
    """The user says this output is right. The clock stops here.

    "Configured to confirmed correct" is the only figure that describes the
    actual cost of a new task: creating the card, fixing rules, re-reading the
    preview. It is measured from `created.at`, so a task whose card was made
    somewhere else simply has no such figure rather than a wrong one.
    """
    data = load(path)
    created = data.get("created") or {}
    setup: float | None = None
    started = created.get("at") if isinstance(created, dict) else None
    if started:
        try:
            delta = datetime.fromisoformat(_now()) - datetime.fromisoformat(str(started))
            setup = round(delta.total_seconds(), 1)
        except ValueError:
            setup = None
    entry: dict[str, Any] = {"at": _now(), "rows": int(rows)}
    if card_hash:
        entry["card_hash"] = card_hash
    if sample_hash:
        entry["sample_hash"] = sample_hash
    if run is not None:
        entry["run"] = int(run)
    if setup is not None:
        entry["setup_seconds"] = setup
    data.setdefault("confirmations", []).append(entry)
    summary = summarise(data)
    summary["recorded"] = save(path, data)
    return summary


def record_fix(path: str | Path, *, field: str, kind: str, detail: str = "") -> bool:
    """Count one rule correction. False when the record could not be written.

    A "fix" is the user changing a rule because the output was wrong. It is the
    number this project cares about most -- the whole design goal is that it
    goes down -- so it is recorded even when the fix arrives from an edit the
    tool did not perform.
    """
    data = load(path)
    data["fixes"].append({"at": _now(), "field": field, "kind": kind,
                          **({"detail": detail} if detail else {})})
    return save(path, data)


def summarise(data: dict[str, Any]) -> dict[str, Any]:
    """Task timing, corrections and reuse, from the raw log.

    Three different questions, deliberately kept apart:

    - `first_seconds` / `last_seconds`: how long the tool took. Mechanical.
    - `setup_seconds`: how long the *user* took to get from "made a task" to
      "this output is right". That is the cost of a new task, and it is the only
      one that includes fixing rules and re-reading previews.
    - `clean_reruns`: run two or later with no rule correction in between. Note
      what this measures -- that nobody had to *touch the rules* -- not that the
      output was correct. A user who never confirms anything still scores well
      here, which is why `confirmed` is reported alongside it.
    """
    runs = [r for r in data.get("runs") or [] if isinstance(r, dict)]
    fixes = [f for f in data.get("fixes") or [] if isinstance(f, dict)]
    confirms = [c for c in data.get("confirmations") or [] if isinstance(c, dict)]
    # Only entries that actually measured it. `max(... or 0)` would turn
    # "we never knew when the card was made" into "it took 0 seconds", which
    # reads as a perfect score instead of a missing one.
    setups = [c["setup_seconds"] for c in confirms
              if isinstance(c.get("setup_seconds"), (int, float))]
    first = runs[0] if runs else {}
    last = runs[-1] if runs else {}
    return {
        "runs": len(runs),
        # Kept for records written before every run carried its timing.
        "first_seconds": first.get("seconds"),
        "last_seconds": last.get("seconds"),
        "setup_seconds": setups[0] if setups else None,
        "confirmed": len(confirms),
        "fixes": len(fixes),
        "last_rows": last.get("rows") if runs else None,
        # "Used again without a fix" is the success figure: run two or later
        # that needed zero corrections since the previous run.
        "clean_reruns": _clean_reruns(runs, fixes),
    }


def _clean_reruns(runs: list[dict[str, Any]], fixes: list[dict[str, Any]]) -> int:
    if len(runs) < 2:
        return 0
    clean = 0
    for prev, cur in zip(runs, runs[1:]):
        if not cur.get("ok"):
            continue
        start = prev.get("fixes")
        end = cur.get("fixes")
        if isinstance(start, int) and isinstance(end, int):
            if end > start:
                continue
        elif _fix_between(prev, cur, fixes):
            continue
        clean += 1
    return clean


def _fix_between(prev: dict[str, Any], cur: dict[str, Any],
                 fixes: list[dict[str, Any]]) -> bool:
    """Fallback for old log files that predate the fix counter."""
    start = str(prev.get("at") or "")
    end = str(cur.get("at") or "")
    return any(start < str(f.get("at") or "") <= end for f in fixes)


def _secs(x: float) -> str:
    """Seconds as a person reads them: 0.4 秒, 12 秒, 1 分 04 秒."""
    if x < 1:
        return "不到 1 秒"
    if x < 60:
        return f"{x:.0f} 秒"
    return f"{int(x // 60)} 分 {int(x % 60):02d} 秒"


def describe(summary: dict[str, Any]) -> list[str]:
    """The track-record block for a report. Empty for a task never run."""
    if not summary.get("runs") and not summary.get("confirmed"):
        return []
    bits = []
    if summary.get("runs"):
        bits.append(f"这是第 {summary['runs']} 次运行")
    if summary.get("last_seconds") is not None:
        bits.append(f"这次 {_secs(summary['last_seconds'])}")
    if summary.get("setup_seconds") is not None:
        bits.append(f"从建任务到确认结果正确用了 {_secs(summary['setup_seconds'])}")
    if summary.get("confirmed"):
        bits.append(f"确认过 {summary['confirmed']} 次结果正确")
    if summary.get("fixes"):
        bits.append(f"改过 {summary['fixes']} 次规则")
    elif summary.get("runs"):
        bits.append("还没改过规则")
    if summary.get("clean_reruns"):
        # Worded for what it actually counts. "No fix needed" is evidence the
        # rules held, not evidence the values were right -- the latter needs a
        # confirmation, which is reported above.
        bits.append(f"其中 {summary['clean_reruns']} 次重跑不用改规则")
    if not bits:
        return []
    return ["任务记录：" + "，".join(bits)]
