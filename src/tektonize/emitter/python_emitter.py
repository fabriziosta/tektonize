"""Emit Python source code from transformed AST representations.

Takes PythonClass objects (from class_transformer) and generates
properly formatted Python source files.

Includes a compile-and-fix sanitizer that iteratively comments out lines
causing SyntaxError until the output passes ``py_compile``, ensuring
maximum compilability of the converted Python.
"""

from __future__ import annotations

import re
import warnings

from tektonize.emitter.formatting import format_docstring, format_python_source
from tektonize.parser.ast_nodes import GroovySourceFile, MethodDecl
from tektonize.parser.span_join import join_open_spans
from tektonize.tekton.param_schema import ParamSchema
from tektonize.transformer.class_transformer import (
    ClassTransformer,
    PythonClass,
    PythonField,
    PythonMethod,
    PythonParam,
)
from tektonize.transformer.expression_transformer import ExpressionTransformer
from tektonize.transformer.import_transformer import ImportTransformer
from tektonize.transformer.jenkins_bindings import (
    format_param_schema_assignment,
    harvest_param_names,
)
from tektonize.transformer.jenkins_dsl_transformer import JenkinsDSLTransformer
from tektonize.transformer.method_transformer import MethodTransformer
from tektonize.transformer.type_mapper import TypeMapper


class PythonEmitter:
    """Generate Python source code from transformed Groovy AST."""

    def __init__(self, dsl_hints: dict[str, str] | None = None) -> None:
        self.type_mapper = TypeMapper()
        self.class_transformer = ClassTransformer(self.type_mapper)
        self.method_transformer = MethodTransformer()
        self.expr_transformer = ExpressionTransformer()
        self.jenkins_transformer = JenkinsDSLTransformer(dsl_hints=dsl_hints)

    def emit_file(self, source_file: GroovySourceFile) -> str:
        """Emit a complete Python file from a parsed Groovy source file."""
        sections: list[str] = []

        # Header
        sections.append(f'"""Converted from {source_file.file_path}."""')
        sections.append("")
        sections.append("from __future__ import annotations")
        sections.append("")

        # Transform imports
        import_transformer = ImportTransformer(source_file.package)
        import_result = import_transformer.transform_imports(source_file.imports)

        # Transform classes to collect type imports
        py_classes = []
        for cls in source_file.classes:
            py_cls = self.class_transformer.transform(cls)
            py_classes.append(py_cls)

        # Collect all extra imports
        all_extra_imports: list[str] = []
        for py_cls in py_classes:
            all_extra_imports.extend(py_cls.extra_imports)

        # Type mapper imports
        type_import_lines = self.type_mapper.get_import_lines()
        if type_import_lines:
            all_extra_imports.extend(type_import_lines)

        # Unbound ParamSchema from params.FOO / params['FOO'] (names only — no values).
        param_names = harvest_param_names(self._collect_body_text(source_file))
        if param_names:
            all_extra_imports.append("from tektonize.tekton import ParamSchema")

        import_result.extra_imports = sorted(set(all_extra_imports))

        # Write imports
        import_lines = import_result.to_lines()
        if import_lines:
            sections.extend(import_lines)
            sections.append("")

        if param_names:
            sections.append(
                "# Jenkins params.* names harvested as unbound ParamSchema "
                "(values are never substituted)."
            )
            sections.append(format_param_schema_assignment(ParamSchema(required=param_names)))
            sections.append("")

        # Write classes
        for py_cls in py_classes:
            sections.append("")
            sections.extend(self._emit_class(py_cls))

        # Write top-level methods (vars/ entry points)
        for method in source_file.top_level_methods:
            sections.append("")
            sections.extend(self._emit_top_level_method(method, source_file))

        # Add trailing newline
        sections.append("")

        source = "\n".join(sections)

        # Compile-fix pass: iteratively comment out lines that cause SyntaxError
        source = self._sanitize_syntax(source)

        # Format with black
        return format_python_source(source)

    def _collect_body_text(self, source_file: GroovySourceFile) -> str:
        """Concatenate method bodies for Jenkins ``params.*`` name harvest."""
        chunks: list[str] = []

        def walk_type(node: object) -> None:
            methods = getattr(node, "methods", None) or []
            for method in methods:
                if method.body_text:
                    chunks.append(method.body_text)
            constructors = getattr(node, "constructors", None) or []
            for ctor in constructors:
                if ctor.body_text:
                    chunks.append(ctor.body_text)
            for inner in getattr(node, "inner_classes", None) or []:
                walk_type(inner)

        for cls in source_file.classes:
            walk_type(cls)
        for method in source_file.top_level_methods:
            if method.body_text:
                chunks.append(method.body_text)
        return "\n".join(chunks)

    def _emit_class(self, py_cls: PythonClass) -> list[str]:
        lines: list[str] = []

        # Decorators
        for dec in py_cls.decorators:
            lines.append(dec)

        # Class header
        if py_cls.bases:
            bases_str = ", ".join(py_cls.bases)
            lines.append(f"class {py_cls.name}({bases_str}):")
        else:
            lines.append(f"class {py_cls.name}:")

        # Docstring
        if py_cls.docstring:
            lines.extend(format_docstring(py_cls.docstring))

        has_body = False

        # Enum values
        if py_cls.kind == "enum":
            for ev in py_cls.enum_values:
                if ev.docstring:
                    lines.append(f"    # {ev.docstring}")
                lines.append(f"    {ev.name} = {ev.value}")
                has_body = True
            if py_cls.enum_values:
                lines.append("")

        # Fields
        if py_cls.kind == "dataclass":
            for f in py_cls.fields:
                lines.extend(self._emit_dataclass_field(f))
                has_body = True
        else:
            for f in py_cls.fields:
                lines.extend(self._emit_field(f))
                has_body = True

        if py_cls.fields:
            lines.append("")

        # Methods
        for m in py_cls.methods:
            lines.extend(self._emit_method(m))
            lines.append("")
            has_body = True

        # Inner classes
        for inner in py_cls.inner_classes:
            inner_lines = self._emit_class(inner)
            lines.extend(f"    {line}" if line.strip() else "" for line in inner_lines)
            has_body = True

        if not has_body:
            lines.append("    pass")

        return lines

    def _emit_dataclass_field(self, f: PythonField) -> list[str]:
        lines: list[str] = []
        if f.docstring:
            # Ensure multi-line docstrings are properly commented
            for doc_line in f.docstring.split("\n"):
                lines.append(f"    # {doc_line.strip()}")

        type_hint = f": {f.type_hint}" if f.type_hint else ""
        default = ""
        if f.default_value is not None:
            val = self._sanitize_literal(f.default_value)
            # Use field(default_factory=...) for mutable defaults
            if val in ("[]", "{}") or val.startswith("[") or val.startswith("{"):
                if val == "[]":
                    default = " = field(default_factory=list)"
                elif val == "{}":
                    default = " = field(default_factory=dict)"
                else:
                    default = f" = field(default_factory=lambda: {val})"
            else:
                default = f" = {val}"

        lines.append(f"    {f.name}{type_hint}{default}")
        return lines

    def _emit_field(self, f: PythonField) -> list[str]:
        lines: list[str] = []
        if f.docstring:
            for doc_line in f.docstring.split("\n"):
                lines.append(f"    # {doc_line.strip()}")

        type_hint = f": {f.type_hint}" if f.type_hint else ""
        if f.default_value is not None:
            lines.append(f"    {f.name}{type_hint} = {self._sanitize_literal(f.default_value)}")
        elif f.type_hint:
            lines.append(f"    {f.name}{type_hint}")
        else:
            lines.append(f"    {f.name} = None")

        return lines

    def _sanitize_literal(self, value: str) -> str:
        """Lower Groovy literals / map defaults to valid Python.

        Simple scalars go through ``TypeMapper.map_default_value``. Complex
        ``[…]`` values (including nested maps) always run
        ``ExpressionTransformer`` so identifier/string-key maps and normalize
        sentinels become live Python dicts — not left for ``_sanitize_syntax``
        to comment out as ``# GROOVY:``.
        """
        import re

        mapped = self.type_mapper.map_default_value(value, None)
        text = mapped if mapped is not None else value.strip()
        # Re-run expression transform when the value still looks like Groovy
        # (maps, sentinels, booleans inside nested literals, etc.).
        if (
            "[" in text
            or "__TEKTONIZE_" in text
            or "[:]" in text
            or re.search(r"\b(true|false|null)\b", text)
            or re.search(r"(^|[{,\n]\s*)[A-Za-z_]\w*\s*:", text)
        ):
            text = self.expr_transformer.transform(text)
        text = re.sub(r"\b(\d+)[Ll]\b", r"\1", text)
        return text

    def _emit_method(self, m: PythonMethod) -> list[str]:
        lines: list[str] = []

        # Decorators
        for dec in m.decorators:
            lines.append(f"    {dec}")

        # Signature
        params_str = self._format_params(m.params)
        return_hint = f" -> {m.return_type}" if m.return_type else ""
        lines.append(f"    def {m.name}({params_str}){return_hint}:")

        # Docstring
        if m.docstring:
            lines.extend(format_docstring(m.docstring, indent_level=2))

        # Body
        if m.raw_body:
            groovy_params = [
                p.source_name or p.name
                for p in m.params
                if p.name != "self" and (p.source_name or p.name)
            ]
            body_lines = self._transform_method_body(m.raw_body, param_names=groovy_params)
            lines.extend(self._indent_body_lines(body_lines, base="        "))
        else:
            lines.extend(self._indent_body_lines(m.body_lines, base="        "))

        return lines

    def _emit_top_level_method(
        self, method: MethodDecl, source_file: GroovySourceFile
    ) -> list[str]:
        """Emit a top-level method (e.g. vars/ call() function)."""
        from tektonize.transformer.name_converter import camel_to_snake

        lines: list[str] = []

        name = method.name
        if name == "call":
            # Use the filename as the function name for vars/ entry points

            fname = source_file.file_path.split("/")[-1].replace(".groovy", "")
            name = camel_to_snake(fname)

        params = self._format_groovy_params(method.parameters)
        lines.append(f"def {name}({params}):")

        # Docstring from javadoc
        if method.javadoc:
            doc = self.class_transformer._clean_javadoc(method.javadoc)
            if doc:
                lines.extend(format_docstring(doc))

        # Body
        if method.body_text:
            groovy_params = [p.name for p in method.parameters]
            body_lines = self._transform_method_body(method.body_text, param_names=groovy_params)
            lines.extend(self._indent_body_lines(body_lines, base="    "))
        else:
            lines.append("    pass")

        return lines

    def _indent_body_lines(self, body_lines: list[str], *, base: str) -> list[str]:
        """Re-indent transformed body lines, preserving relative indentation."""
        nonempty = [bl for bl in body_lines if bl.strip()]
        if not nonempty:
            return [f"{base}pass"]

        min_indent = min(len(bl) - len(bl.lstrip()) for bl in nonempty)
        result: list[str] = []
        for bl in body_lines:
            if not bl.strip():
                result.append("")
                continue
            relative = bl[min_indent:] if len(bl) >= min_indent else bl.lstrip()
            result.append(f"{base}{relative}")
        return result

    def _transform_method_body(
        self, body_text: str, *, param_names: list[str] | None = None
    ) -> list[str]:
        """Transform method body through all transformer stages."""
        # Keep original text for local-name discovery: Jenkins sh rewrite can
        # drop ``def exitCode = script.sh(`` before MethodTransformer sees it.
        rename_source = body_text
        # Strip the method's outer ``{…}`` *before* span-join. Otherwise a leading
        # ``{`` looks like an open map/dict span and join_open_spans flattens the
        # entire body onto one megline (cascade GROOVY + sanitize swallowing
        # following methods).
        unwrapped = MethodTransformer._unwrap_method_body(body_text)
        # Join unfinished call/map/if-header spans so line-oriented transformers
        # do not see mid-``(`` / mid-``{`` fragments (triple-quotes preserved).
        spanned = join_open_spans(unwrapped)
        # First apply expression-level transforms
        transformed = self.expr_transformer.transform(spanned)
        # Then apply Jenkins DSL transforms
        transformed = self.jenkins_transformer.transform_body(transformed)
        # Then apply method body transforms (incl. local/param snake_case uses).
        # Body is already unwrapped; MethodTransformer unwrap is a no-op dedent.
        lines = self.method_transformer.transform_body(
            transformed,
            param_names=param_names,
            rename_source=rename_source,
        )
        return lines

    def _format_params(self, params: list[PythonParam]) -> str:
        parts = []
        seen_default = False
        for p in params:
            hint = f": {p.type_hint}" if p.type_hint else ""
            if p.default_value:
                seen_default = True
                dflt = f" = {self._sanitize_literal(p.default_value)}"
            elif seen_default:
                # Groovy allows trailing Closure after Map defaults; Python does not.
                dflt = " = None"
            else:
                dflt = ""
            parts.append(f"{p.name}{hint}{dflt}")
        return ", ".join(parts)

    def _format_groovy_params(self, params: list) -> str:
        """Format MethodDecl parameters for a top-level Python def."""
        from tektonize.transformer.name_converter import convert_identifier

        parts: list[str] = []
        seen_default = False
        for p in params:
            type_hint = self.type_mapper.map_type(p.type_ref) if p.type_ref else ""
            hint = f": {type_hint}" if type_hint else ""
            if p.default_value:
                seen_default = True
                dflt = f" = {self._sanitize_literal(p.default_value)}"
            elif seen_default:
                dflt = " = None"
            else:
                dflt = ""
            parts.append(f"{convert_identifier(p.name)}{hint}{dflt}")
        return ", ".join(parts)

    # ------------------------------------------------------------------
    # Post-emit compile-and-fix sanitizer
    # ------------------------------------------------------------------

    def _sanitize_syntax(self, source: str) -> str:
        """Iteratively fix SyntaxErrors/SyntaxWarnings by commenting bad lines.

        Tries ``compile()`` in a loop (up to *max_passes* rounds).  On each
        failure the method comments out or trims the offending line and retries.
        ``SyntaxWarning`` (invalid ``\\{`` escapes, ``return`` in ``finally``)
        is treated like an error so convert does not spam stderr and output
        stays warning-clean under ``py_compile``.
        """
        source = self._rewrite_invalid_brace_escapes(source)
        max_passes = max(200, len(source.split("\n")))
        for _ in range(max_passes):
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always", SyntaxWarning)
                try:
                    compile(source, "<sanitize>", "exec")
                except SyntaxError as exc:
                    fixed = self._fix_one_syntax_error(source, exc)
                    if fixed is None or fixed == source:
                        break
                    source = fixed
                    continue

                warn_fixed = self._fix_syntax_warnings(source, caught)
                if warn_fixed is None or warn_fixed == source:
                    return self._drop_orphan_groovy_stubs(source)
                source = warn_fixed
        return self._drop_orphan_groovy_stubs(source)

    @staticmethod
    def _drop_orphan_groovy_stubs(source: str) -> str:
        """Remove or reclassify sanitizer GROOVY stubs that inflate counts.

        Compile-sanitizer often re-comments orphan ``else:`` / ``elif`` / nested
        ``def`` after brace unwrap — convert those to TODO (or drop) so they
        are not counted as ``# GROOVY:``. Also reclassifies field-default
        dict peels, shell fragments, Job DSL leftovers, and prose help text.
        """
        stub_re = re.compile(
            r"^(?:else:|else\s*\{|pass|finally:|continue|break|"
            r"except\s+\w+(?:\s+as\s+\w+)?\s*:)\s*$"
        )
        shell_closer_re = re.compile(r'^("""|\'\'\')\)?\s*(?:\}\s*)+$')
        decorator_re = re.compile(r"^@(?:staticmethod|classmethod|property)\s*$")
        elif_re = re.compile(r"^elif\b.+:\s*$")
        nested_def_re = re.compile(r"^def\s+\w+\s*\(\s*(?:self|cls)\b")
        dict_entry_re = re.compile(
            r"^(?:"
            r"\([^)]+\)\s*:\s*\{|"
            r"['\"][^'\"]+['\"]\s*:|"
            r"//|"
            r"['\"][^'\"]+['\"]\s*,\s*(?://.*)?$|"
            r"\}\s*,?$|"
            r"\[(?:'[^']*',\s*)+'[^']*'\]$"
            r")"
        )
        shell_re = re.compile(
            r"^(?:oc\s|kubectl\s|git\s|set\s|export\s|source\s|echo\s|grep\s|"
            r"ansible-|pip\s|python3?\s|mkdir\s|chmod\s|cat\s|tail\s|which\s|"
            r"helm\s|terraform\s|aws\s|az\s)"
        )
        prose_re = re.compile(
            r"^(?:[-*]\s|\d+\.\s|"
            r"(?:Examples|Conditions|Notes|Description|Usage|WARNING|NOTE)\s*:|"
            r"Leave blank|"
            r"[A-Za-z][\w\s,/'-]{0,60}:\s*$)"
        )
        job_dsl_re = re.compile(
            r"^(?:(?:\w[\w.]*\.)?(?:pipelineJob|applyParameters|applyProperties)\b|"
            r"definition\s*\{|buildDiscarder\s*\{|pipelineTriggers\s*\{|"
            r"lines\.append\(f?[\"'].*formatKey|"
            r".*\.map\(lambda\s+it:\s*\w+\s*,|"
            r"String\[\]\s|"
            r"stages\s*\[)"
        )
        out: list[str] = []
        lines = source.split("\n")
        i = 0
        while i < len(lines):
            line = lines[i]
            stripped = line.strip()
            indent = line[: len(line) - len(line.lstrip())]
            if stripped.startswith("# GROOVY:"):
                content = stripped[len("# GROOVY:") :].strip()
                if stub_re.match(content) or shell_closer_re.match(content):
                    i += 1
                    continue
                if decorator_re.match(content):
                    i += 1
                    continue
                if elif_re.match(content):
                    out.append(f"{indent}# TEKTONIZE-TODO: orphan elif -> {content[:80]}")
                    i += 1
                    continue
                if nested_def_re.match(content):
                    out.append(f"{indent}# TEKTONIZE-TODO: nested method decl -> {content[:80]}")
                    i += 1
                    continue
                if content in ("default:", "default") or content.startswith("default:"):
                    out.append(
                        f"{indent}# TEKTONIZE-TODO: switch/case -> match/case (not converted)"
                    )
                    i += 1
                    continue
                if shell_re.match(content):
                    out.append(f"{indent}# | {content}")
                    i += 1
                    continue
                if prose_re.match(content) or (
                    len(content) > 12
                    and "=" not in content
                    and "(" not in content
                    and not re.search(r"\b(def|return|if|for|while|class)\b", content)
                ):
                    out.append(f"{indent}# {content}")
                    i += 1
                    continue
                if job_dsl_re.match(content):
                    out.append(
                        f"{indent}# TEKTONIZE-TODO: Job DSL / half-convert -> {content[:80]}"
                    )
                    i += 1
                    continue
                # Collapse consecutive dict/list peel GROOVY into one TODO.
                if dict_entry_re.match(content):
                    j = i + 1
                    while j < len(lines):
                        nxt = lines[j].strip()
                        if not nxt:
                            j += 1
                            continue
                        if nxt.startswith("# GROOVY:"):
                            body = nxt[len("# GROOVY:") :].strip()
                            if dict_entry_re.match(body):
                                j += 1
                                continue
                        break
                    out.append(
                        f"{indent}# TEKTONIZE-TODO: dict/list literal peel "
                        "-> keep as Python literal"
                    )
                    i = j
                    continue
            out.append(line)
            i += 1
        return "\n".join(out)

    def _rewrite_invalid_brace_escapes(self, source: str) -> str:
        """Turn invalid ``\\{`` / ``\\}`` / ``\\$`` string escapes into safe forms.

        Leftover shell/GString mangling produces ``f\"\\{VAR}\"`` or ``\\$(…)``
        inside ``\"\"\"…\"\"\"`` bodies — those only *warn* on ``compile()``.
        Also rewrite lines that look like comments but are string content.
        """
        return source.replace("\\{", "{{").replace("\\}", "}}").replace("\\$", "$")

    def _fix_syntax_warnings(
        self, source: str, caught: list[warnings.WarningMessage]
    ) -> str | None:
        """Comment out or rewrite lines that triggered SyntaxWarning."""
        # Prefer a second escape rewrite (covers ``\"\"\"`` bodies) before
        # commenting — ``lineno`` may point at string content that looks like
        # a ``#`` comment.
        rewritten = self._rewrite_invalid_brace_escapes(source)
        if rewritten != source:
            return rewritten

        lines = source.split("\n")
        changed = False
        seen: set[int] = set()
        for w in caught:
            if not issubclass(w.category, SyntaxWarning):
                continue
            lineno = w.lineno
            if lineno is None or lineno < 1 or lineno > len(lines):
                continue
            if lineno in seen:
                continue
            seen.add(lineno)
            idx = lineno - 1
            bad = lines[idx]
            stripped = bad.strip()
            if not stripped:
                continue
            indent = bad[: len(bad) - len(bad.lstrip())]
            # Do not skip ``#…`` — inside triple-quoted strings it is content.
            if stripped.startswith("# GROOVY:"):
                continue
            lines[idx] = f"{indent}# GROOVY: {stripped}"
            changed = True
        return "\n".join(lines) if changed else None

    def _fix_one_syntax_error(self, source: str, error: SyntaxError) -> str | None:
        """Attempt to fix a single SyntaxError by commenting/trimming one line."""
        lines = source.split("\n")
        lineno = error.lineno
        msg = str(error.msg) if error.msg else ""

        if lineno is None or lineno < 1 or lineno > len(lines):
            return None

        idx = lineno - 1
        bad_line = lines[idx]
        stripped = bad_line.strip()
        indent = bad_line[: len(bad_line) - len(bad_line.lstrip())]

        # ----- Error on a blank / comment line → real cause is earlier -----
        if not stripped or stripped.startswith("#"):
            return self._fix_backward(lines, idx)

        # ----- expected 'except' or 'finally' → orphaned try block -----
        if "expected 'except' or 'finally'" in msg:
            return self._fix_orphan_try(lines, idx)

        # ----- Trailing continuation (+  \  ,) whose continuation is gone -----
        for j in range(idx, max(idx - 2, -1), -1):
            jline = lines[j].rstrip()
            js = jline.strip()
            if not js or js.startswith("#"):
                continue
            if js.endswith("+") or (js.endswith("\\") and not js.endswith("\\\\")):
                ji = jline[: len(jline) - len(jline.lstrip())]
                cleaned = js[:-1].rstrip()
                if cleaned:
                    lines[j] = f"{ji}{cleaned}"
                else:
                    lines[j] = f"{ji}# GROOVY: {js}"
                return "\n".join(lines)
            break

        # ----- Structural lines (def/class/decorator) should not be commented
        # out — the real problem is usually orphaned deep-indented code above. -----
        if stripped.startswith(("def ", "class ", "@")):
            err_indent = len(bad_line) - len(bad_line.lstrip())
            for j in range(idx - 1, -1, -1):
                prev = lines[j].strip()
                if not prev or prev.startswith("#"):
                    continue
                prev_indent = len(lines[j]) - len(lines[j].lstrip())
                if prev_indent > err_indent:
                    pi = lines[j][: len(lines[j]) - len(lines[j].lstrip())]
                    lines[j] = f"{pi}# GROOVY: {prev}"
                    return "\n".join(lines)
                break
            # Fall through to default if no orphan found above

        # ----- Default: comment out the error line -----
        if stripped.startswith("# GROOVY:"):
            return None
        lines[idx] = f"{indent}# GROOVY: {stripped}"
        return "\n".join(lines)

    def _fix_backward(self, lines: list[str], idx: int) -> str | None:
        """When the error is on a comment/blank, search backward for the cause."""
        for j in range(idx - 1, -1, -1):
            prev = lines[j].strip()
            if not prev or prev.startswith("#"):
                continue
            prev_indent = lines[j][: len(lines[j]) - len(lines[j].lstrip())]
            # Trailing continuation operator
            if prev.endswith("+") or (prev.endswith("\\") and not prev.endswith("\\\\")):
                cleaned = prev[:-1].rstrip()
                if cleaned:
                    lines[j] = f"{prev_indent}{cleaned}"
                    return "\n".join(lines)
            # Unclosed paren at end of line
            if prev.endswith("("):
                lines[j] = f"{prev_indent}# GROOVY: {prev}"
                return "\n".join(lines)
            # Already a GROOVY comment — keep searching
            if prev.startswith("# GROOVY:"):
                continue
            # Generic: comment it out
            lines[j] = f"{prev_indent}# GROOVY: {prev}"
            return "\n".join(lines)
        return None

    def _fix_orphan_try(self, lines: list[str], error_idx: int) -> str | None:
        """Fix an unmatched ``try:`` by inserting a dummy ``except Exception: pass``.

        Walks backward, tracking handlers and try blocks **by indent level**
        so that nested try/except pairs at deeper indents are not incorrectly
        consumed by an outer orphan.
        """
        seen_handlers: dict[int, int] = {}
        for j in range(error_idx - 1, -1, -1):
            jline = lines[j].strip()
            if not jline or jline.startswith("#"):
                continue
            jindent = len(lines[j]) - len(lines[j].lstrip())

            if jline.startswith(("except ", "except:")) or jline == "finally:":
                seen_handlers[jindent] = seen_handlers.get(jindent, 0) + 1
            elif jline == "try:":
                count = seen_handlers.get(jindent, 0)
                if count > 0:
                    seen_handlers[jindent] = count - 1
                else:
                    # Find the end of the try body (last line deeper than try indent).
                    body_end = j
                    for k in range(j + 1, min(error_idx, len(lines))):
                        kline = lines[k].strip()
                        kindent = len(lines[k]) - len(lines[k].lstrip())
                        if kline and not kline.startswith("#") and kindent <= jindent:
                            break
                        if kline:
                            body_end = k
                    pad = " " * jindent
                    inner = " " * (jindent + 4)
                    lines.insert(body_end + 1, f"{inner}pass")
                    lines.insert(body_end + 1, f"{pad}except Exception:")
                    return "\n".join(lines)
        return None
