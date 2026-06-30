"""A best-effort guard that keeps each phase's Bash commands inside its own repo.

The ``Bash`` tool (``agent.py``) runs commands with ``cwd`` set to the generated repo, but
``cwd`` is not a boundary: a phase can still read, write, or ``rm`` an absolute path anywhere
on disk. That is exactly how a cleaner run on one replication once wandered into a *sibling*
replication under the shared ``replications/`` parent and started re-linting and re-testing it.

This module scans a shell command for *absolute* paths (or ``..`` traversal) that point
outside the repo root, so the Bash tool can refuse it before running. The file-path tools
(Read/Write/Edit/Glob/Grep) are already confined by ``_resolve`` in ``agent.py``, so only
Bash needs this scan.

It is a best-effort guard against the common accidental case — a phase naming an out-of-repo
absolute path, or a ``..`` that climbs out of the repo — not a security sandbox. Because it
only string-scans the command, it does not stop every escape: a path opened inside a spawned
interpreter (``python -c '...'``) or a bare ``cd`` to ``$HOME`` leaves nothing for the regex
to catch. Treat it as a guardrail that complements the prompts, not a process boundary.
"""

from __future__ import annotations

import re
from pathlib import Path


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

# Single-quoted strings are shell literals — most often a regex/program for awk/grep/sed
# (``awk '/Epoch/{print}'``, ``grep '/api/v1'``) whose leading ``/`` is not a filesystem
# path. Strip them before the absolute-path scan so those idioms aren't wrongly denied.
# (The ``..`` traversal scan below still runs over the raw command.)
_SQUOTED = re.compile(r"'[^']*'")

# A path glued onto a short flag (``cp -t/home/x``, ``tar -C/home/y``) hides the leading
# ``/`` behind a flag letter, defeating ``_ABS_PATH``'s lookbehind. Peel the flag off
# (only at an operand boundary) so the path underneath is scanned.
_GLUED_FLAG = re.compile(r"(?<![\w.])-{1,2}[A-Za-z]+(?=[/~])")

# Out-of-repo paths a phase legitimately references and the guard should not flag:
# system tooling/libraries named by absolute path (interpreters, coreutils, Homebrew
# under ``/opt`` on macOS), kernel/device pseudo-filesystems (``/dev/null`` redirections,
# ``/proc`` and ``/sys`` introspection), and the scratch/cache roots phases write to
# (``/tmp``, ``/var/tmp``, ``~/.cache`` and the standard dataset/weight caches). The
# trailing slashes keep ``/lib/`` from matching ``/library-data``; a bare reference to the
# root itself (``df -h /tmp``) is allowed by ``_is_allowed_outside``. Anything else
# outside the repo is denied.
_ALLOWED_OUTSIDE = (
    "/usr/",
    "/bin/",
    "/lib/",
    "/opt/",
    "/System/",
    "/Library/",
    "/tmp/",
    "/var/",
    "/dev/",
    "/proc/",
    "/sys/",
    "~/.cache/",
    "~/.keras/",
    "~/torch/",
)

# A whitespace/operator-delimited operand, used to spot ``..`` traversal. Each operand is
# also split on ``=`` so a path tucked into ``--out=../x`` is inspected.
_TOKEN = re.compile(r"[^\s'\"`;|&()<>]+")


def _path_tokens(command: str):
    """Yield candidate path operands from ``command`` (splitting ``flag=value`` pairs)."""
    for raw in _TOKEN.findall(command):
        yield from raw.split("=")


def _is_allowed_outside(path: str) -> bool:
    """True if ``path`` is one of the whitelisted out-of-repo roots, or under one.

    Matches both a bare root (``/tmp``, e.g. ``df -h /tmp``) and any path beneath it
    (``/tmp/x``); the entries in ``_ALLOWED_OUTSIDE`` carry a trailing slash so the
    subtree test does not let ``/lib/`` match ``/library-data``.
    """
    return any(path == prefix.rstrip("/") or path.startswith(prefix) for prefix in _ALLOWED_OUTSIDE)


def _bash_escapes(root: Path, command: str) -> str | None:
    """Return the first path in ``command`` that escapes ``root``, or None.

    Two best-effort checks: an absolute or ``~`` path pointing outside the repo, and
    relative ``..`` traversal (``cd ..``, ``find ..``, ``rm -rf ../sibling``) that resolves
    outside it. A repo-local ``./x`` or bare relative path stays inside ``root`` by
    construction and is left alone.
    """
    command = _REMOTE_URL.sub(" ", command)
    abs_scan = _GLUED_FLAG.sub(" ", _SQUOTED.sub(" ", command))
    for match in _ABS_PATH.findall(abs_scan):
        if _is_allowed_outside(match):
            continue
        if not _resolve(root, match).is_relative_to(root):
            return match
    for token in _path_tokens(command):
        if ".." in token.split("/") and not _is_allowed_outside(token):
            if not _resolve(root, token).is_relative_to(root):
                return token
    return None


def bash_escape(repo: Path, command: str) -> str | None:
    """First out-of-repo path the Bash ``command`` references, or None if it stays inside ``repo``."""
    return _bash_escapes(repo.resolve(), command)
