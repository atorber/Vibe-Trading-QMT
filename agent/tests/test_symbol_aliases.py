from __future__ import annotations

from src.symbol_aliases import (
    normalize_market_data_codes,
    resolve_a_share_index_alias,
    validate_a_share_source,
)


def test_resolve_a_share_index_alias_chinese() -> None:
    assert resolve_a_share_index_alias("沪深300") == ("000300.SH", "沪深300")
    assert resolve_a_share_index_alias("上证指数") == ("000001.SH", "上证指数")


def test_resolve_a_share_index_alias_english() -> None:
    assert resolve_a_share_index_alias("CSI 300") == ("000300.SH", "沪深300")
    assert resolve_a_share_index_alias("000300.SH") == ("000300.SH", "沪深300")


def test_normalize_market_data_codes_resolves_aliases() -> None:
    codes, alias_map = normalize_market_data_codes(["沪深300", "600519.SH"])
    assert codes == ["000300.SH", "600519.SH"]
    assert alias_map["000300.SH"] == "沪深300"


def test_validate_a_share_source_rejects_yfinance_for_indices() -> None:
    err = validate_a_share_source(["000300.SH"], "yfinance")
    assert err is not None
    assert "000300.SH" in err
    assert validate_a_share_source(["000300.SH"], "auto") is None
    assert validate_a_share_source(["AAPL.US"], "yfinance") is None
