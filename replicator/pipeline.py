"""The code-driven orchestrator: run each sub-agent phase in order on one repo.

The orchestration is deterministic Python — we do not rely on the model to
auto-delegate. Each phase is an independent ``query()`` with a fresh context;
state is shared only through files in the generated repo. After the planner
phase the pipeline pauses for the user to approve ``PLAN.md`` before any code
is written.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    query,
)

from .phases import PHASES, Phase

# Default model per phase: spend on the hard reasoning steps, save on the rest.
# Overridden wholesale by an explicit --model on the CLI.
_DEFAULT_MODELS = {
    "planner": "opus",
    "coder": "opus",
    "tester": "sonnet",
    "benchmarker": "sonnet",
    "cleaner": "sonnet",
}

_LOG_DIR_NAME = ".replicator/logs"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


def _short(text: str, limit: int = 500) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit] + " […]"


async def _run_phase(phase: Phase, repo: Path, model: str) -> None:
    """Run one sub-agent to completion, streaming progress and logging the transcript."""
    log_path = repo / _LOG_DIR_NAME / f"{phase.name}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = log_path.open("w")

    print(f"\n{'=' * 70}\n▶  {phase.name.upper()}  (model: {model})\n{'=' * 70}")
    options = ClaudeAgentOptions(
        system_prompt=phase.system_prompt(),
        cwd=str(repo),
        allowed_tools=phase.allowed_tools,
        permission_mode=phase.permission_mode,
        max_turns=phase.max_turns,
        model=model,
    )
    task = phase.task.format(pdf=_paper_pdf(repo))

    try:
        async for message in query(prompt=task, options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock) and block.text.strip():
                        print(f"  [{_now()}] {_short(block.text)}")
                        log.write(f"\n[assistant] {block.text}\n")
                    elif isinstance(block, ToolUseBlock):
                        target = _tool_target(block.input)
                        print(f"  [{_now()}] · {block.name}{target}")
                        log.write(f"[tool] {block.name} {block.input}\n")
            elif isinstance(message, ResultMessage):
                print(f"  [{_now()}] ✔ {phase.name} finished ({message.subtype})")
                log.write(f"\n[result] {message.subtype}\n")
    finally:
        log.close()


def _tool_target(tool_input: dict) -> str:
    """A compact hint of what a tool call is acting on, for the progress line."""
    for key in ("file_path", "path", "command", "pattern", "url"):
        if key in tool_input:
            return f" → {_short(str(tool_input[key]), 80)}"
    return ""


def _paper_pdf(repo: Path) -> str:
    """Path to the downloaded paper PDF, relative to the repo (for the planner task)."""
    pdfs = sorted((repo / "paper").glob("*.pdf"))
    return f"paper/{pdfs[0].name}" if pdfs else "paper/"


def _checkpoint(repo: Path) -> bool:
    """Show PLAN.md and ask the user to approve. Returns True to continue.

    Answering ``edit`` lets the user modify PLAN.md (in their own editor) and
    then re-presents the prompt, so plans can be tweaked before any code lands.
    """
    plan = repo / "PLAN.md"
    while True:
        print(f"\n{'─' * 70}\n📋  PLAN.md (review before implementation)\n{'─' * 70}")
        print(plan.read_text() if plan.exists() else "  (PLAN.md was not created!)")
        print("─" * 70)
        answer = input("Approve plan and continue? [y]es / [N]o / [e]dit PLAN.md then re-ask: ")
        choice = answer.strip().lower()
        if choice in ("y", "yes"):
            return True
        if choice in ("e", "edit"):
            input(f"Edit {plan} now, save it, then press Enter to re-review… ")
            continue
        return False


async def run_pipeline(repo: Path, model_override: str | None = None) -> None:
    """Run all phases over ``repo`` in order, pausing at the planning checkpoint."""
    print(f"\nBaseline replicator → {repo}")
    for phase in PHASES:
        model = model_override or _DEFAULT_MODELS[phase.name]
        await _run_phase(phase, repo, model)
        if phase.checkpoint_after and not _checkpoint(repo):
            print("\n✋ Stopped at planning checkpoint. The plan is in PLAN.md.")
            sys.exit(0)
    print(f"\n✅ Done. Replicated baseline is in {repo} (see README.md and REPORT.md).")
