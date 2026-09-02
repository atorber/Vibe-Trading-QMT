"""CSI300 bench loader falls back to QMT Bridge before akshare."""

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


def test_csi300_uses_qmt_before_akshare(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_tushare(monkeypatch)
    codes = ["600519.SH", "000001.SZ", "000002.SZ", "600036.SH", "000858.SZ"]
    monkeypatch.setattr(
        tool,
        "_fetch_csi300_constituents_akshare",
        lambda: (codes, "2026-09-01"),
    )

    qmt_frames = {
        code: pd.DataFrame(
            {
                "open": [10.0, 11.0],
                "high": [11.0, 12.0],
                "low": [9.0, 10.0],
                "close": [10.5, 11.5],
                "volume": [10000.0, 11000.0],  # shares
            },
            index=pd.to_datetime(["2024-01-02", "2024-01-03"]),
        )
        for code in codes
    }

    def _fake_qmt_fetch(codes: list[str], start: str, end: str) -> dict[str, pd.DataFrame]:
        del start, end
        out: dict[str, pd.DataFrame] = {}
        for code in codes:
            normalized = tool._normalize_qmt_daily_for_bench(qmt_frames[code])
            if normalized is not None:
                out[code] = normalized
        return out

    monkeypatch.setattr(tool, "_fetch_csi300_prices_qmt", _fake_qmt_fetch)
    akshare_mock = MagicMock(return_value={})
    monkeypatch.setattr(tool, "_fetch_csi300_prices_akshare", akshare_mock)

    panel = tool._load_csi300_panel("2024-01-01", "2024-01-31")

    assert panel["close"].shape[1] == 5
    assert panel["_meta"]["price_adjustment"] == "qmt bridge qfq (dividend_type=front)"
    assert "amount" in panel
    akshare_mock.assert_not_called()


def test_normalize_qmt_daily_converts_shares_to_tushare_units() -> None:
    raw = pd.DataFrame(
        {
            "open": [10.0],
            "high": [11.0],
            "low": [9.0],
            "close": [10.0],
            "volume": [10000.0],
        },
        index=pd.to_datetime(["2024-01-02"]),
    )
    out = tool._normalize_qmt_daily_for_bench(raw)
    assert out is not None
    assert float(out.iloc[0]["volume"]) == 100.0  # 手
    assert float(out.iloc[0]["amount"]) == pytest.approx(100.0)  # 千元
