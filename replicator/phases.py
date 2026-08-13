"""Definitions of the pipeline's scoped sub-agents.

Each phase is one isolated Codex thread with its own system prompt and scoped
capabilities. Phases share state through files in the generated repo
(``PLAN.md``, the source code, ``REPORT.md``).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

_PROMPTS_DIR = Path(__file__).parent / "prompts"

# Hardware profile injected into every phase's system prompt via the {{HARDWARE}} sentinel.
# CPU_PROFILE reproduces today's CPU-only behaviour byte-for-byte.
# GPU_PROFILE enables the two-config GPU model (full paper-scale default + reduced
# verification run for the pipeline itself).
CPU_PROFILE = (
    "**Faithful by default, within a CPU budget.** The default run should reproduce the"
    " paper's most informative experiment that completes on a modern multi-core CPU within"
    " **roughly tens of minutes to about one hour**. If the real experiment is too costly,"
    " reproduce a smaller-but-real version (fewer steps, smaller data, same algorithm). Only"
    " fall back to a synthetic toy when the paper's experiments genuinely require a GPU or"
    " large data downloads with no CPU-feasible version. No large downloads in the default"
    " path. The default run is what the entry point executes, what writes"
    " `.replicator/results.json`, and what the benchmarker runs. Calibrate"
    " `.replicator/criteria.json` to this within-budget run, not paper-scale numbers."
)

GPU_PROFILE = (
    "**Use the GPU — full and verification configs.** Target hardware is a single CUDA GPU."
    " The pipeline is running on a GPU node (launched from inside a GPU allocation;"
    " `python train.py` and the benchmarker run locally on this machine). Ship **two**"
    " experiment configs alongside the fast test config:\n"
    "  - **FULL config** (`python train.py`, no extra args): ambitious parameters matching"
    " the paper's actual experiment (model size, dataset, steps). May take hours on the GPU;"
    " this is what a user clones and runs to reproduce the paper. Document it prominently.\n"
    "  - **VERIFICATION config** (e.g. `python train.py --quick`): a clearly documented"
    " reduced setting that finishes within roughly tens of minutes to about one hour on a"
    " single GPU, with a hard step or time cap so it cannot run away.\n"
    "\n"
    "  Contract: the entry point writes `.replicator/results.json` **only from the"
    " VERIFICATION run** (not the full config). The benchmarker runs the **VERIFICATION"
    " command** — never the full config. Calibrate `.replicator/criteria.json` thresholds"
    " to the VERIFICATION run (sanity-level signals: loss decreases, metric beats trivial"
    " baseline), not paper headline numbers. Assert `torch.cuda.is_available()` at"
    " entry-point start; fail loudly with a clear message if no GPU is found. Also emit a"
    " `run_full.sbatch` Slurm batch script for the FULL config with placeholder fields"
    " (`--partition`, `--account`, `--time`, `--gpus`) — the user submits this; the"
    " pipeline never runs it. No large downloads in the default path."
)


def hardware_profile(gpu: bool) -> str:
    """The hardware profile string to inject into every phase's system prompt."""
    return GPU_PROFILE if gpu else CPU_PROFILE


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
    """The per-run instruction handed to the sub-agent as its first user message."""

    allowed_tools: list[str]
    """Tools this sub-agent may call. Everything else is unavailable."""

    def system_prompt(self, hardware: str) -> str:
        """Load this phase's system prompt from ``prompts/<name>.md``.

        Fills the ``{{HARDWARE}}`` sentinel with the chosen hardware profile so
        each phase reads as one coherent voice — no contradictory CPU prose left
        in the system prompt when GPU mode is active.
        """
        text = (_PROMPTS_DIR / f"{self.name}.md").read_text()
        if "{{HARDWARE}}" not in text:
            raise ValueError(f"prompt {self.name}.md is missing the {{{{HARDWARE}}}} sentinel")
        return text.replace("{{HARDWARE}}", hardware)


# The phases. ``{sources}`` and ``{instructions}`` in the planner task, ``{pdf}`` and
# ``{failures}`` in the repair task are filled in by the orchestrator. The orchestrator
# decides the control flow: planner → bounded official-code adoption or coder →
# (tester → benchmarker, with repair + re-verify on failure) → cleaner. The judging
# phases (tester, benchmarker) only diagnose and write a verdict.

PLANNER = Phase(
    name="planner",
    task=(
        "Read the paper ({sources}) and write `PLAN.md` plus `.replicator/criteria.json` for "
        "a faithful implementation that reproduces the paper's most informative experiment "
        "within the compute budget, following your instructions.{instructions}"
    ),
    # Planner reads the paper, may search the web for context, writes only PLAN.md.
    allowed_tools=[*_READ_TOOLS, "Write", "WebFetch", "WebSearch"],
)

REVISER = Phase(
    name="reviser",
    # Empty: the checkpoint chat loop drives this phase turn-by-turn with the user's
    # live messages, not a templated task string formatted once up front.
    task="",
    # Like the planner (paper + web) plus Edit, for surgical changes to the existing
    # PLAN.md and criteria.json.
    allowed_tools=[*_READ_TOOLS, "Write", "Edit", "WebFetch", "WebSearch"],
)

ADOPTION_INSPECTOR = Phase(
    name="adoption_inspector",
    task=(
        "Inspect this untouched official-code working copy and write "
        "`.replicator/adoption-candidate.json`, following your instructions. Do not run or "
        "modify official files."
    ),
    allowed_tools=[*_READ_TOOLS, "Write"],
)

ENVIRONMENT_FIXER = Phase(
    name="environment_fixer",
    task=(
        "The unchanged official-code invocation failed. Make one bounded dependency or "
        "environment-only repair in this working copy, then update "
        "`.replicator/adoption-candidate.json`, following your instructions.\n\n{failure}"
    ),
    allowed_tools=[*_READ_TOOLS, *_WRITE_TOOLS],
)

ADAPTER = Phase(
    name="adapter",
    task=(
        "The official invocation still failed. Add only a thin invocation and metric-extraction "
        "adapter in this working copy, then update "
        "`.replicator/adoption-candidate.json`, following your instructions.\n\n{failure}"
    ),
    allowed_tools=[*_READ_TOOLS, *_WRITE_TOOLS],
)

SOURCE_PATCHER = Phase(
    name="source_patcher",
    task=(
        "Environment and adapter attempts failed. Apply one minimal source patch to this "
        "working copy, then update "
        "`.replicator/adoption-candidate.json`, following your instructions.\n\n{failure}"
    ),
    allowed_tools=[*_READ_TOOLS, *_WRITE_TOOLS],
)

CODER = Phase(
    name="coder",
    task=(
        "Implement the method described in `PLAN.md` as a clean, minimal repo, "
        "following your instructions.{reference}\n\n{phase_instruction}"
    ),
    allowed_tools=[*_READ_TOOLS, *_WRITE_TOOLS],
)

TESTER = Phase(
    name="tester",
    task=(
        "Write a small, fast, deterministic pytest suite for this implementation, run it, "
        "and write a structured verdict, following your instructions.{reference}"
    ),
    allowed_tools=[*_READ_TOOLS, *_WRITE_TOOLS],
)

BENCHMARKER = Phase(
    name="benchmarker",
    task=(
        "Run the implementation, verify each success criterion in `PLAN.md`, write an "
        "honest `REPORT.md` and a structured verdict, following your instructions.{reference}"
    ),
    allowed_tools=[*_READ_TOOLS, *_WRITE_TOOLS],
)

REPAIR = Phase(
    name="repair",
    task=(
        "A judging phase found the implementation does not yet satisfy the plan. Fix the "
        "root cause of these failures, following your instructions. The paper PDF is at "
        "`{pdf}` — consult it as the authority whenever a failure is about method "
        "correctness (a formula, equation, or algorithm bug).{reference}\n\n{failures}"
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
