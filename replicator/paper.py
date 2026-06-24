"""Acquire a paper PDF from an arXiv URL/id, a direct PDF URL, or a local file.

Also pre-extracts PDF text via ``pymupdf`` so the planner has a cheap, clean text
source for any paper — not just arXiv papers that have an HTML rendering. This
intentionally adds ``pymupdf`` as a project dependency (see ``pyproject.toml``);
the module is no longer stdlib-only.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

import pymupdf

# Matches the id in forms like:
#   https://arxiv.org/abs/2017.12345
#   http://arxiv.org/pdf/2017.12345v2
#   arxiv.org/abs/cs/0112017       (old-style id with a category prefix)
#   2017.12345                     (a bare id)
_ARXIV_ID = r"(?:[a-z\-]+/\d{7}|\d{4}\.\d{4,5})(?:v\d+)?"
_URL_RE = re.compile(rf"arxiv\.org/(?:abs|pdf)/(?P<id>{_ARXIV_ID})", re.IGNORECASE)
_BARE_RE = re.compile(rf"^(?P<id>{_ARXIV_ID})$", re.IGNORECASE)

_PDF_URL = "https://arxiv.org/pdf/{id}"
_HTML_URL = "https://arxiv.org/html/{id}"
# arXiv blocks the default urllib user agent; pretend to be a normal browser.
_USER_AGENT = "Mozilla/5.0 (compatible; baseline-replicator/0.1)"


def parse_arxiv_id(url: str) -> str:
    """Extract the arXiv id from a URL or bare id.

    The trailing version suffix (``v2``) is stripped so the same paper always
    maps to the same id. Raises ``ValueError`` if no id can be found.
    """
    text = url.strip()
    match = _URL_RE.search(text) or _BARE_RE.match(text)
    if match is None:
        raise ValueError(f"Could not find an arXiv id in: {url!r}")
    return re.sub(r"v\d+$", "", match.group("id"))


def download_pdf(arxiv_id: str, dest_dir: Path) -> Path:
    """Download the PDF for ``arxiv_id`` into ``dest_dir`` and return its path.

    If the file already exists it is reused, so re-running the pipeline does not
    re-download the paper.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = dest_dir / f"{arxiv_id.replace('/', '_')}.pdf"
    if pdf_path.exists() and pdf_path.stat().st_size > 0:
        return pdf_path

    url = _PDF_URL.format(id=arxiv_id)
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(request, timeout=60) as response:
        data = response.read()
    if not data:
        raise RuntimeError(f"Downloaded an empty PDF from {url}")
    pdf_path.write_bytes(data)
    return pdf_path


def download_html(arxiv_id: str, dest_dir: Path) -> Path | None:
    """Download arXiv's HTML rendering of the paper, if one exists.

    arXiv only publishes HTML for papers with usable LaTeX source (roughly 2023
    onward); many papers have none, in which case this returns None and the
    caller falls back to the PDF. A 404 is the normal "not available" signal.

    If the file already exists it is reused, like :func:`download_pdf`.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    html_path = dest_dir / f"{arxiv_id.replace('/', '_')}.html"
    if html_path.exists() and html_path.stat().st_size > 0:
        return html_path

    url = _HTML_URL.format(id=arxiv_id)
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            data = response.read()
    except urllib.error.URLError:  # HTTPError (404) is a subclass — covers "no HTML"
        return None
    if not data:
        return None
    html_path.write_bytes(data)
    return html_path


def repo_name_for_source(source: str) -> str:
    """Default output-directory name for a non-arXiv source (URL or local path)."""
    digest = hashlib.sha256(source.encode()).hexdigest()[:8]
    return f"pdf-{digest}"


def _pdf_filename_from_url(url: str) -> str:
    """Pick a filename for a downloaded PDF from its URL, falling back to paper.pdf."""
    name = Path(urlparse(url).path).name
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    if not name or not name.lower().endswith(".pdf"):
        name = f"{name}.pdf" if name else "paper.pdf"
    else:
        name = name[: -len(".pdf")] + ".pdf"
    return name


def download_pdf_from_url(url: str, dest_dir: Path) -> Path:
    """Download an arbitrary PDF ``url`` into ``dest_dir`` and return its path.

    Like :func:`download_pdf`, an existing file is reused. The downloaded bytes are
    checked for a PDF header so a URL that returns an HTML error page fails loudly
    instead of feeding garbage to the planner.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = dest_dir / _pdf_filename_from_url(url)
    if pdf_path.exists() and pdf_path.stat().st_size > 0:
        return pdf_path

    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(request, timeout=60) as response:
        data = response.read()
    if not data:
        raise RuntimeError(f"Downloaded an empty PDF from {url}")
    if not data.startswith(b"%PDF-"):
        raise RuntimeError(f"{url} did not return a PDF (no %PDF- header)")
    pdf_path.write_bytes(data)
    return pdf_path


def copy_local_pdf(src: Path, dest_dir: Path) -> Path:
    """Copy a local PDF at ``src`` into ``dest_dir`` and return the new path."""
    if not src.is_file():
        raise FileNotFoundError(f"No such PDF file: {src}")
    if src.read_bytes()[:5] != b"%PDF-":
        raise ValueError(f"{src} is not a PDF (no %PDF- header)")
    dest_dir.mkdir(parents=True, exist_ok=True)
    name = re.sub(r"[^A-Za-z0-9._-]", "_", src.name) or "paper.pdf"
    if not name.lower().endswith(".pdf"):
        name = f"{name}.pdf"
    else:
        name = name[: -len(".pdf")] + ".pdf"
    dest = dest_dir / name
    if not (dest.exists() and dest.stat().st_size > 0):
        shutil.copy2(src, dest)
    return dest


_CLONE_HOSTS = {"github.com", "gitlab.com", "bitbucket.org"}


def clone_reference_code(url: str, dest_dir: Path) -> Path | None:
    """Shallow-clone an official code repository into ``dest_dir`` as a read-only reference.

    Only clones from known code-hosting hosts (GitHub, GitLab, Bitbucket) to avoid
    fetching arbitrary URLs. Returns ``dest_dir`` on success; returns ``None`` on any
    failure (network error, private repo, unsupported host, etc.). Never raises — a
    reference is optional and must not break the pipeline.

    Drops the ``.git`` directory after cloning so the reference does not interfere
    with the generated repo's git history. Reuses an existing non-empty ``dest_dir``
    without re-cloning, so re-running the pipeline is cheap.
    """
    try:
        host = urlparse(url).netloc.lower()
        if host.startswith("www."):
            host = host[4:]
        if host not in _CLONE_HOSTS:
            return None
        if dest_dir.exists() and any(dest_dir.iterdir()):
            return dest_dir  # already cloned on a previous run
        dest_dir.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "clone", "--depth", "1", "--single-branch", url, str(dest_dir)],
            check=True,
            timeout=120,
            capture_output=True,
        )
        git_dir = dest_dir / ".git"
        if git_dir.exists():
            shutil.rmtree(git_dir)
        return dest_dir
    except Exception:
        return None


def pdf_to_text(pdf_path: Path) -> str:
    """Extract and return all plain text from a PDF via ``pymupdf``.

    Raises on a malformed or unreadable PDF (callers decide how to report it). Used
    both by :func:`extract_pdf_text` and by the Read tool, so a phase pointed at a raw
    ``paper/*.pdf`` gets readable text instead of a binary-decode error.
    """
    doc = pymupdf.open(str(pdf_path))
    return "\n\n".join(page.get_text() for page in doc).strip()


def extract_pdf_text(pdf_path: Path) -> Path | None:
    """Extract plain text from ``pdf_path`` and write it alongside the PDF.

    Writes ``<pdf_path.stem>.txt`` in the same directory as ``pdf_path``; if the
    file already exists and is non-empty it is reused without re-extracting.
    Returns the ``.txt`` path on success, or ``None`` if extraction fails or
    yields near-empty output (e.g. scanned/encrypted PDFs) — callers should fall
    back to the PDF in that case. Never raises.
    """
    txt_path = pdf_path.with_suffix(".txt")
    if txt_path.exists() and txt_path.stat().st_size > 100:
        return txt_path
    try:
        text = pdf_to_text(pdf_path)
    except Exception:
        return None
    if len(text) < 500:
        # Too little text — likely a scanned PDF with no extractable content.
        return None
    txt_path.write_text(text, encoding="utf-8")
    return txt_path
