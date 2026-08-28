"""Read-only QMT Bridge connector via HTTP.

Wraps a remote/local QMT Bridge (``qmt-server``) for the five read operations the
trading layer exposes: account / positions / orders / quote / history. Connection
settings are loaded from ``QMT_BRIDGE_*`` env vars (see ``DataConfig``). Order
placement is refused — Bridge binds one account at startup and exposes no
runtime paper/live discriminator this connector can verify
(``paper_guard="config_declared"``).

Architecture mirrors Futu OpenD / MT5: the Bridge runs beside QMT on Windows;
Vibe-Trading on any OS speaks HTTP over the LAN. A TCP probe plus
``GET /api/meta/health`` degrade cleanly when the Bridge is down.
"""

from __future__ import annotations

import socket
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Mapping
from urllib.parse import urlencode, urljoin

import requests

from src.config.accessor import get_env_config

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
#: Closed vocabulary for ``GET /live/status`` environment_identity.
PAPER_GUARD = "config_declared"

PROFILE_ENVIRONMENTS = {
    "paper": "paper",
    "live-readonly": "live",
    "live": "live",
}

#: Canonical period token → QMT Bridge period (tick/1m/5m/15m/30m/60m/1d).
_PERIOD_MAP = {
    "tick": "tick",
    "1m": "1m",
    "5m": "5m",
    "15m": "15m",
    "30m": "30m",
    "60m": "60m",
    "1h": "60m",
    "1H": "60m",
    "1d": "1d",
    "1D": "1d",
}

_ORDER_TYPE_SIDE = {
    23: "buy",
    24: "sell",
    "23": "buy",
    "24": "sell",
}

_ORDER_DISABLED_ERROR = (
    "QMT Bridge connector is read-only: order placement and cancellation are "
    "disabled until a structural paper/live safety boundary is available."
)

_LIVE_ORDER_ERROR = (
    "QMT Bridge order placement is not supported for live/read-only profiles "
    "(no runtime paper/live discriminator is available)."
)


class QmtConfigError(RuntimeError):
    """Raised when the QMT Bridge connector configuration is invalid."""


class QmtAPIError(RuntimeError):
    """Raised when QMT Bridge returns an auth, HTTP, network, or JSON error."""


@dataclass(frozen=True)
class QmtConfig:
    """QMT Bridge connector connection settings.

    Args:
        host: Bridge host (Windows LAN IP or localhost).
        port: Bridge HTTP port (default 8000).
        api_key: API key for ``/api/trading/*`` (header ``X-API-Key``).
        account_id: Trading account id matching ``qmt-server --account-id``.
        account_type: ``STOCK`` or ``CREDIT`` for Bridge ``account_type``.
        profile: ``paper``, ``live-readonly`` or ``live`` (operator-declared).
        timeout: Network timeout in seconds.
        readonly: Always true for built-in profiles.
    """

    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    api_key: str = ""
    account_id: str = ""
    account_type: str = ""
    profile: str = "live-readonly"
    timeout: float = 15.0
    readonly: bool = True

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None = None) -> "QmtConfig":
        """Build a config from a JSON-like mapping, normalizing the profile."""
        payload = dict(data or {})
        profile = str(payload.get("profile") or "live-readonly").strip().lower()
        if profile not in PROFILE_ENVIRONMENTS:
            raise QmtConfigError("profile must be 'paper', 'live-readonly' or 'live'")
        host = str(payload.get("host") or DEFAULT_HOST).strip() or DEFAULT_HOST
        if host in {"0.0.0.0", "::"}:
            raise QmtConfigError(
                "QMT_BRIDGE_HOST must be a connectable address (127.0.0.1 or a LAN IP), "
                "not 0.0.0.0"
            )
        return cls(
            host=host,
            port=int(payload.get("port") or DEFAULT_PORT),
            api_key=str(payload.get("api_key") or "").strip(),
            account_id=str(payload.get("account_id") or "").strip(),
            account_type=str(payload.get("account_type") or "").strip().upper(),
            profile=profile,
            timeout=float(payload.get("timeout") or 15.0),
            readonly=bool(payload.get("readonly", True)),
        )

    def with_overrides(self, **kwargs: Any) -> "QmtConfig":
        """Return a copy with known overrides applied.

        CLI/agent tools pass ``account``; Bridge uses ``account_id``. Map that
        alias here so ``--account`` / ``trading_account(account=...)`` select
        the QMT funds account instead of being dropped.
        """
        payload = asdict(self)
        for key, value in _normalized_overrides(kwargs).items():
            payload[key] = value
        return QmtConfig.from_mapping(payload)

    @property
    def environment(self) -> str:
        """Return ``paper`` or ``live`` for the operator-declared profile."""
        return PROFILE_ENVIRONMENTS.get(self.profile, "live")

    @property
    def base_url(self) -> str:
        """HTTP origin for Bridge requests."""
        return f"http://{self.host}:{int(self.port)}"


_OVERRIDE_KEYS = ("host", "port", "api_key", "account_id", "account_type", "profile", "timeout")


def _normalized_overrides(kwargs: Mapping[str, Any]) -> dict[str, Any]:
    """Keep known config keys and map ``account`` → ``account_id``."""
    payload = dict(kwargs)
    if payload.get("account") and not payload.get("account_id"):
        payload["account_id"] = payload["account"]
    return {
        key: value
        for key, value in payload.items()
        if key in _OVERRIDE_KEYS and value not in (None, "")
    }


def _environment_values() -> dict[str, Any]:
    """Load QMT Bridge settings from EnvConfig / ``QMT_BRIDGE_*``."""
    data = get_env_config().data
    return {
        "host": str(data.qmt_bridge_host or DEFAULT_HOST).strip() or DEFAULT_HOST,
        "port": int(data.qmt_bridge_port or DEFAULT_PORT),
        "api_key": str(data.qmt_bridge_api_key or "").strip(),
        "account_id": str(data.qmt_bridge_account_id or "").strip(),
        "account_type": str(data.qmt_bridge_account_type or "").strip(),
    }


def load_config() -> QmtConfig:
    """Load QMT Bridge settings from environment (``.env`` / EnvConfig)."""
    return QmtConfig.from_mapping(_environment_values())


def build_config(
    profile_config: Mapping[str, Any] | None = None,
    overrides: Mapping[str, Any] | None = None,
) -> QmtConfig:
    """Resolve config: env ← profile defaults ← CLI/tool overrides."""
    base = asdict(load_config())
    for key, value in dict(profile_config or {}).items():
        if value is not None:
            base[key] = value
    cfg = QmtConfig.from_mapping(base)
    clean = _normalized_overrides(overrides or {})
    return cfg.with_overrides(**clean) if clean else cfg


def tcp_port_open(host: str, port: int, *, timeout: float = 1.5) -> bool:
    """Return whether a TCP listener accepts connections at ``host:port``."""
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def check_status(config: QmtConfig | None = None) -> dict[str, Any]:
    """Check Bridge reachability with the Runtime ``/live/status`` envelope."""
    cfg = config or load_config()
    configured = bool(cfg.api_key)
    report: dict[str, Any] = {
        "status": "ok",
        "configured": configured,
        "credential_source": "environment" if configured else None,
        "connection_state": "connected",
        "error_code": None,
        "error": None,
        "config": _public_config(cfg),
        "sdk": {"package": "requests", "installed": True},
        "sdk_installed": True,
        "paper_guard": PAPER_GUARD,
        "environment_identity": PAPER_GUARD,
        "base_url": cfg.base_url,
    }

    if not configured:
        return _status_error(
            report,
            "credentials_missing",
            "QMT Bridge connector not configured: missing QMT_BRIDGE_API_KEY.",
        )

    gateway_open = tcp_port_open(cfg.host, cfg.port)
    report["gateway"] = {"host": cfg.host, "port": cfg.port, "open": gateway_open}
    if not gateway_open:
        return _status_error(
            report,
            "network_unreachable",
            (
                f"No QMT Bridge is listening at {cfg.host}:{cfg.port}. "
                "On the Windows host start QMT (独立交易), then "
                "`qmt-server --trading --api-key ... --account-id ...`."
            ),
        )

    try:
        health = _get(cfg, "/api/meta/health", require_api_key=False)
    except (QmtConfigError, QmtAPIError) as exc:
        return _status_error(report, _connection_error_code(exc), str(exc))
    except Exception as exc:  # noqa: BLE001 - health endpoint reports cleanly
        return _status_error(
            report, "broker_error", f"QMT Bridge health check failed: {exc}"
        )

    report["health"] = _mapping_or_raw(health)
    trading = health.get("trading") if isinstance(health, Mapping) else None
    if isinstance(trading, Mapping):
        report["trading"] = dict(trading)
        if trading.get("enabled") and trading.get("connected") is False:
            return _status_error(
                report,
                "broker_error",
                str(trading.get("error") or "QMT Bridge trading module is not connected."),
            )

    try:
        snapshot = get_account_snapshot(cfg)
    except (QmtConfigError, QmtAPIError) as exc:
        return _status_error(report, _connection_error_code(exc), str(exc))
    except Exception as exc:  # noqa: BLE001
        return _status_error(
            report, "broker_error", f"QMT Bridge account check failed: {exc}"
        )

    assets = snapshot.get("assets") or []
    first = assets[0] if assets and isinstance(assets[0], Mapping) else {}
    report["account"] = {
        "profile": cfg.profile,
        "account_id": cfg.account_id or first.get("account_id"),
        "total_asset": first.get("total_asset"),
        "currency": first.get("currency") or "CNY",
    }
    report["last_checked_at"] = datetime.now(timezone.utc).isoformat()
    return report


def _status_error(report: dict[str, Any], code: str, message: str) -> dict[str, Any]:
    """Fill a fail-closed Runtime verify envelope without leaking secrets."""
    if code in ("credentials_missing", "credentials_partial"):
        report["configured"] = False
        report["credential_source"] = None
    report.update(
        status="error",
        connection_state=(
            "not_configured" if code in ("credentials_missing", "credentials_partial") else "error"
        ),
        error_code=code,
        error=message,
        last_checked_at=datetime.now(timezone.utc).isoformat(),
    )
    return report


def _connection_error_code(exc: Exception) -> str:
    text = str(exc).lower()
    if "authentication failed" in text or "api key" in text:
        return "authentication_failed"
    if any(token in text for token in ("failed to establish", "connection refused", "timed out", "unreachable")):
        return "network_unreachable"
    if isinstance(exc, QmtConfigError) and "missing" in text:
        return "credentials_missing"
    return "broker_error"


def get_account_snapshot(config: QmtConfig | None = None) -> dict[str, Any]:
    """Fetch account funds/assets via ``GET /api/trading/asset``."""
    cfg = config or load_config()
    payload = _get(cfg, "/api/trading/asset", params=_account_params(cfg), require_api_key=True)
    rows = _extract_items(payload, preferred_keys=("data", "assets", "asset", "result"))
    if not rows and isinstance(payload, Mapping):
        # Single asset dict without a list wrapper.
        if any(k in payload for k in ("cash", "total_asset", "market_value", "available_cash")):
            rows = [dict(payload)]
    snapshot = _account_snapshot(cfg, rows)
    try:
        credit_payload = _get(
            cfg,
            "/api/credit/asset",
            params=_account_params(cfg),
            require_api_key=True,
        )
        credit_rows = _extract_items(credit_payload, preferred_keys=("data",))
        if credit_rows and isinstance(credit_rows[0], Mapping):
            _apply_qmt_credit_detail(snapshot, credit_rows[0])
    except Exception:
        pass
    return snapshot


def get_positions(config: QmtConfig | None = None) -> dict[str, Any]:
    """Fetch current positions via ``GET /api/trading/positions``."""
    cfg = config or load_config()
    payload = _get(
        cfg, "/api/trading/positions", params=_account_params(cfg), require_api_key=True
    )
    rows = _extract_items(payload, preferred_keys=("data", "positions", "result"))
    return {
        "status": "ok",
        "profile": cfg.profile,
        "paper_guard": PAPER_GUARD,
        "account_id": cfg.account_id,
        "positions": [_position_to_dict(row, account_id=cfg.account_id) for row in rows],
    }


def get_batch_stock_names(
    stocks: list[str], config: QmtConfig | None = None
) -> dict[str, str]:
    """Resolve Chinese instrument names via ``GET /api/utility/batch_stock_name``."""
    cfg = config or load_config()
    codes = [str(code).strip().upper() for code in stocks if str(code).strip()]
    if not codes:
        return {}
    payload = _get(
        cfg,
        "/api/utility/batch_stock_name",
        params={"stocks": ",".join(codes)},
        require_api_key=False,
    )
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return {}
    return {
        str(code).strip().upper(): str(name).strip()
        for code, name in data.items()
        if str(code).strip() and str(name).strip()
    }


def get_open_orders(
    config: QmtConfig | None = None,
    *,
    include_executions: bool = False,
    start_time: str = "",
    end_time: str = "",
) -> dict[str, Any]:
    """Fetch QMT session orders/fills, plus optional history via export/query.

    Always calls today's ``/api/trading/orders`` and ``/api/trading/trades``.
    When ``start_time`` is set, also calls ``/api/trading/history_trades`` and
    ``/api/trading/history_orders`` (``export_data`` + ``query_data``).
    """
    cfg = config or load_config()
    account = _account_params(cfg)
    open_payload = _get(
        cfg,
        "/api/trading/orders",
        params={**account, "cancelable_only": "true"},
        require_api_key=True,
    )
    session_payload = _get(
        cfg,
        "/api/trading/orders",
        params=account,
        require_api_key=True,
    )
    trades_payload = _get(
        cfg, "/api/trading/trades", params=account, require_api_key=True
    )
    open_rows = _extract_items(open_payload, preferred_keys=("data", "orders", "result"))
    session_rows = _extract_items(
        session_payload, preferred_keys=("data", "orders", "result")
    )
    trade_rows = _extract_items(
        trades_payload, preferred_keys=("data", "trades", "result")
    )
    source = {
        "open_orders": "GET /api/trading/orders?cancelable_only=true",
        "orders": "GET /api/trading/orders",
        "executions": "GET /api/trading/trades",
    }
    result: dict[str, Any] = {
        "status": "ok",
        "profile": cfg.profile,
        "paper_guard": PAPER_GUARD,
        "account_id": cfg.account_id,
        "source": source,
        "open_orders": [_order_to_dict(row, account_id=cfg.account_id) for row in open_rows],
        "orders": [_order_to_dict(row, account_id=cfg.account_id) for row in session_rows],
        "executions": [_trade_to_dict(row) for row in trade_rows],
        "include_executions": True,
    }
    history_start = _ymd(start_time)
    history_end = _ymd(end_time)
    if history_start:
        history_params = {**account, "start_time": history_start, "end_time": history_end}
        trades_hist = _get(
            cfg,
            "/api/trading/history_trades",
            params=history_params,
            require_api_key=True,
            timeout=max(cfg.timeout, 60.0),
        )
        result["history_start"] = history_start
        result["history_end"] = history_end
        if isinstance(trades_hist, Mapping) and not history_end:
            result["history_end"] = str(trades_hist.get("end_time") or "")
        hist_error = _bridge_error_message(trades_hist)
        hist_rows = [
            row
            for row in _extract_items(trades_hist, preferred_keys=("data", "trades", "result"))
            if not _is_bridge_error_row(row)
        ]
        result["history_executions"] = [_trade_to_dict(row) for row in hist_rows]
        source["history_executions"] = "GET /api/trading/history_trades"
        if hist_error:
            result["history_executions_error"] = hist_error
        try:
            orders_hist = _get(
                cfg,
                "/api/trading/history_orders",
                params=history_params,
                require_api_key=True,
                timeout=max(cfg.timeout, 60.0),
            )
            order_error = _bridge_error_message(orders_hist)
            if order_error:
                result["history_orders"] = []
                result["history_orders_error"] = order_error
            else:
                result["history_orders"] = [
                    _order_to_dict(row, account_id=cfg.account_id)
                    for row in _extract_items(
                        orders_hist, preferred_keys=("data", "orders", "result")
                    )
                    if not _is_bridge_error_row(row)
                ]
            source["history_orders"] = "GET /api/trading/history_orders"
        except QmtAPIError as exc:
            result["history_orders"] = []
            result["history_orders_error"] = str(exc)
    _ = include_executions
    return result


def get_quote(symbol: str, *, config: QmtConfig | None = None, **_: Any) -> dict[str, Any]:
    """Fetch a market snapshot via ``GET /api/market/full_tick``."""
    cfg = config or load_config()
    code = symbol.strip().upper()
    payload = _get(
        cfg,
        "/api/market/full_tick",
        params={"stocks": code},
        require_api_key=False,
    )
    quote = _quote_from_payload(payload, code)
    return {"status": "ok", "symbol": code, "quote": quote}


def get_historical_bars(
    symbol: str,
    *,
    config: QmtConfig | None = None,
    period: str = "1d",
    limit: int = 90,
    **_: Any,
) -> dict[str, Any]:
    """Fetch historical bars via ``GET /api/market/market_data_ex``."""
    cfg = config or load_config()
    code = symbol.strip().upper()
    bridge_period = _PERIOD_MAP.get(period.strip(), period.strip() or "1d")
    payload = _get(
        cfg,
        "/api/market/market_data_ex",
        params={
            "stocks": code,
            "period": bridge_period,
            "count": int(limit),
            "dividend_type": "none",
            "fill_data": "true",
        },
        require_api_key=False,
    )
    bars = [_bar_to_dict(row) for row in _bars_from_payload(payload, symbol=code)]
    return {
        "status": "ok",
        "symbol": code,
        "period": period,
        "bars": bars,
    }


def download_history_bars(
    symbols: list[str],
    *,
    config: QmtConfig | None = None,
    period: str = "1d",
    start_time: str = "",
    end_time: str = "",
    timeout: float = 90.0,
) -> dict[str, Any]:
    """Trigger Bridge ``POST /api/download/history_data2`` for local K-line cache."""
    cfg = config or load_config()
    codes = [str(code).strip().upper() for code in symbols if str(code).strip()]
    if not codes:
        return {"status": "ok", "stocks": [], "period": period, "result": {}}
    bridge_period = _PERIOD_MAP.get(period.strip(), period.strip() or "1d")
    payload = _request(
        cfg,
        "POST",
        "/api/download/history_data2",
        require_api_key=False,
        timeout=timeout,
        json_body={
            "stocks": codes,
            "period": bridge_period,
            "start_time": start_time,
            "end_time": end_time,
        },
    )
    if isinstance(payload, dict):
        return payload
    return {"status": "ok", "stocks": codes, "period": bridge_period, "result": payload}


def place_order(
    config: QmtConfig | None = None,
    *,
    symbol: str,
    side: str,
    quantity: float | None = None,
    notional: float | None = None,
    order_type: str = "market",
    limit_price: float | None = None,
    time_in_force: str = "day",
) -> dict[str, Any]:
    """Refuse QMT order placement before any HTTP client is touched."""
    cfg = config or load_config()
    if cfg.environment != "paper":
        return _order_refused(cfg, _LIVE_ORDER_ERROR, symbol=symbol, side=side)
    return _order_refused(
        cfg,
        _ORDER_DISABLED_ERROR,
        symbol=symbol,
        side=side,
        quantity=quantity,
        notional=notional,
        order_type=order_type,
        limit_price=limit_price,
        time_in_force=time_in_force,
    )


def cancel_order(
    config: QmtConfig | None = None,
    order_id: str = "",
    *,
    symbol: str | None = None,
) -> dict[str, Any]:
    """Refuse QMT order cancellation before any HTTP client is touched."""
    cfg = config or load_config()
    if cfg.environment != "paper":
        return _order_refused(cfg, _LIVE_ORDER_ERROR, order_id=order_id, symbol=symbol)
    return _order_refused(cfg, _ORDER_DISABLED_ERROR, order_id=order_id, symbol=symbol)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def _account_params(cfg: QmtConfig) -> dict[str, str]:
    params: dict[str, str] = {}
    if cfg.account_id:
        params["account_id"] = cfg.account_id
    if cfg.account_type in {"STOCK", "CREDIT"}:
        params["account_type"] = cfg.account_type
    return params


def _ymd(value: str) -> str:
    raw = str(value or "").strip().replace("-", "").replace("/", "")
    if not raw:
        return ""
    if len(raw) != 8 or not raw.isdigit():
        raise QmtConfigError("QMT history dates must be YYYYMMDD.")
    return raw


def _is_bridge_error_row(row: Any) -> bool:
    return isinstance(row, Mapping) and "error" in row and "stock_code" not in row and "symbol" not in row


def _bridge_error_message(payload: Any) -> str | None:
    if not isinstance(payload, Mapping):
        return None
    err = payload.get("error")
    if isinstance(err, Mapping):
        return str(err.get("errorMsg") or err.get("message") or err)
    if isinstance(err, str) and err.strip():
        return err
    return None


def _get(
    config: QmtConfig,
    path: str,
    *,
    params: Mapping[str, Any] | None = None,
    require_api_key: bool,
    timeout: float | None = None,
) -> Any:
    """Perform a read-only GET against the QMT Bridge."""
    return _request(
        config,
        "GET",
        path,
        params=params,
        require_api_key=require_api_key,
        timeout=timeout,
    )


def _request(
    config: QmtConfig,
    method: str,
    path: str,
    *,
    params: Mapping[str, Any] | None = None,
    require_api_key: bool,
    timeout: float | None = None,
    json_body: Mapping[str, Any] | None = None,
) -> Any:
    """Run an HTTP request and normalize Bridge failure modes."""
    if require_api_key and not config.api_key:
        raise QmtConfigError(
            "QMT Bridge connector not configured: missing QMT_BRIDGE_API_KEY."
        )

    url = urljoin(f"{config.base_url.rstrip('/')}/", path.lstrip("/"))
    if params:
        query = urlencode({k: v for k, v in dict(params).items() if v not in (None, "")})
        if query:
            url = f"{url}?{query}"

    headers = {"Accept": "application/json"}
    if json_body is not None:
        headers["Content-Type"] = "application/json"
    if config.api_key:
        headers["X-API-Key"] = config.api_key

    try:
        response = requests.request(
            method.upper(),
            url,
            headers=headers,
            json=dict(json_body) if json_body is not None else None,
            timeout=timeout if timeout is not None else config.timeout,
        )
    except requests.RequestException as exc:
        raise QmtAPIError(f"QMT Bridge request failed: {exc}") from exc

    if response.status_code in (401, 403):
        raise QmtAPIError("QMT Bridge authentication failed: check QMT_BRIDGE_API_KEY.")
    if response.status_code == 504:
        raise QmtAPIError(
            f"QMT Bridge market request timed out: {_error_message(response)}"
        )
    if response.status_code == 503:
        raise QmtAPIError(
            f"QMT Bridge xtdata lock wait timed out: {_error_message(response)}"
        )
    if response.status_code == 502:
        raise QmtAPIError(
            f"QMT Bridge upstream failed: {_error_message(response)}"
        )
    if response.status_code >= 400:
        raise QmtAPIError(
            f"QMT Bridge returned HTTP {response.status_code}: {_error_message(response)}"
        )
    if not response.content:
        return None
    try:
        return response.json()
    except ValueError as exc:
        raise QmtAPIError("QMT Bridge returned invalid JSON.") from exc


def _error_message(response: requests.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text.strip() or response.reason or "request failed"
    if isinstance(payload, Mapping):
        for key in ("message", "error", "detail", "title"):
            value = payload.get(key)
            if value:
                return str(value)
    return response.text.strip() or response.reason or "request failed"


# ---------------------------------------------------------------------------
# Normalization helpers
# ---------------------------------------------------------------------------


def _public_config(cfg: QmtConfig) -> dict[str, Any]:
    """Config snapshot with API key redacted."""
    payload = asdict(cfg)
    key = str(payload.get("api_key") or "")
    if key:
        payload["api_key"] = f"{key[:4]}***" if len(key) > 4 else "***"
    else:
        payload["api_key"] = ""
    return payload


def _order_refused(cfg: QmtConfig, error: str, **extra: Any) -> dict[str, Any]:
    payload = {
        "status": "error",
        "error": error,
        "profile": cfg.profile,
        "paper_guard": PAPER_GUARD,
        "account_id": cfg.account_id,
    }
    payload.update({k: v for k, v in extra.items() if v is not None})
    return payload


def _mapping_or_raw(payload: Any) -> Any:
    if isinstance(payload, Mapping):
        return dict(payload)
    return payload


def _extract_items(
    payload: Any, *, preferred_keys: tuple[str, ...] = ("data", "result", "items")
) -> list[dict[str, Any]]:
    """Normalize Bridge list/dict envelopes to a list of row dicts."""
    if payload is None:
        return []
    if isinstance(payload, list):
        return [dict(item) if isinstance(item, Mapping) else {"value": item} for item in payload]
    if not isinstance(payload, Mapping):
        return []
    for key in preferred_keys:
        value = payload.get(key)
        if isinstance(value, list):
            return [
                dict(item) if isinstance(item, Mapping) else {"value": item} for item in value
            ]
        if isinstance(value, Mapping):
            # Nested list under common keys.
            for nested in ("list", "items", "rows", "positions", "orders", "trades"):
                inner = value.get(nested)
                if isinstance(inner, list):
                    return [
                        dict(item) if isinstance(item, Mapping) else {"value": item}
                        for item in inner
                    ]
            return [dict(value)]
    return []


def _first(row: Mapping[str, Any], names: tuple[str, ...], default: Any = None) -> Any:
    for name in names:
        if name in row and row[name] is not None:
            return row[name]
    return default


def _num(value: Any) -> float | int | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number.is_integer():
        return int(number)
    return number


def _account_to_dict(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "account_id": _first(row, ("account_id", "accountId", "acc_id")),
        "cash": _num(_first(row, ("cash", "balance"))),
        "available": _num(_first(row, ("available_cash", "enable_amount", "available", "cash"))),
        "market_value": _num(_first(row, ("market_value", "marketValue"))),
        "total_asset": _num(_first(row, ("total_asset", "totalAsset", "total_assets"))),
        "frozen": _num(_first(row, ("frozen_cash", "frozen", "freeze_amount"))),
        "currency": str(_first(row, ("currency", "money_type"), "CNY") or "CNY"),
    }


def _apply_qmt_credit_detail(
    snapshot: dict[str, Any], detail: Mapping[str, Any]
) -> None:
    """Merge QMT credit-account fields: gross (m_dBalance) vs net (m_dAssureAsset)."""
    gross = _num(
        _first(
            detail,
            ("m_dBalance", "m_d_balance", "balance", "total_asset", "gross_assets"),
        )
    )
    net = _num(
        _first(
            detail,
            (
                "m_dAssureAsset",
                "m_d_assure_asset",
                "assure_asset",
                "net_assets",
                "equity",
            ),
        )
    )
    debt = _num(
        _first(
            detail,
            ("m_dTotalDebt", "m_d_total_debt", "m_dTotalDebit", "total_debt"),
        )
    )
    if gross is None and net is None:
        return
    account = snapshot.setdefault("account", {})
    if gross is not None:
        account["gross_assets"] = gross
        account["total_asset"] = gross
    if net is not None:
        account["net_assets"] = net
        account["equity"] = net
    if debt is not None:
        account["total_debt"] = debt
    for item in snapshot.get("assets") or []:
        if not isinstance(item, dict):
            continue
        if gross is not None:
            item["gross_assets"] = gross
            item["total_asset"] = gross
        if net is not None:
            item["net_assets"] = net
        if debt is not None:
            item["total_debt"] = debt
    for bal in snapshot.get("balances") or []:
        if not isinstance(bal, dict):
            continue
        if gross is not None:
            bal["gross_assets"] = gross
        if net is not None:
            bal["net_assets"] = net
        if debt is not None:
            bal["total_debt"] = debt


def _account_snapshot(cfg: QmtConfig, rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    """Normalize Bridge assets into the shared account shapes CLI/agent already read.

    QMT's native payload is ``assets[]``. Other connectors expose ``balances``,
    a flat ``account`` mapping, and ``accounts``. Emit all three so account
    surfaces do not have to special-case QMT.
    """
    assets = [_account_to_dict(row) for row in rows]
    first = assets[0] if assets else {}
    account_id = str(cfg.account_id or first.get("account_id") or "")
    for item in assets:
        if not item.get("account_id"):
            item["account_id"] = account_id or None
    currency = str(first.get("currency") or "CNY")
    account = (
        {
            "account_number": account_id,
            "currency": currency,
            "cash": first.get("cash"),
            "available": first.get("available"),
            "equity": first.get("total_asset"),
            "buying_power": first.get("available"),
            "market_value": first.get("market_value"),
            "total_asset": first.get("total_asset"),
            "gross_assets": first.get("total_asset"),
            "net_assets": first.get("total_asset"),
            "frozen": first.get("frozen"),
        }
        if first
        else {}
    )
    return {
        "status": "ok",
        "profile": cfg.profile,
        "paper_guard": PAPER_GUARD,
        "account_id": account_id,
        "accounts": [account_id] if account_id else [],
        "assets": assets,
        "balances": [
            {
                "currency": item.get("currency") or "CNY",
                "net_assets": item.get("total_asset"),
                "total_cash": item.get("cash"),
                "buy_power": item.get("available"),
                "init_margin": item.get("frozen"),
                "maintenance_margin": None,
            }
            for item in assets
        ],
        "account": account,
    }


def _position_to_dict(row: Mapping[str, Any], *, account_id: str = "") -> dict[str, Any]:
    symbol = str(_first(row, ("stock_code", "symbol", "ticker", "code"), "") or "").upper()
    qty = _num(_first(row, ("volume", "qty", "quantity", "position")))
    avg_cost = _num(_first(row, ("avg_price", "open_price", "cost_price", "avg_cost")))
    name = _first(row, ("stock_name", "InstrumentName", "instrument_name", "name"))
    return {
        "account": account_id or None,
        "symbol": symbol,
        "name": str(name).strip() if name else None,
        "symbol_name": str(name).strip() if name else None,
        "qty": qty,
        "quantity": qty,
        "available_qty": _num(
            _first(row, ("can_use_volume", "available_qty", "enable_amount", "available"))
        ),
        "avg_cost": avg_cost,
        "cost_price": avg_cost,
        "market_value": _num(_first(row, ("market_value", "marketValue"))),
        "unrealized_pnl": _num(
            _first(row, ("profit", "float_profit", "unrealized_pnl", "pnl"))
        ),
        "last_price": _num(_first(row, ("last_price", "price", "lastPrice"))),
        "currency": "CNY",
    }


def _order_to_dict(row: Mapping[str, Any], *, account_id: str = "") -> dict[str, Any]:
    order_type = _first(row, ("order_type", "orderType", "side"))
    side = _ORDER_TYPE_SIDE.get(order_type)
    if side is None and isinstance(order_type, str):
        token = order_type.strip().lower()
        if token in {"buy", "sell"}:
            side = token
    symbol = str(_first(row, ("stock_code", "symbol", "ticker", "code"), "") or "").upper()
    qty = _num(_first(row, ("order_volume", "volume", "qty", "quantity")))
    price = _num(_first(row, ("price", "order_price", "limit_price")))
    status = _first(row, ("order_status", "status", "orderStatus"))
    action = side.upper() if isinstance(side, str) and side else side
    return {
        "account": account_id or None,
        "order_id": _first(row, ("order_id", "orderId", "order_sysid", "sysid")),
        "symbol": symbol,
        "side": side,
        "action": action,
        "qty": qty,
        "quantity": qty,
        "total_quantity": qty,
        "filled_qty": _num(
            _first(row, ("traded_volume", "filled_qty", "traded_qty", "filled"))
        ),
        "price": price,
        "limit_price": price,
        "status": status,
        "order_status": status,
        "order_type": order_type,
        "order_time": _first(row, ("order_time", "time")),
        "order_remark": _first(row, ("order_remark", "remark")),
        "contract": {"symbol": symbol, "local_symbol": symbol},
        "order": {
            "account": account_id or None,
            "action": action,
            "order_type": order_type,
            "total_quantity": qty,
            "limit_price": price,
        },
    }


def _trade_to_dict(row: Mapping[str, Any]) -> dict[str, Any]:
    symbol = str(_first(row, ("stock_code", "symbol", "ticker", "code"), "") or "").upper()
    qty = _num(_first(row, ("traded_volume", "volume", "qty", "quantity")))
    price = _num(_first(row, ("traded_price", "price")))
    return {
        "trade_id": _first(row, ("traded_id", "trade_id", "exec_id", "id")),
        "order_id": _first(row, ("order_id", "orderId")),
        "order_sysid": _first(row, ("order_sysid", "sysid")),
        "symbol": symbol,
        "qty": qty,
        "quantity": qty,
        "price": price,
        "amount": _num(_first(row, ("traded_amount", "amount", "turnover"))),
        "side": _ORDER_TYPE_SIDE.get(_first(row, ("order_type", "side"))),
        "time": _first(row, ("traded_time", "time", "order_time")),
        "strategy_name": _first(row, ("strategy_name", "strategyName")),
    }


def _quote_from_payload(payload: Any, code: str) -> dict[str, Any]:
    """Extract one symbol's tick from full_tick envelopes."""
    row: Mapping[str, Any] | None = None
    if isinstance(payload, Mapping):
        # Direct map keyed by symbol.
        for key in (code, code.upper(), code.lower()):
            candidate = payload.get(key)
            if isinstance(candidate, Mapping):
                row = candidate
                break
        if row is None:
            data = payload.get("data")
            if isinstance(data, Mapping):
                for key in (code, code.upper(), code.lower()):
                    candidate = data.get(key)
                    if isinstance(candidate, Mapping):
                        row = candidate
                        break
            elif isinstance(data, list) and data and isinstance(data[0], Mapping):
                row = data[0]
        if row is None and any(
            k in payload for k in ("lastPrice", "last", "price", "last_price")
        ):
            row = payload
    elif isinstance(payload, list) and payload and isinstance(payload[0], Mapping):
        row = payload[0]

    if row is None:
        return {}
    last = _num(_first(row, ("lastPrice", "last", "price", "last_price", "close")))
    bid = _num(_first(row, ("bidPrice", "bid_price", "bid1", "bid")))
    ask = _num(_first(row, ("askPrice", "ask_price", "ask1", "ask")))
    return {
        "symbol": code,
        "last": last,
        "open": _num(_first(row, ("open", "openPrice"))),
        "high": _num(_first(row, ("high", "highPrice"))),
        "low": _num(_first(row, ("low", "lowPrice"))),
        "volume": _num(_first(row, ("volume", "vol"))),
        "amount": _num(_first(row, ("amount", "turnover"))),
        "bid": bid,
        "ask": ask,
        "bid_price": bid,
        "ask_price": ask,
    }


def _bars_from_payload(payload: Any, *, symbol: str = "") -> list[dict[str, Any]]:
    if payload is None:
        return []
    if isinstance(payload, list):
        return [dict(item) if isinstance(item, Mapping) else {"value": item} for item in payload]
    if isinstance(payload, Mapping):
        # DataFrame-serialized styles or {data: [...]} / {data: {code: [...]}}
        for key in ("data", "bars", "result", "kline"):
            value = payload.get(key)
            if isinstance(value, list):
                return [
                    dict(item) if isinstance(item, Mapping) else {"value": item}
                    for item in value
                ]
            if isinstance(value, Mapping):
                for candidate in (symbol, symbol.upper(), symbol.lower()):
                    inner = value.get(candidate)
                    if isinstance(inner, list):
                        return [
                            dict(item) if isinstance(item, Mapping) else {"value": item}
                            for item in inner
                        ]
                for inner in value.values():
                    if isinstance(inner, list):
                        return [
                            dict(item) if isinstance(item, Mapping) else {"value": item}
                            for item in inner
                        ]
        # Column-oriented: {open: [...], close: [...], ...}
        closes = payload.get("close")
        if isinstance(closes, list):
            keys = [k for k, v in payload.items() if isinstance(v, list)]
            length = len(closes)
            rows: list[dict[str, Any]] = []
            for i in range(length):
                rows.append({k: payload[k][i] for k in keys if i < len(payload[k])})
            return rows
        # Single bar dict
        if any(k in payload for k in ("open", "high", "low", "close")):
            return [dict(payload)]
    return []


def _bar_to_dict(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "time": _format_bar_time(_first(row, ("time", "date", "datetime", "index"))),
        "open": _num(_first(row, ("open", "Open"))),
        "high": _num(_first(row, ("high", "High"))),
        "low": _num(_first(row, ("low", "Low"))),
        "close": _num(_first(row, ("close", "Close"))),
        "volume": _num(_first(row, ("volume", "vol", "Volume"))),
    }


def _format_bar_time(value: Any) -> str:
    """Normalize QMT / Bridge bar timestamps to a readable date or datetime string.

    Accepts epoch ms/sec, ``YYYYMMDD``, ``YYYYMMDDHHmmss``, ISO strings, and
    numeric day codes. Daily values become ``YYYY-MM-DD``; intraday keep
    ``YYYY-MM-DD HH:MM``.
    """
    if value is None or value == "":
        return ""
    if isinstance(value, datetime):
        if value.hour or value.minute or value.second:
            return value.strftime("%Y-%m-%d %H:%M")
        return value.strftime("%Y-%m-%d")

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = int(value)
        # Epoch milliseconds
        if number >= 10**12:
            dt = datetime.fromtimestamp(number / 1000.0)
            if dt.hour == 0 and dt.minute == 0 and dt.second == 0:
                return dt.strftime("%Y-%m-%d")
            # A-share daily bars often stamp session close (15:00).
            if dt.hour == 15 and dt.minute == 0:
                return dt.strftime("%Y-%m-%d")
            return dt.strftime("%Y-%m-%d %H:%M")
        # Epoch seconds
        if number >= 10**9:
            dt = datetime.fromtimestamp(number)
            if dt.hour == 0 and dt.minute == 0 and dt.second == 0:
                return dt.strftime("%Y-%m-%d")
            if dt.hour == 15 and dt.minute == 0:
                return dt.strftime("%Y-%m-%d")
            return dt.strftime("%Y-%m-%d %H:%M")
        # Compact YYYYMMDD / YYYYMMDDHHmmss as int
        digits = str(number)
        if len(digits) == 8:
            return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
        if len(digits) == 14:
            return (
                f"{digits[:4]}-{digits[4:6]}-{digits[6:8]} "
                f"{digits[8:10]}:{digits[10:12]}"
            )
        return digits

    text = str(value).strip()
    if not text:
        return ""
    if text.isdigit():
        return _format_bar_time(int(text))
    # ISO / "YYYY-MM-DD HH:MM:SS"
    normalized = text.replace("T", " ").replace("/", "-")
    if len(normalized) >= 19 and normalized[4] == "-" and normalized[10] == " ":
        # Keep minutes for intraday; drop seconds.
        return normalized[:16]
    if len(normalized) >= 10 and normalized[4] == "-" and normalized[7] == "-":
        return normalized[:10]
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    if len(text) == 14 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:8]} {text[8:10]}:{text[10:12]}"
    return text
