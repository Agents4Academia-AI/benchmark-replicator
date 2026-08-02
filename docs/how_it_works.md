# How the Baseline Replicator Works

The Baseline Replicator is a reuse-first acquisition tool. A deterministic Python
orchestrator owns every branch and bound; model phases inspect a paper or make one scoped
change, but never choose the next stage.

## Pipeline

```text
paper → planner → human checkpoint (--yes skips the prompt)
                         │
                         ├─ --strategy scratch ─────────────────────────────┐
                         │                                                  │
                         └─ reuse-first                                     │
                              │                                             │
                              ├─ no confirmed official code ────────────────┤
                              │                                             │
                              └─ preserve immutable official source         │
                                   │                                        │
                                   ├─ 1. run unchanged (one execution)      │
                                   ├─ 2. environment fix (one turn + run)   │
                                   ├─ 3. thin adapter (one turn + run)      │
                                   └─ 4. minimal patch (one turn + run)     │
                                          │                                 │
                            candidate runs?                                 │
                              │ yes                  no ─────────────────────┤
                              ▼                                             ▼
                    isolated adoption verification                 scratch coder phases
                              │                                             │
                              └──────── tester → benchmarker ───────────────┘
                                                │
                                      failure → repair (max 2)
                                                │
                              adoption still fails? → discard staging and
                                                        run scratch coder
                                                │
                                              cleaner
                                                │
                            validate baseline.json + stable runner
```

The official-code levels are cumulative and fixed. Environment repair may change only
dependency/environment files. The adapter may add listed files but may not edit official
source. A source patch is limited to five files and 400 unified-diff lines. There is one
model turn and one command execution per adoption level, and each local command has a
timeout. The existing tester/benchmarker/repair loop remains bounded at two repairs.

`--strategy scratch` bypasses every adoption phase and retains the original planner,
checkpoint, phased Coder, judging, repair, and Cleaner behavior. CPU/GPU profiles, `--yes`,
mechanical criteria, and the interactive plan revision checkpoint apply to both strategies.

## Official source isolation and provenance

Confirmed official code is cloned to `.replicator/reference_code/`. The orchestrator records
the repository URL and HEAD commit before removing git metadata; this directory is then used
only as the immutable comparison/reference tree and its filesystem permissions are made
read-only. Every attempted change is made in
`.replicator/official_working/`.

After a candidate command runs, judging happens in
`.replicator/adoption_verification/`, a disposable nested output repo. Only a candidate that
passes Tester, Benchmarker, mechanical criteria, bounded Repair, Cleaner, manifest validation,
and the stable runner is promoted. If verification fails, the whole nested repo and working
copy are removed before the scratch Coder starts. Tests, reports, source, and generated runner
files from incomplete adoption therefore cannot contaminate the fallback.

`.replicator/adoption.json` records:

- strategy and whether execution was local;
- official URL, commit SHA, detected license, and UTC retrieval time;
- each stage, command, return code, runtime, and scrubbed failure;
- environment changes, adapter files, source modifications, and a unified source patch;
- the final implementation origin or the scratch fallback.

Common credential keys and values are redacted before this file is written. The orchestrator
does not persist process environment variables, and credential-looking environment variables
are removed before implementation commands run. Commands run locally without a shell. The
Codex SDK phases use a workspace-write execution boundary; this is a path guard, not a
security sandbox.

## Planning and human checkpoint

The Planner reads arXiv HTML or extracted text when available, with the PDF authoritative for
figures and ambiguities. It writes:

- `PLAN.md`: the method, verified experiment, compute budget, fidelity decisions, success
  criteria, and possible scratch implementation phases;
- `.replicator/criteria.json`: numeric/boolean criteria for deterministic comparison;
- `.replicator/artifacts.json`: a confirmed official code URL or `null`.

The orchestrator presents `PLAN.md` before acquisition or coding. `[c]hat` invokes the Reviser,
`[y]es` continues, and `[N]o` stops. `--yes` auto-approves for unattended CPU or GPU jobs.

When official code exists, the read-only Official Code Inspector derives a candidate argv,
result location/format, metric map, default seed, and only the overrides the official entry
point really supports. Subsequent agents each receive the last concrete failure and may make
only their stage's class of change.

## Verification and repair

The same verification path judges adopted and reimplemented outputs:

1. Tester writes and runs focused deterministic tests, exercises the stable runner, and writes
   `.replicator/verdict.json`.
2. Benchmarker runs `bash run.sh --spec run-spec.json --output run-result.json`, writes
   `REPORT.md` and `EVAL.md`, and records a verdict.
3. Python compares `.replicator/criteria.json` with `.replicator/results.json`. Missing,
   malformed, non-numeric/non-boolean, or failing required metrics force failure.
4. Repair receives the exact failures and may try a root-cause fix. Both judges run again.

A missing or contradictory verdict is a failure. If an adopted candidate still fails after
the repair budget, Python discards it and starts scratch. If scratch still fails, the command
exits non-zero before cleanup. Cleaner runs only after both judges pass.

## Portable output contract

Every successful output has a validated root-level `baseline.json` with:

- schema/status, method name, and implementation origin;
- paper URL and official code URL/commit when available;
- stable command and normalized result path/format;
- supported overrides and their types/flags;
- local hardware/environment facts;
- modifications and verification status/command;
- an internal argv/cwd/result mapping used by the runner.

The origins are `official_unmodified`, `official_environment_fixed`, `official_adapted`,
`official_patched`, or `reimplemented`.

Every baseline runs through exactly:

```bash
bash run.sh --spec run-spec.json --output run-result.json
```

The checked-in default spec is `{}` and reproduces the verified run. Supported values such as
seed, dataset, or split are converted to real command flags. Unknown or incorrectly typed
overrides fail with a clear error; they are never ignored. A successful normalized result has
`status`, `seed`, a flat object of numeric/boolean `metrics`, `runtime_seconds`, and
`implementation_origin`. The runner also writes the same flat metrics to
`.replicator/results.json` for compatibility with the mechanical criteria check.

## Output layout

```text
<repo>/
├── baseline.json
├── run.sh
├── run-spec.json
├── run-result.json
├── PLAN.md
├── README.md
├── REPORT.md
├── EVAL.md
├── official/                    # present for adopted outputs
├── source/tests/configs         # scratch output, or adoption-facing tests/docs
├── paper/
└── .replicator/
    ├── adoption.json
    ├── criteria.json
    ├── results.json
    ├── verdict.json
    ├── reference_code/          # immutable official reference when found
    ├── run.py                   # self-contained stable runner
    └── logs/
```

## Design principles

- Python owns control flow, bounds, policy checks, promotion, and fallback.
- Reuse escalates from zero source changes to the smallest necessary source patch.
- Official provenance and modifications remain auditable.
- Judges diagnose independently; Repair changes implementation code.
- Numeric and boolean acceptance criteria are checked mechanically.
- Anything short of a validated manifest and reproduced stable run is not a success.
