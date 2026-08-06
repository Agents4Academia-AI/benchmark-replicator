"""Run one scoped workspace task through Benchmark Replicator's LLM backend."""

from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

from .agent import configure_backend, run_agent
from .phases import Phase


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="replicate-agent")
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--prompt-file", type=Path, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument(
        "--provider", choices=("codex", "openrouter", "openai-compatible"), default="openrouter"
    )
    parser.add_argument("--api-key-env", default="OPENROUTER_API_KEY")
    parser.add_argument("--base-url", default="")
    parser.add_argument("--timeout-hardware", default="CPU-only workspace")
    args = parser.parse_args(argv)
    configure_backend(args.provider, os.environ.get(args.api_key_env, ""), args.base_url)
    phase = Phase(
        name="paper_author",
        task="{prompt}",
        allowed_tools=["Read", "Glob", "Grep", "Write", "Edit", "Bash"],
    )
    asyncio.run(
        run_agent(
            phase,
            args.workspace,
            args.model,
            args.timeout_hardware,
            prompt=args.prompt_file.read_text(),
        )
    )


if __name__ == "__main__":
    main()
