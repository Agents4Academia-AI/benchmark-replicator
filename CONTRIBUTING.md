# Contributing

Thanks for your interest in improving Benchmark Replicator! Bug reports, provider
integrations, prompt improvements, and documentation are all welcome.

## Ways to contribute

- **Report a bug** or request a feature via the
  [issue tracker](https://github.com/Agents4Academia-AI/benchmark-replicator/issues).
- **Improve a prompt.** The behaviour of each phase lives in
  [`replicator/prompts/`](replicator/prompts/) as plain Markdown — these are
  first-class to edit, no Python required.
- **Add or harden a provider, tool, or pipeline step** in the `replicator/`
  package.
- **Improve the docs** — the README, this guide, or
  [`docs/how_it_works.md`](docs/how_it_works.md).

## Project layout

The pipeline is a deterministic Python orchestrator that runs scoped LLM
sub-agents in a fixed order. Read
**[`docs/how_it_works.md`](docs/how_it_works.md)** for the full architecture; the
short version:

| Path | Role |
|---|---|
| `replicator/pipeline.py` | The orchestrator — decides what runs next (control flow is code, not the model). |
| `replicator/agent.py` | Provider-agnostic LangGraph agent backend + the tool set. |
| `replicator/phases.py` | The phase definitions (planner, coder, tester, …). |
| `replicator/prompts/*.md` | The system prompt for each phase. |
| `replicator/paper.py` | Fetch/extract the source PDF (arXiv, URL, or local). |
| `replicator/criteria.py`, `verdict.py` | Mechanical pass/fail checking shared with the judging phases. |
| `replicator/sandbox.py` | Best-effort guard keeping a phase's Bash commands inside its repo. |

## Dev setup

```bash
git clone https://github.com/Agents4Academia-AI/benchmark-replicator.git
cd benchmark-replicator
python -m venv .venv && source .venv/bin/activate
pip install -e ".[anthropic,dev]"   # swap anthropic for your provider
```

The `dev` extra adds [ruff](https://docs.astral.sh/ruff/) (lint + format) and
[pytest](https://docs.pytest.org/).

## Before you open a PR

Run the same checks CI runs:

```bash
ruff check .            # lint
ruff format .           # auto-format (CI runs `ruff format --check`)
pytest                  # unit tests
```

The test suite covers the **deterministic** modules only (criteria, verdict,
sandbox, paper parsing, CLI) and makes no network or LLM calls — so CI runs
without any API key.

There is **no end-to-end test in CI**, because a real run calls a paid LLM and
takes tens of minutes. So if your change touches the agents, prompts, or
pipeline flow, please **smoke-test a real run locally** before submitting and
note in the PR what you ran:

```bash
./demo_run.sh           # or: replicate https://arxiv.org/abs/<id>
```

## Pull request guidelines

- Branch off `main` and keep PRs focused on one change.
- Describe what you changed and why; link any related issue.
- For agent/prompt/pipeline changes, include the paper/command you smoke-tested.
- By contributing, you agree your contributions are licensed under the project's
  [AGPL-3.0](LICENSE) license.
