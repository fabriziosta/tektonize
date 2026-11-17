# Conversion pipeline design docs

This directory documents **each step** of the tektonize conversion process in
detail. The human-facing overview stays in the root [README.md](../README.md);
these files are the deeper design contract for maintainers and agents.

| Step | Doc | Output directory | Code entry |
|------|-----|------------------|------------|
| 0 | [00_discovery.md](00_discovery.md) | *(none — selects inputs)* | `tektonize.discovery` |
| 1 | [01_raw_ast.md](01_raw_ast.md) | `01_raw_ast/` | `GroovyParser.get_raw_tree` |
| 2 | [02_typed_ast.md](02_typed_ast.md) | `02_typed_ast/` | `GroovyParser.parse_file` |
| 3 | [03_transformed.md](03_transformed.md) | `03_transformed/` | transformers under `tektonize.transformer` |
| 4 | [04_emitted.md](04_emitted.md) | `04_emitted/` | `PythonEmitter.emit_file` |
| 5 | [05_final.md](05_final.md) | `05_final/` | `format_python_source`, `__init__.py` generation |

Orchestrator: `tektonize.converter.Converter` (`STAGE_ORDER` in `converter.py`).

## Glossary (acronyms and terms)

| Term | Expansion | Meaning in tektonize |
|------|-----------|----------------------|
| **AST** | Abstract Syntax Tree | A tree of *language constructs* (class, method, import) after discarding most punctuation and layout. Stage **02** stores a *typed* AST (`GroovySourceFile` and friends in `parser/ast_nodes.py`). |
| **CST** | Concrete Syntax Tree | A tree that mirrors the *source text* closely (keywords, braces, every token). Stage **01** dumps tree-sitter’s CST as JSON. The folder is named `01_raw_ast` for historical reasons; the content is a **CST**, not an AST. |
| **ACS** | *(not used)* | This project does **not** use an “ACS” acronym. If you see it in conversation, it almost always means **AST** or **CST** — check the stage number. |
| **DSL** | Domain-Specific Language | Here: the Jenkins Pipeline step API (`sh`, `echo`, `stage`, `withCredentials`, …) embedded in Groovy. |
| **CPS** | Continuation-Passing Style | Jenkins Pipeline execution model. Groovy methods may be annotated `@NonCPS` to opt out; tektonize strips that annotation during conversion. |
| **POJO** | Plain Old Java Object | A simple data-holding class. Mapped toward a Python `@dataclass`. |
| **ABC** | Abstract Base Class | Python’s `abc.ABC` — target shape for Groovy `interface` declarations. |
| **CLI** | Command-Line Interface | `tektonize` Click entry points (`convert`, `convert-file`, `report`). |
| **JSON** | JavaScript Object Notation | Format of intermediate stage dumps (`01_raw_ast/` … `03_transformed/`). |
| **YAML** | YAML Ain't Markup Language | Format of `conversion_report.yaml` (top-level and per-stage). |
| **UTC** | Coordinated Universal Time | Timestamp used in default `output/<YYYYMMDDTHHMMSSZ>/` run directories. |

Related product terms (not pipeline stages, but appear in output types):

| Term | Meaning |
|------|---------|
| **Tekton** | Kubernetes-native CI system; tektonize emits *incomplete* Python models of Tasks / fragments, not finished cluster YAML. |
| **Task** / **TaskDefinition** | Reusable unit of work (one or more steps). See `tektonize.tekton.task_definition`. |
| **PipelineFragment** | Ordered list of tasks with optional `when` conditions. See `tektonize.tekton.pipeline_fragment`. |
| **ParamSchema** | Declared but unbound parameters for a later job-invoker. See `tektonize.tekton.param_schema`. Harvested from `params.UPPER_SNAKE` / `params['…']` names only — never filled with Jenkins controller values (`transformer/jenkins_bindings.py`). |
| **TEKTONIZE-TODO** | Distinctive marker for incomplete conversions (`tektonize.markers`). Grep with `rg TEKTONIZE-TODO`. |

## Keeping these docs accurate

When conversion logic changes, update the matching file(s) in this directory in
the **same** change. See `.cursor/rules/keep-docs-updated.mdc` and `AGENTS.md`.
