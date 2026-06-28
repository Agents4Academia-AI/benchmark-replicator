# How the Baseline Replicator Works

The Baseline Replicator turns an arXiv paper (or any PDF) into a clean, minimal,
modular implementation of its main method. It does this with a **deterministic
Python orchestrator** that runs a fixed sequence of scoped Claude sub-agents. The key
design choice: *the control flow is code, not a model decision*. The model never
auto-delegates or decides what to do next &mdash; the orchestrator (`pipeline.py`) calls each
sub-agent in order, and the sub-agents share state only through files in the generated
repo (`PLAN.md`, the source code, `criteria.json`, `results.json`, `REPORT.md`).

## The agentic loop at a glance

```
                          ┌──────────────────────────────────┐
                          │           ORCHESTRATOR           │
                          │  deterministic Python (pipeline) │
                          │  control flow != model decisions │
                          └──────────────────────────────────┘
                                          │
                                          ▼
                            ╔═══════════════════════════════╗
            paper.pdf ─────▶║     ① PLANNER     (opus)      ║─────▶ PLAN.md
            paper.html ────▶║   most informative experiment ║       + criteria.json
            paper.txt ─────▶║   within budget (CPU or --gpu)║       + artifacts.json
                            ╚═══════════════════════════════╝       (official code URL)
                                          │
                                          ▼
                            ┌─────────────────────────────────────┐
                            │   👤 HUMAN CHECKPOINT                │
                            │   review PLAN.md                     │
                            │   [y]es / [N]o / [c]hat to revise    │
                            │   (--yes auto-approves, no prompt)  │
                            └─────────────────────────────────────┘
                               │ y         │ c            │ N
                               │           ▼              └────────▶ exit(0)
                               │  ╔═══════════════════════╗
                               │  ║  ②  REVISER  (opus)   ║  stateful chat:
                               │  ║  edits PLAN.md +      ║  user revises the plan
                               │  ║  criteria.json        ║  turn-by-turn, then
                               │  ╚═══════════════════════╝  re-presents checkpoint
                               │           │
                               │◀──────────┘ (loop back to checkpoint)
                               ▼
                            ┌─────────────────────────────────────────┐
                            │  ORCHESTRATOR clones official code repo,│
                            │  if artifacts.json gave a URL →         │
                            │  .replicator/reference_code/ (read-only)│
                            └─────────────────────────────────────────┘
                                          │
                                          ▼
        ╭───────────────────────────────────────────────────────────────────────╮
        │  ③ CODER  (opus):  N sequential phases parsed from PLAN.md  (max 4)   │
        │                                                                       │
        │   for each Phase n of N (from the plan's Implementation Phases):      │
        │     ╔════════════════════════════╗                                    │
        │     ║   ③ CODER  (opus)           ║  implements that phase's scope    │
        │     ║   Phase n of N              ║  (reads reference_code/ when the  │
        │     ╚════════════════════════════╝   official repo was cloned)        │
        │              │                                                        │
        │     n < N ───┤   ast syntax-check, then run the next phase (loop up)  │
        │              │                                                        │
        │     n = N ───┴─▶ final phase wires entry point, writes results.json   │
        ╰─────────────────────────────────┬─────────────────────────────────────╯
                                          ▼
                            src/   +   .replicator/results.json
                                          │
        ╭───────────────────────────────────────────────────────────────────────
        │  ④ VERIFY-AND-REPAIR LOOP            (max 2 repair attempts)          │
        │                                                                       │
        │   clear verdict.json before each judge; judges DIAGNOSE, never fix    │
        │                                                                       │
        │     ╔═══════════════════════╗      verdict.json                       │
        │     ║  ④a TESTER  (sonnet)  ║─────▶ pass / fail ──┐                   │
        │     ╚═══════════════════════╝   (writes test suite)│                  │
        │                │ PASS                               │ FAIL            │
        │                ▼                                    │                 │
        │     ╔═══════════════════════╗                       │                 │
        │     ║④b BENCHMARKER (sonnet)║──▶ verdict.json       │                 │
        │     ╚═══════════════════════╝   (writes REPORT.md)  │                 │
        │                │                                    │                 │
        │                ▼                                    │                 │
        │     ┌───────────────────────────────┐               │                 │
        │     │ MECHANICAL CHECK (Python)      │ pass/fail    │                 │
        │     │ criteria.json vs results.json  │─────▶ ─┐     │                 │
        │     │ required failure → fold in     │        │     │                 │
        │     └───────────────────────────────┘         │     │                 │
        │                │ PASS                  FAIL   ▼     ▼                 │
        │                │                      ╔═══════════════════════╗       │
        │                │            failures  ║   ④c REPAIR  (opus)   ║       │
        │                │            + paper ─▶║  fix root cause; only ║       │
        │                │                      ║  agent that edits src ║       │
        │                │                      ╚═══════════════════════╝       │
        │                │                                    │                 │
        │                │                    attempts < 2 ?  │                 │
        │                │                       yes ◀────────┤                 │
        │                │                       (re-run judges from ④a)        │
        │                │                        no ─────────┴──▶ exit(1)      │
        │                │                                    failures stand    │
        │   both judges PASS                                                    │
        ╰────────────────┼──────────────────────────────────────────────────────╯
                         ▼
            ╔═════════════════════════╗
            ║   ⑤ CLEANER  (sonnet)   ║─────▶ tidy src/ + README.md
            ╚═════════════════════════╝       (ruff, re-run tests)
                         │
                         ▼
                    ✅  DONE
```

## Entry point and paper acquisition

The CLI (`cli.py`, exposed as `replicate`) accepts an arXiv URL/id, a direct PDF URL, or
a local PDF path. `paper.py` (standard library only) resolves the source and populates a
`paper/` folder inside the output repo:

- **arXiv input** →  downloads the **PDF** and, when available, arXiv's **HTML rendering**.
  HTML exists only for papers with usable LaTeX source (~2023 onward); when present it is
  preferred because its text and equations are cleaner and far cheaper to read than PDF
  page-images. The PDF remains authoritative and is the only source with figures.
- **Direct PDF URL / local PDF** → downloads or copies the PDF (validated by a `%PDF-`
  header). No HTML in this case.

The source link is recorded in `paper/SOURCE.txt`. Optional `--instructions` (literal text
or a file path) are passed through to the planner. `--provider` selects the LLM provider
(Anthropic by default; OpenAI, Google, or a local model), `--model` overrides every phase's
model with a single `provider:model` id, and `--base-url` points at a local OpenAI-compatible
server (vLLM / llama.cpp). The CLI then hands the prepared repo to `run_pipeline` (`pipeline.py`).

## How each phase runs

Every phase is one independent LangGraph tool-calling agent with a **fresh context**
(`run_agent` in `agent.py`). A phase (`phases.py`) is a small dataclass bundling:

- a **system prompt** loaded from `prompts/<name>.md`,
- a **task** string (the per-run instruction; placeholders like `{sources}`, `{pdf}`,
  `{failures}` are filled by the orchestrator),
- an **allowed tool set** (everything else is unavailable to that agent), and
- a **turn cap** (`max_turns`, mapped to the graph recursion limit) to bound cost.

The tools (`Read`, `Write`, `Edit`, `Bash`, `Glob`, `Grep`, `WebFetch`, `WebSearch`) are
defined in `agent.py` and bound to the generated repo. As each phase streams, the orchestrator
prints progress lines and writes a full transcript to `.replicator/logs/<phase>.log`. Per-phase
token usage is summed from each message's `usage_metadata`, and a USD cost is shown when the
model's price is known &mdash; reported in a cost summary table at the end.

**Default models** (`agent.py`): each phase is a *strong* or *cheap* tier &mdash; strong for
the hard reasoning steps (planner, reviser, coder, repair), cheap for the rest (tester,
benchmarker, cleaner). `--provider` maps those tiers to that provider's default pair (e.g.
opus/sonnet for Anthropic); a `--model` flag overrides every phase with one model id.

## The phases in detail

### ① Planner (opus)
Reads the paper (HTML preferred, PDF authoritative) and writes two files:

- **`PLAN.md`** &mdash; a plan for the paper's most informative CPU-feasible experiment (roughly
  tens of minutes to about an hour on a modern CPU): one method, the real dataset or task from
  the paper (or a smaller version if needed), and success criteria targeting the paper's
  qualitative result at that scale. Qualitative criteria stay here as prose.
- **`.replicator/criteria.json`** &mdash; the *mechanizable* subset of success criteria, each as a
  stable `id`, a `metric` description, a `comparison` operator (`>= <= > < == !=`), a numeric
  or boolean `threshold`, and a `required` flag.

Tools: read tools + `Write` + web search/fetch (for context). It does **not** write code.

### Human checkpoint
The orchestrator prints `PLAN.md` and asks the user to approve (`_checkpoint`):

- **`[y]es`** → proceed to coding.
- **`[N]o`** → stop cleanly (`exit(0)`); the plan remains on disk.
- **`[c]hat`** → open the **Reviser** to revise the plan, then re-present the checkpoint.

No code is written until the plan is approved.

### ② Reviser (opus) &mdash; only on `[c]hat`
Unlike the one-shot phases, this is a **stateful** conversation kept across turns by a
LangGraph checkpointer (`run_chat_agent`). The user types successive revision requests; the agent remembers the
conversation and edits `PLAN.md` / `.replicator/criteria.json` in place (it has `Edit` in
addition to the planner's tools). The first message tells it where the paper lives. Typing
`done`/`exit`/`quit` or an empty line returns to the approve prompt with the updated plan.

### ③ Coder (opus)
Implements the method from `PLAN.md` as a clean, minimal, CPU-runnable repo. Its entry point
must write **`.replicator/results.json`** &mdash; a flat dict mapping each `criteria.json` `id` to
its measured value. Tools: read + write (`Write`, `Edit`, `Bash`).

### ④ Verify-and-repair loop
Run by `_verify_and_repair`, this is the only loop in the pipeline (max
`_MAX_REPAIR_ATTEMPTS = 2` repair attempts). Each round:

1. **Clear `verdict.json`**, then run the **Tester (sonnet)** &mdash; it writes a small, fast,
   deterministic pytest suite, runs it, and writes a structured verdict. If it fails, skip to
   repair.
2. Clear `verdict.json`, run the **Benchmarker (sonnet)** &mdash; it runs the implementation
   end-to-end on CPU, judges the qualitative `PLAN.md` criteria, writes `REPORT.md`, and
   writes a structured verdict.
3. **Mechanical check (Python, not the LLM)** &mdash; `criteria.py` compares `results.json`
   against `criteria.json` deterministically. Any *required* criterion that fails (or whose
   value is missing, or with a missing `results.json`) becomes a `Failure` folded into the
   benchmarker's verdict, forcing a FAIL regardless of what the benchmarker concluded. If no
   valid `criteria.json` exists, the check is skipped and the benchmarker's narrative verdict
   stands.

The judges **only diagnose** &mdash; they never edit source. Verdict handling
(`verdict.py`) is deliberately strict: a missing file, malformed JSON, a phase mismatch, an
unrecognized status, or a "pass" that still lists failures **all resolve to FAIL**. Silently
assuming a pass is the pipeline's biggest false-success risk, so anything short of a clean,
well-formed `pass` triggers repair.

If a round produces a failure verdict and the repair budget isn't spent:

- **④c Repair (opus)** &mdash; the *only* phase that edits source. It receives the recorded
  failures (formatted as a markdown brief) and the paper PDF path, and is told to consult the
  paper as the authority for any method-correctness bug (a formula, equation, or algorithm
  error). After repair, the **whole round restarts from the tester** so the fix is
  re-verified.

The loop returns success once both judges pass within the budget. If failures remain after 2
repair attempts, the pipeline stops before cleanup with `exit(1)`, leaving `REPORT.md` and
`verdict.json` describing the outstanding failures honestly.

### ⑤ Cleaner (sonnet)
Runs only over an implementation that passed judging. It simplifies, lints, and formats the
repo (ruff), confirms tests and training still run, and writes `README.md`.

## State and artifacts

All inter-phase state lives in files inside the generated repo:

```
<repo>/
├── PLAN.md, README.md, REPORT.md
├── source files (.py)
├── tests/
├── paper/             # PDF, optional HTML, SOURCE.txt
├── pyproject.toml
└── .replicator/
    ├── logs/          # one transcript per phase (repair-1.log, repair-2.log, …)
    ├── criteria.json  # planner: mechanizable success criteria
    ├── results.json   # coder's entry point: measured values, keyed by criteria ids
    └── verdict.json   # current judging phase's structured verdict
```

The contract that makes the planner → coder → benchmarker handoff mechanical rather than
prose-judged is the pair `criteria.json` (what to measure and the pass threshold) and
`results.json` (the measured values). Their comparison in `criteria.py` is pure Python &mdash; a
numeric or boolean criterion provably passes or provably fails, with no LLM judgement in the
loop.

## Design principles, summarized

- **Code drives control flow.** The orchestrator decides the sequence; the model never
  auto-delegates.
- **Scoped agents.** Each phase has only the tools and context it needs, with a fresh
  context per run.
- **Separation of diagnosis and repair.** Judges never edit source; only Repair does. A
  "pass" is therefore an independent judgement, not self-graded.
- **Mechanical where possible.** Numeric/boolean criteria are checked deterministically in
  Python; only genuinely qualitative criteria are left to an LLM judge.
- **Fail loud, not silent.** Any malformed or ambiguous verdict resolves to FAIL, and an
  unrepairable implementation exits non-zero with its failures intact.
```
