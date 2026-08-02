"""Command-line entry point for the baseline replicator.

Usage::

    replicate https://arxiv.org/abs/<id> [--out DIR] [--model MODEL]
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from .agent import load_agent_settings
from .paper import (
    copy_local_pdf,
    download_html,
    download_pdf,
    download_pdf_from_url,
    extract_pdf_text,
    parse_arxiv_id,
    repo_name_for_source,
)
from .pipeline import run_pipeline


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="replicate",
        description="Turn a paper into a clean, minimal baseline repo.",
    )
    parser.add_argument(
        "url",
        help="arXiv URL/id, a direct PDF URL, or a local PDF path "
        "(e.g. https://arxiv.org/abs/1706.03762).",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Output directory for the generated repo "
        "(default: replications/<arxiv-id> or replications/pdf-<hash>).",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Override the Codex model for every phase (default: gpt-5.6-sol).",
    )
    parser.add_argument(
        "--agent-config",
        type=Path,
        default=None,
        help="JSON file with per-agent model and reasoning_effort settings.",
    )
    parser.add_argument(
        "--instructions",
        default=None,
        help="Extra instructions for the planner: literal text, or a path to a "
        "file whose contents are used (e.g. --instructions notes.md).",
    )
    parser.add_argument(
        "--gpu",
        action="store_true",
        default=False,
        help="Enable GPU mode: the agent targets a CUDA GPU available on this machine. "
        "Ship ambitious paper-scale default configs plus a reduced --quick verification "
        "run for the pipeline. Must be run from inside a GPU allocation on HPC clusters "
        "(e.g. srun --gpus=1 --pty bash). Default: CPU-only.",
    )
    parser.add_argument(
        "--yes",
        "-y",
        action="store_true",
        default=False,
        help="Auto-approve the plan and run the full pipeline without stopping at the "
        "human checkpoint. Useful for unattended/batch runs (e.g. HPC jobs).",
    )
    return parser.parse_args(argv)


def _resolve_instructions(value: str | None) -> str:
    if not value:
        return ""
    path = Path(value)
    return path.read_text() if path.is_file() else value


def main(argv: list[str] | None = None) -> None:
    # Line-buffer stdout/stderr so progress streams to the output file when run
    # non-interactively (e.g. redirected to a Slurm .out), instead of appearing
    # all at once when the full-buffer flushes at exit.
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
    args = _parse_args(argv)
    source = args.url.strip()

    try:
        arxiv_id = parse_arxiv_id(source)
    except ValueError:
        arxiv_id = None

    if arxiv_id is not None:
        repo = args.out or (Path("replications") / arxiv_id.replace("/", "_"))
        repo.mkdir(parents=True, exist_ok=True)
        print(f"Paper: arXiv:{arxiv_id}")
        pdf_path = download_pdf(arxiv_id, repo / "paper")
        print(f"PDF:   {pdf_path}")
        html_path = download_html(arxiv_id, repo / "paper")
        print(f"HTML:  {html_path}" if html_path else "HTML:  (none — planner will read the PDF)")
        txt_path = extract_pdf_text(pdf_path)
        print(f"Text:  {txt_path}" if txt_path else "Text:  (none — planner will read the PDF)")
        link = f"https://arxiv.org/abs/{arxiv_id}"
    else:
        repo = args.out or (Path("replications") / repo_name_for_source(source))
        repo.mkdir(parents=True, exist_ok=True)
        local = Path(source)
        if local.exists():
            print(f"Paper: {local}")
            pdf_path = copy_local_pdf(local, repo / "paper")
        else:
            print(f"Paper: {source}")
            pdf_path = download_pdf_from_url(source, repo / "paper")
        print(f"PDF:   {pdf_path}")
        txt_path = extract_pdf_text(pdf_path)
        print(f"Text:  {txt_path}" if txt_path else "Text:  (none — planner will read the PDF)")
        link = source

    (repo / "paper" / "SOURCE.txt").write_text(link + "\n")

    instructions = _resolve_instructions(args.instructions)
    agent_settings = load_agent_settings(args.agent_config) if args.agent_config else {}
    if instructions:
        print(f"Instructions: {instructions[:80]}{'…' if len(instructions) > 80 else ''}")
    print(f"Agent: Codex{f' (model={args.model})' if args.model else ''}")
    if args.gpu:
        print("Mode:  GPU (full + verification configs)")
    if args.yes:
        print("Mode:  auto-approve (no checkpoint)")
    asyncio.run(
        run_pipeline(
            repo,
            model_override=args.model,
            agent_settings=agent_settings,
            instructions=instructions,
            gpu=args.gpu,
            auto_approve=args.yes,
        )
    )


if __name__ == "__main__":
    main()
