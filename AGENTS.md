# AGENTS.md

Guidance for AI coding assistants (Cursor, Claude Code, and similar) working in this repository.

## Before You Change Anything

1. **Read and follow project rules.** Obey any Cursor rules under `.cursor/rules/` (including `*.mdc` files), `.cursorrules`, and this `AGENTS.md`. If a rule conflicts with a casual request, prefer the written project rules unless the user explicitly overrides them.
2. **Read [README.md](README.md)** for product intent, CLI usage, and the five-stage conversion pipeline.
3. **Read [docs/](docs/)** for per-step design detail and the glossary (AST, CST, DSL, …).
4. **Keep [README.md](README.md) in sync with program logic.** When you change conversion stages, CLI defaults/flags, output layout, Tekton models, or user-facing behavior, update the README in the same change (TL;DR, mermaid flow, Usage, How Conversion Works, and related sections). See `.cursor/rules/keep-readme-updated.mdc`.
5. **Keep [docs/](docs/) in sync with step logic.** When discovery, parse, transform, emit, format, failure semantics, or stage artifacts change, update the matching `docs/0*.md` file(s) and the index/glossary in the same change. See `.cursor/rules/keep-docs-updated.mdc`.
6. **Prefer small, targeted edits.** Do not refactor unrelated code or invent features outside the request.
7. **Do not invent Jenkins steps or Tekton APIs.** Only use constructs already present in the codebase or clearly documented in Tekton/Jenkins docs the user points to.
8. **Add regression tests for every fix.** When you fix a parse/diagnostic/emit/compile
   (or other) failure, add a test in the same change that locks the fixed behavior
   (see `.cursor/rules/add-regression-tests.mdc` and `tests/test_regression_parse.py`).

## Project Overview

**tektonize** is an upstream Python tool that converts **Jenkins shared-library Groovy** into Python describing **incomplete** Tekton artifacts:

- Reusable **Task** definitions (from step methods)
- Composable **pipeline fragments** (from scenarios / stage graphs)
- **Parameter schemas** (declared, unbound)

It does **not** produce finished `PipelineRun` YAML. Binding params, triggers, and workspaces is left to a future job-invoker converter or manual work.

Stay vendor-neutral: no company, product, or internal branding in tool source, docs, fixtures, or TODO markers. Use `TEKTONIZE-TODO:` (see `tektonize.markers`) for incomplete conversions — easy to search with `rg TEKTONIZE-TODO`.

DSL matching: prefer **structural** `with\w+` / `sh\w+` wrapper detection in
`jenkins_dsl_transformer.py` over exact product method names. Optional
`--dsl-hints` may refine TODO **messages** only. Do not reintroduce org-specific
step allowlists into upstream source or fixtures.

## Scripted vs Declarative Pipelines

`tektonize` accepts any Jenkins shared-library Groovy tree; it does **not** reject Declarative Pipelines. It is **tuned for scripted-style shared libraries**.

- **Primary target:** `src/` classes + thin `vars/` with `def call(...)`, and bodies using `script.sh()` / `script.stage()` / bare `sh` / `echo` / `stage` / `withCredentials` (see `jenkins_dsl_transformer.py`).
- **Limited today:** Declarative DSL (`pipeline { }`, `agent { }`, `stages { }`, `when { }`, `post { }`) is **not specially modeled**. Those constructs mostly become `# GROOVY:` or `# TEKTONIZE-TODO:` rather than structured `PipelineFragment`s.
- Declarative jobs that only wrap a thin `vars/` call while keeping logic in `src/` still convert the library side reasonably; the Declarative shell itself converts poorly.
- Do not claim full Declarative support. New Declarative mappings (`pipeline` / `agent` / `stages` / `when` / `post`) are a deliberate feature addition, not an assumed capability.

## Repository Layout

```text
tektonize/
├── AGENTS.md                 # This file — agent instructions
├── README.md                 # Human-facing docs
├── docs/              # Per-step design docs + glossary (AST/CST/…)
├── pyproject.toml            # Package metadata, CLI entry, pytest/black config
├── Makefile                  # venv-backed install, test, lint, format, convert, clean
├── src/tektonize/            # Application package
│   ├── cli.py                # Click CLI: convert, convert-file, report
│   ├── converter.py          # Orchestrator: parse → transform → emit → format
│   ├── discovery.py          # Select .groovy inputs; skip tests
│   ├── dsl_hints.py          # Optional --dsl-hints YAML/JSON message overlays
│   ├── parser/               # Groovy CST/AST (tree-sitter + fallback)
│   ├── transformer/          # Name, type, class, expression, method, import, Jenkins DSL / bindings
│   ├── emitter/              # Python source emission + black formatting
│   └── tekton/               # TaskDefinition, PipelineFragment, ParamSchema models
├── examples/                 # Sample dsl-hints (synthetic names only)
├── tests/
│   └── fixtures/sample_lib/  # Neutral sample Jenkins shared library (mylib.*)
└── .cursor/
    ├── plans/                # Historical design notes
    └── rules/                # Always-on agent rules (README + docs)
```

### Conversion pipeline stages

Detailed per-step design (including acronyms) lives in [docs/](docs/).
When changing conversion behavior, preserve the staged debug outputs **and**
update the matching design doc:

| Stage | Directory | Content |
|-------|-----------|---------|
| 1 | `01_raw_ast/` | tree-sitter CST as JSON; stage `conversion_report.yaml` errors include `details` |
| 2 | `02_typed_ast/` | Typed AST nodes as JSON |
| 3 | `03_transformed/` | Post-transform AST as JSON |
| 4 | `04_emitted/` | Raw Python (pre-format) |
| 5 | `05_final/` | Formatted Python + `__init__.py` |

Plus top-level `conversion_report.yaml` and per-stage `<stage>/conversion_report.yaml`
(failure lists; `05_final` includes `py_compile` errors). Do **not** write separate
parse-error sidecar files — embed structured `details` on each stage error object.
Parse diagnostics live in `tektonize.parser.parse_diagnostics` and must include
source span + context (not bare `Parse error at line N: ERROR`). Intermediate trees
must mirror the input library’s folder layout. Overall `failed` counts hard
emit/compile failures; parse issues are soft.

## Architecture Rules

- **Package name:** `tektonize` only (`src/tektonize/`). CLI command: `tektonize`.
- **Import handling:** Treat non-stdlib / non-Java / non-Groovy / non-Jenkins imports as generic library packages. Do not special-case a vendor package name.
- **Jenkins DSL:** Map known steps in `jenkins_dsl_transformer.py`; unknown or unsupported steps get `# TEKTONIZE-TODO:` comments — do not silently drop them.
- **Skip tests:** Never convert Groovy under `test/` / `tests/` (relative to the library root) or files named `*Test` / `*Tests` / `*Spec` / `*TestCase` (see `tektonize.discovery`).
- **Output incompleteness:** Converted step methods should return Tekton task abstractions (`TaskDefinition`, etc.), not execute Jenkins. Fragments declare params; they do not bind cluster-specific secrets/workspaces.
- **Jenkins params (names only):** Known Pipeline globals (`params`, `env`, `currentBuild`, `scm`, `docker`) are allowlisted so `params.FOO` / `env.BAR` are not rejected as Groovy. UPPER_SNAKE `params.*` and `params['…']` names are harvested into unbound `ParamSchema`. **Never** load or substitute Jenkins controller/job parameter *values* during convert — that belongs to a future job-invoker.
- **Tests:** Prefer the neutral fixture under `tests/fixtures/sample_lib/`. Do not hardcode paths to private or product-specific shared libraries in tests or the Makefile default.
- **Virtualenv:** Local installs and Makefile targets (`install`, `install-dev`, `test`, `lint`, `format`, `convert`) use `.venv/` only. Never instruct users to `pip install` into the system interpreter.

## Tech Stack

| Component | Detail |
|-----------|--------|
| Language | Python ≥ 3.10 |
| Build | hatchling via `pyproject.toml` |
| CLI | Click (`tektonize` console script) |
| Parsing | `tree-sitter`, `tree-sitter-groovy` (+ regex fallback) |
| Formatting | black (line length 100); ruff `--fix` for import/auto-fixes |
| Linting | ruff, mypy, `python -m compileall` via `make lint` |
| Tests | pytest |

## Essential Commands

Install and local tooling **must** use the project virtualenv (`.venv/`). Prefer
`make install` / `make install-dev` — do not `pip install` into the system Python.

```bash
# Install (creates .venv if needed) and activate in this shell
make install-dev && eval "$(make activate)"

# Tests (runs via .venv; activation not required for make test)
make test
.venv/bin/pytest -v tests/test_transformers.py

# Style / guardrails (compileall + ruff + black --check + mypy)
make format
make lint

# Convert a library
make convert INPUT_DIR=/path/to/jenkins-shared-library
# or with venv on PATH:
tektonize convert --input-dir /path/to/lib
# default --output-dir is ./output/<UTC-timestamp>; override with --output-dir
# or write stage dirs into the library root:
tektonize convert --input-dir /path/to/lib --in-place
tektonize convert-file --input path/to/File.groovy
tektonize report --output-dir ./output/20261016T112045Z
make report OUTPUT_DIR=./output/20261016T112045Z
```

After substantive changes, run `make test` and `make lint`. When the change fixes a
bug, ensure a regression test covers it before considering the work done.

## Coding Conventions

- Match existing module style: `from __future__ import annotations`, type hints, dataclasses where already used.
- Keep transformers focused (one concern per module under `transformer/`).
- Preserve intermediate-stage write paths and metadata headers in the orchestrator unless the user asks to change that contract.
- Do not commit secrets, vault material, or credentials.
- Do not create extra documentation files unless the user asks.
- **README and docs are required for logic changes:** update [README.md](README.md) and the matching [docs/](docs/) step file(s) whenever conversion pipeline stages, discovery, CLI surface, defaults, output directories, or Tekton artifact shapes change. Do not leave those docs describing stale behavior.

## Cursor / Project Rules

- Always apply rules from:
 - `.cursor/rules/**` (if present) — including `keep-readme-updated.mdc`,
   `keep-docs-updated.mdc`, and `add-regression-tests.mdc`
 - `.cursorrules` (if present)
 - This `AGENTS.md`
- Plans under `.cursor/plans/` are historical design notes; they are not executable instructions unless the user attaches them and asks to implement.
- If the user says not to edit a specific plan file, do not modify it.

## Out of Scope (Unless Explicitly Requested)

- Generating complete `PipelineRun` YAML with bound secrets and triggers
- Converting JCasC / Job DSL job invokers (separate future tool)
- Reintroducing vendor- or product-specific naming into this upstream project
- Assuming or advertising full Declarative Pipeline DSL conversion without implementing dedicated transformers for it
