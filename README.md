# Benchmark Replicator

> Give it an arXiv paper, get back a **clean, minimal, single-purpose repo**
> that implements the paper's main method -- simple enough to read and trust,
> and easy to drop in as a baseline.

**Team:** Arya · Olga · Sahel
**Day-5 demo targets (Fri 19 Jun):**
1. Eval dataset and pipeline
2. Agent in action

It's built for ML researchers (ICML/NeurIPS/ICLR) who need to compare against methods whose
code is missing, sloppy, or unrunnable.

## What it does (and doesn't)

- ✅ Implements the paper's **one core method/algorithm** in clean Python (+`torch` if needed).
- ✅ Verifies it **runs and learns** on a tiny, CPU-only smoke-test task.
- ✅ Produces tests, an honest benchmark `REPORT.md`, and a `README.md` in the generated repo.
- ❌ Does **not** reproduce full paper results, real datasets, or exact table numbers (v1).

It is intentionally cheap: everything the generated repo runs finishes on a laptop CPU in
minutes.

## How it works

A deterministic Python orchestrator (`replicator/pipeline.py`) runs five scoped sub-agents,
each its own [Claude Agent SDK](https://github.com/anthropics/claude-agent-sdk-python)
`query()` with a restricted tool set. They share state through files in the generated repo:

```
arxiv URL → download PDF → [planner] → ⏸ you approve PLAN.md ⏸
                                 → [coder] → [tester] → [benchmarker] → [cleaner]
```

| Sub-agent     | Scope                                                              | Writes        |
|---------------|-------------------------------------------------------------------|---------------|
| planner       | read paper, pick the main method, plan a CPU smoke test           | `PLAN.md`     |
| coder         | implement it cleanly with minimal deps                            | source        |
| tester        | small, fast, deterministic pytest suite                           | `tests/`      |
| benchmarker   | run it, check success criteria, report the honest gap to the paper| `REPORT.md`   |
| cleaner       | simplify, lint/format, write the repo's README                    | `README.md`   |

After planning, the pipeline **pauses for your approval** of `PLAN.md` before any code is
written.

## Usage

```bash
uv sync
uv run replicate https://arxiv.org/abs/<id>
# options:
#   --out DIR      output directory (default: replications/<arxiv-id>)
#   --model MODEL  override the model for all phases (default: opus for plan/code, sonnet otherwise)
```

The generated baseline lands in `replications/<arxiv-id>/` — a standalone repo with its own
`README.md`, `PLAN.md`, `REPORT.md`, source, and tests. Per-phase transcripts are saved under
`<repo>/.replicator/logs/`.

## Layout

```
replicator/
├── cli.py        # argument parsing + entry point
├── paper.py      # arXiv URL → id, PDF download (stdlib only)
├── phases.py     # the five sub-agents: scope, tools, prompts, turn caps
├── pipeline.py   # orchestrator: runs phases, streams progress, planning checkpoint
└── prompts/      # one system prompt per sub-agent
```


---

## Acknowledgements

Built during [Agents4Academia](https://github.com/Agents4Academia-AI), 14–26 June 2026.
