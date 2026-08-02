"""Offline pipeline fallback tests with fake agents and fake git repositories."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

from replicator.adoption import acquire_official_code
from replicator.agent import PhaseUsage
from replicator.pipeline import run_pipeline


def _fake_git_repo(tmp_path: Path, *, succeeds: bool) -> Path:
    source = tmp_path / "official-source"
    source.mkdir()
    body = (
        "import json\njson.dump({'score': 1}, open('metrics.json', 'w'))\n"
        if succeeds
        else "raise RuntimeError('official failure')\n"
    )
    (source / "main.py").write_text(body)
    (source / "LICENSE").write_text("MIT License\n")
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
    return source


def _prepare_output(tmp_path: Path) -> Path:
    repo = tmp_path / "output"
    (repo / "paper").mkdir(parents=True)
    (repo / "paper" / "SOURCE.txt").write_text("https://example.test/paper\n")
    return repo


def _allow_fake_clone(monkeypatch):
    monkeypatch.setattr(
        "replicator.pipeline.acquire_official_code",
        lambda url, destination: acquire_official_code(url, destination, allow_local=True),
    )


def _agent(
    code_url: str | None,
    calls: list[str],
    *,
    fail_adopted_verification: bool = False,
):
    async def fake_run_agent(phase, repo: Path, *_args, **_kwargs):
        calls.append(phase.name)
        control = repo / ".replicator"
        control.mkdir(parents=True, exist_ok=True)
        if phase.name == "planner":
            (repo / "PLAN.md").write_text("# Plan\n\nNo implementation phase headings.\n")
            (control / "criteria.json").write_text(
                '{"criteria":[{"id":"score","metric":"score",'
                '"comparison":">=","threshold":1,"required":true}]}\n'
            )
            (control / "artifacts.json").write_text(json.dumps({"code_url": code_url}) + "\n")
        elif phase.name == "adoption_inspector":
            (control / "adoption-candidate.json").write_text(
                json.dumps(
                    {
                        "schema_version": "1",
                        "method_name": "Official Fake",
                        "command": [sys.executable, "main.py"],
                        "result": {"path": "metrics.json", "format": "json"},
                        "metric_map": {"score": "score"},
                        "supported_overrides": {},
                        "default_seed": 3,
                        "adapter_files": [],
                    }
                )
                + "\n"
            )
        elif phase.name == "coder":
            (repo / "scratch.py").write_text(
                "import json\njson.dump({'score': 1}, open('.replicator/scratch-raw.json', 'w'))\n"
            )
            (control / "execution.json").write_text(
                json.dumps(
                    {
                        "schema_version": "1",
                        "method_name": "Scratch Fake",
                        "command": [sys.executable, "scratch.py"],
                        "result": {"path": ".replicator/scratch-raw.json", "format": "json"},
                        "metric_map": {"score": "score"},
                        "supported_overrides": {},
                        "default_seed": 5,
                    }
                )
                + "\n"
            )
            (repo / "run.sh").write_text("#!/bin/sh\npython scratch.py\n")
        elif phase.name == "tester":
            adopted = "adoption_verification" in repo.parts
            if adopted:
                tests = repo / "tests"
                tests.mkdir(exist_ok=True)
                (tests / "adoption_only.py").write_text("CONTAMINATION = True\n")
            status = "fail" if adopted and fail_adopted_verification else "pass"
            failures = (
                [{"criterion": "forced", "detail": "forced adoption failure"}]
                if status == "fail"
                else []
            )
            (control / "verdict.json").write_text(
                json.dumps({"phase": "tester", "status": status, "failures": failures}) + "\n"
            )
        elif phase.name == "benchmarker":
            subprocess.run(
                ["bash", "run.sh", "--spec", "run-spec.json", "--output", "run-result.json"],
                cwd=repo,
                check=True,
            )
            (repo / "REPORT.md").write_text("verified\n")
            (repo / "EVAL.md").write_text("score: 1\n")
            (control / "verdict.json").write_text(
                '{"phase":"benchmarker","status":"pass","failures":[]}\n'
            )
        elif phase.name == "cleaner":
            (repo / "README.md").write_text("fake baseline\n")
        return PhaseUsage(phase.name)

    return fake_run_agent


def test_no_official_code_uses_scratch_fallback(monkeypatch, tmp_path):
    repo = _prepare_output(tmp_path)
    calls: list[str] = []
    monkeypatch.setattr("replicator.pipeline.run_agent", _agent(None, calls))

    asyncio.run(run_pipeline(repo, auto_approve=True))

    manifest = json.loads((repo / "baseline.json").read_text())
    assert manifest["implementation_origin"] == "reimplemented"
    assert "adoption_inspector" not in calls
    assert (repo / "scratch.py").exists()


def test_official_code_is_promoted_when_verification_passes(monkeypatch, tmp_path):
    source = _fake_git_repo(tmp_path, succeeds=True)
    repo = _prepare_output(tmp_path)
    calls: list[str] = []
    _allow_fake_clone(monkeypatch)
    monkeypatch.setattr("replicator.pipeline.run_agent", _agent(str(source), calls))

    asyncio.run(run_pipeline(repo, auto_approve=True))

    manifest = json.loads((repo / "baseline.json").read_text())
    adoption = json.loads((repo / ".replicator" / "adoption.json").read_text())
    assert manifest["implementation_origin"] == "official_unmodified"
    assert manifest["commit_sha"] == adoption["official_code"]["commit_sha"]
    assert adoption["selected_origin"] == "official_unmodified"
    assert (repo / "official" / "main.py").exists()
    assert "coder" not in calls


def test_all_adoption_attempts_fail_then_clean_scratch_fallback(monkeypatch, tmp_path):
    source = _fake_git_repo(tmp_path, succeeds=False)
    repo = _prepare_output(tmp_path)
    calls: list[str] = []
    _allow_fake_clone(monkeypatch)
    monkeypatch.setattr("replicator.pipeline.run_agent", _agent(str(source), calls))

    asyncio.run(run_pipeline(repo, auto_approve=True))

    adoption = json.loads((repo / ".replicator" / "adoption.json").read_text())
    assert [attempt["stage"] for attempt in adoption["attempts"]] == [
        "official_unmodified",
        "official_environment_fixed",
        "official_adapted",
        "official_patched",
    ]
    assert adoption["selected_origin"] == "reimplemented"
    assert not (repo / ".replicator" / "official_working").exists()
    assert not (repo / "official").exists()
    assert (repo / "scratch.py").exists()


def test_adopted_verification_failure_falls_back_without_contamination(monkeypatch, tmp_path):
    source = _fake_git_repo(tmp_path, succeeds=True)
    repo = _prepare_output(tmp_path)
    calls: list[str] = []
    _allow_fake_clone(monkeypatch)
    monkeypatch.setattr(
        "replicator.pipeline.run_agent",
        _agent(str(source), calls, fail_adopted_verification=True),
    )

    asyncio.run(run_pipeline(repo, auto_approve=True))

    manifest = json.loads((repo / "baseline.json").read_text())
    adoption = json.loads((repo / ".replicator" / "adoption.json").read_text())
    assert manifest["implementation_origin"] == "reimplemented"
    assert adoption["selected_origin"] == "reimplemented"
    assert not (repo / "official").exists()
    assert not (repo / "tests" / "adoption_only.py").exists()
    assert not (repo / ".replicator" / "adoption_verification").exists()


def test_strategy_scratch_skips_adoption(monkeypatch, tmp_path):
    source = _fake_git_repo(tmp_path, succeeds=True)
    repo = _prepare_output(tmp_path)
    calls: list[str] = []
    _allow_fake_clone(monkeypatch)
    monkeypatch.setattr("replicator.pipeline.run_agent", _agent(str(source), calls))

    asyncio.run(run_pipeline(repo, auto_approve=True, strategy="scratch"))

    assert "adoption_inspector" not in calls
    assert "environment_fixer" not in calls
    assert (
        json.loads((repo / "baseline.json").read_text())["implementation_origin"] == "reimplemented"
    )
