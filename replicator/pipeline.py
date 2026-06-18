"""The code-driven orchestrator: run each sub-agent phase in order on one repo.

The orchestration is deterministic Python — we do not rely on the model to
auto-delegate. Each phase is an independent ``query()`` with a fresh context;
state is shared only through files in the generated repo. After the planner
phase the pipeline pauses for the user to approve ``PLAN.md`` before any code
is written.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
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

from .phases import BENCHMARKER, CLEANER, CODER, PLANNER, REPAIR, TESTER, Phase
from .verdict import VERDICT_PATH, Verdict, clear_verdict, format_failures, read_verdict

# Default model per phase: spend on the hard reasoning steps, save on the rest.
# Overridden wholesale by an explicit --model on the CLI.
_DEFAULT_MODELS = {
    "planner": "opus",
    "coder": "opus",
    "tester": "sonnet",
    "benchmarker": "sonnet",
    "repair": "opus",  # Fixing real bugs is hard reasoning — use the strong model.
    "cleaner": "sonnet",
}

# How many times the orchestrator will repair-and-re-verify before giving up.
_MAX_REPAIR_ATTEMPTS = 2

_LOG_DIR_NAME = ".replicator/logs"


@dataclass
class _PhaseUsage:
    label: str
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


def _short(text: str, limit: int = 500) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit] + " […]"


async def _run_phase(
    phase: Phase, repo: Path, model: str, *, label: str | None = None, **task_kwargs: str
) -> _PhaseUsage:
    """Run one sub-agent to completion, streaming progress and logging the transcript.

    ``label`` overrides the log filename and header (so repeated phases like repair
    get one log per attempt). ``task_kwargs`` fill placeholders in the phase task.
    """
    label = label or phase.name
    log_path = repo / _LOG_DIR_NAME / f"{label}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = log_path.open("w")

    print(f"\n{'=' * 70}\n▶  {label.upper()}  (model: {model})\n{'=' * 70}")
    options = ClaudeAgentOptions(
        system_prompt=phase.system_prompt(),
        cwd=str(repo),
        allowed_tools=phase.allowed_tools,
        permission_mode=phase.permission_mode,
        max_turns=phase.max_turns,
        model=model,
    )
    task = phase.task.format(**task_kwargs)
    usage = _PhaseUsage(label=label)

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
                print(f"  [{_now()}] ✔ {label} finished ({message.subtype})")
                log.write(f"\n[result] {message.subtype}\n")
                if message.total_cost_usd is not None:
                    usage.cost_usd = message.total_cost_usd
                if message.usage:
                    usage.input_tokens = message.usage.get("input_tokens", 0)
                    usage.output_tokens = message.usage.get("output_tokens", 0)
    finally:
        log.close()

    return usage


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


def _model(phase: Phase, override: str | None) -> str:
    return override or _DEFAULT_MODELS[phase.name]


async def _verify_and_repair(
    repo: Path, model_override: str | None
) -> tuple[bool, list[_PhaseUsage]]:
    """Run the judging phases; on a failure verdict, repair and re-verify.

    Each round runs the tester then the benchmarker. They only diagnose — a failing
    verdict triggers a Repair phase (fed the recorded failures), after which the whole
    round restarts so the fix is re-verified. Returns (passed, usages) where passed is
    True once both judges pass within the repair budget, False if failures remain after
    :data:`_MAX_REPAIR_ATTEMPTS`.
    """
    attempts = 0
    usages: list[_PhaseUsage] = []
    while True:
        failure: Verdict | None = None
        for judge in (TESTER, BENCHMARKER):
            clear_verdict(repo)
            usages.append(await _run_phase(judge, repo, _model(judge, model_override)))
            verdict = read_verdict(repo)
            if verdict is None:
                print(f"  ⚠️  {judge.name} wrote no verdict; assuming it passed.")
                continue
            if not verdict.passed:
                print(f"  ✗ {judge.name} verdict: FAIL ({len(verdict.failures)} issue(s)).")
                failure = verdict
                break  # Repair before running the next judge.
            print(f"  ✓ {judge.name} verdict: PASS.")

        if failure is None:
            return True, usages
        if attempts >= _MAX_REPAIR_ATTEMPTS:
            return False, usages

        attempts += 1
        print(
            f"\n🔧 Repair attempt {attempts}/{_MAX_REPAIR_ATTEMPTS} "
            f"(triggered by {failure.phase})."
        )
        usages.append(
            await _run_phase(
                REPAIR,
                repo,
                _model(REPAIR, model_override),
                label=f"repair-{attempts}",
                pdf=_paper_pdf(repo),
                failures=format_failures([failure]),
            )
        )


def _print_cost_summary(usages: list[_PhaseUsage]) -> None:
    total_cost = sum(u.cost_usd for u in usages)
    total_in = sum(u.input_tokens for u in usages)
    total_out = sum(u.output_tokens for u in usages)

    w = 18  # label column width
    print(f"\n{'─' * 62}")
    print("  Cost summary")
    print(f"{'─' * 62}")
    print(f"  {'Phase':<{w}}  {'Input tok':>10}  {'Output tok':>10}  {'Cost (USD)':>10}")
    print(f"  {'─' * (w)}  {'─' * 10}  {'─' * 10}  {'─' * 10}")
    for u in usages:
        print(
            f"  {u.label:<{w}}  {u.input_tokens:>10,}  {u.output_tokens:>10,}  ${u.cost_usd:>9.4f}"
        )
    print(f"  {'─' * (w)}  {'─' * 10}  {'─' * 10}  {'─' * 10}")
    print(f"  {'TOTAL':<{w}}  {total_in:>10,}  {total_out:>10,}  ${total_cost:>9.4f}")
    print(f"{'─' * 62}")


async def run_pipeline(repo: Path, model_override: str | None = None) -> None:
    """Run the replication pipeline over ``repo``.

    Flow: plan → (human checkpoint) → code → verify-and-repair → clean. The cleaner
    only runs over an implementation that passed the judging phases; if repair cannot
    make it pass, the pipeline stops and reports the outstanding failures honestly.
    """
    print(f"\nBaseline replicator → {repo}")
    all_usages: list[_PhaseUsage] = []

    all_usages.append(
        await _run_phase(PLANNER, repo, _model(PLANNER, model_override), pdf=_paper_pdf(repo))
    )
    if not _checkpoint(repo):
        print("\n✋ Stopped at planning checkpoint. The plan is in PLAN.md.")
        _print_cost_summary(all_usages)
        sys.exit(0)

    all_usages.append(await _run_phase(CODER, repo, _model(CODER, model_override)))

    passed, vr_usages = await _verify_and_repair(repo, model_override)
    all_usages.extend(vr_usages)

    if not passed:
        print(
            f"\n⚠️  Stopping before cleanup: the implementation still fails after "
            f"{_MAX_REPAIR_ATTEMPTS} repair attempt(s).\n"
            f"    See REPORT.md and {VERDICT_PATH} for the outstanding failures."
        )
        _print_cost_summary(all_usages)
        sys.exit(1)

    all_usages.append(await _run_phase(CLEANER, repo, _model(CLEANER, model_override)))
    print(f"\n✅ Done. Replicated baseline is in {repo} (see README.md and REPORT.md).")
    _print_cost_summary(all_usages)
