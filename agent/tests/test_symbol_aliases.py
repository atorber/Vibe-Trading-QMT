from __future__ import annotations

from src.symbol_aliases import (
    is_canonical_symbol_query,
    matches_a_share_brief_intent,
    matches_broker_trade_review_intent,
    normalize_market_data_codes,
    resolve_a_share_index_alias,
    resolve_canonical_symbol_query,
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


def test_matches_a_share_brief_intent() -> None:
    assert matches_a_share_brief_intent("分析今日A股市场行情")
    assert matches_a_share_brief_intent("今日大盘行情怎么样")
    assert not matches_a_share_brief_intent("帮我回测一个策略")
    assert not matches_a_share_brief_intent("今日交易复盘")


def test_matches_broker_trade_review_intent() -> None:
    assert matches_broker_trade_review_intent("今日交易复盘")
    assert matches_broker_trade_review_intent("帮我做账户复盘")
    assert not matches_broker_trade_review_intent("分析今日A股市场行情")


def test_resolve_canonical_symbol_query() -> None:
    assert resolve_canonical_symbol_query("300285.SZ") == ("300285.SZ", "300285.SZ")
    assert resolve_canonical_symbol_query("AAPL.US") == ("AAPL.US", "AAPL.US")
    assert resolve_canonical_symbol_query("apple") is None
    assert is_canonical_symbol_query("603986.SH")
