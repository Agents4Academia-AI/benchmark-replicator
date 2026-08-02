"""Self-contained stable runner installed into generated baseline repositories."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
import time
from pathlib import Path

_SECRET_VALUE = re.compile(
    r"(?i)(bearer\s+)[^\s]+|\b(sk-[A-Za-z0-9_-]{8,})\b|"
    r"((?:api[_-]?key|password|secret|token)\s*[=:]\s*)[^\s,;]+"
)
_URL_CREDENTIALS = re.compile(r"(https?://)[^/@\s]+@", re.I)
_SECRET_ENV = re.compile(
    r"(?:token|secret|password|api[_-]?key|authorization|credential|cookie|"
    r"^aws_|^openai_|^anthropic_|^github_|^gitlab_|^huggingface_|^hf_)",
    re.I,
)


def _scrub_error(value: str) -> str:
    value = _SECRET_VALUE.sub(
        lambda match: (match.group(1) or match.group(3) or "") + "[REDACTED]", value
    )
    return _URL_CREDENTIALS.sub(r"\1[REDACTED]@", value)


def _load_object(path: Path, label: str) -> dict:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is not readable JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _lookup(value: object, dotted_path: str) -> object:
    for part in dotted_path.split(".") if dotted_path else []:
        if not isinstance(value, dict) or part not in value:
            raise ValueError(f"result has no value at {dotted_path!r}")
        value = value[part]
    return value


def _inside(repo: Path, path: Path, label: str) -> Path:
    resolved = path.resolve()
    root = repo.resolve()
    if root not in (resolved, *resolved.parents):
        raise ValueError(f"{label} escapes the baseline repository")
    return resolved


def _check_override(name: str, value: object, definition: dict) -> None:
    expected = definition.get("type")
    valid = {
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "string": isinstance(value, str),
        "boolean": isinstance(value, bool),
    }.get(expected, False)
    if not valid:
        raise ValueError(f"override {name!r} must have type {expected!r}")
    choices = definition.get("choices")
    if choices is not None and value not in choices:
        raise ValueError(f"override {name!r} must be one of {choices!r}")


def _command(manifest: dict, spec: dict) -> list[str]:
    invocation = manifest["invocation"]
    command = list(invocation["command"])
    supported = manifest["supported_overrides"]
    unknown = sorted(set(spec) - set(supported))
    if unknown:
        raise ValueError(
            "unsupported override(s): "
            + ", ".join(unknown)
            + "; supported: "
            + (", ".join(sorted(supported)) if supported else "none")
        )
    for name, value in spec.items():
        definition = supported[name]
        _check_override(name, value, definition)
        flag = definition.get("flag")
        if not isinstance(flag, str) or not flag:
            raise ValueError(f"override {name!r} has no invocation flag")
        if definition["type"] == "boolean":
            if value:
                command.append(flag)
            elif definition.get("false_flag"):
                command.append(definition["false_flag"])
            elif definition.get("default") is not False:
                raise ValueError(
                    f"override {name!r}=false cannot be applied; no false_flag or false default"
                )
        else:
            command.extend([flag, str(value)])
    return command


def _metrics(raw: object, metric_map: dict[str, str]) -> dict[str, int | float | bool]:
    if not isinstance(raw, dict):
        raise ValueError("implementation result must be a JSON object")
    selected = (
        {name: _lookup(raw, path) for name, path in metric_map.items()} if metric_map else raw
    )
    metrics: dict[str, int | float | bool] = {}
    for name, value in selected.items():
        if not isinstance(name, str) or not isinstance(value, (int, float, bool)):
            raise ValueError(f"metric {name!r} is not numeric or boolean")
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError(f"metric {name!r} is not finite")
        metrics[name] = value
    if not metrics:
        raise ValueError("implementation produced no numeric or boolean metrics")
    return metrics


def run(repo: Path, spec_path: Path, output_path: Path) -> int:
    manifest: dict = {}
    spec: dict = {}
    try:
        manifest = _load_object(repo / "baseline.json", "baseline.json")
        spec_path = _inside(repo, spec_path, "run spec path")
        output_path = _inside(repo, output_path, "output path")
        spec = _load_object(spec_path, "run spec")
        command = _command(manifest, spec)
        invocation = manifest["invocation"]
        cwd = _inside(repo, repo / invocation["cwd"], "invocation working directory")
        result = invocation["result"]
        result_path = _inside(repo, cwd / result["path"], "implementation result path")
        if result["format"] == "json":
            result_path.unlink(missing_ok=True)
        started = time.monotonic()
        process = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            env={key: value for key, value in os.environ.items() if not _SECRET_ENV.search(key)},
        )
        runtime = time.monotonic() - started
        if process.returncode:
            detail = (process.stderr.strip() or process.stdout.strip() or "command failed")[-2000:]
            raise RuntimeError(f"implementation command failed ({process.returncode}): {detail}")
        raw = (
            json.loads(result_path.read_text())
            if result["format"] == "json"
            else json.loads(process.stdout.strip().splitlines()[-1])
        )
        metrics = _metrics(raw, invocation.get("metric_map", {}))
        seed = spec.get("seed", invocation.get("default_seed"))
        normalized = {
            "status": "success",
            "seed": seed,
            "metrics": metrics,
            "runtime_seconds": round(runtime, 6),
            "implementation_origin": manifest["implementation_origin"],
        }
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(normalized, indent=2, sort_keys=True) + "\n")
        compatibility = repo / ".replicator" / "results.json"
        compatibility.parent.mkdir(parents=True, exist_ok=True)
        compatibility.write_text(json.dumps(metrics, indent=2, sort_keys=True) + "\n")
        return 0
    except Exception as exc:
        # If the requested output itself escaped, report on stderr without writing there.
        try:
            output_path = _inside(repo, output_path, "output path")
        except ValueError:
            print(f"run.sh: {_scrub_error(str(exc))}", file=sys.stderr)
            return 2
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(
                {
                    "status": "error",
                    "seed": spec.get("seed"),
                    "metrics": {},
                    "runtime_seconds": 0.0,
                    "implementation_origin": manifest.get("implementation_origin", "unknown"),
                    "error": _scrub_error(str(exc)),
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
        print(f"run.sh: {_scrub_error(str(exc))}", file=sys.stderr)
        return 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    repo = Path(__file__).resolve().parents[1]
    return run(repo, args.spec.resolve(), args.output.resolve())


if __name__ == "__main__":
    raise SystemExit(main())
