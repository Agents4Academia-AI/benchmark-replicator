"""Deterministic acquisition of an authors' official implementation.

The model may propose a command or make one bounded class of change, but Python
owns the stage order, executes every candidate locally, checks the change class,
and records a scrubbed audit trail.
"""

from __future__ import annotations

import difflib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, urlunparse

ADOPTION_PATH = ".replicator/adoption.json"
CANDIDATE_PATH = ".replicator/adoption-candidate.json"
REFERENCE_PATH = ".replicator/reference_code"
WORKING_PATH = ".replicator/official_working"

STAGES = (
    ("official_unmodified", None),
    ("official_environment_fixed", "environment"),
    ("official_adapted", "adapter"),
    ("official_patched", "source_patch"),
)

_CLONE_HOSTS = {"github.com", "gitlab.com", "bitbucket.org"}
_ENVIRONMENT_FILES = {
    ".python-version",
    "environment.yml",
    "environment.yaml",
    "package-lock.json",
    "pdm.lock",
    "pipfile",
    "pipfile.lock",
    "poetry.lock",
    "pyproject.toml",
    "requirements.txt",
    "runtime.txt",
    "setup.cfg",
    "setup.py",
    "tox.ini",
    "uv.lock",
}
_IGNORED_PARTS = {
    ".git",
    ".replicator",
    ".venv",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
}
_SECRET_KEY = re.compile(
    r"(?:^|[_-])(?:api[_-]?key|authorization|password|secret|token)(?:$|[_-])", re.I
)
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


@dataclass(frozen=True)
class OfficialProvenance:
    url: str
    commit_sha: str
    license: str
    retrieved_at: str


@dataclass(frozen=True)
class Candidate:
    method_name: str
    setup_command: list[str]
    command: list[str]
    result_path: str
    result_format: str
    metric_map: dict[str, str]
    supported_overrides: dict[str, dict]
    default_seed: int | None
    adapter_files: tuple[str, ...]


@dataclass(frozen=True)
class ExecutionResult:
    succeeded: bool
    command: list[str]
    returncode: int | None
    runtime_seconds: float
    metrics: dict[str, int | float | bool]
    failure: str = ""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_clone_source(url: str, *, allow_local: bool) -> str | None:
    """Return a clone source for known hosts or an existing local fake repository."""
    parsed = urlparse(url)
    if parsed.password or (parsed.scheme in {"http", "https"} and parsed.username):
        return None
    host = parsed.netloc.lower().removeprefix("www.")
    if parsed.scheme in {"http", "https", "ssh", "git"} and host in _CLONE_HOSTS:
        return url
    if allow_local and parsed.scheme == "file" and Path(parsed.path).is_dir():
        return url
    local = Path(url)
    return str(local.resolve()) if allow_local and local.is_dir() else None


def _detect_license(repo: Path) -> str:
    candidates = sorted(
        path
        for path in repo.iterdir()
        if path.is_file() and path.name.lower().startswith(("license", "copying"))
    )
    if not candidates:
        return "unknown"
    text = candidates[0].read_text(errors="replace")[:8000].lower()
    known = (
        ("apache license", "Apache-2.0"),
        ("mit license", "MIT"),
        ("gnu affero general public license", "AGPL"),
        ("gnu general public license", "GPL"),
        ("bsd 3-clause", "BSD-3-Clause"),
        ("redistribution and use in source and binary forms", "BSD"),
    )
    for marker, name in known:
        if marker in text:
            return name
    return f"unknown ({candidates[0].name})"


def _set_tree_writable(root: Path) -> None:
    if not root.exists():
        return
    for path in root.rglob("*"):
        if path.is_symlink():
            continue
        mode = path.stat().st_mode
        if path.is_dir():
            path.chmod(mode | stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
        else:
            path.chmod(mode | stat.S_IRUSR | stat.S_IWUSR)
    root.chmod(root.stat().st_mode | stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)


def _set_tree_read_only(root: Path) -> None:
    for path in root.rglob("*"):
        if path.is_symlink():
            continue
        mode = path.stat().st_mode
        if path.is_dir():
            path.chmod(0o555)
        else:
            path.chmod(0o555 if mode & 0o111 else 0o444)
    root.chmod(0o555)


def remove_tree(root: Path) -> None:
    """Remove a tree created by this module, including read-only reference copies."""
    if root.exists():
        _set_tree_writable(root)
        shutil.rmtree(root)


def acquire_official_code(
    url: str, destination: Path, *, allow_local: bool = False
) -> OfficialProvenance | None:
    """Clone official code once and retain a content-stable reference copy.

    Remote URLs are restricted to established source-code hosts. Tests may explicitly
    enable local paths and ``file://`` URLs with ``allow_local=True``. The git metadata
    is removed after recording HEAD so later phases cannot mutate history.
    """
    source = _safe_clone_source(url, allow_local=allow_local)
    if source is None:
        return None
    if destination.exists():
        remove_tree(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            [
                "git",
                "clone",
                "--quiet",
                "--depth",
                "1",
                "--single-branch",
                "--no-tags",
                source,
                str(destination),
            ],
            check=True,
            timeout=120,
            capture_output=True,
            text=True,
        )
        commit = subprocess.run(
            ["git", "-C", str(destination), "rev-parse", "HEAD"],
            check=True,
            timeout=10,
            capture_output=True,
            text=True,
        ).stdout.strip()
        license_name = _detect_license(destination)
        shutil.rmtree(destination / ".git", ignore_errors=True)
        _set_tree_read_only(destination)
        return OfficialProvenance(
            url=url, commit_sha=commit, license=license_name, retrieved_at=_utc_now()
        )
    except (OSError, subprocess.SubprocessError):
        remove_tree(destination)
        return None


def make_working_copy(reference: Path, working: Path) -> None:
    """Create a disposable working copy without modifying the reference tree."""
    remove_tree(working)
    shutil.copytree(reference, working, symlinks=True)
    _set_tree_writable(working)


def load_candidate(path: Path) -> Candidate:
    """Load and strictly validate the agent's proposed local invocation contract."""
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid adoption candidate: {exc}") from exc
    if not isinstance(data, dict) or data.get("schema_version") != "1":
        raise ValueError("adoption candidate schema_version must be '1'")
    method_name = data.get("method_name")
    setup_command = data.get("setup_command")
    command = data.get("command")
    result = data.get("result")
    if not isinstance(method_name, str) or not method_name.strip():
        raise ValueError("adoption candidate method_name must be non-empty")
    if (
        not isinstance(command, list)
        or not command
        or not all(isinstance(v, str) and v for v in command)
    ):
        raise ValueError("adoption candidate command must be a non-empty string array")
    if not isinstance(setup_command, list) or not all(
        isinstance(value, str) and value for value in setup_command
    ):
        raise ValueError("adoption candidate setup_command must be a string array")
    if not isinstance(result, dict):
        raise ValueError("adoption candidate result must be an object")
    result_path = result.get("path")
    result_format = result.get("format")
    if not isinstance(result_path, str) or not result_path or Path(result_path).is_absolute():
        raise ValueError("adoption candidate result.path must be a relative path")
    if ".." in Path(result_path).parts:
        raise ValueError("adoption candidate result.path must stay in the working copy")
    if result_format not in {"json", "stdout_json"}:
        raise ValueError("adoption candidate result.format must be json or stdout_json")
    metric_map = data.get("metric_map", {})
    overrides = data.get("supported_overrides", {})
    adapters = data.get("adapter_files", [])
    seed = data.get("default_seed")
    if not isinstance(metric_map, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in metric_map.items()
    ):
        raise ValueError("adoption candidate metric_map must map strings to strings")
    if not isinstance(overrides, dict) or not all(
        isinstance(k, str) and isinstance(v, dict) for k, v in overrides.items()
    ):
        raise ValueError("adoption candidate supported_overrides must be an object")
    if not isinstance(adapters, list) or not all(isinstance(v, str) for v in adapters):
        raise ValueError("adoption candidate adapter_files must be a string array")
    if seed is not None and (not isinstance(seed, int) or isinstance(seed, bool)):
        raise ValueError("adoption candidate default_seed must be an integer or null")
    return Candidate(
        method_name=method_name.strip(),
        setup_command=setup_command,
        command=command,
        result_path=result_path,
        result_format=result_format,
        metric_map=metric_map,
        supported_overrides=overrides,
        default_seed=seed,
        adapter_files=tuple(adapters),
    )


def _lookup(data: object, dotted_path: str) -> object:
    value = data
    for part in dotted_path.split(".") if dotted_path else []:
        if not isinstance(value, dict) or part not in value:
            raise KeyError(dotted_path)
        value = value[part]
    return value


def _numeric_metrics(data: object, metric_map: dict[str, str]) -> dict[str, int | float | bool]:
    if not isinstance(data, dict):
        raise ValueError("result is not a JSON object")
    selected = (
        {key: _lookup(data, path) for key, path in metric_map.items()} if metric_map else data
    )
    metrics: dict[str, int | float | bool] = {}
    for key, value in selected.items():
        if not isinstance(key, str) or not isinstance(value, (int, float, bool)):
            raise ValueError(f"metric {key!r} is not numeric or boolean")
        metrics[key] = value
    if not metrics:
        raise ValueError("result contains no numeric or boolean metrics")
    return metrics


def execute_candidate(
    candidate: Candidate,
    working: Path,
    timeout: int = 900,
    *,
    unsafe_local: bool = False,
) -> ExecutionResult:
    """Run a candidate only after an explicit unsafe-local opt-in."""
    import time

    if not unsafe_local:
        return ExecutionResult(
            False,
            candidate.command,
            None,
            0.0,
            {},
            "official code execution is disabled; use --unsafe-local-official-code or a sandbox backend",
        )

    result_file = working / candidate.result_path
    started = time.monotonic()
    try:
        if candidate.setup_command:
            setup = subprocess.run(
                candidate.setup_command,
                cwd=working,
                timeout=timeout,
                capture_output=True,
                text=True,
                env={
                    key: value for key, value in os.environ.items() if not _SECRET_ENV.search(key)
                },
            )
            if setup.returncode:
                detail = setup.stderr.strip() or setup.stdout.strip() or "setup command failed"
                return ExecutionResult(
                    False,
                    candidate.command,
                    setup.returncode,
                    time.monotonic() - started,
                    {},
                    scrub_text(f"setup command failed: {detail[-2000:]}"),
                )
        if candidate.result_format == "json":
            result_file.unlink(missing_ok=True)
        process = subprocess.run(
            candidate.command,
            cwd=working,
            timeout=timeout,
            capture_output=True,
            text=True,
            env={key: value for key, value in os.environ.items() if not _SECRET_ENV.search(key)},
        )
        runtime = time.monotonic() - started
        if process.returncode != 0:
            detail = process.stderr.strip() or process.stdout.strip() or "command failed"
            return ExecutionResult(
                False,
                candidate.command,
                process.returncode,
                runtime,
                {},
                scrub_text(detail[-2000:]),
            )
        raw = (
            json.loads(result_file.read_text())
            if candidate.result_format == "json"
            else json.loads(process.stdout.strip().splitlines()[-1])
        )
        metrics = _numeric_metrics(raw, candidate.metric_map)
        return ExecutionResult(True, candidate.command, process.returncode, runtime, metrics)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError, ValueError, KeyError) as exc:
        return ExecutionResult(
            False,
            candidate.command,
            None,
            time.monotonic() - started,
            {},
            scrub_text(str(exc)),
        )


def _files(root: Path) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for path in root.rglob("*"):
        rel = path.relative_to(root)
        if path.is_symlink() or any(part in _IGNORED_PARTS for part in rel.parts):
            continue
        if path.is_file():
            files[str(rel)] = path.read_bytes()
    return files


def source_changes(reference: Path, working: Path) -> dict[str, str]:
    """Return added/modified/deleted paths between immutable and working copies."""
    before, after = _files(reference), _files(working)
    changes: dict[str, str] = {}
    for path in sorted(before.keys() | after.keys()):
        if path not in before:
            changes[path] = "added"
        elif path not in after:
            changes[path] = "deleted"
        elif before[path] != after[path]:
            changes[path] = "modified"
    return changes


def source_patch(reference: Path, working: Path, exclude: set[str] | None = None) -> str:
    """Render a bounded unified patch for text source changes."""
    before, after = _files(reference), _files(working)
    excluded = exclude or set()
    lines: list[str] = []
    for name in sorted(before.keys() | after.keys()):
        if name in excluded:
            continue
        if before.get(name) == after.get(name):
            continue
        try:
            old = before.get(name, b"").decode()
            new = after.get(name, b"").decode()
        except UnicodeDecodeError:
            lines.append(f"Binary file changed: {name}\n")
            continue
        lines.extend(
            difflib.unified_diff(
                old.splitlines(keepends=True),
                new.splitlines(keepends=True),
                fromfile=f"a/{name}",
                tofile=f"b/{name}",
            )
        )
    return "".join(lines)


def is_environment_path(path: str) -> bool:
    item = Path(path)
    name = item.name.lower()
    return name in _ENVIRONMENT_FILES or name.startswith("requirements") or "dockerfile" in name


def validate_stage_changes(
    stage: str, reference: Path, working: Path, candidate: Candidate
) -> tuple[bool, str]:
    """Enforce the permitted mutation class and a small source-patch budget."""
    changes = source_changes(reference, working)
    generated = {candidate.result_path, *candidate.adapter_files}
    relevant = {p: kind for p, kind in changes.items() if p not in generated}
    if stage == "official_unmodified":
        invalid = relevant
    elif stage == "official_environment_fixed":
        invalid = {p: kind for p, kind in relevant.items() if not is_environment_path(p)}
    elif stage == "official_adapted":
        invalid = {
            p: kind
            for p, kind in relevant.items()
            if not is_environment_path(p) and not (kind == "added" and p in candidate.adapter_files)
        }
    else:
        invalid = {}
        excluded = {p for p in changes if is_environment_path(p) or p in generated}
        patch = source_patch(reference, working, excluded)
        changed_source = [p for p in relevant if not is_environment_path(p)]
        if len(changed_source) > 5 or len(patch.splitlines()) > 400:
            return False, "source patch exceeds the 5-file/400-line adoption limit"
    if invalid:
        paths = ", ".join(f"{path} ({kind})" for path, kind in sorted(invalid.items()))
        return False, f"stage made disallowed changes: {paths}"
    return True, ""


def scrub_text(value: str) -> str:
    """Remove common credentials from persisted provenance text."""
    value = _SECRET_VALUE.sub(lambda m: (m.group(1) or m.group(3) or "") + "[REDACTED]", value)
    value = _URL_CREDENTIALS.sub(r"\1[REDACTED]@", value)
    parsed = urlparse(value)
    if parsed.scheme and parsed.hostname and (parsed.username or parsed.password):
        host = parsed.hostname + (f":{parsed.port}" if parsed.port else "")
        value = urlunparse(parsed._replace(netloc=host))
    return value


def scrub_command(command: list[str]) -> list[str]:
    cleaned: list[str] = []
    hide_next = False
    for value in command:
        if hide_next:
            cleaned.append("[REDACTED]")
            hide_next = False
            continue
        cleaned.append(scrub_text(value))
        hide_next = bool(_SECRET_KEY.search(value)) and "=" not in value
    return cleaned


def scrub(value: object, key: str = "") -> object:
    if _SECRET_KEY.search(key):
        return "[REDACTED]"
    if key == "command" and isinstance(value, list) and all(isinstance(v, str) for v in value):
        return scrub_command(value)
    if isinstance(value, dict):
        return {str(k): scrub(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub(v) for v in value]
    return scrub_text(value) if isinstance(value, str) else value


def new_adoption_record(
    provenance: OfficialProvenance | None, *, strategy: str, execution_local: bool = False
) -> dict:
    return {
        "schema_version": "1",
        "strategy": strategy,
        "execution_local": execution_local,
        "official_code": (
            {
                "url": provenance.url,
                "commit_sha": provenance.commit_sha,
                "license": provenance.license,
                "retrieved_at": provenance.retrieved_at,
            }
            if provenance
            else None
        ),
        "attempts": [],
        "environment_changes": [],
        "adapters": [],
        "source_modifications": [],
        "source_patch": "",
        "failures": [],
        "selected_origin": None,
    }


def write_adoption(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(scrub(record), indent=2, sort_keys=True) + "\n")


StageAction = Callable[[str, Path, Path], None]


def try_adoption(
    reference: Path,
    working: Path,
    candidate_path: Path,
    record: dict,
    *,
    stage_actions: dict[str, StageAction] | None = None,
    timeout: int = 900,
    unsafe_local: bool = False,
) -> tuple[str | None, Candidate | None, ExecutionResult | None]:
    """Try the four adoption levels exactly once each, in fixed order."""
    actions = stage_actions or {}
    last_result: ExecutionResult | None = None
    for origin, action_name in STAGES:
        if action_name:
            action = actions.get(action_name)
            if action is None:
                record["attempts"].append(
                    {"stage": origin, "status": "skipped", "failure": "no stage action available"}
                )
                continue
            with tempfile.TemporaryDirectory(prefix="replicator-stage-") as temp:
                backup = Path(temp) / "working"
                shutil.copytree(working, backup, symlinks=True)
                try:
                    action(action_name, working, candidate_path)
                    candidate = load_candidate(candidate_path)
                    allowed, detail = validate_stage_changes(origin, reference, working, candidate)
                except Exception as exc:
                    allowed, detail = False, scrub_text(str(exc))
                if not allowed:
                    remove_tree(working)
                    shutil.copytree(backup, working, symlinks=True)
                    record["attempts"].append(
                        {"stage": origin, "status": "failed", "failure": detail}
                    )
                    record["failures"].append(detail)
                    continue
        try:
            candidate = load_candidate(candidate_path)
        except ValueError as exc:
            detail = scrub_text(str(exc))
            record["attempts"].append({"stage": origin, "status": "failed", "failure": detail})
            record["failures"].append(detail)
            continue
        allowed, detail = validate_stage_changes(origin, reference, working, candidate)
        if not allowed:
            record["attempts"].append({"stage": origin, "status": "failed", "failure": detail})
            record["failures"].append(detail)
            continue
        last_result = execute_candidate(candidate, working, timeout, unsafe_local=unsafe_local)
        attempt = {
            "stage": origin,
            "status": "succeeded" if last_result.succeeded else "failed",
            "setup_command": scrub_command(candidate.setup_command),
            "command": scrub_command(last_result.command),
            "returncode": last_result.returncode,
            "runtime_seconds": round(last_result.runtime_seconds, 3),
        }
        if last_result.failure:
            attempt["failure"] = last_result.failure
            record["failures"].append(last_result.failure)
        record["attempts"].append(attempt)
        if last_result.succeeded:
            changes = source_changes(reference, working)
            record["environment_changes"] = sorted(p for p in changes if is_environment_path(p))
            record["adapters"] = sorted(p for p in candidate.adapter_files if p in changes)
            record["source_modifications"] = sorted(
                p
                for p in changes
                if not is_environment_path(p)
                and p not in candidate.adapter_files
                and p != candidate.result_path
            )
            record["source_patch"] = source_patch(reference, working, {candidate.result_path})
            record["selected_origin"] = origin
            return origin, candidate, last_result
    return None, None, last_result
