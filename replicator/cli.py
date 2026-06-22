"""Command-line entry point for the baseline replicator.

Usage::

    replicate https://arxiv.org/abs/<id> [--out DIR] [--model MODEL]
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from .paper import (
    copy_local_pdf,
    download_html,
    download_pdf,
    download_pdf_from_url,
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
        help="Override the model for every phase (e.g. 'opus', 'sonnet'). "
        "Default: opus for planning/coding, sonnet for the rest.",
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
    return parser.parse_args(argv)


def _resolve_instructions(value: str | None) -> str:
    if not value:
        return ""
    path = Path(value)
    return path.read_text() if path.is_file() else value


def main(argv: list[str] | None = None) -> None:
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
        print(
            f"HTML:  {html_path}"
            if html_path
            else "HTML:  (none — planner will read the PDF)"
        )
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
        link = source

    (repo / "paper" / "SOURCE.txt").write_text(link + "\n")

    instructions = _resolve_instructions(args.instructions)
    if instructions:
        print(
            f"Instructions: {instructions[:80]}{'…' if len(instructions) > 80 else ''}"
        )
    if args.gpu:
        print("Mode:  GPU (full + verification configs)")
    asyncio.run(
        run_pipeline(repo, model_override=args.model, instructions=instructions, gpu=args.gpu)
    )


if __name__ == "__main__":
    main()
