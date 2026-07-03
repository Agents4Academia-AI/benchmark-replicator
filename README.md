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

https://github.com/Agents4Academia-AI/benchmark-replicator/raw/main/docs/timelapse.mp4

## Installation

Requires Python ≥ 3.10. Install straight from GitHub into a fresh virtual
environment:

```bash
python -m venv .venv && source .venv/bin/activate
pip install "benchmark-replicator[anthropic] @ git+https://github.com/Agents4Academia-AI/benchmark-replicator.git"
```

Swap `anthropic` for whichever provider you want to use:

| Extra | Provider | Env var |
|---|---|---|
| `anthropic` | Anthropic | `ANTHROPIC_API_KEY` |
| `openai` | OpenAI / OpenAI-compatible local servers | `OPENAI_API_KEY` |
| `google` | Google Gemini | `GOOGLE_API_KEY` |
| `ollama` | Ollama (and other local providers) | &mdash; |
| `all` | All of the above | &mdash; |

Then set the API key for your provider (a local Ollama model needs none):

```bash
export ANTHROPIC_API_KEY=...   # or OPENAI_API_KEY / GOOGLE_API_KEY
```

> **Want to hack on the replicator itself?** Clone it and install in editable
> mode instead &mdash; see [CONTRIBUTING.md](CONTRIBUTING.md).

> **Claude subscription users:** if you'd rather use your Claude monthly plan instead of an API key, check out the [`claude-sdk` branch](../../tree/claude-sdk).

## Usage

```bash
replicate https://example.com/paper.pdf   # any PDF URL, or
replicate ./paper.pdf                     # a local PDF
```

Options:

```
--out DIR                output directory (default: replications/<arxiv-id> or replications/pdf-<hash>)
--model MODEL            override the model for all phases (default: opus for plan/code, sonnet otherwise)
--instructions TEXT|FILE extra instructions for the planner: literal text or a path to a file
--yes, -y                auto-approve the plan and run without stopping at the human checkpoint
--gpu                    GPU mode: ships ambitious paper-scale parameters; must be run inside a GPU allocation
```

Use `--instructions` to steer what the planner focuses on before it writes `PLAN.md`:

```bash
replicate https://arxiv.org/abs/1706.03762 --instructions "focus only on scaled dot-product attention, skip multi-head"
replicate https://arxiv.org/abs/1706.03762 --instructions ./my_notes.md
```

> **What to expect:** a full run drives several LLM phases &mdash; a strong model
> for planning and coding, a cheaper one for the rest &mdash; so a single paper
> takes from tens of minutes to about an hour and, on a hosted provider, costs a
> few dollars in API usage. Point `--provider` at a local model (Ollama / vLLM)
> to avoid API cost. The pipeline pauses for your approval of `PLAN.md` before it
> writes any code, so you can stop early if the plan looks wrong.

The generated baseline lands in the output directory &mdash; a standalone repo
with its own `README.md`, `PLAN.md`, `REPORT.md`, source, and tests. The
pipeline runs the **default** config (the real experiment within budget, roughly
tens of minutes to about an hour on a modern CPU); the repo also ships an
optional **scale-up** config for paper-scale runs.

## How it works

A deterministic Python orchestrator (`replicator/pipeline.py`) runs a fixed
sequence of scoped LLM sub-agents &mdash; planner, coder, tester, benchmarker,
repair, and cleaner &mdash; that share state through files in the generated
repo. Each sub-agent is a [LangGraph](https://github.com/langchain-ai/langgraph)
tool-calling agent over the provider you choose via
[LangChain](https://github.com/langchain-ai/langchain)'s `init_chat_model`:
Anthropic (default), OpenAI, Google, or a local model (ollama / llama.cpp /
vLLM). After planning, the pipeline **pauses for your approval** of `PLAN.md`
before any code is written. Pass `--yes` to skip this checkpoint for unattended
runs (e.g. batch jobs on an HPC cluster).

```mermaid
flowchart TD
    subgraph legend [" "]
        direction LR
        L1["higher-reasoning agents"]:::higher
        L2["lower-reasoning agents"]:::lower
        L3["orchestrator &amp; I/O"]:::io
    end

    Paper["<b>Paper</b><br>PDF · HTML · text"]:::io
    Planner["<b>① Planner</b><br>writes the plan"]:::higher
    Checkpoint["<b>Human checkpoint</b><br>approve or revise"]:::io
    Reviser["<b>② Reviser</b><br>revise on chat"]:::higher
    Coder["<b>③ Coder</b><br>builds the repo"]:::higher

    subgraph loop ["④ Verify &amp; repair loop — judges diagnose · max 2 repairs"]
        Tester["<b>④a Tester</b><br>writes &amp; runs tests"]:::lower
        Bench["<b>④b Benchmarker</b><br>runs end-to-end"]:::lower
        Mech["<b>Mechanical check</b><br>criteria vs results"]:::io
        Repair["<b>④c Repair</b><br>fixes root cause, then re-verify"]:::higher

        Tester --> Bench --> Mech
        Bench -->|fail| Repair
        Repair -->|re-run| Tester
    end

    Cleaner["<b>⑤ Cleaner</b><br>lint, format, README"]:::lower
    Done["<b>Done</b><br>clean baseline repo"]:::io

    Paper --> Planner --> Checkpoint
    Checkpoint -->|"on [c]hat"| Reviser
    Reviser --> Checkpoint
    Checkpoint --> Coder
    Coder -->|results.json| Tester
    Mech -->|judges pass| Cleaner
    Cleaner --> Done

    classDef higher fill:#E7E4FB,stroke:#B7B0EE,color:#3D2C8D
    classDef lower fill:#D6F0E0,stroke:#9BD9B8,color:#1B6B45
    classDef io fill:#ECE7DE,stroke:#D4CCBE,color:#3A3A3A
```

See **[docs/how_it_works.md](docs/how_it_works.md)** for the full pipeline, the
verify-and-repair loop, and an annotated diagram.

## Contributing

Contributions are welcome &mdash; bug reports, provider integrations, prompt
improvements, and docs. See **[CONTRIBUTING.md](CONTRIBUTING.md)** for the dev
setup, how to run the checks, and the PR process, and
[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) for community expectations.

## License

[AGPL-3.0](LICENSE). In short: it's free and open for everyone, and if you
distribute a modified version &mdash; including running it as a network service
&mdash; you must share your changes under the same license.

---

## Acknowledgements

Built during [Agents4Academia](https://github.com/Agents4Academia-AI), 14–26 June 2026.
