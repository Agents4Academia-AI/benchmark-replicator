# Benchmark Replicator

[![License: AGPL v3](https://img.shields.io/badge/License-AGPL%20v3-blue.svg)](LICENSE)
[![Python ≥3.10](https://img.shields.io/badge/python-%E2%89%A53.10-blue.svg)](https://www.python.org/downloads/)
[![CI](https://github.com/Agents4Academia-AI/benchmark-replicator/actions/workflows/ci.yml/badge.svg)](https://github.com/Agents4Academia-AI/benchmark-replicator/actions/workflows/ci.yml)

Ever opened a paper's codebase to use, extend, or compare against, and given up
because of how rough, undocumented, or bitrotted it is?

Point this agent at a link to the paper PDF and it builds a clean, minimal,
modular implementation of the method &mdash; code you can actually read, run, and
build on, plus the experiments to reproduce the paper's key results.

## Demo

A timelapse of the agent turning a paper into a clean, runnable baseline repo:

https://github.com/user-attachments/assets/48e06395-4af0-498c-96d2-8922d719a26d

> **This is the `openai-codex-sdk` branch.** It runs every phase with the native
> [Codex Python SDK](https://developers.openai.com/codex/sdk/), so it does not
> depend on LangChain or LiteLLM. See [`main`](../../tree/main) for the
> provider-agnostic version or [`claude-sdk`](../../tree/claude-sdk) for Claude.

## Installation

Requires Python ≥ 3.10. Install straight from GitHub into a fresh virtual
environment:

```bash
python -m venv .venv && source .venv/bin/activate
pip install "benchmark-replicator @ git+https://github.com/Agents4Academia-AI/benchmark-replicator.git@openai-codex-sdk"
```

Sign in once with Codex. The SDK reuses the same saved authentication as the
Codex CLI, including ChatGPT-managed Codex access:

```bash
codex login
```

## Usage

```bash
replicate https://example.com/paper.pdf   # any PDF URL, or
replicate ./paper.pdf                     # a local PDF
```

Options:

```
--out DIR                output directory (default: replications/<arxiv-id> or replications/pdf-<hash>)
--model MODEL            override the Codex model for all phases (default: gpt-5.6-sol)
--instructions TEXT|FILE extra instructions for the planner: literal text or a path to a file
--yes, -y                auto-approve the plan and run without stopping at the human checkpoint
--gpu                    GPU mode: ships ambitious paper-scale parameters; must be run inside a GPU allocation
```

Use `--instructions` to steer what the planner focuses on before it writes `PLAN.md`:

```bash
replicate https://arxiv.org/abs/1706.03762 --instructions "focus only on scaled dot-product attention, skip multi-head"
replicate https://arxiv.org/abs/1706.03762 --instructions ./my_notes.md
```

> **What to expect:** a full run drives several Codex phases and can take from
> tens of minutes to about an hour. The pipeline pauses for your approval of
> `PLAN.md` before it writes implementation code, so you can stop early if the
> plan looks wrong.

The generated baseline lands in the output directory &mdash; a standalone repo
with its own `README.md`, `PLAN.md`, `REPORT.md`, source, and tests. The
pipeline runs the **default** config (the real experiment within budget, roughly
tens of minutes to about an hour on a modern CPU); the repo also ships an
optional **scale-up** config for paper-scale runs.

## How it works

A deterministic Python orchestrator (`replicator/pipeline.py`) runs a fixed
sequence of scoped Codex agents &mdash; planner, coder, tester, benchmarker,
repair, and cleaner &mdash; that share state through files in the generated
repo. Each phase is an isolated Codex SDK thread, with the generated repository
as its working directory and a workspace-write sandbox. After planning, the
pipeline **pauses for your approval** of `PLAN.md`
before any code is written. Pass `--yes` to skip this checkpoint for unattended
runs (e.g. batch jobs on an HPC cluster).

![Pipeline overview: paper → planner → human checkpoint → coder → verify-and-repair loop → cleaner](docs/pipeline.png)

See **[docs/how_it_works.md](docs/how_it_works.md)** for the full pipeline, the
verify-and-repair loop, and an annotated diagram.

## Contributing

Contributions are welcome &mdash; bug reports, Codex integration improvements, prompt
improvements, and docs. See **[CONTRIBUTING.md](CONTRIBUTING.md)** for the dev
setup, how to run the checks, and the PR process, and
[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) for community expectations.

## License

[AGPL-3.0](LICENSE). In short: it's free and open for everyone, and if you
distribute a modified version &mdash; including running it as a network service
&mdash; you must share your changes under the same license.

## Acknowledgements

Built during [Agents4Academia](https://agents4academia.github.io/) at Oxford, 14–26 June 2026.
