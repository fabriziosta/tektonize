---
name: groovyc precheck feasibility
overview: "Yes: an optional groovyc-based pre-check can run after discovery, but Jenkins shared-library idioms make a strict compile gate unreliable without stubs. Recommended design keeps JDK/groovyc optional, soft by default, and does not replace tree-sitter."
todos:
  - id: precheck-module
    content: Add groovyc runner (PATH detect, temp -d, src classpath, minimal Jenkins stubs) invoked after discovery
    status: pending
  - id: cli-flags
    content: Wire --groovy-syntax-check and --require-groovy-syntax into convert/convert-file + Converter.run timing/report
    status: pending
  - id: docs-tests
    content: Update docs/00_discovery.md + README; add regression tests for soft vs hard and missing groovyc
    status: pending
isProject: false
---

# Groovy groovyc pre-check feasibility

## Short answer

**Yes, it is possible** to shell out to `groovyc` on the discovered input set before stage `01_raw_ast`. It is **not** a drop-in replacement for today’s tree-sitter parse, and it should **not** be a hard install dependency.

Today tektonize never compiles Groovy. Discovery is path-only ([src/tektonize/discovery.py](src/tektonize/discovery.py)); “validation” is soft CST diagnostics in `01_raw_ast`, then Python `py_compile` after emit ([src/tektonize/converter.py](src/tektonize/converter.py) `_check_final_compile`). Prerequisites are Python-only ([README.md](README.md)).

## Why groovyc alone is a weak gate for Jenkins libs

Plain `groovyc` on a shared library will often fail on **valid-for-Jenkins** code:

- Implicit DSL (`sh`, `echo`, `stage`, `withCredentials`, …) not on the classpath
- CPS / `@NonCPS` / `script.` bindings
- `vars/*.groovy` script plugins (`def call(...)`) vs normal `src/` packages
- Third-party / product JARs referenced by imports
- Declarative `pipeline { }` blocks without Pipeline classpath

So a naive “fail convert if groovyc fails” would block many libraries that tektonize already converts best-effort. Conversely, tree-sitter ERROR nodes and groovyc failures overlap only partially: normalize fixes grammar quirks that are still valid Groovy; groovyc catches some real syntax tree-sitter-groovy may soft-miss.

```mermaid
flowchart LR
  discover[Discovery] --> groovycOpt[OptionalGroovycPrecheck]
  groovycOpt --> rawAst[01_raw_ast]
  rawAst --> typed[02_typed_ast]
  typed --> rest[03_to_05_plus_py_compile]
```

## Recommended design (if implemented)

1. **Hook:** [Converter.run()](src/tektonize/converter.py) immediately after building `groovy_files`, before `_batch_01_raw_ast`.
2. **Default off:** new CLI flag `--groovy-syntax-check` on `convert` / `convert-file`. No JDK required unless the flag is set.
3. **Soft failures by default:** record under a new timed step (e.g. `00_groovy_syntax`) like parse issues; do not increment overall `failed` unless `--require-groovy-syntax` is also set (then missing `groovyc` or any compile error is a hard abort before conversion).
4. **Tooling:** invoke host `groovyc` from PATH; joint-compile discovered files with `-d` under a temp dir; put library `src/` on `-classpath` so cross-file packages resolve when possible. Ship a minimal stub JAR/sources (empty/no-op Jenkins DSL + script base) under `resources/` so common DSL calls do not explode the check — not a full Jenkins classpath.
5. **Keep tree-sitter as the always-on path.** groovyc is advisory enrichment, not a substitute for CST normalize/diagnostics.
6. **Container:** leave the Python slim image unchanged; document that the flag needs a host/image with JDK + Groovy.
7. **Docs/tests:** update [docs/00_discovery.md](docs/00_discovery.md) (or a short sibling doc), README Prerequisites/Usage, stage timing list in [cli.py](src/tektonize/cli.py); add regression tests that stub groovyc success/failure and lock soft vs `--require-groovy-syntax` behavior.

## What this would catch well

- Unbalanced braces / clear syntax errors
- Some malformed constructs tree-sitter soft-reports inconsistently
- Package layout issues under `src/` when classpath is set

## What it would not reliably catch (without heavy stubs)

- Missing Jenkins step definitions as logic errors
- CPS runtime semantics
- Full product library type-correctness

## Effort / risk

- Moderate for optional soft wrapper + stub classpath + flags/docs/tests
- High false-fail rate if made a default hard gate on real shared libs

## Decision

Treat this as a worthwhile optional, soft, host-groovyc preflight — not a required pipeline stage and not a replacement for tree-sitter. Proceed to implement only if you want that flag-driven precheck next.
