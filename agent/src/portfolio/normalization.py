"""Normalization and valuation helpers for connector payloads.

The generic path is the default: any connector whose account and position rows
use the common field names is handled without a line of code here. Two
connectors ship payload shapes the generic reader cannot express and therefore
have dedicated branches:

* ``ibkr`` — the account is a tag/value ``summary`` list, and positions carry
  ``position``/``avg_cost`` with an IB ``sec_type``.
* ``longbridge`` — the account is a per-currency ``balances`` list, and
  positions are market-suffixed symbols with a ``symbol_name``.

Inside the generic path, ``binance`` and ``okx`` additionally classify holdings
as crypto/stablecoin and quote against USDT. Everything else falls through
unchanged, so adding a connector needs no edit to this module.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from src.trading.types import TradingProfile

STABLECOINS = frozenset({"USDT", "USDC", "FDUSD", "TUSD", "BUSD"})

# Futu/moomoo style ``HK.00700`` / ``US.AAPL`` / ``SH.600519`` prefixes, plus the
# more common ``700.HK`` / ``AAPL.US`` suffixes used by Longbridge and loaders.
_SYMBOL_CURRENCY_BY_PREFIX = {
    "HK": ("HKD", "HK"),
    "US": ("USD", "US"),
    "SH": ("CNY", "SH"),
    "SZ": ("CNY", "SZ"),
    "CN": ("CNY", "CN"),
    "SG": ("SGD", "SG"),
    "JP": ("JPY", "JP"),
}
_SYMBOL_CURRENCY_BY_SUFFIX = {
    "HK": ("HKD", "HK"),
    "US": ("USD", "US"),
    "SH": ("CNY", "SH"),
    "SZ": ("CNY", "SZ"),
    "SS": ("CNY", "SH"),
    "SG": ("SGD", "SG"),
    "JP": ("JPY", "JP"),
    "T": ("JPY", "JP"),
}

_TRANSPORT_AUTH = {
    "remote_mcp": ("OAuth", "automatic"),
    "local_tws": ("Local broker session", "session"),
    "broker_sdk": ("API credentials", "provider_managed"),
    "local_plugin": ("Local connector", "provider_managed"),
}


def _decimal(value: Any, default: Decimal = Decimal("0")) -> Decimal:
    try:
        if value is None or value == "":
            return default
        result = Decimal(str(value))
        return result if result.is_finite() else default
    except (InvalidOperation, ValueError, TypeError):
        return default


def _number(value: Decimal) -> float:
    return float(value.quantize(Decimal("0.00000001")))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _infer_currency_and_market(
    symbol: str, *, currency: str = "", market: str = ""
) -> tuple[str, str]:
    """Infer settlement currency and market from a symbol when the connector omits them.

    Args:
        symbol: Instrument code such as ``HK.00700``, ``700.HK`` or ``AAPL``.
        currency: Connector-reported currency, if any.
        market: Connector-reported market, if any.

    Returns:
        ``(currency, market)`` with blanks filled from the symbol when possible.
    """
    cleaned_currency = str(currency or "").strip().upper()
    if cleaned_currency in {"N/A", "NONE"}:
        cleaned_currency = ""
    if cleaned_currency == "CNH":
        cleaned_currency = "CNY"
    cleaned_market = str(market or "").strip().upper()
    if cleaned_market in {"N/A", "NONE"}:
        cleaned_market = ""

    token = str(symbol or "").strip().upper()
    prefix = ""
    suffix = ""
    if "." in token:
        left, _, right = token.partition(".")
        if left in _SYMBOL_CURRENCY_BY_PREFIX:
            prefix = left
        if right in _SYMBOL_CURRENCY_BY_SUFFIX:
            suffix = right

    inferred_currency = ""
    inferred_market = ""
    if prefix:
        inferred_currency, inferred_market = _SYMBOL_CURRENCY_BY_PREFIX[prefix]
    elif suffix:
        inferred_currency, inferred_market = _SYMBOL_CURRENCY_BY_SUFFIX[suffix]

    # Prefer the instrument market when a connector reports a generic account
    # settlement currency (often USD) that conflicts with HK./SH./SZ. codes.
    if inferred_currency and (
        not cleaned_currency
        or (cleaned_currency == "USD" and inferred_currency != "USD")
    ):
        cleaned_currency = inferred_currency
        if inferred_market:
            cleaned_market = inferred_market
    elif not cleaned_currency:
        cleaned_currency = "USD"

    if not cleaned_market:
        cleaned_market = inferred_market
    return cleaned_currency, cleaned_market


def normalize_position(broker: str, row: dict[str, Any]) -> dict[str, Any]:
    """Convert a connector position row to the portfolio wire shape.

    Args:
        broker: The connector key, used only to select a dedicated payload
            shape; unknown connectors take the generic path.
        row: One raw position row as the connector reported it.

    Returns:
        A position dict in the portfolio wire shape, unpriced fields left as
        ``None`` rather than guessed.
    """
    if broker == "ibkr":
        symbol = str(row.get("symbol") or row.get("local_symbol") or "").upper()
        sec_type = str(row.get("sec_type") or "STK").upper()
        market_price = _decimal(row.get("market_price", row.get("current_price")))
        currency, market = _infer_currency_and_market(
            symbol,
            currency=str(row.get("currency") or "USD"),
            market=str(row.get("exchange") or ""),
        )
        return {
            "broker": broker,
            "symbol": symbol,
            "quote_symbol": symbol,
            "name": symbol,
            "asset_type": "etf" if sec_type == "ETF" else "stock",
            "market": market or str(row.get("exchange") or ""),
            "exchange": row.get("exchange"),
            "currency": currency,
            "quantity": _number(_decimal(row.get("position", row.get("quantity")))),
            "cost_price": _number(_decimal(row.get("avg_cost", row.get("average_cost")))),
            "market_price": _number(market_price) if market_price > 0 else None,
            "source_market_value": row.get("market_value"),
            "source_unrealized_pnl": row.get("unrealized_pnl"),
            "contract_id": row.get("contract_id"),
            "updated_at": _now(),
        }
    if broker == "longbridge":
        symbol = str(row.get("symbol") or "").upper()
        currency, market = _infer_currency_and_market(
            symbol,
            currency=str(row.get("currency") or ""),
            market=str(row.get("market") or symbol.rsplit(".", 1)[-1]),
        )
        return {
            "broker": broker,
            "symbol": symbol,
            "quote_symbol": symbol,
            "name": str(row.get("symbol_name") or symbol),
            "asset_type": "stock",
            "market": market,
            "currency": currency,
            "quantity": _number(_decimal(row.get("quantity"))),
            "cost_price": _number(_decimal(row.get("cost_price"))),
            "updated_at": _now(),
        }

    symbol = str(row.get("symbol") or row.get("code") or row.get("ticker") or "").upper()
    currency, market = _infer_currency_and_market(
        symbol,
        currency=str(row.get("currency") or ""),
        market=str(row.get("market") or row.get("exchange") or ""),
    )
    if not market:
        market = str(row.get("market") or row.get("exchange") or broker).upper()
    source = str(row.get("source") or ("spot" if broker == "binance" else "account"))
    if not currency:
        currency = (
            "HKD"
            if symbol.endswith(".HK") or symbol.startswith("HK.")
            else "CNY"
            if symbol.startswith(("SH.", "SZ.", "BJ."))
            else "USD"
        )
    quantity = _decimal(
        row.get(
            "quantity",
            row.get(
                "qty",
                row.get(
                    "position",
                    row.get("position_qty", row.get("volume", row.get("units"))),
                ),
            ),
        )
    )
    cost = _decimal(
        row.get(
            "cost_price",
            row.get(
                "average_cost",
                row.get(
                    "avg_cost",
                    row.get(
                        "avg_entry_price",
                        row.get(
                            "average_price",
                            row.get("price_open", row.get("open_rate")),
                        ),
                    ),
                ),
            ),
        )
    )
    market_price = _decimal(
        row.get(
            "market_price",
            row.get("current_price", row.get("ltp", row.get("price_current"))),
        )
    )
    source_market_value = row.get("market_value", row.get("market_val", row.get("value")))
    if market_price <= 0 and quantity != 0 and _decimal(source_market_value) > 0:
        market_price = abs(_decimal(source_market_value) / quantity)
    sec_type = str(row.get("sec_type") or "").upper()
    declared_asset_type = str(row.get("asset_type") or "").strip().lower()
    crypto = broker in {"binance", "okx"} or declared_asset_type in {
        "crypto",
        "stablecoin",
    }
    asset_type = declared_asset_type or (
        "stablecoin" if symbol in STABLECOINS else "crypto" if crypto else "etf" if sec_type == "ETF" else "stock"
    )
    return {
        "broker": broker,
        "symbol": symbol,
        "quote_symbol": str(row.get("quote_symbol") or (f"{symbol}/USDT" if broker == "binance" else symbol)),
        "name": str(
            row.get("name")
            or row.get("symbol_name")
            or (f"{symbol} (Simple Earn)" if source == "simple_earn_flexible" else symbol)
        ),
        "asset_type": asset_type,
        "market": market,
        "currency": currency,
        "quantity": _number(quantity),
        "cost_price": _number(cost) if cost > 0 else None,
        "market_price": _number(market_price) if market_price > 0 else None,
        "price_currency": str(row.get("price_currency") or currency).upper(),
        "source_market_value": source_market_value,
        "source_unrealized_pnl": row.get(
            "unrealized_pnl",
            row.get("unrealized_pl", row.get("pnl", row.get("profit"))),
        ),
        "free": row.get("free"),
        "used": row.get("used"),
        "source": source,
        "updated_at": _now(),
    }


def value_position(
    row: dict[str, Any], *, usd_hkd: Decimal, usd_cny: Decimal
) -> dict[str, Any]:
    """Calculate native/USD/CNY market value and unrealized P/L.

    Args:
        row: A normalized position row; it is updated in place.
        usd_hkd: USD/HKD rate used to convert HKD-priced rows.
        usd_cny: USD/CNY rate used to convert CNY-priced rows.

    Returns:
        The same row, with ``priced``, native and USD/CNY valuation fields
        filled in and the connector-only fields dropped.
    """
    price = _decimal(row.get("market_price"))
    quantity = _decimal(row.get("quantity"))
    currency = str(row.get("price_currency") or row.get("currency") or "USD").upper()
    if currency == "CNH":
        currency = "CNY"
    fx_to_usd = Decimal("1")
    if currency == "HKD":
        fx_to_usd = Decimal("1") / usd_hkd if usd_hkd else Decimal("0")
    elif currency in {"CNY", "CNH"}:
        fx_to_usd = Decimal("1") / usd_cny if usd_cny else Decimal("0")
    priced = price > 0
    source_market_value = row.get("source_market_value")
    market_native = (
        _decimal(source_market_value)
        if source_market_value is not None and _decimal(source_market_value) != 0
        else (quantity * price if priced else Decimal("0"))
    )
    market_usd = market_native * fx_to_usd if priced or market_native != 0 else Decimal("0")
    cost = _decimal(row.get("cost_price"))
    source_pnl = row.get("source_unrealized_pnl")
    pnl_native = (
        _decimal(source_pnl)
        if source_pnl is not None
        else ((price - cost) * quantity if priced and cost > 0 else None)
    )
    pnl_usd = (
        _decimal(source_pnl) * fx_to_usd
        if priced and source_pnl is not None
        else (price - cost) * quantity * fx_to_usd
        if priced and cost > 0
        else pnl_native * fx_to_usd
        if pnl_native is not None
        else None
    )
    row.update(
        currency=currency,
        price_currency=currency,
        priced=priced or market_native != 0,
        market_value=_number(market_native),
        market_value_usd=_number(market_usd),
        market_value_cny=_number(market_usd * usd_cny),
        unrealized_pnl=_number(pnl_native) if pnl_native is not None else None,
        unrealized_pnl_usd=_number(pnl_usd) if pnl_usd is not None else None,
    )
    for key in (
        "quote_symbol",
        "exchange",
        "source_market_value",
        "source_unrealized_pnl",
        "contract_id",
    ):
        row.pop(key, None)
    return row


def _to_usd(
    value: Decimal, currency: str, usd_hkd: Decimal, usd_cny: Decimal
) -> Decimal:
    code = "CNY" if currency == "CNH" else currency
    if code == "HKD":
        return value / usd_hkd if usd_hkd else Decimal("0")
    if code == "CNY":
        return value / usd_cny if usd_cny else Decimal("0")
    return value


def _account_containers(account: dict[str, Any]):
    nested = account.get("account") if isinstance(account.get("account"), dict) else {}
    if nested:
        yield nested
    for row in account.get("assets") or []:
        if isinstance(row, dict):
            yield row
    for row in account.get("balances") or []:
        if isinstance(row, dict):
            yield row


def _first_positive(mapping: dict[str, Any], keys: tuple[str, ...]) -> Decimal:
    for key in keys:
        value = _decimal(mapping.get(key))
        if value > 0:
            return value
    return Decimal("0")


def _futu_financing_debt(row: dict[str, Any]) -> Decimal:
    """富途融资负债：debt_cash / 计息金额优先，负现金兜底。"""
    for key in ("total_debt", "debt_cash", "interest_charged_amount"):
        value = _decimal(row.get(key))
        if value > 0:
            return value
    cash = _decimal(row.get("cash"))
    if cash < 0:
        return abs(cash)
    return Decimal("0")


def _futu_row_amount_usd(
    row: dict[str, Any],
    keys: tuple[str, ...],
    *,
    usd_hkd: Decimal,
    usd_cny: Decimal,
    fallback_currency: str,
) -> Decimal:
    value = _first_positive(row, keys)
    if value <= 0:
        return Decimal("0")
    currency = str(row.get("currency") or fallback_currency).upper() or "HKD"
    return _to_usd(value, currency, usd_hkd, usd_cny)


def account_net_assets_usd(
    broker: str,
    account: dict[str, Any],
    usd_hkd: Decimal,
    usd_cny: Decimal,
    fallback: Decimal = Decimal("0"),
) -> Decimal:
    """Extract broker-reported net equity (净资产), after financing liabilities."""
    if broker == "futu":
        nested = account.get("account") if isinstance(account.get("account"), dict) else {}
        fallback_currency = str(nested.get("currency") or "HKD").upper()
        net = _futu_row_amount_usd(
            nested,
            ("net_assets", "total_assets"),
            usd_hkd=usd_hkd,
            usd_cny=usd_cny,
            fallback_currency=fallback_currency,
        )
        if net > 0:
            return net
        total = Decimal("0")
        for row in account.get("assets", []):
            if not isinstance(row, dict):
                continue
            total += _futu_row_amount_usd(
                row,
                ("net_assets", "total_assets"),
                usd_hkd=usd_hkd,
                usd_cny=usd_cny,
                fallback_currency=fallback_currency,
            )
        return total if total > 0 else fallback
    if broker == "longbridge":
        total = Decimal("0")
        for row in account.get("balances", []):
            total += _to_usd(
                _decimal(row.get("net_assets")),
                str(row.get("currency") or "USD").upper(),
                usd_hkd,
                usd_cny,
            )
        return total
    if broker == "ibkr":
        candidates: dict[str, Decimal] = {}
        for row in account.get("summary", []):
            if str(row.get("tag") or "").lower() == "netliquidation":
                currency = str(row.get("currency") or "USD").upper()
                candidates[currency] = max(candidates.get(currency, Decimal("0")), _decimal(row.get("value")))
        if "USD" in candidates:
            return candidates["USD"]
        return sum(
            (_to_usd(value, currency, usd_hkd, usd_cny) for currency, value in candidates.items()),
            Decimal("0"),
        )
    nested = account.get("account") if isinstance(account.get("account"), dict) else {}
    currency = str(nested.get("currency") or "USD").upper()
    for container in _account_containers(account):
        net = _first_positive(
            container,
            (
                "net_assets",
                "m_dAssureAsset",
                "m_d_assure_asset",
                "assure_asset",
                "equity",
                "net_liquidation",
            ),
        )
        if net > 0:
            ccy = str(container.get("currency") or currency).upper()
            return _to_usd(net, ccy, usd_hkd, usd_cny)
    for key in ("portfolio_value", "total_equity", "equity"):
        value = _decimal(nested.get(key))
        if value > 0:
            return _to_usd(value, currency, usd_hkd, usd_cny)
    total = Decimal("0")
    for row in account.get("assets", []):
        value = _decimal(row.get("net_liquidation", row.get("total_assets", row.get("equity"))))
        total += _to_usd(value, str(row.get("currency") or currency).upper(), usd_hkd, usd_cny)
    return total if total > 0 else fallback


def account_gross_assets_usd(
    broker: str,
    account: dict[str, Any],
    usd_hkd: Decimal,
    usd_cny: Decimal,
    fallback: Decimal = Decimal("0"),
) -> Decimal:
    """Extract broker-reported gross total assets (总资产), before financing liabilities."""
    if broker == "futu":
        nested = account.get("account") if isinstance(account.get("account"), dict) else {}
        fallback_currency = str(nested.get("currency") or "HKD").upper()
        gross = _futu_row_amount_usd(
            nested,
            ("gross_assets",),
            usd_hkd=usd_hkd,
            usd_cny=usd_cny,
            fallback_currency=fallback_currency,
        )
        if gross <= 0:
            net = _futu_row_amount_usd(
                nested,
                ("net_assets", "total_assets"),
                usd_hkd=usd_hkd,
                usd_cny=usd_cny,
                fallback_currency=fallback_currency,
            )
            debt = _futu_financing_debt(nested)
            gross = net + debt if net > 0 else Decimal("0")
        if gross > 0:
            return gross
        total = Decimal("0")
        for row in account.get("assets", []):
            if not isinstance(row, dict):
                continue
            row_gross = _futu_row_amount_usd(
                row,
                ("gross_assets",),
                usd_hkd=usd_hkd,
                usd_cny=usd_cny,
                fallback_currency=fallback_currency,
            )
            if row_gross <= 0:
                net = _futu_row_amount_usd(
                    row,
                    ("net_assets", "total_assets"),
                    usd_hkd=usd_hkd,
                    usd_cny=usd_cny,
                    fallback_currency=fallback_currency,
                )
                debt = _futu_financing_debt(row)
                row_gross = net + debt if net > 0 else Decimal("0")
            total += row_gross
        return total if total > 0 else account_net_assets_usd(
            broker, account, usd_hkd, usd_cny, fallback
        )
    if broker == "ibkr":
        gross_candidates: dict[str, Decimal] = {}
        for row in account.get("summary", []):
            if str(row.get("tag") or "").lower() != "grosspositionvalue":
                continue
            currency = str(row.get("currency") or "USD").upper()
            gross_candidates[currency] = max(
                gross_candidates.get(currency, Decimal("0")), _decimal(row.get("value"))
            )
        if "USD" in gross_candidates:
            return gross_candidates["USD"]
        converted = sum(
            (
                _to_usd(value, currency, usd_hkd, usd_cny)
                for currency, value in gross_candidates.items()
            ),
            Decimal("0"),
        )
        if converted > 0:
            return converted
    nested = account.get("account") if isinstance(account.get("account"), dict) else {}
    currency = str(nested.get("currency") or "USD").upper()
    for container in _account_containers(account):
        gross = _first_positive(
            container,
            (
                "gross_assets",
                "m_dBalance",
                "m_d_balance",
                "balance",
                "total_asset",
                "total_assets",
                "portfolio_value",
            ),
        )
        if gross > 0:
            ccy = str(container.get("currency") or currency).upper()
            return _to_usd(gross, ccy, usd_hkd, usd_cny)
    if broker == "longbridge":
        return account_net_assets_usd(broker, account, usd_hkd, usd_cny, fallback)
    net = account_net_assets_usd(broker, account, usd_hkd, usd_cny, fallback)
    return net if net > 0 else fallback


def account_total_usd(
    broker: str,
    account: dict[str, Any],
    usd_hkd: Decimal,
    usd_cny: Decimal,
    fallback: Decimal = Decimal("0"),
) -> Decimal:
    """Backward-compatible alias for :func:`account_net_assets_usd`."""
    return account_net_assets_usd(broker, account, usd_hkd, usd_cny, fallback)


def account_cash_usd(
    broker: str, account: dict[str, Any], usd_hkd: Decimal, usd_cny: Decimal
) -> Decimal:
    """Return broker-reported cash without guessing from missing quotes.

    Args:
        broker: The connector key selecting the account payload shape.
        account: The raw account payload.
        usd_hkd: USD/HKD rate for HKD-denominated balances.
        usd_cny: USD/CNY rate for CNY-denominated balances.

    Returns:
        Cash in USD, never negative and never inferred from an unpriced
        position.
    """
    rows = account.get("balances", []) if broker == "longbridge" else []
    if rows:
        return max(
            Decimal("0"),
            sum(
                (
                    _to_usd(
                        _decimal(row.get("total_cash")),
                        str(row.get("currency") or "USD").upper(),
                        usd_hkd,
                        usd_cny,
                    )
                    for row in rows
                ),
                Decimal("0"),
            ),
        )
    if broker == "ibkr":
        candidates: dict[str, Decimal] = {}
        for row in account.get("summary", []):
            if str(row.get("tag") or "").lower() in {"totalcashvalue", "cashbalance"}:
                currency = str(row.get("currency") or "USD").upper()
                candidates[currency] = max(candidates.get(currency, Decimal("0")), _decimal(row.get("value")))
        if "USD" in candidates:
            return max(Decimal("0"), candidates["USD"])
        return max(
            Decimal("0"),
            sum(
                (_to_usd(value, currency, usd_hkd, usd_cny) for currency, value in candidates.items()),
                Decimal("0"),
            ),
        )
    nested = account.get("account") if isinstance(account.get("account"), dict) else {}
    currency = str(nested.get("currency") or "USD").upper()
    nested_cash = _decimal(nested.get("cash"))
    if nested_cash > 0:
        return _to_usd(nested_cash, currency, usd_hkd, usd_cny)
    return max(
        Decimal("0"),
        sum(
            (
                _to_usd(
                    _decimal(row.get("cash", row.get("cash_balance"))),
                    str(row.get("currency") or currency).upper(),
                    usd_hkd,
                    usd_cny,
                )
                for row in account.get("assets", [])
            ),
            Decimal("0"),
        ),
    )


def auth_metadata(profile: TradingProfile) -> dict[str, Any]:
    """Return public profile metadata without inferring broker-side key permissions.

    Args:
        profile: The connector profile backing a portfolio source.

    Returns:
        The authentication method, renewal model, read-only flag and the
        profile's own notes.
    """
    method, renewal = _TRANSPORT_AUTH.get(
        profile.transport,
        (profile.transport.replace("_", " ").title(), "provider_managed"),
    )
    return {
        "method": method,
        "renewal": renewal,
        "readonly": profile.readonly,
        "detail": profile.notes or "Read-only connector profile.",
    }
