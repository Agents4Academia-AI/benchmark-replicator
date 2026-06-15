"""Command-line entry point for the baseline replicator.

Usage::

    replicate https://arxiv.org/abs/<id> [--out DIR] [--model MODEL]
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from .paper import download_pdf, parse_arxiv_id
from .pipeline import run_pipeline


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="replicate",
        description="Turn an arXiv paper into a clean, minimal, CPU-runnable baseline repo.",
    )
    parser.add_argument("url", help="arXiv URL or id, e.g. https://arxiv.org/abs/1706.03762")
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Output directory for the generated repo (default: replications/<arxiv-id>).",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Override the model for every phase (e.g. 'opus', 'sonnet'). "
        "Default: opus for planning/coding, sonnet for the rest.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)

    arxiv_id = parse_arxiv_id(args.url)
    repo = args.out or (Path("replications") / arxiv_id.replace("/", "_"))
    repo.mkdir(parents=True, exist_ok=True)

    print(f"Paper: arXiv:{arxiv_id}")
    pdf_path = download_pdf(arxiv_id, repo / "paper")
    print(f"PDF:   {pdf_path}")

    asyncio.run(run_pipeline(repo, model_override=args.model))


if __name__ == "__main__":
    main()
