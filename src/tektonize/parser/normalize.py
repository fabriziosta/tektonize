"""Pre-parse rewrites for Groovy idioms that tree-sitter-groovy misparses.

This is intentionally **not** a staged pipeline step — it runs in-memory before
CST/typed-AST extraction (see ``docs/01_raw_ast.md``). Emitters undo sentinels.
"""

from __future__ import annotations

import re

# Sentinels — must stay valid Groovy identifiers / call shapes for the grammar.
EMPTY_MAP_SENTINEL = "__TEKTONIZE_EMPTY_MAP__"
EMPTY_LIST_SENTINEL = "__TEKTONIZE_EMPTY_LIST__"
EMPTY_STR_SENTINEL = "__TEKTONIZE_EMPTY_STR__"
ELVIS_SENTINEL = "__TEKTONIZE_ELVIS__"
SAFE_NAV_SENTINEL = "__TEKTONIZE_SAFE_NAV__"
SAFE_CALL_SENTINEL = "__TEKTONIZE_SAFE_CALL__"
AS_CAST_SENTINEL = "__TEKTONIZE_AS__"
METHOD_PTR_SENTINEL = "__TEKTONIZE_METHOD_PTR__"

_EMPTY_MAP_LITERAL = re.compile(r"\[:\]")


# Keywords that cannot be part of an elvis / safe-nav left operand when scanning left.
_LEFT_STOP_KEYWORDS = frozenset(
    {
        "return",
        "throw",
        "assert",
        "if",
        "else",
        "while",
        "for",
        "switch",
        "case",
        "default",
        "try",
        "catch",
        "finally",
        "break",
        "continue",
        "def",
        "class",
        "interface",
        "enum",
        "package",
        "import",
        "public",
        "private",
        "protected",
        "static",
        "final",
        "abstract",
        "void",
        "boolean",
        "byte",
        "char",
        "short",
        "int",
        "long",
        "float",
        "double",
    }
)


def normalize_groovy_for_treesitter(source: str) -> str:
    """Rewrite niche Groovy constructs so tree-sitter can build a usable CST."""
    result = _EMPTY_MAP_LITERAL.sub(EMPTY_MAP_SENTINEL, source)
    result = rewrite_empty_lists(result)
    result = rewrite_empty_strings(result)
    # Terminate empty-* / boolean defaults so the next field is not swallowed
    # (Groovy line-continuation treats ``x = SENTINEL\\nList y = …`` as one stmt).
    result = _terminate_field_default_lines(
        result,
        (
            EMPTY_MAP_SENTINEL,
            EMPTY_LIST_SENTINEL,
            EMPTY_STR_SENTINEL,
        ),
    )
    result = _terminate_boolean_field_defaults(result)
    result = _terminate_dotted_field_defaults(result)
    result = _terminate_bare_typed_fields(result)
    result = rewrite_backslash_line_continuations(result)
    result = rewrite_slashy_regex_literals(result)
    result = rewrite_slashy_string_literals(result)
    # Join continuation lines BEFORE operator rewrites so that multiline
    # ``?:``, ``? … :``, and ``.method()`` chains are on one line when the
    # operator-specific passes scan them.
    result = rewrite_multiline_dot_chains(result)
    result = rewrite_for_each_colon(result)
    result = rewrite_multiline_ternaries(result)
    # Safe-nav before elvis so ``a?.b ?: c`` becomes elvis(safe_nav(a,b), c).
    result = rewrite_safe_navigation(result)
    result = rewrite_elvis_operators(result)
    result = rewrite_as_casts(result)
    result = rewrite_method_pointers(result)
    result = rewrite_field_at_access(result)
    result = rewrite_assert_comma_messages(result)
    result = rewrite_enum_semicolons(result)
    result = rewrite_inline_return_blocks(result)
    result = rewrite_script_method_ternary_args(result)
    result = rewrite_untyped_method_formals(result)
    # Wrappers before nested-quote rewrite so ``"'${expr}'"`` becomes concat first.
    result = rewrite_quoted_gstring_wrappers(result)
    result = rewrite_gstring_interpolation_double_quotes(result)
    result = rewrite_closure_signatures_and_locals(result)
    result = rewrite_command_style_invocations(result)
    result = rewrite_closure_bodies(result)
    result = _terminate_bare_asserts(result)
    return result


# Next line closes a map/list arg — do not inject ``;`` (invalid inside ``[ … ]``).
_NOT_FIELD_CONTINUATION = re.compile(r"\s*[\])},]")

_BOOL_FIELD_DEFAULT = re.compile(
    rf"(?<![=!<>])=\s*(true|false)(\s*\n)(?!{_NOT_FIELD_CONTINUATION.pattern})"
)

# Enum / static constant field defaults: ``Type x = Type.CONST`` before next field.
_DOTTED_FIELD_DEFAULT = re.compile(
    rf"(?<![=!<>])=\s*([A-Z]\w*(?:\.[A-Z]\w*)+)(\s*\n)(?!{_NOT_FIELD_CONTINUATION.pattern})"
)

_STATEMENT_AFTER_CLOSURE_ARROW = re.compile(
    r"^(?:def\b|return\b|throw\b|if\b|while\b|for\b|switch\b|try\b|new\b|"
    r"(?:String|boolean|Boolean|int|Integer|long|Long|void|Map|List|Set|"
    r"double|float|char|byte|short)\b)",
    re.MULTILINE,
)

_ENUM_FIELD_LINE = re.compile(
    r"^(\s*)(?:(?:public|private|protected)\s+)?(?:static\s+)?(?:final\s+)?"
    r"(?:String|boolean|Boolean|int|long|double|float|char|byte|short)\s+(\w+)\s*$"
)


def _terminate_field_default_lines(source: str, literals: tuple[str, ...]) -> str:
    """Append ``;`` after field-default sentinels when the next line starts a new member."""
    result = source
    for literal in literals:
        result = re.sub(
            rf"({re.escape(literal)})(\s*\n)(?!{_NOT_FIELD_CONTINUATION.pattern})",
            r"\1;\2",
            result,
        )
    return result


def _terminate_boolean_field_defaults(source: str) -> str:
    """Append ``;`` after ``= true`` / ``= false`` field defaults (not ``==``)."""
    return _BOOL_FIELD_DEFAULT.sub(r"= \1;\2", source)


def _terminate_dotted_field_defaults(source: str) -> str:
    """Append ``;`` after ``= Type.CONST`` field defaults so the next field parses."""
    return _DOTTED_FIELD_DEFAULT.sub(r"= \1;\2", source)


# ``Map config`` / ``protected Object script`` without ``;`` — next member is swallowed.
_BARE_TYPED_FIELD = re.compile(
    r"^([ \t]*(?:(?:public|private|protected|static|final|volatile|transient)\s+)*)"
    r"((?:[A-Z]\w*\.)*[A-Z]\w*(?:<[^>\n]+>)?)\s+([A-Za-z_]\w*)\s*$"
)


def _terminate_bare_typed_fields(source: str) -> str:
    """Append ``;`` after typed fields that have no initializer.

    Skips constructor/method parameter lines (next significant token is ``)``).
    """
    lines = source.split("\n")
    out: list[str] = []
    for i, line in enumerate(lines):
        if _BARE_TYPED_FIELD.match(line) and not line.rstrip().endswith(";"):
            j = i + 1
            while j < len(lines) and not lines[j].strip():
                j += 1
            if j < len(lines):
                nxt = lines[j].lstrip()
                # Last formal before ``)`` must not gain a semicolon.
                if nxt.startswith(")") or nxt.startswith(","):
                    out.append(line)
                    continue
            out.append(line + ";")
        else:
            out.append(line)
    return "\n".join(out)


def rewrite_backslash_line_continuations(source: str) -> str:
    """Join Groovy lines ending with ``\\`` onto the next line (assert messages, etc.)."""
    lines = source.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        while line.rstrip().endswith("\\") and i + 1 < len(lines):
            line = line.rstrip()[:-1].rstrip() + " " + lines[i + 1].lstrip()
            i += 1
        out.append(line)
        i += 1
    return "\n".join(out)


_CLOSURE_SIMPLE_PARAM_TYPE = re.compile(
    r"(^|[,{]\s*)(?:def\s+)?(?:String|boolean|Boolean|int|Integer|long|Long|"
    r"void|Map|List|Set|double|float|char|byte|short|Object)\s+(\w+)"
)

_CLOSURE_QUALIFIED_PARAM_TYPE = re.compile(
    r"(^|[,{]\s*)(?:[A-Z]\w*(?:\s*\.\s*[A-Z]\w*)*)(?:<[^>]+>)?\s+(\w+)(?=\s*[,}])"
)

_CLOSURE_TYPED_LOCAL = re.compile(
    r"(^|\n)([ \t]+)"
    r"(?:def\s+)?(?:String|boolean|Boolean|int|Integer|long|Long|void|Map|List|Set|"
    r"double|float|char|byte|short|Object|[A-Z]\w*(?:<[^>]+>)?)"
    r"\s+(\w+)\s*="
)


def rewrite_inline_return_blocks(source: str) -> str:
    """``{ return }`` / ``{ continue }`` / ``{ break }`` need a semicolon for CST."""
    return re.sub(
        r"\{\s*(return|continue|break)\s*\}",
        r"{ \1; }",
        source,
    )


def _find_line_comment_start(text: str) -> int:
    """Index of ``//`` outside strings, or -1."""
    in_str: str | None = None
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if in_str:
            if c == "\\" and i + 1 < n:
                i += 2
                continue
            if c == in_str:
                in_str = None
            i += 1
            continue
        if c in ('"', "'"):
            in_str = c
            i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            return i
        i += 1
    return -1


def _terminate_bare_asserts(source: str) -> str:
    """Ensure ``assert`` statements end with ``;`` (comments / ``}`` / colon msgs)."""
    lines = source.split("\n")
    out: list[str] = []
    for line in lines:
        m = re.match(r"^([ \t]*assert\b)(.*)$", line)
        if not m:
            out.append(line)
            continue
        rest = m.group(2)
        cpos = _find_line_comment_start(rest)
        comment = rest[cpos:] if cpos >= 0 else ""
        code = rest[:cpos] if cpos >= 0 else rest
        code_r = code.rstrip()
        # Do not terminate when the assert message / expression continues.
        if (
            code_r
            and not code_r.endswith(";")
            and not code_r.endswith((":", "+", "\\", ",", "(", "[", "{", "||", "&&", "?", "."))
        ):
            line = m.group(1) + code_r + ";" + ((" " + comment.lstrip()) if comment else "")
        out.append(line)
    return "\n".join(out)


def rewrite_script_method_ternary_args(source: str) -> str:
    """``echo|script.echo result ? a : b`` → parenthesized after ternary join."""
    return re.sub(
        r"(\b(?:script\.\w+|echo|error|println))\s+(\S[^\n]*\?[^\n]+:[^\n]+)",
        r"\1(\2)",
        source,
    )


_METHOD_FORMAL_DECL = re.compile(
    r"(?m)^([ \t]*(?:(?:public|private|protected|static|final|synchronized|abstract)\s+)*"
    r"(?:def|void|[A-Z]\w*(?:<[^>\n]+>)?)\s+"
    r"[A-Za-z_]\w*\s*)\(([^;{]*)\)(\s*\{)"
)


def rewrite_untyped_method_formals(source: str) -> str:
    """Prefix untyped formals with ``def`` — tree-sitter treats bare names as types."""

    def repl(m: re.Match[str]) -> str:
        return m.group(1) + "(" + _add_def_to_untyped_formals(m.group(2)) + ")" + m.group(3)

    return _METHOD_FORMAL_DECL.sub(repl, source)


def _add_def_to_untyped_formals(params: str) -> str:
    if not params.strip():
        return params
    parts = _split_top_level_args(params)
    out: list[str] = []
    for part in parts:
        raw = part.strip()
        if not raw:
            continue
        if re.match(r"^(?:final\s+)?def\b", raw):
            out.append(raw)
            continue
        if re.match(r"^(?:final\s+)?(?:[A-Z]\w*\.)*[A-Z]\w*", raw):
            out.append(raw)
            continue
        m = re.match(r"^(final\s+)?([a-z_]\w*)(\s*=\s*.*)?$", raw)
        if m:
            out.append(f"{m.group(1) or ''}def {m.group(2)}{m.group(3) or ''}")
        else:
            out.append(raw)
    # Preserve original spacing style lightly: comma+space join.
    leading = params[: len(params) - len(params.lstrip())] if params.strip() else ""
    trailing = params[len(params.rstrip()) :] if params.strip() else ""
    return leading + ", ".join(out) + trailing


def rewrite_gstring_interpolation_double_quotes(source: str) -> str:
    """Inside ``\"…${…}…\"``, rewrite nested ``\"`` to ``'`` so the CST can close the GString."""
    out: list[str] = []
    i = 0
    n = len(source)
    while i < n:
        if source.startswith('"""', i) or source.startswith("'''", i):
            j = _skip_string_or_comment(source, i)
            out.append(source[i:j])
            i = j
            continue
        if source.startswith("//", i) or source.startswith("/*", i):
            j = _skip_string_or_comment(source, i)
            out.append(source[i:j])
            i = j
            continue
        if source[i] == "'":
            j = _skip_string_or_comment(source, i)
            out.append(source[i:j])
            i = j
            continue
        if source[i] == '"':
            chunk, end = _rewrite_one_double_quoted_gstring(source, i)
            out.append(chunk)
            i = end
            continue
        out.append(source[i])
        i += 1
    return "".join(out)


def _rewrite_one_double_quoted_gstring(source: str, start: int) -> tuple[str, int]:
    """Rewrite one double-quoted literal starting at *start*; return (text, end_index)."""
    assert source[start] == '"'
    i = start + 1
    n = len(source)
    parts: list[str] = ['"']
    while i < n:
        c = source[i]
        if c == "\\" and i + 1 < n:
            parts.append(source[i : i + 2])
            i += 2
            continue
        if c == '"':
            parts.append('"')
            return "".join(parts), i + 1
        if c == "$" and i + 1 < n and source[i + 1] == "{":
            close = _match_balanced(source, i + 1, "{", "}")
            if close < 0:
                parts.append(c)
                i += 1
                continue
            inner = source[i + 2 : close]
            inner = inner.replace('"', "'")
            parts.append("${" + inner + "}")
            i = close + 1
            continue
        parts.append(c)
        i += 1
    return "".join(parts), n


def rewrite_quoted_gstring_wrappers(source: str) -> str:
    """``\"'${expr}'\"`` → ``\"'\" + (expr) + \"'\"`` — avoids nested quotes in GStrings."""
    out: list[str] = []
    i = 0
    n = len(source)
    while i < n:
        if source.startswith("\"'${", i):
            # "'${
            open_brace = i + 3  # points at '{'
            close = _match_balanced(source, open_brace, "{", "}")
            if close >= 0 and source.startswith("}'\"", close):
                expr = source[open_brace + 1 : close]
                out.append('"\'" + (' + expr + ') + "\'"')
                i = close + 3
                continue
        if _at_string_or_comment(source, i):
            j = _skip_string_or_comment(source, i)
            # Avoid double-processing if we already handled above
            out.append(source[i:j])
            i = j
            continue
        out.append(source[i])
        i += 1
    return "".join(out)


def rewrite_closure_signatures_and_locals(source: str) -> str:
    """Drop typed closure params / locals that confuse tree-sitter-groovy."""
    out: list[str] = []
    i = 0
    n = len(source)
    while i < n:
        if _at_string_or_comment(source, i):
            j = _skip_string_or_comment(source, i)
            out.append(source[i:j])
            i = j
            continue
        if source[i] == "{":
            close = _match_balanced(source, i, "{", "}")
            if close < 0:
                out.append(source[i])
                i += 1
                continue
            inner = rewrite_closure_signatures_and_locals(source[i + 1 : close])
            if "->" in inner:
                inner = _rewrite_closure_param_types(inner)
                inner = _rewrite_closure_typed_locals(inner)
            out.append("{")
            out.append(inner)
            out.append("}")
            i = close + 1
            continue
        out.append(source[i])
        i += 1
    return "".join(out)


def _rewrite_closure_param_types(inner: str) -> str:
    arrow = _find_closure_arrow(inner)
    if arrow < 0:
        return inner
    params = inner[: arrow + 2]
    body = inner[arrow + 2 :]
    params = _CLOSURE_SIMPLE_PARAM_TYPE.sub(r"\1\2", params)
    # Qualified / custom types (e.g. PipelineJobBuilder, Map.Entry<…>).
    for _ in range(32):
        nxt = _CLOSURE_QUALIFIED_PARAM_TYPE.sub(r"\1\2", params)
        if nxt == params:
            break
        params = nxt
    return params + body


def _rewrite_closure_typed_locals(inner: str) -> str:
    arrow = _find_closure_arrow(inner)
    if arrow < 0:
        return inner
    params = inner[: arrow + 2]
    body = inner[arrow + 2 :]
    body = _CLOSURE_TYPED_LOCAL.sub(r"\1\2\3 =", body)
    return params + body


def _closure_body_line_count(body: str) -> int:
    return sum(1 for ln in body.splitlines() if ln.strip() and not ln.strip().startswith("//"))


def _closure_body_is_if_else_chain(body: str) -> bool:
    stripped = body.lstrip()
    if not stripped.startswith(("if ", "if(")):
        return False
    return bool(re.search(r"\}\s*else\s*\{", stripped))


def _closure_body_has_control_flow_statements(body: str) -> bool:
    stripped = body.lstrip()
    if stripped.startswith(
        (
            "if ",
            "if(",
            "for ",
            "for(",
            "while ",
            "while(",
            "switch ",
            "switch(",
            "try ",
            "try(",
            "return ",
            "return;",
        )
    ):
        return True
    depth = 0
    in_str: str | None = None
    i = 0
    n = len(body)
    while i < n:
        c = body[i]
        if in_str:
            if c == "\\" and i + 1 < n:
                i += 2
                continue
            if c == in_str:
                in_str = None
            i += 1
            continue
        if c in ('"', "'"):
            in_str = c
            i += 1
            continue
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth = max(0, depth - 1)
        elif depth == 0 and c == "\n":
            j = i + 1
            while j < n and body[j] in " \t":
                j += 1
            if j < n:
                rest = body[j:]
                if rest.startswith(
                    (
                        "if ",
                        "if(",
                        "for ",
                        "for(",
                        "while ",
                        "while(",
                        "switch ",
                        "switch(",
                        "try ",
                        "try(",
                        "return ",
                        "return;",
                    )
                ):
                    return True
        i += 1
    return False


def rewrite_command_style_invocations(source: str) -> str:
    """Rewrite Jenkins command-style calls to parenthesized / map-literal forms."""
    out: list[str] = []
    i = 0
    n = len(source)
    while i < n:
        if _at_string_or_comment(source, i):
            j = _skip_string_or_comment(source, i)
            out.append(source[i:j])
            i = j
            continue
        repl = _try_rewrite_command_invocation_at(source, i)
        if repl is not None:
            out.append(repl[0])
            i = repl[1]
            continue
        out.append(source[i])
        i += 1
    return "".join(out)


def _try_rewrite_command_invocation_at(source: str, i: int) -> tuple[str, int] | None:
    """If ``i`` starts a command-style call, return ``(replacement, end_index)``."""
    n = len(source)
    if not _is_command_invocation_start(source, i):
        return None
    callee_end = _scan_callee_name(source, i)
    if callee_end <= i:
        return None
    j = callee_end
    while j < n and source[j] in " \t":
        j += 1
    if j >= n:
        return None
    if source[j] == "(":
        return None
    if source[j] in ('"', "'"):
        lit_end = _skip_string_or_comment(source, j)
        return source[i:callee_end] + "(" + source[j:lit_end] + ")", lit_end
    if source[j] == "}" or source[j] == "]":
        return None
    m = re.match(r"[A-Za-z_]\w*\s*:", source[j:])
    if m:
        args_end = _scan_named_argument_list(source, j)
        if args_end < 0:
            return None
        args = source[j:args_end].strip()
        return source[i:callee_end] + "([" + args + "])", args_end
    # Positional single arg: ``echo lines.join("\\n")``, ``this.script.parallel stages``
    # Never treat ``{`` as an arg — that is a class/method/closure body.
    if source[j] in "\n\r;,{":
        return None
    # Ban operator starts (``build != null`` must not become ``build(!= null)``).
    if not re.match(r"""[A-Za-z_'"\d(\[+-]""", source[j]):
        return None
    if source.startswith(("!=", "!~", "==", "<=", ">=", "&&", "||"), j):
        return None
    step = _command_callee_step_name(source[i:callee_end])
    if step not in _POSITIONAL_COMMAND_STEPS:
        return None
    args_end = _scan_positional_command_arg(source, j)
    if args_end <= j:
        return None
    args = source[j:args_end].strip()
    if not args:
        return None
    return source[i:callee_end] + "(" + args + ")", args_end


def _command_callee_step_name(callee: str) -> str:
    """Last identifier in ``script.echo`` / ``this.script.parallel``."""
    parts = re.findall(r"[A-Za-z_]\w*", callee)
    return parts[-1] if parts else ""


def _scan_positional_command_arg(source: str, start: int) -> int:
    """Scan one positional command-style argument to end of expression."""
    _start, end = _expr_bounds_right(source, start)
    return end


_COMMAND_INVOKE_KEYWORDS = frozenset(
    {
        "if",
        "else",
        "for",
        "while",
        "switch",
        "case",
        "return",
        "throw",
        "try",
        "catch",
        "finally",
        "break",
        "continue",
        "new",
        "assert",
        "import",
        "package",
        "class",
        "interface",
        "enum",
        "def",
        "static",
        "public",
        "private",
        "protected",
        "final",
        "abstract",
        "synchronized",
        "volatile",
        "transient",
        "extends",
        "implements",
        "throws",
    }
)

# Positional command-style (no ``name:``) only for known Jenkins / Groovy steps.
# Arbitrary identifiers must not be wrapped (``Map config``, ``String x = …``).
_POSITIONAL_COMMAND_STEPS = frozenset(
    {
        "echo",
        "error",
        "println",
        "print",
        "sh",
        "sleep",
        "timeout",
        "wait",
        "retry",
        "parallel",
        "stage",
        "input",
        "pwd",
        "readFile",
        "writeFile",
        "dir",
        "deleteDir",
        "tool",
        "isUnix",
        "fileExists",
        "libraryResource",
        "catchError",
        "warnError",
        "unstable",
        # ``build`` is omitted: common local variable (``if (build != null)``).
        # Named-arg form ``build job: …`` still rewrites via the map-literal path.
        "archiveArtifacts",
        "junit",
        "stash",
        "unstash",
        "checkout",
        "git",
        "bat",
        "powershell",
        "node",
        "ws",
        "withEnv",
        "withCredentials",
        "lock",
        "milestone",
    }
)


def _is_command_invocation_start(source: str, i: int) -> bool:
    if i > 0 and _is_ident_char(source[i - 1]):
        return False
    # Only start at the head of a callee chain (``script.echo``), never mid-dot.
    if i > 0 and source[i - 1] == ".":
        return False
    if not (source[i].isalpha() or source[i] == "_"):
        return False
    if i > 0 and source[i - 1] not in " \t\n\r;({},":
        return False
    m = re.match(r"[A-Za-z_]\w*", source[i:])
    return not (m and m.group(0) in _COMMAND_INVOKE_KEYWORDS)


def _scan_callee_name(source: str, i: int) -> int:
    """Return index after optional ``receiver.`` chain and method name."""
    j = i
    n = len(source)
    while j < n:
        m = re.match(r"[A-Za-z_]\w*", source[j:])
        if not m:
            break
        j += m.end()
        k = j
        while k < n and source[k] in " \t":
            k += 1
        if k < n and source[k] == ".":
            j = k + 1
            while j < n and source[j] in " \t":
                j += 1
            continue
        break
    return j


def _scan_named_argument_list(source: str, start: int) -> int:
    """Scan ``name: value, …`` until the command-style arg list ends."""
    i = start
    n = len(source)
    depth = 0
    in_str: str | None = None
    saw_named = False
    while i < n:
        c = source[i]
        if in_str:
            if c == "\\" and i + 1 < n:
                i += 2
                continue
            if c == in_str:
                in_str = None
            i += 1
            continue
        if c in ('"', "'"):
            in_str = c
            i += 1
            continue
        if c in "([{":
            depth += 1
            i += 1
            continue
        if c in ")]}":
            if depth == 0:
                break
            depth -= 1
            i += 1
            continue
        if depth == 0 and c == "\n":
            k = i + 1
            while k < n and source[k] in " \t":
                k += 1
            if k >= n or source[k] == "\n":
                break
            if not re.match(r"[A-Za-z_]\w*\s*:", source[k:]):
                break
            i = k
            continue
        if depth == 0 and c == ",":
            comma = i
            i += 1
            while i < n and source[i] in " \t\r\n":
                i += 1
            if i >= n or not re.match(r"[A-Za-z_]\w*\s*:", source[i:]):
                return comma if saw_named else -1
            continue
        if depth == 0 and re.match(r"[A-Za-z_]\w*\s*:", source[i:]):
            saw_named = True
            m = re.match(r"[A-Za-z_]\w*\s*:", source[i:])
            i += m.end() if m else 1
            continue
        if depth == 0 and saw_named and c in ";":
            break
        i += 1
    return i if saw_named else -1


def rewrite_closure_bodies(source: str) -> str:
    """Wrap closure bodies in ``{ … }`` when tree-sitter misparses statement bodies."""
    out: list[str] = []
    i = 0
    n = len(source)
    while i < n:
        if _at_string_or_comment(source, i):
            j = _skip_string_or_comment(source, i)
            out.append(source[i:j])
            i = j
            continue
        if source[i] == "{":
            close = _match_balanced(source, i, "{", "}")
            if close < 0:
                out.append(source[i])
                i += 1
                continue
            inner = rewrite_closure_bodies(source[i + 1 : close])
            if "->" in inner:
                inner = _maybe_wrap_closure_inner(inner)
            out.append("{")
            out.append(inner)
            out.append("}")
            i = close + 1
            continue
        out.append(source[i])
        i += 1
    return "".join(out)


def _maybe_wrap_closure_inner(inner: str) -> str:
    arrow = _find_closure_arrow(inner)
    if arrow < 0:
        return inner
    params = inner[: arrow + 2]
    body = inner[arrow + 2 :]
    stripped = body.strip()
    if not stripped or stripped.startswith("{") or _is_single_expression_closure_body(stripped):
        return inner
    if _closure_body_is_if_else_chain(stripped):
        lead = body[: len(body) - len(body.lstrip("\r\n\t "))]
        trail = body[len(body.rstrip("\r\n\t ")) :]
        return f"{params}{lead}{{{stripped}}}{trail}"
    if _closure_body_has_control_flow_statements(stripped):
        return inner
    if _closure_body_line_count(stripped) > 1:
        return inner
    lead = body[: len(body) - len(body.lstrip("\r\n\t "))]
    trail = body[len(body.rstrip("\r\n\t ")) :]
    core = body.strip()
    return f"{params}{lead}{{{core}}}{trail}"


def _find_closure_arrow(inner: str) -> int:
    depth = 0
    in_str: str | None = None
    i = 0
    while i < len(inner) - 1:
        c = inner[i]
        if in_str:
            if c == "\\" and i + 1 < len(inner):
                i += 2
                continue
            if c == in_str:
                in_str = None
            i += 1
            continue
        if c in ('"', "'"):
            in_str = c
            i += 1
            continue
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif depth == 0 and inner[i : i + 2] == "->":
            return i
        i += 1
    return -1


def _is_single_expression_closure_body(body: str) -> bool:
    if "\n" in body:
        return False
    if _STATEMENT_AFTER_CLOSURE_ARROW.match(body):
        return False
    if re.match(r"[A-Za-z_]\w*\s*\(", body):
        return False
    if re.match(r"[A-Za-z_]\w*\s*=", body):
        return False
    # Groovy command-style calls: ``println x`` (no parentheses).
    return not re.match(r"[A-Za-z_]\w*\s+[A-Za-z_]", body)


def rewrite_empty_lists(source: str) -> str:
    """Rewrite empty list ``[]`` — not ``Type[]`` / ``List<T>[]`` array types."""
    out: list[str] = []
    i = 0
    n = len(source)
    while i < n:
        if _at_string_or_comment(source, i):
            j = _skip_string_or_comment(source, i)
            out.append(source[i:j])
            i = j
            continue
        if source.startswith("[]", i):
            prev = source[i - 1] if i > 0 else ""
            # Array types: ``String[]``, ``List<String>[]``
            if prev.isalnum() or prev in "_]>":
                out.append("[]")
            else:
                out.append(EMPTY_LIST_SENTINEL)
            i += 2
            continue
        out.append(source[i])
        i += 1
    return "".join(out)


def rewrite_multiline_ternaries(source: str) -> str:
    """Join multiline ternary / elvis onto one line — tree-sitter drops the condition.

    Handles:
    1. ``cond\\n ? a\\n : b``  (three-line ternary)
    2. ``cond\\n ? a : b``    (two-line ternary — ``?`` and ``:`` on same line)
    3. ``cond ?\\n a\\n : b``  (``?`` at end of condition line)
    4. ``a\\n ?: b`` / ``a ?:\\n b`` (multiline elvis)
    """
    # 3-line ternary: cond\n ? a\n : b
    pat3 = re.compile(r"(?m)([^\n?]+?)\n[ \t]*\?([^\n]+)\n[ \t]*:([^\n]+)")
    # 2-line ternary: cond\n ? a : b  (? not followed by : or . or word)
    pat2 = re.compile(r"(?m)([^\n]+?)\n[ \t]*\?(?![:.\w])([^\n]+)")
    # Multiline elvis: ?: at START of continuation line
    pat_elvis_start = re.compile(r"(?m)([^\n]+?)\n[ \t]*\?:([^\n]+)")
    # Multiline elvis: ?: at END of preceding line, right operand on next
    pat_elvis_end = re.compile(r"(?m)([^\n]+\?:)\n[ \t]+([^\n]+)")
    result = _join_question_at_eol_ternaries(source)
    prev = None
    while prev != result:
        prev = result
        result = pat3.sub(r"\1 ?\2 :\3", result)
        result = pat_elvis_start.sub(r"\1 ?:\2", result)
        result = pat_elvis_end.sub(r"\1 \2", result)
        result = pat2.sub(r"\1 ?\2", result)
    return result


def _join_question_at_eol_ternaries(source: str) -> str:
    """Join ``cond ?\\n true :\\n false`` without matching ``:`` inside strings."""
    lines = source.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        cpos = _find_line_comment_start(line)
        code = line[:cpos] if cpos >= 0 else line
        code_r = code.rstrip()
        if code_r.endswith("?") and not code_r.endswith("?:") and i + 1 < len(lines):
            j = i + 1
            while j < len(lines) and not lines[j].strip():
                j += 1
            if j < len(lines):
                true_line = lines[j]
                tc = _find_line_comment_start(true_line)
                true_code = (true_line[:tc] if tc >= 0 else true_line).rstrip()
                if true_code.endswith(":"):
                    true_expr = true_code[:-1].rstrip()
                    k = j + 1
                    while k < len(lines) and not lines[k].strip():
                        k += 1
                    if k < len(lines):
                        false_line = lines[k]
                        fc = _find_line_comment_start(false_line)
                        false_expr = (false_line[:fc] if fc >= 0 else false_line).strip()
                        out.append(f"{code_r} {true_expr} : {false_expr}")
                        i = k + 1
                        continue
                else:
                    # true\n : false
                    k = j + 1
                    while k < len(lines) and not lines[k].strip():
                        k += 1
                    if k < len(lines):
                        false_line = lines[k].lstrip()
                        if false_line.startswith(":"):
                            false_expr = false_line[1:].strip()
                            out.append(f"{code_r} {true_code} : {false_expr}")
                            i = k + 1
                            continue
        out.append(line)
        i += 1
    return "\n".join(out)


def rewrite_multiline_dot_chains(source: str) -> str:
    """Join ``expr\\n    .method(…)`` continuation lines onto the preceding line.

    tree-sitter-groovy treats a leading ``.`` on a new line as a standalone
    ERROR rather than a chained method invocation.  Only joins when the
    preceding line ends with an expression token (identifier, ``)``, ``]``)
    and the continuation has indentation.
    """
    # Preceding line must end with something chainable; continuation must be indented.
    pattern = re.compile(r"(?m)([\w)\]>])([ \t]*)(?://[^\n]*)?\n[ \t]+\.([A-Za-z_]\w*)")
    result = source
    prev = None
    while prev != result:
        prev = result
        result = pattern.sub(r"\1.\3", result)
    return result


def rewrite_for_each_colon(source: str) -> str:
    """Rewrite ``for (Type var : iterable)`` to ``for (var in iterable)``.

    tree-sitter-groovy does not recognise the Java enhanced-for-each syntax
    (``for (T x : coll)``) — it uses Groovy's ``for (x in coll)`` form.
    Handles ``def``, simple types, and qualified generics (``Map.Entry<K, V>``).
    """
    return re.sub(
        r"\bfor\s*\(\s*"
        r"(?:def|final\s+def|(?:[A-Z]\w*\.)*[A-Z]\w*(?:<[^>]+>)?)"
        r"\s+(\w+)\s*:\s*",
        r"for (\1 in ",
        source,
    )


def rewrite_empty_strings(source: str) -> str:
    """Rewrite empty ``''`` / ``\"\"`` to a sentinel — both confuse tree-sitter."""
    out: list[str] = []
    i = 0
    n = len(source)
    while i < n:
        if source.startswith('"""', i) or source.startswith("'''", i):
            j = _skip_string_or_comment(source, i)
            out.append(source[i:j])
            i = j
            continue
        if source.startswith("''", i):
            out.append(EMPTY_STR_SENTINEL)
            i += 2
            continue
        if source.startswith('""', i):
            out.append(EMPTY_STR_SENTINEL)
            i += 2
            continue
        if _at_string_or_comment(source, i):
            j = _skip_string_or_comment(source, i)
            out.append(source[i:j])
            i = j
            continue
        out.append(source[i])
        i += 1
    return "".join(out)


def _consume_slashy_pattern(source: str, slash_index: int) -> tuple[str, int] | None:
    """Read ``/pat/`` starting at ``slash_index``; return (escaped pattern, end) or None."""
    n = len(source)
    k = slash_index + 1
    body: list[str] = []
    while k < n:
        if source[k] == "\\" and k + 1 < n:
            body.append(source[k : k + 2])
            k += 2
            continue
        if source[k] == "/":
            pattern = "".join(body).replace("\\", "\\\\").replace('"', '\\"')
            return pattern, k + 1
        if source[k] == "\n":
            return None
        body.append(source[k])
        k += 1
    return None


def rewrite_slashy_string_literals(source: str) -> str:
    """Rewrite slashy ``/pat/`` in args, assignments, ternary arms, and ``~/pat/``."""
    out: list[str] = []
    i = 0
    n = len(source)
    while i < n:
        if _at_string_or_comment(source, i):
            j = _skip_string_or_comment(source, i)
            out.append(source[i:j])
            i = j
            continue
        # Groovy pattern ``~/pat/`` (e.g. ``case ~/…/:``).
        if source[i] == "~" and i + 1 < n:
            j = i + 1
            while j < n and source[j] in " \t":
                j += 1
            if j < n and source[j] == "/":
                consumed = _consume_slashy_pattern(source, j)
                if consumed is not None:
                    pattern, end = consumed
                    out.append(f'~"{pattern}"')
                    i = end
                else:
                    out.append(source[i])
                    i += 1
                continue
        if source[i] == "/" and i > 0:
            p = i - 1
            while p >= 0 and source[p] in " \t":
                p -= 1
            # Args ``(/x/,)``, assignments ``= /x/``, ternary ``: /x/``.
            if p >= 0 and source[p] in "(,=:":
                consumed = _consume_slashy_pattern(source, i)
                if consumed is not None:
                    pattern, end = consumed
                    out.append(f'"{pattern}"')
                    i = end
                else:
                    out.append(source[i])
                    i += 1
                continue
        out.append(source[i])
        i += 1
    return "".join(out)


def rewrite_field_at_access(source: str) -> str:
    """Rewrite ``obj.@field`` / ``this.@field`` to plain field access."""
    # ``this.@ns`` → ``this.ns``; ``foo.@bar`` → ``foo.bar``
    return re.sub(r"\b([A-Za-z_]\w*)\s*\.\s*@([A-Za-z_]\w*)", r"\1.\2", source)


def rewrite_assert_comma_messages(source: str) -> str:
    """Rewrite ``assert expr, "msg"`` to Groovy colon form ``assert expr : "msg"``.

    The comma form is widely misparsed; colon-message form is accepted.
    """
    out: list[str] = []
    i = 0
    n = len(source)
    while i < n:
        if _at_string_or_comment(source, i):
            j = _skip_string_or_comment(source, i)
            out.append(source[i:j])
            i = j
            continue
        if (
            source.startswith("assert", i)
            and (i == 0 or not _is_ident_char(source[i - 1]))
            and (i + 6 >= n or not _is_ident_char(source[i + 6]))
        ):
            j = i + 6
            while j < n and source[j] in " \t":
                j += 1
            # Scan expression until top-level comma before a string, or newline/semicolon.
            depth = 0
            in_str: str | None = None
            k = j
            comma_at = -1
            while k < n:
                c = source[k]
                if in_str:
                    if c == "\\" and k + 1 < n:
                        k += 2
                        continue
                    if c == in_str:
                        in_str = None
                    k += 1
                    continue
                if c in ('"', "'"):
                    in_str = c
                    k += 1
                    continue
                if c in "([{":
                    depth += 1
                elif c in ")]}":
                    depth -= 1
                elif depth == 0 and c in "\n;":
                    break
                elif depth == 0 and c == ",":
                    # Look ahead for a string message
                    m = k + 1
                    while m < n and source[m] in " \t":
                        m += 1
                    if m < n and source[m] in ('"', "'"):
                        comma_at = k
                    break
                k += 1
            if comma_at >= 0:
                out.append(source[i:comma_at])
                out.append(" :")
                i = comma_at + 1
                continue
            out.append(source[i:k])
            i = k
            continue
        out.append(source[i])
        i += 1
    return "".join(out)


_ENUM_HEADER = re.compile(r"\benum\b\s+[A-Za-z_]\w*")
_ENUM_CONST_LINE = re.compile(r"^(\s*)([A-Z][A-Z0-9_]*)\s*(?:\(([^)]*)\))?\s*(,)?\s*(;?\s*)$")
_ENUM_MEMBER_START = re.compile(
    r"^\s*(?:(?:public|private|protected|static|final|def|void|@)|"
    r"[A-Za-z_]\w*(?:\s*<[^>]+>)?\s+[a-z_])"
)


def rewrite_enum_semicolons(source: str) -> str:
    """Insert ``;`` after the last enum constant when fields/methods follow.

    Groovy/Java require a semicolon between the constant list and member
    declarations; tree-sitter reports a missing node without it.
    """
    out: list[str] = []
    i = 0
    n = len(source)
    while i < n:
        if _at_string_or_comment(source, i):
            j = _skip_string_or_comment(source, i)
            out.append(source[i:j])
            i = j
            continue
        m = _ENUM_HEADER.match(source, i)
        if not m:
            out.append(source[i])
            i += 1
            continue
        out.append(source[i : m.end()])
        i = m.end()
        while i < n and source[i] in " \t\n":
            out.append(source[i])
            i += 1
        if i >= n or source[i] != "{":
            continue
        body_end = _match_balanced(source, i, "{", "}")
        if body_end < 0:
            out.append(source[i])
            i += 1
            continue
        body = source[i + 1 : body_end]
        out.append("{")
        out.append(_insert_enum_semicolon(body))
        out.append("}")
        i = body_end + 1
    return "".join(out)


def _insert_enum_semicolon(body: str) -> str:
    lines = body.split("\n")
    last_const_idx: int | None = None
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("*") or stripped.startswith("/*"):
            continue
        if stripped.startswith("//"):
            continue
        if _ENUM_CONST_LINE.match(line.rstrip("\r")):
            last_const_idx = idx
            continue
        if last_const_idx is not None and _ENUM_MEMBER_START.match(line):
            const_line = lines[last_const_idx]
            m = _ENUM_CONST_LINE.match(const_line.rstrip("\r"))
            if m and not (m.group(5) or "").strip().startswith(";"):
                # Drop trailing comma if present; add semicolon.
                indent, name = m.group(1), m.group(2)
                args = m.group(3)
                args_part = f"({args})" if args is not None else ""
                lines[last_const_idx] = f"{indent}{name}{args_part};"
            break
        # Another enum constant that did not match _ENUM_CONST_LINE exactly.
        if (
            last_const_idx is not None
            and stripped
            and not stripped.startswith("@")
            and re.match(r"^[A-Z][A-Z0-9_]*\b", stripped)
        ):
            last_const_idx = idx
    for idx, line in enumerate(lines):
        if _ENUM_FIELD_LINE.match(line.rstrip("\r")) and not line.rstrip().endswith(";"):
            lines[idx] = line.rstrip("\r") + ";"
    return "\n".join(lines)


def rewrite_slashy_regex_literals(source: str) -> str:
    """Rewrite ``==~ /pat/`` / ``=~ /pat/`` to use quoted patterns."""

    out: list[str] = []
    i = 0
    n = len(source)
    while i < n:
        if _at_string_or_comment(source, i):
            j = _skip_string_or_comment(source, i)
            out.append(source[i:j])
            i = j
            continue
        op: str | None = None
        if source.startswith("==~", i):
            op = "==~"
        elif source.startswith("=~", i) and (i == 0 or source[i - 1] != "="):
            op = "=~"
        if op is not None:
            j = i + len(op)
            while j < n and source[j] in " \t":
                j += 1
            if j < n and source[j] == "/":
                k = j + 1
                body: list[str] = []
                while k < n:
                    if source[k] == "\\" and k + 1 < n:
                        body.append(source[k : k + 2])
                        k += 2
                        continue
                    if source[k] == "/":
                        pattern = "".join(body).replace("\\", "\\\\").replace('"', '\\"')
                        out.append(f'{op} "{pattern}"')
                        i = k + 1
                        break
                    body.append(source[k])
                    k += 1
                else:
                    out.append(source[i])
                    i += 1
                continue
        out.append(source[i])
        i += 1
    return "".join(out)


def rewrite_elvis_operators(source: str) -> str:
    """Rewrite ``left ?: right`` to ``__TEKTONIZE_ELVIS__(left, right)``."""
    result = source
    # One pass rewrites one ``?:``; real files can have hundreds.
    # Iterate until stable.
    for _ in range(512):
        nxt = _rewrite_one_elvis(result)
        if nxt == result:
            break
        result = nxt
    return result


def _rewrite_one_elvis(source: str) -> str:
    i = 0
    n = len(source)
    while i < n - 1:
        if _at_string_or_comment(source, i):
            i = _skip_string_or_comment(source, i)
            continue
        if source[i] == "?" and source[i + 1] == ":":
            left_start, left_end = _tighten(_expr_bounds_left(source, i), source)
            right_start, right_end = _tighten(_expr_bounds_right(source, i + 2), source)
            left = source[left_start:left_end]
            right = source[right_start:right_end]
            if left and right:
                replacement = f"{ELVIS_SENTINEL}({left}, {right})"
                return source[:left_start] + replacement + source[right_end:]
        i += 1
    return source


def rewrite_safe_navigation(source: str) -> str:
    """Rewrite ``obj?.prop`` / ``obj?.method(...)`` to sentinel calls."""
    result = source
    for _ in range(64):
        nxt = _rewrite_one_safe_nav(result)
        if nxt == result:
            break
        result = nxt
    return result


def _rewrite_one_safe_nav(source: str) -> str:
    i = 0
    n = len(source)
    while i < n - 1:
        if _at_string_or_comment(source, i):
            i = _skip_string_or_comment(source, i)
            continue
        if source[i] == "?" and source[i + 1] == ".":
            left_start, left_end = _tighten(_expr_bounds_left(source, i), source)
            j = i + 2
            while j < n and source[j] in " \t":
                j += 1
            left = source[left_start:left_end]
            if not left:
                i += 1
                continue
            # Dynamic property: ``obj?."${key}"`` / ``obj?.'name'``.
            if j < n and source[j] in "'\"":
                quote = source[j]
                k = j + 1
                while k < n:
                    if source[k] == "\\" and k + 1 < n:
                        k += 2
                        continue
                    if source[k] == quote:
                        k += 1
                        break
                    k += 1
                else:
                    i += 1
                    continue
                prop = source[j:k]
                replacement = f"{SAFE_NAV_SENTINEL}({left}, {prop})"
                return source[:left_start] + replacement + source[k:]
            m = re.match(r"[A-Za-z_]\w*", source[j:])
            if not m:
                i += 1
                continue
            name = m.group(0)
            k = j + len(name)
            if k < n and source[k] == "(":
                args_end = _match_balanced(source, k, "(", ")")
                if args_end < 0:
                    i += 1
                    continue
                args = source[k + 1 : args_end]
                replacement = (
                    f'{SAFE_CALL_SENTINEL}({left}, "{name}", {args})'
                    if args.strip()
                    else f'{SAFE_CALL_SENTINEL}({left}, "{name}")'
                )
                return source[:left_start] + replacement + source[args_end + 1 :]
            replacement = f'{SAFE_NAV_SENTINEL}({left}, "{name}")'
            return source[:left_start] + replacement + source[k:]
        i += 1
    return source


def rewrite_as_casts(source: str) -> str:
    """Rewrite ``expr as Type`` / ``expr as Type<...>`` to a sentinel call."""
    result = source
    for _ in range(64):
        nxt = _rewrite_one_as_cast(result)
        if nxt == result:
            break
        result = nxt
    return result


def _rewrite_one_as_cast(source: str) -> str:
    i = 0
    n = len(source)
    while i < n:
        if _at_string_or_comment(source, i):
            i = _skip_string_or_comment(source, i)
            continue
        if (
            source.startswith("as", i)
            and (i == 0 or not _is_ident_char(source[i - 1]))
            and (i + 2 >= n or not _is_ident_char(source[i + 2]))
        ):
            j = i + 2
            while j < n and source[j] in " \t":
                j += 1
            type_start = j
            if j >= n or not (source[j].isalpha() or source[j] == "_"):
                i += 1
                continue
            while j < n and _is_ident_char(source[j]):
                j += 1
            if j < n and source[j] == "<":
                close = _match_balanced(source, j, "<", ">")
                if close < 0:
                    i += 1
                    continue
                j = close + 1
            type_str = source[type_start:j].strip()
            if not type_str:
                i += 1
                continue
            left_start, left_end = _tighten(_expr_bounds_left(source, i), source)
            left = source[left_start:left_end]
            if not left:
                i += 1
                continue
            # ``import pkg.Type as Alias`` — left is only the type path; check the line.
            line_start = source.rfind("\n", 0, i) + 1
            if re.match(r"\s*import\b", source[line_start:i]):
                i += 1
                continue
            replacement = f'{AS_CAST_SENTINEL}({left}, "{type_str}")'
            return source[:left_start] + replacement + source[j:]
        i += 1
    return source


def rewrite_method_pointers(source: str) -> str:
    """Rewrite ``obj.&method`` to ``__TEKTONIZE_METHOD_PTR__(obj, "method")``."""
    result = source
    for _ in range(64):
        nxt = _rewrite_one_method_pointer(result)
        if nxt == result:
            break
        result = nxt
    return result


def _rewrite_one_method_pointer(source: str) -> str:
    i = 0
    n = len(source)
    while i < n - 1:
        if _at_string_or_comment(source, i):
            i = _skip_string_or_comment(source, i)
            continue
        if source[i] == "." and source[i + 1] == "&":
            left_start, left_end = _tighten(_expr_bounds_left(source, i), source)
            j = i + 2
            while j < n and source[j] in " \t":
                j += 1
            m = re.match(r"[A-Za-z_]\w*", source[j:])
            if not m:
                i += 1
                continue
            name = m.group(0)
            left = source[left_start:left_end]
            if not left:
                i += 1
                continue
            replacement = f'{METHOD_PTR_SENTINEL}({left}, "{name}")'
            return source[:left_start] + replacement + source[j + len(name) :]
        i += 1
    return source


def expand_normalize_sentinels(text: str) -> str:
    """Turn normalize sentinels back into Python-oriented expressions for emit."""
    result = text.replace(EMPTY_MAP_SENTINEL, "{}")
    result = result.replace(EMPTY_LIST_SENTINEL, "[]")
    result = result.replace(EMPTY_STR_SENTINEL, '""')
    result = _expand_calls(result, ELVIS_SENTINEL, _expand_elvis_args)
    result = _expand_calls(result, SAFE_NAV_SENTINEL, _expand_safe_nav_args)
    result = _expand_calls(result, SAFE_CALL_SENTINEL, _expand_safe_call_args)
    result = _expand_calls(result, AS_CAST_SENTINEL, _expand_as_cast_args)
    result = _expand_calls(result, METHOD_PTR_SENTINEL, _expand_method_ptr_args)
    return result


def _expand_method_ptr_args(args: list[str]) -> str:
    if len(args) != 2:
        return f"{METHOD_PTR_SENTINEL}({', '.join(args)})"
    return f"getattr({args[0]}, {args[1]})"


def _expand_as_cast_args(args: list[str]) -> str:
    # Python has no Groovy ``as`` — keep the value, drop the cast.
    if len(args) < 1:
        return f"{AS_CAST_SENTINEL}({', '.join(args)})"
    return args[0]


def _expand_elvis_args(args: list[str]) -> str:
    if len(args) != 2:
        return f"{ELVIS_SENTINEL}({', '.join(args)})"
    return f"({args[0]} or {args[1]})"


def _expand_safe_nav_args(args: list[str]) -> str:
    if len(args) != 2:
        return f"{SAFE_NAV_SENTINEL}({', '.join(args)})"
    return f"getattr({args[0]}, {args[1]}, None)"


def _expand_safe_call_args(args: list[str]) -> str:
    if len(args) < 2:
        return f"{SAFE_CALL_SENTINEL}({', '.join(args)})"
    obj, name = args[0], args[1]
    rest = ", ".join(args[2:])
    # name is already a quoted string from normalize
    if rest:
        return f"((lambda _o: getattr(_o, {name})({rest}) if _o is not None else None)({obj}))"
    return f"((lambda _o: getattr(_o, {name})() if _o is not None else None)({obj}))"


def _expand_calls(text: str, name: str, expander) -> str:  # type: ignore[no-untyped-def]
    result = text
    needle = name + "("
    for _ in range(256):
        idx = result.find(needle)
        if idx < 0:
            break
        open_paren = idx + len(name)
        close = _match_balanced(result, open_paren, "(", ")")
        if close < 0:
            break
        inner = result[open_paren + 1 : close]
        args = _split_top_level_args(inner)
        replacement = expander(args)
        result = result[:idx] + replacement + result[close + 1 :]
    return result


def _split_top_level_args(inner: str) -> list[str]:
    args: list[str] = []
    depth = 0
    in_str: str | None = None
    start = 0
    i = 0
    while i < len(inner):
        c = inner[i]
        if in_str:
            if c == "\\" and i + 1 < len(inner):
                i += 2
                continue
            if c == in_str:
                in_str = None
            i += 1
            continue
        if c in ('"', "'"):
            in_str = c
            i += 1
            continue
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif c == "," and depth == 0:
            args.append(inner[start:i].strip())
            start = i + 1
        i += 1
    tail = inner[start:].strip()
    if tail:
        args.append(tail)
    return args


def _tighten(bounds: tuple[int, int], source: str) -> tuple[int, int]:
    """Drop leading/trailing whitespace from an expression span."""
    start, end = bounds
    while start < end and source[start] in " \t":
        start += 1
    while end > start and source[end - 1] in " \t":
        end -= 1
    return start, end


def _expr_bounds_left(source: str, op_index: int) -> tuple[int, int]:
    """Return ``[start, op_index)`` covering the expression left of an operator."""
    end = op_index
    i = op_index - 1
    while i >= 0 and source[i] in " \t":
        i -= 1
    if i < 0:
        return op_index, op_index
    depth = 0
    in_str: str | None = None
    j = i
    while j >= 0:
        c = source[j]
        if in_str:
            if c == in_str and not _escaped_at(source, j):
                in_str = None
            j -= 1
            continue
        if c in ('"', "'"):
            in_str = c
            j -= 1
            continue
        if c in ")]}":
            depth += 1
        elif c in "([{":
            if depth == 0:
                return j + 1, end
            depth -= 1
        elif depth == 0 and c in " \t":
            # Stop before statement/type keywords: ``return x ?: y``, ``new Foo()?.x``
            k = j
            while k >= 0 and source[k] in " \t":
                k -= 1
            if k >= 0 and _is_ident_char(source[k]):
                id_end = k + 1
                while k >= 0 and _is_ident_char(source[k]):
                    k -= 1
                ident = source[k + 1 : id_end]
                if ident in _LEFT_STOP_KEYWORDS:
                    return j + 1, end
        elif depth == 0 and c == "?":
            # Ternary ``?`` or start of ``?.`` / ``?:`` — never cross these.
            return j + 1, end
        elif depth == 0 and c == ">" and j > 0 and source[j - 1] == "-":
            # Closure arrow ``->``: ``{ a, b -> b?.x }`` left operand is ``b``.
            return j + 1, end
        elif depth == 0 and c in "=,;:\n{}":
            # Hard-stop after elvis ``?:`` so safe-nav on the right does not
            # treat ``: left`` as its operand (``a ?: b?.c``).
            if c == ":" and j > 0 and source[j - 1] == "?":
                return j + 1, end
            return j + 1, end
        j -= 1
    return 0, end


def _is_ident_char(c: str) -> bool:
    return c.isalnum() or c == "_"


def _expr_bounds_right(source: str, start: int) -> tuple[int, int]:
    """Return ``[expr_start, expr_end)`` covering the expression right of an operator."""
    n = len(source)
    i = start
    while i < n and source[i] in " \t":
        i += 1
    depth = 0
    in_str: str | None = None
    j = i
    while j < n:
        c = source[j]
        if in_str:
            if c == "\\" and j + 1 < n:
                j += 2
                continue
            if c == in_str:
                in_str = None
            j += 1
            continue
        if c in ('"', "'"):
            in_str = c
            j += 1
            continue
        if c in "([{":
            depth += 1
        elif c in ")]}":
            if depth == 0:
                return i, j
            depth -= 1
        elif depth == 0 and c == "/" and j + 1 < n and source[j + 1] == "/":
            # Stop before EOL comments (elvis right of ``?: "x" // lint``).
            return i, j
        elif depth == 0 and c in ",;\n":
            return i, j
        j += 1
    return i, n


def _match_balanced(source: str, open_index: int, open_ch: str, close_ch: str) -> int:
    if open_index >= len(source) or source[open_index] != open_ch:
        return -1
    depth = 0
    in_str: str | None = None
    i = open_index
    while i < len(source):
        c = source[i]
        if in_str:
            if c == "\\" and i + 1 < len(source):
                i += 2
                continue
            if c == in_str:
                in_str = None
            i += 1
            continue
        if c in ('"', "'"):
            in_str = c
        elif c == open_ch:
            depth += 1
        elif c == close_ch:
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _at_string_or_comment(source: str, i: int) -> bool:
    if i >= len(source):
        return False
    if source.startswith("//", i) or source.startswith("/*", i):
        return True
    if source.startswith('"""', i) or source.startswith("'''", i):
        return True
    return source[i] in ('"', "'")


def _skip_string_or_comment(source: str, i: int) -> int:
    n = len(source)
    if source.startswith("//", i):
        j = source.find("\n", i)
        return n if j < 0 else j
    if source.startswith("/*", i):
        j = source.find("*/", i + 2)
        return n if j < 0 else j + 2
    if source.startswith('"""', i) or source.startswith("'''", i):
        q = source[i : i + 3]
        j = i + 3
        while j < n:
            if source.startswith(q, j):
                return j + 3
            j += 1
        return n
    q = source[i]
    j = i + 1
    while j < n:
        if source[j] == "\\" and j + 1 < n:
            j += 2
            continue
        if source[j] == q:
            return j + 1
        j += 1
    return n


def _escaped_at(source: str, index: int) -> bool:
    """True if ``source[index]`` is preceded by an odd number of backslashes."""
    bs = 0
    k = index - 1
    while k >= 0 and source[k] == "\\":
        bs += 1
        k -= 1
    return bs % 2 == 1
