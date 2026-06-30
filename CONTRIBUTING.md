# Contributing

Thanks for your interest in improving Benchmark Replicator! Bug reports, prompt
improvements, pipeline enhancements, and documentation are all welcome.

> **Heads up:** this is the `claude-sdk` branch, which runs the pipeline on the
> [Claude Agent SDK](https://github.com/anthropics/claude-agent-sdk-python)
> (handy if you'd rather use a Claude subscription than an API key). The
> provider-agnostic version lives on
> [`main`](../../tree/main) — please target your PR at the branch your change
> belongs to.

## Ways to contribute

- **Report a bug** or request a feature via the
  [issue tracker](https://github.com/Agents4Academia-AI/benchmark-replicator/issues).
- **Improve a prompt.** The behaviour of each phase lives in
  [`replicator/prompts/`](replicator/prompts/) as plain Markdown — these are
  first-class to edit, no Python required.
- **Harden a tool or pipeline step** in the `replicator/` package.
- **Improve the docs** — the README, this guide,
  [`docs/how_it_works.md`](docs/how_it_works.md), or
  [`docs/web_ui.md`](docs/web_ui.md).

## Project layout

The pipeline is a deterministic Python orchestrator that runs scoped Claude
Agent SDK sub-agents in a fixed order. Read
**[`docs/how_it_works.md`](docs/how_it_works.md)** for the full architecture; the
short version:

| Path | Role |
|---|---|
| `replicator/pipeline.py` | The orchestrator — decides what runs next (control flow is code, not the model). |
| `replicator/phases.py` | The phase definitions (planner, reviser, coder, tester, …): scope, tools, prompts. |
| `replicator/prompts/*.md` | The system prompt for each phase. |
| `replicator/paper.py` | Fetch/extract the source PDF (arXiv, URL, or local). |
| `replicator/criteria.py`, `verdict.py` | Mechanical pass/fail checking shared with the judging phases. |
| `replicator/sandbox.py` | Best-effort guard keeping a phase's Bash commands inside its repo. |
| `replicator/web.py` | The local browser UI (`replicator-web` entry point). |
| `replicator/cli.py` | Argument parsing + entry point. |

## Dev setup

```bash
git clone https://github.com/Agents4Academia-AI/benchmark-replicator.git
cd benchmark-replicator
git checkout claude-sdk
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"         # the project + ruff (lint/format)
```

## Before you open a PR

Run the same checks CI runs:

```bash
ruff check .             # lint
ruff format .            # auto-format (CI runs `ruff format --check`)
```

There is **no end-to-end test in CI**, because a real run drives a paid LLM (or
your Claude subscription) and takes tens of minutes. So if your change touches
the agents, prompts, or pipeline flow, please **smoke-test a real run locally**
before submitting and note in the PR what you ran:

```bash
./demo_run.sh           # or: replicate https://arxiv.org/abs/<id>
```

## Pull request guidelines

- Branch off `claude-sdk` (or `main` for the provider-agnostic version) and keep
  PRs focused on one change.
- Describe what you changed and why; link any related issue.
- For agent/prompt/pipeline changes, include the paper/command you smoke-tested.
- By contributing, you agree your contributions are licensed under the project's
  [AGPL-3.0](LICENSE) license.
