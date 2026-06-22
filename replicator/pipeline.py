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
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    query,
)

from .criteria import mechanical_failures
from .phases import (
    BENCHMARKER,
    CLEANER,
    CODER,
    PLANNER,
    REPAIR,
    REVISER,
    TESTER,
    Phase,
    hardware_profile,
)
from .verdict import (
    VERDICT_PATH,
    Verdict,
    clear_verdict,
    format_failures,
    read_verdict,
    write_verdict,
)

# Default model per phase: spend on the hard reasoning steps, save on the rest.
# Overridden wholesale by an explicit --model on the CLI.
_DEFAULT_MODELS = {
    "planner": "opus",
    "reviser": "opus",  # Conversationally revising the plan is the same hard reasoning.
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
    phase: Phase,
    repo: Path,
    model: str,
    hardware: str,
    *,
    label: str | None = None,
    **task_kwargs: str,
) -> _PhaseUsage:
    """Run one sub-agent to completion, streaming progress and logging the transcript.

    ``label`` overrides the log filename and header (so repeated phases like repair
    get one log per attempt). ``hardware`` is the profile string (CPU_PROFILE or
    GPU_PROFILE) that fills the ``{{HARDWARE}}`` sentinel in the system prompt.
    ``task_kwargs`` fill placeholders in the phase task.
    """
    label = label or phase.name
    log_path = repo / _LOG_DIR_NAME / f"{label}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = log_path.open("w")

    print(f"\n{'=' * 70}\n▶  {label.upper()}  (model: {model})\n{'=' * 70}")
    options = ClaudeAgentOptions(
        system_prompt=phase.system_prompt(hardware),
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
            _render(message, label, log, usage)
    finally:
        log.close()

    return usage


def _render(message, label: str, log, usage: _PhaseUsage) -> None:
    """Print and log one streamed SDK message, keeping latest cost/token totals.

    Shared by the one-shot phases (``_run_phase``) and the interactive revision chat
    (``_run_chat_phase``). The SDK reports session totals in ``ResultMessage``; keep
    the latest totals instead of adding them across chat turns.
    """
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


async def _run_chat_phase(phase: Phase, repo: Path, model: str, hardware: str) -> _PhaseUsage:
    """Drive a multi-turn revision conversation over PLAN.md.

    Unlike ``_run_phase`` (a one-shot ``query``), this keeps a stateful ``ClaudeSDKClient``
    session open so the agent remembers the conversation: the user types successive revision
    requests and the agent edits ``PLAN.md`` / ``.replicator/criteria.json`` in place. The
    whole session lives inside this one coroutine (the SDK forbids using a client across
    async contexts). Returns the session usage totals for the cost summary.
    """
    label = phase.name
    log_path = repo / _LOG_DIR_NAME / f"{label}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = log_path.open("w")
    usage = _PhaseUsage(label=label)

    print(f"\n{'=' * 70}\n▶  {label.upper()}  (model: {model})\n{'=' * 70}")
    print(
        "  Chat to revise PLAN.md and criteria.json. Type a request and press Enter;\n"
        "  the agent edits the plan and reports back. Type 'done' (or an empty line)\n"
        "  when you're finished to return to the approve prompt."
    )
    options = ClaudeAgentOptions(
        system_prompt=phase.system_prompt(hardware),
        cwd=str(repo),
        allowed_tools=phase.allowed_tools,
        permission_mode=phase.permission_mode,
        max_turns=phase.max_turns,
        model=model,
    )
    # First message tells the agent where the paper lives, like the planner is told.
    first_prefix = (
        f"You are revising the existing PLAN.md and .replicator/criteria.json. For reference, "
        f"{_paper_sources(repo)}.\n\nMy first request:\n"
    )

    try:
        async with ClaudeSDKClient(options=options) as client:
            first = True
            while True:
                try:
                    message = input("\nyou › ").strip()
                except EOFError:
                    break
                if message.lower() in ("", "done", "exit", "quit"):
                    break
                log.write(f"\n[user] {message}\n")
                await client.query(first_prefix + message if first else message)
                first = False
                async for reply in client.receive_response():
                    _render(reply, label, log, usage)
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
    """Path to the downloaded paper PDF, relative to the repo (for the repair task)."""
    pdfs = sorted((repo / "paper").glob("*.pdf"))
    return f"paper/{pdfs[0].name}" if pdfs else "paper/"


def _paper_sources(repo: Path) -> str:
    """Describe the reading sources for the planner, preferring HTML when present.

    arXiv's HTML rendering (when it exists) is cleaner and far cheaper to read than
    the PDF page-images, but it carries no figures — so the PDF stays the figure and
    tie-break authority. When there is no HTML, this degrades to the PDF alone.
    """
    pdf = _paper_pdf(repo)
    source_file = repo / "paper" / "SOURCE.txt"
    link = ""
    if source_file.exists():
        link = f"; the paper's source link is `{source_file.read_text().strip()}`"
    htmls = sorted((repo / "paper").glob("*.html"))
    if htmls:
        return (
            f"an HTML rendering is at `paper/{htmls[0].name}` — prefer it, its text and "
            f"equations are cleaner and far cheaper to read; the PDF at `{pdf}` is "
            f"authoritative and the only source with figures, so consult it for figures "
            f"or anything the HTML renders ambiguously{link}"
        )
    return f"the PDF is at `{pdf}`{link}"


async def _checkpoint(
    repo: Path, model_override: str | None, usages: list[_PhaseUsage], hardware: str
) -> bool:
    """Show PLAN.md and ask the user to approve. Returns True to continue.

    Answering ``chat`` opens a multi-turn conversation with the reviser agent, which
    edits PLAN.md (and criteria.json) per the user's requests; the updated plan is then
    re-presented, so plans can be revised before any code lands. The chat's usage is
    appended to ``usages`` so its cost shows up in the final summary.
    """
    plan = repo / "PLAN.md"
    while True:
        print(f"\n{'─' * 70}\n📋  PLAN.md (review before implementation)\n{'─' * 70}")
        print(plan.read_text() if plan.exists() else "  (PLAN.md was not created!)")
        print("─" * 70)
        answer = input(
            "Approve plan and continue? [y]es / [N]o / [c]hat to revise PLAN.md: "
        )
        choice = answer.strip().lower()
        if choice in ("y", "yes"):
            return True
        if choice in ("c", "chat"):
            usages.append(
                await _run_chat_phase(REVISER, repo, _model(REVISER, model_override), hardware)
            )
            continue
        return False


def _model(phase: Phase, override: str | None) -> str:
    return override or _DEFAULT_MODELS[phase.name]


def _apply_mechanical_check(repo: Path, verdict: Verdict) -> Verdict:
    """Fold the deterministic criteria/results comparison into the benchmarker verdict.

    The numeric and boolean criteria are judged in Python, not by the LLM: any
    required criterion that fails (or whose value is missing from ``results.json``) is
    added to the verdict's failures, forcing a FAIL that triggers Repair regardless of
    what the benchmarker concluded. When no valid ``criteria.json`` exists the check is
    skipped and the benchmarker's own verdict stands.
    """
    failures, notes = mechanical_failures(repo)
    for line in notes:
        print(f"  · {line}")
    if not failures:
        return verdict
    combined = list(verdict.failures) + [
        f for f in failures if f not in verdict.failures
    ]
    verdict = Verdict(phase=verdict.phase, status="fail", failures=combined)
    write_verdict(repo, verdict)
    return verdict


async def _verify_and_repair(
    repo: Path, model_override: str | None, hardware: str
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
            usages.append(await _run_phase(judge, repo, _model(judge, model_override), hardware))
            verdict = read_verdict(repo, judge.name)
            if judge is BENCHMARKER:
                verdict = _apply_mechanical_check(repo, verdict)
            if not verdict.passed:
                print(
                    f"  ✗ {judge.name} verdict: FAIL ({len(verdict.failures)} issue(s))."
                )
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
                hardware,
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
    print(
        f"  {'Phase':<{w}}  {'Input tok':>10}  {'Output tok':>10}  {'Cost (USD)':>10}"
    )
    print(f"  {'─' * (w)}  {'─' * 10}  {'─' * 10}  {'─' * 10}")
    for u in usages:
        print(
            f"  {u.label:<{w}}  {u.input_tokens:>10,}  {u.output_tokens:>10,}  ${u.cost_usd:>9.4f}"
        )
    print(f"  {'─' * (w)}  {'─' * 10}  {'─' * 10}  {'─' * 10}")
    print(f"  {'TOTAL':<{w}}  {total_in:>10,}  {total_out:>10,}  ${total_cost:>9.4f}")
    print(f"{'─' * 62}")


async def run_pipeline(
    repo: Path,
    model_override: str | None = None,
    instructions: str = "",
    gpu: bool = False,
) -> None:
    """Run the replication pipeline over ``repo``.

    Flow: plan → (human checkpoint) → code → verify-and-repair → clean. The cleaner
    only runs over an implementation that passed the judging phases; if repair cannot
    make it pass, the pipeline stops and reports the outstanding failures honestly.

    ``gpu=True`` activates GPU mode: the hardware profile injected into every phase's
    system prompt switches from CPU_PROFILE to GPU_PROFILE, steering the planner toward
    ambitious paper-scale default configs plus a separate reduced verification run.
    """
    print(f"\nBaseline replicator → {repo}")
    all_usages: list[_PhaseUsage] = []

    hardware = hardware_profile(gpu)

    instructions_block = (
        "\n\nAdditional instructions from the user (treat as authoritative):\n"
        + instructions.strip()
        if instructions.strip()
        else ""
    )
    all_usages.append(
        await _run_phase(
            PLANNER,
            repo,
            _model(PLANNER, model_override),
            hardware,
            sources=_paper_sources(repo),
            instructions=instructions_block,
        )
    )
    if not await _checkpoint(repo, model_override, all_usages, hardware):
        print("\n✋ Stopped at planning checkpoint. The plan is in PLAN.md.")
        _print_cost_summary(all_usages)
        sys.exit(0)

    all_usages.append(await _run_phase(CODER, repo, _model(CODER, model_override), hardware))

    passed, vr_usages = await _verify_and_repair(repo, model_override, hardware)
    all_usages.extend(vr_usages)

    if not passed:
        print(
            f"\n⚠️  Stopping before cleanup: the implementation still fails after "
            f"{_MAX_REPAIR_ATTEMPTS} repair attempt(s).\n"
            f"    See REPORT.md and {VERDICT_PATH} for the outstanding failures."
        )
        _print_cost_summary(all_usages)
        sys.exit(1)

    all_usages.append(
        await _run_phase(CLEANER, repo, _model(CLEANER, model_override), hardware)
    )
    print(f"\n✅ Done. Replicated baseline is in {repo} (see README.md and REPORT.md).")
    _print_cost_summary(all_usages)
