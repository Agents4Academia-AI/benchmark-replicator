"""The code-driven orchestrator: run each sub-agent phase in order on one repo.

The orchestration is deterministic Python — we do not rely on the model to
auto-delegate. Each phase is an independent agent run (see ``agent.py``) with a
fresh context; state is shared only through files in the generated repo. After the
planner phase the pipeline pauses for approval, then tries bounded official-code
adoption before falling back to the original scratch implementation path.
"""

from __future__ import annotations

import ast
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from .adoption import (
    ADOPTION_PATH,
    CANDIDATE_PATH,
    REFERENCE_PATH,
    STAGES,
    WORKING_PATH,
    Candidate,
    OfficialProvenance,
    acquire_official_code,
    execute_candidate,
    is_environment_path,
    load_candidate,
    make_working_copy,
    new_adoption_record,
    remove_tree,
    source_changes,
    source_patch,
    validate_stage_changes,
    write_adoption,
)
from .agent import AgentSettings, PhaseUsage, resolve_agent_settings, run_agent, run_chat_agent
from .criteria import mechanical_failures
from .manifest import (
    install_runner,
    manifest_from_candidate,
    scratch_manifest,
    validate_manifest,
    write_manifest,
)
from .phases import (
    ADAPTER,
    ADOPTION_INSPECTOR,
    BENCHMARKER,
    CLEANER,
    CODER,
    ENVIRONMENT_FIXER,
    PLANNER,
    REPAIR,
    REVISER,
    SOURCE_PATCHER,
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

# Upper bound on coder phases parsed from PLAN.md — each phase is a full Codex run, so a
# runaway plan (or an over-eager human edit at the checkpoint) must not multiply cost.
_MAX_CODER_PHASES = 4

# Each official-code adoption stage gets one model turn and one local execution.
_ADOPTION_TIMEOUT_SECONDS = 900


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


def _decisions_needed(plan_text: str) -> str:
    """Extract the body of PLAN.md's '## Decisions needed' / '**Decisions needed**' section.

    Returns the section text (stripped) or "" if absent or explicitly "None". Used to
    surface paper/code or paper-internal contradictions at the human checkpoint so they
    are resolved before any code is written.
    """
    # The body ends at the next section. "Decisions needed" is always followed by
    # "Risks / open questions" and then the `## Implementation Phases` H2, so we stop at
    # a Risks bullet/heading or any markdown heading — *not* at the next bold bullet,
    # since the planner often renders each decision as its own `- **Name**:` bullet and a
    # generic `\n-\s*\*\*[A-Z]` terminator would drop every decision after the first.
    # ``[ \t]*`` (not ``\s*``) after the header keeps the trailing whitespace from eating
    # the newline, so an empty section terminates immediately instead of swallowing Risks.
    m = re.search(
        r"(?:^#+[ \t]*Decisions needed|^[ \t]*(?:-[ \t]*)?\*\*Decisions needed\*\*)"
        r"[ \t]*:?[ \t]*(.*?)"
        r"(?=\n#+\s|\n[ \t]*-?[ \t]*\*\*Risks|\Z)",
        plan_text,
        re.IGNORECASE | re.DOTALL | re.MULTILINE,
    )
    if not m:
        return ""
    body = m.group(1).strip()
    # Treat an explicit "None"/"N/A" (however elaborated, e.g. "None found.",
    # "None — paper and code agree.") as no decisions, so the checkpoint banner only
    # fires on real contradictions.
    if re.match(r"\s*(?:none|n/?a)\b", body, re.IGNORECASE):
        return ""
    return body


async def _checkpoint(
    repo: Path,
    model_override: str | None,
    agent_settings: dict[str, AgentSettings],
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
        plan_text = plan.read_text() if plan.exists() else "  (PLAN.md was not created!)"
        print(plan_text)
        print("─" * 70)
        decisions = _decisions_needed(plan_text)
        if decisions:
            print("\n⚠️  Decisions needed before coding (resolve via [c]hat):")
            print(decisions)
            print("─" * 70)
        answer = input("Approve plan and continue? [y]es / [N]o / [c]hat to revise PLAN.md: ")
        choice = answer.strip().lower()
        if choice in ("y", "yes"):
            return True
        if choice in ("c", "chat"):
            reviser_settings = _model(REVISER, model_override, agent_settings)
            usages.append(
                await run_chat_agent(
                    REVISER,
                    repo,
                    reviser_settings.model,
                    hardware,
                    paper_sources=_paper_sources(repo),
                    reasoning_effort=reviser_settings.reasoning_effort,
                )
            )
            continue
        return False


def _model(
    phase: Phase, override: str | None, agent_settings: dict[str, AgentSettings]
) -> AgentSettings:
    return resolve_agent_settings(phase.name, override, agent_settings)


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
    combined = list(verdict.failures) + [f for f in failures if f not in verdict.failures]
    verdict = Verdict(phase=verdict.phase, status="fail", failures=combined)
    write_verdict(repo, verdict)
    return verdict


async def _verify_and_repair(
    repo: Path,
    model_override: str | None,
    agent_settings: dict[str, AgentSettings],
    hardware: str,
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
    # On a repair round triggered by the benchmarker, the tester already passed and the
    # repair targets a benchmark criterion — re-running the (slow) tester before the
    # benchmarker is green adds cost without new signal. So defer it: run the benchmarker
    # first and re-run the tester only after the benchmarker passes. A full tester+benchmarker
    # pass is still required for SUCCESS — the loop breaks on the first failing judge, so the
    # deferred tester runs (confirming the fix didn't break tests) exactly when the benchmarker
    # is green, and is skipped while the benchmarker is still red.
    defer_tester = False
    while True:
        failure: Verdict | None = None
        judges = (BENCHMARKER, TESTER) if defer_tester else (TESTER, BENCHMARKER)
        defer_tester = False
        for judge in judges:
            clear_verdict(repo)
            settings = _model(judge, model_override, agent_settings)
            usages.append(
                await run_agent(
                    judge,
                    repo,
                    settings.model,
                    hardware,
                    reasoning_effort=settings.reasoning_effort,
                    reference=reference,
                )
            )
            verdict = read_verdict(repo, judge.name)
            if judge is BENCHMARKER:
                verdict = _apply_mechanical_check(repo, verdict)
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
        # If the benchmarker triggered this repair, the tester was green and the fix targets
        # a benchmark criterion — next round run the benchmarker first and defer the tester
        # behind it, so the slow tester re-runs (confirming the fix didn't break it) only once
        # the benchmarker goes green.
        defer_tester = failure.phase == "benchmarker"
        print(
            f"\n🔧 Repair attempt {attempts}/{_MAX_REPAIR_ATTEMPTS} (triggered by {failure.phase})."
        )
        repair_settings = _model(REPAIR, model_override, agent_settings)
        usages.append(
            await run_agent(
                REPAIR,
                repo,
                repair_settings.model,
                hardware,
                label=f"repair-{attempts}",
                reasoning_effort=repair_settings.reasoning_effort,
                pdf=_paper_pdf(repo),
                failures=format_failures([failure]),
                reference=reference,
            )
        )


def _print_usage_summary(usages: list[PhaseUsage]) -> None:
    total_in = sum(u.input_tokens for u in usages)
    total_out = sum(u.output_tokens for u in usages)

    w = 18  # label column width
    print(f"\n{'─' * 48}")
    print("  Token usage")
    print(f"{'─' * 48}")
    print(f"  {'Phase':<{w}}  {'Input tok':>10}  {'Output tok':>10}")
    print(f"  {'─' * w}  {'─' * 10}  {'─' * 10}")
    for u in usages:
        print(f"  {u.label:<{w}}  {u.input_tokens:>10,}  {u.output_tokens:>10,}")
    print(f"  {'─' * w}  {'─' * 10}  {'─' * 10}")
    print(f"  {'TOTAL':<{w}}  {total_in:>10,}  {total_out:>10,}")
    print(f"{'─' * 48}")


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


def _build_phase_instruction(n: int, total: int, title: str, desc: str, *, is_final: bool) -> str:
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


def _record_successful_adoption(
    record: dict, reference: Path, working: Path, candidate: Candidate
) -> None:
    changes = source_changes(reference, working)
    environment = [path for path in changes if is_environment_path(path)]
    record["environment_changes"] = sorted(environment)
    record["adapters"] = sorted(path for path in candidate.adapter_files if path in changes)
    record["source_modifications"] = sorted(
        path
        for path in changes
        if path not in environment
        and path not in candidate.adapter_files
        and path != candidate.result_path
    )
    record["source_patch"] = source_patch(reference, working, {candidate.result_path})


async def _attempt_official_code(
    repo: Path,
    code_url: str | None,
    model_override: str | None,
    agent_settings: dict[str, AgentSettings],
    hardware: str,
    usages: list[PhaseUsage],
    *,
    unsafe_local_official_code: bool = False,
) -> tuple[str | None, Candidate | None, OfficialProvenance | None, dict]:
    """Run the four fixed adoption levels, once each, on a disposable copy."""
    reference = repo / REFERENCE_PATH
    working = repo / WORKING_PATH
    adoption_path = repo / ADOPTION_PATH
    if not code_url:
        record = new_adoption_record(None, strategy="reuse-first")
        record["failures"].append("planner found no verified official code repository")
        write_adoption(adoption_path, record)
        return None, None, None, record

    provenance = acquire_official_code(code_url, reference)
    record = new_adoption_record(
        provenance,
        strategy="reuse-first",
        execution_local=unsafe_local_official_code,
    )
    if provenance is None:
        record["failures"].append("official repository could not be retrieved")
        write_adoption(adoption_path, record)
        return None, None, None, record

    print(
        f"Official: preserved {code_url} at {REFERENCE_PATH} "
        f"({provenance.commit_sha[:12]}, license: {provenance.license})"
    )
    if not unsafe_local_official_code:
        record["failures"].append(
            "official code was not executed: use --unsafe-local-official-code or configure a sandbox backend"
        )
        write_adoption(adoption_path, record)
        return None, None, provenance, record
    make_working_copy(reference, working)
    context = working / ".replicator" / "context"
    context.mkdir(parents=True)
    if (repo / "PLAN.md").exists():
        shutil.copy2(repo / "PLAN.md", context / "PLAN.md")
    if (repo / ".replicator" / "criteria.json").exists():
        shutil.copy2(repo / ".replicator" / "criteria.json", context / "criteria.json")
    inspector_settings = _model(ADOPTION_INSPECTOR, model_override, agent_settings)
    try:
        usages.append(
            await run_agent(
                ADOPTION_INSPECTOR,
                working,
                inspector_settings.model,
                hardware,
                reasoning_effort=inspector_settings.reasoning_effort,
            )
        )
    except Exception as exc:
        record["failures"].append(f"official-code inspection failed: {exc}")
        write_adoption(adoption_path, record)
        remove_tree(working)
        return None, None, provenance, record
    if source_changes(reference, working):
        record["failures"].append("official-code inspector modified its read-only working copy")
        write_adoption(adoption_path, record)
        remove_tree(working)
        return None, None, provenance, record

    candidate_path = working / CANDIDATE_PATH
    phases = {
        "environment": ENVIRONMENT_FIXER,
        "adapter": ADAPTER,
        "source_patch": SOURCE_PATCHER,
    }
    failure = "The initial candidate has not run yet."
    for origin, action_name in STAGES:
        if action_name:
            phase = phases[action_name]
            with tempfile.TemporaryDirectory(prefix="replicator-adoption-") as temp:
                backup = Path(temp) / "working"
                reference_backup = Path(temp) / "reference"
                shutil.copytree(working, backup, symlinks=True)
                shutil.copytree(reference, reference_backup, symlinks=True)
                candidate_backup = candidate_path.read_bytes() if candidate_path.exists() else None
                settings = _model(phase, model_override, agent_settings)
                phase_error = ""
                try:
                    usages.append(
                        await run_agent(
                            phase,
                            working,
                            settings.model,
                            hardware,
                            reasoning_effort=settings.reasoning_effort,
                            failure=failure,
                        )
                    )
                except Exception as exc:
                    phase_error = f"{phase.name} failed: {exc}"
                if phase_error:
                    allowed, detail = False, phase_error
                else:
                    try:
                        candidate = load_candidate(candidate_path)
                        allowed, detail = validate_stage_changes(
                            origin, reference, working, candidate
                        )
                    except Exception as exc:
                        allowed, detail = False, str(exc)
                immutable_changes = source_changes(reference_backup, reference)
                if immutable_changes:
                    remove_tree(reference)
                    shutil.copytree(reference_backup, reference, symlinks=True)
                    allowed = False
                    detail = "stage attempted to modify the immutable official reference"
                if not allowed:
                    remove_tree(working)
                    shutil.copytree(backup, working, symlinks=True)
                    if candidate_backup is None:
                        candidate_path.unlink(missing_ok=True)
                    else:
                        candidate_path.write_bytes(candidate_backup)
                    failure = detail
                    record["attempts"].append(
                        {"stage": origin, "status": "failed", "failure": detail}
                    )
                    record["failures"].append(detail)
                    write_adoption(adoption_path, record)
                    continue
        try:
            candidate = load_candidate(candidate_path)
            allowed, detail = validate_stage_changes(origin, reference, working, candidate)
        except Exception as exc:
            candidate, allowed, detail = None, False, str(exc)
        if not allowed or candidate is None:
            failure = detail
            record["attempts"].append({"stage": origin, "status": "failed", "failure": detail})
            record["failures"].append(detail)
            write_adoption(adoption_path, record)
            continue

        result = execute_candidate(
            candidate,
            working,
            _ADOPTION_TIMEOUT_SECONDS,
            unsafe_local=unsafe_local_official_code,
        )
        attempt = {
            "stage": origin,
            "status": "succeeded" if result.succeeded else "failed",
            "command": result.command,
            "returncode": result.returncode,
            "runtime_seconds": round(result.runtime_seconds, 3),
        }
        attempt["setup_command"] = candidate.setup_command
        if result.failure:
            attempt["failure"] = result.failure
            record["failures"].append(result.failure)
        record["attempts"].append(attempt)
        if result.succeeded:
            record["selected_origin"] = origin
            _record_successful_adoption(record, reference, working, candidate)
            write_adoption(adoption_path, record)
            print(f"Adoption: {origin} candidate ran successfully.")
            return origin, candidate, provenance, record
        failure = result.failure
        write_adoption(adoption_path, record)

    remove_tree(working)
    return None, None, provenance, record


def _seed_verification_repo(
    repo: Path,
    working: Path,
    candidate: Candidate,
    origin: str,
    provenance: OfficialProvenance,
    record: dict,
    hardware_mode: str,
) -> Path:
    """Build an isolated repo so failed adoption judges cannot pollute scratch output."""
    stage = repo / ".replicator" / "adoption_verification"
    remove_tree(stage)
    stage.mkdir(parents=True)
    for name in ("PLAN.md",):
        if (repo / name).exists():
            shutil.copy2(repo / name, stage / name)
    shutil.copytree(repo / "paper", stage / "paper", symlinks=True)
    control = stage / ".replicator"
    control.mkdir()
    for name in ("criteria.json", "artifacts.json"):
        source = repo / ".replicator" / name
        if source.exists():
            shutil.copy2(source, control / name)
    shutil.copytree(repo / REFERENCE_PATH, control / "reference_code", symlinks=True)
    shutil.copytree(
        working,
        stage / "official",
        symlinks=True,
        ignore=shutil.ignore_patterns(
            ".replicator", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache"
        ),
    )
    modifications = {
        "environment": record.get("environment_changes", []),
        "adapters": record.get("adapters", []),
        "source": record.get("source_modifications", []),
        "source_patch": record.get("source_patch", ""),
    }
    manifest = manifest_from_candidate(
        stage,
        candidate,
        origin=origin,
        provenance=provenance,
        modifications=modifications,
        hardware_mode=hardware_mode,
    )
    write_manifest(stage, manifest)
    install_runner(stage)
    (stage / "run-spec.json").write_text("{}\n")
    return stage


def _mark_manifest_verified(
    repo: Path, origin: str | None = None, modifications: dict[str, object] | None = None
) -> None:
    manifest = json.loads((repo / "baseline.json").read_text())
    manifest["status"] = "success"
    manifest["verification"]["status"] = "passed"
    if origin is not None:
        manifest["implementation_origin"] = origin
    if modifications is not None:
        manifest["modifications"] = modifications
    write_manifest(repo, manifest)
    install_runner(repo)


def _run_stable_runner(repo: Path) -> tuple[bool, str]:
    """Validate the manifest and execute the portable default contract once."""
    try:
        manifest = json.loads((repo / "baseline.json").read_text())
    except (OSError, json.JSONDecodeError) as exc:
        return False, f"baseline.json is unreadable: {exc}"
    errors = validate_manifest(manifest)
    if errors:
        return False, "; ".join(errors)
    try:
        result = subprocess.run(
            ["bash", "run.sh", "--spec", "run-spec.json", "--output", "run-result.json"],
            cwd=repo,
            timeout=_ADOPTION_TIMEOUT_SECONDS,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    if result.returncode:
        return False, (result.stderr.strip() or result.stdout.strip())[-2000:]
    try:
        normalized = json.loads((repo / "run-result.json").read_text())
    except (OSError, json.JSONDecodeError) as exc:
        return False, f"run-result.json is unreadable: {exc}"
    required = {"status", "seed", "metrics", "runtime_seconds", "implementation_origin"}
    if not isinstance(normalized, dict) or not required <= normalized.keys():
        return False, "run-result.json does not satisfy the normalized result contract"
    if normalized["status"] != "success":
        return False, "stable runner did not report success"
    failures, _notes = mechanical_failures(repo)
    if failures:
        return False, "stable runner metrics failed mechanical criteria: " + ", ".join(
            failure.criterion for failure in failures
        )
    return True, ""


def _mark_manifest_pending(repo: Path) -> None:
    manifest = json.loads((repo / "baseline.json").read_text())
    manifest["status"] = "pending"
    manifest["verification"]["status"] = "pending"
    write_manifest(repo, manifest)


def _promote_verification_repo(stage: Path, repo: Path) -> None:
    """Promote a verified adoption while preserving outer provenance and paper files."""
    for child in stage.iterdir():
        if child.name in {"PLAN.md", "paper", ".replicator"}:
            continue
        target = repo / child.name
        if target.exists() or target.is_symlink():
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            else:
                target.unlink()
        if child.is_dir() and not child.is_symlink():
            shutil.copytree(child, target, symlinks=True)
        else:
            shutil.copy2(child, target)
    stage_control = stage / ".replicator"
    outer_control = repo / ".replicator"
    for name in ("run.py", "results.json", "verdict.json"):
        source = stage_control / name
        if source.exists():
            shutil.copy2(source, outer_control / name)
    if (stage_control / "logs").exists():
        shutil.copytree(stage_control / "logs", outer_control / "logs", dirs_exist_ok=True)


def _discard_adoption(repo: Path, stage: Path, record: dict, failure: str) -> bool:
    record["failures"].append(failure)
    record["selected_origin"] = None
    write_adoption(repo / ADOPTION_PATH, record)
    remove_tree(stage)
    remove_tree(repo / WORKING_PATH)
    return False


async def _verify_adoption(
    repo: Path,
    origin: str,
    candidate: Candidate,
    provenance: OfficialProvenance,
    record: dict,
    model_override: str | None,
    agent_settings: dict[str, AgentSettings],
    hardware: str,
    hardware_mode: str,
    usages: list[PhaseUsage],
) -> bool:
    stage = repo / ".replicator" / "adoption_verification"
    try:
        stage = _seed_verification_repo(
            repo,
            repo / WORKING_PATH,
            candidate,
            origin,
            provenance,
            record,
            hardware_mode,
        )
        passed, verification_usages = await _verify_and_repair(
            stage, model_override, agent_settings, hardware
        )
    except Exception as exc:
        return _discard_adoption(repo, stage, record, f"adoption verification error: {exc}")
    usages.extend(verification_usages)
    if not passed:
        return _discard_adoption(
            repo, stage, record, "adopted implementation failed bounded verification repairs"
        )

    cleaner_settings = _model(CLEANER, model_override, agent_settings)
    try:
        usages.append(
            await run_agent(
                CLEANER,
                stage,
                cleaner_settings.model,
                hardware,
                reasoning_effort=cleaner_settings.reasoning_effort,
            )
        )
    except Exception as exc:
        return _discard_adoption(repo, stage, record, f"adoption cleanup error: {exc}")
    changes = source_changes(repo / REFERENCE_PATH, stage / "official")
    post_repair_source = [
        path
        for path in changes
        if path not in record.get("environment_changes", [])
        and path not in record.get("adapters", [])
        and path != candidate.result_path
    ]
    final_origin = "official_patched" if post_repair_source else origin
    allowed, patch_failure = validate_stage_changes(
        "official_patched", repo / REFERENCE_PATH, stage / "official", candidate
    )
    if not allowed:
        return _discard_adoption(
            repo, stage, record, f"verification repair rejected: {patch_failure}"
        )
    record["source_modifications"] = sorted(post_repair_source)
    record["source_patch"] = source_patch(
        repo / REFERENCE_PATH, stage / "official", {candidate.result_path}
    )
    record["selected_origin"] = final_origin
    modifications = {
        "environment": record.get("environment_changes", []),
        "adapters": record.get("adapters", []),
        "source": record.get("source_modifications", []),
        "source_patch": record.get("source_patch", ""),
    }
    try:
        _mark_manifest_verified(stage, final_origin, modifications)
        stable, failure = _run_stable_runner(stage)
    except Exception as exc:
        return _discard_adoption(repo, stage, record, f"stable runner setup failed: {exc}")
    if not stable:
        return _discard_adoption(
            repo, stage, record, f"stable runner verification failed: {failure}"
        )

    write_adoption(repo / ADOPTION_PATH, record)
    _promote_verification_repo(stage, repo)
    remove_tree(stage)
    remove_tree(repo / WORKING_PATH)
    return True


async def run_pipeline(
    repo: Path,
    model_override: str | None = None,
    agent_settings: dict[str, AgentSettings] | None = None,
    instructions: str = "",
    gpu: bool = False,
    auto_approve: bool = False,
    strategy: str = "reuse-first",
    unsafe_local_official_code: bool = False,
) -> None:
    """Run the replication pipeline over ``repo``.

    Flow: plan → checkpoint → bounded official-code adoption → verify-and-repair →
    clean. If adoption is unavailable or fails, the existing scratch coder path runs.

    ``gpu=True`` activates GPU mode: the hardware profile injected into every phase's
    system prompt switches from CPU_PROFILE to GPU_PROFILE, steering the planner toward
    ambitious paper-scale default configs plus a separate reduced verification run.
    """
    print(f"\nBaseline replicator → {repo}")
    if strategy not in {"reuse-first", "scratch"}:
        raise ValueError("strategy must be 'reuse-first' or 'scratch'")
    all_usages: list[PhaseUsage] = []
    agent_settings = agent_settings or {}

    hardware = hardware_profile(gpu)

    instructions_block = (
        "\n\nAdditional instructions from the user (treat as authoritative):\n"
        + instructions.strip()
        if instructions.strip()
        else ""
    )
    planner_settings = _model(PLANNER, model_override, agent_settings)
    all_usages.append(
        await run_agent(
            PLANNER,
            repo,
            planner_settings.model,
            hardware,
            reasoning_effort=planner_settings.reasoning_effort,
            sources=_paper_sources(repo),
            instructions=instructions_block,
        )
    )
    if not await _checkpoint(
        repo, model_override, agent_settings, all_usages, hardware, auto_approve
    ):
        print("\n✋ Stopped at planning checkpoint. The plan is in PLAN.md.")
        _print_usage_summary(all_usages)
        sys.exit(0)

    code_url = _read_code_url(repo)
    provenance: OfficialProvenance | None = None
    adoption_record: dict
    if strategy == "reuse-first":
        origin, candidate, provenance, adoption_record = await _attempt_official_code(
            repo,
            code_url,
            model_override,
            agent_settings,
            hardware,
            all_usages,
            unsafe_local_official_code=unsafe_local_official_code,
        )
        if origin and candidate and provenance:
            adopted = await _verify_adoption(
                repo,
                origin,
                candidate,
                provenance,
                adoption_record,
                model_override,
                agent_settings,
                hardware,
                "gpu" if gpu else "cpu",
                all_usages,
            )
            if adopted:
                print(f"\n✅ Done. Adopted official baseline is in {repo}.")
                _print_usage_summary(all_usages)
                return
            print("\n↪ Official code did not pass verification; starting clean scratch fallback.")
        else:
            print("\n↪ Official code was unavailable or unusable; starting scratch fallback.")
    else:
        if code_url:
            provenance = acquire_official_code(code_url, repo / REFERENCE_PATH)
        adoption_record = new_adoption_record(provenance, strategy="scratch")
        adoption_record["selected_origin"] = "reimplemented"
        write_adoption(repo / ADOPTION_PATH, adoption_record)

    reference = _reference_note(repo)
    coder_phases = _parse_coder_phases(repo)
    if len(coder_phases) == 1 and coder_phases[0][0] == "Full implementation":
        print(
            "Coder: no Implementation Phases section found in PLAN.md — running a single coder phase."
        )
    else:
        print(f"Coder: {len(coder_phases)} implementation phase(s) parsed from PLAN.md.")
    for i, (phase_title, phase_desc) in enumerate(coder_phases):
        n, total = i + 1, len(coder_phases)
        is_final = i == total - 1
        phase_instruction = _build_phase_instruction(
            n, total, phase_title, phase_desc, is_final=is_final
        )
        label = "coder" if total == 1 else f"coder-{n}"
        coder_settings = _model(CODER, model_override, agent_settings)
        all_usages.append(
            await run_agent(
                CODER,
                repo,
                coder_settings.model,
                hardware,
                label=label,
                reasoning_effort=coder_settings.reasoning_effort,
                reference=reference,
                phase_instruction=phase_instruction,
            )
        )
        if not is_final:
            _syntax_check(repo)

    manifest = scratch_manifest(repo, "gpu" if gpu else "cpu", provenance=provenance)
    write_manifest(repo, manifest)
    install_runner(repo)
    (repo / "run-spec.json").write_text("{}\n")

    passed, vr_usages = await _verify_and_repair(repo, model_override, agent_settings, hardware)
    all_usages.extend(vr_usages)

    if not passed:
        print(
            f"\n⚠️  Stopping before cleanup: the implementation still fails after "
            f"{_MAX_REPAIR_ATTEMPTS} repair attempt(s).\n"
            f"    See REPORT.md and {VERDICT_PATH} for the outstanding failures."
        )
        _print_usage_summary(all_usages)
        sys.exit(1)

    cleaner_settings = _model(CLEANER, model_override, agent_settings)
    all_usages.append(
        await run_agent(
            CLEANER,
            repo,
            cleaner_settings.model,
            hardware,
            reasoning_effort=cleaner_settings.reasoning_effort,
        )
    )
    _mark_manifest_verified(repo)
    stable, failure = _run_stable_runner(repo)
    if not stable:
        _mark_manifest_pending(repo)
        print(f"\n⚠️  Stable runner verification failed: {failure}")
        _print_usage_summary(all_usages)
        sys.exit(1)
    adoption_record["selected_origin"] = "reimplemented"
    write_adoption(repo / ADOPTION_PATH, adoption_record)
    print(f"\n✅ Done. Replicated baseline is in {repo} (see README.md and REPORT.md).")
    _print_usage_summary(all_usages)
