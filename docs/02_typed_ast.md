# Step 2 — Extract typed AST → `02_typed_ast/`

**Purpose:** Lift the CST into a **typed abstract syntax tree (AST)** that
names packages, imports, classes, interfaces, enums, fields, methods, and
top-level `vars/` callables — the structure later transformers and the emitter
consume.

**Primary code:**
- `GroovyParser.parse_file` / `parse_source` — `parser/groovy_ast.py`
- Node types — `parser/ast_nodes.py` (`GroovySourceFile`, `ClassDecl`, …)
- Fallback — `parser/fallback_parser.py` (regex recovery when tree-sitter fails)
- Dump helper — `Converter._source_file_to_dict`

**Output directory:** `02_typed_ast/` (`.json` summaries per input file)

## Acronyms for this stage

| Term | Meaning here |
|------|----------------|
| **AST** | Abstract Syntax Tree — language-level nodes (`class`, `method`, `import`), not every brace/token. |
| **CST** | Input shape from step 1; this stage *extracts* meaning from it (and may re-parse). |
| **POJO** | Simple field-heavy classes that step 3/4 will map toward `@dataclass`. |
| **CPS** / **@NonCPS** | Jenkins execution hint; recorded on methods when present and later stripped. |

## What it does

1. Parse the file again via `GroovyParser.parse_file` (tree-sitter walk of
   top-level declarations; same pre-parse normalize as step 1).
2. Populate a `GroovySourceFile`:
   - `package`, `imports`
   - `classes` (`ClassDecl` / `InterfaceDecl` / `EnumDecl`)
   - `top_level_methods` (typical of `vars/*.groovy` `def call(...)`)
   - `parse_errors` (CST summaries, for the per-file JSON dump)
   - `used_fallback` / `fallback_details` when the regex fallback replaces or
     merges into the typed AST
3. If the CST has errors:
   - Run the **fallback regex parser**.
   - Prefer fallback classes when tree-sitter found none.
   - Merge missing fields/methods/extends/implements/constructors/**enum values**
     from fallback when tree-sitter produced a partial class.
   - For `vars/`-style files, adopt fallback top-level methods when needed.
4. Write a JSON view under `02_typed_ast/` (class names, field/method outlines,
   import list, `parse_errors`, fallback flags) for debugging — not a full
   round-trip of every statement body AST.

## Stage report vs step 1

`02_typed_ast/conversion_report.yaml` records **step-2-specific** events only:

- Fallback adoption (`Used fallback regex parser…` with structured `details`)
- Extraction / conversion exceptions attributed to this stage

CST `Parse error` / `Parse missing` lines are **not** re-listed here — they
already appear on `01_raw_ast/conversion_report.yaml` with full `details`.
They remain on the per-file `02_typed_ast/*.json` under `parse_errors`.

## Important behavioral notes

- Method **bodies** remain largely as raw Groovy text on `MethodDecl` nodes;
  deep statement rewriting happens in the method / Jenkins DSL transformers
  during emit (steps 3–4), not as a separate persisted full AST rewrite today.
- Soft failures on this stage (fallback notices) do **not** alone mark the file
  as overall `failed`.

## Downstream

Step 3 records a transform summary; step 4’s emitter applies the transformers
to these typed nodes and produces Python source.
