"""Offline adoption-stage tests using tiny fake official repositories."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from replicator.adoption import (
    acquire_official_code,
    make_working_copy,
    new_adoption_record,
    scrub,
    try_adoption,
    write_adoption,
)


def _candidate(path: Path, *, command: list[str] | None = None, adapters: list[str] | None = None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": "1",
                "method_name": "Fake Method",
                "command": command or [sys.executable, "main.py"],
                "result": {"path": "metrics.json", "format": "json"},
                "metric_map": {"score": "score"},
                "supported_overrides": {},
                "default_seed": 7,
                "adapter_files": adapters or [],
            }
        )
    )


def _trees(tmp_path: Path, source: str) -> tuple[Path, Path, Path]:
    reference = tmp_path / "reference"
    reference.mkdir()
    (reference / "main.py").write_text(source)
    working = tmp_path / "working"
    make_working_copy(reference, working)
    candidate = tmp_path / "control" / "candidate.json"
    _candidate(candidate)
    return reference, working, candidate


def _write_success(path: str = "metrics.json") -> str:
    return f"import json\njson.dump({{'score': 1}}, open({path!r}, 'w'))\n"


def test_official_code_works_unchanged_without_inheriting_secrets(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-be-visible")
    source = (
        "import json, os\n"
        "json.dump({'score': 'OPENAI_API_KEY' not in os.environ}, open('metrics.json', 'w'))\n"
    )
    reference, working, candidate = _trees(tmp_path, source)
    record = new_adoption_record(None, strategy="reuse-first")

    origin, _, result = try_adoption(reference, working, candidate, record, timeout=5)

    assert origin == "official_unmodified"
    assert result is not None and result.metrics == {"score": True}
    assert [attempt["stage"] for attempt in record["attempts"]] == ["official_unmodified"]


def test_environment_fix_succeeds(tmp_path):
    source = (
        "from pathlib import Path\nimport sys\n"
        "sys.exit(1) if Path('requirements.txt').read_text() != 'fixed' else None\n"
        + _write_success()
    )
    reference, working, candidate = _trees(tmp_path, source)
    (reference / "requirements.txt").write_text("broken")
    make_working_copy(reference, working)
    record = new_adoption_record(None, strategy="reuse-first")

    def environment(_name: str, work: Path, _candidate_path: Path):
        (work / "requirements.txt").write_text("fixed")

    origin, _, _ = try_adoption(
        reference,
        working,
        candidate,
        record,
        stage_actions={"environment": environment},
        timeout=5,
    )

    assert origin == "official_environment_fixed"
    assert record["environment_changes"] == ["requirements.txt"]


def test_adapter_succeeds_without_source_changes(tmp_path):
    reference, working, candidate = _trees(tmp_path, "print('score=1')\n")
    record = new_adoption_record(None, strategy="reuse-first")

    def noop(_name: str, _work: Path, _candidate_path: Path):
        pass

    def adapter(_name: str, work: Path, candidate_path: Path):
        (work / "adapter.py").write_text(_write_success())
        _candidate(candidate_path, command=[sys.executable, "adapter.py"], adapters=["adapter.py"])

    origin, _, _ = try_adoption(
        reference,
        working,
        candidate,
        record,
        stage_actions={"environment": noop, "adapter": adapter},
        timeout=5,
    )

    assert origin == "official_adapted"
    assert record["adapters"] == ["adapter.py"]
    assert record["source_modifications"] == []


def test_minimal_source_patch_succeeds(tmp_path):
    reference, working, candidate = _trees(tmp_path, "raise RuntimeError('old API')\n")
    record = new_adoption_record(None, strategy="reuse-first")

    def noop(_name: str, _work: Path, _candidate_path: Path):
        pass

    def patch(_name: str, work: Path, _candidate_path: Path):
        (work / "main.py").write_text(_write_success())

    origin, _, _ = try_adoption(
        reference,
        working,
        candidate,
        record,
        stage_actions={"environment": noop, "adapter": noop, "source_patch": patch},
        timeout=5,
    )

    assert origin == "official_patched"
    assert record["source_modifications"] == ["main.py"]
    assert "a/main.py" in record["source_patch"]


def test_all_adoption_attempts_fail_with_fixed_bound(tmp_path):
    reference, working, candidate = _trees(tmp_path, "raise RuntimeError('nope')\n")
    record = new_adoption_record(None, strategy="reuse-first")

    def noop(_name: str, _work: Path, _candidate_path: Path):
        pass

    origin, _, result = try_adoption(
        reference,
        working,
        candidate,
        record,
        stage_actions={"environment": noop, "adapter": noop, "source_patch": noop},
        timeout=5,
    )

    assert origin is None
    assert result is not None and not result.succeeded
    assert [attempt["stage"] for attempt in record["attempts"]] == [
        "official_unmodified",
        "official_environment_fixed",
        "official_adapted",
        "official_patched",
    ]


def test_provenance_and_secret_scrubbing(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "LICENSE").write_text("MIT License\n")
    (source / "main.py").write_text(_write_success())
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(source),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-qm",
            "initial",
        ],
        check=True,
    )

    destination = tmp_path / "immutable"
    provenance = acquire_official_code(str(source), destination, allow_local=True)

    assert provenance is not None
    assert len(provenance.commit_sha) == 40
    assert provenance.license == "MIT"
    assert not (destination / ".git").exists()
    assert destination.stat().st_mode & 0o222 == 0
    assert (destination / "main.py").stat().st_mode & 0o222 == 0
    working = tmp_path / "working"
    make_working_copy(destination, working)
    assert (working / "main.py").stat().st_mode & 0o200
    record = new_adoption_record(provenance, strategy="reuse-first")
    record["attempts"].append(
        {
            "command": ["python", "x.py", "--token", "very-secret"],
            "failure": "Authorization: Bearer abcdefgh",
            "api_key": "sk-1234567890",
        }
    )
    path = tmp_path / "adoption.json"
    write_adoption(path, record)
    persisted = path.read_text()
    assert "very-secret" not in persisted
    assert "abcdefgh" not in persisted
    assert "sk-1234567890" not in persisted
    assert "[REDACTED]" in persisted
    assert scrub({"password": "visible"}) == {"password": "[REDACTED]"}
