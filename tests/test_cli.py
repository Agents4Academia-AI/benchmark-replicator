"""Tests for CLI argument parsing and instruction resolution (no pipeline run)."""

from __future__ import annotations

from pathlib import Path

import pytest

from replicator.cli import _parse_args, _resolve_instructions


def test_defaults():
    args = _parse_args(["https://arxiv.org/abs/1706.03762"])
    assert args.url == "https://arxiv.org/abs/1706.03762"
    assert args.provider == "anthropic"
    assert args.yes is False
    assert args.gpu is False
    assert args.out is None
    assert args.model is None


def test_flags_parse():
    args = _parse_args(
        [
            "paper.pdf",
            "--provider",
            "openai",
            "--model",
            "openai:gpt-5.1",
            "-y",
            "--gpu",
            "--out",
            "myout",
        ]
    )
    assert args.provider == "openai"
    assert args.model == "openai:gpt-5.1"
    assert args.yes is True
    assert args.gpu is True
    assert args.out == Path("myout")


def test_invalid_provider_is_rejected():
    with pytest.raises(SystemExit):
        _parse_args(["paper.pdf", "--provider", "nonexistent-provider"])


def test_missing_url_errors():
    with pytest.raises(SystemExit):
        _parse_args([])


def test_resolve_instructions_literal_text():
    assert _resolve_instructions("focus on attention") == "focus on attention"


def test_resolve_instructions_empty():
    assert _resolve_instructions(None) == ""


def test_resolve_instructions_reads_a_file(tmp_path):
    notes = tmp_path / "notes.md"
    notes.write_text("my detailed notes")
    assert _resolve_instructions(str(notes)) == "my detailed notes"
