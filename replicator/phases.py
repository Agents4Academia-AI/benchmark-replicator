"""Definitions of the pipeline's scoped sub-agents.

Each phase is one Claude Agent SDK ``query()`` with its own system prompt, a
restricted tool set, a permission mode, and a turn cap. They run in the order
listed in :data:`PHASES` and share state through files in the generated repo
(``PLAN.md``, the source code, ``REPORT.md``).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

_PROMPTS_DIR = Path(__file__).parent / "prompts"

# Read-only tools every phase may use to orient itself.
_READ_TOOLS = ["Read", "Glob", "Grep"]
# Tools that mutate the repo (the coder onward need these).
_WRITE_TOOLS = ["Write", "Edit", "Bash"]


@dataclass(frozen=True)
class Phase:
    """One scoped sub-agent in the pipeline."""

    name: str
    """Short identifier, e.g. ``"planner"``. Also the prompt filename stem."""

    task: str
    """The per-run instruction handed to the sub-agent as the ``query`` prompt."""

    allowed_tools: list[str]
    """Tools auto-approved for this sub-agent. Everything else is unavailable."""

    permission_mode: str = "acceptEdits"
    """SDK permission mode. The planner only writes PLAN.md; others edit freely."""

    max_turns: int = 80
    """Hard cap on agentic turns, to bound cost."""

    checkpoint_after: bool = False
    """If True, the pipeline pauses for user approval once this phase finishes."""

    def system_prompt(self) -> str:
        """Load this phase's system prompt from ``prompts/<name>.md``."""
        return (_PROMPTS_DIR / f"{self.name}.md").read_text()


# The pipeline, in execution order. ``{pdf}`` in the planner task is filled in
# by the orchestrator with the downloaded paper's path.
PHASES: list[Phase] = [
    Phase(
        name="planner",
        task=(
            "Read the paper PDF at `{pdf}` and write `PLAN.md` for a minimal, CPU-only "
            "smoke-test implementation of its main method, following your instructions."
        ),
        # Planner reads the paper, may search the web for context, writes only PLAN.md.
        allowed_tools=[*_READ_TOOLS, "Write", "WebFetch", "WebSearch"],
        max_turns=40,
        checkpoint_after=True,
    ),
    Phase(
        name="coder",
        task=(
            "Implement the method described in `PLAN.md` as a clean, minimal, CPU-runnable "
            "repo, following your instructions."
        ),
        allowed_tools=[*_READ_TOOLS, *_WRITE_TOOLS],
    ),
    Phase(
        name="tester",
        task=(
            "Write a small, fast, deterministic pytest suite for this implementation and "
            "make it pass, following your instructions."
        ),
        allowed_tools=[*_READ_TOOLS, *_WRITE_TOOLS],
    ),
    Phase(
        name="benchmarker",
        task=(
            "Run the implementation on CPU, verify each success criterion in `PLAN.md`, and "
            "write an honest `REPORT.md`, following your instructions."
        ),
        allowed_tools=[*_READ_TOOLS, *_WRITE_TOOLS],
    ),
    Phase(
        name="cleaner",
        task=(
            "Simplify, lint, and format the repo, confirm tests and training still run, and "
            "write `README.md`, following your instructions."
        ),
        allowed_tools=[*_READ_TOOLS, *_WRITE_TOOLS],
    ),
]
