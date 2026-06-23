# CLAUDE.md

**Baseline Replicator** turns arXiv papers into clean, minimal baseline implementations via a deterministic pipeline of Claude Agent SDK sub-agents. State is shared only through files in the generated repo; the orchestrator controls flow—the model never auto-delegates.

## Pipeline Phases

1. **Planner** (opus): Reads PDF → writes `PLAN.md` with a plan for the most informative
   experiment within the compute budget, `.replicator/criteria.json` — the mechanizable
   criteria as stable ids + comparison + threshold — and `.replicator/artifacts.json` recording
   the official code URL (or null). If official code is found, the planner briefly skims it
   online to ground hyperparameters and architecture details in the plan.
2. **Human checkpoint**: User approves, rejects, or `[c]hat`s to revise `PLAN.md` before any
   code is written. The chat opens the **Reviser** (opus): a stateful `ClaudeSDKClient`
   conversation that edits `PLAN.md` / `.replicator/criteria.json` per the user's requests.
   After approval, the orchestrator shallow-clones the official repo (if any) into
   `.replicator/reference_code/` for downstream phases to read.
3. **Coder** (opus): Implements the method from the plan, using `.replicator/reference_code/`
   as a read-only reference (when present) to cross-check math and hyperparameters. Its entry
   point writes `.replicator/results.json` keyed by the `criteria.json` ids.
4. **Tester** (sonnet): Writes pytest suite, runs tests, writes verdict.
5. **Benchmarker** (sonnet): Runs end-to-end, judges the qualitative criteria, writes `REPORT.md`
   and verdict. The orchestrator then compares `results.json` against `criteria.json`
   *mechanically* and folds any required-criterion failure into the benchmarker verdict.
6. **Repair** (opus, on failure): Reads verdict + paper, fixes bugs. Retried up to `_MAX_REPAIR_ATTEMPTS` (2); pipeline exits with code 1 if judges still fail.
7. **Cleaner** (sonnet): Lints, formats, writes `README.md`.

Judges (tester, benchmarker) only diagnose—they never edit source. Repair is the only phase that edits source.

## Module Organization

```
replicator/
├── cli.py          # Entry point: argument parsing, PDF download trigger
├── paper.py        # arXiv URL parsing, PDF download (stdlib only)
├── phases.py       # Phase dataclass, all phase definitions + tool sets
├── pipeline.py     # Orchestrator: runs phases in sequence, handles checkpoint, repair loop
├── verdict.py      # Verdict/Failure dataclasses, read/write verdict.json
├── criteria.py     # Machine-readable criteria.json/results.json: load, validate, compare
├── __init__.py
└── prompts/        # System prompts for each phase (planner/reviser/coder/tester/benchmarker/repair/cleaner).md
```

## Commands

```bash
uv sync                                                        # install deps
uv run replicate https://arxiv.org/abs/XXXX.XXXXX             # full pipeline (CPU mode)
uv run replicate https://arxiv.org/abs/XXXX.XXXXX --gpu       # GPU mode (run from inside a GPU allocation)
uv run replicate https://arxiv.org/abs/XXXX.XXXXX --out /tmp/test-baseline
uv run replicate https://arxiv.org/abs/XXXX.XXXXX --model sonnet
```

No formal test suite for the orchestrator. Verify changes by running the full pipeline on a small paper (e.g., `1905.13002`).

## Verdict Format

Tester and benchmarker write `.replicator/verdict.json`:

```json
{
  "phase": "tester",
  "status": "pass | fail",
  "failures": [
    { "criterion": "...", "severity": "major | minor", "detail": "...", "evidence": "..." }
  ]
}
```

## Generated Repo Structure

```
<repo>/
├── PLAN.md, README.md, REPORT.md
├── source files (.py)
├── tests/
├── paper/
├── pyproject.toml
└── .replicator/
    ├── logs/              # Phase transcripts: planner.log, coder.log, tester.log, benchmarker.log, cleaner.log, repair-*.log
    ├── artifacts.json     # Planner: official code URL (or null); orchestrator clones it after checkpoint
    ├── reference_code/    # Shallow clone of the official repo (when found); read-only reference for coder/repair/judges
    ├── criteria.json      # Planner: mechanizable success criteria (stable ids, comparison, threshold, required)
    ├── results.json       # Coder's entry point: measured values keyed by criteria.json ids
    └── verdict.json
```

## Criteria contract (machine-readable success criteria)

The planner→coder→benchmarker handoff for measurable criteria is mechanical, not prose-judged:

- **Planner** writes `criteria.json`: `{"criteria": [{"id", "metric", "comparison", "threshold", "required"}]}`. `comparison` is one of `>= <= > < == !=`; `threshold` is a number or boolean. Only criteria reducible to metric+operator+threshold go here; qualitative ones stay as PLAN.md prose.
- **Coder**'s entry point writes `results.json`: a flat dict mapping each criterion `id` to its measured value.
- **Orchestrator** (`criteria.py`) compares the two after the benchmarker runs. A required criterion that fails, has a missing id, or a missing `results.json` becomes a `Failure` folded into the benchmarker verdict → triggers repair. A missing/malformed `criteria.json` is skipped (it's a planner artifact repair can't fix) and the benchmarker's narrative verdict stands.

Generated baselines target the paper's most informative experiment within the compute budget: one main method, a real dataset or task from the paper (or a smaller version within budget).

**CPU mode** (default): the default run completes in roughly tens of minutes to about an hour on a modern CPU. Only one run config is shipped as the "default"; paper-scale configs are documented only.

**GPU mode** (`--gpu`): the default `python train.py` ships ambitious paper-matching parameters (may take hours on GPU). The pipeline itself verifies via a reduced `--quick` config (tens of minutes to ~1 hour on a single GPU). Must be invoked from inside a GPU allocation (e.g. `srun --gpus=1 --pty bash`); the coder also emits a `run_full.sbatch` Slurm template for the user to submit the full run. Hardware profiles (`CPU_PROFILE` / `GPU_PROFILE` in `phases.py`) are injected into every phase's system prompt via the `{{HARDWARE}}` sentinel so prompts read coherently in both modes.
