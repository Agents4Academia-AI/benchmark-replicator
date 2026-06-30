"""Tests for the offline parts of paper acquisition (no network)."""

from __future__ import annotations

import pytest

from replicator.paper import copy_local_pdf, parse_arxiv_id, repo_name_for_source


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://arxiv.org/abs/1706.03762", "1706.03762"),
        ("http://arxiv.org/pdf/1706.03762v5", "1706.03762"),
        ("arxiv.org/abs/cs/0112017", "cs/0112017"),
        ("1706.03762", "1706.03762"),
        ("2310.12345v2", "2310.12345"),
        ("  https://arxiv.org/abs/1706.03762  ", "1706.03762"),
    ],
)
def test_parse_arxiv_id(url, expected):
    assert parse_arxiv_id(url) == expected


@pytest.mark.parametrize(
    "bad",
    [
        "https://example.com/paper.pdf",
        "not-an-id",
        "https://arxiv.org/list/cs.LG/recent",
    ],
)
def test_parse_arxiv_id_rejects_non_ids(bad):
    with pytest.raises(ValueError):
        parse_arxiv_id(bad)


def test_repo_name_for_source_is_deterministic():
    a = repo_name_for_source("https://example.com/paper.pdf")
    b = repo_name_for_source("https://example.com/paper.pdf")
    assert a == b
    assert a.startswith("pdf-")
    assert len(a) == len("pdf-") + 8


def test_repo_name_for_source_differs_by_input():
    assert repo_name_for_source("a") != repo_name_for_source("b")


def test_copy_local_pdf_rejects_missing(tmp_path):
    with pytest.raises(FileNotFoundError):
        copy_local_pdf(tmp_path / "nope.pdf", tmp_path / "out")


def test_copy_local_pdf_rejects_non_pdf(tmp_path):
    fake = tmp_path / "fake.pdf"
    fake.write_bytes(b"not a pdf at all")
    with pytest.raises(ValueError):
        copy_local_pdf(fake, tmp_path / "out")


def test_copy_local_pdf_copies_valid_pdf(tmp_path):
    src = tmp_path / "mypaper.pdf"
    src.write_bytes(b"%PDF-1.4\n%minimal pdf\n")
    dest = copy_local_pdf(src, tmp_path / "out")
    assert dest.exists()
    assert dest.suffix == ".pdf"
    assert dest.read_bytes().startswith(b"%PDF-")
