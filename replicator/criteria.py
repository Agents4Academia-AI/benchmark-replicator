"""Machine-readable success criteria shared across the planner, coder, and benchmarker.

The Planner writes ``.replicator/criteria.json`` declaring each *mechanizable* smoke-run
criterion as a stable ``id`` plus a metric description, a comparison operator, a
threshold, and whether it is ``required``. The Coder's entry point writes the measured
values to ``.replicator/results.json`` keyed by those same ids. The orchestrator then
compares the two **mechanically** — no LLM judgement — so a numeric or boolean criterion
either provably passes or provably fails.

Qualitative criteria that cannot be reduced to metric + operator + threshold stay as prose
in ``PLAN.md`` and remain the Benchmarker's job to judge narratively.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .verdict import Failure

CRITERIA_PATH = ".replicator/criteria.json"
RESULTS_PATH = ".replicator/results.json"

# The only comparison operators a criterion may declare. Each maps to a binary test of the
# measured value against the declared threshold.
_OPS = {
    ">=": lambda v, t: v >= t,
    "<=": lambda v, t: v <= t,
    ">": lambda v, t: v > t,
    "<": lambda v, t: v < t,
    "==": lambda v, t: v == t,
    "!=": lambda v, t: v != t,
}


def _is_numeric(value: object) -> bool:
    """True for JSON numbers, but not booleans."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


@dataclass(frozen=True)
class Criterion:
    """One mechanizable smoke-run criterion, as declared by the Planner."""

    id: str
    metric: str
    comparison: str  # one of _OPS
    threshold: float | bool
    required: bool = True


@dataclass(frozen=True)
class CriterionResult:
    """The outcome of comparing one criterion against the measured results."""

    criterion: Criterion
    value: object  # the measured value, or None if absent from results.json
    passed: bool
    detail: str


def read_criteria(repo: Path) -> list[Criterion] | None:
    """Load and validate ``criteria.json``; return ``None`` if absent or malformed.

    A missing or malformed criteria file is a Planner-phase problem the orchestrator
    cannot repair (Repair only edits source), so callers degrade to the Benchmarker's
    narrative verdict rather than looping repair pointlessly. Individually malformed
    entries are skipped; the file is usable as long as at least one valid criterion remains.
    """
    try:
        data = json.loads((repo / CRITERIA_PATH).read_text())
    except FileNotFoundError, OSError, json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("criteria"), list):
        return None

    criteria: list[Criterion] = []
    for entry in data["criteria"]:
        if not isinstance(entry, dict):
            continue
        cid = entry.get("id")
        comparison = entry.get("comparison")
        threshold = entry.get("threshold")
        if not isinstance(cid, str) or comparison not in _OPS:
            continue
        if not isinstance(threshold, bool) and not _is_numeric(threshold):
            continue
        criteria.append(
            Criterion(
                id=cid,
                metric=str(entry.get("metric", "")),
                comparison=comparison,
                threshold=threshold,
                required=bool(entry.get("required", True)),
            )
        )
    return criteria or None


def read_results(repo: Path) -> dict | None:
    """Load ``results.json`` (the values the entry point measured); ``None`` if unreadable."""
    try:
        data = json.loads((repo / RESULTS_PATH).read_text())
    except FileNotFoundError, OSError, json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def evaluate(criteria: list[Criterion], results: dict) -> list[CriterionResult]:
    """Compare each criterion's measured value against its threshold, deterministically."""
    out: list[CriterionResult] = []
    for c in criteria:
        if c.id not in results:
            out.append(
                CriterionResult(
                    c, None, False, f"results.json has no value for id {c.id!r}"
                )
            )
            continue
        value = results[c.id]
        if isinstance(c.threshold, bool):
            if not isinstance(value, bool):
                out.append(
                    CriterionResult(
                        c,
                        value,
                        False,
                        f"measured {value!r} is not a boolean value like threshold "
                        f"{c.threshold!r}",
                    )
                )
                continue
        elif not _is_numeric(value):
            out.append(
                CriterionResult(
                    c,
                    value,
                    False,
                    f"measured {value!r} is not a numeric value like threshold "
                    f"{c.threshold!r}",
                )
            )
            continue
        try:
            ok = bool(_OPS[c.comparison](value, c.threshold))
        except TypeError:
            out.append(
                CriterionResult(
                    c,
                    value,
                    False,
                    f"measured {value!r} is not comparable to threshold "
                    f"{c.threshold!r} with {c.comparison!r}",
                )
            )
            continue
        out.append(
            CriterionResult(
                c,
                value,
                ok,
                f"measured {value!r} {c.comparison} {c.threshold!r} → {'pass' if ok else 'fail'}",
            )
        )
    return out


def mechanical_failures(repo: Path) -> tuple[list[Failure], list[str]]:
    """Mechanically check the smoke results against the declared criteria.

    Returns ``(failures, notes)``. ``failures`` are required-criterion failures to fold
    into the Benchmarker verdict — each triggers Repair. ``notes`` are human-readable
    console lines covering every criterion checked (plus any degraded/skipped case).
    """
    criteria = read_criteria(repo)
    if not criteria:
        return [], [
            f"mechanical check skipped: no valid {CRITERIA_PATH} (planner artifact) — "
            f"falling back to the benchmarker's narrative verdict."
        ]

    results = read_results(repo)
    if results is None:
        return (
            [
                Failure(
                    criterion="results.json missing",
                    detail=f"the entry point wrote no readable {RESULTS_PATH}, so no smoke-run "
                    f"criterion can be checked mechanically.",
                    severity="major",
                    evidence=RESULTS_PATH,
                )
            ],
            [],
        )

    failures: list[Failure] = []
    notes: list[str] = []
    for r in evaluate(criteria, results):
        tag = "" if r.criterion.required else " (informational, not required)"
        notes.append(f"{'✓' if r.passed else '✗'} [{r.criterion.id}] {r.detail}{tag}")
        if not r.passed and r.criterion.required:
            failures.append(
                Failure(
                    criterion=r.criterion.id,
                    detail=f"{r.criterion.metric}: {r.detail}",
                    severity="major",
                    evidence=f"{CRITERIA_PATH} vs {RESULTS_PATH}",
                )
            )
    return failures, notes
