"""Tests for reading/writing structured pass/fail verdicts."""

from __future__ import annotations

from replicator.verdict import (
    Failure,
    Verdict,
    clear_verdict,
    format_failures,
    read_verdict,
    write_verdict,
)


def _write_raw(repo, text: str) -> None:
    path = repo / ".replicator" / "verdict.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def test_write_then_read_pass(tmp_path):
    write_verdict(tmp_path, Verdict(phase="tester", status="pass"))
    verdict = read_verdict(tmp_path, "tester")
    assert verdict.passed is True
    assert verdict.failures == []


def test_missing_verdict_is_fail(tmp_path):
    assert read_verdict(tmp_path, "tester").passed is False


def test_phase_mismatch_is_fail(tmp_path):
    write_verdict(tmp_path, Verdict(phase="benchmarker", status="pass"))
    assert read_verdict(tmp_path, "tester").passed is False


def test_pass_with_failures_is_coerced_to_fail(tmp_path):
    write_verdict(
        tmp_path,
        Verdict(phase="tester", status="pass", failures=[Failure(criterion="x", detail="d")]),
    )
    verdict = read_verdict(tmp_path, "tester")
    assert verdict.passed is False
    assert verdict.failures[0].criterion == "x"


def test_invalid_status_is_fail(tmp_path):
    _write_raw(tmp_path, '{"phase": "tester", "status": "maybe"}')
    assert read_verdict(tmp_path, "tester").passed is False


def test_malformed_json_is_fail(tmp_path):
    _write_raw(tmp_path, "{not valid json")
    assert read_verdict(tmp_path, "tester").passed is False


def test_phase_and_status_are_case_insensitive(tmp_path):
    _write_raw(tmp_path, '{"phase": "Tester", "status": "PASS"}')
    assert read_verdict(tmp_path, "tester").passed is True


def test_clear_verdict_is_idempotent(tmp_path):
    write_verdict(tmp_path, Verdict(phase="tester", status="pass"))
    clear_verdict(tmp_path)
    assert not (tmp_path / ".replicator" / "verdict.json").exists()
    clear_verdict(tmp_path)  # second call must not raise


def test_format_failures_renders_details():
    verdict = Verdict(
        phase="tester",
        status="fail",
        failures=[Failure(criterion="acc", detail="too low", severity="major", evidence="x.json")],
    )
    out = format_failures([verdict])
    assert "acc" in out
    assert "too low" in out
    assert "evidence" in out


def test_format_failures_handles_no_failures():
    assert "no specific failures" in format_failures([Verdict(phase="tester", status="fail")])
