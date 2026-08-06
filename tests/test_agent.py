"""Unit tests for the Codex SDK boundary (no model or network calls)."""

from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace

import pytest
from openai_codex import ApprovalMode, AsyncCodex, AsyncThread, Sandbox

from replicator import agent
from replicator.phases import PLANNER, TESTER


def test_default_and_overridden_model():
    assert agent.resolve_model("planner", None) == "gpt-5.6-sol"
    assert agent.resolve_model("tester", "gpt-5.4-mini") == "gpt-5.4-mini"


def test_openrouter_backend_requires_a_key_and_can_be_configured():
    with pytest.raises(ValueError, match="requires an API key"):
        agent.configure_backend("openrouter")

    agent.configure_backend("openrouter", "sk-test")
    assert agent._backend.provider == "openrouter"
    assert agent._api_base_url() == "https://openrouter.ai/api/v1"
    agent.configure_backend()


def test_load_and_resolve_agent_settings(tmp_path):
    config = tmp_path / "agents.json"
    config.write_text(
        '{"planner": {"model": "gpt-5.6-sol", "reasoning_effort": "high"}, '
        '"tester": {"reasoning_effort": "low"}}'
    )

    settings = agent.load_agent_settings(config)
    assert agent.resolve_agent_settings("planner", None, settings) == agent.AgentSettings(
        model="gpt-5.6-sol", reasoning_effort="high"
    )
    assert agent.resolve_agent_settings("tester", None, settings) == agent.AgentSettings(
        model="gpt-5.6-sol", reasoning_effort="low"
    )
    assert agent.resolve_agent_settings("planner", "gpt-5.4-mini", settings) == agent.AgentSettings(
        model="gpt-5.4-mini", reasoning_effort="high"
    )


def test_load_agent_settings_rejects_unknown_agent(tmp_path):
    config = tmp_path / "agents.json"
    config.write_text('{"unknown": {"model": "gpt-5.6-sol"}}')

    with pytest.raises(ValueError, match="unknown agent"):
        agent.load_agent_settings(config)


def test_thread_options_match_installed_sdk(tmp_path):
    options = agent._thread_options(PLANNER, tmp_path, "gpt-5.6-sol", "test hardware")

    inspect.signature(AsyncCodex.thread_start).bind(object(), **options)
    inspect.signature(AsyncThread.run).bind(object(), "task", effort="high")
    assert options["approval_mode"] is ApprovalMode.deny_all
    assert options["sandbox"] is Sandbox.workspace_write
    assert options["ephemeral"] is True
    assert options["config"] == {
        "project_doc_max_bytes": 0,
        "sandbox_workspace_write": {"network_access": True},
        "web_search": "live",
    }
    assert str(tmp_path.resolve()) == options["cwd"]


def test_non_web_phase_disables_search(tmp_path):
    options = agent._thread_options(TESTER, tmp_path, "gpt-5.6-sol", "test hardware")
    assert options["config"]["web_search"] == "disabled"


def test_run_agent_uses_codex_and_records_result(monkeypatch, tmp_path):
    captured = {}

    class Item:
        def model_dump(self, **_kwargs):
            return {"type": "agentMessage", "text": "done"}

    result = SimpleNamespace(
        final_response="done",
        items=[Item()],
        status=SimpleNamespace(value="completed"),
        usage=SimpleNamespace(
            total=SimpleNamespace(input_tokens=12, output_tokens=3),
        ),
    )

    class Thread:
        async def run(self, prompt):
            captured["prompt"] = prompt
            return result

    class FakeCodex:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def thread_start(self, **kwargs):
            captured["options"] = kwargs
            return Thread()

    monkeypatch.setattr(agent, "AsyncCodex", FakeCodex)
    usage = asyncio.run(
        agent.run_agent(
            TESTER,
            tmp_path,
            "gpt-5.6-sol",
            "test hardware",
            reference="",
        )
    )

    assert captured["prompt"].startswith("Write a small, fast, deterministic pytest suite")
    assert captured["options"]["sandbox"] is Sandbox.workspace_write
    assert captured["options"]["approval_mode"] is ApprovalMode.deny_all
    assert usage.input_tokens == 12
    assert usage.output_tokens == 3
    log = (tmp_path / ".replicator/logs/tester.log").read_text()
    assert '"type": "agentMessage"' in log
    assert "[result] completed" in log


def test_run_agent_passes_configured_reasoning_effort(monkeypatch, tmp_path):
    captured = {}
    result = SimpleNamespace(
        final_response="done",
        items=[],
        status=SimpleNamespace(value="completed"),
        usage=None,
    )

    class Thread:
        async def run(self, prompt, **kwargs):
            captured["prompt"] = prompt
            captured["kwargs"] = kwargs
            return result

    class FakeCodex:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def thread_start(self, **_kwargs):
            return Thread()

    monkeypatch.setattr(agent, "AsyncCodex", FakeCodex)
    asyncio.run(
        agent.run_agent(
            TESTER,
            tmp_path,
            "gpt-5.6-sol",
            "test hardware",
            reasoning_effort="high",
            reference="",
        )
    )

    assert captured["kwargs"] == {"effort": "high"}


def test_chat_agent_reuses_one_codex_thread(monkeypatch, tmp_path):
    prompts = []

    result = SimpleNamespace(
        final_response="updated",
        items=[],
        status=SimpleNamespace(value="completed"),
        usage=SimpleNamespace(
            total=SimpleNamespace(input_tokens=20, output_tokens=4),
        ),
    )

    class Thread:
        async def run(self, prompt):
            prompts.append(prompt)
            return result

    class FakeCodex:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def thread_start(self, **_kwargs):
            return Thread()

    answers = iter(["make it smaller", "add a metric", "done"])
    monkeypatch.setattr(agent, "AsyncCodex", FakeCodex)
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))

    usage = asyncio.run(
        agent.run_chat_agent(
            agent.Phase(
                name="reviser",
                task="",
                allowed_tools=["Read", "Write"],
            ),
            tmp_path,
            "gpt-5.6-sol",
            "test hardware",
            paper_sources="paper/paper.pdf",
        )
    )

    assert len(prompts) == 2
    assert "paper/paper.pdf" in prompts[0]
    assert prompts[1] == "add a metric"
    assert usage.input_tokens == 20
