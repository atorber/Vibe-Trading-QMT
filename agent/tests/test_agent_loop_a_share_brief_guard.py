"""Regression tests for A-share daily brief anti-loop guards."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.agent.context import ContextBuilder
from src.agent.loop import AgentLoop, _market_data_coverage_key
from src.agent.memory import WorkspaceMemory
from src.agent.tools import ToolRegistry
from src.agent.trace import TraceWriter
from src.tools.market_data_tool import MarketDataTool
from src.tools.web_search_tool import WebSearchTool


def _process(
    agent: AgentLoop,
    *,
    tool_name: str,
    arguments: dict[str, object],
    call_id: str,
    run_dir: Path,
) -> list[dict[str, object]]:
    trace = TraceWriter(run_dir)
    messages: list[dict[str, object]] = []
    react_trace: list[dict[str, object]] = []
    context = ContextBuilder(agent.registry, agent.memory)
    agent._process_tool_calls(
        [SimpleNamespace(id=call_id, name=tool_name, arguments=arguments)],
        context,
        messages,
        trace,
        react_trace,
        1,
    )
    trace.close()
    return messages


def test_a_share_brief_mode_blocks_web_search(tmp_path: Path) -> None:
    registry = ToolRegistry()
    registry.register(WebSearchTool())
    agent = AgentLoop(registry=registry, llm=SimpleNamespace(), max_iterations=2)
    agent.memory = WorkspaceMemory(run_dir=str(tmp_path / "run"))
    agent._a_share_brief_mode = True

    messages = _process(
        agent,
        tool_name="web_search",
        arguments={"query": "A股 今日 大盘"},
        call_id="ws1",
        run_dir=tmp_path / "run",
    )

    payload = json.loads(str(messages[0]["content"]))
    assert payload["skipped"] is True
    assert "web_search is disabled" in payload["reason"]


def test_market_data_coverage_cache_skips_repeat_source(tmp_path: Path, monkeypatch) -> None:
    tool = MarketDataTool()
    calls: list[dict[str, object]] = []

    def _execute(**kwargs: object) -> str:
        calls.append(kwargs)
        return json.dumps(
            {
                "ok": True,
                "data": {
                    "000001.SH": [{"date": "2026-09-01", "close": 3200.0}],
                },
            }
        )

    monkeypatch.setattr(tool, "execute", _execute)
    registry = ToolRegistry()
    registry.register(tool)
    agent = AgentLoop(registry=registry, llm=SimpleNamespace(), max_iterations=2)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    agent.memory = WorkspaceMemory(run_dir=str(run_dir))

    args = {
        "codes": ["000001.SH"],
        "as_of": "today",
        "lookback_days": 10,
        "source": "auto",
    }
    _process(agent, tool_name="get_market_data", arguments=args, call_id="md1", run_dir=run_dir)
    _process(
        agent,
        tool_name="get_market_data",
        arguments={**args, "source": "tencent"},
        call_id="md2",
        run_dir=run_dir,
    )

    assert len(calls) == 1
    assert _market_data_coverage_key(args) == _market_data_coverage_key(
        {**args, "source": "tencent"}
    )


def test_brief_sop_skips_repeat_sector_ranking(tmp_path: Path) -> None:
    registry = ToolRegistry()
    agent = AgentLoop(registry=registry, llm=SimpleNamespace(), max_iterations=2)
    agent.memory = WorkspaceMemory(run_dir=str(tmp_path / "run"))
    agent._a_share_brief_mode = True
    agent._brief_sop_done.add("sector_ranking")

    messages = _process(
        agent,
        tool_name="get_sector_info",
        arguments={"mode": "ranking", "limit": 20},
        call_id="sec1",
        run_dir=tmp_path / "run",
    )

    payload = json.loads(str(messages[0]["content"]))
    assert payload["skipped"] is True
    assert "ranking already succeeded" in payload["reason"]


def test_broker_trade_review_blocks_optional_market_tools(tmp_path: Path) -> None:
    registry = ToolRegistry()
    agent = AgentLoop(registry=registry, llm=SimpleNamespace(), max_iterations=2)
    agent.memory = WorkspaceMemory(run_dir=str(tmp_path / "run"))
    agent._broker_trade_review_mode = True

    messages = _process(
        agent,
        tool_name="get_sector_info",
        arguments={"mode": "ranking", "limit": 20},
        call_id="sec2",
        run_dir=tmp_path / "run",
    )

    payload = json.loads(str(messages[0]["content"]))
    assert payload["skipped"] is True
    assert "broker trade review" in payload["reason"]
