"""Tests for akshare_fallbacks parsing helpers."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pandas as pd

from src.tools import akshare_fallbacks as af


class TestIndustryBoardRanking:
    def test_fetch_industry_board_ranking_shapes_rows(self):
        frame = pd.DataFrame(
            [
                {
                    "板块代码": "BK0477",
                    "板块名称": "白酒",
                    "最新价": 12345.0,
                    "涨跌幅": 3.4,
                    "上涨家数": 18,
                    "下跌家数": 2,
                    "领涨股票": "贵州茅台",
                },
                {
                    "板块代码": "BK0727",
                    "板块名称": "银行",
                    "最新价": 6789.0,
                    "涨跌幅": 1.1,
                    "上涨家数": 30,
                    "下跌家数": 12,
                    "领涨股票": "-",
                },
            ]
        )
        mock_ak = MagicMock()
        mock_ak.stock_board_industry_name_em.return_value = frame
        with patch.dict("sys.modules", {"akshare": mock_ak}):
            boards = af.fetch_industry_board_ranking(1)

        assert len(boards) == 1
        assert boards[0]["board_name"] == "白酒"
        assert boards[0]["leader"] == "贵州茅台"


class TestScreenAShareMarket:
    def test_screen_a_share_market_shapes_rows(self):
        frame = pd.DataFrame(
            [
                {
                    "代码": "600519",
                    "名称": "贵州茅台",
                    "最新价": 1688.0,
                    "涨跌幅": 9.98,
                    "涨跌额": 153.0,
                    "成交量": 1234567.0,
                    "成交额": 2.08e9,
                    "换手率": 1.23,
                }
            ]
        )
        mock_ak = MagicMock()
        mock_ak.stock_zh_a_spot_em.return_value = frame
        with patch.dict("sys.modules", {"akshare": mock_ak}):
            rows = af.screen_a_share_market(sort_by="change_pct", top_n=1)

        assert rows[0]["code"] == "600519"
        assert rows[0]["name"] == "贵州茅台"
        assert rows[0]["turnover_rate"] == 1.23
