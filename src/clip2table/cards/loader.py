"""Card loader: TOML in, Card out. No magic lookup order beyond the obvious."""
from __future__ import annotations

import tomllib
from pathlib import Path

from ..errors import InputError
from .schema import Card, parse_card


def load_card(path: str | Path) -> Card:
    p = Path(path)
    if not p.exists():
        raise InputError(f"找不到卡：{p}", hint="用 `clip2table cards list` 看内置卡")
    if p.suffix.lower() != ".toml":
        raise InputError(f"卡必须是 .toml 文件：{p}")
    try:
        raw = p.read_bytes()
    except OSError as e:
        raise InputError(f"读不了卡 {p}：{e}") from None
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except UnicodeDecodeError:
        raise InputError(f"卡不是 UTF-8：{p}") from None
    except tomllib.TOMLDecodeError as e:
        raise InputError(f"卡不是合法 TOML：{p}\n  {e}") from None
    return parse_card(data, str(p))


def builtin_dir() -> Path:
    return Path(__file__).with_name("builtin")


def list_builtin() -> list[Path]:
    d = builtin_dir()
    if not d.is_dir():
        return []
    return sorted(d.glob("*.toml"))


def resolve_card(name_or_path: str) -> Card:
    """A path, or a builtin card name without the .toml suffix."""
    p = Path(name_or_path)
    if p.exists():
        return load_card(p)
    candidate = builtin_dir() / f"{name_or_path}.toml"
    if candidate.exists():
        return load_card(candidate)
    raise InputError(f"找不到卡 {name_or_path!r}",
                     hint="给一个 .toml 路径，或用 `clip2table cards list` 里的名字")
