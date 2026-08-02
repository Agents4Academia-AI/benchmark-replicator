"""Creation and validation of the portable baseline contract."""

from __future__ import annotations

import json
import platform
import re
import shutil
import stat
from pathlib import Path

from .adoption import Candidate, OfficialProvenance

BASELINE_PATH = "baseline.json"
RUNNER_PATH = ".replicator/run.py"
RUN_SCRIPT = "run.sh"
ORIGINS = {
    "official_unmodified",
    "official_environment_fixed",
    "official_adapted",
    "official_patched",
    "reimplemented",
}
_OVERRIDE_TYPES = {"integer", "number", "string", "boolean"}
_SECRET_FLAG = re.compile(
    r"(?:^|[_-])(?:api[_-]?key|authorization|password|secret|token)(?:$|[_-])", re.I
)


def paper_url(repo: Path) -> str:
    try:
        return (repo / "paper" / "SOURCE.txt").read_text().strip()
    except OSError:
        return ""


def environment_info() -> dict:
    return {
        "execution": "local",
        "python": platform.python_version(),
        "platform": platform.platform(),
    }


def build_manifest(
    repo: Path,
    *,
    method_name: str,
    origin: str,
    command: list[str],
    cwd: str,
    result_path: str,
    result_format: str,
    metric_map: dict[str, str],
    supported_overrides: dict[str, dict],
    default_seed: int | None,
    hardware_mode: str,
    modifications: list[str],
    provenance: OfficialProvenance | None,
    verification_status: str = "pending",
) -> dict:
    return {
        "schema_version": "1.0",
        "status": "success" if verification_status == "passed" else "pending",
        "method_name": method_name,
        "implementation_origin": origin,
        "paper_url": paper_url(repo),
        "code_url": provenance.url if provenance else None,
        "commit_sha": provenance.commit_sha if provenance else None,
        "command": "bash run.sh --spec run-spec.json --output run-result.json",
        "result": {"path": "run-result.json", "format": "json"},
        "supported_overrides": supported_overrides,
        "hardware": {"mode": hardware_mode, "execution": "local"},
        "environment": environment_info(),
        "modifications": modifications,
        "verification": {
            "status": verification_status,
            "command": "bash run.sh --spec run-spec.json --output run-result.json",
        },
        "invocation": {
            "command": command,
            "cwd": cwd,
            "result": {"path": result_path, "format": result_format},
            "metric_map": metric_map,
            "default_seed": default_seed,
        },
    }


def validate_manifest(data: object) -> list[str]:
    """Return all contract errors; an empty list means the manifest is valid."""
    errors: list[str] = []
    if not isinstance(data, dict):
        return ["manifest must be a JSON object"]
    required = {
        "schema_version",
        "status",
        "method_name",
        "implementation_origin",
        "paper_url",
        "code_url",
        "commit_sha",
        "command",
        "result",
        "supported_overrides",
        "hardware",
        "environment",
        "modifications",
        "verification",
        "invocation",
    }
    for name in sorted(required - data.keys()):
        errors.append(f"missing field: {name}")
    if data.get("schema_version") != "1.0":
        errors.append("schema_version must be '1.0'")
    if data.get("status") not in {"pending", "success"}:
        errors.append("status must be pending or success")
    if data.get("implementation_origin") not in ORIGINS:
        errors.append("implementation_origin is invalid")
    if not isinstance(data.get("method_name"), str) or not data.get("method_name"):
        errors.append("method_name must be non-empty")
    if not isinstance(data.get("supported_overrides"), dict):
        errors.append("supported_overrides must be an object")
    else:
        for name, definition in data["supported_overrides"].items():
            if not isinstance(name, str) or not name:
                errors.append("supported override names must be non-empty strings")
                continue
            if not isinstance(definition, dict):
                errors.append(f"supported override {name!r} must be an object")
                continue
            if definition.get("type") not in _OVERRIDE_TYPES:
                errors.append(f"supported override {name!r} has an invalid type")
            if not isinstance(definition.get("flag"), str) or not definition.get("flag"):
                errors.append(f"supported override {name!r} must declare a flag")
            if definition.get("type") == "boolean":
                false_flag = definition.get("false_flag")
                if false_flag is not None and not isinstance(false_flag, str):
                    errors.append(f"supported override {name!r} false_flag must be a string")
                if false_flag is None and definition.get("default") is not False:
                    errors.append(
                        f"supported override {name!r} needs false_flag or a false default"
                    )
            if "choices" in definition and not isinstance(definition["choices"], list):
                errors.append(f"supported override {name!r} choices must be an array")
    if not isinstance(data.get("modifications"), list):
        errors.append("modifications must be an array")
    result = data.get("result")
    if not isinstance(result, dict) or result.get("format") != "json" or not result.get("path"):
        errors.append("result must name a JSON path")
    hardware = data.get("hardware")
    if not isinstance(hardware, dict) or hardware.get("execution") != "local":
        errors.append("hardware.execution must record local execution")
    verification = data.get("verification")
    if not isinstance(verification, dict) or verification.get("status") not in {
        "pending",
        "passed",
    }:
        errors.append("verification.status must be pending or passed")
    elif (data.get("status") == "success") != (verification.get("status") == "passed"):
        errors.append("manifest status and verification.status disagree")
    invocation = data.get("invocation")
    if not isinstance(invocation, dict):
        errors.append("invocation must be an object")
    else:
        command = invocation.get("command")
        if (
            not isinstance(command, list)
            or not command
            or not all(isinstance(x, str) for x in command)
        ):
            errors.append("invocation.command must be a non-empty string array")
        elif any(_SECRET_FLAG.search(part) for part in command):
            errors.append("invocation.command must not contain credential flags")
        cwd = invocation.get("cwd")
        if not isinstance(cwd, str) or Path(cwd).is_absolute() or ".." in Path(cwd).parts:
            errors.append("invocation.cwd must be a safe relative path")
        raw_result = invocation.get("result")
        if not isinstance(raw_result, dict) or raw_result.get("format") not in {
            "json",
            "stdout_json",
        }:
            errors.append("invocation.result format must be json or stdout_json")
        elif (
            not isinstance(raw_result.get("path"), str)
            or not raw_result.get("path")
            or Path(raw_result["path"]).is_absolute()
            or ".." in Path(raw_result["path"]).parts
        ):
            errors.append("invocation.result path must be a safe relative path")
        metric_map = invocation.get("metric_map", {})
        if not isinstance(metric_map, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in metric_map.items()
        ):
            errors.append("invocation.metric_map must map strings to strings")
    if data.get("implementation_origin", "").startswith("official_"):
        if not isinstance(data.get("code_url"), str) or not data.get("code_url"):
            errors.append("official origins require code_url")
        if not isinstance(data.get("commit_sha"), str) or not data.get("commit_sha"):
            errors.append("official origins require commit_sha")
    return errors


def write_manifest(repo: Path, manifest: dict) -> None:
    errors = validate_manifest(manifest)
    if errors:
        raise ValueError("invalid baseline manifest: " + "; ".join(errors))
    (repo / BASELINE_PATH).write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")


def install_runner(repo: Path) -> None:
    runner_source = Path(__file__).with_name("runner.py")
    runner_dest = repo / RUNNER_PATH
    runner_dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(runner_source, runner_dest)
    script = repo / RUN_SCRIPT
    script.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'python3 "$(dirname "$0")/.replicator/run.py" "$@"\n'
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def manifest_from_candidate(
    repo: Path,
    candidate: Candidate,
    *,
    origin: str,
    provenance: OfficialProvenance,
    modifications: list[str],
    hardware_mode: str,
    verification_status: str = "pending",
) -> dict:
    return build_manifest(
        repo,
        method_name=candidate.method_name,
        origin=origin,
        command=candidate.command,
        cwd="official",
        result_path=candidate.result_path,
        result_format=candidate.result_format,
        metric_map=candidate.metric_map,
        supported_overrides=candidate.supported_overrides,
        default_seed=candidate.default_seed,
        hardware_mode=hardware_mode,
        modifications=modifications,
        provenance=provenance,
        verification_status=verification_status,
    )


def load_execution_contract(repo: Path) -> dict:
    """Load a scratch implementation contract, preserving old generated run scripts."""
    path = repo / ".replicator" / "execution.json"
    if path.exists():
        data = json.loads(path.read_text())
        if isinstance(data, dict):
            return data
    old_runner = repo / RUN_SCRIPT
    implementation_runner = repo / ".replicator" / "run-implementation.sh"
    if old_runner.exists():
        shutil.copy2(old_runner, implementation_runner)
        implementation_runner.chmod(implementation_runner.stat().st_mode | stat.S_IXUSR)
    else:
        implementation_runner.write_text("#!/usr/bin/env bash\npython3 train.py\n")
        implementation_runner.chmod(implementation_runner.stat().st_mode | stat.S_IXUSR)
    return {
        "schema_version": "1",
        "method_name": "paper baseline",
        "command": ["bash", ".replicator/run-implementation.sh"],
        "result": {"path": ".replicator/results.json", "format": "json"},
        "metric_map": {},
        "supported_overrides": {},
        "default_seed": 0,
    }


def scratch_manifest(
    repo: Path,
    hardware_mode: str,
    verification_status: str = "pending",
    provenance: OfficialProvenance | None = None,
) -> dict:
    contract = load_execution_contract(repo)
    candidate = Candidate(
        method_name=str(contract.get("method_name", "paper baseline")),
        command=list(contract["command"]),
        result_path=contract["result"]["path"],
        result_format=contract["result"]["format"],
        metric_map=dict(contract.get("metric_map", {})),
        supported_overrides=dict(contract.get("supported_overrides", {})),
        default_seed=contract.get("default_seed"),
        adapter_files=(),
    )
    return build_manifest(
        repo,
        method_name=candidate.method_name,
        origin="reimplemented",
        command=candidate.command,
        cwd=".",
        result_path=candidate.result_path,
        result_format=candidate.result_format,
        metric_map=candidate.metric_map,
        supported_overrides=candidate.supported_overrides,
        default_seed=candidate.default_seed,
        hardware_mode=hardware_mode,
        modifications=["Reimplemented from the paper plan."],
        provenance=provenance,
        verification_status=verification_status,
    )
