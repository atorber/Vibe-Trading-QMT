"""Persistent user watchlist for the Markets SPA."""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from src.config.paths import get_runtime_root

CONFIG_FILENAME = "markets-watchlist.json"
_SYMBOL_RE = re.compile(r"^\d{6}\.(SH|SZ)$", re.IGNORECASE)
_MAX_SYMBOLS = 50

DEFAULT_SYMBOLS: tuple[str, ...] = (
    "600519.SH",
    "000858.SZ",
    "601318.SH",
    "600036.SH",
    "000001.SZ",
    "300750.SZ",
    "002594.SZ",
    "601012.SH",
    "688981.SH",
    "510300.SH",
)


@dataclass(frozen=True)
class WatchlistItem:
    """One followed A-share symbol with an optional cached display name."""

    symbol: str
    name: str | None = None

    def to_dict(self) -> dict[str, Any]:
        row: dict[str, Any] = {"symbol": self.symbol}
        if self.name:
            row["name"] = self.name
        return row


@dataclass(frozen=True)
class MarketsWatchlist:
    """Ordered watchlist symbols."""

    symbols: tuple[WatchlistItem, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"symbols": [item.to_dict() for item in self.symbols]}


def normalize_symbol(symbol: str) -> str:
    """Validate and canonicalize an A-share code such as ``600519.SH``.

    Raises:
        ValueError: When the code is empty or not ``NNNNNN.SH|SZ``.
    """
    code = str(symbol or "").strip().upper()
    if not _SYMBOL_RE.match(code):
        raise ValueError(f"invalid symbol: {symbol}")
    return code


def parse_watchlist(payload: dict[str, Any]) -> MarketsWatchlist:
    """Validate a watchlist JSON object.

    Raises:
        ValueError: When the payload is malformed or contains duplicates.
    """
    if not isinstance(payload, dict):
        raise ValueError("watchlist root must be an object")
    raw_symbols = payload.get("symbols", [])
    if not isinstance(raw_symbols, list):
        raise ValueError("symbols must be a list")

    items: list[WatchlistItem] = []
    seen: set[str] = set()
    for row in raw_symbols:
        if isinstance(row, str):
            symbol = normalize_symbol(row)
            name = None
        elif isinstance(row, dict):
            symbol = normalize_symbol(str(row.get("symbol") or ""))
            raw_name = row.get("name")
            name = str(raw_name).strip() if raw_name else None
            if name == "":
                name = None
        else:
            raise ValueError("each watchlist entry must be a string or object")
        if symbol in seen:
            raise ValueError(f"duplicate symbol: {symbol}")
        seen.add(symbol)
        items.append(WatchlistItem(symbol=symbol, name=name))
        if len(items) > _MAX_SYMBOLS:
            raise ValueError(f"watchlist cannot exceed {_MAX_SYMBOLS} symbols")
    return MarketsWatchlist(symbols=tuple(items))


class MarketsWatchlistStore:
    """Owner-only JSON store for the Markets watchlist."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (get_runtime_root() / CONFIG_FILENAME)

    def load(self) -> MarketsWatchlist:
        """Read the watchlist, seeding defaults on first use."""
        if self.path.exists():
            try:
                payload = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ValueError(f"invalid markets watchlist: {exc}") from exc
            return parse_watchlist(payload)

        seeded = MarketsWatchlist(
            symbols=tuple(WatchlistItem(symbol=code) for code in DEFAULT_SYMBOLS)
        )
        self.save(seeded)
        return seeded

    def save(self, watchlist: MarketsWatchlist | dict[str, Any]) -> MarketsWatchlist:
        """Validate and atomically persist the watchlist."""
        validated = (
            parse_watchlist(watchlist)
            if isinstance(watchlist, dict)
            else parse_watchlist(watchlist.to_dict())
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            prefix=".markets-watchlist-", suffix=".json", dir=self.path.parent
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(validated.to_dict(), handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            os.replace(temporary, self.path)
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return validated

    def symbol_codes(self) -> list[str]:
        """Return ordered symbol codes."""
        return [item.symbol for item in self.load().symbols]

    def add(self, symbol: str, *, name: str | None = None) -> MarketsWatchlist:
        """Append one symbol or move it to the front when already present."""
        code = normalize_symbol(symbol)
        current = list(self.load().symbols)
        label = str(name).strip() if name else None
        if label == "":
            label = None
        current = [item for item in current if item.symbol != code]
        current.insert(0, WatchlistItem(symbol=code, name=label))
        if len(current) > _MAX_SYMBOLS:
            raise ValueError(f"watchlist cannot exceed {_MAX_SYMBOLS} symbols")
        return self.save(MarketsWatchlist(symbols=tuple(current)))

    def remove(self, symbol: str) -> MarketsWatchlist:
        """Drop one symbol from the watchlist."""
        code = normalize_symbol(symbol)
        current = [item for item in self.load().symbols if item.symbol != code]
        return self.save(MarketsWatchlist(symbols=tuple(current)))
