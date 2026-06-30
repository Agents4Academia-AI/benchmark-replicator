"""Tests for the mechanical (no-LLM) criteria checker."""

from __future__ import annotations

import json
from pathlib import Path

from replicator.criteria import (
    Criterion,
    evaluate,
    mechanical_failures,
    read_criteria,
)


def _write(repo: Path, rel: str, obj) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj))


def test_read_criteria_missing_returns_none(tmp_path):
    assert read_criteria(tmp_path) is None


def test_read_criteria_skips_malformed_entries(tmp_path):
    _write(
        tmp_path,
        ".replicator/criteria.json",
        {
            "criteria": [
                {"id": "acc", "metric": "accuracy", "comparison": ">=", "threshold": 0.9},
                {"id": "bad", "comparison": "??", "threshold": 1},  # bad operator
                {"metric": "no id", "comparison": ">=", "threshold": 1},  # missing id
                "garbage",
            ]
        },
    )
    criteria = read_criteria(tmp_path)
    assert criteria is not None
    assert [c.id for c in criteria] == ["acc"]


def test_read_criteria_all_invalid_returns_none(tmp_path):
    _write(
        tmp_path,
        ".replicator/criteria.json",
        {"criteria": [{"id": "x", "comparison": "??", "threshold": 1}]},
    )
    assert read_criteria(tmp_path) is None


def test_evaluate_numeric_pass_and_fail():
    criteria = [
        Criterion(id="acc", metric="accuracy", comparison=">=", threshold=0.9),
        Criterion(id="loss", metric="loss", comparison="<", threshold=0.1),
    ]
    results = {"acc": 0.95, "loss": 0.2}
    passed = {r.criterion.id: r.passed for r in evaluate(criteria, results)}
    assert passed == {"acc": True, "loss": False}


def test_evaluate_missing_value_fails():
    criteria = [Criterion(id="acc", metric="accuracy", comparison=">=", threshold=0.9)]
    (result,) = evaluate(criteria, {})
    assert result.passed is False
    assert result.value is None


def test_evaluate_boolean_criterion():
    criteria = [Criterion(id="converged", metric="converged", comparison="==", threshold=True)]
    (ok,) = evaluate(criteria, {"converged": True})
    assert ok.passed is True
    (not_bool,) = evaluate(criteria, {"converged": "yes"})
    assert not_bool.passed is False


def test_evaluate_non_numeric_value_against_numeric_threshold_fails():
    criteria = [Criterion(id="acc", metric="acc", comparison=">=", threshold=0.9)]
    (result,) = evaluate(criteria, {"acc": "high"})
    assert result.passed is False


def test_mechanical_failures_reports_only_required_failures(tmp_path):
    _write(
        tmp_path,
        ".replicator/criteria.json",
        {
            "criteria": [
                {
                    "id": "acc",
                    "metric": "accuracy",
                    "comparison": ">=",
                    "threshold": 0.9,
                    "required": True,
                },
                {
                    "id": "speed",
                    "metric": "throughput",
                    "comparison": ">=",
                    "threshold": 100,
                    "required": False,
                },
            ]
        },
    )
    _write(tmp_path, ".replicator/results.json", {"acc": 0.5, "speed": 10})
    failures, notes = mechanical_failures(tmp_path)
    assert [f.criterion for f in failures] == ["acc"]  # the optional one is not a failure
    assert len(notes) == 2  # every criterion produces a console note


def test_mechanical_failures_skips_when_no_criteria(tmp_path):
    failures, notes = mechanical_failures(tmp_path)
    assert failures == []
    assert notes and "skipped" in notes[0]


def test_mechanical_failures_flags_missing_results(tmp_path):
    _write(
        tmp_path,
        ".replicator/criteria.json",
        {"criteria": [{"id": "acc", "metric": "accuracy", "comparison": ">=", "threshold": 0.9}]},
    )
    failures, _ = mechanical_failures(tmp_path)
    assert len(failures) == 1
    assert failures[0].criterion == "results.json missing"
