"""Resolve an arXiv URL to a paper id and download its PDF.

Kept deliberately small: only the standard library is used so the orchestrator
has no dependencies beyond the Claude Agent SDK itself.
"""

from __future__ import annotations

import re
import urllib.error
import urllib.request
from pathlib import Path

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
