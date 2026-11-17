"""Transform Groovy expressions to Python equivalents.

Handles: Elvis (?:), safe navigation (?.), GString -> f-string,
regex (==~), instanceof, as cast, map/list literals, closures.
"""

from __future__ import annotations

import re

from tektonize.parser.normalize import expand_normalize_sentinels


class ExpressionTransformer:
    """Transform Groovy expression syntax to Python."""

    def transform(self, text: str) -> str:
        """Apply all expression transformations to a text block."""
        # Undo pre-parse normalize sentinels first (elvis / safe-nav / empty map).
        result = expand_normalize_sentinels(text)

        result = self._transform_new_keyword(result)
        result = self._transform_string_interpolation(result)
        result = self._transform_elvis_operator(result)
        result = self._transform_safe_navigation(result)
        result = self._transform_instanceof(result)
        result = self._transform_ternary_operator(result)
        result = self._transform_regex_match(result)
        result = self._transform_as_cast(result)
        result = self._transform_map_literals(result)
        result = self._transform_list_literals(result)
        # Quote bare keys in ``{ ident: val }`` brace maps (not only ``[ident:]``).
        result = self._quote_brace_map_keys(result)
        # After maps are ``{…}``, rewrite map ``<<`` merges before list append.
        result = self._transform_map_left_shift(result)
        # Empty-list left-shift: ``[] << x`` → ``[x]`` (Groovy append to new list).
        result = re.sub(
            r"\[\s*\]\s*<<\s*(\w+(?:\.\w+)*(?:\([^)]*\))?|'[^']*'|\"[^\"]*\")",
            r"[\1]",
            result,
        )
        # After maps are ``{…}``, rewrite ``func(name: value)`` → kwargs.
        result = self._transform_named_args(result)
        result = self._transform_null_keyword(result)
        result = self._transform_boolean_keywords(result)
        result = self._transform_this_to_self(result)
        # Jenkins ``script.env`` / ``currentBuild`` → Pipeline global bindings.
        result = self._transform_script_bindings(result)
        result = self._transform_string_methods(result)
        result = self._transform_collection_methods(result)
        result = self._transform_groovy_operators(result)

        return result

    def _transform_script_bindings(self, text: str) -> str:
        """Rewrite ``(self.)?script.env`` / ``currentBuild`` to Jenkins globals.

        Aligns with ``jenkins_bindings`` policy: live ``env.FOO`` / ``env[…]`` /
        ``currentBuild`` attribute access (no value substitution).
        """
        result = re.sub(
            r"\b(?:self\.)?script\.env\.([A-Za-z_][\w]*)",
            r"env.\1",
            text,
        )
        result = re.sub(r"\b(?:self\.)?script\.env\[", "env[", result)
        result = re.sub(
            r"\b(?:self\.)?script\.currentBuild\b",
            "currentBuild",
            result,
        )
        return result

    def _quote_brace_map_keys(self, text: str) -> str:
        """Quote bare keys inside ``{ ident: value }`` dict/map literals.

        Bracket maps are already rewritten to ``{…}`` with quoted keys; native
        Groovy / half-converted ``{ deploymentType: X }`` still needs quoting.
        String-aware and brace-balanced.
        """
        out: list[str] = []
        i = 0
        n = len(text)
        while i < n:
            if self._at_string_or_comment(text, i):
                j = self._skip_string_or_comment(text, i)
                out.append(text[i:j])
                i = j
                continue
            if text[i] == "{":
                close = self._match_balanced(text, i, "{", "}")
                if close < 0:
                    out.append(text[i])
                    i += 1
                    continue
                inner = text[i + 1 : close]
                # Recurse into nested braces first, then quote keys at this level.
                inner = self._quote_brace_map_keys(inner)
                if self._looks_like_brace_map_inner(inner):
                    inner = self._quote_map_keys(inner)
                out.append("{" + inner + "}")
                i = close + 1
                continue
            out.append(text[i])
            i += 1
        return "".join(out)

    @staticmethod
    def _looks_like_brace_map_inner(inner: str) -> bool:
        """True if *inner* looks like a map (starts with ``key:`` / ``'key':``).

        Requires a map entry at the start so control bodies (``{ for x in y:``)
        and merges (``{**a, **b}``) are not rewritten.
        """
        stripped = inner.strip()
        if not stripped or stripped.startswith(("*", "#")):
            return False
        if re.match(
            r"^(?:if|for|while|try|catch|else|def|class|return|assert|switch|"
            r"stage|script|steps|always|post|stages)\b",
            stripped,
        ):
            return False
        return bool(
            re.match(
                r"([A-Za-z_]\w*|'[^']*'|\"[^\"]*\")\s*:",
                stripped,
            )
        )

    def _transform_new_keyword(self, text: str) -> str:
        """Convert 'new ClassName(args)' to 'ClassName(args)'."""
        result = re.sub(r"\bnew\s+(\w+)\s*\(", r"\1(", text)
        # Also handle: new ClassName() without args
        result = re.sub(r"\bnew\s+(\w+)\s*\(\)", r"\1()", result)
        return result

    def _transform_string_interpolation(self, text: str) -> str:
        """Convert GString "${var}" and "$var" to f-string f"{var}".

        Shell-escaped ``\\${VAR}`` / ``\\$VAR`` (common in ``sh`` bodies) must
        **not** become ``\\{VAR}`` — that is an invalid Python escape and emits
        ``SyntaxWarning``.  Those stay as plain ``${VAR}`` for the shell.

        Triple-quoted ``\"\"\"…\"\"\"`` / ``'''…'''`` blocks are left untouched:
        the double-quote rewriter would otherwise match the empty ``\"\"`` prefix
        of ``\"\"\"`` and emit mangled ``script=\"\"f\"`` debris.
        """

        def replace_gstring(m: re.Match) -> str:
            content = m.group(1)
            # Real GString interpolation: ``$ident`` or ``${…}`` (not regex ``…$``).
            if re.search(r"(?<!\\)\$\{", content) or re.search(r"(?<!\\)\$[A-Za-z_]", content):
                content = re.sub(r"(?<!\\)\$\{([^}]+)\}", r"{\1}", content)
                content = re.sub(r"(?<!\\)\$(\w+)", r"{\1}", content)
                # Remaining shell escapes inside a GString → literal ``${…}``.
                content = re.sub(r"\\\$\{([^}]+)\}", r"${{\1}}", content)
                content = re.sub(r"\\\$(\w+)", r"$\1", content)
                return f'f"{content}"'
            if "$" in content:
                # Only escaped dollars: drop the Groovy escape for the shell.
                content = content.replace("\\${", "${")
                content = re.sub(r"\\\$(\w+)", r"$\1", content)
                return f'"{content}"'
            return f'"{content}"'

        # Protect triple-quoted regions so ``"""`` is not eaten as ``""`` + ``"``.
        chunks: list[str] = []
        i = 0
        n = len(text)
        while i < n:
            if text.startswith('"""', i) or text.startswith("'''", i):
                quote = text[i : i + 3]
                j = i + 3
                while j < n:
                    if text.startswith(quote, j):
                        j += 3
                        break
                    if text[j] == "\\" and j + 1 < n:
                        j += 2
                        continue
                    j += 1
                chunks.append(text[i:j])
                i = j
                continue
            # Already an f-string — do not wrap again (prevents ``ff"…``).
            if text.startswith('f"', i) or text.startswith("f'", i):
                quote = text[i + 1]
                j = i + 2
                while j < n:
                    if text[j] == "\\" and j + 1 < n:
                        j += 2
                        continue
                    if text[j] == quote:
                        j += 1
                        break
                    j += 1
                chunks.append(text[i:j])
                i = j
                continue
            # Single-line double-quoted GStrings only (outside triple quotes).
            m = re.match(r'"((?:[^"\\]|\\.)*?)"', text[i:])
            if m:
                chunks.append(replace_gstring(m))
                i += m.end()
                continue
            chunks.append(text[i])
            i += 1
        return "".join(chunks)

    def _transform_elvis_operator(self, text: str) -> str:
        """Convert x ?: default to ``x or default`` (before ternary sees ``?``)."""
        result = text
        # Empty-map default must run before ternary (``?: [:]`` contains ``?``).
        result = re.sub(r"\?:\s*\[:\]", " or {}", result)

        # Parenthesized LHS: (expr) ?: default
        out: list[str] = []
        i = 0
        n = len(result)
        while i < n:
            if result[i] == "(":
                close = self._match_balanced(result, i, "(", ")")
                if close >= 0:
                    rest = result[close + 1 :]
                    m = re.match(r"\s*\?:\s*(\S+)", rest)
                    if m:
                        lhs = result[i : close + 1]
                        default = m.group(1)
                        if default == "[:]":
                            default = "{}"
                        out.append(f"({lhs} or {default})")
                        i = close + 1 + m.end()
                        continue
            out.append(result[i])
            i += 1
        result = "".join(out)

        # Simple cases: var ?: 'default' / call.chain ?: x
        result = re.sub(
            r"(\w[\w.]*)\s*\?:\s*(\S+)",
            r"\1 or \2",
            result,
        )
        # call(...) ?: default (no outer parens around the call)
        result = re.sub(
            r"(\w[\w.]*(?:\([^)]*\))+)\s*\?:\s*(\S+)",
            r"(\1 or \2)",
            result,
        )
        return result

    def _transform_safe_navigation(self, text: str) -> str:
        """Convert x?.property to getattr(x, 'property', None)."""
        # Simple: obj?.prop -> getattr(obj, 'prop', None)
        result = re.sub(
            r"(\w+)\?\.\s*(\w+)",
            r"getattr(\1, '\2', None)",
            text,
        )
        return result

    def _transform_instanceof(self, text: str) -> str:
        """Convert x instanceof Type to isinstance(x, Type)."""
        # Match: expr instanceof Type — handles dotted expressions
        result = re.sub(
            r"([\w.]+)\s+instanceof\s+(\w+)",
            r"isinstance(\1, \2)",
            text,
        )
        # Handle negated: !(x instanceof Type) -> not isinstance(x, Type)
        result = re.sub(
            r"!\s*\(\s*isinstance\(([^)]+)\)\s*\)",
            r"not isinstance(\1)",
            result,
        )
        # Common Groovy collection types in isinstance checks
        result = re.sub(r"isinstance\(([^,]+),\s*Map\)", r"isinstance(\1, dict)", result)
        result = re.sub(r"isinstance\(([^,]+),\s*List\)", r"isinstance(\1, list)", result)
        return result

    def _transform_ternary_operator(self, text: str) -> str:
        """Convert Groovy ternary ``cond ? a : b`` to ``a if cond else b``.

        Assignment forms ``lhs = cond ? a : b`` become
        ``lhs = (a if cond else b)`` so the ``=`` is not swallowed into the
        condition (which produced invalid ``a if lhs = cond else b``).

        Skips arms that contain typed decls / ``return`` / block braces — those
        become ``# TEKTONIZE-TODO: ternary-with-decls`` in MethodTransformer.
        """
        # Elvis (?:) is handled separately; skip lines that only use elvis.
        if "?" not in text:
            return text

        def _arm_is_complex(arm: str) -> bool:
            # Typed decls / return / blocks — not ``as Map`` casts (handled later).
            return bool(
                "{" in arm
                or re.search(r"\breturn\b", arm)
                or re.search(
                    r"\b(def|String|Map|Boolean|Integer|List)\s+\w+",
                    arm,
                )
            )

        def _split_assign_cond(cond: str) -> tuple[str | None, str]:
            """If ``cond`` is ``lhs = real_cond``, return (lhs, real_cond)."""
            m = re.match(r"^(.+?)(?<![=!<>])=(?!=)\s*(.+)$", cond)
            if not m:
                return None, cond
            lhs, real = m.group(1).strip(), m.group(2).strip()
            # Reject if LHS looks like a comparison fragment.
            if not lhs or re.search(r"[<>!]=?$", lhs):
                return None, cond
            return lhs, real

        def repl(match: re.Match[str]) -> str:
            cond = match.group(1).strip()
            then_expr = match.group(2).strip()
            else_expr = match.group(3).strip()
            lhs, real_cond = _split_assign_cond(cond)
            if _arm_is_complex(then_expr) or _arm_is_complex(else_expr):
                # Sentinel for MethodTransformer (not valid Python on purpose).
                return f"__TEKTONIZE_TERNARY_TODO__({real_cond!r})"
            body = f"({then_expr} if {real_cond} else {else_expr})"
            if lhs is not None:
                return f"{lhs} = {body}"
            return f"{then_expr} if {real_cond} else {else_expr}"

        # Match per line; elvis (?:) and safe-nav (?.) are already rewritten above.
        return re.sub(
            r"([^\n]+?)(?<!\?)\?(?![?.])\s*([^\n]+?)\s*:\s*([^\n]+)",
            repl,
            text,
        )

    def _transform_regex_match(self, text: str) -> str:
        """Convert ``x ==~ /pattern/`` / ``x ==~ "…"`` to ``re.match(…)``."""
        # Dotted LHS (params.BUILD_NVR) — ``\w+`` alone would only match the tail.
        result = re.sub(
            r"([\w.]+)\s*==~\s*/([^/]+)/",
            r're.match(r"\2", \1)',
            text,
        )
        result = re.sub(
            r"([\w.]+)\s*==~\s*'((?:[^'\\]|\\.)*)'",
            r're.match(r"\2", \1)',
            result,
        )
        result = re.sub(
            r'([\w.]+)\s*==~\s*"((?:[^"\\]|\\.)*)"',
            r're.match(r"\2", \1)',
            result,
        )
        # After GString: ==~ f"pattern"
        result = re.sub(
            r'([\w.]+)\s*==~\s*f"((?:[^"\\]|\\.)*)"',
            r're.match(r"\2", \1)',
            result,
        )
        return result

    def _transform_as_cast(self, text: str) -> str:
        """Convert 'x as Type' to Type(x)."""
        # Be careful not to transform 'as' in import statements
        result = re.sub(
            r"(\w[\w.]*)\s+as\s+(List|Map|Set|String|Integer|Boolean|int|boolean|float|double)\b",
            lambda m: f"{self._map_cast_type(m.group(2))}({m.group(1)})",
            text,
        )
        return result

    def _transform_map_literals(self, text: str) -> str:
        """Convert Groovy map literals ``[key: value]`` to Python ``{key: value}``.

        Handles single-line, multi-line, and nested maps with identifier or
        string keys (e.g. ``['ui-controller': [recordKeyCredId: 'x']]``).
        """
        result = text.replace("[:]", "{}")
        result = self._rewrite_bracket_maps(result)
        return result

    def _rewrite_bracket_maps(self, text: str) -> str:
        """Rewrite Groovy ``[key: …]`` maps (possibly nested / multiline) to dicts."""
        out: list[str] = []
        i = 0
        n = len(text)
        while i < n:
            if self._at_string_or_comment(text, i):
                j = self._skip_string_or_comment(text, i)
                out.append(text[i:j])
                i = j
                continue
            if text[i] == "[" and self._looks_like_map_start(text, i):
                close = self._match_balanced(text, i, "[", "]")
                if close < 0:
                    out.append(text[i])
                    i += 1
                    continue
                inner = self._rewrite_bracket_maps(text[i + 1 : close])
                inner = self._quote_map_keys(inner)
                out.append("{" + inner + "}")
                i = close + 1
                continue
            out.append(text[i])
            i += 1
        return "".join(out)

    @staticmethod
    def _looks_like_map_start(text: str, open_index: int) -> bool:
        """True if ``[`` at *open_index* begins a ``[key: …]`` map literal.

        Keys may be bare identifiers or quoted strings (including hyphenated
        names such as ``'ui-controller'``).
        """
        j = open_index + 1
        n = len(text)
        while j < n and text[j] in " \t\r\n":
            j += 1
        if j >= n:
            return False
        m = re.match(
            r"([A-Za-z_]\w*|'[^']*'|\"[^\"]*\")\s*:",
            text[j:],
        )
        return m is not None

    @staticmethod
    def _quote_map_keys(inner: str) -> str:
        """Quote bare map keys: ``secretKeyCredId:`` → ``\"secretKeyCredId\":``.

        Already-quoted string keys (``'ui-controller':``) are left unchanged.
        """
        # Entry starts at beginning, after ``{``, ``,``, or newline (with indent).
        return re.sub(
            r"(^|[{,\n])(\s*)([A-Za-z_]\w*)\s*:",
            r'\1\2"\3":',
            inner,
        )

    @staticmethod
    def _at_string_or_comment(text: str, i: int) -> bool:
        if i >= len(text):
            return False
        if text.startswith("//", i) or text.startswith("/*", i):
            return True
        if text.startswith('"""', i) or text.startswith("'''", i):
            return True
        return text[i] in ('"', "'")

    @staticmethod
    def _skip_string_or_comment(text: str, i: int) -> int:
        n = len(text)
        if text.startswith("//", i):
            j = i + 2
            while j < n and text[j] != "\n":
                j += 1
            return j
        if text.startswith("/*", i):
            j = text.find("*/", i + 2)
            return n if j < 0 else j + 2
        if text.startswith('"""', i) or text.startswith("'''", i):
            quote = text[i : i + 3]
            j = i + 3
            while j < n:
                if text.startswith(quote, j):
                    return j + 3
                if text[j] == "\\" and j + 1 < n:
                    j += 2
                    continue
                j += 1
            return n
        quote = text[i]
        j = i + 1
        while j < n:
            if text[j] == "\\" and j + 1 < n:
                j += 2
                continue
            if text[j] == quote:
                return j + 1
            j += 1
        return n

    @staticmethod
    def _match_balanced(text: str, open_index: int, open_ch: str, close_ch: str) -> int:
        if open_index >= len(text) or text[open_index] != open_ch:
            return -1
        depth = 0
        in_str: str | None = None
        i = open_index
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
            if text.startswith("//", i):
                while i < n and text[i] != "\n":
                    i += 1
                continue
            if text.startswith("/*", i):
                end = text.find("*/", i + 2)
                i = n if end < 0 else end + 2
                continue
            # Triple quotes before single-char quotes (``"""`` ≠ empty ``""``).
            if text.startswith('"""', i) or text.startswith("'''", i):
                quote = text[i : i + 3]
                i += 3
                while i < n:
                    if text.startswith(quote, i):
                        i += 3
                        break
                    if text[i] == "\\" and i + 1 < n:
                        i += 2
                        continue
                    i += 1
                continue
            if c in ('"', "'"):
                in_str = c
                i += 1
                continue
            if c == open_ch:
                depth += 1
            elif c == close_ch:
                depth -= 1
                if depth == 0:
                    return i
            i += 1
        return -1

    def _transform_list_literals(self, text: str) -> str:
        """Groovy list literals [] are already Python list syntax."""
        return text

    def _transform_named_args(self, text: str) -> str:
        """Rewrite Groovy named args inside ``(…)`` to Python kwargs.

        ``choice(name: 'X', choices: ['a'])`` → ``choice(name='X', choices=['a'])``.
        Dict entries inside ``{…}`` are left alone (already map→dict).
        """
        out: list[str] = []
        i = 0
        n = len(text)
        while i < n:
            if self._at_string_or_comment(text, i):
                j = self._skip_string_or_comment(text, i)
                out.append(text[i:j])
                i = j
                continue
            if text[i] == "(":
                close = self._match_balanced(text, i, "(", ")")
                if close < 0:
                    out.append(text[i])
                    i += 1
                    continue
                inner = self._transform_named_args(text[i + 1 : close])
                inner = self._rewrite_named_args_in_call(inner)
                out.append("(" + inner + ")")
                i = close + 1
                continue
            out.append(text[i])
            i += 1
        return "".join(out)

    def _rewrite_named_args_in_call(self, inner: str) -> str:
        """Convert ``ident: value`` to ``ident=value`` at call-arg depth 0."""
        out: list[str] = []
        i = 0
        n = len(inner)
        at_boundary = True
        depth_paren = depth_bracket = depth_brace = 0
        while i < n:
            if self._at_string_or_comment(inner, i):
                j = self._skip_string_or_comment(inner, i)
                out.append(inner[i:j])
                i = j
                at_boundary = False
                continue
            c = inner[i]
            if depth_paren == 0 and depth_bracket == 0 and depth_brace == 0:
                if c == "\n":
                    out.append(c)
                    i += 1
                    at_boundary = True
                    continue
                if at_boundary:
                    m = re.match(r"([A-Za-z_]\w*)\s*:\s*", inner[i:])
                    if m:
                        out.append(m.group(1) + "=")
                        i += m.end()
                        at_boundary = False
                        continue
                if c == ",":
                    out.append(c)
                    i += 1
                    at_boundary = True
                    continue
                if c.isspace():
                    out.append(c)
                    i += 1
                    continue
            if c == "(":
                depth_paren += 1
            elif c == ")":
                depth_paren = max(0, depth_paren - 1)
            elif c == "[":
                depth_bracket += 1
            elif c == "]":
                depth_bracket = max(0, depth_bracket - 1)
            elif c == "{":
                depth_brace += 1
            elif c == "}":
                depth_brace = max(0, depth_brace - 1)
            out.append(c)
            i += 1
            at_boundary = False
        return "".join(out)

    def _transform_null_keyword(self, text: str) -> str:
        """Convert null to None."""
        return re.sub(r"\bnull\b", "None", text)

    def _transform_boolean_keywords(self, text: str) -> str:
        """Convert true/false to True/False."""
        result = re.sub(r"\btrue\b", "True", text)
        result = re.sub(r"\bfalse\b", "False", result)
        return result

    def _transform_this_to_self(self, text: str) -> str:
        """Convert ``this.x`` and bare ``this`` to ``self``."""
        result = re.sub(r"\bthis\.", "self.", text)
        result = re.sub(r"\bthis\b", "self", result)
        return result

    def _transform_string_methods(self, text: str) -> str:
        """Convert common Groovy string methods to Python."""
        # .startsWith -> .startswith
        result = text.replace(".startsWith(", ".startswith(")
        result = result.replace(".endsWith(", ".endswith(")
        result = result.replace(".toLowerCase()", ".lower()")
        result = result.replace(".toUpperCase()", ".upper()")
        result = result.replace(".toString()", "")
        result = result.replace(".trim()", ".strip()")
        result = result.replace(".isEmpty()", " == ''")
        result = result.replace(".length()", ".__len__()")
        result = re.sub(r"\.size\(\)", ".__len__()", result)
        result = result.replace(".contains(", ".__contains__(")
        return result

    # Shell / builtin names that must never be treated as map-merge LHS.
    _MAP_PLUS_SKIP_LHS = frozenset(
        {
            "set",
            "export",
            "echo",
            "source",
            "cd",
            "exit",
            "return",
            "unset",
            "alias",
            "eval",
            "exec",
            "printf",
            "read",
            "shift",
            "test",
            "trap",
            "umask",
            "wait",
        }
    )

    def _transform_map_plus(self, text: str) -> str:
        """Rewrite Groovy map ``+`` merges to ``{**a, **b}`` (not string concat).

        Requires whitespace on **both** sides of ``+`` so shell ``set +e`` is
        never rewritten. Skips shell-builtin LHS names and shell-like lines.
        """
        # Whole-line shell fragments (after multiline sh was split).
        stripped = text.lstrip()
        if re.match(
            r"^(set|export|source|echo|cd|exit|unset)\b",
            stripped,
        ):
            return text

        out: list[str] = []
        i = 0
        n = len(text)
        # Whitespace required on both sides of ``+`` (blocks ``set +e``).
        # Also ``{} + map`` empty-dict merge.
        empty_plus = re.compile(r"\{\s*\}\s+\+\s+")
        ident_plus = re.compile(r"(\w+(?:\.\w+)*)\s+\+\s+")
        while i < n:
            if self._at_string_or_comment(text, i):
                j = self._skip_string_or_comment(text, i)
                out.append(text[i:j])
                i = j
                continue
            m_empty = empty_plus.match(text, i)
            if m_empty:
                j = m_empty.end()
                if j < n and text[j] == "{":
                    close = self._match_balanced(text, j, "{", "}")
                    if close >= 0:
                        inner = text[j : close + 1]
                        out.append(f"{{**{{}}, **{inner}}}")
                        i = close + 1
                        continue
                m2 = re.match(r"(\w+(?:\.\w+)*)\b(?!\s*[\(\[.])", text[j:])
                if m2:
                    right = m2.group(1)
                    out.append(f"{{**{{}}, **{right}}}")
                    i = j + m2.end()
                    continue
            m = ident_plus.match(text, i)
            if m and (i == 0 or text[i - 1] not in "'\""):
                left = m.group(1)
                root = left.split(".", 1)[0]
                if root in self._MAP_PLUS_SKIP_LHS:
                    out.append(text[i])
                    i += 1
                    continue
                # Numeric / index ``i + 1`` / ``pass + fail`` — keep arithmetic ``+``.
                if re.fullmatch(r"[ijkmn]|idx|index|count|total", root) or re.search(
                    r"\.(?:pass|fail|skip|total|count|Count|size|length)\b",
                    left,
                ):
                    out.append(text[i])
                    i += 1
                    continue
                j = m.end()
                if j < n and text[j] == "{":
                    close = self._match_balanced(text, j, "{", "}")
                    if close >= 0:
                        inner = text[j : close + 1]
                        out.append(f"{{**{left}, **{inner}}}")
                        i = close + 1
                        continue
                # Numeric literal RHS: ``x + 1`` must stay arithmetic.
                if re.match(r"\d+", text[j:]):
                    out.append(text[i])
                    i += 1
                    continue
                m2 = re.match(r"(\w+(?:\.\w+)*)\b(?!\s*[\(\[.])", text[j:])
                if m2:
                    right = m2.group(1)
                    if re.search(
                        r"\.(?:pass|fail|skip|total|count|Count|size|length)\b",
                        right,
                    ):
                        out.append(text[i])
                        i += 1
                        continue
                    out.append(f"{{**{left}, **{right}}}")
                    i = j + m2.end()
                    continue
            out.append(text[i])
            i += 1
        return "".join(out)

    def _transform_collection_methods(self, text: str) -> str:
        """Convert Groovy collection methods to Python."""
        result = text
        # map.containsKey(k) → (k in map)
        result = re.sub(
            r"(\w+(?:\.\w+)*)\.containsKey\s*\(([^)]+)\)",
            r"(\2 in \1)",
            result,
        )
        # map.getOrDefault(k, d) → map.get(k, d)
        result = re.sub(
            r"(\w+(?:\.\w+)*)\.getOrDefault\s*\(",
            r"\1.get(",
            result,
        )
        # map.putAll(other) → map.update(other)
        result = re.sub(
            r"(\w+(?:\.\w+)*)\.putAll\s*\(",
            r"\1.update(",
            result,
        )
        # items.each { x -> / items.each { String x -> → for x in items:
        result = re.sub(
            r"(\S+)\.each\s*\{\s*(?:(?:String|def|int|Integer|Object)\s+)?(\w+)\s*->\s*",
            r"for \2 in \1:",
            result,
        )
        # map.each { k, v -> → for k, v in map.items():
        result = re.sub(
            r"(\S+)\.each\s*\{\s*(\w+)\s*,\s*(\w+)\s*->\s*",
            r"for \2, \3 in \1.items():",
            result,
        )
        # coll.collect { "…" / f"…" }.join(sep) — string body (brace-safe).
        result = re.sub(
            r"(\w[\w.]*)\.collect\s*\{\s*(f?(?:\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*'))"
            r"\s*\}\s*\.join\s*\(([^)]+)\)",
            r"\3.join(\2 for it in \1)",
            result,
        )
        # coll.collect { it.prop }.join(sep) → sep.join(it.prop for it in coll)
        result = re.sub(
            r"(\w[\w.]*)\.collect\s*\{\s*([^}\"']+?)\s*\}\s*\.join\s*\(([^)]+)\)",
            r"\3.join(\2 for it in \1)",
            result,
        )
        result = re.sub(r"\.collect\s*\{", ".map(lambda it:", result)
        # Close ``.map(lambda it: …}`` without eating f-string ``{it}``.
        result = self._close_map_lambda_braces(result)
        # Half-emitted join after map lambda.
        result = re.sub(
            r"(\w[\w.]*)\.map\(lambda it:\s*(.+?)\)\s*\.join\s*\(([^)]+)\)",
            r"\3.join(\2 for it in \1)",
            result,
        )

        # map.findAll { k, v -> cond } / .filter { k, v -> cond } → dict comp
        def _map_filter_kv(m: re.Match[str]) -> str:
            recv, k, v, cond = m.group(1), m.group(2), m.group(3), m.group(4).strip()
            return f"{{{k}: {v} for {k}, {v} in {recv}.items() if ({cond})}}"

        result = re.sub(
            r"(\w[\w.]*)\.(?:findAll|filter)\s*\{\s*(\w+)\s*,\s*(\w+)\s*->\s*([^}]+)\}",
            _map_filter_kv,
            result,
        )
        result = re.sub(r"\.findAll\s*\{", ".filter(lambda it:", result)

        # xs.find { pred } / xs.find { it -> pred } → next((x for x in xs if pred), None)
        def _find_one(m: re.Match[str]) -> str:
            recv, pred = m.group(1), m.group(2).strip()
            pred = re.sub(r"\bit\b", "x", pred)
            return f"next((x for x in {recv} if ({pred})), None)"

        result = re.sub(
            r"(\w[\w.]*)\.find\s*\{\s*(?:(?:def|String|Map|Object)\s+)?"
            r"(?:it\s*->\s*)?([^}]+)\}",
            _find_one,
            result,
        )
        # Half-emitted residue: ``Namenext((x for x in`` → repair or leave for TODO
        result = re.sub(
            r"(\w[\w.]*)next\(\(x for x in\s*",
            r"next((x for x in \1 if ",
            result,
        )
        # collectEntries { … } — sentinel for MethodTransformer single TODO
        result = re.sub(
            r"(\w[\w.]*)\.collectEntries\s*\{",
            r"__TEKTONIZE_COLLECT_ENTRIES__(\1){{",
            result,
        )
        result = result.replace(".keySet()", ".keys()")
        result = result.replace(".toList()", "")
        result = result.replace(".sort()", ".sort()")
        result = result.replace(".unique()", "")
        return result

    def _close_map_lambda_braces(self, text: str) -> str:
        """Turn ``.map(lambda it: EXPR}`` into ``.map(lambda it: EXPR)`` string-aware.

        Does not treat ``}`` inside f-strings / quotes as the Groovy closer
        (avoids ``f\"…/{it)\"`` corruption).
        """
        out: list[str] = []
        i = 0
        n = len(text)
        marker = ".map(lambda it:"
        while i < n:
            if self._at_string_or_comment(text, i):
                j = self._skip_string_or_comment(text, i)
                out.append(text[i:j])
                i = j
                continue
            if text.startswith(marker, i):
                out.append(marker)
                i += len(marker)
                # Scan body until Groovy ``}`` outside strings.
                body_chars: list[str] = []
                while i < n:
                    if self._at_string_or_comment(text, i):
                        j = self._skip_string_or_comment(text, i)
                        body_chars.append(text[i:j])
                        i = j
                        continue
                    if text[i] == "}":
                        body = "".join(body_chars).rstrip()
                        out.append(f" {body})" if not body.startswith(" ") else f"{body})")
                        i += 1
                        break
                    body_chars.append(text[i])
                    i += 1
                else:
                    out.append("".join(body_chars))
                continue
            out.append(text[i])
            i += 1
        return "".join(out)

    def _transform_map_left_shift(self, text: str) -> str:
        """Rewrite Groovy map ``{…} << expr`` merges to ``{**{…}, **(expr)}``.

        Also ``ident << {…}`` → ``{**ident, **{…}}``. List ``ident << x`` append
        stays in ``_transform_groovy_operators``.
        """
        result = text
        # ident << {…}  (map RHS)
        out: list[str] = []
        i = 0
        n = len(result)
        ident_shift = re.compile(r"(\w+(?:\.\w+)*)\s*<<\s*\{")
        while i < n:
            if self._at_string_or_comment(result, i):
                j = self._skip_string_or_comment(result, i)
                out.append(result[i:j])
                i = j
                continue
            m = ident_shift.match(result, i)
            if m:
                left = m.group(1)
                brace_at = m.end() - 1
                close = self._match_balanced(result, brace_at, "{", "}")
                if close >= 0:
                    rhs = result[brace_at : close + 1]
                    out.append(f"{{**{left}, **{rhs}}}")
                    i = close + 1
                    continue
            out.append(result[i])
            i += 1
        result = "".join(out)

        # {…} << expr  (dict literal left-shift merge)
        out = []
        i = 0
        n = len(result)
        while i < n:
            if self._at_string_or_comment(result, i):
                j = self._skip_string_or_comment(result, i)
                out.append(result[i:j])
                i = j
                continue
            if result[i] == "{":
                close = self._match_balanced(result, i, "{", "}")
                if close >= 0:
                    rest = result[close + 1 :]
                    m = re.match(r"\s*<<\s*", rest)
                    if m:
                        rhs_start = close + 1 + m.end()
                        shift_rhs, shift_end = self._read_shift_rhs(result, rhs_start)
                        if shift_rhs is not None:
                            inner = result[i : close + 1]
                            # Collapse whitespace so MethodTransformer does not
                            # see a leading ``}, **(`` line (unmatched ``}``).
                            inner_flat = re.sub(r"\s+", " ", inner.strip())
                            rhs_flat = re.sub(r"\s+", " ", shift_rhs.strip())
                            out.append(f"{{**{inner_flat}, **({rhs_flat})}}")
                            i = shift_end
                            continue
            out.append(result[i])
            i += 1
        return "".join(out)

    def _read_shift_rhs(self, text: str, start: int) -> tuple[str | None, int]:
        """Read the RHS of a ``<<`` merge; returns (rhs, index_after) or (None, start)."""
        if start >= len(text):
            return None, start
        # Skip whitespace
        j = start
        while j < len(text) and text[j] in " \t":
            j += 1
        if j >= len(text):
            return None, start
        if text[j] == "(":
            close = self._match_balanced(text, j, "(", ")")
            if close < 0:
                return None, start
            return text[j : close + 1], close + 1
        if text[j] == "{":
            close = self._match_balanced(text, j, "{", "}")
            if close < 0:
                return None, start
            return text[j : close + 1], close + 1
        m = re.match(r"(\w+(?:\.\w+)*(?:\([^)]*\))*)", text[j:])
        if m:
            return m.group(1), j + m.end()
        return None, start

    def _transform_groovy_operators(self, text: str) -> str:
        """Convert remaining Groovy-specific syntax to Python."""
        result = text

        # list << f-string / string / ident → list.append(...) (maps already merged)
        result = re.sub(
            r"(\w+(?:\.\w+)*)\s*<<\s*(f?(?:'[^']*'|\"[^\"]*\")|\w+(?:\.\w+)*|\d+)\s*"
            r"(?=[;\n,}]|$)",
            r"\1.append(\2)",
            result,
        )
        # Trailing ``}`` noise after append rewrite: ``list.append(x) }`` → append only
        result = re.sub(
            r"(\w+(?:\.\w+)*)\.append\(([^)]+)\)\s*\}",
            r"\1.append(\2)",
            result,
        )
        # mapish << call(...) → map.update(call(...)) when RHS is a call
        result = re.sub(
            r"(\w+(?:\.\w+)*)\s*<<\s*(\w+(?:\.\w+)*\([^)]*\))",
            r"\1.update(\2)",
            result,
        )
        # x++ / x-- → x += 1 / x -= 1 (also subscript: total_counters[k]++)
        result = re.sub(
            r"\b(\w+(?:\.\w+)*(?:\[[^\]]+\])?)\+\+(?!\+)",
            r"\1 += 1",
            result,
        )
        result = re.sub(
            r"\b(\w+(?:\.\w+)*(?:\[[^\]]+\])?)--(?!-)",
            r"\1 -= 1",
            result,
        )
        # Groovy find operator ``=~`` → re.search (``==~`` already handled above).
        result = re.sub(
            r"([\w.]+)\s*(?<!=)=~\s*/([^/]+)/",
            r're.search(r"\2", \1)',
            result,
        )
        result = re.sub(
            r'([\w.]+)\s*(?<!=)=~\s*"((?:[^"\\]|\\.)*)"',
            r're.search(r"\2", \1)',
            result,
        )
        result = re.sub(
            r"([\w.]+)\s*(?<!=)=~\s*'((?:[^'\\]|\\.)*)'",
            r're.search(r"\2", \1)',
            result,
        )
        # Variable pattern: ``value =~ pattern_text``
        result = re.sub(
            r"([\w.]+)\s*(?<!=)=~\s*([\w.]+)\b",
            r"re.search(\2, \1)",
            result,
        )
        # Broken collect→join: ``.join(return f"…")`` / ``" ".join(return f…)``
        result = re.sub(r"\.join\(\s*return\s+", ".join(", result)
        result = re.sub(r"join\(\s*return\s+", "join(", result)

        # Groovy inclusive range slice ``x[0..-3]`` / ``x[1..4]`` → Python slices.
        def _range_slice(m: re.Match[str]) -> str:
            recv, start_s, end_s = m.group(1), m.group(2), m.group(3)
            start_i, end_i = int(start_s), int(end_s)
            if end_i < 0:
                py_end = "" if end_i == -1 else str(end_i + 1)
                return f"{recv}[{start_i}:{py_end}]"
            return f"{recv}[{start_i}:{end_i + 1}]"

        result = re.sub(
            r"(\w[\w.]*)\[(\d+)\.\.(-?\d+)\]",
            _range_slice,
            result,
        )
        # mapish += call(…) → .update(call(…))
        result = re.sub(
            r"\b(\w+(?:\.\w+)*)\s*\+=\s*(\w+(?:\.\w+)*\([^)]*\))",
            r"\1.update(\2)",
            result,
        )
        # mapA + {…} / mapA + mapB / {} + map → {**mapA, **…} (not string +)
        result = self._transform_map_plus(result)

        # !(...) -> not (...)
        result = re.sub(r"!\s*\(([^)]+)\)", r"not (\1)", result)
        # !identifier / !getattr(...) but not !=
        result = re.sub(r"!(?!=)\s*(?=\w)", "not ", result)

        # Boolean operators
        result = re.sub(r"&&", " and ", result)
        result = re.sub(r"\|\|", " or ", result)

        # Long/float suffixes: 1L, 2.0d → 1, 2.0
        result = re.sub(r"\b(\d+)[Ll]\b", r"\1", result)
        result = re.sub(r"\b(\d+\.\d+)[DdFf]\b", r"\1", result)

        # Groovy assert with colon message → Python assert with message
        # Handles plain strings, f-strings, and string concatenation after the colon.
        result = re.sub(
            r"\bassert\s+(.+?)\s*:\s*(f?\"(?:[^\"\\]|\\.)*\"|f?'(?:[^'\\]|\\.)*')",
            r"assert \1, \2",
            result,
        )

        # Groovy assert -> Python assert
        result = re.sub(r"^\s*assert\s+", "assert ", result)

        # .class -> .__class__
        result = re.sub(r"\.class\b(?!\s*:)", ".__class__", result)

        # .getClass() -> type()
        result = re.sub(r"(\w+)\.getClass\(\)", r"type(\1)", result)

        # println -> print
        result = re.sub(r"\bprintln\b", "print", result)

        # Closure braces wrongly left after .map/.filter/.collect lambdas
        result = re.sub(
            r"(lambda\s+\w+\s*:\s*[^\n{]*?)\s*\}(\s*\.\s*(?:collect|each|find|findAll|any|every|sum)\b)",
            r"\1)\2",
            result,
        )
        result = re.sub(r"(lambda\s+\w+\s*:\s*[^\n{]*?)\s*\}", r"\1)", result)

        # String concatenation with + (preserve as-is, works in Python too)
        # Remove semicolons
        result = result.rstrip(";")

        return result

    def _map_cast_type(self, groovy_type: str) -> str:
        """Map a Groovy cast type to Python."""
        mapping = {
            "List": "list",
            "Map": "dict",
            "Set": "set",
            "String": "str",
            "Integer": "int",
            "Boolean": "bool",
            "int": "int",
            "boolean": "bool",
            "float": "float",
            "double": "float",
        }
        return mapping.get(groovy_type, groovy_type)
