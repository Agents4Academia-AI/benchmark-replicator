"""Structured pass/fail verdicts shared between the judging phases and the orchestrator.

The Tester and Benchmarker sub-agents *diagnose* the implementation and write a
verdict to ``.replicator/verdict.json``. The orchestrator reads it to decide
whether to invoke the Repair sub-agent (and re-verify) or move on. Fixing is the
Repair agent's job, never the judges' — so a "pass" is an independent judgement,
not a self-graded one.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

# Where the judging phases write their verdict (relative to the generated repo).
VERDICT_PATH = ".replicator/verdict.json"


@dataclass(frozen=True)
class Failure:
    """One thing the implementation got wrong, as diagnosed by a judging phase."""

    criterion: str
    detail: str
    severity: str = "major"  # "major" | "minor"
    evidence: str = ""


@dataclass(frozen=True)
class Verdict:
    """A judging phase's structured verdict on the current implementation."""

    phase: str
    status: str  # "pass" | "fail"
    failures: list[Failure] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.status == "pass"


def read_verdict(repo: Path, expected_phase: str) -> Verdict:
    """Read and validate the judging phase's verdict, treating any problem as a FAIL.

    A judge that does not leave a clean, well-formed "pass" has not demonstrated
    success — silently assuming a pass is the pipeline's biggest false-success risk.
    So a missing file, unparseable JSON, a non-object payload, a phase that does not
    match the judge that just ran, an unrecognised status, or a "pass" that still
    lists failures all resolve to a failing verdict that triggers repair (or a stop).
    """
    path = repo / VERDICT_PATH

    def malformed(detail: str) -> Verdict:
        return Verdict(
            phase=expected_phase,
            status="fail",
            failures=[
                Failure(
                    criterion="malformed verdict", detail=detail, evidence=VERDICT_PATH
                )
            ],
        )

    try:
        data = json.loads(path.read_text())
    except (FileNotFoundError, OSError):
        return malformed(f"{expected_phase} wrote no verdict at {VERDICT_PATH}.")
    except json.JSONDecodeError as exc:
        return malformed(f"{expected_phase}'s verdict is not valid JSON: {exc}.")
    if not isinstance(data, dict):
        return malformed(f"{expected_phase}'s verdict is not a JSON object.")

    failures = [
        Failure(
            criterion=str(f.get("criterion", "")),
            detail=str(f.get("detail", "")),
            severity=str(f.get("severity", "major")),
            evidence=str(f.get("evidence", "")),
        )
        for f in data.get("failures", [])
        if isinstance(f, dict)
    ]
    phase = str(data.get("phase", "")).strip().lower()
    if phase != expected_phase:
        return malformed(
            f"verdict phase {phase!r} does not match the {expected_phase} judge."
        )
    status = str(data.get("status", "")).strip().lower()
    if status not in ("pass", "fail"):
        return malformed(f"verdict status {status!r} is not 'pass' or 'fail'.")
    if status == "pass" and failures:
        # A self-contradictory "pass" with recorded failures is not a pass; keep the
        # recorded failures so Repair can act on the real issues.
        return Verdict(phase=phase, status="fail", failures=failures)
    return Verdict(phase=phase, status=status, failures=failures)


def clear_verdict(repo: Path) -> None:
    """Remove any stale verdict so a fresh judging phase starts from a clean slate."""
    (repo / VERDICT_PATH).unlink(missing_ok=True)


def write_verdict(repo: Path, verdict: Verdict) -> None:
    """Persist a structured verdict in the format expected from judging phases."""
    path = repo / VERDICT_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "phase": verdict.phase,
        "status": verdict.status,
        "failures": [
            {
                "criterion": f.criterion,
                "severity": f.severity,
                "detail": f.detail,
                "evidence": f.evidence,
            }
            for f in verdict.failures
        ],
    }
    path.write_text(json.dumps(data, indent=2) + "\n")


def format_failures(verdicts: list[Verdict]) -> str:
    """Render the failures across one or more verdicts as a markdown brief for Repair."""
    lines: list[str] = []
    for verdict in verdicts:
        for f in verdict.failures:
            lines.append(
                f"- **[{f.severity.upper()}] ({verdict.phase}) {f.criterion}** — {f.detail}"
            )
            if f.evidence:
                lines.append(f"  - evidence: {f.evidence}")
    return "\n".join(lines) if lines else "- (no specific failures were recorded)"
