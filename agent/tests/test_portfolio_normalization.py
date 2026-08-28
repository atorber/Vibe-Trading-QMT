"""Currency inference and valuation for multi-market portfolio positions."""

from __future__ import annotations

from decimal import Decimal

import pytest

from src.portfolio.normalization import normalize_position, value_position


def test_futu_style_hk_prefix_is_valued_in_hkd_not_usd():
    """``HK.00100`` without an explicit currency must not be treated as USD."""
    row = normalize_position(
        "futu",
        {
            "code": "HK.00100",
            "qty": 1000,
            "cost_price": 287.3,
            "market_val": 188760,
            "pl_val": -98536.2,
        },
    )
    assert row["currency"] == "HKD"
    assert row["market"] == "HK"

    valued = value_position(row, usd_hkd=Decimal("7.8"), usd_cny=Decimal("7.2"))
    assert valued["market_value"] == pytest.approx(188760)
    assert valued["market_value_usd"] == pytest.approx(188760 / 7.8)
    assert valued["unrealized_pnl"] == pytest.approx(-98536.2)
    assert valued["unrealized_pnl_usd"] == pytest.approx(-98536.2 / 7.8)


def test_futu_style_us_and_a_share_prefixes():
    us = normalize_position("futu", {"code": "US.MRVL", "qty": 10, "market_val": 1000})
    sh = normalize_position(
        "futu", {"code": "SH.600519", "qty": 100, "market_val": 170000}
    )
    assert us["currency"] == "USD"
    assert us["market"] == "US"
    assert sh["currency"] == "CNY"
    assert sh["market"] == "SH"


def test_longbridge_suffix_still_resolves_hkd():
    row = normalize_position(
        "longbridge",
        {
            "symbol": "700.HK",
            "symbol_name": "Tencent",
            "quantity": 10,
            "cost_price": 300,
            "market": "HK",
            "currency": "HKD",
        },
    )
    assert row["currency"] == "HKD"
    assert row["market"] == "HK"


def test_explicit_usd_on_hk_symbol_is_overridden_to_hkd():
    """Connectors that report account USD must not override HK./.HK instruments."""
    row = normalize_position(
        "futu",
        {
            "code": "HK.00100",
            "qty": 600,
            "market_val": 188760,
            "currency": "USD",
        },
    )
    assert row["currency"] == "HKD"
    assert row["market"] == "HK"
    valued = value_position(row, usd_hkd=Decimal("7.8"), usd_cny=Decimal("6.72"))
    assert valued["market_value"] == pytest.approx(188760)
    assert valued["market_value_usd"] == pytest.approx(188760 / 7.8)
    assert valued["market_value_cny"] == pytest.approx(188760 / 7.8 * 6.72)
