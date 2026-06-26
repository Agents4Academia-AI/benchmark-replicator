"""A PreToolUse guard that confines every phase to its own replication directory.

``cwd`` tells the SDK where shell commands start, but it is not a boundary: a phase
can still read, write, or ``rm`` an absolute path anywhere on disk. That is exactly
how a cleaner run on one replication once wandered into a *sibling* replication under
the shared ``replications/`` parent and started re-linting and re-testing it.

This module builds a ``PreToolUse`` hook that denies any tool call whose target path
escapes the repo root. It is enforced by the harness, not merely requested in a prompt,
so it holds even if a phase's instructions don't. File-path tools (Read/Write/Edit/
Glob/Grep) are checked by their path argument; Bash commands are scanned for absolute
paths that point outside the repo.
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


def _inside(root: Path, target: Path) -> bool:
    """True if ``target`` is ``root`` itself or lives under it."""
    return target == root or root in target.parents


# Absolute paths embedded in a shell command: POSIX ``/foo/bar`` and ``~/foo``.
# Deliberately conservative — we only flag *absolute* paths, since relative paths in a
# command run from ``cwd`` (the repo) and stay inside it by construction.
_ABS_PATH = re.compile(r"(?<![\w])(~/[^\s'\"`;|&)]+|/[^\s'\"`;|&)]{2,})")


def _bash_escapes(root: Path, command: str) -> str | None:
    """Return the first absolute path in ``command`` that escapes ``root``, or None."""
    for match in _ABS_PATH.findall(command):
        # Skip the obvious system read-only paths a phase legitimately touches
        # (interpreters, coreutils); they are not replication directories.
        if match.startswith(("/usr/", "/bin/", "/opt/", "/etc/", "/lib", "/System/", "/Library/")):
            continue
        target = _resolve(root, match)
        if not _inside(root, target):
            return match
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
                if not _inside(root, target):
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
