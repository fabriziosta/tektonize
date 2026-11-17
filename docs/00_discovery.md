# Step 0 — Discovery (select Groovy inputs)

**Purpose:** Decide *which* `.groovy` files enter the conversion pipeline before
any parsing happens.

**Primary code:** `src/tektonize/discovery.py`  
**Called from:** `Converter.run()` (batch) and `convert-file` CLI (single file)  
**Output directory:** none (this step only builds the input file list)

## What it does

1. Walks `--input-dir` recursively for `*.groovy` (`Path.rglob`).
2. **Excludes test sources** so Spock/JUnit and `test/` trees never become Tekton
   fragments:
   - Path segments named `test` or `tests` (any case), **relative to the library
     root** (`--input-dir` or discovered shared-library root).
   - Filename stems ending in `Test`, `Tests`, `Spec`, or `TestCase`
     (e.g. `FooSpec.groovy`, `BarTest.groovy`).
3. Does **not** treat names like `TestScenario.groovy` as tests (stem ends in
   `Scenario`, not `Test`).
4. Records skipped paths on the conversion report as `skipped_test_files` /
   `skipped_tests`.
5. For `convert-file`, resolves a Jenkins shared-library root by walking upward
   until a directory containing `vars/` and/or `src/` is found
   (`find_shared_library_root`), then applies the same exclusion rules. Test
   files are refused with a non-zero exit.

## Why exclusion is relative to the library root

A library may live under a parent folder named `tests` (for example this repo’s
fixture at `tests/fixtures/sample_lib/`). Only path components *under*
`--input-dir` count; parent directories outside the root do not trigger exclusion.

## Soft vs hard outcomes

Discovery failures are not “parse errors.” Skipped tests simply never appear in
`total_files` / stage outputs. There is no per-file stage folder for this step.

## Related acronyms

See [README.md](README.md#glossary-acronyms-and-terms) for **DSL** (Jenkins step
API that later stages will rewrite) and pipeline stage names.
