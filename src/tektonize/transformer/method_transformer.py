"""Transform Groovy method bodies to Python.

Handles: self param injection, body conversion, implicit returns,
getter/setter patterns, @NonCPS removal, closure conversion.
"""

from __future__ import annotations

import re

from tektonize.markers import todo_comment, todo_text
from tektonize.transformer.expression_transformer import ExpressionTransformer
from tektonize.transformer.jenkins_bindings import mask_jenkins_binding_accesses
from tektonize.transformer.name_converter import convert_identifier


class MethodTransformer:
    """Transform Groovy method body text to Python method body lines."""

    def __init__(self) -> None:
        self.expr_transformer = ExpressionTransformer()
        # Open ``{`` depth for Python dicts emitted after map→dict rewrite.
        # Used so orphan Groovy ``}`` closers are still dropped.
        self._dict_brace_depth = 0
        # Open ``(`` / ``[`` depth for multiline calls / list literals.
        self._paren_depth = 0
        self._bracket_depth = 0

    def transform_body(
        self,
        body_text: str,
        is_static: bool = False,
        param_names: list[str] | None = None,
        rename_source: str | None = None,
    ) -> list[str]:
        """Transform a Groovy method body to Python lines.

        ``param_names`` are original Groovy formal names; together with local
        declarations they drive camelCase→snake_case rewrites of bare uses so
        ``command_log_name = …`` is not followed by ``…{commandLogName}…``.

        ``rename_source`` is the pre-Jenkins body (or original Groovy) used to
        discover locals — TaskStep rewrite can drop ``def exitCode = script.sh(``
        before line transform runs.
        """
        if not body_text or not body_text.strip():
            return ["pass"]

        body = self._unwrap_method_body(body_text)
        if not body:
            return ["pass"]

        renames = self._collect_local_renames(body, param_names)
        if rename_source and rename_source.strip():
            src = self._unwrap_method_body(rename_source)
            if src:
                for old, new in self._collect_local_renames(src, None).items():
                    renames.setdefault(old, new)

        lines = body.split("\n")
        result = []
        has_valid_line = False
        self._dict_brace_depth = 0
        self._paren_depth = 0
        self._bracket_depth = 0

        # Absorb multiline call tails (buildAndExecute(, foo({) so the emit
        # compile-sanitizer cannot cascade-comment live Python as GROOVY.
        call_absorb_depth = 0
        # Orphan switch/case span without a live ``match``.
        switch_absorb = False
        switch_absorb_indent = -1
        # Live ``x = """`` … ``"""`` — emit body as string content (not GROOVY).
        triple_quote: str | None = None
        # Nested ``def foo(self,…):`` inside a method body (AST bleed).
        nested_def_absorb = False
        nested_def_indent = -1
        for line in lines:
            stripped_raw = line.strip()
            indent = line[: len(line) - len(line.lstrip())]

            if nested_def_absorb:
                if not stripped_raw:
                    continue
                ind = len(line) - len(line.lstrip())
                if ind <= nested_def_indent and not stripped_raw.startswith(
                    ("#", "}", "case ", "default")
                ):
                    nested_def_absorb = False
                    # Fall through to process this sibling line.
                else:
                    continue

            if triple_quote is not None:
                if not stripped_raw:
                    result.append("")
                    continue
                # Closer (bare or with trailing junk already peeled elsewhere).
                if stripped_raw == triple_quote or re.match(
                    rf"^{re.escape(triple_quote)}\)?\s*$", stripped_raw
                ):
                    result.append(f"{indent}{triple_quote}")
                    triple_quote = None
                    has_valid_line = True
                    continue
                # Keep YAML/shell/prose as live string content (never GROOVY).
                result.append(line if line.strip() else "")
                has_valid_line = True
                continue

            if call_absorb_depth > 0:
                if not stripped_raw:
                    continue
                call_absorb_depth += stripped_raw.count("(") - stripped_raw.count(")")
                call_absorb_depth += stripped_raw.count("{") - stripped_raw.count("}")
                call_absorb_depth += stripped_raw.count("[") - stripped_raw.count("]")
                if call_absorb_depth < 0:
                    call_absorb_depth = 0
                result.append(
                    todo_comment(
                        "call-with-args continuation -> part of prior call TODO",
                        indent=indent,
                    )
                )
                continue

            if switch_absorb:
                if not stripped_raw:
                    continue
                ind = len(line) - len(line.lstrip())
                if (
                    ind < switch_absorb_indent
                    and not stripped_raw.startswith(("case ", "default", "break", "}", "#"))
                ) or (
                    ind < switch_absorb_indent
                    and stripped_raw.startswith(("def ", "class ", "return ", "if ", "for "))
                ):
                    switch_absorb = False
                else:
                    if stripped_raw in ("}", "};") or (
                        ind <= switch_absorb_indent and stripped_raw.startswith(("def ", "class "))
                    ):
                        switch_absorb = False
                    # Consume the span silently after the opener TODO (one marker).
                    if stripped_raw in ("}", "};"):
                        switch_absorb = False
                    continue

            converted = self._transform_line(line)
            if converted is not None:
                # Strip trailing Groovy ``;`` so otherwise-valid Python stays live.
                if converted.rstrip().endswith(";") and not converted.strip().startswith("#"):
                    converted = converted.rstrip()[:-1].rstrip() + (
                        "\n" if converted.endswith("\n") else ""
                    )
                # Ternary-with-decls on the whole line *before* juxtaposition
                # splits peel typed arms into separate GROOVY fragments.
                conv_all = converted.strip()
                conv_indent = converted[: len(converted) - len(converted.lstrip())]
                if (
                    "__TEKTONIZE_TERNARY_TODO__" in conv_all
                    or (
                        re.search(r"\bif\s*\{", conv_all)
                        and re.search(
                            r"\b(def|String|Map|Boolean|Integer|List|return)\b",
                            conv_all,
                        )
                    )
                    or re.search(r"\bvalueOf\s*\([^)]*\)\s+if\s*\{", conv_all)
                ):
                    result.append(
                        todo_comment(
                            "ternary-with-decls -> pure-expression ternary "
                            f"or manual convert ({conv_all[:60]})",
                            indent=conv_indent,
                        )
                    )
                    continue

                # Wave 23: peel ``for x in xs:stmt`` / ``if cond:stmt`` (no space after ``:``).
                # Wave 28: peel ``if (cond) { stmt }`` one-liners.
                # Also peel ``stmt; def x =`` semicolon-joined locals.
                pieces = self._split_glued_control_bodies(converted)
                expanded: list[str] = []
                for p in pieces:
                    for q in self._split_semicolon_statements(p):
                        for r in self._split_juxtaposed_declarations(q):
                            expanded.extend(self._split_brace_if_oneliner(r))
                for piece in expanded:
                    # Re-strip typed/def locals on pieces created after _transform_line.
                    piece_indent = piece[: len(piece) - len(piece.lstrip())]
                    decl = self._transform_variable_declaration(piece.strip())
                    if decl is not None:
                        piece = piece_indent + decl
                    # ``""") // comment if (…)`` → bare closer (keep string closed).
                    closer_m = re.match(
                        r'^(\s*)("""|\'\'\')\)?\s*(?://.*|/\*.*)?$',
                        piece,
                    )
                    if closer_m and ("//" in piece or "/*" in piece or piece.strip().endswith(")")):
                        # Only peel when trailing junk follows the delimiter.
                        rest = piece.strip()[3:].lstrip(")").strip()
                        if (
                            rest.startswith("//")
                            or rest.startswith("/*")
                            or (rest and not rest.startswith(")"))
                        ):
                            piece = f"{closer_m.group(1)}{closer_m.group(2)}"
                    conv_stripped = piece.strip()
                    piece_indent = piece[: len(piece) - len(piece.lstrip())]

                    # Multi-stmt meglines (``} config.update`` / ``} BuildPipeline``).
                    # Do not treat ``params['KEY'] def`` (bracket + next token) as multi-stmt.
                    # ``; def x =`` is split earlier — only TODO when ``}`` precedes next stmt.
                    if re.search(
                        r"\}\s+(?:[a-zA-Z_]\w*\s*(?:[=\(.]|update\b)|"
                        r"[A-Z][A-Za-z0-9_]*\b|return\b|def\b)",
                        conv_stripped,
                    ):
                        result.append(
                            todo_comment(
                                f"multi-stmt megline -> {conv_stripped[:80]}",
                                indent=piece_indent,
                            )
                        )
                        continue

                    # Leftover sh named-arg trailers / eachWithIndex / findFiles / Date().
                    # (``=~`` still becomes GROOVY via validity — keep that path.)
                    if (
                        re.match(r'^""",?\s*returnStdout', conv_stripped)
                        or re.match(r"^'''?,?\s*returnStdout", conv_stripped)
                        or re.search(r"\beachWithIndex\b", conv_stripped)
                        or re.search(r"\bfindFiles\s*\(", conv_stripped)
                        or re.search(r"\bDate\s*\(\s*\)", conv_stripped)
                        or re.match(r"^\}\s*catch\b", conv_stripped)
                        or re.match(r"^try\s*\{", conv_stripped)
                        or re.match(r"^\}\s*else\s*\{", conv_stripped)
                    ):
                        result.append(
                            todo_comment(
                                f"groovy/shell residue -> {conv_stripped[:80]}",
                                indent=piece_indent,
                            )
                        )
                        continue

                    # ``buildAndExecute(`` / ``.build(`` wrap huge stagesClosure tails —
                    # absorb immediately so emit compile-sanitizer cannot cascade.
                    if re.search(r"(?:buildAndExecute|\.build)\s*\(\s*$", conv_stripped):
                        result.append(
                            todo_comment(
                                f"call-with-args -> {conv_stripped}",
                                indent=piece_indent,
                            )
                        )
                        call_absorb_depth = 1
                        break

                    # Groovy ``switch`` / brace-style ``case`` / stray ``default:``.
                    if (
                        conv_stripped.startswith("switch ")
                        or re.match(r"^switch\s*\(", conv_stripped)
                        or conv_stripped == "default:"
                        or conv_stripped.startswith("default:")
                    ):
                        result.append(
                            todo_comment(
                                "switch/case -> match/case (not converted)",
                                indent=piece_indent,
                            )
                        )
                        switch_absorb = True
                        switch_absorb_indent = len(piece) - len(piece.lstrip())
                        continue
                    if re.match(r"^case\s+", conv_stripped) and not self._has_live_match(
                        result, len(piece) - len(piece.lstrip())
                    ):
                        result.append(
                            todo_comment(
                                "switch/case -> match/case (not converted)",
                                indent=piece_indent,
                            )
                        )
                        switch_absorb = True
                        switch_absorb_indent = len(piece) - len(piece.lstrip())
                        continue
                    # Hand-written / residual ``match`` with brace residue → absorb.
                    if (
                        conv_stripped.startswith("match ")
                        and conv_stripped.endswith(":")
                        and ("{" in conv_stripped or not self._is_valid_python_line(piece))
                    ):
                        result.append(
                            todo_comment(
                                "switch/case -> match/case (not converted)",
                                indent=piece_indent,
                            )
                        )
                        switch_absorb = True
                        switch_absorb_indent = len(piece) - len(piece.lstrip())
                        continue

                    # Nested method decl inside a body (AST bleed) → TODO absorb.
                    if re.match(
                        r"^def\s+\w+\s*\(\s*(?:self|cls)\b",
                        conv_stripped,
                    ):
                        result.append(
                            todo_comment(
                                f"nested method decl -> {conv_stripped[:80]}",
                                indent=piece_indent,
                            )
                        )
                        nested_def_absorb = True
                        nested_def_indent = len(piece) - len(piece.lstrip())
                        continue

                    # Broken ternary residues with return/script.echo → TODO.
                    if re.search(
                        r"""['\"][^'\"]*['\"]\s+if\s+(?:return\b|script\.|self\.script\.)""",
                        conv_stripped,
                    ) or re.search(
                        r"\bif\s+return\b",
                        conv_stripped,
                    ):
                        result.append(
                            todo_comment(
                                "ternary-with-decls -> pure-expression ternary "
                                f"or manual convert ({conv_stripped[:60]})",
                                indent=piece_indent,
                            )
                        )
                        continue

                    # Groovy one-liner ``if (cond) return`` (not a multiline dict opener).
                    if re.match(r"^if\s*\(.+\)\s+return\b", conv_stripped):
                        result.append(
                            todo_comment(
                                f"if-return one-liner -> {conv_stripped[:80]}",
                                indent=piece_indent,
                            )
                        )
                        continue

                    # Closure-shaped ``return { it ->`` / ``return { x ->`` residue.
                    if re.match(r"^return\s*\{\s*\w+\s*->", conv_stripped):
                        result.append(
                            todo_comment(
                                f"return-closure residue -> {conv_stripped[:80]}",
                                indent=piece_indent,
                            )
                        )
                        continue

                    # Unbalanced ``return { k: v, …`` meglines (keys on same line,
                    # missing closer). Do **not** close bare ``return {`` openers —
                    # those start a multiline dict whose body follows.
                    if re.match(r"^return\s*\{", conv_stripped) and not re.match(
                        r"^return\s*\{\s*$", conv_stripped
                    ):
                        opens = conv_stripped.count("{")
                        closes = conv_stripped.count("}")
                        if opens > closes and (
                            ":" in conv_stripped or conv_stripped.rstrip().endswith(",")
                        ):
                            fixed = conv_stripped + ("}" * (opens - closes))
                            if self._is_valid_python_line(piece_indent + fixed):
                                result.append(piece_indent + fixed)
                                self._nudge_dict_brace_depth(fixed)
                                continue
                            result.append(
                                todo_comment(
                                    f"return-map megline -> {conv_stripped[:80]}",
                                    indent=piece_indent,
                                )
                            )
                            continue

                    # ``' '.join(return f"…")`` collect residue → strip inner return.
                    join_ret = re.sub(
                        r"\.join\(\s*return\s+",
                        ".join(",
                        conv_stripped,
                    )
                    join_ret = re.sub(
                        r"join\(\s*return\s+",
                        "join(",
                        join_ret,
                    )
                    if join_ret != conv_stripped:
                        piece = piece_indent + join_ret
                        conv_stripped = join_ret
                        if self._is_valid_python_line(piece):
                            result.append(piece)
                            self._nudge_dict_brace_depth(conv_stripped)
                            continue

                    # Split ternary arm left on own line: ``'good' if`` / ``cfg.x if``
                    if re.search(r"\bif\s*$", conv_stripped) or re.match(
                        r"^['\"][^'\"]*['\"]\s+if\s*$", conv_stripped
                    ):
                        result.append(
                            todo_comment(
                                f"split ternary residue -> {conv_stripped[:80]}",
                                indent=piece_indent,
                            )
                        )
                        continue

                    # Elvis/ternary residue ``lhs = mid else rhs`` (missing ``if``).
                    elvis_res = re.match(
                        r"^([A-Za-z_][\w.]*(?:\[[^\]]+\])?)\s*=\s*(.+?)\s+else\s+(.+)$",
                        conv_stripped,
                    )
                    if elvis_res and " if " not in conv_stripped:
                        lhs, mid, rhs = (
                            elvis_res.group(1),
                            elvis_res.group(2).strip(),
                            elvis_res.group(3).strip(),
                        )
                        # Simple name/attr mid → ``lhs = (mid if mid else rhs)``.
                        if re.fullmatch(r"[A-Za-z_][\w.]*(?:\[[^\]]+\])?", mid):
                            result.append(f"{piece_indent}{lhs} = ({mid} if {mid} else {rhs})")
                            continue
                        # Comparison mid without then-arm → one TODO (cannot recover).
                        result.append(
                            todo_comment(
                                f"ternary residue -> {conv_stripped[:80]}",
                                indent=piece_indent,
                            )
                        )
                        continue

                    # Half-converted collectEntries / filter(k, v ->) → one TODO.
                    if (
                        "__TEKTONIZE_COLLECT_ENTRIES__" in conv_stripped
                        or "collectEntries" in conv_stripped
                        or re.search(
                            r"\.filter\(\s*lambda\s+it:\s*\w+\s*,\s*\w+\s*->",
                            conv_stripped,
                        )
                        or re.search(r"\bfilter\(lambda it:\s*\w+\s*,", conv_stripped)
                        or re.match(r"^\}\.keys\(\)", conv_stripped)
                        or re.match(r"^v\.get\(", conv_stripped)
                    ):
                        result.append(
                            todo_comment(
                                "collectEntries/filter map -> dict comprehension "
                                f"({conv_stripped[:60]})",
                                indent=piece_indent,
                            )
                        )
                        continue

                    # Validate that the line is valid Python-ish
                    if self._is_valid_python_line(piece):
                        result.append(piece)
                        self._nudge_dict_brace_depth(conv_stripped)
                        self._nudge_call_depths(conv_stripped)
                        if conv_stripped and not conv_stripped.startswith("#"):
                            has_valid_line = True
                        # Open triple-quoted assignment: following lines are content.
                        # Ignore TaskStep / TODO comments that merely contain ``="""``.
                        if not conv_stripped.startswith("#"):
                            tq = re.search(r"""=\s*(\"\"\"|\'\'\')\s*$""", conv_stripped)
                            if tq:
                                triple_quote = tq.group(1)
                    elif conv_stripped:
                        # Failed opener: TODO-absorb the tail instead of GROOVY cascade.
                        if re.search(r"\(\s*\{\s*$", conv_stripped) or (
                            re.search(r"\(\s*$", conv_stripped)
                            and re.match(r"^[a-zA-Z_]", conv_stripped)
                        ):
                            result.append(
                                todo_comment(
                                    f"call-with-args -> {conv_stripped}",
                                    indent=piece_indent,
                                )
                            )
                            call_absorb_depth = max(
                                1,
                                conv_stripped.count("(")
                                - conv_stripped.count(")")
                                + conv_stripped.count("{")
                                - conv_stripped.count("}"),
                            )
                            break
                        # Multi-stmt glued residue after for/if split → one TODO.
                        if re.search(
                            r"\b(?:for|if|elif)\b[^:]*:\S",
                            conv_stripped,
                        ) or (
                            conv_stripped.count(";") >= 1
                            and conv_stripped.count("{") + conv_stripped.count("}") >= 2
                        ):
                            result.append(
                                todo_comment(
                                    f"glued control-body residue -> {conv_stripped[:80]}",
                                    indent=piece_indent,
                                )
                            )
                        elif re.match(r"^if\s*\(.+\)\s*\{", conv_stripped):
                            result.append(
                                todo_comment(
                                    f"brace-if residue -> {conv_stripped[:80]}",
                                    indent=piece_indent,
                                )
                            )
                        else:
                            result.append(f"# GROOVY: {conv_stripped}")

        result = self._ensure_block_bodies(result)
        result = self._clamp_indent_jumps(result)
        result = self._comment_orphan_clauses(result)
        # Second clamp pass: commenting-out clauses can leave orphaned deeper
        # lines (e.g. ``pass`` after a commented ``else:``).
        result = self._clamp_indent_jumps(result)
        result = self._revive_groovy_else(result)
        result = self._absorb_orphan_elif(result)
        result = self._absorb_orphan_closers(result)
        result = self._absorb_call_with_map_groovy(result)
        result = self._revive_ternary_residue(result)
        result = self._absorb_job_dsl_and_stages(result)
        result = self._absorb_groovy_dict_entry_runs(result)
        result = self._absorb_shell_fragment_groovy(result)
        result = self._absorb_prose_groovy(result)

        # Rewrite bare local/param uses to match snake_case declarations.
        if renames:
            result = [self._apply_local_renames(ln, renames) for ln in result]

        # Re-check after post-processing: _comment_orphan_clauses may have
        # commented out all non-comment lines that originally set has_valid_line.
        if has_valid_line:
            has_valid_line = any(ln.strip() and not ln.strip().startswith("#") for ln in result)
        if not has_valid_line:
            result.append(f"pass  # {todo_text('convert method body')}")

        return result if result else ["pass"]

    @staticmethod
    def _collect_local_renames(body: str, param_names: list[str] | None = None) -> dict[str, str]:
        """Map Groovy local/param camelCase names to snake_case Python names."""
        renames: dict[str, str] = {}

        def _add(name: str) -> None:
            if not name or name == "self":
                return
            new = convert_identifier(name)
            if new != name:
                renames[name] = new

        for name in param_names or []:
            _add(name)

        # Typed / def locals: ``String commandLogName = …`` / ``def exitCode = …``
        for m in re.finditer(
            r"(?m)^\s*(?:def|[\w.]+(?:\s*<[^;\n]+>)?)\s+(\w+)\s*=",
            body,
        ):
            type_or_def = m.group(0)
            # Skip control-flow false positives: ``if x =``, ``return x =``
            lead = type_or_def.lstrip()
            first = lead.split(None, 1)[0] if lead else ""
            if first in (
                "if",
                "for",
                "while",
                "return",
                "throw",
                "elif",
                "else",
                "assert",
            ):
                continue
            _add(m.group(1))

        # for (Type x : …) / for (x in …) / for Type x in …
        for m in re.finditer(
            r"\bfor\s*\(\s*(?:(?:[\w.]+)(?:\s*<[^>]+>)?\s+)?(\w+)\s*(?::|in)\b",
            body,
        ):
            _add(m.group(1))
        for m in re.finditer(
            r"\bfor\s+(?:(?:[\w.]+)(?:\s*<[^>]+>)?\s+)?(\w+)\s+in\b",
            body,
        ):
            _add(m.group(1))

        return renames

    @staticmethod
    def _apply_local_renames(text: str, renames: dict[str, str]) -> str:
        """Rewrite bare identifiers per ``renames``; keep attrs and string keys.

        - ``commandLogName`` → ``command_log_name``
        - ``config.commandLogName`` unchanged (attribute / Map key access)
        - ``\"archiveLog\"`` unchanged (string literal key)
        - ``f\"…{commandLogName}…\"`` interpolations are rewritten
        """
        if not renames or not text:
            return text

        names = sorted(renames.keys(), key=len, reverse=True)
        ident_re = re.compile(r"\b(" + "|".join(re.escape(n) for n in names) + r")\b")

        def rewrite_code(chunk: str) -> str:
            """Rename idents not preceded by ``.``."""

            def repl(m: re.Match[str]) -> str:
                start = m.start()
                if start > 0 and chunk[start - 1] == ".":
                    return m.group(0)
                return renames[m.group(1)]

            return ident_re.sub(repl, chunk)

        def rewrite_fstring_body(body: str) -> str:
            """Rename inside ``{expr}`` parts of an f-string body only."""
            out: list[str] = []
            i = 0
            n = len(body)
            while i < n:
                if body[i] == "{":
                    if i + 1 < n and body[i + 1] == "{":
                        out.append("{{")
                        i += 2
                        continue
                    depth = 1
                    j = i + 1
                    while j < n and depth:
                        if body[j] == "{":
                            depth += 1
                        elif body[j] == "}":
                            depth -= 1
                        j += 1
                    expr = body[i + 1 : j - 1] if depth == 0 else body[i + 1 :]
                    out.append("{")
                    out.append(rewrite_code(expr))
                    if depth == 0:
                        out.append("}")
                    i = j if depth == 0 else n
                    continue
                if body[i] == "}" and i + 1 < n and body[i + 1] == "}":
                    out.append("}}")
                    i += 2
                    continue
                out.append(body[i])
                i += 1
            return "".join(out)

        out: list[str] = []
        i = 0
        n = len(text)
        while i < n:
            # Line comments — rewrite freely (helps # GROOVY: / # | shell notes).
            if text[i] == "#":
                j = text.find("\n", i)
                if j < 0:
                    out.append(rewrite_code(text[i:]))
                    break
                out.append(rewrite_code(text[i:j]))
                i = j
                continue

            # Triple-quoted strings
            if text.startswith('"""', i) or text.startswith("'''", i):
                quote = text[i : i + 3]
                is_f = i >= 1 and text[i - 1] == "f"
                j = i + 3
                while j < n:
                    if text.startswith(quote, j):
                        j += 3
                        break
                    if text[j] == "\\" and j + 1 < n:
                        j += 2
                        continue
                    j += 1
                inner = text[i + 3 : j - 3] if j >= i + 6 else text[i + 3 :]
                out.append(quote)
                out.append(rewrite_fstring_body(inner) if is_f else inner)
                if j >= i + 6:
                    out.append(quote)
                i = j
                continue

            # Single/double-quoted strings (incl. f"…")
            if text[i] in ('"', "'"):
                quote = text[i]
                is_f = i >= 1 and text[i - 1] == "f"
                j = i + 1
                while j < n:
                    if text[j] == "\\" and j + 1 < n:
                        j += 2
                        continue
                    if text[j] == quote:
                        j += 1
                        break
                    j += 1
                inner = text[i + 1 : j - 1] if j > i + 1 and text[j - 1] == quote else text[i + 1 :]
                out.append(quote)
                out.append(rewrite_fstring_body(inner) if is_f else inner)
                if j > i + 1 and text[j - 1] == quote:
                    out.append(quote)
                i = j
                continue

            # Run of non-string code until next quote / comment / EOL handling
            j = i
            while (
                j < n and text[j] not in ('"', "'", "#") and not text.startswith(('"""', "'''"), j)
            ):
                j += 1
            if j == i:
                # Lone # mid-token shouldn't happen often; copy one char.
                out.append(text[i])
                i += 1
                continue
            out.append(rewrite_code(text[i:j]))
            i = j

        return "".join(out)

    @staticmethod
    def _unwrap_method_body(body_text: str) -> str:
        """Remove outer ``{…}`` and uniformly dedent the body.

        A plain ``strip()`` after dropping braces only removes indent from the
        *first* line, so later sibling statements keep deeper indent and nest
        under the first ``if``/``for``. Dedent by the minimum indent of all
        non-empty lines so brace-relative Groovy layout becomes Python-relative.
        """
        body = body_text.strip()
        if body.startswith("{"):
            body = body[1:]
        if body.endswith("}"):
            body = body[:-1]
        lines = body.split("\n")
        nonempty = [ln for ln in lines if ln.strip()]
        if not nonempty:
            return ""
        min_indent = min(len(ln) - len(ln.lstrip()) for ln in nonempty)
        out: list[str] = []
        for ln in lines:
            if not ln.strip():
                out.append("")
                continue
            if min_indent and ln[:min_indent].strip() == "":
                out.append(ln[min_indent:])
            else:
                out.append(ln.lstrip())
        while out and not out[0].strip():
            out.pop(0)
        while out and not out[-1].strip():
            out.pop()
        return "\n".join(out)

    def _is_valid_python_line(self, line: str) -> bool:
        """Quick check if a line looks like valid Python (not raw Groovy).

        This is deliberately aggressive — we'd rather comment out a valid line
        than emit an invalid one, since the goal is compilable Python output.
        """
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            return True

        # Multiline call / list continuations (before unmatched-close reject).
        if (self._paren_depth > 0 or self._bracket_depth > 0) and self._is_call_continuation(
            stripped
        ):
            return True

        # Triple-quote delimiters: keep live so ``x = """`` … ``"""`` assignments
        # close. Commenting the closer as ``# GROOVY: """`` leaves an open string
        # and the emit sanitizer cascades GROOVY over the rest of the file.
        if stripped in ('"""', "'''") or re.match(r'^("""|\'\'\')\)?\s*,?\s*$', stripped):
            return True
        if stripped.startswith('"""') or stripped.startswith("'''"):
            return bool(self._paren_depth > 0 and re.match(r'^("""|\'\'\')\)?\s*,?\s*$', stripped))

        # Dict closers / entries from map→dict rewrite (before unmatched-``}`` check).
        # String keys may include hyphens (``'ui-controller':``).
        if re.match(r"^\}\s*,?\s*$", stripped):
            return True
        if re.match(r"^('[^']*'|\"[^\"]*\")\s*:", stripped):
            return True

        # Reject lines with unmatched closing delimiters — always invalid.
        if self._has_unmatched_close_delimiters(stripped):
            return False

        # Shell fragments that leaked from Groovy GString / sh blocks.
        if r"\$" in stripped or r"\((" in stripped:
            return False

        # Groovy single-line if without colon:  if (cond) return x
        if re.match(r"^if\s*\(.+\)\s+(?!.*:\s*$)\S", stripped):
            return False

        # Lines ending with a trailing continuation operator whose
        # continuation was (or will be) commented out.
        if re.match(r".+[+\\]\s*$", stripped) and not stripped.endswith("\\\\"):
            # Heuristic: trailing + or \ is almost always a broken
            # multi-line expression in the conversion output.
            return False

        # Accept well-known Python patterns
        if stripped in ("pass", "return", "break", "continue", "else:", "try:", "finally:"):
            return True

        # Decorators left on converted methods.
        if re.match(r"^@(?:staticmethod|classmethod|property)\b", stripped):
            return True

        # Thin vars / top-level ``def some_pipeline_*(…):``
        if re.match(r"^def\s+[a-z_][\w]*\s*\(", stripped):
            return not self._has_groovy_syntax(stripped)

        # Allowlisted safe-nav expansions from normalize (balanced).
        if (
            "(lambda _o: getattr(_o," in stripped
            and not self._has_unmatched_close_delimiters(stripped)
            and not self._has_groovy_syntax(stripped)
        ):
            return True

        safe_starters = (
            "return ",
            "raise ",
            "if ",
            "elif ",
            "for ",
            "while ",
            "except ",
            "print(",
            "match ",
            "case ",
            "assert ",
            "import ",
            "from ",
            "def ",
            "class ",
            "def ",
            "yield ",
            "del ",
            "global ",
            "nonlocal ",
            "with ",
        )
        if any(stripped.startswith(s) for s in safe_starters):
            # Unconverted Groovy map opener (expr rewrite should have made ``return {``).
            if re.match(r"^return\s*\[", stripped) and (
                stripped.rstrip().endswith("[") or re.search(r"(?<![\"'])\w+\s*:", stripped)
            ):
                return False
            # ``return {`` / ``return {…}`` after map→dict rewrite.
            if re.match(r"^return\s*\{", stripped):
                return True
            # Multiline ``raise Exception(`` — PascalCase is a type, not Groovy ``new``.
            if re.match(r"^raise\s+[A-Za-z_]\w*\s*\(", stripped):
                return True
            return not self._has_groovy_syntax(stripped)

        # Accept assignment / augmented-assignment lines with snake_case LHS
        aug = re.match(
            r"^([a-z_][\w]*(?:\.\w+)*(?:\[.*\])?)\s*([+\-*/%]?=)\s*(.*)$",
            stripped,
        )
        if aug and aug.group(2) != "==":
            lhs, _op, rhs = aug.group(1), aug.group(2), aug.group(3).strip()
            # Python keywords as attributes on the LHS: obj.pass, obj.class, etc.
            if re.search(
                r"\.(pass|return|import|class|def|for|while|if|else|try|except|finally)\b",
                lhs,
            ):
                return False
            # Dict opener after map rewrite; bare ``[`` is still Groovy/list.
            if rhs == "{":
                return True
            if rhs == "[":
                return False
            return not self._has_groovy_syntax(rhs)

        # Dict entries / closers from multiline map→dict rewrite.
        if re.match(r"^('[^']*'|\"[^\"]*\")\s*:", stripped):
            return True
        # Bare / ident keys while inside an open dict (map→dict peel).
        if self._dict_brace_depth > 0 and (
            re.match(r"^[A-Za-z_][\w]*\s*:", stripped)
            or re.match(r"^('[^']*'|\"[^\"]*\")\s*:", stripped)
            or re.match(r"^#\s", stripped)
            or re.match(r"^\}\s*,?\s*$", stripped)
            or re.match(r"^\{", stripped)
        ):
            return not self._has_groovy_syntax(stripped) or stripped.startswith("#")
        if re.match(r"^\}\s*,?\s*$", stripped):
            return True

        # Accept standalone function/method calls: func(args) or multiline opener.
        # ``func({`` / ``obj.method({`` — dict-arg call (Groovy map → Python dict).
        if re.match(r"^[a-z_][\w]*(\.\w+)*\(\{\s*$", stripped):
            return True
        if re.match(r"^[a-z_][\w]*(\.\w+)*\(\s*$", stripped):
            return True
        if re.match(r"^[a-z_][\w]*(\.\w+)*\(.*\)\s*$", stripped):
            return not self._has_groovy_syntax(stripped)
        # Line-start PascalCase call / constructor (BuildPipeline, Yaml).
        # Distinct from mid-identifier false positives (getOrDefault → OrDefault().
        if re.match(r"^[A-Z][A-Za-z0-9_]*\(\{\s*$", stripped):
            return True
        if re.match(r"^[A-Z][A-Za-z0-9_]*\(\s*$", stripped):
            return True
        if re.match(r"^[A-Z][A-Za-z0-9_]*\(.*\)\s*$", stripped):
            return not self._has_groovy_syntax(stripped)
        # Assignment of list-of-maps opener: ``postAlwaysClosures=[{``
        if re.match(r"^[a-z_][\w]*(\.\w+)*\s*=\s*\[\s*\{\s*$", stripped):
            return True
        # Dict-arg / list-of-map closers only while a call or dict is open.
        if re.match(r"^\}\)\s*,?\s*$", stripped):
            return self._paren_depth > 0 or self._dict_brace_depth > 0
        if re.match(r"^\}\]\s*,?\s*$", stripped) or stripped in ("}]", "}],"):
            return self._bracket_depth > 0 or self._dict_brace_depth > 0
        # Kwarg-only continuation lines (after named-arg rewrite).
        if re.match(r"^[A-Za-z_]\w*\s*=\s*.+,?\s*$", stripped):
            return not self._has_groovy_syntax(stripped)

        # Everything else is suspicious
        return False

    @staticmethod
    def _absorb_call_with_map_groovy(lines: list[str]) -> list[str]:
        """Collapse ``# GROOVY: foo({`` … ``# GROOVY: })`` cascades into one TODO.

        When a dict-arg call opener still fails validity, map entries and closers
        become a trail of ``# GROOVY:`` noise — replace the whole span with a
        single ``# TEKTONIZE-TODO: call-with-map …``.
        """
        if not lines:
            return lines
        opener_re = re.compile(
            r"^# GROOVY:\s*.+\(\{\s*$|"
            r"^# GROOVY:\s*[a-z_][\w.]*\s*=\s*\[\s*\{\s*$|"
            r"^# GROOVY:\s*[a-z_][\w.]*\s*=\s*\{\s*$"
        )
        entry_re = re.compile(r"^# GROOVY:\s*(?:(['\"]).*\1|[A-Za-z_]\w*)\s*:")
        closer_re = re.compile(r"^# GROOVY:\s*(?:\}\)\s*,?|\}\]\s*,?|\}\s*,?|\]\s*,?|\)\s*,?)\s*$")
        result: list[str] = []
        i = 0
        while i < len(lines):
            stripped = lines[i].strip()
            indent = lines[i][: len(lines[i]) - len(lines[i].lstrip())]
            if opener_re.match(stripped):
                # Summarize opener text for the TODO message.
                body = stripped[len("# GROOVY:") :].strip()
                j = i + 1
                while j < len(lines):
                    nxt = lines[j].strip()
                    if not nxt:
                        j += 1
                        continue
                    if entry_re.match(nxt) or closer_re.match(nxt) or nxt.startswith("# GROOVY:"):
                        done = bool(closer_re.match(nxt))
                        j += 1
                        if done:
                            # Consume consecutive closer-only GROOVY lines.
                            while j < len(lines) and closer_re.match(lines[j].strip()):
                                j += 1
                            break
                        continue
                    break
                result.append(todo_comment(f"call-with-map -> {body}", indent=indent))
                i = j
                continue
            result.append(lines[i])
            i += 1
        return result

    @staticmethod
    def _absorb_orphan_closers(lines: list[str]) -> list[str]:
        """Drop punctuation closers / stub ``pass``/``else:`` that only close debris.

        When the previous line is ``# GROOVY:`` / ``# TEKTONIZE-TODO:`` /
        ``# TaskStep``, bare ``\"\"\")``, ``}})``, ``}}``, ``}) {``, ``])``,
        or ``# GROOVY: pass`` / ``# GROOVY: else:`` residue is noise from a
        partial convert — absorb (drop) it.

        Do **not** absorb single ``}`` / ``)`` after live dict entries or
        call args — those closers are structural Python.
        """
        opener_prefixes = (
            "# GROOVY:",
            "# TEKTONIZE-TODO:",
            "# TaskStep",
            "# --- Stage:",
            "# | ",
            "# on_error=",
        )
        # Closers that only make sense as debris after a commented opener.
        # Do **not** absorb bare ``"""`` / ``'''`` — those close ``x = """``
        # assignments; dropping them leaves an open string for the sanitizer.
        closer_re = re.compile(
            r"^(?:"
            r'("""|\'\'\')\)\s*,?\s*'  # """) shell/call closer only
            r"|\)\}\s*\{\s*"
            r"|\}\)\s*\{\s*"
            r"|\)\}\s*"
            r"|\}{2,}\)\s*"  # }}) / }}})
            r"|\}{2,}\s*"  # }} / }}}
            r"|\}\)\s*"
            r"|\]\)\s*,?\s*"
            r"|\]\s*,?\s*"
            r"|\)\s*,?\s*"
            r"|\}\s*,?\s*"
            r")$"
        )
        stub_re = re.compile(r"^(?:pass|else:|finally:|continue|break)\s*$")
        # Orphan try/except debris after a commented span.
        orphan_clause_re = re.compile(
            r"^(?:else:|else\s*\{|pass|finally:|continue|break|"
            r"except\s+\w+(?:\s+as\s+\w+)?\s*:)\s*$"
        )
        # Multi-brace debris may follow a finished live line (half-emitted call).
        multi_brace_re = re.compile(r"^\}{2,}\)?\s*,?\s*$")
        # Shell closer debris: ``""") }`` / ``""") } }``
        shell_closer_re = re.compile(r'^("""|\'\'\')\)?\s*(?:\}\s*)+$')

        # TODO/TaskStep openers — live ``else:``/``pass`` after these is debris.
        # Bare ``# GROOVY:`` body lines must NOT swallow a following live ``else:``.
        todo_opener_prefixes = (
            "# TEKTONIZE-TODO:",
            "# TaskStep",
            "# --- Stage:",
            "# | ",
            "# on_error=",
        )

        out: list[str] = []
        for line in lines:
            stripped = line.strip()
            # Normalize already-commented closers / stub pass / else
            content = stripped
            is_groovy_comment = stripped.startswith("# GROOVY:")
            if is_groovy_comment:
                content = stripped[len("# GROOVY:") :].strip()
            if out:
                prev = out[-1].strip()
                prev_is_opener = any(prev.startswith(p) for p in opener_prefixes)
                prev_is_todo = any(prev.startswith(p) for p in todo_opener_prefixes)
                prev_is_groovy = prev.startswith("# GROOVY:")
                if prev_is_opener and closer_re.match(content):
                    continue
                # Shell ``""") }`` after TaskStep / GROOVY / TODO.
                if (prev_is_opener or prev_is_groovy) and shell_closer_re.match(content):
                    continue
                # Already-commented stubs after any commented opener.
                if is_groovy_comment and stub_re.match(content) and prev_is_opener:
                    continue
                # Live else:/pass only after TODO/TaskStep (not # GROOVY: args).
                if (
                    stub_re.match(content)
                    and not is_groovy_comment
                    and not stripped.startswith("#")
                    and prev_is_todo
                ):
                    continue
                # ``}})`` / ``}}`` after a live statement is always debris.
                if multi_brace_re.match(content) and prev and not prev.endswith((":", "{")):
                    continue
            # After revive: remaining ``# GROOVY: else:`` / ``pass`` / ``except`` /
            # ``finally:`` stubs are never useful — drop regardless of previous line
            # (brace-unwrap often leaves them after live if-bodies).
            if is_groovy_comment and orphan_clause_re.match(content):
                continue
            out.append(line)
        return out

    @staticmethod
    def _revive_ternary_residue(lines: list[str]) -> list[str]:
        """Rejoin ``split ternary residue -> THEN if`` + ``lhs = COND else RHS``.

        Wave 36: after a bad ternary split, revive
        ``lhs = (THEN if COND else RHS)`` as one live line.
        """
        out: list[str] = []
        i = 0
        while i < len(lines):
            line = lines[i]
            stripped = line.strip()
            indent = line[: len(line) - len(line.lstrip())]
            m_todo = re.match(
                r"^# TEKTONIZE-TODO: split ternary residue -> (.+?)\s+if\s*$",
                stripped,
            )
            if m_todo and i + 1 < len(lines):
                then_arm = m_todo.group(1).strip()
                nxt = lines[i + 1].strip()
                body = nxt[len("# GROOVY:") :].strip() if nxt.startswith("# GROOVY:") else nxt
                m_else = re.match(
                    r"^([A-Za-z_][\w.]*)\s*=\s*(.+?)\s+else\s+(.+)$",
                    body,
                )
                if m_else and " if " not in body:
                    lhs = m_else.group(1)
                    cond = m_else.group(2).strip()
                    rhs = m_else.group(3).strip()
                    out.append(f"{indent}{lhs} = ({then_arm} if {cond} else {rhs})")
                    i += 2
                    continue
            out.append(line)
            i += 1
        return out

    @staticmethod
    def _absorb_job_dsl_and_stages(lines: list[str]) -> list[str]:
        """Collapse Job DSL / ``stages[…] = {`` / ``with*`` GROOVY spans to one TODO."""
        opener_res = [
            (
                re.compile(r"^# GROOVY:\s*definition\s*\{"),
                "Job DSL definition block",
            ),
            (
                re.compile(r"^# GROOVY:\s*(?:\w[\w.]*\.)?pipelineJob\s*\("),
                "Job DSL pipelineJob",
            ),
            (
                re.compile(r"^# GROOVY:\s*stages\s*\["),
                "stages map closure",
            ),
            (
                re.compile(r"^# GROOVY:\s*.*\.each\s*\{"),
                "each closure",
            ),
            (
                re.compile(
                    r"^# GROOVY:\s*(?:buildDiscarder|logRotator|cron|parameters|"
                    r"triggers|pipelineTriggers|parameterizedTimerTrigger)\s*\{"
                ),
                "Job DSL block",
            ),
            (
                re.compile(r"^# GROOVY:\s*(?:\w[\w.]*\.)?with\w+\s*[\({]"),
                "with* wrapper",
            ),
            (
                re.compile(r"^# GROOVY:\s*param_specs\.each\b"),
                "Job DSL param_specs.each",
            ),
            (
                re.compile(r"^# GROOVY:\s*collectDiagnostics\s*\("),
                "collectDiagnostics shell helper",
            ),
        ]
        out: list[str] = []
        i = 0
        while i < len(lines):
            stripped = lines[i].strip()
            indent = lines[i][: len(lines[i]) - len(lines[i].lstrip())]
            matched_label: str | None = None
            for cre, label in opener_res:
                if cre.match(stripped):
                    matched_label = label
                    break
            if matched_label is None:
                out.append(lines[i])
                i += 1
                continue
            # Absorb following # GROOVY: lines that continue the brace span.
            depth = stripped.count("{") - stripped.count("}")
            j = i + 1
            while j < len(lines) and depth > 0:
                nxt = lines[j].strip()
                if not nxt:
                    j += 1
                    continue
                if nxt.startswith("# GROOVY:"):
                    body = nxt[len("# GROOVY:") :]
                    depth += body.count("{") - body.count("}")
                    j += 1
                    continue
                if nxt.startswith("# TEKTONIZE-TODO:") or nxt.startswith("# TaskStep"):
                    j += 1
                    continue
                break
            out.append(todo_comment(f"{matched_label} -> not converted", indent=indent))
            i = j
        return out

    @staticmethod
    def _absorb_groovy_dict_entry_runs(lines: list[str]) -> list[str]:
        """Collapse runs of ``# GROOVY:`` dict-entry / comment peel into one TODO."""
        entry_re = re.compile(
            r"^# GROOVY:\s*(?:"
            r"\([^)]+\)\s*:\s*\{|"  # (CONST): {
            r"['\"][^'\"]+['\"]\s*:|"  # 'k': / \"k\":
            r"//|"  # // comment
            r"['\"][^'\"]+['\"]\s*,|"  # list string entry
            r"\}\s*,?|"  # },
            r"\{"  # nested {
            r")"
        )
        out: list[str] = []
        i = 0
        while i < len(lines):
            if not entry_re.match(lines[i].strip()):
                out.append(lines[i])
                i += 1
                continue
            indent = lines[i][: len(lines[i]) - len(lines[i].lstrip())]
            j = i + 1
            while j < len(lines) and (entry_re.match(lines[j].strip()) or not lines[j].strip()):
                j += 1
            if j - i >= 2:
                out.append(
                    todo_comment(
                        "dict/list literal peel -> keep as Python literal",
                        indent=indent,
                    )
                )
            else:
                out.append(lines[i])
                j = i + 1
            i = j
        return out

    @staticmethod
    def _absorb_shell_fragment_groovy(lines: list[str]) -> list[str]:
        """Turn shell-body ``# GROOVY: oc/git/set/…`` lines into ``# |`` fragments."""
        shell_re = re.compile(
            r"^# GROOVY:\s*(?:"
            r"oc\s|kubectl\s|git\s|set\s|export\s|source\s|echo\s|grep\s|"
            r"ansible-|pip\s|python3?\s|mkdir\s|chmod\s|cat\s|tail\s|which\s|"
            r"helm\s|terraform\s|aws\s|az\s"
            r")"
        )
        out: list[str] = []
        for line in lines:
            stripped = line.strip()
            indent = line[: len(line) - len(line.lstrip())]
            if shell_re.match(stripped):
                body = stripped[len("# GROOVY:") :].strip()
                out.append(f"{indent}# | {body}")
            else:
                out.append(line)
        return out

    @staticmethod
    def _absorb_prose_groovy(lines: list[str]) -> list[str]:
        """Turn doc/help/YAML prose ``# GROOVY:`` lines into plain ``#`` comments.

        Job DSL help text, YAML keys, and numbered lists often leak as GROOVY
        after heredoc mishaps — they are not executable and should not count
        as conversion failures.
        """
        prose_re = re.compile(
            r"^# GROOVY:\s*(?:"
            r"[-*]\s|"  # markdown / YAML list
            r"\d+\.\s|"  # numbered list
            r"(?:Examples|Conditions|Notes|Description|Usage|WARNING|NOTE)\s*:|"
            r"[A-Za-z][\w\s,/'-]{0,60}:\s*$|"  # bare prose label
            r"Leave blank|"
            r"'''|"  # stray closer crumbs from help strings
            r'"""'
            r")"
        )
        # Lines that are clearly not code: no assignment, no call, mostly words.
        soft_prose_re = re.compile(r"^# GROOVY:\s+[A-Za-z][^={}()\[\]]{10,}$")
        out: list[str] = []
        for line in lines:
            stripped = line.strip()
            indent = line[: len(line) - len(line.lstrip())]
            if not stripped.startswith("# GROOVY:"):
                out.append(line)
                continue
            body = stripped[len("# GROOVY:") :].strip()
            if prose_re.match(stripped) or (
                soft_prose_re.match(stripped)
                and not re.search(r"\b(def|return|if|for|while|class)\b", body)
                and "=" not in body
            ):
                out.append(f"{indent}# {body}")
            else:
                out.append(line)
        return out

    @staticmethod
    def _revive_groovy_else(lines: list[str]) -> list[str]:
        """Un-comment ``# GROOVY: else:`` / ``elif`` when a live ``if`` matches indent.

        Inverse of ``_comment_orphan_clauses`` for the brace-unwrap case where
        ``else:`` was wrapped as GROOVY but the matching ``if`` stayed live.
        """
        out: list[str] = []
        if_levels: list[int] = []
        for line in lines:
            stripped = line.strip()
            indent = len(line) - len(line.lstrip())
            prefix = " " * indent

            if not stripped:
                out.append(line)
                continue

            while if_levels and if_levels[-1] > indent:
                if_levels.pop()

            if not stripped.startswith("#") and stripped.startswith("if "):
                if_levels.append(indent)
                out.append(line)
                continue

            if stripped == "# GROOVY: else:" and if_levels and if_levels[-1] == indent:
                # Only revive when still inside the if body (prev line deeper).
                # A sibling at the same indent (e.g. print after ``if: pass``)
                # means ``else`` would be a SyntaxError — leave GROOVY for absorb.
                prev_indent: int | None = None
                for prev in reversed(out):
                    ps = prev.strip()
                    if not ps or ps.startswith("#"):
                        continue
                    prev_indent = len(prev) - len(prev.lstrip())
                    break
                if prev_indent is not None and prev_indent > indent:
                    if_levels.pop()
                    out.append(f"{prefix}else:")
                    continue

            # Revive ``# GROOVY: elif …:`` when a live if/elif exists at indent.
            if (
                stripped.startswith("# GROOVY: elif ")
                and stripped.endswith(":")
                and if_levels
                and if_levels[-1] == indent
            ):
                content = stripped[len("# GROOVY: ") :].strip()
                out.append(f"{prefix}{content}")
                continue

            # Live else/elif consumes matching if (keep stacks honest).
            if not stripped.startswith("#") and stripped == "else:":
                if if_levels and if_levels[-1] == indent:
                    if_levels.pop()
                out.append(line)
                continue
            if not stripped.startswith("#") and stripped.startswith("elif "):
                out.append(line)
                continue

            out.append(line)
        return out

    @staticmethod
    def _absorb_orphan_elif(lines: list[str]) -> list[str]:
        """Turn remaining ``# GROOVY: elif …:`` into TODO (no live if to revive)."""
        out: list[str] = []
        for line in lines:
            stripped = line.strip()
            indent = line[: len(line) - len(line.lstrip())]
            if stripped.startswith("# GROOVY: elif ") and stripped.endswith(":"):
                out.append(
                    todo_comment(
                        f"orphan elif -> {stripped[len('# GROOVY: '):][:80]}",
                        indent=indent,
                    )
                )
                continue
            if stripped in ("# GROOVY: default:", "# GROOVY: default"):
                out.append(
                    todo_comment(
                        "switch/case -> match/case (not converted)",
                        indent=indent,
                    )
                )
                continue
            out.append(line)
        return out

    def _nudge_dict_brace_depth(self, stripped: str) -> None:
        """Update ``_dict_brace_depth`` from an emitted (non-comment) line."""
        if not stripped or stripped.startswith("#"):
            return
        self._dict_brace_depth += stripped.count("{") - stripped.count("}")
        if self._dict_brace_depth < 0:
            self._dict_brace_depth = 0

    def _nudge_call_depths(self, stripped: str) -> None:
        """Update paren/bracket depth from an emitted (non-comment) line."""
        if not stripped or stripped.startswith("#"):
            return
        # Ignore braces — those are tracked separately for dicts.
        self._paren_depth += stripped.count("(") - stripped.count(")")
        self._bracket_depth += stripped.count("[") - stripped.count("]")
        if self._paren_depth < 0:
            self._paren_depth = 0
        if self._bracket_depth < 0:
            self._bracket_depth = 0

    @staticmethod
    def _is_call_continuation(stripped: str) -> bool:
        """True for closers / args that belong inside an open ``(`` or ``[``."""
        if re.match(r"^\)\s*,?\s*$", stripped):
            return True
        if re.match(r"^\]\s*,?\s*$", stripped):
            return True
        if stripped in ("})", "}) {", "),", "],"):
            return True
        if re.match(r'^("""|\'\'\')\)?\s*,?\s*$', stripped):
            return True
        # String / f-string / number / bare name arg lines.
        if re.match(r"^f?(['\"]).*\1,?\s*$", stripped, re.DOTALL):
            return True
        if re.match(r"^-?\d+(\.\d+)?,?\s*$", stripped):
            return True
        if re.match(r"^[A-Za-z_][\w.]*\s*,?\s*$", stripped):
            return True
        # Kwargs after named-arg rewrite (Wave 3): ``name='X',``
        return bool(re.match(r"^[A-Za-z_]\w*\s*=\s*.+,?\s*$", stripped))

    def _has_unmatched_close_delimiters(self, text: str) -> bool:
        """Return True when text has more closing ``)``, ``]``, or ``}`` than opening ones.

        Skips content inside string literals to avoid false positives.
        """
        depth = {"(": 0, "[": 0, "{": 0}
        opener_for = {")": "(", "]": "[", "}": "{"}
        in_string: str | None = None
        i = 0
        while i < len(text):
            c = text[i]
            if in_string:
                if c == "\\" and i + 1 < len(text):
                    i += 2
                    continue
                if c == in_string:
                    in_string = None
            else:
                if c in ('"', "'"):
                    in_string = c
                elif c in depth:
                    depth[c] += 1
                elif c in opener_for:
                    opener = opener_for[c]
                    if depth[opener] > 0:
                        depth[opener] -= 1
                    else:
                        return True
            i += 1
        return False

    @staticmethod
    def _split_glued_control_bodies(line: str) -> list[str]:
        """Split glued ``for``/``if``/``elif`` bodies (colon immediately followed by code).

        ``for x in xs:stmt`` (no space after ``:``) is a span-join / Groovy one-liner
        artifact. Valid Python one-liners use ``: `` (space) and are left alone.
        """
        indent = line[: len(line) - len(line.lstrip())]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            return [line]
        m = re.match(r"^((?:for|if|elif)\b[^:\n]*):(\S.*)$", stripped)
        if not m:
            return [line]
        header = m.group(1).rstrip()
        body = m.group(2)
        body_indent = indent + "    "
        pieces = [f"{indent}{header}:"]
        pieces.extend(MethodTransformer._split_glued_control_bodies(f"{body_indent}{body}"))
        return pieces

    def _has_groovy_syntax(self, text: str) -> bool:
        """Check if text contains Groovy-specific syntax."""
        # Keep params.FOO / env.BAR — UPPER_SNAKE attrs falsely match camelCase.
        text = mask_jenkins_binding_accesses(text)
        # Mask pre-parse safe-nav expansions so they are not rejected as Groovy.
        text = re.sub(
            r"\(\(lambda _o: getattr\(_o,\s*['\"][^'\"]+['\"]\)"
            r"(?:\([^)]*\))? if _o is not None else None\)\([^)]*\)\)",
            "__SAFE_NAV__",
            text,
        )
        # Mask string / f-string literals so ``@Library`` / ``key:`` inside
        # quotes are not treated as Groovy annotations / named params.
        text = re.sub(
            r"f?(?:'''[\s\S]*?'''|\"\"\"[\s\S]*?\"\"\"|'[^'\\]*(?:\\.[^'\\]*)*'|\"[^\"\\]*(?:\\.[^\"\\]*)*\")",
            '""',
            text,
        )
        groovy_indicators = [
            r"\bnew\s+\w+\s*\(",  # new ClassName(
            r"\?\.",  # safe navigation ?.
            r"\?:",  # elvis ?:
            r"\s->\s",  # closure arrow
            r"\{\s*$",  # trailing brace
            r"^\s*\}",  # lone closing brace
            r"\bscript\.",  # Jenkins script binding
            r"\bas\s+(Map|List|Set|String|Integer|Boolean)\b",  # Groovy cast
            r"\binstanceof\b",  # should be isinstance
            r"\.each\s*\{",  # .each { closure }
            r"\bthis\.",  # this. should be self.
            r"\)\s*\{",  # closure block
            # Do NOT flag ``[A-Z]\w*(`` — it matches inside camelCase
            # (getOrDefault → OrDefault(, putAll → All(, checkProvisionInventory).
            # PascalCase calls like Yaml() are valid Python after ``new`` strip.
            # camelCase attrs (config.unsupportedVersions) are valid Python / Map keys.
            r"(?<![?])\?(?![=.])",  # leftover ternary ?
            r"!\w",  # leftover groovy bang
            r"\+\s*\[\s*$",  # map/list concat opening
            r"@(?!staticmethod\b|classmethod\b|property\b)\w+",  # Groovy @field, not py decorators
            r"(?<![=\w])=~",  # Groovy =~ / ==~ (not ``re.match`` / equality)
            r"\bvoid\b",  # Groovy typed method leftover
            r"&&",  # should already be and; leftover is invalid Python
            r"\|\|",  # leftover or
            r"\bdef\s+\w+\s*=",  # Groovy local ``def x =`` (not ``def func(``)
            r"\b(String|boolean|Boolean|int|Integer|Map|List)\s+\w+",  # typed locals
            # Groovy named params — ignore ``key:`` inside quotes (print/f-string labels).
            r"(?:[(,]\s*)\w+\s*:\s*(?:'|\"|\(|\w)",  # Groovy named params: func(key: value)
            r"\['[^']+'\s*:",  # Groovy map in list brackets: ['key': value]
            r"\.pass\b",  # Python keyword as attribute (.pass)
            r"\breturn\b.*\breturn\b",  # leftover double-return from Groovy if (x) return
        ]
        return any(re.search(pattern, text) for pattern in groovy_indicators)

    def _transform_line(self, line: str) -> str | None:
        """Transform a single Groovy line to Python."""
        indent = line[: len(line) - len(line.lstrip())]
        stripped = line.strip()

        # Skip empty lines and lone ``{`` (orphan Groovy block). Keep ``}`` / ``},``
        # only while inside a dict opened by map→dict rewrite (else drop Groovy closers).
        if not stripped or stripped == "{":
            return ""
        if re.match(r"^\}\s*,?\s*$", stripped):
            if self._dict_brace_depth > 0:
                return indent + stripped
            return ""
        # Keep call/list closers while inside an open ``(`` / ``[``.
        if re.match(r"^\)\s*,?\s*$", stripped) or re.match(r"^\)\s*\}\s*$", stripped):
            if self._paren_depth > 0:
                return indent + stripped
            return ""
        if re.match(r"^\]\s*,?\s*$", stripped):
            if self._bracket_depth > 0:
                return indent + stripped
            return ""
        if re.match(r"^\)\}\s*$", stripped) or stripped == "})":
            if self._paren_depth > 0 or self._dict_brace_depth > 0:
                return indent + stripped
            return ""

        # Skip comments (preserve them)
        if stripped.startswith("//"):
            return indent + stripped.replace("//", "#", 1)
        if stripped.startswith("/*") or stripped.startswith("*"):
            return f"{indent}# {stripped}"
        # Already-emitted TaskStep / shell body / TODOs — do not re-run
        # ExpressionTransformer (would turn shell ``set +e`` into map-merge).
        if stripped.startswith(
            (
                "# TaskStep",
                "# | ",
                "# TEKTONIZE-TODO:",
                "# GROOVY:",
                "# --- Stage:",
                "# on_error=",
            )
        ):
            return line

        # Variable declarations
        result = self._transform_variable_declaration(stripped)
        if result is not None:
            return indent + result

        # Apply expression transforms
        result = self.expr_transformer.transform(stripped)

        # Handle specific patterns
        result = self._transform_return(result)
        result = self._transform_throw(result)
        result = self._transform_if_else(result)
        result = self._transform_for_loop(result)
        result = self._transform_try_catch(result)
        result = self._transform_switch(result)

        # Remove trailing semicolons
        result = result.rstrip(";")

        # Pattern helpers may already include indent when matching ``^(\s*)``;
        # only prefix when the transformed line is still unindented.
        if result and not result[0].isspace() and indent:
            return indent + result
        return result

    def _transform_variable_declaration(self, line: str) -> str | None:
        """Convert 'def x = value' or 'Type x = value' to 'x = value'."""
        # def x = value
        m = re.match(r"^def\s+(\w+)\s*=\s*(.+?)[\s;]*$", line)
        if m:
            name = m.group(1)
            value = self.expr_transformer.transform(m.group(2))
            return f"{convert_identifier(name)} = {value}"

        # Type x = value (generics may contain spaces: Map<String, List<Map>>)
        m = re.match(
            r"^((?:[\w.]+)(?:\s*<[^;={]+>)?)\s+(\w+)\s*=\s*(.+?)[\s;]*$",
            line,
        )
        if m:
            type_name = m.group(1).strip()
            # Skip if it looks like a method call / control keyword
            if "(" in type_name or type_name in (
                "if",
                "for",
                "while",
                "return",
                "throw",
                "new",
            ):
                return None
            # Require a type-like token (capitalized, or common Groovy types)
            root = type_name.split("<", 1)[0].split(".", 1)[-1]
            if not (
                root[:1].isupper()
                or root
                in (
                    "def",
                    "int",
                    "long",
                    "boolean",
                    "float",
                    "double",
                    "void",
                )
            ):
                return None
            name = m.group(2)
            value = self.expr_transformer.transform(m.group(3))
            return f"{convert_identifier(name)} = {value}"

        # Bare typed local without init: ``String imageUrl`` / ``Map foo;``
        m = re.match(
            r"^((?:[\w.]+)(?:\s*<[^;={]+>)?)\s+(\w+)\s*;?\s*$",
            line,
        )
        if m:
            type_name = m.group(1).strip()
            if "(" in type_name or type_name in (
                "if",
                "for",
                "while",
                "return",
                "throw",
                "new",
                "else",
                "try",
                "except",
                "finally",
                "class",
                "def",
            ):
                return None
            root = type_name.split("<", 1)[0].split(".", 1)[-1]
            if not (
                root[:1].isupper() or root in ("int", "long", "boolean", "float", "double", "void")
            ):
                return None
            return f"{convert_identifier(m.group(2))} = None"

        return None

    def _transform_return(self, line: str) -> str:
        """Transform return statements."""
        if line.strip().startswith("return "):
            return line
        return line

    def _transform_throw(self, line: str) -> str:
        """Convert 'throw new Exception(msg)' to 'raise Exception(msg)'."""
        m = re.match(r"^(\s*)throw\s+new\s+(\w+)\s*\((.+)\)\s*$", line)
        if m:
            indent = m.group(1)
            exc_type = m.group(2)
            msg = self.expr_transformer.transform(m.group(3))
            return f"{indent}raise {exc_type}({msg})"

        m = re.match(r"^(\s*)throw\s+(.+)$", line)
        if m:
            return f"{m.group(1)}raise {m.group(2)}"

        return line

    def _transform_if_else(self, line: str) -> str:
        """Convert if/else syntax."""
        # if (condition) { -> if condition:
        m = re.match(r"^(\s*)if\s*\((.+)\)\s*\{?\s*$", line)
        if m:
            cond = self.expr_transformer.transform(m.group(2))
            return f"{m.group(1)}if {cond}:"

        # } else if (condition) { -> elif condition:
        m = re.match(r"^(\s*)}\s*else\s+if\s*\((.+)\)\s*\{?\s*$", line)
        if m:
            cond = self.expr_transformer.transform(m.group(2))
            return f"{m.group(1)}elif {cond}:"

        # } else { -> else:
        m = re.match(r"^(\s*)}\s*else\s*\{?\s*$", line)
        if m:
            return f"{m.group(1)}else:"

        return line

    @staticmethod
    def _split_semicolon_statements(line: str) -> list[str]:
        """Split ``stmt; def x = …; def y = …`` into separate lines."""
        indent = line[: len(line) - len(line.lstrip())]
        stripped = line.strip()
        if "; def " not in stripped and "; String " not in stripped and "; Map " not in stripped:
            return [line]
        parts = re.split(r";\s*(?=def\s+|String\s+|Map\s+)", stripped)
        if len(parts) < 2:
            return [line]
        return [f"{indent}{p.strip()}" for p in parts if p.strip()]

    @staticmethod
    def _split_juxtaposed_declarations(line: str) -> list[str]:
        """Split glued ``x = y def z =`` / ``String a = … String b =`` meglines."""
        indent = line[: len(line) - len(line.lstrip())]
        stripped = line.strip()
        if stripped.startswith("#"):
            return [line]
        # Typed / def locals glued without semicolons.
        type_tok = (
            r"(?:def|String|Map|List|Boolean|boolean|Integer|int|Long|long|"
            r"Object|CollectionMigrationSettings|DeploymentType|"
            r"LegacyTestSuiteRunner)"
        )
        pieces: list[str] = []
        last = 0
        for m in re.finditer(rf"(?<!\S)(?=(?:{type_tok})\s+\w+\s*=)", stripped):
            if m.start() == 0:
                continue
            chunk = stripped[last : m.start()].strip()
            if chunk:
                pieces.append(chunk)
            last = m.start()
        tail = stripped[last:].strip()
        if tail:
            pieces.append(tail)
        if len(pieces) < 2:
            return [line]
        return [f"{indent}{p}" for p in pieces]

    @staticmethod
    def _split_brace_if_oneliner(line: str) -> list[str]:
        """Split ``if (cond) { stmt }`` into header + body (single stmt only)."""
        indent = line[: len(line) - len(line.lstrip())]
        stripped = line.strip()
        m = re.match(r"^if\s*\((.+)\)\s*\{\s*(.+)\s*\}\s*$", stripped)
        if not m:
            return [line]
        body = m.group(2).strip()
        # Multi-stmt / nested braces → leave for TODO residue path.
        if not body or "{" in body or ";" in body:
            return [line]
        cond = ExpressionTransformer().transform(m.group(1))
        # Strip a trailing semicolon on the body stmt.
        body = body.rstrip(";").strip()
        body = ExpressionTransformer().transform(body)
        return [f"{indent}if {cond}:", f"{indent}    {body}"]

    @staticmethod
    def _has_live_match(lines: list[str], case_indent: int) -> bool:
        """True if a live ``match`` exists at an indent <= *case_indent*."""
        for prev in reversed(lines):
            ps = prev.strip()
            if not ps or ps.startswith("#"):
                continue
            pi = len(prev) - len(prev.lstrip())
            if pi > case_indent:
                continue
            if ps.startswith("match "):
                return True
            if pi < case_indent and (ps.startswith(("def ", "class ", "if ", "for ", "return "))):
                return False
        return False

    def _transform_for_loop(self, line: str) -> str:
        """Convert Groovy for loops to Python."""
        # for (Type var : collection) -> for var in collection:
        m = re.match(
            r"^(\s*)for\s*\(\s*(?:\w+\s+)?(\w+)\s*(?:in|:)\s*(.+)\)\s*\{?\s*$",
            line,
        )
        if m:
            indent = m.group(1)
            var = m.group(2)
            collection = self.expr_transformer.transform(m.group(3))
            from tektonize.transformer.name_converter import convert_identifier

            return f"{indent}for {convert_identifier(var)} in {collection}:"

        # for (int i = 0; i < n; i++) -> for i in range(n):
        m = re.match(
            r"^(\s*)for\s*\(\s*(?:int\s+)?(\w+)\s*=\s*(\d+)\s*;\s*\w+\s*<\s*(\w+)\s*;\s*\w+\+\+\s*\)\s*\{?\s*$",
            line,
        )
        if m:
            indent = m.group(1)
            var = m.group(2)
            start = m.group(3)
            end = m.group(4)
            if start == "0":
                return f"{indent}for {var} in range({end}):"
            return f"{indent}for {var} in range({start}, {end}):"

        return line

    def _transform_try_catch(self, line: str) -> str:
        """Convert try/catch to try/except."""
        # try {
        m = re.match(r"^(\s*)try\s*\{?\s*$", line)
        if m:
            return f"{m.group(1)}try:"

        # } catch (Type e) {
        m = re.match(r"^(\s*)}\s*catch\s*\(\s*(\w+)\s+(\w+)\s*\)\s*\{?\s*$", line)
        if m:
            return f"{m.group(1)}except {m.group(2)} as {m.group(3)}:"

        # } finally {
        m = re.match(r"^(\s*)}\s*finally\s*\{?\s*$", line)
        if m:
            return f"{m.group(1)}finally:"

        return line

    def _transform_switch(self, line: str) -> str:
        """Leave switch/case for TODO absorb (brace-style ≠ indented match/case).

        Emitting live ``match``/``case`` from Groovy brace layout inserts empty
        ``pass`` bodies and triggers emit sanitizer cascades. Wave 29 absorbs
        the whole span as ``# TEKTONIZE-TODO: switch/case → match/case``.
        """
        # Remove break statements (not needed once absorbed / in match/case)
        if line.strip() == "break":
            return ""

        return line

    def _ensure_block_bodies(self, lines: list[str]) -> list[str]:
        """Insert ``pass`` under empty ``if``/``for``/``else``/etc. blocks."""
        if not lines:
            return lines

        block_enders = (
            "if ",
            "elif ",
            "else:",
            "for ",
            "while ",
            "try:",
            "except ",
            "finally:",
            "match ",
            "case ",
            "with ",
            "def ",
            "class ",
        )
        # Clause continuations are sibling clauses, not body content.
        # Even if they appear at a deeper raw indent (common in Groovy
        # brace-based code), the block body is still empty.
        # ``case`` continues ``match`` / prior ``case`` (Python 3.10+).
        _clause_prefixes = (
            "elif ",
            "else:",
            "except ",
            "except:",
            "finally:",
            "case ",
        )

        result: list[str] = []
        for i, line in enumerate(lines):
            result.append(line)
            stripped = line.strip()
            if not stripped.endswith(":") or stripped.startswith("#"):
                continue
            if not any(
                stripped == "else:"
                or stripped == "try:"
                or stripped == "finally:"
                or stripped.startswith(prefix)
                for prefix in block_enders
            ):
                continue

            indent = len(line) - len(line.lstrip())
            j = i + 1
            while j < len(lines):
                nxt = lines[j]
                if nxt.strip() and not nxt.strip().startswith("#"):
                    break
                j += 1
            else:
                result.append(" " * (indent + 4) + "pass")
                continue

            next_stripped = lines[j].strip()
            next_indent = len(lines[j]) - len(lines[j].lstrip())
            # Clause continuations (elif/else/except/finally) are siblings,
            # not block content — the body is empty regardless of raw indent.
            is_clause = any(next_stripped.startswith(cp) for cp in _clause_prefixes)
            if next_indent <= indent or is_clause:
                result.append(" " * (indent + 4) + "pass")

        return result

    def _clamp_indent_jumps(self, lines: list[str], step: int = 4) -> list[str]:
        """Clamp indent so levels only increase after a block opener (``...:``).

        When parent Groovy lines become ``# GROOVY:`` comments, child lines can keep
        deep indents and produce ``unexpected indent``. Never indent deeper than one
        level past the previous non-empty line, and never deepen without a ``:`` opener.

        Clause alignment:
        - ``except`` / ``finally`` → enclosing ``try``
        - ``elif`` / ``else`` → enclosing ``if``
        - ``case`` → one level under enclosing ``match`` (Python ``match``/``case``)

        ``except`` peeks the try stack (the matching ``finally`` pops it) so that
        ``try/except/finally`` chains all align to the same ``try`` level.
        """
        result: list[str] = []
        prev_level = 0
        prev_opened_block = False
        try_stack: list[int] = []
        if_stack: list[int] = []
        match_stack: list[int] = []
        for line in lines:
            stripped = line.strip()
            if not stripped:
                result.append("")
                continue

            is_comment = stripped.startswith("#")
            raw_spaces = len(line) - len(line.lstrip())
            raw_level = raw_spaces // step

            if not is_comment and stripped.startswith(("except ", "except:")):
                # Peek — finally will pop.
                level = try_stack[-1] if try_stack else max(0, prev_level - 1)
            elif not is_comment and stripped.startswith("finally:"):
                level = try_stack.pop() if try_stack else max(0, prev_level - 1)
            elif not is_comment and stripped.startswith("elif "):
                level = if_stack[-1] if if_stack else max(0, prev_level - 1)
            elif not is_comment and stripped.startswith("else:"):
                level = if_stack.pop() if if_stack else max(0, prev_level - 1)
            elif not is_comment and stripped.startswith("case "):
                level = (match_stack[-1] + 1) if match_stack else max(0, prev_level)
            elif prev_opened_block:
                # prev_level is already opener_level + 1; do not add another +1.
                level = min(raw_level, prev_level)
            else:
                level = min(raw_level, prev_level)
                # Leaving a match block: drop match frames we are no longer inside.
                while match_stack and level <= match_stack[-1]:
                    match_stack.pop()
                while if_stack and level < if_stack[-1]:
                    if_stack.pop()

            result.append((" " * (level * step)) + stripped)

            if is_comment:
                continue
            prev_opened_block = stripped.endswith(":")
            if prev_opened_block:
                if stripped.startswith("try:"):
                    try_stack.append(level)
                elif stripped.startswith("if "):
                    if_stack.append(level)
                elif stripped.startswith("match "):
                    match_stack.append(level)
                prev_level = level + 1
            else:
                prev_level = level

        return result

    def _comment_orphan_clauses(self, lines: list[str]) -> list[str]:
        """Comment ``else``/``elif``/``except``/``finally``/``case`` with no matching opener.

        Uses indent-level-aware stacks so that an ``else:`` at indent 28
        cannot steal a match from an ``if`` at indent 8.  When a clause is
        commented out, any immediately following ``pass`` at a deeper indent
        (inserted earlier by ``_ensure_block_bodies``) is also commented out.
        """
        result: list[str] = []
        if_levels: list[int] = []
        try_levels: list[int] = []
        match_levels: list[int] = []
        # Flag: when we just commented out a clause, remember its indent so we
        # can also comment out the orphaned ``pass`` that follows.
        commented_clause_indent: int | None = None

        for line in lines:
            stripped = line.strip()
            if not stripped:
                result.append(line)
                continue

            indent = len(line) - len(line.lstrip())
            prefix = " " * indent

            # If we just commented a clause, check for orphaned pass body.
            if commented_clause_indent is not None:
                if stripped == "pass" and indent > commented_clause_indent:
                    result.append(f"{prefix}# GROOVY: {stripped}")
                    commented_clause_indent = None
                    continue
                commented_clause_indent = None

            if stripped.startswith("#"):
                result.append(line)
                continue

            # Pop stacks of blocks we have exited (dedented past).
            while if_levels and if_levels[-1] > indent:
                if_levels.pop()
            while try_levels and try_levels[-1] > indent:
                try_levels.pop()
            while match_levels and match_levels[-1] >= indent:
                match_levels.pop()

            if stripped.startswith("if "):
                if_levels.append(indent)
                result.append(line)
                continue

            if stripped.startswith("elif "):
                if not if_levels or if_levels[-1] != indent:
                    result.append(f"{prefix}# GROOVY: {stripped}")
                    commented_clause_indent = indent
                    continue
                result.append(line)
                continue

            if stripped.startswith("else:"):
                if not if_levels or if_levels[-1] != indent:
                    result.append(f"{prefix}# GROOVY: {stripped}")
                    commented_clause_indent = indent
                    continue
                if_levels.pop()
                result.append(line)
                continue

            if stripped.startswith("try:"):
                try_levels.append(indent)
                result.append(line)
                continue

            if stripped.startswith(("except ", "except:")):
                if not try_levels or try_levels[-1] != indent:
                    result.append(f"{prefix}# GROOVY: {stripped}")
                    commented_clause_indent = indent
                    continue
                result.append(line)
                continue

            if stripped.startswith("finally:"):
                if not try_levels or try_levels[-1] != indent:
                    result.append(f"{prefix}# GROOVY: {stripped}")
                    commented_clause_indent = indent
                    continue
                try_levels.pop()
                result.append(line)
                continue

            if stripped.startswith("match ") and stripped.endswith(":"):
                match_levels.append(indent)
                result.append(line)
                continue

            if stripped.startswith("case "):
                expected_indent = match_levels[-1] + 4 if match_levels else None
                if expected_indent is None or indent != expected_indent:
                    result.append(f"{prefix}# GROOVY: {stripped}")
                    commented_clause_indent = indent
                    continue
                result.append(line)
                continue

            result.append(line)
        return result
