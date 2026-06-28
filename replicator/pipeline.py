"""The code-driven orchestrator: run each sub-agent phase in order on one repo.

The orchestration is deterministic Python — we do not rely on the model to
auto-delegate. Each phase is an independent agent run (see ``agent.py``) with a
fresh context; state is shared only through files in the generated repo. After the
planner phase the pipeline pauses for the user to approve ``PLAN.md`` before any
code is written.
"""

from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

from .agent import PhaseUsage, resolve_model, run_agent, run_chat_agent
from .criteria import mechanical_failures
from .paper import clone_reference_code
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

# How many times the orchestrator will repair-and-re-verify before giving up.
_MAX_REPAIR_ATTEMPTS = 2

# Upper bound on coder phases parsed from PLAN.md — each phase is a full opus run, so a
# runaway plan (or an over-eager human edit at the checkpoint) must not multiply cost.
_MAX_CODER_PHASES = 4

def _paper_pdf(repo: Path) -> str:
    """Path to the downloaded paper PDF, relative to the repo (for the repair task)."""
    pdfs = sorted((repo / "paper").glob("*.pdf"))
    return f"paper/{pdfs[0].name}" if pdfs else "paper/"


def _paper_sources(repo: Path) -> str:
    """Describe the reading sources for the planner, preferring HTML or plain text.

    Priority:
    1. arXiv HTML (when present) — cleanest text + equations; PDF stays the figure/tie-break
       authority.
    2. Pre-extracted plain-text file (``paper/<pdf-stem>.txt``) — a ``pymupdf`` extraction
       written by the CLI; good for non-arXiv PDFs with no HTML. Still defer to the PDF for
       figures or anything the extraction renders ambiguously.
    3. Raw PDF — fallback when neither of the above exists (e.g. scanned/encrypted papers).
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
    # Look for a pre-extracted plain-text file (named <pdf-stem>.txt).
    pdfs = sorted((repo / "paper").glob("*.pdf"))
    if pdfs:
        txt_path = pdfs[0].with_suffix(".txt")
        if txt_path.exists() and txt_path.stat().st_size > 0:
            rel_txt = f"paper/{txt_path.name}"
            return (
                f"a plain-text extraction is at `{rel_txt}` — prefer it, its text is "
                f"far cheaper to read; the PDF at `{pdf}` is authoritative and the only "
                f"source with figures, so consult it for figures or anything the text "
                f"renders ambiguously{link}"
            )
    return f"the PDF is at `{pdf}`{link}"


def _read_code_url(repo: Path) -> str | None:
    """Read the official code URL from ``.replicator/artifacts.json``, if present.

    Returns the ``code_url`` string when the planner recorded one, ``None`` when the
    file is absent, malformed, or the planner found no code URL. Mirrors the tolerant
    approach used in ``_apply_mechanical_check`` for ``criteria.json``.
    """
    path = repo / ".replicator" / "artifacts.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        url = data.get("code_url")
        return url if isinstance(url, str) and url.strip() else None
    except Exception:
        return None


def _reference_note(repo: Path) -> str:
    """Task note pointing phases to the cloned reference code, when present.

    Returns a non-empty string only when ``.replicator/reference_code/`` exists and
    is non-empty; otherwise returns ``""`` so the ``{reference}`` placeholder in phase
    tasks expands to nothing and the prompt reads as if it were never there.
    """
    ref_dir = repo / ".replicator" / "reference_code"
    if ref_dir.exists() and any(ref_dir.iterdir()):
        return (
            "\n\nThe authors' official implementation is cloned at "
            "`.replicator/reference_code/` — consult it as a read-only reference "
            "per your instructions."
        )
    return ""


async def _checkpoint(
    repo: Path,
    provider: str,
    model_override: str | None,
    base_url: str | None,
    usages: list[PhaseUsage],
    hardware: str,
    auto_approve: bool = False,
) -> bool:
    """Show PLAN.md and ask the user to approve. Returns True to continue.

    Answering ``chat`` opens a multi-turn conversation with the reviser agent, which
    edits PLAN.md (and criteria.json) per the user's requests; the updated plan is then
    re-presented, so plans can be revised before any code lands. The chat's usage is
    appended to ``usages`` so its cost shows up in the final summary.

    When ``auto_approve=True`` the plan is printed and immediately approved without
    blocking on ``input()``, making the pipeline safe to run unattended.
    """
    plan = repo / "PLAN.md"
    if auto_approve:
        print(f"\n{'─' * 70}\n📋  PLAN.md (auto-approved)\n{'─' * 70}")
        print(plan.read_text() if plan.exists() else "  (PLAN.md was not created!)")
        print("─" * 70)
        return True
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
                await run_chat_agent(
                    REVISER,
                    repo,
                    _model(REVISER, provider, model_override),
                    hardware,
                    base_url,
                    paper_sources=_paper_sources(repo),
                )
            )
            continue
        return False


def _model(phase: Phase, provider: str, override: str | None) -> str:
    return resolve_model(phase.name, provider, override)


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
    repo: Path, provider: str, model_override: str | None, base_url: str | None, hardware: str
) -> tuple[bool, list[PhaseUsage]]:
    """Run the judging phases; on a failure verdict, repair and re-verify.

    Each round runs the tester then the benchmarker. They only diagnose — a failing
    verdict triggers a Repair phase (fed the recorded failures), after which the whole
    round restarts so the fix is re-verified. Returns (passed, usages) where passed is
    True once both judges pass within the repair budget, False if failures remain after
    :data:`_MAX_REPAIR_ATTEMPTS`.
    """
    attempts = 0
    usages: list[PhaseUsage] = []
    reference = _reference_note(repo)
    while True:
        failure: Verdict | None = None
        for judge in (TESTER, BENCHMARKER):
            clear_verdict(repo)
            usages.append(
                await run_agent(
                    judge, repo, _model(judge, provider, model_override), hardware, base_url,
                    reference=reference,
                )
            )
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
            await run_agent(
                REPAIR,
                repo,
                _model(REPAIR, provider, model_override),
                hardware,
                base_url,
                label=f"repair-{attempts}",
                pdf=_paper_pdf(repo),
                failures=format_failures([failure]),
                reference=reference,
            )
        )


def _fmt_cost(cost: float | None) -> str:
    """USD cell for the summary, or a dash when the model's price is unknown."""
    return f"${cost:>9.4f}" if cost is not None else f"{'—':>10}"


def _print_cost_summary(usages: list[PhaseUsage]) -> None:
    total_in = sum(u.input_tokens for u in usages)
    total_out = sum(u.output_tokens for u in usages)
    costs = [u.cost_usd for u in usages if u.cost_usd is not None]
    total_cost = sum(costs) if costs else None

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
            f"  {u.label:<{w}}  {u.input_tokens:>10,}  {u.output_tokens:>10,}  {_fmt_cost(u.cost_usd)}"
        )
    print(f"  {'─' * (w)}  {'─' * 10}  {'─' * 10}  {'─' * 10}")
    print(f"  {'TOTAL':<{w}}  {total_in:>10,}  {total_out:>10,}  {_fmt_cost(total_cost)}")
    print(f"{'─' * 62}")


def _parse_coder_phases(repo: Path) -> list[tuple[str, str]]:
    """Extract (title, description) pairs from the Implementation Phases section of PLAN.md.

    Returns a single-element list as a fallback when the section is absent or malformed,
    so the orchestrator degrades gracefully to the old single-phase behaviour.
    """
    plan_path = repo / "PLAN.md"
    if not plan_path.exists():
        return [("Full implementation", "")]
    plan = plan_path.read_text()
    section = re.search(r"## Implementation Phases[^\n]*\n(.*?)(?=\n## |\Z)", plan, re.DOTALL)
    if not section:
        return [("Full implementation", "")]
    # Tolerate any title separator (``Phase 1: Core``, ``Phase 1 — Core``, bare ``Phase 1``)
    # so a small format drift in the plan doesn't silently revert to a single coder run.
    phases = re.findall(
        r"### (Phase \d+[^\n]*)\n(.*?)(?=### Phase \d+|\Z)",
        section.group(1),
        re.DOTALL,
    )
    if not phases:
        return [("Full implementation", "")]
    parsed = [(title.strip(), desc.strip()) for title, desc in phases]
    if len(parsed) > _MAX_CODER_PHASES:
        # The plan's final phase builds the entry point (train.py/run.sh), so a plain
        # truncation would drop it and leave an incomplete repo. Keep the leading phases
        # and fold the overflow — including the original final phase — into the last slot.
        print(
            f"  · PLAN.md lists {len(parsed)} implementation phases; merging the overflow "
            f"into phase {_MAX_CODER_PHASES} to keep the plan's final (entry-point) phase."
        )
        head = parsed[: _MAX_CODER_PHASES - 1]
        tail = parsed[_MAX_CODER_PHASES - 1 :]
        merged_title = tail[-1][0]
        merged_desc = "\n\n".join(f"{title}\n{desc}".strip() for title, desc in tail)
        parsed = head + [(merged_title, merged_desc)]
    return parsed


def _build_phase_instruction(
    n: int, total: int, title: str, desc: str, *, is_final: bool
) -> str:
    """Build the per-phase task suffix injected into the coder's query prompt."""
    parts = [f"You are working on **Phase {n} of {total}: {title}**."]
    if desc:
        parts.append(desc)
    if is_final:
        parts.append(
            "You are the **final phase**. After implementing your scope, "
            "do the quick verification run (step 6) to confirm the full "
            "implementation runs end-to-end."
        )
    else:
        parts.append(
            f"You are **not** the final phase ({total - n} phase(s) follow). "
            "Implement your scope and stop — do not wire up the full entry point "
            "or run a verification run."
        )
    return "\n\n".join(parts)


def _syntax_check(repo: Path) -> None:
    """Syntax-check the coder's Python files; warn the operator but never abort.

    Parses each file in-process with ``ast.parse`` (non-mutating — no ``__pycache__``
    artifacts, and no dependency on a ``python`` executable being on ``PATH``; the
    pipeline runs under ``uv``). Skips ``__pycache__`` and any hidden directory
    (``.replicator/`` reference clone and logs, the ``.venv/`` the coder may create,
    ``.git/`` …) so it only ever flags files the coder actually wrote, and reports —
    rather than aborting on — a file that is not valid UTF-8 (e.g. a vendored test
    fixture with a ``big5`` coding declaration).
    """
    errs = []
    for f in repo.rglob("*.py"):
        rel = f.relative_to(repo)
        if "__pycache__" in rel.parts or any(p.startswith(".") for p in rel.parts):
            continue
        try:
            src = f.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError) as exc:
            errs.append(f"{rel}: {exc}")
            continue
        try:
            ast.parse(src, filename=str(rel))
        except SyntaxError as exc:
            errs.append(f"{rel}: {exc}")
    if errs:
        joined = "\n".join(errs)
        print(f"  ⚠ Syntax errors found (next phase will need to fix these):\n{joined[:400]}")


async def run_pipeline(
    repo: Path,
    provider: str = "anthropic",
    model_override: str | None = None,
    base_url: str | None = None,
    instructions: str = "",
    gpu: bool = False,
    auto_approve: bool = False,
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
    all_usages: list[PhaseUsage] = []

    hardware = hardware_profile(gpu)

    instructions_block = (
        "\n\nAdditional instructions from the user (treat as authoritative):\n"
        + instructions.strip()
        if instructions.strip()
        else ""
    )
    all_usages.append(
        await run_agent(
            PLANNER,
            repo,
            _model(PLANNER, provider, model_override),
            hardware,
            base_url,
            sources=_paper_sources(repo),
            instructions=instructions_block,
        )
    )
    if not await _checkpoint(
        repo, provider, model_override, base_url, all_usages, hardware, auto_approve
    ):
        print("\n✋ Stopped at planning checkpoint. The plan is in PLAN.md.")
        _print_cost_summary(all_usages)
        sys.exit(0)

    code_url = _read_code_url(repo)
    if code_url:
        ref_dir = repo / ".replicator" / "reference_code"
        result = clone_reference_code(code_url, ref_dir)
        if result:
            print(f"Ref:   cloned {code_url} → {ref_dir.relative_to(repo)}")
        else:
            print(f"Ref:   could not clone {code_url} (skipping reference)")

    reference = _reference_note(repo)
    coder_phases = _parse_coder_phases(repo)
    if len(coder_phases) == 1 and coder_phases[0][0] == "Full implementation":
        print("Coder: no Implementation Phases section found in PLAN.md — running a single coder phase.")
    else:
        print(f"Coder: {len(coder_phases)} implementation phase(s) parsed from PLAN.md.")
    for i, (phase_title, phase_desc) in enumerate(coder_phases):
        n, total = i + 1, len(coder_phases)
        is_final = i == total - 1
        phase_instruction = _build_phase_instruction(
            n, total, phase_title, phase_desc, is_final=is_final
        )
        label = "coder" if total == 1 else f"coder-{n}"
        all_usages.append(
            await run_agent(
                CODER, repo, _model(CODER, provider, model_override), hardware, base_url,
                label=label,
                reference=reference,
                phase_instruction=phase_instruction,
            )
        )
        if not is_final:
            _syntax_check(repo)

    passed, vr_usages = await _verify_and_repair(
        repo, provider, model_override, base_url, hardware
    )
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
        await run_agent(CLEANER, repo, _model(CLEANER, provider, model_override), hardware, base_url)
    )
    print(f"\n✅ Done. Replicated baseline is in {repo} (see README.md and REPORT.md).")
    _print_cost_summary(all_usages)
