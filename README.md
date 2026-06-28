# Benchmark Replicator

Ever opened a paper's codebase to use, extend, or compare against, and given up
because of how rough, undocumented, or bitrotted it is?

Point this agent at a link to the paper PDF and it builds a clean, minimal,
modular implementation of the method &mdash; code you can actually read, run, and
build on, plus the experiments to reproduce the paper's key results.

## Installation

Requires Python ≥ 3.10.

```bash
python -m venv .venv && source .venv/bin/activate
pip install ".[anthropic]"   # see below for other choices of providers
```

We offer different choices for LLM providers:

| Extra | Provider | Env var |
|---|---|---|
| `anthropic` | Anthropic | `ANTHROPIC_API_KEY` |
| `openai` | OpenAI / OpenAI-compatible local servers | `OPENAI_API_KEY` |
| `google` | Google Gemini | `GOOGLE_API_KEY` |
| `ollama` | Ollama (and other local providers) | &mdash; |
| `all` | All of the above | &mdash; |

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

See **[docs/how_it_works.md](docs/how_it_works.md)** for the full pipeline, the
verify-and-repair loop, and an annotated diagram.

---

## Acknowledgements

Built during [Agents4Academia](https://github.com/Agents4Academia-AI), 14–26 June 2026.
