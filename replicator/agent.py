"""LLM backends for the replication pipeline.

Codex remains available, but OpenRouter and other OpenAI-compatible endpoints
use the same scoped tool contract.  This keeps the pipeline independent of a
Codex subscription.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from openai_codex import ApprovalMode, AsyncCodex, Sandbox

from .phases import Phase

# Keep the existing Codex default for backwards compatibility.  API users
# should pass their provider's model slug (normally from the Paperena config).
DEFAULT_MODEL = "gpt-5.6-sol"
_LOG_DIR_NAME = ".replicator/logs"
_AGENT_NAMES = frozenset(
    {
        "planner",
        "reviser",
        "adoption_inspector",
        "environment_fixer",
        "adapter",
        "source_patcher",
        "coder",
        "tester",
        "benchmarker",
        "repair",
        "cleaner",
    }
)


@dataclass(frozen=True)
class BackendSettings:
    provider: str = "codex"
    api_key: str = ""
    base_url: str = ""


_backend = BackendSettings()


def configure_backend(provider: str = "codex", api_key: str = "", base_url: str = "") -> None:
    """Select the process-wide model backend used by pipeline phases."""
    if provider not in {"codex", "openrouter", "openai-compatible"}:
        raise ValueError("provider must be codex, openrouter, or openai-compatible")
    if provider != "codex" and not api_key:
        raise ValueError(f"{provider} requires an API key")
    if provider == "openai-compatible" and not base_url:
        raise ValueError("openai-compatible requires --base-url")
    global _backend
    _backend = BackendSettings(provider, api_key, base_url.rstrip("/"))


def resolve_model(_phase_name: str, override: str | None) -> str:
    """Return the requested model, or the provider-neutral default."""
    return override or DEFAULT_MODEL


@dataclass(frozen=True)
class AgentSettings:
    """Optional per-agent model settings loaded from a JSON configuration file."""

    model: str | None = None
    reasoning_effort: str | None = None


def load_agent_settings(path: Path) -> dict[str, AgentSettings]:
    """Load and validate the per-agent model settings JSON file."""
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid agent settings JSON in {path}: {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError("agent settings must be a JSON object keyed by agent name")

    settings: dict[str, AgentSettings] = {}
    for name, values in data.items():
        if name not in _AGENT_NAMES:
            valid = ", ".join(sorted(_AGENT_NAMES))
            raise ValueError(f"unknown agent {name!r}; expected one of: {valid}")
        if not isinstance(values, dict):
            raise ValueError(f"settings for {name!r} must be a JSON object")
        unknown = set(values) - {"model", "reasoning_effort"}
        if unknown:
            raise ValueError(f"unknown settings for {name!r}: {', '.join(sorted(unknown))}")
        model = values.get("model")
        effort = values.get("reasoning_effort")
        if model is not None and (not isinstance(model, str) or not model.strip()):
            raise ValueError(f"{name}.model must be a non-empty string")
        if effort is not None and (not isinstance(effort, str) or not effort.strip()):
            raise ValueError(f"{name}.reasoning_effort must be a non-empty string")
        settings[name] = AgentSettings(model=model, reasoning_effort=effort)

    return settings


def resolve_agent_settings(
    phase_name: str, model_override: str | None, settings: dict[str, AgentSettings]
) -> AgentSettings:
    """Resolve one phase's settings, with ``--model`` taking precedence."""
    configured = settings.get(phase_name, AgentSettings())
    return AgentSettings(
        model=resolve_model(phase_name, model_override or configured.model),
        reasoning_effort=configured.reasoning_effort,
    )


@dataclass
class PhaseUsage:
    label: str
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


def _short(text: str, limit: int = 500) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit] + " […]"


def _developer_instructions(phase: Phase, hardware: str) -> str:
    capabilities = ", ".join(phase.allowed_tools)
    return (
        f"{phase.system_prompt(hardware)}\n\n"
        "This is one scoped phase in a deterministic pipeline. Do not delegate to "
        "sub-agents. Work only inside the current working directory. "
        f"Limit yourself to these capabilities: {capabilities}."
    )


def _record_result(result, label: str, log, usage: PhaseUsage) -> None:
    """Render a completed SDK turn and record its transcript and token usage."""
    for item in result.items:
        data = item.model_dump(mode="json", by_alias=True)
        log.write(json.dumps(data, ensure_ascii=False) + "\n")

    response = result.final_response or ""
    if response.strip():
        print(f"  [{_now()}] {_short(response)}")

    if result.usage is not None:
        total = result.usage.total
        usage.input_tokens = total.input_tokens
        usage.output_tokens = total.output_tokens

    status = getattr(result.status, "value", str(result.status))
    print(f"  [{_now()}] ✔ {label} finished ({status})")
    log.write(f"\n[result] {status}\n")


def _thread_options(phase: Phase, repo: Path, model: str, hardware: str) -> dict:
    """Build the native SDK arguments for one isolated pipeline phase."""
    return {
        "approval_mode": ApprovalMode.deny_all,
        "config": {
            "project_doc_max_bytes": 0,
            "sandbox_workspace_write": {"network_access": True},
            "web_search": "live" if "WebSearch" in phase.allowed_tools else "disabled",
        },
        "cwd": str(repo.resolve()),
        "developer_instructions": _developer_instructions(phase, hardware),
        "ephemeral": True,
        "model": model,
        "sandbox": Sandbox.workspace_write,
    }


async def _start_thread(codex: AsyncCodex, phase: Phase, repo: Path, model: str, hardware: str):
    return await codex.thread_start(**_thread_options(phase, repo, model, hardware))


async def _run_thread(thread, prompt: str, reasoning_effort: str | None):
    """Run a turn, setting effort only when it was explicitly configured."""
    if reasoning_effort is None:
        return await thread.run(prompt)
    return await thread.run(prompt, effort=reasoning_effort)


def _api_base_url() -> str:
    return _backend.base_url or "https://openrouter.ai/api/v1"


def _api_tools(phase: Phase, repo: Path) -> tuple[list[dict], dict[str, object]]:
    """Build a small OpenAI tool surface constrained to this phase's capabilities."""
    root = repo.resolve()

    def path(value: str) -> Path:
        candidate = (root / value).resolve()
        if candidate != root and root not in candidate.parents:
            raise ValueError("paths must remain inside the replication directory")
        return candidate

    def read_file(file: str) -> str:
        return path(file).read_text(errors="replace")

    def write_file(file: str, content: str) -> str:
        target = path(file)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        return f"wrote {file}"

    def run_command(command: str) -> str:
        completed = subprocess.run(
            command, cwd=root, shell=True, text=True, capture_output=True, timeout=600
        )
        output = (completed.stdout + completed.stderr)[-12000:]
        return f"exit={completed.returncode}\n{output}"

    definitions: list[dict] = []
    handlers: dict[str, object] = {}
    if any(tool in phase.allowed_tools for tool in ("Read", "Glob", "Grep")):
        definitions.append(
            {
                "type": "function",
                "function": {
                    "name": "read_file",
                    "description": "Read a UTF-8 text file relative to the workspace.",
                    "parameters": {
                        "type": "object",
                        "properties": {"file": {"type": "string"}},
                        "required": ["file"],
                    },
                },
            }
        )
        handlers["read_file"] = read_file
    if "Write" in phase.allowed_tools or "Edit" in phase.allowed_tools:
        definitions.append(
            {
                "type": "function",
                "function": {
                    "name": "write_file",
                    "description": "Create or replace a text file relative to the workspace.",
                    "parameters": {
                        "type": "object",
                        "properties": {"file": {"type": "string"}, "content": {"type": "string"}},
                        "required": ["file", "content"],
                    },
                },
            }
        )
        handlers["write_file"] = write_file
    if "Bash" in phase.allowed_tools:
        definitions.append(
            {
                "type": "function",
                "function": {
                    "name": "run_command",
                    "description": "Run a shell command in the workspace and return its output.",
                    "parameters": {
                        "type": "object",
                        "properties": {"command": {"type": "string"}},
                        "required": ["command"],
                    },
                },
            }
        )
        handlers["run_command"] = run_command
    return definitions, handlers


async def _run_openai_agent(
    phase: Phase, repo: Path, model: str, hardware: str, task: str, log, usage: PhaseUsage
) -> None:
    """Execute one tool-calling turn through an OpenAI-compatible API."""
    from openai import AsyncOpenAI

    tools, handlers = _api_tools(phase, repo)
    client = AsyncOpenAI(api_key=_backend.api_key, base_url=_api_base_url())
    messages: list[dict] = [
        {"role": "system", "content": _developer_instructions(phase, hardware)},
        {"role": "user", "content": task},
    ]
    for _ in range(80):
        response = await client.chat.completions.create(
            model=model, messages=messages, tools=tools or None
        )
        choice = response.choices[0].message
        messages.append(choice.model_dump(exclude_none=True))
        if response.usage:
            usage.input_tokens += response.usage.prompt_tokens or 0
            usage.output_tokens += response.usage.completion_tokens or 0
        tool_calls = choice.tool_calls or []
        if not tool_calls:
            final = choice.content or ""
            log.write(f"[assistant] {final}\n")
            if final.strip():
                print(f"  [{_now()}] {_short(final)}")
            return
        for call in tool_calls:
            try:
                arguments = json.loads(call.function.arguments)
                handler = handlers[call.function.name]
                result = handler(**arguments)  # type: ignore[operator]
            except Exception as exc:
                result = f"tool error: {exc}"
            log.write(f"[tool:{call.function.name}] {result}\n")
            messages.append({"role": "tool", "tool_call_id": call.id, "content": str(result)})
    raise RuntimeError("model exceeded the 80-call tool limit")


async def run_agent(
    phase: Phase,
    repo: Path,
    model: str,
    hardware: str,
    *,
    label: str | None = None,
    reasoning_effort: str | None = None,
    **task_kwargs: str,
) -> PhaseUsage:
    """Run one isolated agent to completion using the configured backend."""
    label = label or phase.name
    log_path = repo / _LOG_DIR_NAME / f"{label}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    usage = PhaseUsage(label=label, model=model)

    effort = f", effort: {reasoning_effort}" if reasoning_effort else ""
    print(f"\n{'=' * 70}\n▶  {label.upper()}  (model: {model}{effort})\n{'=' * 70}")
    task = phase.task.format(**task_kwargs)

    with log_path.open("w", buffering=1) as log:
        log.write(f"[user] {task}\n")
        if _backend.provider == "codex":
            async with AsyncCodex() as codex:
                thread = await _start_thread(codex, phase, repo, model, hardware)
                result = await _run_thread(thread, task, reasoning_effort)
            _record_result(result, label, log, usage)
        else:
            if reasoning_effort:
                log.write(f"[note] reasoning_effort={reasoning_effort} is provider-specific\n")
            await _run_openai_agent(phase, repo, model, hardware, task, log, usage)
            print(f"  [{_now()}] ✔ {label} finished (completed)")

    return usage


async def run_chat_agent(
    phase: Phase,
    repo: Path,
    model: str,
    hardware: str,
    *,
    paper_sources: str = "",
    reasoning_effort: str | None = None,
) -> PhaseUsage:
    """Drive a stateful Codex thread for interactive plan revision."""
    label = phase.name
    log_path = repo / _LOG_DIR_NAME / f"{label}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    usage = PhaseUsage(label=label, model=model)

    effort = f", effort: {reasoning_effort}" if reasoning_effort else ""
    print(f"\n{'=' * 70}\n▶  {label.upper()}  (model: {model}{effort})\n{'=' * 70}")
    print(
        "  Chat to revise PLAN.md and criteria.json. Type a request and press Enter;\n"
        "  the agent edits the plan and reports back. Type 'done' (or an empty line)\n"
        "  when you're finished to return to the approve prompt."
    )
    first_prefix = (
        "You are revising the existing PLAN.md and .replicator/criteria.json. "
        f"For reference, {paper_sources}.\n\nMy first request:\n"
    )

    with log_path.open("w", buffering=1) as log:
        if _backend.provider != "codex":
            raise RuntimeError(
                "interactive plan revision currently requires the Codex backend; use --yes"
            )
        async with AsyncCodex() as codex:
            thread = await _start_thread(codex, phase, repo, model, hardware)
            first = True
            while True:
                try:
                    message = input("\nyou › ").strip()
                except EOFError:
                    break
                if message.lower() in ("", "done", "exit", "quit"):
                    break
                prompt = first_prefix + message if first else message
                first = False
                log.write(f"\n[user] {message}\n")
                result = await _run_thread(thread, prompt, reasoning_effort)
                _record_result(result, label, log, usage)

    return usage
