# Step 5 — Format and finalize → `05_final/`

**Purpose:** Turn raw emitted Python into the **deliverable tree**: black-formatted
modules, package `__init__.py` files, compile checks, and the top-level /
per-stage conversion reports.

**Primary code:**
- `emitter/formatting.py` — `format_python_source` (black)
- `Converter._batch_05_final` / `_check_final_compile` (write + compile-check)
- `Converter._run_timed_step` — terminal progress (`01_raw_ast  3.2s`, …)
- `Converter._generate_init_files`
- `Converter._write_report` / `_write_stage_reports`

**Output directory:** `05_final/` plus root `conversion_report.yaml` and
`<stage>/conversion_report.yaml`

## Acronyms for this stage

| Term | Meaning here |
|------|----------------|
| **JSON** | Report format for counts, stage failures, and parse diagnostics. |
| **UTC** | Default run folder timestamps (`output/YYYYMMDDTHHMMSSZ`). |
| **CLI** | `tektonize report` re-prints the same summary `convert` shows at the end. |

## What it does

1. Run **black** on the emitted source (`format_python_source`). The emitted
   source has already been through the compile-and-fix sanitizer (see step 4),
   so most syntax errors are resolved before formatting. On format failure,
   record a soft-ish stage error and fall back to unformatted text so a file
   still lands on disk.
2. Write `05_final/<python-relative-path>`.
3. After all files: generate missing `__init__.py` files under `05_final/` so
   the tree is importable as packages.
4. **Compile-check** every `05_final/**/*.py` with `py_compile`. Failures are
   **hard**: they flip the file (or a synthetic result) to `failed` and appear
   on the `05_final` stage report.
5. Write reports (YAML via `tektonize.report_io`):
   - Top-level `conversion_report.yaml` — totals, `file_results`,
     `stage_summaries`, `stage_timings_seconds`, skipped tests,
     `total_todo_markers` / `total_groovy_comments` counted **only** from
     `05_final/**/*.py` (not from `01_*`…`04_emitted` intermediates)
   - Per-stage `*/conversion_report.yaml` — each error is
     `{message, details}` (parse stages fill `details` with span/context from
     `parse_diagnostics`)

## Hard vs soft failures (recap)

| Kind | Stages | Effect on overall `failed` |
|------|--------|----------------------------|
| Soft | `01_raw_ast`, `02_typed_ast` (parse / CST) | Listed in stage reports; file can still `succeed` |
| Hard | `04_emitted`, `05_final` (emit / format / compile) | Increments overall `failed` |

## Stage filters

CLI flags `--stages` and `--no-intermediates` control which directories are written
(`Converter.stage_filter`). `--no-intermediates` keeps only `05_final/`. Reports
are still produced for the run.

## End state

Consumers should treat **`05_final/`** as the conversion product: Python modules
describing incomplete Tekton tasks and pipeline fragments, with
`TEKTONIZE-TODO` markers for Tekton/DSL follow-up and `# GROOVY:` comments for
lines that could not be converted to Python. The CLI report prints both counts
from this tree only (`rg TEKTONIZE-TODO output/<run>/05_final`).
