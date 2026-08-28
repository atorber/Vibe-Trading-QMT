"""Tests for the read-only QMT Bridge connector."""

from __future__ import annotations

import json

import pytest

from src.config.accessor import reset_env_config
from src.live import registry
from src.live.classification import ToolClass, classify_tool
from src.trading import profiles, service
from src.trading.connectors.qmt import sdk as qmt
from src.trading.connectors.qmt.classification import QMT_TOOL_CLASS

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _reset_env_singleton():
    reset_env_config()
    yield
    reset_env_config()


def test_qmt_profiles_registered_readonly() -> None:
    ids = {profile.id for profile in profiles.list_profiles()}
    assert {"qmt-paper-sdk", "qmt-live-sdk-readonly"} <= ids

    for profile_id, environment in (
        ("qmt-paper-sdk", "paper"),
        ("qmt-live-sdk-readonly", "live"),
    ):
        profile = profiles.profile_by_id(profile_id)
        assert profile.connector == "qmt"
        assert profile.environment == environment
        assert profile.transport == "broker_sdk"
        assert profile.readonly is True
        assert "orders.place" not in profile.capabilities
        assert "orders.place.requires_mandate" not in profile.capabilities
        assert set(profile.capabilities) >= {
            "account.read",
            "positions.read",
            "orders.read",
            "quotes.read",
            "history.read",
        }


def test_qmt_build_config_reads_env(monkeypatch) -> None:
    monkeypatch.setenv("QMT_BRIDGE_HOST", "192.168.1.50")
    monkeypatch.setenv("QMT_BRIDGE_PORT", "8080")
    monkeypatch.setenv("QMT_BRIDGE_API_KEY", "bridge-key")
    monkeypatch.setenv("QMT_BRIDGE_ACCOUNT_ID", "acc-9")
    monkeypatch.setenv("QMT_BRIDGE_ACCOUNT_TYPE", "CREDIT")
    reset_env_config()

    profile = profiles.profile_by_id("qmt-live-sdk-readonly")
    cfg = qmt.build_config(profile.config)

    assert cfg.host == "192.168.1.50"
    assert cfg.port == 8080
    assert cfg.api_key == "bridge-key"
    assert cfg.account_id == "acc-9"
    assert cfg.account_type == "CREDIT"
    assert cfg.profile == "live-readonly"
    assert cfg.environment == "live"
    assert cfg.base_url == "http://192.168.1.50:8080"


def test_qmt_rejects_bind_all_host() -> None:
    with pytest.raises(qmt.QmtConfigError, match="0.0.0.0"):
        qmt.QmtConfig.from_mapping({"host": "0.0.0.0"})


def test_qmt_read_write_classification_registered() -> None:
    curated = registry._BROKER_CURATED_MAPS["qmt"]

    for name in (
        "health_check",
        "query_asset",
        "query_positions",
        "query_orders",
        "query_trades",
        "get_market_snapshot",
        "get_history",
    ):
        assert QMT_TOOL_CLASS[name] is ToolClass.READ
        assert classify_tool(name, None, curated) is ToolClass.READ

    for name in ("place_order", "cancel_order", "credit_order", "bank_transfer_in"):
        assert QMT_TOOL_CLASS[name] is ToolClass.WRITE
        assert classify_tool(name, None, curated) is ToolClass.WRITE

    assert classify_tool("brand_new_qmt_operation", None, curated) is ToolClass.UNKNOWN


def test_qmt_service_dispatches_positions(monkeypatch) -> None:
    def fake_request(config, method, path, *, params=None, require_api_key, timeout=None):
        assert method == "GET"
        assert path == "/api/trading/positions"
        assert require_api_key is True
        assert config.api_key == "key"
        assert params == {"account_id": "A1"}
        return {
            "data": [
                {
                    "stock_code": "600519.SH",
                    "volume": 100,
                    "can_use_volume": 100,
                    "avg_price": 1600.0,
                    "market_value": 165000.0,
                    "profit": 5000.0,
                }
            ]
        }

    monkeypatch.setattr(qmt, "_request", fake_request)

    result = service.get_positions(
        "qmt-live-sdk-readonly", api_key="key", account_id="A1"
    )
    assert result["status"] == "ok"
    assert result["connector"] == "qmt"
    assert result["environment"] == "live"
    position = result["positions"][0]
    assert position["symbol"] == "600519.SH"
    assert position["qty"] == 100
    assert position["quantity"] == 100
    assert position["available_qty"] == 100
    assert position["avg_cost"] == 1600.0
    assert position["cost_price"] == 1600.0
    assert position["market_value"] == 165000.0
    assert position["unrealized_pnl"] == 5000.0
    assert position["account"] == "A1"
    assert position["currency"] == "CNY"


def test_qmt_service_dispatches_quote(monkeypatch) -> None:
    def fake_request(config, method, path, *, params=None, require_api_key, timeout=None):
        assert path == "/api/market/full_tick"
        assert require_api_key is False
        assert params == {"stocks": "000001.SZ"}
        return {"000001.SZ": {"lastPrice": 11.2, "volume": 1000}}

    monkeypatch.setattr(qmt, "_request", fake_request)
    result = service.get_quote("000001.SZ", "qmt-paper-sdk", api_key="unused")
    assert result["status"] == "ok"
    assert result["symbol"] == "000001.SZ"
    assert result["quote"]["last"] == 11.2


def test_qmt_check_connection_gateway_down(monkeypatch) -> None:
    monkeypatch.setattr(qmt, "tcp_port_open", lambda host, port, timeout=1.5: False)
    result = service.check_connection(
        "qmt-live-sdk-readonly", host="10.0.0.2", port=8000, api_key="k"
    )
    assert result["status"] == "error"
    assert result["connection_state"] == "error"
    assert result["error_code"] == "network_unreachable"
    assert result["configured"] is True
    assert result["credential_source"] == "environment"
    assert "No QMT Bridge is listening" in result["error"]
    assert result["connector"] == "qmt"
    assert result["gateway"]["open"] is False
    assert result["last_checked_at"]


def test_qmt_check_connection_missing_api_key(monkeypatch) -> None:
    monkeypatch.setenv("QMT_BRIDGE_API_KEY", "")
    reset_env_config()

    result = service.check_connection("qmt-live-sdk-readonly", host="127.0.0.1")
    assert result["status"] == "error"
    assert result["connection_state"] == "not_configured"
    assert result["error_code"] == "credentials_missing"
    assert result["configured"] is False
    assert "QMT_BRIDGE_API_KEY" in result["error"]


def test_qmt_check_status_emits_runtime_envelope(monkeypatch) -> None:
    monkeypatch.setattr(qmt, "tcp_port_open", lambda host, port, timeout=1.5: True)

    def fake_request(config, method, path, *, params=None, require_api_key, timeout=None):
        if path == "/api/meta/health":
            return {"status": "ok", "trading": {"enabled": True, "connected": True}}
        if path == "/api/trading/asset":
            return {"data": {"account_id": "A1", "cash": 1.0, "total_asset": 2.0, "market_value": 1.0, "frozen_cash": 0}}
        raise AssertionError(path)

    monkeypatch.setattr(qmt, "_request", fake_request)
    result = service.check_connection(
        "qmt-live-sdk-readonly", host="10.211.55.13", port=8080, api_key="k", account_id="A1"
    )
    assert result["status"] == "ok"
    assert result["connection_state"] == "connected"
    assert result["configured"] is True
    assert result["credential_source"] == "environment"
    assert result["paper_guard"] == "config_declared"
    assert result["environment_identity"] == "config_declared"
    assert result["sdk_installed"] is True
    assert result["last_checked_at"]
    assert result["account"]["total_asset"] == 2.0
    assert result["trading"]["connected"] is True


def test_qmt_account_snapshot_exposes_shared_account_fields(monkeypatch) -> None:
    def fake_request(config, method, path, *, params=None, require_api_key, timeout=None):
        assert path == "/api/trading/asset"
        assert params == {"account_id": "755860001037"}
        return {
            "data": {
                "cash": 1000.0,
                "available_cash": 800.0,
                "market_value": 200.0,
                "total_asset": 1200.0,
                "frozen_cash": 50.0,
            }
        }

    monkeypatch.setattr(qmt, "_request", fake_request)
    result = service.get_account(
        "qmt-live-sdk-readonly", api_key="key", account="755860001037"
    )
    assert result["status"] == "ok"
    assert result["connector"] == "qmt"
    assert result["account_id"] == "755860001037"
    assert result["accounts"] == ["755860001037"]
    assert result["assets"][0]["total_asset"] == 1200.0
    assert result["balances"][0]["net_assets"] == 1200.0
    assert result["balances"][0]["buy_power"] == 800.0
    assert result["account"]["account_number"] == "755860001037"
    assert result["account"]["cash"] == 1000.0
    assert result["account"]["equity"] == 1200.0
    assert result["account"]["buying_power"] == 800.0
    assert result["account"]["currency"] == "CNY"


def test_qmt_account_snapshot_merges_credit_detail(monkeypatch) -> None:
    calls: list[str] = []

    def fake_request(config, method, path, *, params=None, require_api_key, timeout=None):
        calls.append(path)
        if path == "/api/trading/asset":
            return {
                "data": {
                    "cash": 1000.0,
                    "market_value": 200.0,
                    "total_asset": 1200.0,
                }
            }
        if path == "/api/credit/asset":
            return {
                "data": [
                    {
                        "m_dBalance": 1_500_000.0,
                        "m_dAssureAsset": 1_200_000.0,
                        "m_dTotalDebt": 300_000.0,
                    }
                ]
            }
        raise AssertionError(path)

    monkeypatch.setattr(qmt, "_request", fake_request)
    result = qmt.get_account_snapshot(
        qmt.QmtConfig(api_key="key", account_id="755860001037")
    )
    assert calls == ["/api/trading/asset", "/api/credit/asset"]
    assert result["account"]["total_asset"] == 1_500_000.0
    assert result["account"]["gross_assets"] == 1_500_000.0
    assert result["account"]["net_assets"] == 1_200_000.0
    assert result["account"]["total_debt"] == 300_000.0
    assert result["balances"][0]["net_assets"] == 1_200_000.0


def test_qmt_build_config_maps_account_alias() -> None:
    cfg = qmt.QmtConfig(host="10.0.0.1", api_key="k").with_overrides(account="ACC-9")
    assert cfg.account_id == "ACC-9"


def test_qmt_public_config_redacts_api_key() -> None:
    pub = qmt._public_config(qmt.QmtConfig(api_key="KEY12345", account_id="1"))
    assert pub["api_key"] == "KEY1***"
    assert "KEY12345" not in str(pub)


def test_qmt_order_attempts_refused_before_http() -> None:
    cfg = qmt.QmtConfig(profile="live-readonly", api_key="k")
    placed = qmt.place_order(cfg, symbol="600519.SH", side="buy", quantity=100)
    cancelled = qmt.cancel_order(cfg, "1", symbol="600519.SH")
    assert placed["status"] == "error"
    assert cancelled["status"] == "error"
    assert "not supported" in placed["error"]


def test_qmt_paper_orders_also_disabled() -> None:
    cfg = qmt.QmtConfig(profile="paper", api_key="k")
    placed = qmt.place_order(cfg, symbol="600519.SH", side="buy", quantity=100)
    assert placed["status"] == "error"
    assert "read-only" in placed["error"]


def test_qmt_trading_connections_lists_compact_qmt(monkeypatch) -> None:
    from src.tools.trading_connector_tool import TradingConnectionsTool

    monkeypatch.setenv("QMT_BRIDGE_API_KEY", "k")
    reset_env_config()
    payload = json.loads(TradingConnectionsTool().execute())
    connectors = {row["connector"]: row for row in payload["connectors"]}
    assert "qmt" in connectors
    assert "qmt-live-sdk-readonly" in connectors["qmt"]["profiles"]
    assert connectors["qmt"]["configured_hint"] == "env"
    ids = {row["id"] for row in payload["profiles"]}
    assert "qmt-live-sdk-readonly" in ids


def test_qmt_cn_equity_classification() -> None:
    instrument, asset = service._order_classification("qmt", "600519.SH")
    assert instrument.value == "equity"
    assert asset is not None
    assert asset.value == "cn_equity"


class _FakeResponse:
    status_code = 200
    content = b'{"ok":true}'
    text = '{"ok":true}'
    reason = "OK"

    def json(self):
        return {"ok": True}


def test_qmt_request_sends_x_api_key(monkeypatch) -> None:
    seen: dict = {}

    def fake_request(method, url, *, headers, timeout):
        seen.update({"method": method, "url": url, "headers": headers, "timeout": timeout})
        return _FakeResponse()

    monkeypatch.setattr(qmt.requests, "request", fake_request)
    qmt._request(
        qmt.QmtConfig(host="192.168.1.1", port=8000, api_key="secret"),
        "GET",
        "/api/trading/asset",
        params={"account_id": "9"},
        require_api_key=True,
    )
    assert seen["method"] == "GET"
    assert seen["url"] == "http://192.168.1.1:8000/api/trading/asset?account_id=9"
    assert seen["headers"]["X-API-Key"] == "secret"


def test_qmt_history_uses_market_data_ex(monkeypatch) -> None:
    def fake_request(config, method, path, *, params=None, require_api_key, timeout=None):
        assert path == "/api/market/market_data_ex"
        assert params["stocks"] == "000001.SZ"
        assert params["period"] == "60m"
        assert params["count"] == 20
        return {
            "data": {
                "000001.SZ": [
                    {"time": "20260105", "open": 10.0, "high": 10.2, "low": 9.9, "close": 10.1, "volume": 100}
                ]
            }
        }

    monkeypatch.setattr(qmt, "_request", fake_request)
    result = service.get_history(
        "000001.SZ", "qmt-paper-sdk", period="1h", limit=20, api_key="unused"
    )
    assert result["status"] == "ok"
    assert result["bars"][0]["close"] == 10.1


def test_qmt_orders_use_bridge_orders_and_trades(monkeypatch) -> None:
    seen: list[tuple[str, dict]] = []

    def fake_request(config, method, path, *, params=None, require_api_key, timeout=None):
        seen.append((path, dict(params or {})))
        if path == "/api/trading/orders" and (params or {}).get("cancelable_only") == "true":
            return {"data": []}
        if path == "/api/trading/orders":
            return {
                "data": [
                    {
                        "stock_code": "300285.SZ",
                        "order_id": 11,
                        "order_type": 23,
                        "order_volume": 1000,
                        "traded_volume": 1000,
                        "price": 12.3,
                        "order_status": 56,
                    }
                ]
            }
        if path == "/api/trading/trades":
            return {
                "data": [
                    {
                        "stock_code": "300285.SZ",
                        "order_id": 11,
                        "traded_id": "t1",
                        "order_type": 23,
                        "traded_volume": 1000,
                        "traded_price": 12.3,
                        "traded_time": "20260814",
                    }
                ]
            }
        raise AssertionError(path)

    monkeypatch.setattr(qmt, "_request", fake_request)
    result = service.get_open_orders("qmt-live-sdk-readonly", api_key="k", account_id="A1")
    assert [path for path, _ in seen] == [
        "/api/trading/orders",
        "/api/trading/orders",
        "/api/trading/trades",
    ]
    assert seen[0][1]["cancelable_only"] == "true"
    assert "cancelable_only" not in seen[1][1]
    assert result["open_orders"] == []
    assert result["orders"][0]["symbol"] == "300285.SZ"
    assert result["orders"][0]["filled_qty"] == 1000
    assert result["executions"][0]["qty"] == 1000
    assert result["executions"][0]["side"] == "buy"
    assert result["source"]["executions"] == "GET /api/trading/trades"


def test_qmt_orders_fetch_history_when_start_time_set(monkeypatch) -> None:
    seen: list[str] = []

    def fake_request(config, method, path, *, params=None, require_api_key, timeout=None):
        seen.append(path)
        if path == "/api/trading/history_trades":
            assert params["start_time"] == "20260810"
            assert params["end_time"] == "20260813"
            return {
                "data": [
                    {
                        "stock_code": "300476.SZ",
                        "order_type": 24,
                        "traded_volume": 500,
                        "traded_price": 20.1,
                        "traded_time": "20260811",
                    }
                ]
            }
        if path == "/api/trading/history_orders":
            return {"export_code": {"error": {"errorMsg": "unsupported data type"}}, "error": "unsupported data type", "data": []}
        return {"data": []}

    monkeypatch.setattr(qmt, "_request", fake_request)
    result = service.get_open_orders(
        "qmt-live-sdk-readonly",
        api_key="k",
        account_id="A1",
        start_time="2026-08-10",
        end_time="20260813",
    )
    assert "/api/trading/history_trades" in seen
    assert "/api/trading/history_orders" in seen
    assert result["history_executions"][0]["symbol"] == "300476.SZ"
    assert result["history_executions"][0]["side"] == "sell"
    assert result["history_orders"] == []
    assert "unsupported" in str(result.get("history_orders_error") or "").lower()
