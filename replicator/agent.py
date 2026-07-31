"""Codex SDK backend for the replication pipeline."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from openai_codex import ApprovalMode, AsyncCodex, Sandbox

from .phases import Phase

DEFAULT_MODEL = "gpt-5.6-sol"
_LOG_DIR_NAME = ".replicator/logs"


def resolve_model(_phase_name: str, override: str | None) -> str:
    """Return the requested Codex model, or the branch-wide default."""
    return override or DEFAULT_MODEL


@dataclass
class PhaseUsage:
    label: str
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


def _short(text: str, limit: int = 500) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit] + " […]"


def _developer_instructions(phase: Phase, hardware: str) -> str:
    capabilities = ", ".join(phase.allowed_tools)
    return (
        f"{phase.system_prompt(hardware)}\n\n"
        "This is one scoped phase in a deterministic pipeline. Do not delegate to "
        "sub-agents. Work only inside the current working directory. "
        f"Limit yourself to these capabilities: {capabilities}."
    )


def _record_result(result, label: str, log, usage: PhaseUsage) -> None:
    """Render a completed SDK turn and record its transcript and token usage."""
    for item in result.items:
        data = item.model_dump(mode="json", by_alias=True)
        log.write(json.dumps(data, ensure_ascii=False) + "\n")

    response = result.final_response or ""
    if response.strip():
        print(f"  [{_now()}] {_short(response)}")

    if result.usage is not None:
        total = result.usage.total
        usage.input_tokens = total.input_tokens
        usage.output_tokens = total.output_tokens

    status = getattr(result.status, "value", str(result.status))
    print(f"  [{_now()}] ✔ {label} finished ({status})")
    log.write(f"\n[result] {status}\n")


def _thread_options(phase: Phase, repo: Path, model: str, hardware: str) -> dict:
    """Build the native SDK arguments for one isolated pipeline phase."""
    return {
        "approval_mode": ApprovalMode.deny_all,
        "config": {
            "project_doc_max_bytes": 0,
            "sandbox_workspace_write": {"network_access": True},
            "web_search": "live" if "WebSearch" in phase.allowed_tools else "disabled",
        },
        "cwd": str(repo.resolve()),
        "developer_instructions": _developer_instructions(phase, hardware),
        "ephemeral": True,
        "model": model,
        "sandbox": Sandbox.workspace_write,
    }


async def _start_thread(codex: AsyncCodex, phase: Phase, repo: Path, model: str, hardware: str):
    return await codex.thread_start(**_thread_options(phase, repo, model, hardware))


async def run_agent(
    phase: Phase,
    repo: Path,
    model: str,
    hardware: str,
    *,
    label: str | None = None,
    **task_kwargs: str,
) -> PhaseUsage:
    """Run one isolated Codex thread to completion."""
    label = label or phase.name
    log_path = repo / _LOG_DIR_NAME / f"{label}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    usage = PhaseUsage(label=label, model=model)

    print(f"\n{'=' * 70}\n▶  {label.upper()}  (model: {model})\n{'=' * 70}")
    task = phase.task.format(**task_kwargs)

    with log_path.open("w", buffering=1) as log:
        log.write(f"[user] {task}\n")
        async with AsyncCodex() as codex:
            thread = await _start_thread(codex, phase, repo, model, hardware)
            result = await thread.run(task)
        _record_result(result, label, log, usage)

    return usage


async def run_chat_agent(
    phase: Phase,
    repo: Path,
    model: str,
    hardware: str,
    *,
    paper_sources: str = "",
) -> PhaseUsage:
    """Drive a stateful Codex thread for interactive plan revision."""
    label = phase.name
    log_path = repo / _LOG_DIR_NAME / f"{label}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    usage = PhaseUsage(label=label, model=model)

    print(f"\n{'=' * 70}\n▶  {label.upper()}  (model: {model})\n{'=' * 70}")
    print(
        "  Chat to revise PLAN.md and criteria.json. Type a request and press Enter;\n"
        "  the agent edits the plan and reports back. Type 'done' (or an empty line)\n"
        "  when you're finished to return to the approve prompt."
    )
    first_prefix = (
        "You are revising the existing PLAN.md and .replicator/criteria.json. "
        f"For reference, {paper_sources}.\n\nMy first request:\n"
    )

    with log_path.open("w", buffering=1) as log:
        async with AsyncCodex() as codex:
            thread = await _start_thread(codex, phase, repo, model, hardware)
            first = True
            while True:
                try:
                    message = input("\nyou › ").strip()
                except EOFError:
                    break
                if message.lower() in ("", "done", "exit", "quit"):
                    break
                prompt = first_prefix + message if first else message
                first = False
                log.write(f"\n[user] {message}\n")
                result = await thread.run(prompt)
                _record_result(result, label, log, usage)

    return usage
