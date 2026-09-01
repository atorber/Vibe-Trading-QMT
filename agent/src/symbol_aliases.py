"""Canonical A-share index symbols and alias resolution for tools."""

from __future__ import annotations

import re
from typing import Any

# Canonical index codes used across Markets API, autopilot, and agent tools.
A_SHARE_INDEX_CODES: dict[str, str] = {
    "000001.SH": "上证指数",
    "399001.SZ": "深证成指",
    "399006.SZ": "创业板指",
    "000300.SH": "沪深300",
    "000016.SH": "上证50",
    "000905.SH": "中证500",
    "000852.SH": "中证1000",
}

_A_SHARE_SYMBOL_RE = re.compile(r"^\d{6}\.(SH|SZ|BJ)$", re.IGNORECASE)


def _norm_alias(value: str) -> str:
    """Normalize free-text aliases for lookup."""
    text = value.strip().lower()
    text = text.replace("Ａ", "a").replace("股", "")
    text = re.sub(r"[\s_\-]+", "", text)
    text = text.replace("index", "").replace("composite", "")
    return text


_EXTRA_ALIASES: dict[str, str] = {
    "上证指数": "000001.SH",
    "上证综指": "000001.SH",
    "上证综合": "000001.SH",
    "shanghaicomposite": "000001.SH",
    "ssecomposite": "000001.SH",
    "深证成指": "399001.SZ",
    "深成指": "399001.SZ",
    "shenzhencomponent": "399001.SZ",
    "创业板指": "399006.SZ",
    "创业板": "399006.SZ",
    "chinext": "399006.SZ",
    "gem": "399006.SZ",
    "沪深300": "000300.SH",
    "csi300": "000300.SH",
    "csi 300": "000300.SH",
    "hs300": "000300.SH",
    "上证50": "000016.SH",
    "sse50": "000016.SH",
    "中证500": "000905.SH",
    "csi500": "000905.SH",
    "csi 500": "000905.SH",
    "中证1000": "000852.SH",
    "csi1000": "000852.SH",
}

_ALIAS_TO_CODE: dict[str, str] = {}
for code, name in A_SHARE_INDEX_CODES.items():
    _ALIAS_TO_CODE[_norm_alias(code)] = code
    _ALIAS_TO_CODE[_norm_alias(name)] = code
for alias, code in _EXTRA_ALIASES.items():
    _ALIAS_TO_CODE[_norm_alias(alias)] = code

# Four headline indices for the daily A-share brief SOP.
A_SHARE_BRIEF_INDEX_CODES = [
    "000001.SH",
    "399001.SZ",
    "399006.SZ",
    "000300.SH",
]

# Explicit sources that cannot serve A-share OHLCV reliably when requested alone.
_A_SHARE_INCOMPATIBLE_SOURCES = frozenset({"yfinance", "yahoo"})


def is_a_share_symbol(code: str) -> bool:
    """Return True for ``NNNNNN.SH/SZ/BJ`` symbols."""
    return bool(_A_SHARE_SYMBOL_RE.match(code.strip()))


def is_a_share_index(code: str) -> bool:
    """Return True for major A-share index symbols."""
    upper = code.strip().upper()
    if upper in A_SHARE_INDEX_CODES:
        return True
    if upper.endswith(".SH"):
        digits = upper.split(".")[0]
        return len(digits) == 6 and digits.isdigit() and digits.startswith("000")
    if upper.endswith(".SZ"):
        digits = upper.split(".")[0]
        return len(digits) == 6 and digits.isdigit() and digits.startswith("399")
    return False


def resolve_a_share_index_alias(query: str) -> tuple[str, str] | None:
    """Resolve a Chinese/English index alias to ``(code, display_name)``."""
    text = query.strip()
    if not text:
        return None
    upper = text.upper()
    if upper in A_SHARE_INDEX_CODES:
        return upper, A_SHARE_INDEX_CODES[upper]
    key = _norm_alias(text)
    code = _ALIAS_TO_CODE.get(key)
    if code:
        return code, A_SHARE_INDEX_CODES[code]
    return None


def normalize_market_data_codes(codes: list[str]) -> tuple[list[str], dict[str, str]]:
    """Resolve index aliases in a code list; return canonical codes + alias map."""
    normalized: list[str] = []
    alias_map: dict[str, str] = {}
    for raw in codes:
        text = raw.strip()
        if not text:
            continue
        resolved = resolve_a_share_index_alias(text)
        if resolved is not None:
            code, _ = resolved
            alias_map[code] = text
            normalized.append(code)
            continue
        normalized.append(text.upper() if is_a_share_symbol(text) else text)
    return normalized, alias_map


def validate_a_share_source(codes: list[str], source: str) -> str | None:
    """Return an error message when ``source`` cannot serve these A-share codes."""
    if source in ("auto", *()):
        return None
    if source not in _A_SHARE_INCOMPATIBLE_SOURCES:
        return None
    a_share = [code for code in codes if is_a_share_symbol(code)]
    if not a_share:
        return None
    indices = [code for code in a_share if is_a_share_index(code)]
    hint = (
        "Use source='auto' (qmt/tencent/mootdx/tushare/eastmoney chain) "
        "or source='tencent' for A-share indices."
    )
    if indices:
        return (
            f"source={source!r} does not serve A-share indices "
            f"({', '.join(indices)}). {hint}"
        )
    return (
        f"source={source!r} is not in the A-share fallback chain for "
        f"{', '.join(a_share)}. {hint}"
    )


def index_search_candidate(code: str, name: str) -> dict[str, Any]:
    """Build a single candidate row for ``search_symbol`` short-circuit."""
    return {
        "symbol": code,
        "name": name,
        "market": "cn",
        "venue": code.rsplit(".", 1)[-1],
        "source": "builtin_index_alias",
    }
