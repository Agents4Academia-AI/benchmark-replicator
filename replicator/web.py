"""Local web interface for running the benchmark replicator.

This is intentionally small and dependency-free: it wraps the existing CLI in a
background process, streams stdout/stderr to the browser, and lets the user send
checkpoint input back to the process. It is a local-first interface, not a
multi-tenant hosted service.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import posixpath
import shutil
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 8765
_JOB_DIR = ".replicator-web/jobs"
_MAX_TEXT_BYTES = 2_000_000
_TREE_LIMIT = 500
_TERMINAL_STATUSES = {"completed", "failed", "canceled", "interrupted", "archived"}
_REQUIRED_MODULES = {
    "claude_agent_sdk": "claude-agent-sdk",
    "pymupdf": "pymupdf",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json_dumps(data: Any) -> bytes:
    return json.dumps(data, ensure_ascii=False).encode("utf-8")


def _environment_status() -> dict[str, Any]:
    """Report whether this Python interpreter can run the existing CLI."""
    missing = [
        {"module": module, "package": package}
        for module, package in _REQUIRED_MODULES.items()
        if importlib.util.find_spec(module) is None
    ]
    warnings: list[str] = []
    if sys.version_info < (3, 14):
        warnings.append(
            f"pyproject.toml declares Python >=3.14; this server is running Python {sys.version.split()[0]}."
        )
    install_hint = (
        "Run `uv sync` and start the UI with `uv run replicator-web`, or install "
        f"the project into this interpreter: `{sys.executable} -m pip install -e .`"
    )
    return {
        "ok": not missing,
        "python": sys.version.split()[0],
        "executable": sys.executable,
        "missing": missing,
        "warnings": warnings,
        "install_hint": install_hint,
    }


@dataclass
class Job:
    """One local pipeline process managed by the web server."""

    id: str
    source: str
    workdir: Path
    repo: Path
    command: list[str]
    created_at: str
    instructions: str = ""
    model: str = ""
    gpu: bool = False
    auto_approve: bool = False
    status: str = "queued"
    exit_code: int | None = None
    finished_at: str | None = None
    process: subprocess.Popen[str] | None = field(default=None, repr=False)
    log_chunks: list[str] = field(default_factory=list, repr=False)
    condition: threading.Condition = field(default_factory=threading.Condition, repr=False)
    stdin_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    recent_output: str = field(default="", repr=False)
    canceled: bool = False

    def append_log(self, chunk: str) -> None:
        with self.condition:
            self.log_chunks.append(chunk)
            self.recent_output = (self.recent_output + chunk)[-4000:]
            if self.status not in _TERMINAL_STATUSES:
                if "Approve plan and continue?" in self.recent_output:
                    self.status = "awaiting_approval"
                elif "you \u203a" in self.recent_output:
                    self.status = "revising_plan"
            self.condition.notify_all()

    def set_status(self, status: str) -> None:
        with self.condition:
            self.status = status
            self.recent_output = ""
            self.condition.notify_all()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source": self.source,
            "repo": str(self.repo),
            "command": self.command,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "status": self.status,
            "exit_code": self.exit_code,
            "instructions": self.instructions,
            "model": self.model,
            "gpu": self.gpu,
            "auto_approve": self.auto_approve,
        }


class JobManager:
    """In-memory job registry for the local server."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.jobs_dir = root / _JOB_DIR
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.jobs: dict[str, Job] = {}
        self.lock = threading.Lock()
        self._load_jobs()

    def _job_metadata_path(self, workdir: Path) -> Path:
        return workdir / "job.json"

    def _save_job(self, job: Job) -> None:
        path = self._job_metadata_path(job.workdir)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(job.to_dict(), indent=2) + "\n", encoding="utf-8")

    def _load_jobs(self) -> None:
        for workdir in sorted(self.jobs_dir.iterdir()):
            if not workdir.is_dir():
                continue
            path = self._job_metadata_path(workdir)
            if path.exists():
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except OSError, json.JSONDecodeError:
                    continue
                status = str(data.get("status", "archived"))
                if status not in _TERMINAL_STATUSES:
                    status = "interrupted"
                job = Job(
                    id=str(data.get("id", workdir.name)),
                    source=str(data.get("source", "(unknown source)")),
                    workdir=workdir,
                    repo=Path(str(data.get("repo", workdir / "repo"))),
                    command=list(data.get("command", [])),
                    created_at=str(data.get("created_at", _utc_now())),
                    instructions=str(data.get("instructions", "")),
                    model=str(data.get("model", "")),
                    gpu=bool(data.get("gpu", False)),
                    auto_approve=bool(data.get("auto_approve", False)),
                    status=status,
                    exit_code=data.get("exit_code"),
                    finished_at=data.get("finished_at"),
                )
            else:
                repo = workdir / "repo"
                source_file = repo / "paper" / "SOURCE.txt"
                source = "(unknown source)"
                if source_file.exists():
                    try:
                        source = source_file.read_text(encoding="utf-8").strip()
                    except OSError:
                        pass
                job = Job(
                    id=workdir.name,
                    source=source or "(unknown source)",
                    workdir=workdir,
                    repo=repo,
                    command=[],
                    created_at=datetime.fromtimestamp(workdir.stat().st_mtime, timezone.utc).isoformat(
                        timespec="seconds"
                    ),
                    status="archived",
                )
            self.jobs[job.id] = job

    def create_job(self, payload: dict[str, Any]) -> Job:
        env = _environment_status()
        if not env["ok"]:
            missing = ", ".join(item["package"] for item in env["missing"])
            raise ValueError(f"Missing required Python packages: {missing}. {env['install_hint']}")

        source = str(payload.get("source", "")).strip()
        if not source:
            raise ValueError("source is required")

        job_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        job_id = f"{job_id}-{uuid.uuid4().hex[:8]}"
        workdir = self.jobs_dir / job_id
        repo = workdir / "repo"
        workdir.mkdir(parents=True, exist_ok=False)

        instructions = str(payload.get("instructions", "")).strip()
        model = str(payload.get("model", "")).strip()
        gpu = bool(payload.get("gpu", False))
        auto_approve = bool(payload.get("auto_approve", False))

        command = [
            sys.executable,
            "-m",
            "replicator.cli",
            source,
            "--out",
            str(repo),
        ]
        if instructions:
            command.extend(["--instructions", instructions])
        if model:
            command.extend(["--model", model])
        if gpu:
            command.append("--gpu")
        if auto_approve:
            command.append("--yes")

        job = Job(
            id=job_id,
            source=source,
            workdir=workdir,
            repo=repo,
            command=command,
            created_at=_utc_now(),
            instructions=instructions,
            model=model,
            gpu=gpu,
            auto_approve=auto_approve,
        )
        with self.lock:
            self.jobs[job_id] = job
            self._save_job(job)
        thread = threading.Thread(target=self._run_job, args=(job,), daemon=True)
        thread.start()
        return job

    def list_jobs(self) -> list[Job]:
        with self.lock:
            return sorted(self.jobs.values(), key=lambda job: job.created_at, reverse=True)

    def get_job(self, job_id: str) -> Job:
        with self.lock:
            job = self.jobs.get(job_id)
        if job is None:
            raise KeyError(job_id)
        return job

    def send_input(self, job_id: str, text: str, status: str | None = None) -> None:
        job = self.get_job(job_id)
        process = job.process
        if process is None or process.poll() is not None or process.stdin is None:
            raise RuntimeError("job is not accepting input")
        with job.stdin_lock:
            process.stdin.write(text)
            process.stdin.flush()
        if status:
            job.set_status(status)

    def cancel(self, job_id: str) -> None:
        job = self.get_job(job_id)
        job.canceled = True
        process = job.process
        if process is not None and process.poll() is None:
            process.terminate()
        job.set_status("canceled")
        self._save_job(job)

    def _run_job(self, job: Job) -> None:
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        job.set_status("running")
        try:
            process = subprocess.Popen(
                job.command,
                cwd=str(self.root),
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=0,
            )
            job.process = process
            assert process.stdout is not None
            buffer = ""
            while True:
                chunk = process.stdout.read(1)
                if chunk:
                    buffer += chunk
                    if (
                        chunk == "\n"
                        or "Approve plan and continue?" in buffer
                        or "you \u203a" in buffer
                        or len(buffer) >= 256
                    ):
                        job.append_log(buffer)
                        buffer = ""
                    continue
                if process.poll() is not None:
                    break
                time.sleep(0.02)
            if buffer:
                job.append_log(buffer)

            exit_code = process.wait()
            with job.condition:
                job.exit_code = exit_code
                job.finished_at = _utc_now()
                if job.canceled:
                    job.status = "canceled"
                elif exit_code == 0:
                    job.status = "completed"
                else:
                    job.status = "failed"
                self._save_job(job)
                job.condition.notify_all()
        except Exception as exc:
            job.append_log(f"\n[web] failed to run job: {exc}\n")
            with job.condition:
                job.exit_code = -1
                job.finished_at = _utc_now()
                job.status = "failed"
                self._save_job(job)
                job.condition.notify_all()


class ReplicatorRequestHandler(BaseHTTPRequestHandler):
    """HTTP routes for the local web app."""

    manager: JobManager

    server_version = "ReplicatorWeb/0.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write(f"[web] {self.address_string()} - {fmt % args}\n")

    def do_GET(self) -> None:  # noqa: N802 - stdlib hook name
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/":
            self._send_html(_INDEX_HTML)
            return
        if path == "/api/jobs":
            self._send_json({"jobs": [job.to_dict() for job in self.manager.list_jobs()]})
            return
        if path == "/api/environment":
            self._send_json(_environment_status())
            return
        parts = [unquote(part) for part in path.split("/") if part]
        if len(parts) >= 3 and parts[:2] == ["api", "jobs"]:
            job_id = parts[2]
            if len(parts) == 3:
                self._send_job(job_id)
                return
            route = parts[3]
            if route == "events":
                self._send_events(job_id)
                return
            if route == "file":
                query = parse_qs(parsed.query)
                rel = query.get("path", ["PLAN.md"])[0]
                self._send_file(job_id, rel)
                return
            if route == "tree":
                self._send_tree(job_id)
                return
            if route == "download":
                self._send_download(job_id)
                return
        self._send_error(HTTPStatus.NOT_FOUND, "not found")

    def do_HEAD(self) -> None:  # noqa: N802 - stdlib hook name
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            return
        self.send_response(HTTPStatus.NOT_FOUND)
        self.end_headers()

    def do_POST(self) -> None:  # noqa: N802 - stdlib hook name
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/api/jobs":
            try:
                payload = self._read_json()
                job = self.manager.create_job(payload)
            except Exception as exc:
                self._send_error(HTTPStatus.BAD_REQUEST, str(exc))
                return
            self._send_json({"job": job.to_dict()}, status=HTTPStatus.CREATED)
            return

        parts = [unquote(part) for part in path.split("/") if part]
        if len(parts) == 4 and parts[:2] == ["api", "jobs"]:
            job_id = parts[2]
            action = parts[3]
            try:
                if action == "approve":
                    self.manager.send_input(job_id, "y\n", "running")
                elif action == "reject":
                    self.manager.send_input(job_id, "n\n", "running")
                elif action == "revise":
                    self.manager.send_input(job_id, "c\n", "revising_plan")
                elif action == "message":
                    payload = self._read_json()
                    text = str(payload.get("text", "")).strip()
                    if not text:
                        raise ValueError("message text is required")
                    self.manager.send_input(job_id, text + "\n", "revising_plan")
                elif action == "done":
                    self.manager.send_input(job_id, "done\n", "running")
                elif action == "cancel":
                    self.manager.cancel(job_id)
                else:
                    self._send_error(HTTPStatus.NOT_FOUND, "not found")
                    return
            except Exception as exc:
                self._send_error(HTTPStatus.BAD_REQUEST, str(exc))
                return
            self._send_json({"job": self.manager.get_job(job_id).to_dict()})
            return
        self._send_error(HTTPStatus.NOT_FOUND, "not found")

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        data = self.rfile.read(length)
        payload = json.loads(data.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("JSON body must be an object")
        return payload

    def _send_html(self, text: str) -> None:
        body = text.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, data: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = _json_dumps(data)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, status: HTTPStatus, message: str) -> None:
        self._send_json({"error": message}, status=status)

    def _send_job(self, job_id: str) -> None:
        try:
            job = self.manager.get_job(job_id)
        except KeyError:
            self._send_error(HTTPStatus.NOT_FOUND, "job not found")
            return
        self._send_json({"job": job.to_dict()})

    def _send_events(self, job_id: str) -> None:
        try:
            job = self.manager.get_job(job_id)
        except KeyError:
            self._send_error(HTTPStatus.NOT_FOUND, "job not found")
            return

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        index = 0
        while True:
            with job.condition:
                while index >= len(job.log_chunks) and job.status not in _TERMINAL_STATUSES:
                    job.condition.wait(timeout=15)
                    if not self._write_sse({"type": "status", "job": job.to_dict()}):
                        return
                while index < len(job.log_chunks):
                    chunk = job.log_chunks[index]
                    index += 1
                    if not self._write_sse({"type": "log", "chunk": chunk, "job": job.to_dict()}):
                        return
                if job.status in _TERMINAL_STATUSES:
                    self._write_sse({"type": "status", "job": job.to_dict()})
                    return

    def _write_sse(self, data: Any) -> bool:
        payload = _json_dumps(data).decode("utf-8")
        try:
            self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
            self.wfile.flush()
            return True
        except BrokenPipeError, ConnectionResetError:
            return False

    def _safe_path(self, job: Job, rel: str) -> Path:
        rel = posixpath.normpath("/" + rel).lstrip("/")
        target = (job.repo / rel).resolve()
        root = job.repo.resolve()
        if not target.is_relative_to(root):
            raise ValueError("path escapes the job repo")
        return target

    def _send_file(self, job_id: str, rel: str) -> None:
        try:
            job = self.manager.get_job(job_id)
            target = self._safe_path(job, rel)
        except KeyError:
            self._send_error(HTTPStatus.NOT_FOUND, "job not found")
            return
        except ValueError as exc:
            self._send_error(HTTPStatus.BAD_REQUEST, str(exc))
            return

        if not target.exists():
            self._send_json({"path": rel, "exists": False, "content": ""})
            return
        if target.is_dir():
            self._send_error(HTTPStatus.BAD_REQUEST, "path is a directory")
            return
        raw = target.read_bytes()
        truncated = False
        if len(raw) > _MAX_TEXT_BYTES:
            raw = raw[:_MAX_TEXT_BYTES]
            truncated = True
        content = raw.decode("utf-8", errors="replace")
        self._send_json(
            {
                "path": rel,
                "exists": True,
                "size": target.stat().st_size,
                "truncated": truncated,
                "content": content,
            }
        )

    def _send_tree(self, job_id: str) -> None:
        try:
            job = self.manager.get_job(job_id)
        except KeyError:
            self._send_error(HTTPStatus.NOT_FOUND, "job not found")
            return
        files: list[dict[str, Any]] = []
        if job.repo.exists():
            for path in sorted(job.repo.rglob("*")):
                rel = path.relative_to(job.repo)
                if ".git" in rel.parts:
                    continue
                files.append(
                    {
                        "path": rel.as_posix(),
                        "kind": "dir" if path.is_dir() else "file",
                        "size": path.stat().st_size if path.is_file() else None,
                    }
                )
                if len(files) >= _TREE_LIMIT:
                    break
        self._send_json({"files": files, "limit": _TREE_LIMIT})

    def _send_download(self, job_id: str) -> None:
        try:
            job = self.manager.get_job(job_id)
        except KeyError:
            self._send_error(HTTPStatus.NOT_FOUND, "job not found")
            return
        if not job.repo.exists():
            self._send_error(HTTPStatus.NOT_FOUND, "repo does not exist yet")
            return
        archive = shutil.make_archive(str(job.workdir / "repo"), "zip", job.repo)
        data = Path(archive).read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Disposition", f'attachment; filename="{job.id}-repo.zip"')
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


_INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Benchmark Replicator</title>
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/katex.min.css">
  <script src="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/katex.min.js"></script>
  <style>
    :root {
      --bg: #030607;
      --wash: #030607;
      --panel: #101416;
      --panel-subtle: #151a1d;
      --ink: #f2f5f4;
      --muted: #a5afb7;
      --faint: #74808a;
      --line: #273036;
      --line-strong: #3a4750;
      --accent: #8ab4ff;
      --accent-dark: #c6dcff;
      --accent-soft: #14243b;
      --ok: #81c995;
      --warn: #fdd663;
      --bad: #f28b82;
      --code: #05090b;
      --code-ink: #d7e7ef;
      --soft: #1a2025;
      --shadow: 0 1px 2px rgba(0, 0, 0, 0.35), 0 18px 46px rgba(0, 0, 0, 0.36);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      background:
        radial-gradient(circle at 78% 6%, rgba(138, 180, 255, 0.22), transparent 28rem),
        radial-gradient(circle at 8% 16%, rgba(129, 201, 149, 0.1), transparent 26rem),
        var(--wash);
      color: var(--ink);
      font-family: "Google Sans", "Inter", ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      letter-spacing: 0;
      -webkit-font-smoothing: antialiased;
      text-rendering: optimizeLegibility;
    }
    button, input, textarea, select {
      font: inherit;
    }
    button {
      border: 1px solid var(--line);
      background: var(--panel);
      color: var(--ink);
      min-height: 40px;
      padding: 0 15px;
      border-radius: 999px;
      cursor: pointer;
      font-weight: 650;
      transition: background 140ms ease, border-color 140ms ease, box-shadow 140ms ease, color 140ms ease, transform 140ms ease;
    }
    button.primary {
      background: var(--accent);
      color: #061016;
      border-color: var(--accent);
    }
    button.primary:hover {
      background: var(--accent-dark);
      border-color: var(--accent-dark);
      box-shadow: 0 6px 18px rgba(26, 115, 232, 0.2);
    }
    button:hover:not(:disabled) {
      border-color: var(--line-strong);
      background: #161d22;
    }
    button.danger {
      color: var(--bad);
      border-color: rgba(242, 139, 130, 0.42);
      background: rgba(242, 139, 130, 0.08);
    }
    button:disabled {
      cursor: not-allowed;
      opacity: 0.55;
    }
    input, textarea, select {
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
      color: var(--ink);
      padding: 11px 13px;
      box-shadow: inset 0 1px 0 rgba(15, 23, 42, 0.02);
      outline: none;
      transition: border-color 140ms ease, box-shadow 140ms ease;
    }
    input:focus, textarea:focus, select:focus {
      border-color: var(--accent);
      box-shadow: 0 0 0 3px rgba(26, 115, 232, 0.13);
    }
    textarea {
      min-height: 84px;
      resize: vertical;
    }
    .masthead {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 24px;
      align-items: start;
      padding: 34px 32px 26px;
      border-bottom: 1px solid var(--line);
      background: rgba(3, 6, 7, 0.72);
      backdrop-filter: blur(18px);
    }
    .masthead-kicker {
      color: var(--accent);
      font-size: 13px;
      font-weight: 760;
      letter-spacing: 0.04em;
      text-transform: uppercase;
    }
    .masthead h1 {
      margin: 8px 0 0;
      max-width: 860px;
      color: var(--ink);
      font-size: clamp(38px, 5.2vw, 76px);
      line-height: 0.97;
      letter-spacing: -0.045em;
      font-weight: 760;
    }
    .masthead p {
      max-width: 650px;
      margin: 18px 0 0;
      color: var(--muted);
      font-size: 18px;
      line-height: 1.55;
    }
    .shell {
      display: grid;
      grid-template-columns: 380px minmax(0, 1fr);
      gap: 18px;
      min-height: calc(100vh - 214px);
      padding: 22px 24px 24px;
    }
    aside {
      border: 1px solid var(--line);
      border-radius: 8px;
      background: rgba(16, 20, 22, 0.88);
      backdrop-filter: blur(12px);
      padding: 24px;
      display: flex;
      flex-direction: column;
      gap: 20px;
      min-height: calc(100vh - 260px);
      box-shadow: var(--shadow);
    }
    main {
      display: grid;
      grid-template-rows: auto auto minmax(0, 1fr);
      gap: 12px;
      min-width: 0;
      min-height: calc(100vh - 260px);
    }
    .brand {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
    }
    .brand h1 {
      margin: 0;
      max-width: 220px;
      font-size: 15px;
      line-height: 1.2;
      letter-spacing: 0.04em;
      font-weight: 780;
      text-transform: uppercase;
      color: var(--faint);
    }
    .brand h1::after {
      content: "New replication";
      display: block;
      margin-top: 8px;
      color: var(--ink);
      font-size: 30px;
      line-height: 1.05;
      font-weight: 760;
      letter-spacing: 0;
      text-transform: none;
    }
    .status-pill {
      display: inline-flex;
      align-items: center;
      gap: 7px;
      min-height: 28px;
      padding: 0 11px;
      border: 1px solid var(--line);
      border-radius: 999px;
      color: var(--muted);
      background: var(--panel-subtle);
      font-size: 13px;
      white-space: nowrap;
      font-weight: 650;
    }
    .dot {
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background: var(--muted);
      flex: 0 0 auto;
    }
    .status-running .dot, .status-revising_plan .dot { background: var(--accent); }
    .status-awaiting_approval .dot { background: var(--warn); }
    .status-completed .dot { background: var(--ok); }
    .status-interrupted .dot { background: var(--warn); }
    .status-failed .dot, .status-canceled .dot { background: var(--bad); }
    .form {
      display: flex;
      flex-direction: column;
      gap: 14px;
    }
    .env-banner {
      border: 1px solid rgba(253, 214, 99, 0.38);
      background: rgba(253, 214, 99, 0.1);
      color: #ffe08a;
      border-radius: 8px;
      padding: 12px;
      font-size: 13px;
      line-height: 1.4;
      overflow-wrap: anywhere;
    }
    .env-banner.error {
      border-color: rgba(242, 139, 130, 0.42);
      background: rgba(242, 139, 130, 0.1);
      color: var(--bad);
    }
    .hidden {
      display: none;
    }
    label {
      display: flex;
      flex-direction: column;
      gap: 8px;
      color: var(--muted);
      font-size: 13px;
    }
    label span {
      color: var(--ink);
      font-weight: 700;
    }
    .row {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 10px;
    }
    .check-row {
      display: flex;
      align-items: center;
      gap: 8px;
      color: var(--ink);
      font-size: 14px;
      flex-direction: row;
      justify-content: center;
    }
    .check-row input {
      width: auto;
    }
    .jobs {
      overflow: auto;
      min-height: 160px;
      border-top: 1px solid var(--line);
      padding-top: 16px;
    }
    .jobs::before {
      content: "Recent jobs";
      display: block;
      margin: 0 0 10px;
      color: var(--faint);
      font-size: 12px;
      font-weight: 760;
      letter-spacing: 0.04em;
      text-transform: uppercase;
    }
    .job-item {
      width: 100%;
      display: grid;
      gap: 6px;
      text-align: left;
      padding: 13px 14px;
      margin-bottom: 10px;
      background: var(--panel-subtle);
      border-radius: 8px;
    }
    .job-item.active {
      border-color: var(--accent);
      background: rgba(16, 20, 22, 0.88);
      box-shadow: inset 3px 0 0 var(--accent), 0 4px 14px rgba(26, 115, 232, 0.08);
    }
    .job-title {
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      font-weight: 650;
    }
    .job-meta {
      display: flex;
      justify-content: space-between;
      gap: 8px;
      color: var(--muted);
      font-size: 12px;
    }
    .topbar {
      padding: 0;
      border: 0;
      border-radius: 0;
      background: transparent;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      min-width: 0;
      box-shadow: none;
    }
    .topbar > div:first-child {
      min-width: 0;
      padding: 18px 20px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: rgba(16, 20, 22, 0.9);
      box-shadow: var(--shadow);
      flex: 1 1 auto;
    }
    .repo-path {
      min-width: 0;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      color: var(--muted);
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 13px;
      margin-top: 6px;
    }
    .actions {
      display: flex;
      gap: 8px;
      align-items: center;
      flex-wrap: wrap;
      justify-content: flex-end;
      padding: 10px;
      border: 1px solid var(--line);
      border-radius: 999px;
      background: rgba(16, 20, 22, 0.9);
      box-shadow: var(--shadow);
      flex: 0 0 auto;
    }
    .timeline {
      padding: 16px;
      border: 1px solid var(--line);
      border-radius: 8px;
      display: grid;
      grid-template-columns: repeat(7, minmax(88px, 1fr));
      gap: 12px;
      background: rgba(16, 20, 22, 0.88);
      box-shadow: var(--shadow);
      counter-reset: phase;
    }
    .phase {
      border: 1px solid transparent;
      border-radius: 999px;
      background: var(--soft);
      padding: 8px 12px 8px 8px;
      min-height: 46px;
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 8px;
      color: var(--muted);
      font-size: 13px;
      font-weight: 650;
    }
    .phase::before {
      counter-increment: phase;
      content: counter(phase);
      width: 28px;
      height: 28px;
      border-radius: 50%;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      background: rgba(16, 20, 22, 0.9);
      color: var(--faint);
      font-size: 12px;
      font-weight: 760;
      box-shadow: inset 0 0 0 1px var(--line);
    }
    .phase.active {
      color: var(--accent-dark);
      background: var(--accent-soft);
      border-color: var(--accent);
    }
    .phase.active::before {
      background: var(--accent);
      color: #061016;
      box-shadow: none;
    }
    .workspace {
      display: grid;
      grid-template-columns: minmax(0, 1fr) 440px;
      gap: 18px;
      min-height: 0;
    }
    .document {
      min-width: 0;
      min-height: 0;
      display: grid;
      grid-template-rows: auto minmax(0, 1fr);
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
      overflow: hidden;
      box-shadow: var(--shadow);
    }
    .tabs {
      display: flex;
      gap: 8px;
      border-bottom: 1px solid var(--line);
      padding: 12px;
      overflow-x: auto;
      align-items: center;
      background: var(--panel-subtle);
    }
    .tabs button {
      min-width: 84px;
      background: transparent;
    }
    .tabs button.active {
      background: var(--panel);
      border-color: var(--accent);
      color: var(--accent-dark);
      box-shadow: 0 3px 12px rgba(18, 30, 56, 0.06);
    }
    .view-controls {
      margin-left: auto;
      display: flex;
      gap: 4px;
      flex: 0 0 auto;
    }
    .view-controls button {
      min-width: 72px;
    }
    .content {
      overflow: auto;
      min-height: 0;
      padding: 28px 32px;
    }
    .markdown-body {
      max-width: 980px;
      color: var(--ink);
      font-size: 16px;
      line-height: 1.72;
    }
    .markdown-body > *:first-child {
      margin-top: 0;
    }
    .markdown-body > *:last-child {
      margin-bottom: 0;
    }
    .markdown-body h1,
    .markdown-body h2,
    .markdown-body h3,
    .markdown-body h4,
    .markdown-body h5,
    .markdown-body h6 {
      margin: 1.35em 0 0.55em;
      color: var(--ink);
      line-height: 1.22;
      font-weight: 760;
      letter-spacing: -0.01em;
    }
    .markdown-body h1 {
      font-size: 34px;
      padding-bottom: 0.34em;
      border-bottom: 1px solid var(--line);
    }
    .markdown-body h2 {
      font-size: 25px;
      padding-bottom: 0.24em;
      border-bottom: 1px solid var(--line);
    }
    .markdown-body h3 {
      font-size: 19px;
    }
    .markdown-body h4 {
      font-size: 16px;
    }
    .markdown-body p {
      margin: 0.85em 0;
    }
    .markdown-body a {
      color: var(--accent-dark);
      text-decoration: underline;
      text-decoration-thickness: 1px;
      text-underline-offset: 3px;
    }
    .markdown-body ul,
    .markdown-body ol {
      margin: 0.85em 0;
      padding-left: 1.55em;
    }
    .markdown-body li {
      margin: 0.3em 0;
    }
    .markdown-body blockquote {
      margin: 1em 0;
      padding: 0.1em 1em;
      color: var(--muted);
      border-left: 4px solid var(--accent);
      background: var(--accent-soft);
    }
    .markdown-body table {
      width: 100%;
      margin: 1em 0;
      border-collapse: collapse;
      font-size: 14px;
    }
    .markdown-body th,
    .markdown-body td {
      border: 1px solid var(--line);
      padding: 8px 10px;
      vertical-align: top;
    }
    .markdown-body th {
      background: var(--soft);
      font-weight: 700;
      text-align: left;
    }
    .markdown-body tr:nth-child(even) td {
      background: rgba(255, 255, 255, 0.025);
    }
    .markdown-body code {
      border: 1px solid var(--line);
      border-radius: 4px;
      background: var(--soft);
      padding: 0.1em 0.32em;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 0.92em;
    }
    .markdown-body pre {
      margin: 1em 0;
      padding: 13px 14px;
      overflow: auto;
      border-radius: 6px;
      background: var(--code);
      color: #e8eee9;
      white-space: pre;
    }
    .markdown-body pre code {
      border: 0;
      background: transparent;
      padding: 0;
      color: inherit;
      font-size: 13px;
    }
    .markdown-body hr {
      border: 0;
      border-top: 1px solid var(--line);
      margin: 1.5em 0;
    }
    .markdown-body .math-display {
      margin: 1.1em 0;
      overflow-x: auto;
      overflow-y: hidden;
      padding: 0.25em 0;
      text-align: center;
    }
    .markdown-body .math-inline {
      display: inline-block;
      max-width: 100%;
      overflow-x: auto;
      overflow-y: hidden;
      vertical-align: -0.08em;
    }
    .markdown-body .math-fallback {
      border: 1px solid var(--line);
      border-radius: 4px;
      background: var(--soft);
      color: var(--muted);
      padding: 0.08em 0.28em;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 0.92em;
    }
    .markdown-body input[type="checkbox"] {
      width: auto;
      margin-right: 0.45em;
      vertical-align: -0.12em;
    }
    pre {
      margin: 0;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 13px;
      line-height: 1.5;
    }
    .empty {
      color: var(--muted);
      font-size: 14px;
    }
    .side {
      min-width: 0;
      min-height: 0;
      display: grid;
      grid-template-rows: auto minmax(0, 1fr);
      background: var(--code);
      color: var(--code-ink);
      border-radius: 8px;
      overflow: hidden;
      box-shadow: var(--shadow);
    }
    .console-head {
      min-height: 56px;
      padding: 14px 18px;
      border-bottom: 1px solid rgba(255, 255, 255, 0.09);
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
    }
    .console-head span {
      color: #d7e7ef;
      font-weight: 650;
      font-size: 13px;
    }
    .console {
      overflow: auto;
      min-height: 0;
      padding: 18px;
      background: #070d10;
      color: var(--code-ink);
    }
    .revision {
      display: none;
      gap: 8px;
      align-items: stretch;
      padding: 10px;
      border-top: 1px solid rgba(255, 255, 255, 0.09);
      background: var(--code);
    }
    .revision.visible {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto auto;
    }
    .revision input {
      background: #111a20;
      border-color: #273843;
      color: var(--code-ink);
    }
    .file-list {
      display: grid;
      gap: 8px;
    }
    .file-list button {
      text-align: left;
      justify-content: flex-start;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .file-list .dir {
      color: var(--muted);
      background: var(--soft);
    }
    @media (max-width: 1050px) {
      .masthead {
        grid-template-columns: 1fr;
        padding: 26px 18px 20px;
      }
      .masthead h1 {
        font-size: clamp(36px, 12vw, 58px);
      }
      .masthead p {
        font-size: 16px;
      }
      .shell {
        grid-template-columns: 1fr;
        padding: 10px;
      }
      aside {
        min-height: auto;
      }
      main {
        min-height: 720px;
      }
      .workspace {
        grid-template-columns: 1fr;
        grid-template-rows: minmax(420px, 1fr) 360px;
      }
      .document {
        min-height: 0;
      }
      .timeline {
        grid-template-columns: repeat(3, minmax(88px, 1fr));
      }
      .topbar {
        align-items: flex-start;
        flex-direction: column;
      }
      .actions {
        justify-content: flex-start;
      }
      .content {
        padding: 22px 18px;
      }
    }
  </style>
</head>
<body>
  <header class="masthead">
    <div>
      <div class="masthead-kicker">Benchmark Replicator</div>
      <h1>Turn papers into runnable baselines.</h1>
      <p>Plan the experiment, approve the scope, and watch a generated repo move through code, tests, benchmark, repair, and cleanup.</p>
    </div>
    <span id="serverStatus" class="status-pill"><span class="dot"></span>local</span>
  </header>

  <div class="shell">
    <aside>
      <div class="brand">
        <h1>Control</h1>
      </div>
      <div id="envBanner" class="env-banner hidden"></div>

      <form id="startForm" class="form">
        <label>
          <span>Paper source</span>
          <input id="source" name="source" required placeholder="arXiv id, URL, or local PDF path">
        </label>
        <label>
          <span>Instructions</span>
          <textarea id="instructions" name="instructions" placeholder="Optional steering for the planner"></textarea>
        </label>
        <div class="row">
          <label>
            <span>Model</span>
            <input id="model" name="model" placeholder="default">
          </label>
          <label>
            <span>Hardware</span>
            <select id="hardware" name="hardware">
              <option value="cpu">CPU</option>
              <option value="gpu">GPU</option>
            </select>
          </label>
        </div>
        <label class="check-row">
          <input id="autoApprove" type="checkbox">
          <span>Auto-approve plan</span>
        </label>
        <button class="primary" type="submit">Start replication</button>
      </form>

      <div class="jobs">
        <div id="jobsList"></div>
      </div>
    </aside>

    <main>
      <div class="topbar">
        <div>
          <div id="jobStatus" class="status-pill"><span class="dot"></span>no job</div>
          <div id="repoPath" class="repo-path"></div>
        </div>
        <div class="actions">
          <button id="approveBtn" class="primary" disabled>Approve</button>
          <button id="reviseBtn" disabled>Revise</button>
          <button id="rejectBtn" class="danger" disabled>Stop</button>
          <button id="cancelBtn" class="danger" disabled>Cancel</button>
          <button id="downloadBtn" disabled>Download repo</button>
        </div>
      </div>

      <div class="timeline" id="timeline">
        <div class="phase" data-phase="planner">Planner</div>
        <div class="phase" data-phase="reviser">Reviser</div>
        <div class="phase" data-phase="coder">Coder</div>
        <div class="phase" data-phase="tester">Tester</div>
        <div class="phase" data-phase="benchmarker">Benchmark</div>
        <div class="phase" data-phase="repair">Repair</div>
        <div class="phase" data-phase="cleaner">Cleaner</div>
      </div>

      <div class="workspace">
        <section class="document">
          <div class="tabs">
            <button data-tab="PLAN.md" class="active">Plan</button>
            <button data-tab="REPORT.md">Report</button>
            <button data-tab="EVAL.md">Eval</button>
            <button data-tab="README.md">Readme</button>
            <button data-tab="files">Files</button>
            <div class="view-controls">
              <button id="previewBtn" class="active">Preview</button>
              <button id="rawBtn">Raw</button>
            </div>
          </div>
          <div class="content" id="content"><p class="empty">Start or select a job.</p></div>
        </section>

        <section class="side">
          <div class="console-head">
            <span>Run Log</span>
            <button id="clearLogBtn">Clear</button>
          </div>
          <div class="console" id="console"><pre id="log"></pre></div>
          <div id="revisionBox" class="revision">
            <input id="revisionText" placeholder="Revision message">
            <button id="sendRevisionBtn">Send</button>
            <button id="doneRevisionBtn">Done</button>
          </div>
        </section>
      </div>
    </main>
  </div>

  <script>
    const state = {
      currentJob: null,
      eventSource: null,
      activeTab: 'PLAN.md',
      filePath: null,
      viewMode: 'preview',
      documentContent: '',
      documentExists: false,
      log: '',
      phase: null,
      pollTimer: null
    };

    const $ = (id) => document.getElementById(id);

    async function request(path, options = {}) {
      const headers = options.headers || {};
      if (options.body && !headers['Content-Type']) {
        headers['Content-Type'] = 'application/json';
      }
      const response = await fetch(path, { ...options, headers });
      const text = await response.text();
      let data = {};
      if (text) {
        data = JSON.parse(text);
      }
      if (!response.ok) {
        const error = new Error(data.error || response.statusText);
        error.status = response.status;
        throw error;
      }
      return data;
    }

    function statusClass(status) {
      return `status-pill status-${status || 'idle'}`;
    }

    function setJobStatus(job) {
      const status = job ? job.status : 'no job';
      $('jobStatus').className = statusClass(status);
      $('jobStatus').innerHTML = `<span class="dot"></span>${status}`;
      $('repoPath').textContent = job ? job.repo : '';
      const active = job && !['completed', 'failed', 'canceled', 'interrupted', 'archived'].includes(job.status);
      const awaiting = job && job.status === 'awaiting_approval';
      const revising = job && job.status === 'revising_plan';
      $('approveBtn').disabled = !awaiting;
      $('reviseBtn').disabled = !awaiting;
      $('rejectBtn').disabled = !awaiting;
      $('cancelBtn').disabled = !active;
      $('downloadBtn').disabled = !job;
      $('revisionBox').classList.toggle('visible', !!revising);
    }

    function setPhaseFromLog(chunk) {
      const match = chunk.match(/▶\s+(PLANNER|REVISER|CODER(?:-\d+)?|TESTER|BENCHMARKER|REPAIR(?:-\d+)?|CLEANER)\b/i);
      if (match) {
        const label = match[1].toLowerCase();
        state.phase = label.startsWith('coder') ? 'coder'
          : label.startsWith('repair') ? 'repair'
          : label === 'benchmarker' ? 'benchmarker'
          : label;
      }
      document.querySelectorAll('.phase').forEach((el) => {
        el.classList.toggle('active', el.dataset.phase === state.phase);
      });
    }

    function appendLog(chunk) {
      state.log += chunk;
      $('log').textContent += chunk;
      setPhaseFromLog(chunk);
      const consoleBox = $('console');
      consoleBox.scrollTop = consoleBox.scrollHeight;
    }

    async function loadJobs() {
      const data = await request('/api/jobs');
      const list = $('jobsList');
      list.innerHTML = '';
      for (const job of data.jobs) {
        const button = document.createElement('button');
        button.className = 'job-item' + (state.currentJob && state.currentJob.id === job.id ? ' active' : '');
        button.innerHTML = `
          <div class="job-title">${escapeHtml(job.source)}</div>
          <div class="job-meta">
            <span>${escapeHtml(job.status)}</span>
            <span>${escapeHtml(job.id.slice(9, 15))}</span>
          </div>`;
        button.addEventListener('click', () => selectJob(job.id));
        list.appendChild(button);
      }
    }

    async function loadEnvironment() {
      const banner = $('envBanner');
      const startButton = document.querySelector('#startForm button[type="submit"]');
      try {
        const env = await request('/api/environment');
        const missing = env.missing || [];
        const warnings = env.warnings || [];
        if (!env.ok) {
          banner.className = 'env-banner error';
          banner.textContent = `Missing packages: ${missing.map((m) => m.package).join(', ')}. ${env.install_hint}`;
          startButton.disabled = true;
          return;
        }
        if (warnings.length) {
          banner.className = 'env-banner';
          banner.textContent = warnings.join(' ');
          startButton.disabled = false;
          return;
        }
        banner.className = 'env-banner hidden';
        startButton.disabled = false;
      } catch (error) {
        banner.className = 'env-banner error';
        banner.textContent = error.message;
        startButton.disabled = true;
      }
    }

    async function selectJob(jobId) {
      if (state.eventSource) {
        state.eventSource.close();
      }
      let data;
      try {
        data = await request(`/api/jobs/${jobId}`);
      } catch (error) {
        if (error.status === 404) {
          state.currentJob = null;
          setJobStatus(null);
          await loadJobs();
          return;
        }
        throw error;
      }
      state.currentJob = data.job;
      state.log = '';
      $('log').textContent = '';
      setJobStatus(state.currentJob);
      await loadTab();
      state.eventSource = new EventSource(`/api/jobs/${jobId}/events`);
      state.eventSource.onmessage = (event) => {
        const data = JSON.parse(event.data);
        if (data.job) {
          state.currentJob = data.job;
          setJobStatus(data.job);
          if (['completed', 'failed', 'canceled', 'interrupted', 'archived'].includes(data.job.status) && state.eventSource) {
            state.eventSource.close();
          }
        }
        if (data.type === 'log') {
          appendLog(data.chunk);
          if (state.activeTab !== 'files') {
            lazyRefreshTab();
          }
        }
      };
      state.eventSource.onerror = () => {};
      await loadJobs();
    }

    let refreshTimeout = null;
    function lazyRefreshTab() {
      clearTimeout(refreshTimeout);
      refreshTimeout = setTimeout(loadTab, 700);
    }

    async function loadTab() {
      if (!state.currentJob) {
        $('content').innerHTML = '<p class="empty">Start or select a job.</p>';
        return;
      }
      if (state.activeTab === 'files') {
        await loadTree();
        return;
      }
      const data = await request(`/api/jobs/${state.currentJob.id}/file?path=${encodeURIComponent(state.activeTab)}`);
      renderDocument(state.activeTab, data.content, data.exists);
    }

    async function loadTree() {
      state.filePath = null;
      state.documentContent = '';
      state.documentExists = false;
      updateViewButtons();
      const data = await request(`/api/jobs/${state.currentJob.id}/tree`);
      const wrapper = document.createElement('div');
      wrapper.className = 'file-list';
      for (const file of data.files) {
        const button = document.createElement('button');
        button.className = file.kind === 'dir' ? 'dir' : '';
        button.textContent = `${file.kind === 'dir' ? '/' : ''}${file.path}`;
        button.disabled = file.kind === 'dir';
        button.addEventListener('click', async () => {
          const data = await request(`/api/jobs/${state.currentJob.id}/file?path=${encodeURIComponent(file.path)}`);
          renderDocument(file.path, data.content, data.exists);
        });
        wrapper.appendChild(button);
      }
      if (!data.files.length) {
        wrapper.innerHTML = '<p class="empty">No files yet.</p>';
      }
      $('content').innerHTML = '';
      $('content').appendChild(wrapper);
    }

    function updateViewButtons() {
      $('previewBtn').classList.toggle('active', state.viewMode === 'preview');
      $('rawBtn').classList.toggle('active', state.viewMode === 'raw');
      const noDocumentSelected = state.activeTab === 'files' && !state.filePath;
      $('previewBtn').disabled = noDocumentSelected;
      $('rawBtn').disabled = noDocumentSelected;
    }

    function isMarkdownPath(path) {
      return /\.md$/i.test(path || '');
    }

    function renderDocument(path, content, exists) {
      state.filePath = path;
      state.documentContent = content || '';
      state.documentExists = !!exists;
      updateViewButtons();
      if (!exists) {
        $('content').innerHTML = `<p class="empty">${escapeHtml(path)} is not written yet.</p>`;
        return;
      }
      if (state.viewMode === 'preview' && isMarkdownPath(path)) {
        $('content').innerHTML = `<article class="markdown-body">${renderMarkdown(content)}</article>`;
        return;
      }
      $('content').innerHTML = `<pre>${escapeHtml(content)}</pre>`;
    }

    function renderMarkdown(markdown) {
      const lines = String(markdown || '').replace(/\r\n?/g, '\n').split('\n');
      const out = [];
      let i = 0;
      let paragraph = [];

      function flushParagraph() {
        if (!paragraph.length) return;
        out.push(`<p>${formatInline(paragraph.join(' ').trim())}</p>`);
        paragraph = [];
      }

      while (i < lines.length) {
        const line = lines[i];
        const trimmed = line.trim();

        if (!trimmed) {
          flushParagraph();
          i++;
          continue;
        }

        const fence = trimmed.match(/^(```+|~~~+)\s*([A-Za-z0-9_.+-]*)\s*$/);
        if (fence) {
          flushParagraph();
          const marker = fence[1].slice(0, 3);
          const language = fence[2] ? ` data-language="${escapeAttr(fence[2])}"` : '';
          i++;
          const code = [];
          while (i < lines.length && !lines[i].trim().startsWith(marker)) {
            code.push(lines[i]);
            i++;
          }
          if (i < lines.length) i++;
          out.push(`<pre${language}><code>${escapeHtml(code.join('\n'))}</code></pre>`);
          continue;
        }

        if (trimmed.startsWith('$$')) {
          flushParagraph();
          const sameLine = trimmed.match(/^\$\$\s*([\s\S]*?)\s*\$\$$/);
          if (sameLine && sameLine[1].trim()) {
            out.push(renderMath(sameLine[1].trim(), true));
            i++;
            continue;
          }
          const math = [];
          const first = trimmed.slice(2).trim();
          if (first) math.push(first);
          i++;
          while (i < lines.length && lines[i].trim() !== '$$') {
            math.push(lines[i]);
            i++;
          }
          if (i < lines.length) i++;
          out.push(renderMath(math.join('\n').trim(), true));
          continue;
        }

        if (trimmed.startsWith('\\[')) {
          flushParagraph();
          const sameLine = trimmed.match(/^\\\[\s*([\s\S]*?)\s*\\\]$/);
          if (sameLine && sameLine[1].trim()) {
            out.push(renderMath(sameLine[1].trim(), true));
            i++;
            continue;
          }
          const math = [];
          const first = trimmed.slice(2).trim();
          if (first) math.push(first);
          i++;
          while (i < lines.length && lines[i].trim() !== '\\]') {
            math.push(lines[i]);
            i++;
          }
          if (i < lines.length) i++;
          out.push(renderMath(math.join('\n').trim(), true));
          continue;
        }

        const heading = trimmed.match(/^(#{1,6})\s+(.+?)\s*#*$/);
        if (heading) {
          flushParagraph();
          const level = heading[1].length;
          out.push(`<h${level}>${formatInline(heading[2])}</h${level}>`);
          i++;
          continue;
        }

        if (/^([-*_])(?:\s*\1){2,}\s*$/.test(trimmed)) {
          flushParagraph();
          out.push('<hr>');
          i++;
          continue;
        }

        if (/^>\s?/.test(trimmed)) {
          flushParagraph();
          const quote = [];
          while (i < lines.length && /^>\s?/.test(lines[i].trim())) {
            quote.push(lines[i].trim().replace(/^>\s?/, ''));
            i++;
          }
          out.push(`<blockquote>${renderMarkdown(quote.join('\n'))}</blockquote>`);
          continue;
        }

        if (looksLikeTable(lines, i)) {
          flushParagraph();
          const table = [];
          while (i < lines.length && lines[i].includes('|') && lines[i].trim()) {
            table.push(lines[i]);
            i++;
          }
          out.push(renderTable(table));
          continue;
        }

        const unordered = trimmed.match(/^[-*+]\s+(\[[ xX]\]\s+)?(.+)$/);
        if (unordered) {
          flushParagraph();
          const items = [];
          while (i < lines.length) {
            const item = lines[i].trim().match(/^[-*+]\s+(\[[ xX]\]\s+)?(.+)$/);
            if (!item) break;
            const checked = item[1] && /x/i.test(item[1]);
            const checkbox = item[1]
              ? `<input type="checkbox" disabled${checked ? ' checked' : ''}>`
              : '';
            items.push(`<li>${checkbox}${formatInline(item[2])}</li>`);
            i++;
          }
          out.push(`<ul>${items.join('')}</ul>`);
          continue;
        }

        const ordered = trimmed.match(/^\d+[.)]\s+(.+)$/);
        if (ordered) {
          flushParagraph();
          const items = [];
          while (i < lines.length) {
            const item = lines[i].trim().match(/^\d+[.)]\s+(.+)$/);
            if (!item) break;
            items.push(`<li>${formatInline(item[1])}</li>`);
            i++;
          }
          out.push(`<ol>${items.join('')}</ol>`);
          continue;
        }

        paragraph.push(line);
        i++;
      }
      flushParagraph();
      return out.join('\n');
    }

    function looksLikeTable(lines, index) {
      if (index + 1 >= lines.length) return false;
      const header = lines[index].trim();
      const separator = lines[index + 1].trim();
      return header.includes('|') && /^\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?$/.test(separator);
    }

    function splitTableRow(row) {
      let text = row.trim();
      if (text.startsWith('|')) text = text.slice(1);
      if (text.endsWith('|')) text = text.slice(0, -1);
      return text.split('|').map((cell) => cell.trim());
    }

    function renderTable(rows) {
      const header = splitTableRow(rows[0]);
      const align = splitTableRow(rows[1]).map((cell) => {
        const left = cell.startsWith(':');
        const right = cell.endsWith(':');
        return left && right ? 'center' : right ? 'right' : left ? 'left' : '';
      });
      const body = rows.slice(2).map(splitTableRow);
      const th = header.map((cell, index) => {
        const style = align[index] ? ` style="text-align:${align[index]}"` : '';
        return `<th${style}>${formatInline(cell)}</th>`;
      }).join('');
      const trs = body.map((row) => {
        const tds = row.map((cell, index) => {
          const style = align[index] ? ` style="text-align:${align[index]}"` : '';
          return `<td${style}>${formatInline(cell)}</td>`;
        }).join('');
        return `<tr>${tds}</tr>`;
      }).join('');
      return `<table><thead><tr>${th}</tr></thead><tbody>${trs}</tbody></table>`;
    }

    function formatInline(text) {
      const codeTokens = [];
      const linkTokens = [];
      const mathTokens = [];
      let working = String(text || '').replace(/`([^`]+)`/g, (_, code) => {
        const token = `@@CODE${codeTokens.length}@@`;
        codeTokens.push(`<code>${escapeHtml(code)}</code>`);
        return token;
      });
      working = extractInlineMath(working, mathTokens);
      working = escapeHtml(working);
      working = working.replace(/\[([^\]]+)\]\(([^)\s]+)(?:\s+"[^"]*")?\)/g, (_, label, href) => {
        const safeHref = sanitizeHref(href);
        if (!safeHref) return label;
        const token = `@@LINK${linkTokens.length}@@`;
        linkTokens.push(`<a href="${escapeAttr(safeHref)}" target="_blank" rel="noopener noreferrer">${label}</a>`);
        return token;
      });
      working = working.replace(/(^|[\s(])(https?:\/\/[^\s<)]+)/g, (match, prefix, url) => {
        const cleanUrl = url.replace(/[.,;:!?]+$/, '');
        const trailing = url.slice(cleanUrl.length);
        return `${prefix}<a href="${escapeAttr(cleanUrl)}" target="_blank" rel="noopener noreferrer">${cleanUrl}</a>${trailing}`;
      });
      working = working
        .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
        .replace(/__([^_]+)__/g, '<strong>$1</strong>')
        .replace(/~~([^~]+)~~/g, '<del>$1</del>')
        .replace(/(^|[^*])\*([^*\n]+)\*(?!\*)/g, '$1<em>$2</em>')
        .replace(/(^|[^_])_([^_\n]+)_(?!_)/g, '$1<em>$2</em>');
      linkTokens.forEach((html, index) => {
        working = working.replaceAll(`@@LINK${index}@@`, html);
      });
      mathTokens.forEach((html, index) => {
        working = working.replaceAll(`@@MATH${index}@@`, html);
      });
      codeTokens.forEach((html, index) => {
        working = working.replaceAll(`@@CODE${index}@@`, html);
      });
      return working;
    }

    function extractInlineMath(text, tokens) {
      let out = '';
      let i = 0;
      while (i < text.length) {
        if (text.startsWith('\\(', i)) {
          const end = text.indexOf('\\)', i + 2);
          if (end !== -1) {
            const tex = text.slice(i + 2, end);
            const token = `@@MATH${tokens.length}@@`;
            tokens.push(renderMath(tex, false));
            out += token;
            i = end + 2;
            continue;
          }
        }
        if (text[i] === '$' && text[i + 1] !== '$' && !isEscaped(text, i)) {
          const end = findClosingDollar(text, i + 1);
          if (end !== -1) {
            const tex = text.slice(i + 1, end);
            if (tex.trim()) {
              const token = `@@MATH${tokens.length}@@`;
              tokens.push(renderMath(tex, false));
              out += token;
              i = end + 1;
              continue;
            }
          }
        }
        out += text[i];
        i++;
      }
      return out;
    }

    function findClosingDollar(text, start) {
      for (let i = start; i < text.length; i++) {
        if (text[i] === '$' && text[i + 1] !== '$' && !isEscaped(text, i)) {
          return i;
        }
      }
      return -1;
    }

    function isEscaped(text, index) {
      let backslashes = 0;
      for (let i = index - 1; i >= 0 && text[i] === '\\'; i--) {
        backslashes++;
      }
      return backslashes % 2 === 1;
    }

    function renderMath(tex, displayMode) {
      const source = String(tex || '').trim();
      if (!source) return '';
      if (window.katex && typeof window.katex.renderToString === 'function') {
        try {
          const html = window.katex.renderToString(source, {
            displayMode,
            throwOnError: false,
            strict: 'warn',
            trust: false
          });
          const className = displayMode ? 'math-display' : 'math-inline';
          const tag = displayMode ? 'div' : 'span';
          return `<${tag} class="${className}">${html}</${tag}>`;
        } catch (_) {
          // Fall through to the escaped source fallback below.
        }
      }
      const delimiter = displayMode ? '$$' : '$';
      return `<code class="math-fallback">${delimiter}${escapeHtml(source)}${delimiter}</code>`;
    }

    function sanitizeHref(href) {
      const value = String(href || '').trim();
      if (/^(https?:|mailto:|#|\/|\.\/|\.\.\/)/i.test(value)) return value;
      return '';
    }

    function escapeHtml(text) {
      return String(text)
        .replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;')
        .replaceAll('"', '&quot;')
        .replaceAll("'", '&#039;');
    }

    function escapeAttr(text) {
      return escapeHtml(text).replaceAll('`', '&#096;');
    }

    async function jobAction(action, body) {
      if (!state.currentJob) return;
      const data = await request(`/api/jobs/${state.currentJob.id}/${action}`, {
        method: 'POST',
        body: body ? JSON.stringify(body) : undefined
      });
      state.currentJob = data.job;
      setJobStatus(data.job);
    }

    $('startForm').addEventListener('submit', async (event) => {
      event.preventDefault();
      const payload = {
        source: $('source').value.trim(),
        instructions: $('instructions').value.trim(),
        model: $('model').value.trim(),
        gpu: $('hardware').value === 'gpu',
        auto_approve: $('autoApprove').checked
      };
      try {
        const data = await request('/api/jobs', {
          method: 'POST',
          body: JSON.stringify(payload)
        });
        await loadJobs();
        await selectJob(data.job.id);
      } catch (error) {
        alert(error.message);
      }
    });

    $('approveBtn').addEventListener('click', () => jobAction('approve'));
    $('reviseBtn').addEventListener('click', () => jobAction('revise'));
    $('rejectBtn').addEventListener('click', () => jobAction('reject'));
    $('cancelBtn').addEventListener('click', () => jobAction('cancel'));
    $('downloadBtn').addEventListener('click', () => {
      if (state.currentJob) {
        window.location.href = `/api/jobs/${state.currentJob.id}/download`;
      }
    });
    $('clearLogBtn').addEventListener('click', () => {
      $('log').textContent = '';
    });
    $('sendRevisionBtn').addEventListener('click', async () => {
      const text = $('revisionText').value.trim();
      if (!text) return;
      $('revisionText').value = '';
      await jobAction('message', { text });
    });
    $('doneRevisionBtn').addEventListener('click', () => jobAction('done'));
    $('revisionText').addEventListener('keydown', (event) => {
      if (event.key === 'Enter') {
        event.preventDefault();
        $('sendRevisionBtn').click();
      }
    });

    document.querySelectorAll('.tabs button[data-tab]').forEach((button) => {
      button.addEventListener('click', async () => {
        if (!button.dataset.tab) return;
        document.querySelectorAll('.tabs button[data-tab]').forEach((b) => b.classList.remove('active'));
        button.classList.add('active');
        state.activeTab = button.dataset.tab;
        await loadTab();
      });
    });

    $('previewBtn').addEventListener('click', () => {
      state.viewMode = 'preview';
      renderDocument(state.filePath || state.activeTab, state.documentContent, state.documentExists);
    });
    $('rawBtn').addEventListener('click', () => {
      state.viewMode = 'raw';
      renderDocument(state.filePath || state.activeTab, state.documentContent, state.documentExists);
    });

    async function poll() {
      try {
        await loadJobs();
        if (state.currentJob) {
          const data = await request(`/api/jobs/${state.currentJob.id}`);
          state.currentJob = data.job;
          setJobStatus(data.job);
        }
      } catch (error) {
        if (error.status === 404) {
          state.currentJob = null;
          setJobStatus(null);
          if (state.eventSource) {
            state.eventSource.close();
            state.eventSource = null;
          }
        }
      }
    }

    loadEnvironment();
    loadJobs();
    state.pollTimer = setInterval(poll, 3000);
  </script>
</body>
</html>
"""


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="replicator-web",
        description="Run a local browser interface for the benchmark replicator.",
    )
    parser.add_argument("--host", default=_DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=_DEFAULT_PORT)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="Workspace root where jobs and generated repos are stored.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    root = args.root.resolve()
    manager = JobManager(root)
    ReplicatorRequestHandler.manager = manager
    server = ThreadingHTTPServer((args.host, args.port), ReplicatorRequestHandler)
    url = f"http://{args.host}:{args.port}"
    print(f"Benchmark Replicator web UI running at {url}")
    print("Press Ctrl-C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping web UI.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
