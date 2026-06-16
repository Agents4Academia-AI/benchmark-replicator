"""Definitions of the pipeline's scoped sub-agents.

Each phase is one Claude Agent SDK ``query()`` with its own system prompt, a
restricted tool set, a permission mode, and a turn cap. Phases share state
through files in the generated repo (``PLAN.md``, the source code, ``REPORT.md``).
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

    def system_prompt(self) -> str:
        """Load this phase's system prompt from ``prompts/<name>.md``."""
        return (_PROMPTS_DIR / f"{self.name}.md").read_text()


# The phases. ``{pdf}`` in the planner task and ``{failures}`` in the repair task
# are filled in by the orchestrator. The orchestrator decides the control flow:
# planner → coder → (tester → benchmarker, with repair + re-verify on failure) →
# cleaner. The judging phases (tester, benchmarker) only diagnose and write a
# verdict; the repair phase does the fixing.

PLANNER = Phase(
    name="planner",
    task=(
        "Read the paper PDF at `{pdf}` and write `PLAN.md` for a minimal, CPU-only "
        "smoke-test implementation of its main method, following your instructions."
    ),
    # Planner reads the paper, may search the web for context, writes only PLAN.md.
    allowed_tools=[*_READ_TOOLS, "Write", "WebFetch", "WebSearch"],
    max_turns=40,
)

CODER = Phase(
    name="coder",
    task=(
        "Implement the method described in `PLAN.md` as a clean, minimal, CPU-runnable "
        "repo, following your instructions."
    ),
    allowed_tools=[*_READ_TOOLS, *_WRITE_TOOLS],
)

TESTER = Phase(
    name="tester",
    task=(
        "Write a small, fast, deterministic pytest suite for this implementation, run it, "
        "and write a structured verdict, following your instructions."
    ),
    allowed_tools=[*_READ_TOOLS, *_WRITE_TOOLS],
)

BENCHMARKER = Phase(
    name="benchmarker",
    task=(
        "Run the implementation on CPU, verify each success criterion in `PLAN.md`, write an "
        "honest `REPORT.md` and a structured verdict, following your instructions."
    ),
    allowed_tools=[*_READ_TOOLS, *_WRITE_TOOLS],
)

REPAIR = Phase(
    name="repair",
    task=(
        "A judging phase found the implementation does not yet satisfy the plan. Fix the "
        "root cause of these failures, following your instructions. The paper PDF is at "
        "`{pdf}` — consult it as the authority whenever a failure is about method "
        "correctness (a formula, equation, or algorithm bug).\n\n{failures}"
    ),
    allowed_tools=[*_READ_TOOLS, *_WRITE_TOOLS],
)

CLEANER = Phase(
    name="cleaner",
    task=(
        "Simplify, lint, and format the repo, confirm tests and training still run, and "
        "write `README.md`, following your instructions."
    ),
    allowed_tools=[*_READ_TOOLS, *_WRITE_TOOLS],
)
