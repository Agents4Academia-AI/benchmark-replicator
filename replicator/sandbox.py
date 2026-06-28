"""A PreToolUse guard that keeps each phase pointed at its own replication directory.

``cwd`` tells the SDK where shell commands start, but it is not a boundary: a phase
can still read, write, or ``rm`` an absolute path anywhere on disk. That is exactly
how a cleaner run on one replication once wandered into a *sibling* replication under
the shared ``replications/`` parent and started re-linting and re-testing it.

This module builds a ``PreToolUse`` hook that denies any tool call whose target path
escapes the repo root. File-path tools (Read/Write/Edit/Glob/Grep) are checked by their
path argument; Bash commands are scanned for *absolute* paths that point outside the repo.

It is a best-effort guard against the common accidental case — a phase naming an
out-of-repo absolute path, or a ``..`` that climbs out of the repo — not a security
sandbox. Because it only string-scans the command, it does not stop every escape: a path
opened inside a spawned interpreter (``python -c '...'``) or a bare ``cd`` to ``$HOME``
leaves nothing for the regex to catch. Treat it as a guardrail that complements the
prompts, not a process boundary.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Awaitable, Callable


def _resolve(root: Path, raw: str) -> Path:
    """Resolve ``raw`` against ``root`` (for relative paths) into an absolute path."""
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = root / p
    # ``resolve`` collapses ``..`` so ``repo/../simsiam`` cannot slip through.
    return p.resolve()


# Absolute paths embedded in a shell command: POSIX ``/foo/bar`` and ``~/foo``.
# We flag *absolute* paths here; relative ``..`` traversal is handled separately below.
# The ``.`` in the lookbehind keeps a repo-local ``./run.sh`` (or ``../x``) from being
# read as the absolute path ``/run.sh`` — only a ``/`` that truly starts a path (preceded
# by a space, ``=``, quote, …) is matched.
_ABS_PATH = re.compile(r"(?<![\w.])(~/[^\s'\"`;|&)]+|/[^\s'\"`;|&)]{2,})")

# Remote URLs embed a ``//host/path`` that ``_ABS_PATH`` would otherwise read as an
# absolute ``/host/path`` and wrongly deny (a ``git clone https://…`` or ``pip install
# <url>`` is not a filesystem path). Strip them before scanning. ``file://`` is left in
# on purpose, so a ``file:///some/sibling`` path is still checked.
_REMOTE_URL = re.compile(r"\b(?:https?|ftps?|git|ssh|rsync|scp)://[^\s'\"`;|&)]+", re.IGNORECASE)

# Out-of-repo paths a phase legitimately references and the guard should not flag:
# system tooling/libraries named by absolute path (interpreters, coreutils, Homebrew
# under ``/opt`` on macOS) and the scratch/cache roots phases write to (``/tmp``,
# ``/var/tmp``, ``~/.cache``). The trailing slashes keep ``/lib/`` from matching
# ``/library-data``. Anything else outside the repo is denied.
_ALLOWED_OUTSIDE = ("/usr/", "/bin/", "/lib/", "/opt/", "/System/", "/Library/", "/tmp/", "/var/", "~/.cache/")

# A whitespace/operator-delimited operand, used to spot ``..`` traversal. Each operand is
# also split on ``=`` so a path tucked into ``--out=../x`` is inspected.
_TOKEN = re.compile(r"[^\s'\"`;|&()<>]+")


def _path_tokens(command: str):
    """Yield candidate path operands from ``command`` (splitting ``flag=value`` pairs)."""
    for raw in _TOKEN.findall(command):
        yield from raw.split("=")


def _bash_escapes(root: Path, command: str) -> str | None:
    """Return the first path in ``command`` that escapes ``root``, or None.

    Two best-effort checks: an absolute or ``~`` path pointing outside the repo, and
    relative ``..`` traversal (``cd ..``, ``find ..``, ``rm -rf ../sibling``) that resolves
    outside it. A repo-local ``./x`` or bare relative path stays inside ``root`` by
    construction and is left alone.
    """
    command = _REMOTE_URL.sub(" ", command)
    for match in _ABS_PATH.findall(command):
        if match.startswith(_ALLOWED_OUTSIDE):
            continue
        if not _resolve(root, match).is_relative_to(root):
            return match
    for token in _path_tokens(command):
        if ".." in token.split("/") and not token.startswith(_ALLOWED_OUTSIDE):
            if not _resolve(root, token).is_relative_to(root):
                return token
    return None


def make_repo_guard(repo: Path) -> Callable[[Any, str | None, Any], Awaitable[dict]]:
    """Build a PreToolUse hook callback that denies path access outside ``repo``.

    Returned shape matches the SDK's ``HookCallback``: ``async (input, tool_use_id,
    context) -> HookJSONOutput``. On a violation it returns a ``deny`` permission
    decision with a reason the model sees; otherwise an empty dict (no opinion → the
    phase's own ``permission_mode`` applies).
    """
    root = repo.resolve()

    def _deny(reason: str) -> dict:
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }

    async def guard(input: Any, tool_use_id: str | None, context: Any) -> dict:
        tool = input.get("tool_name", "")
        args = input.get("tool_input", {}) or {}

        # File-path tools: one explicit path argument.
        for key in ("file_path", "path", "notebook_path"):
            raw = args.get(key)
            if isinstance(raw, str) and raw.strip():
                target = _resolve(root, raw)
                if not target.is_relative_to(root):
                    return _deny(
                        f"Path '{raw}' is outside this replication's directory ({root}). "
                        "Phases must operate only on their own repo; sibling replications are off limits."
                    )

        # Bash: scan the command string for absolute paths that escape the repo.
        if tool == "Bash":
            command = args.get("command", "")
            if isinstance(command, str):
                escapee = _bash_escapes(root, command)
                if escapee:
                    return _deny(
                        f"Command references '{escapee}', which is outside this replication's "
                        f"directory ({root}). Use relative paths and stay inside your own repo; "
                        "never read, lint, run, or modify a sibling replication."
                    )

        return {}

    return guard
