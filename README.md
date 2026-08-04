# Benchmark Replicator

[![License: AGPL v3](https://img.shields.io/badge/License-AGPL%20v3-blue.svg)](LICENSE)
[![Python ≥3.10](https://img.shields.io/badge/python-%E2%89%A53.10-blue.svg)](https://www.python.org/downloads/)
[![CI](https://github.com/Agents4Academia-AI/benchmark-replicator/actions/workflows/ci.yml/badge.svg)](https://github.com/Agents4Academia-AI/benchmark-replicator/actions/workflows/ci.yml)

Ever opened a paper's codebase to use, extend, or compare against, and given up
because of how rough, undocumented, or bitrotted it is?

Point this tool at a paper PDF and it acquires a validated baseline. It prefers the
authors' official implementation, escalating only from an unchanged run to environment
repair, a thin adapter, and a minimal patch. If reuse cannot be verified, it builds the
existing clean scratch reimplementation instead.

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
--agent-config FILE      JSON file with per-agent model and reasoning-effort settings
--instructions TEXT|FILE extra instructions for the planner: literal text or a path to a file
--yes, -y                auto-approve the plan and run without stopping at the human checkpoint
--gpu                    GPU mode: ships ambitious paper-scale parameters; must be run inside a GPU allocation
--strategy STRATEGY      reuse-first (default) or scratch
--unsafe-local-official-code
                         execute official repository setup/run commands on this host (unsafe)
```

Use `--instructions` to steer what the planner focuses on before it writes `PLAN.md`:

```bash
replicate https://arxiv.org/abs/1706.03762 --instructions "focus only on scaled dot-product attention, skip multi-head"
replicate https://arxiv.org/abs/1706.03762 --instructions ./my_notes.md
```

Use `--agent-config` to set a model and/or reasoning effort for individual agents. Omitted
fields retain the default model (`gpt-5.6-sol`) and SDK reasoning effort. `--model` still
overrides every configured model, but leaves each configured reasoning effort intact.

```bash
replicate https://arxiv.org/abs/1706.03762 --agent-config ./agents.json
```

```json
{
  "planner": {"model": "gpt-5.6-sol", "reasoning_effort": "high"},
  "coder": {"model": "gpt-5.6-sol", "reasoning_effort": "xhigh"},
  "tester": {"reasoning_effort": "medium"}
}
```

Valid agent names are `planner`, `reviser`, `adoption_inspector`, `environment_fixer`,
`adapter`, `source_patcher`, `coder`, `tester`, `benchmarker`, `repair`, and `cleaner`.
See [`agents.json`](agents.json) for example settings.

> **What to expect:** a full run drives several Codex phases and can take from
> tens of minutes to about an hour. The pipeline pauses for your approval of
> `PLAN.md` before it writes implementation code, so you can stop early if the
> plan looks wrong.

The generated baseline lands in the output directory &mdash; a standalone repo
with its own `README.md`, `PLAN.md`, `REPORT.md`, source, tests, and validated
`baseline.json`. Every successful baseline has one stable invocation:

```bash
bash run.sh --spec run-spec.json --output run-result.json
```

The empty checked-in `run-spec.json` reproduces the verified run, and `bash run.sh` uses those
same default paths. Supported overrides are
listed in `baseline.json`; unknown overrides fail instead of being silently ignored. The
normalized result includes status, seed, numeric/boolean metrics, runtime, and implementation
origin, while `.replicator/results.json` remains available for compatibility. The
pipeline runs the **default** config (the real experiment within budget, roughly
tens of minutes to about an hour on a modern CPU); the repo also ships an
optional **scale-up** config for paper-scale runs.

## How it works

A deterministic Python orchestrator (`replicator/pipeline.py`) owns the fixed adoption
order, attempt bounds, fallback decision, judging, and repair loop. Agents inspect or make
one scoped class of change; they do not choose control flow. Each phase uses a workspace-write
execution boundary. This is a path guard, not a security sandbox, and adopted commands are not
executed by default. To run official setup or entry-point commands directly on the host, pass
`--unsafe-local-official-code`; otherwise reuse-first safely falls back to scratch until a
sandbox backend is available. After planning, the
pipeline **pauses for your approval** of `PLAN.md`
before any code is written. Pass `--yes` to skip this checkpoint for unattended
runs (e.g. batch jobs on an HPC cluster).

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
