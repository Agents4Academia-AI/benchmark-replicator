"""Manifest and stable-runner contract tests."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from replicator.manifest import build_manifest, install_runner, validate_manifest, write_manifest


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "baseline"
    repo.mkdir()
    (repo / ".replicator").mkdir()
    (repo / "paper").mkdir()
    (repo / "paper" / "SOURCE.txt").write_text("https://example.test/paper\n")
    (repo / "impl.py").write_text(
        "import argparse,json\n"
        "p=argparse.ArgumentParser(); p.add_argument('--seed',type=int,default=11); a=p.parse_args()\n"
        "json.dump({'nested':{'score':a.seed > 0}},open('.replicator/raw.json','w'))\n"
    )
    manifest = build_manifest(
        repo,
        method_name="Fake Method",
        origin="reimplemented",
        command=[sys.executable, "impl.py"],
        cwd=".",
        result_path=".replicator/raw.json",
        result_format="json",
        metric_map={"score": "nested.score"},
        supported_overrides={"seed": {"type": "integer", "flag": "--seed", "default": 11}},
        default_seed=11,
        hardware_mode="cpu",
        modifications=["test implementation"],
        provenance=None,
        verification_status="passed",
    )
    write_manifest(repo, manifest)
    install_runner(repo)
    return repo


def test_manifest_validation():
    assert "missing field: method_name" in validate_manifest({"schema_version": "1.0"})
    invalid = {"schema_version": "1.0", "implementation_origin": "invented"}
    assert "implementation_origin is invalid" in validate_manifest(invalid)


def test_stable_runner_default_and_supported_override(tmp_path):
    repo = _repo(tmp_path)
    (repo / "run-spec.json").write_text("{}\n")

    default = subprocess.run(
        ["bash", "run.sh", "--spec", "run-spec.json", "--output", "run-result.json"],
        cwd=repo,
        check=True,
    )
    assert default.returncode == 0
    result = json.loads((repo / "run-result.json").read_text())
    assert result == {
        "implementation_origin": "reimplemented",
        "metrics": {"score": True},
        "runtime_seconds": result["runtime_seconds"],
        "seed": 11,
        "status": "success",
    }
    assert json.loads((repo / ".replicator" / "results.json").read_text()) == {"score": True}

    (repo / "override.json").write_text('{"seed": 0}\n')
    subprocess.run(
        ["bash", "run.sh", "--spec", "override.json", "--output", "override-result.json"],
        cwd=repo,
        check=True,
    )
    overridden = json.loads((repo / "override-result.json").read_text())
    assert overridden["seed"] == 0
    assert overridden["metrics"] == {"score": False}


def test_stable_runner_rejects_unsupported_override(tmp_path):
    repo = _repo(tmp_path)
    (repo / "bad.json").write_text('{"dataset": "ignored"}\n')

    result = subprocess.run(
        ["bash", "run.sh", "--spec", "bad.json", "--output", "bad-result.json"],
        cwd=repo,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "unsupported override" in result.stderr
    assert json.loads((repo / "bad-result.json").read_text())["status"] == "error"
