"""CSI300 bench loader falls back to akshare when Tushare adj_factor is unavailable."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pandas as pd
import pytest

from src.tools import alpha_bench_tool as tool


def _daily_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "trade_date": ["20240102", "20240103"],
            "open": [10.0, 10.5],
            "high": [11.0, 11.5],
            "low": [9.0, 9.5],
            "close": [10.5, 11.0],
            "vol": [100.0, 120.0],
            "amount": [105.0, 126.0],
        }
    )


class _FakeTusharePro:
    def daily(self, **_kwargs) -> pd.DataFrame:
        return _daily_frame()

    def adj_factor(self, **_kwargs) -> pd.DataFrame:
        return pd.DataFrame()

    def index_weight(self, **_kwargs) -> pd.DataFrame:
        raise RuntimeError("no index_weight permission")


def _install_fake_tushare(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        tool,
        "get_env_config",
        lambda: SimpleNamespace(data=SimpleNamespace(tushare_token="test-token")),
    )
    monkeypatch.setitem(
        __import__("sys").modules,
        "tushare",
        SimpleNamespace(pro_api=lambda _token: _FakeTusharePro()),
    )


def test_csi300_uses_akshare_when_adj_factor_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_tushare(monkeypatch)
    monkeypatch.setattr(
        tool,
        "_fetch_csi300_constituents_akshare",
        lambda: (["600519.SH", "000001.SZ", "000002.SZ", "600036.SH", "000858.SZ"], "2026-09-01"),
    )

    akshare_frames = {
        code: pd.DataFrame(
            {
                "open": [10.0, 11.0],
                "high": [11.0, 12.0],
                "low": [9.0, 10.0],
                "close": [10.5, 11.5],
                "volume": [100.0, 110.0],
                "amount": [1.05, 1.15],
            },
            index=pd.to_datetime(["2024-01-02", "2024-01-03"]),
        )
        for code in ["600519.SH", "000001.SZ", "000002.SZ", "600036.SH", "000858.SZ"]
    }

    def _fake_akshare_fetch(codes: list[str], start: str, end: str) -> dict[str, pd.DataFrame]:
        del start, end
        return {code: akshare_frames[code] for code in codes if code in akshare_frames}

    monkeypatch.setattr(tool, "_fetch_csi300_prices_akshare", _fake_akshare_fetch)
    monkeypatch.setattr(tool, "_fetch_csi300_prices_qmt", lambda *_args, **_kwargs: {})

    panel = tool._load_csi300_panel("2024-01-01", "2024-01-31")

    assert panel["close"].shape[1] == 5
    assert panel["_meta"]["price_adjustment"] == "akshare qfq"
    assert panel["_meta"]["constituent_source"] == "akshare index_stock_cons_csindex"
