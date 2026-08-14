"""Regression (#735): connector CLI renderers must tolerate broker_sdk schemas.

The shared ``connector positions`` / ``connector account`` renderers were written
for the IBKR result shape (``position``/``avg_cost``/``sec_type``/``summary``).
Longbridge (and other ``broker_sdk`` connectors) return ``quantity``/``cost_price``/
``market``/``balances``, so every non-matching key rendered as an empty cell.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from cli import _legacy

pytestmark = pytest.mark.unit


def test_first_present_keeps_zero_and_skips_none() -> None:
    row = {"position": 0.0, "quantity": 5.0}
    # A real zero position must win over the fallback key, not be skipped.
    assert _legacy._first_present(row, "position", "quantity") == 0.0
    assert _legacy._first_present({"quantity": 5.0}, "position", "quantity") == 5.0
    assert _legacy._first_present({"position": None, "quantity": 5.0}, "position", "quantity") == 5.0
    assert _legacy._first_present({}, "position", "quantity") is None


def test_connector_positions_renders_longbridge_schema(capsys) -> None:
    longbridge_result = {
        "status": "ok",
        "profile_id": "longbridge-paper-trade",
        "positions": [
            {
                "symbol": "AAPL.US",
                "symbol_name": "Apple",
                "quantity": 20.0,
                "available_quantity": 20.0,
                "cost_price": 321.5,
                "currency": "USD",
                "market": "US",
            }
        ],
    }
    with patch("src.trading.service.get_positions", return_value=longbridge_result):
        rc = _legacy.cmd_connector_positions("longbridge-paper-trade")

    assert rc == _legacy.EXIT_SUCCESS
    out = capsys.readouterr().out
    assert "AAPL.US" in out
    assert "20" in out       # quantity → Qty
    assert "321.5" in out    # cost_price → Avg Cost
    assert "US" in out       # market → Type


def test_connector_account_renders_balances_table(capsys) -> None:
    longbridge_account = {
        "status": "ok",
        "profile_id": "longbridge-paper-trade",
        "balances": [
            {
                "currency": "USD",
                "total_cash": 10_000.0,
                "net_assets": 12_345.0,
                "buy_power": 20_000.0,
                "init_margin": 0.0,
                "maintenance_margin": 0.0,
            }
        ],
    }
    rc = _legacy._print_connector_account(longbridge_account)

    assert rc == _legacy.EXIT_SUCCESS
    out = capsys.readouterr().out
    assert "No account summary returned." not in out
    assert "USD" in out
    assert "12" in out and "345" in out  # net_assets 12,345 rendered


def test_connector_account_still_handles_ibkr_summary(capsys) -> None:
    ibkr_account = {
        "status": "ok",
        "profile_id": "ibkr-local",
        "accounts": ["DU123"],
        "summary": [{"account": "DU123", "tag": "NetLiquidation", "value": "50000", "currency": "USD"}],
    }
    rc = _legacy._print_connector_account(ibkr_account)

    assert rc == _legacy.EXIT_SUCCESS
    out = capsys.readouterr().out
    assert "NetLiquidation" in out
    assert "50000" in out


def test_connector_account_renders_direct_sdk_account_mapping(capsys) -> None:
    alpaca_account = {
        "status": "ok",
        "profile_id": "alpaca-paper-trade",
        "profile": "paper",
        "account": {
            "account_number": "PA123",
            "status": "AccountStatus.ACTIVE",
            "currency": "USD",
            "cash": "100000",
            "equity": "100000",
            "buying_power": "400000",
            "pattern_day_trader": False,
            "trading_blocked": False,
        },
    }

    rc = _legacy._print_connector_account(alpaca_account)

    assert rc == _legacy.EXIT_SUCCESS
    out = capsys.readouterr().out
    assert "No account summary returned." not in out
    assert "PA123" in out
    assert "USD" in out
    assert "buying_power" in out
    assert "400000" in out
    assert "trading_blocked" in out
    assert "False" in out


def test_connector_check_uses_sdk_diagnostics_without_oauth_rows(capsys) -> None:
    profile = SimpleNamespace(
        id="alpaca-paper-trade",
        connector="alpaca",
        environment="paper",
        transport="broker_sdk",
    )
    report = {
        "status": "ok",
        "sdk": {"package": "alpaca-py", "installed": True},
        "tap": False,
    }

    with (
        patch("cli._legacy._selected_profile_or", return_value=profile),
        patch("src.trading.service.check_connection", return_value=report),
    ):
        rc = _legacy.cmd_connector_check("alpaca-paper-trade")

    assert rc == _legacy.EXIT_SUCCESS
    out = capsys.readouterr().out
    assert "Connector profile is ready." in out
    assert "alpaca-py" in out
    assert "installed" in out
    assert "OAuth token" not in out
    assert "Configured" not in out
    assert "Capabilities" not in out


def test_connector_account_renders_qmt_assets(capsys) -> None:
    qmt_account = {
        "status": "ok",
        "profile_id": "qmt-live-sdk-readonly",
        "account_id": "755860001037",
        "assets": [
            {
                "account_id": "755860001037",
                "cash": 100000.5,
                "available": 80000.0,
                "market_value": 50000.0,
                "total_asset": 150000.5,
                "frozen": 0.0,
                "currency": "CNY",
            }
        ],
    }

    rc = _legacy._print_connector_account(qmt_account)

    assert rc == _legacy.EXIT_SUCCESS
    out = capsys.readouterr().out
    assert "No account summary returned." not in out
    assert "755860001037" in out
    assert "CNY" in out
    assert "150000.5" in out
    assert "100000.5" in out


def test_connector_positions_renders_qmt_qty(capsys) -> None:
    qmt_positions = {
        "status": "ok",
        "profile_id": "qmt-live-sdk-readonly",
        "positions": [
            {
                "account": "755860001037",
                "symbol": "300285.SZ",
                "qty": 200,
                "avg_cost": 12.5,
                "currency": "CNY",
            }
        ],
    }
    with patch("src.trading.service.get_positions", return_value=qmt_positions):
        rc = _legacy.cmd_connector_positions("qmt-live-sdk-readonly")

    assert rc == _legacy.EXIT_SUCCESS
    out = capsys.readouterr().out
    assert "300285.SZ" in out
    assert "200" in out
    assert "12.5" in out
    assert "755860001037" in out


def test_connector_orders_renders_flat_qmt_schema(capsys) -> None:
    qmt_orders = {
        "status": "ok",
        "profile_id": "qmt-live-sdk-readonly",
        "open_orders": [
            {
                "account": "755860001037",
                "symbol": "600519.SH",
                "side": "buy",
                "qty": 100,
                "price": 1600.0,
                "status": "已报",
                "order_type": 23,
            }
        ],
    }
    with patch("src.trading.service.get_open_orders", return_value=qmt_orders):
        rc = _legacy.cmd_connector_orders("qmt-live-sdk-readonly")

    assert rc == _legacy.EXIT_SUCCESS
    out = capsys.readouterr().out
    assert "600519.SH" in out
    assert "buy" in out
    assert "100" in out
    assert "1600" in out
    assert "已报" in out
