# Benchmark Replicator

Ever opened a paper's codebase to use, extend, or compare against, and given up
because of how rough, undocumented, or bitrotted it is?

Point this agent at a link to the paper PDF and it builds a clean, minimal,
modular implementation of the method --- code you can actually read, run, and
build on, plus the experiments to reproduce the paper's key results.

## Installation

Requires Python ≥ 3.14.

With [uv](https://docs.astral.sh/uv/) (recommended):

```bash
uv sync          # install into the project's virtual environment
```

With pip:

```bash
pip install .    # or: pip install -e . for an editable install
```

## Usage

With uv:

```bash
uv run replicate https://arxiv.org/abs/<id>
```

The input can be an arXiv URL/id, a direct PDF URL, or a local PDF path:

```bash
uv run replicate https://arxiv.org/abs/<id>      # arXiv (also fetches the HTML rendering when available)
uv run replicate https://example.com/paper.pdf   # any direct PDF URL
uv run replicate ./paper.pdf                     # a local PDF
```

With plain Python (after `pip install .`):

```bash
replicate https://arxiv.org/abs/<id>
# or, without relying on the installed entry point:
python -m replicator.cli https://arxiv.org/abs/<id>
```

Options (all forms):

```
--out DIR                output directory (default: replications/<arxiv-id> or replications/pdf-<hash>)
--model MODEL            override the model for all phases (default: opus for plan/code, sonnet otherwise)
--instructions TEXT|FILE extra instructions for the planner: literal text or a path to a file
```

Use `--instructions` to steer what the planner focuses on before it writes `PLAN.md`:

```bash
uv run replicate https://arxiv.org/abs/1706.03762 --instructions "focus only on scaled dot-product attention, skip multi-head"
uv run replicate https://arxiv.org/abs/1706.03762 --instructions ./my_notes.md
```

The generated baseline lands in the output directory — a standalone repo with its own
`README.md`, `PLAN.md`, `REPORT.md`, source, and tests. The pipeline runs the cheap **smoke**
config; the generated repo also ships a **reference/scale-up** config for researchers to push
toward paper-like experiments. Per-phase transcripts are saved under `<repo>/.replicator/logs/`.

> Run modes today are config files inside the generated repo (smoke vs. reference/scale-up).
> A future CLI `--mode` could distinguish smoke / reference / harness runs directly; it is not
> implemented yet.

## What it does (and doesn't)

- ✅ Implements the paper's **one core method/algorithm** as a faithful, modular reference in
  clean Python (`torch`/`numpy`, plus other common ML deps when justified).
- ✅ Preserves the **real method structure** and exposes realistic config paths — a cheap
  **smoke** config (run by default) and a **reference/scale-up** config closer to the paper.
- ✅ Verifies the method **runs, learns, and respects its invariants** via a cheap smoke run,
  and documents how to scale toward paper-like experiments.
- ✅ Produces tests, an honest benchmark `REPORT.md`, and a `README.md` in the generated repo.
- ❌ Does **not** promise full paper reproduction, exact table numbers, or paper-scale
  training by default.

The default smoke run is intentionally cheap — minutes, on CPU or modest hardware where
feasible. Paper-scale configs may require a GPU; they ship as documented config, not as
something the pipeline runs.

## How it works

A deterministic Python orchestrator (`replicator/pipeline.py`) runs five scoped sub-agents,
each its own [Claude Agent SDK](https://github.com/anthropics/claude-agent-sdk-python)
`query()` with a restricted tool set. They share state through files in the generated repo:

```
paper (arXiv URL/id, PDF URL, or local PDF) → fetch PDF → [planner] → ⏸ you approve PLAN.md ⏸
                                 → [coder] → [tester] → [benchmarker] → [cleaner]
```

| Sub-agent     | Scope                                                              | Writes        |
|---------------|-------------------------------------------------------------------|---------------|
| planner       | read paper, pick the main method, plan a reference impl + run modes| `PLAN.md`     |
| coder         | implement the real method structure cleanly and modularly         | source        |
| tester        | small, fast, deterministic pytest suite (shapes, smoke, invariants)| `tests/`      |
| benchmarker   | run the smoke config, check criteria, report fidelity + gap        | `REPORT.md`   |
| cleaner       | simplify, lint/format, write the repo's README                    | `README.md`   |

After planning, the pipeline **pauses for your approval** of `PLAN.md` before any code is
written.

## Layout

```
replicator/
├── cli.py        # argument parsing + entry point
├── paper.py      # arXiv/PDF URL or local file → PDF (stdlib only)
├── phases.py     # the five sub-agents: scope, tools, prompts, turn caps
├── pipeline.py   # orchestrator: runs phases, streams progress, planning checkpoint
└── prompts/      # one system prompt per sub-agent
```


---

## Acknowledgements

Built during [Agents4Academia](https://github.com/Agents4Academia-AI), 14–26 June 2026.
