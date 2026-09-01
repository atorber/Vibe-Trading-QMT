"""Tests for sector_tool: envelope shape, parsing, mode dispatch, validation.

All HTTP is mocked at the Eastmoney client functions the tool imports
(:func:`get_json` / :func:`resolve_secid`), so no test touches a live endpoint.
"""

from __future__ import annotations

import json
from unittest.mock import patch

from src.tools.sector_tool import SectorInfoTool, fetch_industry_peers

_MEMBERSHIP_PAYLOAD = {
    "data": {
        "diff": [
            {"f12": "BK0477", "f14": "白酒", "f3": 1.23, "f2": 1700.0},
            {"f12": "BK0815", "f14": "酿酒行业", "f3": -0.5, "f2": "-"},
            {"f14": "missing-code"},  # dropped: no f12
        ]
    }
}

_RANKING_PAYLOAD = {
    "data": {
        "diff": [
            {
                "f12": "BK0477",
                "f14": "白酒",
                "f3": 3.4,
                "f2": 12345.0,
                "f104": 18,
                "f105": 2,
                "f140": "贵州茅台",
            },
            {
                "f12": "BK0727",
                "f14": "银行",
                "f3": 1.1,
                "f2": 6789.0,
                "f104": 30,
                "f105": 12,
                "f140": "-",
            },
        ]
    }
}


class TestMembershipEnvelope:
    """A resolvable stock yields the ok envelope with parsed boards."""

    def test_membership_parses_boards(self):
        with patch(
            "src.tools.sector_tool.resolve_secid", return_value="1.600519"
        ), patch(
            "src.tools.sector_tool.get_json", return_value=_MEMBERSHIP_PAYLOAD
        ) as mock_get:
            text = SectorInfoTool().execute(code="600519.SH")

        url = mock_get.call_args[0][0]
        assert "slist/get" in url
        assert mock_get.call_args.kwargs["params"]["secid"] == "1.600519"

        payload = json.loads(text)
        assert payload["ok"] is True
        assert payload["market"] == "stock"
        assert payload["source"] == "eastmoney"
        assert payload["mode"] == "membership"
        assert payload["data"]["code"] == "600519.SH"
        assert payload["data"]["secid"] == "1.600519"

        boards = payload["data"]["boards"]
        assert len(boards) == 2  # the f12-less row is dropped
        assert boards[0] == {
            "board_code": "BK0477",
            "board_name": "白酒",
            "change_pct": 1.23,
            "price": 1700.0,
        }
        # "-" price coerces to None.
        assert boards[1]["price"] is None

    def test_membership_default_mode_when_only_code(self):
        with patch(
            "src.tools.sector_tool.resolve_secid", return_value="0.000001"
        ), patch("src.tools.sector_tool.get_json", return_value={"data": {"diff": []}}):
            payload = json.loads(SectorInfoTool().execute(code="000001.SZ"))

        assert payload["mode"] == "membership"
        assert payload["data"]["boards"] == []


class TestRankingEnvelope:
    """mode='ranking' enumerates the industry-board universe."""

    def test_ranking_parses_boards(self):
        with patch(
            "src.tools.sector_tool.get_json", return_value=_RANKING_PAYLOAD
        ) as mock_get:
            text = SectorInfoTool().execute(mode="ranking", limit=20)

        url = mock_get.call_args[0][0]
        assert "clist/get" in url
        assert mock_get.call_args.kwargs["params"]["fs"] == "m:90+t:2"

        payload = json.loads(text)
        assert payload["ok"] is True
        assert payload["mode"] == "ranking"
        boards = payload["data"]["boards"]
        assert len(boards) == 2
        assert boards[0]["board_name"] == "白酒"
        assert boards[0]["leader"] == "贵州茅台"
        assert boards[0]["up_count"] == 18.0
        # "-" leader coerces to None.
        assert boards[1]["leader"] is None

    def test_ranking_ignores_code_and_skips_resolve(self):
        with patch("src.tools.sector_tool.resolve_secid") as resolve, patch(
            "src.tools.sector_tool.get_json", return_value=_RANKING_PAYLOAD
        ):
            payload = json.loads(
                SectorInfoTool().execute(mode="ranking", code="600519.SH")
            )

        assert payload["ok"] is True
        resolve.assert_not_called()

    def test_ranking_caps_limit(self):
        with patch(
            "src.tools.sector_tool.get_json", return_value=_RANKING_PAYLOAD
        ) as mock_get:
            SectorInfoTool().execute(mode="ranking", limit=10_000)

        # Request pz is capped at the defensive maximum.
        assert mock_get.call_args.kwargs["params"]["pz"] == "100"

    def test_diff_as_dict_is_handled(self):
        dict_payload = {"data": {"diff": {"0": _RANKING_PAYLOAD["data"]["diff"][0]}}}
        with patch("src.tools.sector_tool.get_json", return_value=dict_payload):
            payload = json.loads(SectorInfoTool().execute(mode="ranking"))

        assert len(payload["data"]["boards"]) == 1


class TestErrorEnvelope:
    """Validation and request failures return the ok=false envelope."""

    def test_missing_code_for_membership_rejected(self):
        payload = json.loads(SectorInfoTool().execute())
        assert payload["ok"] is False
        assert "code" in payload["error"]

    def test_blank_code_rejected(self):
        payload = json.loads(SectorInfoTool().execute(code="   "))
        assert payload["ok"] is False

    def test_invalid_mode_rejected(self):
        payload = json.loads(SectorInfoTool().execute(mode="trending"))
        assert payload["ok"] is False
        assert "mode" in payload["error"]

    def test_non_positive_limit_rejected(self):
        payload = json.loads(SectorInfoTool().execute(mode="ranking", limit=0))
        assert payload["ok"] is False
        assert "limit" in payload["error"]

    def test_bool_limit_rejected(self):
        payload = json.loads(SectorInfoTool().execute(mode="ranking", limit=True))
        assert payload["ok"] is False

    def test_unresolvable_symbol_error_envelope(self):
        with patch("src.tools.sector_tool.resolve_secid", return_value=None):
            payload = json.loads(SectorInfoTool().execute(code="WAT.XYZ"))
        assert payload["ok"] is False
        assert "unresolvable" in payload["error"]

    def test_http_failure_membership_error_envelope(self):
        with patch(
            "src.tools.sector_tool.resolve_secid", return_value="1.600519"
        ), patch(
            "src.tools.sector_tool.get_json", side_effect=RuntimeError("HTTP 429")
        ):
            payload = json.loads(SectorInfoTool().execute(code="600519.SH"))
        assert payload["ok"] is False
        assert "429" in payload["error"]

    def test_http_failure_ranking_error_envelope(self):
        with patch(
            "src.tools.sector_tool.get_json", side_effect=RuntimeError("HTTP 503")
        ), patch(
            "src.tools.sector_tool.akshare_fallbacks.fetch_industry_board_ranking",
            side_effect=RuntimeError("akshare down"),
        ):
            payload = json.loads(SectorInfoTool().execute(mode="ranking"))
        assert payload["ok"] is False
        assert "503" in payload["error"]
        assert "akshare" in payload["error"]

    def test_http_failure_ranking_uses_akshare_fallback(self):
        fallback_boards = [
            {
                "board_code": "BK0477",
                "board_name": "白酒",
                "change_pct": 3.4,
                "index": 12345.0,
                "up_count": 18.0,
                "down_count": 2.0,
                "leader": "贵州茅台",
            }
        ]
        with patch(
            "src.tools.sector_tool.get_json", side_effect=RuntimeError("HTTP 503")
        ), patch(
            "src.tools.sector_tool.akshare_fallbacks.fetch_industry_board_ranking",
            return_value=fallback_boards,
        ):
            payload = json.loads(SectorInfoTool().execute(mode="ranking", limit=5))

        assert payload["ok"] is True
        assert payload["source"] == "akshare"
        assert payload["data"]["boards"] == fallback_boards
        assert "akshare fallback" in payload["warnings"][-1]


_INDUSTRY_META_PAYLOAD = {
    "data": {
        "diff": [
            {"f12": "603986", "f13": 1, "f14": "兆易创新"},
            {"f12": "BK1036", "f13": 90, "f14": "半导体", "f3": 0.42},
        ]
    }
}

_PEER_LIST_PAYLOAD = {
    "data": {
        "diff": [
            {"f12": "603986", "f13": 1, "f14": "兆易创新", "f2": 399.1, "f3": 1.2, "f6": 1.1e10},
            {"f12": "688981", "f13": 1, "f14": "中芯国际", "f2": 88.0, "f3": 0.8, "f6": 2.2e10},
            {"f12": "002371", "f13": 0, "f14": "北方华创", "f2": 320.0, "f3": -0.4, "f6": 1.8e10},
        ]
    }
}


class TestIndustryPeers:
  def test_fetch_industry_peers_parses_board_constituents(self):
      def fake_get(url: str, *, params: dict):
          if "slist/get" in url:
              return _INDUSTRY_META_PAYLOAD
          assert params["fs"] == "b:BK1036"
          return _PEER_LIST_PAYLOAD

      with patch("src.tools.sector_tool.get_json", side_effect=fake_get):
          payload = fetch_industry_peers("603986.SH", limit=5)

      assert payload is not None
      assert payload["industry"] == "半导体"
      assert payload["board_code"] == "BK1036"
      assert payload["target"]["symbol"] == "603986.SH"
      assert len(payload["peers"]) == 2
      assert payload["peers"][0]["symbol"] == "688981.SH"
