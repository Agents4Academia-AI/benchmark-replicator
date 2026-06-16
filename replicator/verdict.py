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


def read_verdict(repo: Path) -> Verdict | None:
    """Read the current verdict, or ``None`` if it is missing or unparseable."""
    path = repo / VERDICT_PATH
    try:
        data = json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict):
        return None

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
    status = str(data.get("status", "")).strip().lower()
    if status not in ("pass", "fail"):
        # Infer from the failures rather than trust a malformed status field.
        status = "fail" if failures else "pass"
    return Verdict(phase=str(data.get("phase", "")), status=status, failures=failures)


def clear_verdict(repo: Path) -> None:
    """Remove any stale verdict so a fresh judging phase starts from a clean slate."""
    (repo / VERDICT_PATH).unlink(missing_ok=True)


def format_failures(verdicts: list[Verdict]) -> str:
    """Render the failures across one or more verdicts as a markdown brief for Repair."""
    lines: list[str] = []
    for verdict in verdicts:
        for f in verdict.failures:
            lines.append(f"- **[{f.severity.upper()}] ({verdict.phase}) {f.criterion}** — {f.detail}")
            if f.evidence:
                lines.append(f"  - evidence: {f.evidence}")
    return "\n".join(lines) if lines else "- (no specific failures were recorded)"
