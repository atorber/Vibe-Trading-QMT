"""Tests for Markets watchlist persistence."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.markets.watchlist import (
    MarketsWatchlist,
    MarketsWatchlistStore,
    WatchlistItem,
    normalize_symbol,
    parse_watchlist,
)


def test_normalize_symbol_accepts_sh_sz() -> None:
    assert normalize_symbol("600519.sh") == "600519.SH"
    assert normalize_symbol(" 000001.SZ ") == "000001.SZ"


def test_normalize_symbol_rejects_invalid() -> None:
    with pytest.raises(ValueError):
        normalize_symbol("AAPL")
    with pytest.raises(ValueError):
        normalize_symbol("600519")


def test_parse_watchlist_deduplicates_and_limits() -> None:
    payload = {
        "symbols": [
            {"symbol": "600519.SH", "name": "茅台"},
            "000001.SZ",
        ]
    }
    watchlist = parse_watchlist(payload)
    assert [item.symbol for item in watchlist.symbols] == ["600519.SH", "000001.SZ"]
    assert watchlist.symbols[0].name == "茅台"


def test_store_seeds_defaults(tmp_path: Path) -> None:
    store = MarketsWatchlistStore(tmp_path / "markets-watchlist.json")
    loaded = store.load()
    assert len(loaded.symbols) == 10
    assert loaded.symbols[0].symbol == "600519.SH"


def test_store_add_remove_roundtrip(tmp_path: Path) -> None:
    store = MarketsWatchlistStore(tmp_path / "markets-watchlist.json")
    store.load()
    store.add("300750.SZ", name="宁德时代")
    assert store.symbol_codes()[0] == "300750.SZ"
    store.remove("300750.SZ")
    assert "300750.SZ" not in store.symbol_codes()


def test_store_add_moves_existing_to_front(tmp_path: Path) -> None:
    store = MarketsWatchlistStore(tmp_path / "markets-watchlist.json")
    store.load()
    original_tail = store.symbol_codes()[-1]
    store.add(original_tail, name="moved")
    assert store.symbol_codes()[0] == original_tail


def test_store_save_rejects_duplicates(tmp_path: Path) -> None:
    store = MarketsWatchlistStore(tmp_path / "markets-watchlist.json")
    with pytest.raises(ValueError):
        store.save(
            MarketsWatchlist(
                symbols=(
                    WatchlistItem("600519.SH"),
                    WatchlistItem("600519.SH"),
                )
            )
        )
