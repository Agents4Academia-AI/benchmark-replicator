"""Provider-agnostic agent backend: build models, supply tools, run phases.

This module replaces ``claude-agent-sdk``. Each phase is one LangGraph ReAct agent
(a managed tool-calling loop) over a model chosen with ``init_chat_model``, so any
provider with a LangChain integration can drive the pipeline: Anthropic, OpenAI,
Google, or a local server (ollama, llama.cpp, vLLM) reached over an OpenAI-compatible
endpoint. The eight tools below reproduce the SDK's built-in tool set — same names and
semantics — so the phase prompts need no changes.

Note: reliable agentic tool-calling over 80-turn phases needs a strong tool-calling
model. Frontier hosted models are fine; for local use prefer a large model served by
vLLM. Small ollama models will likely struggle on the long coder/repair phases.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx
from bs4 import BeautifulSoup
from langchain.agents import create_agent
from langchain.chat_models import init_chat_model
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import BaseTool, tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.errors import GraphRecursionError

from .paper import pdf_to_text
from .phases import Phase
from .sandbox import bash_escape

_LOG_DIR_NAME = ".replicator/logs"


# ── Model selection ────────────────────────────────────────────────────────────

# Per-phase model tier: spend on the hard reasoning steps, save on the rest.
_PHASE_TIER = {
    "planner": "strong",
    "reviser": "strong",  # Conversationally revising the plan is the same hard reasoning.
    "coder": "strong",
    "repair": "strong",  # Fixing real bugs is hard reasoning — use the strong model.
    "tester": "cheap",
    "benchmarker": "cheap",
    "cleaner": "cheap",
}

# Default (strong, cheap) model pair per provider. ``init_chat_model`` reads the
# ``provider:model`` prefix. The Anthropic pair reproduces today's behaviour; the
# others are sensible starting points — override wholesale with ``--model``. The
# local (ollama) pair in particular is a placeholder: point ``--model`` / ``--base-url``
# at whatever you actually serve.
_PROVIDER_DEFAULTS = {
    "anthropic": {"strong": "anthropic:claude-opus-4-8", "cheap": "anthropic:claude-sonnet-4-6"},
    "openai": {"strong": "openai:gpt-5.1", "cheap": "openai:gpt-5.1-mini"},
    "google_genai": {"strong": "google_genai:gemini-3-pro", "cheap": "google_genai:gemini-3-flash"},
    "ollama": {"strong": "ollama:qwen3-coder:30b", "cheap": "ollama:qwen3:8b"},
}

PROVIDERS = tuple(_PROVIDER_DEFAULTS)  # the CLI's --provider choices

# Approximate USD per 1M tokens (input, output) for known model ids, so the cost
# summary still reports dollars where we know the price. Unknown ids report tokens
# only. Verify against current provider pricing before trusting the dollar figure.
_PRICE_PER_M = {
    "anthropic:claude-opus-4-8": (15.0, 75.0),
    "anthropic:claude-sonnet-4-6": (3.0, 15.0),
}


def resolve_model(phase_name: str, provider: str, override: str | None) -> str:
    """The concrete ``provider:model`` spec for a phase.

    ``--model`` (``override``) replaces every phase's model wholesale, matching the old
    behaviour; otherwise the phase's tier picks the provider's strong/cheap default.
    """
    if override:
        return override
    return _PROVIDER_DEFAULTS[provider][_PHASE_TIER[phase_name]]


def build_model(spec: str, base_url: str | None = None):
    """Instantiate a chat model from a ``provider:model`` spec via ``init_chat_model``.

    ``base_url`` points at an OpenAI-compatible local server (vLLM / llama.cpp) or a
    custom ollama host; API keys come from the standard provider env vars
    (``ANTHROPIC_API_KEY``, ``OPENAI_API_KEY``, ``GOOGLE_API_KEY``, …).
    """
    kwargs = {"base_url": base_url} if base_url else {}
    return init_chat_model(spec, **kwargs)


# ── Tools (reproducing the SDK's built-in tool set) ─────────────────────────────

# A single Bash command's wall-clock cap (seconds). The default-run / benchmark can be
# long (CPU mode targets up to ~1h), so the cap is generous; it only fires on a genuinely
# hung command. The model may pass its own value, clamped to _BASH_MAX_TIMEOUT.
_BASH_TIMEOUT = 3600
_BASH_MAX_TIMEOUT = 7200  # hard ceiling on a model-supplied Bash timeout
_MAX_FILE = 100_000  # cap on a single file Read — full enough that Edit can match exactly
_MAX_OUTPUT = 30_000  # cap on command / search / fetch output, to keep context bounded

# Directory names Grep/Glob skip when walking from a base: VCS/cache/venv noise plus the
# replicator's own scaffolding (the shallow-cloned reference repo, the paper, phase logs
# under .replicator). Otherwise these crowd out the repo's own source under the hit cap.
# Names are matched relative to the search base, so an explicit `path` into one of them
# (e.g. `.replicator/reference_code`) is still searched in full.
_IGNORE_DIRS = {"__pycache__", ".git", ".venv", ".replicator", "paper"}


def _resolve(repo: Path, file_path: str) -> Path:
    """Resolve `file_path` against the repo root, rejecting escapes outside it.

    Relative paths join the repo root; absolute paths are kept as-is. Either way the
    result must stay within the repo — `../` traversal or an absolute path landing
    outside raises ValueError — so a mis-prompted phase can't read or clobber files
    beyond the generated repo. The returned path keeps the textual repo prefix so
    callers' ``relative_to(repo)`` still works.
    """
    p = Path(file_path)
    full = p if p.is_absolute() else repo / p
    if not full.resolve().is_relative_to(repo.resolve()):
        raise ValueError(f"path {file_path!r} escapes the repo root")
    return full


def _not_ignored(f: Path, base: Path) -> bool:
    """True if `f` is a file not inside an `_IGNORE_DIRS` directory, relative to `base`."""
    return f.is_file() and not _IGNORE_DIRS.intersection(f.relative_to(base).parts)


def _clip_output(text: str) -> str:
    """Clip command output to _MAX_OUTPUT chars, keeping both head and tail.

    A command's verdict lives at the end — the pytest summary line, the final metric, an
    end-of-run traceback — so head-truncation would drop exactly what the judge must see.
    Keep the first and last halves with a marker noting how much was cut.
    """
    if len(text) <= _MAX_OUTPUT:
        return text
    head = _MAX_OUTPUT // 2
    tail = _MAX_OUTPUT - head
    cut = len(text) - _MAX_OUTPUT
    return f"{text[:head]}\n… [{cut} chars truncated] …\n{text[-tail:]}"


def make_tools(repo: Path) -> dict[str, BaseTool]:
    """The eight tools the phases use, bound to ``repo``.

    Keyed by the exact SDK tool names so ``phase.allowed_tools`` and the prompts work
    unchanged. Restricting a phase to a tool subset is just selecting from this dict.
    """
    # Absolutize the repo root so search results render correctly even when the CLI
    # passes a relative path (the default): a model-supplied absolute `path` yields
    # absolute matches, and ``relative_to`` needs both sides to be absolute.
    repo = repo.resolve()

    @tool("Read")
    def read_file(file_path: str) -> str:
        """Read a file's full contents. `file_path` is absolute or relative to the repo root.

        A `.pdf` path is returned as extracted plain text (PyMuPDF), so a phase pointed at
        a raw `paper/*.pdf` gets readable content instead of a binary-decode error.
        """
        try:
            path = _resolve(repo, file_path)
        except ValueError as exc:
            return f"Error: {exc}"
        if not path.exists():
            return f"Error: {file_path} does not exist."
        try:
            if path.suffix.lower() == ".pdf":
                text = pdf_to_text(path)
            else:
                text = path.read_text(encoding="utf-8")
        except Exception as exc:
            return f"Error reading {file_path}: {exc}"
        if len(text) > _MAX_FILE:
            return (
                text[:_MAX_FILE]
                + f"\n\n[truncated: showing first {_MAX_FILE} of {len(text)} chars]"
            )
        return text

    @tool("Write")
    def write_file(file_path: str, content: str) -> str:
        """Write `content` to `file_path`, creating parent directories and overwriting any existing file."""
        try:
            path = _resolve(repo, file_path)
        except ValueError as exc:
            return f"Error: {exc}"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return f"Wrote {len(content)} chars to {file_path}."

    @tool("Edit")
    def edit_file(
        file_path: str, old_string: str, new_string: str, replace_all: bool = False
    ) -> str:
        """Replace `old_string` with `new_string` in `file_path`.

        `old_string` must match the file exactly and must be unique unless `replace_all`
        is true; a non-unique or absent match is rejected so you can add surrounding context.
        """
        try:
            path = _resolve(repo, file_path)
        except ValueError as exc:
            return f"Error: {exc}"
        if not path.exists():
            return f"Error: {file_path} does not exist."
        text = path.read_text(encoding="utf-8")
        count = text.count(old_string)
        if count == 0:
            return f"Error: old_string not found in {file_path}."
        if count > 1 and not replace_all:
            return (
                f"Error: old_string is not unique in {file_path} ({count} matches). "
                "Add surrounding context to make it unique, or set replace_all=true."
            )
        path.write_text(text.replace(old_string, new_string), encoding="utf-8")
        return f"Edited {file_path} ({count if replace_all else 1} occurrence(s))."

    @tool("Bash")
    def bash(command: str, timeout: int = _BASH_TIMEOUT) -> str:
        """Run a shell command from the repo root and return combined stdout/stderr.

        `timeout` is in seconds — raise it for long training/benchmark runs.
        """
        escapee = bash_escape(repo, command)
        if escapee:
            return (
                f"Error: command references '{escapee}', which is outside this replication's "
                f"directory ({repo}). Use relative paths and stay inside your own repo; "
                "never read, lint, run, or modify a sibling replication."
            )
        effective = min(timeout, _BASH_MAX_TIMEOUT)
        try:
            proc = subprocess.run(
                command,
                shell=True,
                cwd=repo,
                capture_output=True,
                text=True,
                timeout=effective,
            )
        except subprocess.TimeoutExpired:
            capped = " (the per-command cap)" if effective < timeout else ""
            return f"Error: command timed out after {effective}s{capped}."
        out = _clip_output((proc.stdout or "") + (proc.stderr or "")) or "(no output)"
        return f"[exit {proc.returncode}]\n{out}"

    @tool("Glob")
    def glob_tool(pattern: str, path: str | None = None) -> str:
        """List files matching a glob `pattern` (e.g. `**/*.py`), newest first. `path` defaults to the repo root."""
        try:
            base = _resolve(repo, path) if path else repo
        except ValueError as exc:
            return f"Error: {exc}"
        matches = [p for p in base.glob(pattern) if _not_ignored(p, base)]
        matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        if not matches:
            return "(no matches)"
        out = "\n".join(str(p.relative_to(repo)) for p in matches[:200])
        if len(matches) > 200:
            out += "\n… (truncated)"
        return out

    @tool("Grep")
    def grep_tool(pattern: str, path: str | None = None, glob: str | None = None) -> str:
        """Search file contents with a regex `pattern`. Optional `path` (dir or file) and `glob` filter (e.g. `*.py`)."""  # noqa: E501
        try:
            rx = re.compile(pattern)
        except re.error as exc:
            return f"Error: invalid regex: {exc}"
        try:
            base = _resolve(repo, path) if path else repo
        except ValueError as exc:
            return f"Error: {exc}"
        files = [base] if base.is_file() else base.rglob(glob or "*")
        hits: list[str] = []
        for f in files:
            if not _not_ignored(f, base):
                continue
            try:
                for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
                    if rx.search(line):
                        hits.append(f"{f.relative_to(repo)}:{i}: {line.strip()[:200]}")
                        if len(hits) >= 200:
                            return "\n".join(hits) + "\n… (truncated)"
            except (UnicodeDecodeError, OSError):
                continue
        return "\n".join(hits) if hits else "(no matches)"

    @tool("WebFetch")
    def web_fetch(url: str) -> str:
        """Fetch a URL over HTTP(S) and return its text content (HTML stripped to plain text)."""
        try:
            resp = httpx.get(
                url,
                follow_redirects=True,
                timeout=30,
                headers={"User-Agent": "Mozilla/5.0 (baseline-replicator)"},
            )
            resp.raise_for_status()
        except Exception as exc:
            return f"Error fetching {url}: {exc}"
        if "html" in resp.headers.get("content-type", ""):
            text = BeautifulSoup(resp.text, "html.parser").get_text("\n", strip=True)
        else:
            text = resp.text
        return text[:_MAX_OUTPUT]

    @tool("WebSearch")
    def web_search(query: str) -> str:
        """Search the web (DuckDuckGo) and return the top results as title / url / snippet blocks."""
        try:
            from ddgs import DDGS
        except ImportError:  # older package name
            from duckduckgo_search import DDGS
        try:
            results = DDGS().text(query, max_results=6)
        except Exception as exc:
            return f"Error searching: {exc}"
        if not results:
            return "(no results)"
        return "\n\n".join(
            f"{r.get('title', '')}\n{r.get('href') or r.get('url', '')}\n{r.get('body', '')}"
            for r in results
        )

    tools = [
        read_file,
        write_file,
        edit_file,
        bash,
        glob_tool,
        grep_tool,
        web_fetch,
        web_search,
    ]
    return {t.name: t for t in tools}


# ── Usage / rendering ───────────────────────────────────────────────────────────


@dataclass
class PhaseUsage:
    label: str
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def cost_usd(self) -> float | None:
        """USD cost from accumulated tokens, or None when the model's price is unknown."""
        price = _PRICE_PER_M.get(self.model)
        if price is None:
            return None
        return self.input_tokens / 1e6 * price[0] + self.output_tokens / 1e6 * price[1]


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


def _short(text: str, limit: int = 500) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit] + " […]"


def _tool_target(args: dict) -> str:
    """A compact hint of what a tool call is acting on, for the progress line."""
    for key in ("file_path", "path", "command", "pattern", "url", "query"):
        if key in args:
            return f" → {_short(str(args[key]), 80)}"
    return ""


def _text_of(msg: AIMessage) -> str:
    """The plain text of an assistant message, whether content is a string or block list."""
    content = msg.content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


def _render(chunk: dict, label: str, log, usage: PhaseUsage) -> None:
    """Print and log one streamed graph update (``stream_mode='updates'``).

    ``chunk`` maps each node name to ``{"messages": [...]}`` for the messages that node
    added this step. We print assistant text and tool calls, and accumulate per-message
    token usage — summed across turns, since LangChain reports usage per message rather
    than a session total.
    """
    for update in chunk.values():
        if not isinstance(update, dict):
            continue
        for msg in update.get("messages", []):
            if isinstance(msg, AIMessage):
                text = _text_of(msg)
                if text.strip():
                    print(f"  [{_now()}] {_short(text)}")
                    log.write(f"\n[assistant] {text}\n")
                for call in msg.tool_calls or []:
                    print(f"  [{_now()}] · {call['name']}{_tool_target(call.get('args', {}))}")
                    log.write(f"[tool] {call['name']} {call.get('args', {})}\n")
                if msg.usage_metadata:
                    usage.input_tokens += msg.usage_metadata.get("input_tokens", 0)
                    usage.output_tokens += msg.usage_metadata.get("output_tokens", 0)
            elif isinstance(msg, ToolMessage):
                log.write(f"[tool-result] {_short(str(msg.content), 1000)}\n")


# ── Phase runners ───────────────────────────────────────────────────────────────


def _build_agent(phase: Phase, repo: Path, model: str, hardware: str, base_url: str | None, **kw):
    """Construct a per-phase ReAct agent: chosen model + this phase's tool subset + system prompt."""
    tools = make_tools(repo)
    return create_agent(
        build_model(model, base_url),
        [tools[name] for name in phase.allowed_tools],
        system_prompt=phase.system_prompt(hardware),
        **kw,
    )


async def run_agent(
    phase: Phase,
    repo: Path,
    model: str,
    hardware: str,
    base_url: str | None = None,
    *,
    label: str | None = None,
    **task_kwargs: str,
) -> PhaseUsage:
    """Run one scoped sub-agent to completion, streaming progress and logging the transcript.

    ``model`` is a resolved ``provider:model`` spec. ``label`` overrides the log filename and
    header (so repeated phases like repair get one log per attempt). ``hardware`` fills the
    ``{{HARDWARE}}`` sentinel; ``task_kwargs`` fill placeholders in the phase task. The
    phase's ``max_turns`` maps to the graph recursion limit (≈ two node steps per turn).
    """
    label = label or phase.name
    log_path = repo / _LOG_DIR_NAME / f"{label}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = log_path.open("w", buffering=1)

    print(f"\n{'=' * 70}\n▶  {label.upper()}  (model: {model})\n{'=' * 70}")
    agent = _build_agent(phase, repo, model, hardware, base_url)
    task = phase.task.format(**task_kwargs)
    usage = PhaseUsage(label=label, model=model)
    config = {"recursion_limit": 2 * phase.max_turns + 1}

    try:
        async for chunk in agent.astream(
            {"messages": [("user", task)]}, config=config, stream_mode="updates"
        ):
            _render(chunk, label, log, usage)
        print(f"  [{_now()}] ✔ {label} finished")
    except GraphRecursionError:
        # The agent produced partial work before hitting the cap; warn and continue
        # rather than crashing — mirrors the SDK's max-turns handling.
        log.write("\n[error] recursion limit (max turns) reached\n")
        print(f"  ⚠  {label} hit its max-turns cap — continuing with whatever it produced.")
    finally:
        log.close()

    return usage


async def run_chat_agent(
    phase: Phase,
    repo: Path,
    model: str,
    hardware: str,
    base_url: str | None = None,
    *,
    paper_sources: str = "",
) -> PhaseUsage:
    """Drive a multi-turn revision conversation over PLAN.md.

    A LangGraph checkpointer plus a fixed ``thread_id`` keep the conversation stateful
    across the user's successive revision requests — the analog of the SDK's stateful
    ``ClaudeSDKClient``: the agent remembers prior turns and edits ``PLAN.md`` /
    ``.replicator/criteria.json`` in place. ``paper_sources`` is the planner's reading-source
    description, injected on the first turn so the reviser knows where the paper lives.
    """
    label = phase.name
    log_path = repo / _LOG_DIR_NAME / f"{label}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = log_path.open("w", buffering=1)
    usage = PhaseUsage(label=label, model=model)

    print(f"\n{'=' * 70}\n▶  {label.upper()}  (model: {model})\n{'=' * 70}")
    print(
        "  Chat to revise PLAN.md and criteria.json. Type a request and press Enter;\n"
        "  the agent edits the plan and reports back. Type 'done' (or an empty line)\n"
        "  when you're finished to return to the approve prompt."
    )
    agent = _build_agent(phase, repo, model, hardware, base_url, checkpointer=InMemorySaver())
    config = {
        "recursion_limit": 2 * phase.max_turns + 1,
        "configurable": {"thread_id": "reviser"},
    }
    # First message tells the agent where the paper lives, like the planner is told.
    first_prefix = (
        f"You are revising the existing PLAN.md and .replicator/criteria.json. For reference, "
        f"{paper_sources}.\n\nMy first request:\n"
    )

    try:
        first = True
        while True:
            try:
                message = input("\nyou › ").strip()
            except EOFError:
                break
            if message.lower() in ("", "done", "exit", "quit"):
                break
            log.write(f"\n[user] {message}\n")
            prompt = first_prefix + message if first else message
            first = False
            try:
                async for chunk in agent.astream(
                    {"messages": [("user", prompt)]}, config=config, stream_mode="updates"
                ):
                    _render(chunk, label, log, usage)
            except GraphRecursionError:
                log.write("\n[error] recursion limit (max turns) reached\n")
                print(f"  ⚠  {label} hit its max-turns cap.")
    finally:
        log.close()

    return usage
