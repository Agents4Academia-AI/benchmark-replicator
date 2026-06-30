"""Tests for the best-effort Bash escape guard."""

from __future__ import annotations

from replicator.sandbox import bash_escape


def test_repo_local_commands_are_allowed(tmp_path):
    assert bash_escape(tmp_path, "python train.py --out results/") is None
    assert bash_escape(tmp_path, "ls ./src && cat README.md") is None


def test_absolute_outside_path_is_flagged(tmp_path):
    assert bash_escape(tmp_path, "rm -rf /etc/passwd") == "/etc/passwd"


def test_parent_traversal_is_flagged(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    assert bash_escape(repo, "rm -rf ../sibling") is not None


def test_allowed_system_roots_pass(tmp_path):
    assert bash_escape(tmp_path, "python -c 'print(1)' > /dev/null") is None
    assert bash_escape(tmp_path, "ls /usr/bin/python3") is None
    assert bash_escape(tmp_path, "mktemp /tmp/work") is None


def test_remote_urls_are_not_flagged(tmp_path):
    assert bash_escape(tmp_path, "pip install torch") is None
    assert bash_escape(tmp_path, "git clone https://github.com/owner/repo") is None


def test_single_quoted_regex_is_not_flagged(tmp_path):
    assert bash_escape(tmp_path, "grep '/api/v1' app.log") is None


def test_absolute_path_inside_repo_is_allowed(tmp_path):
    inside = (tmp_path / "data").resolve()
    assert bash_escape(tmp_path, f"cat {inside}/file.txt") is None
