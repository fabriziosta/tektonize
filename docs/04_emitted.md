# Step 4 — Emit Python source → `04_emitted/`

**Purpose:** Produce **raw Python modules** from the typed AST by running the
class/import/method/expression/Jenkins DSL transformers and concatenating a
complete source file string.

**Primary code:**
- `emitter/python_emitter.py` — `PythonEmitter.emit_file`
- Uses transformers from step 3
- Target types — `tektonize.tekton` (`TaskDefinition`, `TaskStep`,
  `PipelineFragment`, `ParamSchema`) imported into generated code where needed

**Output directory:** `04_emitted/` (`.py` files mirroring the library layout,
with Groovy filenames converted via `groovy_filename_to_python`)

## Acronyms for this stage

| Term | Meaning here |
|------|----------------|
| **AST** | Input: typed AST from step 2. |
| **DSL** | Jenkins calls rewritten or marked TODO during emission. |
| **Tekton** | Generated code *describes* incomplete Task / fragment models; it does not apply YAML to a cluster. |

## What it does

1. Build a module docstring pointing at the original Groovy path.
2. Emit `from __future__ import annotations`.
3. Transform imports (`ImportTransformer`) and collect extra imports required by
   enums, ABCs, dataclasses, Tekton models, etc.
4. Transform each class (`ClassTransformer` → `PythonClass`).
5. Field and parameter **default values** go through
   `PythonEmitter._sanitize_literal`, which runs `ExpressionTransformer` on
   complex `[…]` / sentinel-bearing literals so nested Groovy maps become live
   Python dicts (not left for the compile sanitizer to comment as `# GROOVY:`).
6. For each method: unwrap outer `{…}` → `join_open_spans` (delimiter-aware
   logical-line join; see `docs/03_transformed.md`) → `ExpressionTransformer` →
   `JenkinsDSLTransformer` → `MethodTransformer.transform_body`. Expression rewrite protects triple-quoted
   blocks so named-arg `script.sh` / `self.script.sh(script: """…""")` stays intact
   for Jenkins DSL, which emits `# TaskStep` / `# | …` shell body lines (including `set +e`) and
   trailing kwargs. Method transform skips re-running expressions on those
   comment lines. `_is_valid_python_line` aggressively rejects Groovy leftovers:
   unmatched close delimiters (except when inside tracked paren/dict depth),
   shell `\$` fragments, trailing continuation operators, `=~` regex match,
   `.pass` keyword-as-attribute, and single-line Groovy `if (cond) return`.
   Named args are rewritten to kwargs before validity checks.
   `_comment_orphan_clauses` also tracks `match`/`case` stacks to comment out
   orphaned `case` clauses.
7. Emit top-level `vars/`-style functions (`def call(...)` and siblings) when
   present.
8. **Compile-and-fix sanitizer** (`PythonEmitter._sanitize_syntax`): after
   assembling the full source, rewrite invalid `\{` / `\}` escapes, then
   iteratively `compile()` and comment out or trim lines that cause
   `SyntaxError` **or** `SyntaxWarning` (e.g. `return` in `finally`). Handles
   orphaned `try:` blocks (inserts dummy `except Exception: pass`), trailing
   `+`/`\` continuations, and deep-indented orphan code above `def`/`class`
   boundaries. Iterates up to `max(200, line_count)` rounds. Warnings are
   caught so convert does not spam stderr. After sanitizer passes,
   `_drop_orphan_groovy_stubs` removes leftover `# GROOVY: else:` / `pass` /
   `except` / `finally:` / shell-closer stubs that the sanitizer may have
   re-commented after brace unwrap.
9. Write the unformatted source under `04_emitted/<python-relative-path>`.
   Marker totals for the conversion report are **not** taken here — they are
   counted from `05_final/` only (later stages may still rewrite the body).

## Failure semantics

Emit exceptions are **hard** failures: the file is marked `failed`, recorded on
`04_emitted`’s stage report, and the converter returns early for that file
(no `05_final` write for it).

## What “incomplete” means

Emitted methods are expected to return or construct Tekton abstractions
(`TaskDefinition`, …) rather than execute Jenkins. Parameters are declared
schemas, not bound secrets/workspaces. Gaps become `# TEKTONIZE-TODO:` comments.

When method bodies reference Jenkins job params as `params.UPPER_SNAKE` or
`params['name']`, emit harvests those **names** into a module-level unbound
`_PARAM_SCHEMA = ParamSchema(required=[…])` (via `jenkins_bindings`). Values
are never read from a Jenkins controller or written into the Python. Nested /
camelCase `params.foo` map accesses are not harvested.

## Downstream

Step 5 runs **black**, writes `05_final/`, generates `__init__.py`, and
compile-checks the formatted tree.
