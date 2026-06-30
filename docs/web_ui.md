# Local Web UI

The web UI is a local browser wrapper around the existing `replicate` CLI. It
starts each replication as a background process, streams the CLI output, shows
the generated files, and lets you answer the planning checkpoint from the
browser.

## Start It

After installing the project (see the [README](../README.md)):

```bash
replicator-web
```

Or, without the installed entry point:

```bash
python -m replicator.web
```

Use the second form only when that Python interpreter already has the project
dependencies installed. If the page reports missing `pymupdf` or
`claude-agent-sdk`, install the project into this interpreter with
`pip install -e .` and start it again.

Then open:

```text
http://127.0.0.1:8765
```

## Workflow

1. Paste an arXiv id, arXiv URL, direct PDF URL, or server-local PDF path.
2. Add optional planner instructions.
3. Choose CPU or GPU mode.
4. Start the job.
5. Watch the phase timeline and logs.
6. When the planner pauses, review `PLAN.md`.
7. Approve, stop, or enter revision mode and send plan edits.
8. Inspect `REPORT.md`, `EVAL.md`, `README.md`, and the generated files.
9. Download the generated repo as a zip if desired.

Markdown files are shown in a rendered preview by default, with a `Raw` toggle
available for debugging exact file contents. The preview uses KaTeX for LaTeX
math: inline `$...$` / `\(...\)` and block `$$...$$` / `\[...\]`.

Job state is stored under `.replicator-web/jobs/` and is ignored by git.

## Current Boundary

This is a local-first interface. It runs agent-written code on your machine, the
same as the CLI. A public hosted version would need process isolation, disk and
runtime limits, authentication, cost controls, and per-job sandboxing.
