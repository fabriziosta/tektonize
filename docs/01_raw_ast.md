# Step 1 — Parse (raw CST → `01_raw_ast/`)

**Purpose:** Turn Groovy source text into a **concrete syntax tree (CST)** using
tree-sitter, dump it for inspection, and collect detailed parse diagnostics.

**Primary code:**
- `GroovyParser.get_raw_tree` / `_node_to_dict` — `parser/groovy_ast.py`
- `collect_cst_parse_issues` — `parser/parse_diagnostics.py`
- Orchestration — `Converter._convert_file` stage `01_raw_ast`

**Output directory:** `01_raw_ast/` (mirrors input layout, `.json` per file)

## Acronyms for this stage

| Term | Meaning here |
|------|----------------|
| **CST** | Concrete Syntax Tree — full parse tree from tree-sitter (tokens, structure, `ERROR` / missing nodes). |
| **AST** | Abstract Syntax Tree — *not* what this stage writes. Typed AST extraction is step 2. |
| **ACS** | Unused. Do not invent an “ACS” stage; say CST or AST. |

**Naming note:** The folder is called `01_raw_ast` for historical reasons. The
payload is a **CST** serialized as JSON (`type`, `start`, `end`, optional
`text` / `children`, `is_missing`).

## What it does

1. Read the Groovy file as UTF-8 text.
2. Normalize known tree-sitter-groovy footguns via
   `normalize_groovy_for_treesitter` in `parser/normalize.py` (in-memory only —
   **not** a staged pipeline step). Today:
   - empty map `[:]` → `__TEKTONIZE_EMPTY_MAP__`
   - empty list `[]` → `__TEKTONIZE_EMPTY_LIST__` (not `Type[]` array types; emit as `[]`)
   - empty strings `''` / `""` → `__TEKTONIZE_EMPTY_STR__` (emit as `""`)
   - after empty-* sentinels, `= true` / `= false`, and dotted constants
     (`= Type.CONST`): insert `;` only when the next line starts a new member
     (not before `]` / `}` in map/list literals)
   - bare typed fields without initializer (`Map config`, `protected Object script`)
     → terminate with `;`
   - backslash line continuations (`assert x, \\\n "msg"`) → joined onto one line
   - multiline dot chains `expr\n .method()` → joined onto the preceding line
     (only when continuation is indented and preceding line ends with a chainable
     token)
   - Java-style for-each `for (Type var : iterable)` → `for (var in iterable)`
     (handles `def`, simple types, and qualified generics like `Map.Entry<K, V>`)
   - multiline ternary `cond\n ? a\n : b`, `cond\n ? a : b`, and
     `cond ?\n a :\n b` → joined onto one line (EOL-`?` join is line-based so
     `:` inside strings is not mistaken for the ternary colon)
   - multiline elvis `a\n ?: b` and `a ?:\n b` (both directions) → joined onto
     one line (before `?:` → sentinel rewrite)
   - slashy regex after `=~` / `==~`, method args (`replaceAll(/…/)`),
     assignments (`= /…/`), ternary arms (`: /…/`), and `~/…/` → quoted pattern
   - safe-navigation `?.` / `?.method(...)` / `?."${key}"` →
     `__TEKTONIZE_SAFE_NAV__` / `__TEKTONIZE_SAFE_CALL__`
   - elvis `?:` → `__TEKTONIZE_ELVIS__(left, right)` (up to 512 per file);
     right operand stops before `//` EOL comments
   - cast `expr as Type` / `expr as Type<…>` → `__TEKTONIZE_AS__(expr, "Type")`
     (skips `import pkg.Type as Alias`)
   - method pointer `obj.&method` → `__TEKTONIZE_METHOD_PTR__(obj, "method")`
   - field access `obj.@field` → `obj.field`
   - `assert expr, "msg"` → `assert expr : "msg"`
   - untyped method formals (`printParams(params)`) → `def params` (tree-sitter
     treats bare names as types)
   - GString nested double quotes inside `${…}` → single quotes; `"'${expr}'"`
     wrappers → `"'" + (expr) + "'"` concat
   - enum: insert `;` after the last constant when fields/methods follow; add `;`
     on enum instance fields (e.g. `private final String tier`)
   - closure: recurse inner `{ … }` first; wrap statement bodies after `->` in
     `{ … }` only for single-line / ``if / else`` chains (not multi-statement
     bodies with separate ``if { return }`` guards)
   - closure: strip typed parameters (`Map tier ->` → `tier ->`) and typed locals
     (`String x =` → `x =`) inside closure bodies — tree-sitter misparses them
     in nested ``each { … }``; emit does not rely on those types
   - ``{ return }`` / ``{ continue }`` / ``{ break }`` one-liners → with semicolon
   - bare ``assert expr`` (including trailing `//` comments) → terminate with ``;``
   - Jenkins command-style calls (no parentheses):
     - named args (including multiline after `,`) → map literal
       (`build job: x, wait: false` → `build([job: x, wait: false])`)
     - positional args only for a whitelist of steps (`echo`, `println`,
       `parallel`, `sh`, …) — never wrap types/fields like `Map config`
     - ``script.echo "…"`` / ``echo lines.join(…)`` / ``this.script.parallel stages``
       → parenthesized
   - ``echo|script.echo result ? a : b`` (joined ternary) → parenthesized
   Emitters undo sentinels (`ExpressionTransformer` / `TypeMapper` → `{}`,
   `or`, `getattr`, drop cast, …).

   **Still soft / often unfixable without richer rewrites:** shell text inside
   multiline ``script.sh("""…""")`` strings (``echo`` tokens are string content,
   not Groovy calls), and a few large files that still force the step-2 regex
   fallback when a root-level ERROR remains.
3. Parse with **tree-sitter-groovy** (`tree_sitter` + `tree_sitter_groovy`).
4. Recursively serialize the root `Node` to a JSON-friendly dict:
   - Leaf nodes include `text`.
   - Interior nodes include `children`.
   - Missing nodes set `is_missing: true`.
5. Walk the CST for `ERROR` / missing nodes; build rich **ParseIssue** records
   (span, text, source line, caret, context, child types, ancestors).
   **Noise filtered out** (not reported):
   - missing `;` (optional in Groovy)
   - nested `ERROR` children under a parent `ERROR`
   - stray `:` inside `array_literal` (empty-map fallout if normalize missed)
   - lone `\` tokens from shell line-continuations
6. Write per-file intermediate JSON under `01_raw_ast/<relative-path>.json`.
7. Record each remaining issue on `01_raw_ast/conversion_report.yaml` as:

   ```yaml
   message: "Parse error at L13:C0-… | …"
   details:
     line: 13
     column: 0
     end_line: 13
     end_column: 15
     kind: error
     node_type: ERROR
     text: "…"
     source_line: "…"
     caret: "   ^^^"
     context:
       - "  11 | …"
       - "> 13 | …"
     child_types: []
     ancestor_types:
       - compilation_unit
       - "…"
   ```

   Do **not** write separate `parse_errors.json` / `.txt` sidecars — details belong
   on the stage report error objects only. Conversion reports are YAML for
   readability; per-file CST dumps under `01_raw_ast/` remain JSON.

## Soft failure semantics

CST / parse issues are **soft**: they appear under `01_raw_ast` (and often
`02_typed_ast`) but do **not** alone mark the file as overall `failed`. The
pipeline continues so the fallback regex parser and emitters can still produce
best-effort Python. Overall `failed` is reserved for hard emit/format/compile
problems (see step 5).

## What “Parse error at line N: ERROR” meant historically

Older reports used the CST node type string (`ERROR`) when the node had no leaf
`text`. Diagnostics now slice the original source by span and include context so
maintainers can fix grammar edge cases or accept fallback behavior.

## Downstream

Step 2 re-parses (or continues from the same grammar) to build a **typed AST**.
It may also invoke the **fallback parser** when the CST is fragmented.
